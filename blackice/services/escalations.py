from __future__ import annotations

from typing import Any

from .. import db
from ..bus import bus
from .events import get as get_event
from .listing import list_rows

VALID_VERDICTS = ("true_positive", "false_positive", "unclear")
VALID_STATUS = ("open", "acknowledged", "resolved", "dismissed")


class EscalationError(Exception):
    """An escalation operation could not be completed."""


async def list_escalations(
    q: str | None = None, start: str | None = None, end: str | None = None,
    status: str | None = None, limit: int = 100, offset: int = 0,
) -> dict[str, Any]:
    """List events escalated for attention, newest first."""
    where, params = [], []
    if status:
        where.append("t.status = ?")
        params.append(status)
    return await list_rows(
        table="escalations",
        columns="t.id, t.event_id, t.ts, t.threat_level, t.classification,"
                " t.suggested_action, t.status",
        fts_table="escalations_fts",
        q=q, start=start, end=end, where=where, params=params,
        limit=limit, offset=offset,
    )


async def get_escalation(escalation_id: int) -> dict[str, Any] | None:
    """Full detail: classification, reasoning, the event, and its media."""
    row = await db.fetchone("SELECT * FROM escalations WHERE id = ?", (escalation_id,))
    if row is None:
        return None
    row["event"] = await get_event(row["event_id"])
    row["sensor"] = await db.fetchone(
        "SELECT id, name, kind, plugin FROM sensors WHERE id ="
        " (SELECT sensor_id FROM events WHERE id = ?)",
        (row["event_id"],),
    )
    row["verdicts"] = await db.fetchall(
        "SELECT * FROM verdicts WHERE escalation_id = ? ORDER BY id", (escalation_id,)
    )
    return row


async def set_status(escalation_id: int, status: str) -> dict[str, Any]:
    """Set an escalation's status: open, acknowledged, resolved or dismissed."""
    if status not in VALID_STATUS:
        raise EscalationError(f"status must be one of {', '.join(VALID_STATUS)}")
    await db.execute(
        "UPDATE escalations SET status = ? WHERE id = ?", (status, escalation_id)
    )
    row = await db.fetchone("SELECT * FROM escalations WHERE id = ?", (escalation_id,))
    await bus.publish("escalation", row)
    return row


async def record_verdict(
    escalation_id: int, verdict: str, note: str | None = None, author: str = "user"
) -> dict[str, Any]:
    """Record whether an escalation was right. This is the RSI feedback signal."""
    if verdict not in VALID_VERDICTS:
        raise EscalationError(f"verdict must be one of {', '.join(VALID_VERDICTS)}")
    if not await db.fetchval(
        "SELECT 1 FROM escalations WHERE id = ?", (escalation_id,)
    ):
        raise EscalationError(f"Unknown escalation {escalation_id}")

    verdict_id = await db.execute(
        "INSERT INTO verdicts (escalation_id, verdict, note, author) VALUES (?, ?, ?, ?)",
        (escalation_id, verdict, note, author),
    )
    row = await db.fetchone("SELECT * FROM verdicts WHERE id = ?", (verdict_id,))

    # Late import: the RSI layer depends on this module.
    from ..rsi import feedback

    await feedback.on_verdict(row)
    return row
