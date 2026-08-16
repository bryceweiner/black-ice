"""Replay a candidate prompt against events you have already judged.

Without this, self-editing is a model grading its own homework. The golden set
is built from your verdicts, so "better" means "agrees with you more often",
not "sounds more thorough".
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from .. import db
from ..config import get_settings
from ..llm.client import client as default_client
from ..llm.client import extract_json, json_schema_format, user_message
from ..models import ThreatLevel
from ..triage.pipeline import CLASSIFICATION_SCHEMA, _describe
from . import feedback

log = logging.getLogger("blackice.rsi.regression")

# Threat levels that mean "worth waking someone for".
ESCALATING = {ThreatLevel.ELEVATED, ThreatLevel.HIGH, ThreatLevel.CRITICAL}

# A candidate may not be worse than the incumbent. Equal passes: a rewrite that
# holds accuracy while being clearer is a legitimate improvement.
TOLERANCE = 0.0


@dataclass(slots=True)
class Score:
    agreed: int
    total: int

    @property
    def rate(self) -> float:
        return self.agreed / self.total if self.total else 0.0


def _should_escalate(threat_level: str) -> bool:
    try:
        return ThreatLevel(threat_level) in ESCALATING
    except ValueError:
        return False


def _user_thought_it_mattered(verdict: str) -> bool:
    return verdict == "true_positive"


class RegressionGate:
    def __init__(self, client=None) -> None:
        self.client = client or default_client

    async def classify(self, prompt_text: str, case: dict[str, Any]) -> str:
        """Classify one golden-set case under a specific prompt."""
        event = {
            "id": case["event_id"], "sensor_id": case["sensor_id"],
            "kind": case["kind"], "severity": case["severity"],
            "summary": case["summary"], "sensor_text": case["sensor_text"],
            "payload": db.loads(case["payload"]), "ts": case["ts"], "media": [],
        }
        try:
            message = await self.client.chat(
                [
                    {"role": "system", "content": prompt_text},
                    user_message(_describe(event)),
                ],
                model=get_settings().model_primary, temperature=0.0,
                response_format=json_schema_format(
                    "classification", CLASSIFICATION_SCHEMA
                ),
            )
            return str(extract_json(message).get("threat_level", "unknown"))
        except Exception:
            log.exception("regression classify failed for event %s", case["event_id"])
            return "unknown"

    async def score(self, prompt_text: str, cases: list[dict[str, Any]]) -> Score:
        agreed = 0
        for case in cases:
            level = await self.classify(prompt_text, case)
            if _should_escalate(level) == _user_thought_it_mattered(case["verdict"]):
                agreed += 1
        return Score(agreed, len(cases))

    async def evaluate(
        self, candidate: dict[str, Any], incumbent: dict[str, Any] | None
    ) -> dict[str, Any]:
        """Replay both prompts over the golden set and record the outcome."""
        s = get_settings()
        cases = await feedback.golden_set()

        if len(cases) < s.rsi_golden_set_min:
            detail = (
                f"golden set has {len(cases)} judged events, "
                f"below the {s.rsi_golden_set_min} needed to tell prompts apart"
            )
            log.info("regression skipped: %s", detail)
            run_id = await db.execute(
                """INSERT INTO regression_runs
                     (candidate_id, incumbent_id, golden_set_size, passed, detail)
                   VALUES (?, ?, ?, 0, ?)""",
                (candidate["id"], incumbent["id"] if incumbent else None,
                 len(cases), db.dumps({"skipped": detail})),
            )
            return {"id": run_id, "passed": False, "reason": detail,
                    "golden_set_size": len(cases)}

        candidate_score = await self.score(candidate["text"], cases)
        incumbent_score = (
            await self.score(incumbent["text"], cases) if incumbent else Score(0, 0)
        )
        passed = candidate_score.rate >= incumbent_score.rate - TOLERANCE
        detail = {
            "candidate": {"agreed": candidate_score.agreed, "of": candidate_score.total},
            "incumbent": {"agreed": incumbent_score.agreed, "of": incumbent_score.total},
        }
        run_id = await db.execute(
            """INSERT INTO regression_runs
                 (candidate_id, incumbent_id, golden_set_size,
                  candidate_score, incumbent_score, passed, detail)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (candidate["id"], incumbent["id"] if incumbent else None, len(cases),
             candidate_score.rate, incumbent_score.rate, int(passed),
             db.dumps(detail)),
        )
        log.info(
            "regression %s: candidate %.2f vs incumbent %.2f over %d cases",
            "passed" if passed else "FAILED",
            candidate_score.rate, incumbent_score.rate, len(cases),
        )
        return {
            "id": run_id, "passed": passed, "golden_set_size": len(cases),
            "candidate_score": candidate_score.rate,
            "incumbent_score": incumbent_score.rate,
        }


gate = RegressionGate()
