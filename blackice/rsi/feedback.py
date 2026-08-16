"""Stage one of RSI: your verdicts become durable facts, and triage recalls
them as precedent. No self-modification -- the system only learns what you
told it.
"""

from __future__ import annotations

import logging
from typing import Any

from .. import db
from ..config import get_settings
from ..memory.store import TRUTH, memory
from ..models import Trust

log = logging.getLogger("blackice.rsi.feedback")

READABLE = {
    "false_positive": "was not worth escalating",
    "true_positive": "was worth escalating",
    "unclear": "was ambiguous",
}


async def on_verdict(verdict: dict[str, Any]) -> str | None:
    """Turn a verdict into a fact keyed by sensor and event kind."""
    if not get_settings().rsi_feedback_enabled:
        return None

    context = await db.fetchone(
        """SELECT e.sensor_id, e.kind, e.summary, s.name AS sensor_name,
                  x.classification, x.threat_level
             FROM escalations x
             JOIN events e ON e.id = x.event_id
             LEFT JOIN sensors s ON s.id = e.sensor_id
            WHERE x.id = ?""",
        (verdict["escalation_id"],),
    )
    if not context:
        return None

    sensor = context["sensor_name"] or context["sensor_id"]
    phrase = READABLE.get(verdict["verdict"], verdict["verdict"])
    value = (
        f"On {sensor}, a '{context['kind']}' event classified as "
        f"'{context['classification']}' ({context['threat_level']}) {phrase}."
    )
    # The user's own note is USER-trust text and may be included verbatim.
    if verdict.get("note"):
        value += f" {get_settings().owner_name} said: {verdict['note']}"

    return await memory.add_fact(
        TRUTH,
        f"triage:{context['sensor_id']}:{context['kind']}",
        value,
        source="user_verdict",
        confidence=0.9,
        trust=Trust.USER,
    )


async def precedent(sensor_id: str, kind: str, limit: int = 5) -> list[str]:
    """Prior verdicts relevant to this sensor and event kind."""
    if not get_settings().rsi_feedback_enabled:
        return []
    facts = await memory.recall(f"{sensor_id} {kind} triage verdict", limit=limit)
    return [f.get("text") or f.get("value") or str(f) for f in facts]


async def golden_set(limit: int = 500) -> list[dict[str, Any]]:
    """Past escalations you have judged. This is the regression gate's yardstick."""
    return await db.fetchall(
        """SELECT x.id AS escalation_id, x.event_id, x.threat_level, x.classification,
                  v.verdict, e.sensor_id, e.kind, e.severity, e.summary, e.sensor_text,
                  e.payload, e.ts
             FROM verdicts v
             JOIN escalations x ON x.id = v.escalation_id
             JOIN events e ON e.id = x.event_id
             WHERE v.id = (SELECT max(id) FROM verdicts WHERE escalation_id = x.id)
             ORDER BY v.id DESC LIMIT ?""",
        (limit,),
    )
