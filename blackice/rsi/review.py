"""The daily self-review.

Once a day the primary model reads back what actually happened -- triage
outcomes, escalations, and the verdicts you gave them -- and may propose edits
to the triage prompt and to its own system prompt.

A proposal is never live on the strength of the model liking it. It has to beat
the incumbent on the golden set of events you have already judged, and even
then only applies automatically when RSI_SELF_EDIT_ENABLED is on. Otherwise it
waits in the review queue.
"""

from __future__ import annotations

import logging
from typing import Any

from .. import db
from ..bus import bus
from ..config import get_settings
from ..llm import prompts as prompt_defaults
from ..llm.client import client as default_client
from ..llm.client import extract_json, json_schema_format, user_message
from . import promptstore
from .regression import RegressionGate

log = logging.getLogger("blackice.rsi.review")

JOB = "daily_review"

EDITABLE = (prompt_defaults.SYSTEM, prompt_defaults.TRIAGE)

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "observations": {"type": "string"},
        "edits": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "enum": list(EDITABLE)},
                    "text": {"type": "string"},
                    "rationale": {"type": "string"},
                },
                "required": ["prompt", "text", "rationale"],
            },
        },
    },
    "required": ["observations", "edits"],
}

REVIEW_PROMPT = """You are reviewing your own performance as {name}, the
assistant for a home monitoring system.

Below is what happened recently: how triage classified events, which were
escalated, and how {owner} judged those escalations. A "false_positive" means
you raised something that did not deserve it. A "true_positive" means you were
right to raise it.

Your current prompts follow. Propose edits ONLY where the evidence supports
them -- a pattern of wrong calls, a category you keep missing, an instruction
that is misfiring. If the evidence does not justify a change, return an empty
edits list. That is the expected outcome on a quiet day.

Rules for any edit you propose:
- Return the COMPLETE new prompt text, not a patch.
- Keep the required output format exactly as it is. The triage prompt must
  still return the same JSON fields; breaking that breaks the pipeline.
- Do not weaken safety instructions about untrusted sensor data.
- Explain, in the rationale, which observations drove the change."""


class DailyReview:
    def __init__(self, client=None, gate: RegressionGate | None = None) -> None:
        self.client = client or default_client
        self.gate = gate or RegressionGate(self.client)

    # --- evidence ----------------------------------------------------------

    async def evidence(self, days: int = 7) -> dict[str, Any]:
        since = f"-{days} days"
        return {
            "triage_outcomes": await db.fetchall(
                """SELECT sensor_id, kind, tier, verdict, count(*) AS n
                     FROM events WHERE ts >= datetime('now', ?) AND tier IS NOT NULL
                    GROUP BY sensor_id, kind, tier, verdict ORDER BY n DESC LIMIT 40""",
                (since,),
            ),
            "judged_escalations": await db.fetchall(
                """SELECT x.threat_level, x.classification, e.sensor_id, e.kind,
                          e.summary, v.verdict, v.note
                     FROM verdicts v
                     JOIN escalations x ON x.id = v.escalation_id
                     JOIN events e ON e.id = x.event_id
                    WHERE v.ts >= datetime('now', ?)
                    ORDER BY v.id DESC LIMIT 40""",
                (since,),
            ),
            "unjudged_escalations": await db.fetchall(
                """SELECT threat_level, classification, count(*) AS n
                     FROM escalations
                    WHERE ts >= datetime('now', ?)
                      AND id NOT IN (SELECT escalation_id FROM verdicts)
                    GROUP BY threat_level, classification ORDER BY n DESC LIMIT 20""",
                (since,),
            ),
        }

    def _render(self, evidence: dict[str, Any], current: dict[str, str]) -> str:
        parts = ["## Recent triage outcomes", db.dumps(evidence["triage_outcomes"]),
                 "", "## Escalations you judged", db.dumps(evidence["judged_escalations"]),
                 "", "## Escalations still unjudged",
                 db.dumps(evidence["unjudged_escalations"]), ""]
        for name, text in current.items():
            parts += [f"## Current prompt: {name}", text, ""]
        return "\n".join(parts)

    # --- the run -----------------------------------------------------------

    async def run(self, days: int = 7) -> dict[str, Any]:
        s = get_settings()
        evidence = await self.evidence(days)
        if not any(evidence.values()):
            await self._mark_run({"skipped": "no activity to review"})
            return {"ran": True, "edits": [], "note": "no activity to review"}

        current = {}
        for name in EDITABLE:
            row = await promptstore.active(name)
            current[name] = row["text"] if row else prompt_defaults.DEFAULTS[name]

        system = REVIEW_PROMPT.format(name=s.assistant_name, owner=s.owner_name)
        try:
            message = await self.client.chat(
                [
                    {"role": "system", "content": system},
                    user_message(self._render(evidence, current)),
                ],
                model=s.model_primary, temperature=0.2,
                response_format=json_schema_format("self_review", REVIEW_SCHEMA),
            )
            data = extract_json(message)
        except Exception:
            log.exception("daily review failed to produce a usable answer")
            await self._mark_run({"error": "model call failed"})
            return {"ran": False, "edits": []}

        results = []
        for edit in (data.get("edits") or []):
            results.append(await self._consider(edit))

        outcome = {
            "ran": True,
            "observations": data.get("observations", ""),
            "edits": results,
        }
        await self._mark_run(outcome)
        await bus.publish("rsi_proposal", outcome)
        return outcome

    async def _consider(self, edit: dict[str, Any]) -> dict[str, Any]:
        """Store a candidate, gate it, and apply only if allowed and better."""
        name = edit.get("prompt", "")
        if name not in EDITABLE:
            return {"prompt": name, "status": "rejected",
                    "reason": "not an editable prompt"}
        try:
            candidate = await promptstore.propose(
                name, edit.get("text", ""),
                rationale=edit.get("rationale", ""), author=promptstore.RSI,
            )
        except ValueError as exc:
            return {"prompt": name, "status": "rejected", "reason": str(exc)}

        incumbent = await db.fetchone(
            "SELECT * FROM prompt_versions WHERE id = ?", (candidate["parent_id"],)
        ) if candidate["parent_id"] else None

        verdict = await self.gate.evaluate(candidate, incumbent)
        result = {
            "prompt": name, "version_id": candidate["id"],
            "version": candidate["version"], "rationale": candidate["rationale"],
            "regression": verdict,
            "diff": promptstore.diff(
                incumbent["text"] if incumbent else "", candidate["text"], label=name
            ),
        }

        s = get_settings()
        if not verdict["passed"]:
            result["status"] = "held"
            result["reason"] = verdict.get("reason", "did not beat the active prompt")
        elif not s.rsi_self_edit_enabled:
            result["status"] = "awaiting_approval"
            result["reason"] = "RSI_SELF_EDIT_ENABLED is off"
        else:
            await promptstore.activate(candidate["id"])
            result["status"] = "activated"

        await self._queue_proposal(result)
        return result

    async def _queue_proposal(self, result: dict[str, Any]) -> None:
        await db.execute(
            """INSERT INTO rsi_proposals (kind, target, current, proposed, evidence,
                                          rationale, status)
               VALUES ('prompt', ?, '{}', ?, ?, ?, ?)""",
            (result["prompt"],
             db.dumps({"version_id": result["version_id"],
                       "diff": result["diff"]}),
             db.dumps(result["regression"]),
             result.get("rationale", ""),
             "applied" if result["status"] == "activated" else "pending"),
        )

    async def _mark_run(self, detail: dict[str, Any]) -> None:
        await db.execute(
            """INSERT INTO job_runs (job, last_run, detail)
               VALUES (?, datetime('now'), ?)
               ON CONFLICT(job) DO UPDATE SET
                 last_run = excluded.last_run, detail = excluded.detail""",
            (JOB, db.dumps(detail)),
        )


review = DailyReview()


async def due(interval_hours: int = 24) -> bool:
    last = await db.fetchval("SELECT last_run FROM job_runs WHERE job = ?", (JOB,))
    if not last:
        return True
    overdue = await db.fetchval(
        "SELECT datetime(?, ?) <= datetime('now')", (last, f"+{interval_hours} hours")
    )
    return bool(overdue)
