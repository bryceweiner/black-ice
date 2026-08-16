"""The conversation loop. Every user action -- typed or spoken -- goes through
here, and every step is written to llm_turns before the next one runs, so a
crash mid-loop still leaves a complete record.
"""

from __future__ import annotations

import contextlib
import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any

from .. import db
from ..config import get_settings
from ..models import Trust
from . import guard, prompts
from .client import LMStudioClient, message_text, user_message
from .client import client as default_client
from .tools import ToolRegistry
from .tools import registry as default_tools

log = logging.getLogger("blackice.harness")

MAX_ITERATIONS = 8
MAX_HISTORY = 40

REFUSAL = (
    "That request was rejected by the input filter and has been logged. "
    "If you meant it legitimately, rephrase it."
)


class Harness:
    def __init__(
        self,
        tools: ToolRegistry | None = None,
        client: LMStudioClient | None = None,
    ) -> None:
        self.tools = tools or default_tools
        self.client = client or default_client
        self._history: dict[str, list[dict[str, Any]]] = {}
        self._memory_block: dict[str, str] = {}

    def history(self, session_id: str) -> list[dict[str, Any]]:
        return self._history.setdefault(session_id, [])

    def reset(self, session_id: str) -> None:
        self._history.pop(session_id, None)
        self._memory_block.pop(session_id, None)

    async def system_prompt(self, session_id: str) -> str:
        """Active prompt plus recalled memory.

        The memory block is read once per session: it is a file scan, and it
        should not change mid-conversation anyway.
        """
        base = await prompts.active(prompts.SYSTEM)
        if session_id not in self._memory_block:
            from ..memory.store import memory

            self._memory_block[session_id] = await memory.startup_block()
        block = self._memory_block[session_id]
        return f"{base}\n\n## What you remember\n\n{block}" if block else base

    async def run(
        self,
        text: str,
        *,
        channel: str = "console",
        trust: Trust = Trust.USER,
        session_id: str | None = None,
        images: list[str | Path] | None = None,
        model: str | None = None,
    ) -> str:
        session_id = session_id or uuid.uuid4().hex
        s = get_settings()

        checked = await guard.inspect(text, trust=trust, channel=channel)
        await self._log(session_id, channel, "user", content=checked.normalized)
        if checked.blocked:
            await self._log(session_id, channel, "assistant", content=REFUSAL)
            return REFUSAL

        history = self.history(session_id)
        history.append(user_message(checked.text, images))

        messages = [
            {"role": "system", "content": await self.system_prompt(session_id)},
            *history[-MAX_HISTORY:],
        ]
        specs = self.tools.specs()
        model = model or s.model_primary

        for _ in range(MAX_ITERATIONS):
            started = time.monotonic()
            message = await self.client.chat(
                messages, model=model, tools=specs or None
            )
            latency = int((time.monotonic() - started) * 1000)
            calls = message.get("tool_calls") or []
            await self._log(
                session_id, channel, "assistant",
                content=message_text(message) or None, model=model,
                latency_ms=latency,
            )
            messages.append(message)
            history.append(message)

            if not calls:
                reply = message_text(message)
                await self._remember(checked.normalized, reply, trust)
                return reply

            for call in calls:
                result = await self._invoke(session_id, channel, call)
                tool_message = {
                    "role": "tool",
                    "tool_call_id": call.get("id", ""),
                    "content": db.dumps(result),
                }
                messages.append(tool_message)
                history.append(tool_message)

        log.warning("harness hit the %d-iteration cap", MAX_ITERATIONS)
        return "I got stuck working through that. Try asking more specifically."

    async def _remember(self, asked: str, reply: str, trust: Trust) -> None:
        """Feed the exchange to memory consolidation.

        Here rather than at each call site: console and voice both come through
        run(), and doing it per-caller means the next channel added silently
        never gets remembered.
        """
        from ..memory import consolidate

        with contextlib.suppress(Exception):
            await consolidate.record_turn(asked, reply, trust)

    async def _invoke(self, session_id: str, channel: str, call: dict) -> Any:
        fn = call.get("function", {})
        name = fn.get("name", "")
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except ValueError:
            args = {}
        started = time.monotonic()
        result = await self.tools.dispatch(name, args)
        await self._log(
            session_id, channel, "tool", tool_name=name,
            tool_args=db.dumps(args), tool_result=db.dumps(result),
            latency_ms=int((time.monotonic() - started) * 1000),
        )
        return result

    async def _log(
        self,
        session_id: str,
        channel: str,
        role: str,
        *,
        content: str | None = None,
        model: str | None = None,
        tool_name: str | None = None,
        tool_args: str | None = None,
        tool_result: str | None = None,
        latency_ms: int | None = None,
    ) -> None:
        await db.execute(
            """INSERT INTO llm_turns
                 (session_id, channel, role, model, content, tool_name,
                  tool_args, tool_result, latency_ms)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (session_id, channel, role, model, content, tool_name,
             tool_args, tool_result, latency_ms),
        )


harness = Harness()
