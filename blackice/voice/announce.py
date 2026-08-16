"""Speak a reminder when it comes due.

The clock plugin records a reminder as an event; this is the hop that turns
that event into speech. It exists because `Voice2Backend.say()` had no caller —
the assistant could answer, but never start a sentence of its own.

It polls the events table rather than subscribing to the bus, because the bus
is in-process and `blackice serve` and `blackice voice` are two processes. The
scheduler that claimed the reminder might be in either one; the event always
lands in the shared database, so watching that sees it either way.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from .. import db
from ..config import get_settings
from ..llm import prompts
from ..llm.client import client as default_client

log = logging.getLogger("blackice.announce")

POLL_SECONDS = 5.0
BATCH = 10

INSTRUCTION = """A reminder {owner} set has just come due, and you are speaking
to them unprompted rather than answering a question. Tell them what time it is
and what they asked to be reminded of, in one or two short sentences. Say it
directly, ask nothing, and call no tools."""


def fallback(payload: dict[str, Any]) -> str:
    """What gets said when the model is unreachable.

    A reminder that stays silent because LM Studio is down is a broken promise,
    so composition failure must still produce a sentence.
    """
    now = (payload.get("time_now") or "").strip()
    reason = (payload.get("reason") or "").strip()
    opening = f"It's {now}. " if now else ""
    if not reason:
        return f"{opening}You asked me to remind you about something."
    return f"{opening}You asked me to remind you: {reason}."


class Announcer:
    """Watches for due reminders and says them out loud."""

    def __init__(
        self, backend: Any, client: Any = None, poll_seconds: float = POLL_SECONDS
    ) -> None:
        self.backend = backend
        self.client = client or default_client
        self.poll_seconds = poll_seconds
        self.watermark = 0
        self.task: asyncio.Task | None = None

    async def start(self) -> None:
        # Begin at the newest event: restarting the voice loop must not replay
        # this morning's reminders down the speaker.
        self.watermark = await db.fetchval("SELECT COALESCE(max(id), 0) FROM events") or 0
        self.task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self.task:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
            self.task = None

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self.poll_seconds)
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("reminder announcement poll failed")

    async def poll_once(self) -> list[str]:
        """Announce every reminder recorded since the last look."""
        rows = await db.fetchall(
            "SELECT id, payload FROM events WHERE kind = 'reminder' AND id > ?"
            " ORDER BY id LIMIT ?",
            (self.watermark, BATCH),
        )
        said: list[str] = []
        for row in rows:
            self.watermark = max(self.watermark, row["id"])
            payload = db.loads(row["payload"]) or {}
            text = await self.compose(payload)
            try:
                await self.backend.say(text)
            except Exception:
                log.exception("could not speak reminder event %s", row["id"])
                continue
            said.append(text)
        return said

    async def compose(self, payload: dict[str, Any]) -> str:
        """Ask the model to phrase the announcement; fall back to a plain one.

        The reason is the owner's own words, captured through the guarded
        command channel, so it goes in as ordinary context — wrapping it as
        untrusted would tell the assistant to distrust the very message it was
        asked to deliver.
        """
        s = get_settings()
        try:
            system = await prompts.active(prompts.SYSTEM)
            message = await self.client.chat(
                [
                    {
                        "role": "system",
                        "content": f"{system}\n\n{INSTRUCTION.format(owner=s.owner_name)}",
                    },
                    {"role": "user", "content": self._facts(payload)},
                ],
                model=s.model_voice or None,
                temperature=0.3,
                max_tokens=120,
                # Without this the Qwen3 reasoning block eats the token budget
                # and `content` comes back empty, leaving message_text() to
                # return the model's thinking — which then gets read aloud.
                no_think=True,
            )
            if text := (message.get("content") or "").strip():
                return text
            log.warning("empty announcement from the model; using the plain wording")
        except Exception:
            log.exception("could not compose a reminder announcement; using the plain wording")
        return fallback(payload)

    def _facts(self, payload: dict[str, Any]) -> str:
        lines = [
            f"Time now: {payload.get('time_now', '')} on {payload.get('date_now', '')}",
            f"Reminder was set for: {payload.get('due', '')}",
            f"They asked to be reminded: {payload.get('reason', '')}",
        ]
        late = int(payload.get("late_seconds") or 0)
        if late >= 120:
            lines.append(
                f"This is {late // 60} minutes later than they asked — say so briefly."
            )
        if payload.get("repeat"):
            lines.append(f"It repeats {payload['repeat']}; no need to mention that.")
        return "\n".join(lines)
