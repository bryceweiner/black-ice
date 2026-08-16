"""Session-end consolidation, scoped by trust.

Two sources are eligible and no others:

  1. Conversation turns -- console and voice, already through guard(trust=USER).
  2. Structured event fields -- sensor id, timestamps, kinds, tier verdicts,
     your verdicts. System-generated, not attacker-influenceable.

Candidate facts are *built* from an allow-list rather than filtered out of free
text afterwards. A blocklist here would itself be the memory-poisoning vector:
sensor-supplied strings reach us through OCR, device labels and transcripts, and
one that slipped a filter would become a persistent instruction.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from .. import db
from ..config import get_settings
from ..models import Trust
from .store import EVENT, TRUTH, memory

log = logging.getLogger("blackice.memory.consolidate")

# Columns of `events` that may inform a durable fact. `sensor_text` and
# `payload` are deliberately absent: both carry sensor-supplied content.
EVENT_ALLOWED_FIELDS = (
    "sensor_id", "kind", "severity", "ts", "tier", "verdict",
)


def _bridge_generate(loop: asyncio.AbstractEventLoop):
    """kokoro calls generate_fn(prompt, max_tokens) synchronously from a worker
    thread; our client is async, so hop back onto the running loop."""

    def generate(prompt: str, max_tokens: int = 1024) -> str:
        from ..llm.client import client, message_text

        future = asyncio.run_coroutine_threadsafe(
            client.chat(
                [{"role": "user", "content": prompt}],
                model=get_settings().model_primary,
                temperature=0.2,
                max_tokens=max_tokens,
            ),
            loop,
        )
        return message_text(future.result(timeout=300))

    return generate


async def install_generator() -> bool:
    """Wire the local model into kokoro so it can consolidate sessions."""
    km = memory._load()
    if km is None:
        return False
    km.set_generate_fn(_bridge_generate(asyncio.get_running_loop()))
    return True


async def record_turn(user_text: str, agent_text: str, trust: Trust) -> None:
    """Feed one conversation turn into the consolidation buffer."""
    if trust is not Trust.USER:
        return
    await memory.append_turn(user_text, agent_text)


async def consolidate_session() -> str | None:
    """Run kokoro's session-end extraction over the conversation buffer."""
    km = memory._load()
    if km is None:
        return None
    try:
        summary = await asyncio.to_thread(km.summarize_session)
    except Exception:
        log.exception("session consolidation failed")
        return None
    await memory._mirror("summarize_session", detail={"summary": summary or ""})
    return summary


async def consolidate_events(since_hours: int = 24) -> int:
    """Build facts from the *structured* shape of recent activity.

    Nothing here reads sensor-supplied text. The facts describe patterns --
    which sensor reports what kind of thing, how often, and how triage judged it.
    """
    rows = await db.fetchall(
        f"""SELECT {", ".join(EVENT_ALLOWED_FIELDS)}, count(*) AS n
              FROM events
             WHERE ts >= datetime('now', ?)
               AND tier IS NOT NULL
             GROUP BY sensor_id, kind, tier, verdict""",
        (f"-{since_hours} hours",),
    )
    written = 0
    for row in rows:
        sensor = await db.fetchone(
            "SELECT name FROM sensors WHERE id = ?", (row["sensor_id"],)
        )
        name = (sensor or {}).get("name") or row["sensor_id"]
        value = (
            f"{name} reported {row['n']} '{row['kind']}' event"
            f"{'s' if row['n'] != 1 else ''} in the last {since_hours}h; "
            f"triage settled at {row['tier']}/{row['verdict']}."
        )
        if await memory.add_fact(
            EVENT, f"pattern:{row['sensor_id']}:{row['kind']}", value,
            source="event_rollup", confidence=0.6, trust=Trust.SYSTEM,
        ):
            written += 1
    return written


async def consolidate_all(since_hours: int = 24) -> dict[str, Any]:
    """The periodic job: conversations plus structured event patterns."""
    if not get_settings().memory_enabled:
        return {"enabled": False}
    await install_generator()
    return {
        "enabled": True,
        "session_summary": await consolidate_session(),
        "event_facts": await consolidate_events(since_hours),
    }


__all__ = [
    "EVENT_ALLOWED_FIELDS", "TRUTH", "consolidate_all", "consolidate_events",
    "consolidate_session", "install_generator", "record_turn",
]
