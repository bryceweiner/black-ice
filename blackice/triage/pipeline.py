"""Three-tier triage: rules, then a small model, then the 27B.

Tier verdicts, latency, and model id are recorded for every event, so the
thresholds can later be tuned against real history by the RSI layer.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from .. import db
from ..bus import bus
from ..config import get_settings
from ..llm import prompts
from ..llm.client import client as default_client
from ..llm.client import extract_json, json_schema_format, message_text, user_message
from ..llm.guard import wrap_untrusted
from ..models import Classification, ThreatLevel
from ..services import events as events_service
from . import rules

log = logging.getLogger("blackice.triage")

TIER2_CHOICES = ("benign", "ambiguous", "notable")

# Derived from the model so the constraint and the parser cannot drift.
CLASSIFICATION_SCHEMA = {
    "type": "object",
    "properties": {
        "threat_level": {
            "type": "string",
            "enum": [t.value for t in ThreatLevel],
        },
        "classification": {"type": "string"},
        "reasoning": {"type": "string"},
        "suggested_action": {"type": "string"},
    },
    "required": list(Classification.model_fields),
}

TIER2_PROMPT = """Classify this home monitoring event in one word.

benign    - routine domestic activity, nothing to report
ambiguous - unclear, could matter
notable   - unusual, unfamiliar, or potentially a threat

Reply with only the word."""


def _describe(event: dict[str, Any]) -> str:
    parts = [
        f"sensor: {event['sensor_id']}",
        f"time: {event['ts']}",
        f"kind: {event['kind']}",
        f"severity: {event['severity']}",
        f"summary: {event['summary']}",
    ]
    if payload := event.get("payload"):
        parts.append(f"data: {json.dumps(payload, default=str)[:800]}")
    text = "\n".join(parts)
    # Sensor-supplied free text is attacker-influenceable, so it goes in
    # delimited and labelled rather than inline with our own fields.
    if event.get("sensor_text"):
        text += "\n" + wrap_untrusted(event["sensor_text"], event["sensor_id"])
    return text


async def _with_precedent(event: dict[str, Any]) -> str:
    """The event description, plus what the owner has previously said about
    this sensor and event kind.

    This is the read half of the feedback loop. Verdicts were being written to
    memory and never consulted, which made "the system learns from you" true
    only in the sense that it wrote things down.
    """
    described = _describe(event)
    try:
        from ..rsi.feedback import precedent

        prior = await precedent(event["sensor_id"], event["kind"])
    except Exception:
        log.exception("could not recall precedent for %s", event["sensor_id"])
        return described
    if not prior:
        return described

    s = get_settings()
    lines = "\n".join(f"- {p}" for p in prior)
    # Owner-derived, so it is instruction rather than evidence -- unlike
    # sensor_text, which _describe() wraps as untrusted.
    return (
        f"{described}\n\n"
        f"## What {s.owner_name} has said about this sensor before\n{lines}\n"
        "Weigh this: it is their judgement on earlier events from this sensor."
    )


def _images(event: dict[str, Any]) -> list[str]:
    return [
        m["path"] for m in event.get("media", [])
        if m["mime"].startswith("image/") and not m["pruned_at"]
    ]


class TriagePipeline:
    def __init__(self, client=None) -> None:
        self.client = client or default_client

    async def process(self, event: dict[str, Any]) -> dict[str, Any]:
        """Run an event through the tiers. Returns the outcome record."""
        started = time.monotonic()

        decision = await rules.evaluate(event)
        if not decision.passed:
            return await self._finish(event, "rules", decision.reason, started)

        tier2 = await self._tier2(event)
        if tier2 == "benign":
            return await self._finish(event, "small_model", "benign", started)

        classification = await self._tier3(event)
        escalation_id = await self._escalate(event, classification)
        outcome = await self._finish(event, "primary", classification.threat_level, started)
        outcome["escalation_id"] = escalation_id
        return outcome

    async def _tier2(self, event: dict[str, Any]) -> str:
        s = get_settings()
        try:
            message = await self.client.chat(
                [
                    {"role": "system", "content": TIER2_PROMPT},
                    user_message(await _with_precedent(event), _images(event)),
                ],
                model=s.model_triage, temperature=0.0,
                # Enough for the word plus punctuation. With thinking left on
                # the whole budget goes to reasoning and content comes back
                # empty, which silently escalated every event to the 27B.
                max_tokens=16,
                no_think=s.triage_no_think,
            )
        except Exception:
            log.exception("tier 2 failed; escalating to be safe")
            return "ambiguous"
        word = message_text(message).lower()
        # An unparseable answer escalates rather than silently dropping.
        return next((c for c in TIER2_CHOICES if c in word), "ambiguous")

    async def _tier3(self, event: dict[str, Any]) -> Classification:
        s = get_settings()
        try:
            message = await self.client.chat(
                [
                    {"role": "system", "content": await prompts.active(prompts.TRIAGE)},
                    user_message(await _with_precedent(event), _images(event)),
                ],
                model=s.model_primary, temperature=0.1,
                response_format=json_schema_format(
                    "classification", CLASSIFICATION_SCHEMA
                ),
            )
            data = extract_json(message)
            if not data:
                raise ValueError("no JSON in model reply")
            return Classification(**{
                k: v for k, v in data.items()
                if k in Classification.model_fields
            })
        except Exception:
            log.exception("tier 3 failed for event %s", event["id"])
            return Classification(
                threat_level=ThreatLevel.UNKNOWN,
                classification="classification failed",
                reasoning="The primary model did not return a usable answer.",
                suggested_action="Review this event manually.",
            )

    async def _escalate(self, event: dict[str, Any], c: Classification) -> int:
        s = get_settings()
        version = await db.fetchval(
            "SELECT id FROM prompt_versions WHERE name = ? AND active = 1",
            (prompts.TRIAGE,),
        )
        escalation_id = await db.execute(
            """INSERT INTO escalations
                 (event_id, threat_level, classification, reasoning,
                  suggested_action, model, prompt_version)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (event["id"], str(c.threat_level), c.classification, c.reasoning,
             c.suggested_action, s.model_primary, version),
        )
        row = await db.fetchone("SELECT * FROM escalations WHERE id = ?", (escalation_id,))
        await bus.publish("escalation", row)
        return escalation_id

    async def _finish(
        self, event: dict[str, Any], tier: str, verdict: str, started: float
    ) -> dict[str, Any]:
        await events_service.set_triage(event["id"], tier, str(verdict))
        outcome = {
            "event_id": event["id"],
            "tier": tier,
            "verdict": str(verdict),
            "latency_ms": int((time.monotonic() - started) * 1000),
        }
        log.info("triage %s -> %s/%s", event["id"], tier, verdict)
        return outcome


pipeline = TriagePipeline()


async def on_event(topic: str, event: dict[str, Any]) -> None:
    await pipeline.process(event)


def install() -> None:
    bus.subscribe(on_event, topic="event")
