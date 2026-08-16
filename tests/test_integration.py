"""End-to-end against the real models in LM Studio.

Everything else in the suite uses scripted clients, which proves the wiring but
not that the models behave. These are slow and need LM Studio up, so they are
marked `integration` and deselected by default:

    uv run pytest -m integration
"""

import time

import httpx
import pytest

from blackice import db
from blackice.config import get_settings
from blackice.llm.client import LMStudioClient, message_text
from blackice.models import Event
from blackice.rsi import promptstore
from blackice.rsi.review import DailyReview
from blackice.services import escalations, events
from blackice.triage.pipeline import TriagePipeline

pytestmark = pytest.mark.integration

BENIGN = "Interior hallway motion at 14:05 while the house is occupied"
ALARMING = "Glass breaking at the back door at 03:12, no one is expected home"


def _lmstudio_models() -> list[str]:
    s = get_settings()
    try:
        r = httpx.get(f"{s.lmstudio_base_url}/models", timeout=5)
        r.raise_for_status()
        return [m["id"] for m in r.json().get("data", [])]
    except Exception:
        return []


@pytest.fixture(scope="module")
def available() -> list[str]:
    models = _lmstudio_models()
    if not models:
        pytest.skip("LM Studio is not reachable")
    return models


@pytest.fixture
async def live(data_dir, available, monkeypatch):
    """Real settings and a real client, but an isolated database."""
    from blackice.llm import prompts

    for key in ("MODEL_PRIMARY", "MODEL_TRIAGE"):
        monkeypatch.delenv(key, raising=False)
    get_settings.cache_clear()
    s = get_settings()
    for model in (s.model_primary, s.model_triage):
        if model not in available:
            pytest.skip(f"{model} is not loaded in LM Studio")

    await prompts.ensure_seeded()
    await db.execute(
        "INSERT INTO sensors (id, plugin, name) VALUES ('cam.rear','rtsp','Back Door')"
    )
    client = LMStudioClient()
    yield client
    await client.close()
    get_settings.cache_clear()


async def _event(summary: str, severity: int, kind: str = "motion") -> dict:
    eid = await events.record(Event(
        sensor_id="cam.rear", plugin="rtsp", severity=severity,
        kind=kind, summary=summary,
    ))
    return await events.get(eid)


# --- the triage handoff ----------------------------------------------------

async def test_triage_model_hands_off_to_the_primary_and_answers_the_user(live):
    """The whole point of the tiering: the small model decides this one is
    worth the big model's time, and the big model produces the sentence the
    user actually reads."""
    started = time.monotonic()
    outcome = await TriagePipeline(live).process(await _event(ALARMING, 4, "glass_break"))
    elapsed = time.monotonic() - started

    assert outcome["tier"] == "primary", "the small model should have escalated this"
    assert "escalation_id" in outcome

    escalation = await escalations.get_escalation(outcome["escalation_id"])
    assert escalation["threat_level"] in ("elevated", "high", "critical")
    # A response for the user to act on, not just a label.
    assert len(escalation["suggested_action"].split()) >= 3
    assert len(escalation["reasoning"].split()) >= 5
    assert escalation["classification"]
    assert escalation["sensor"]["name"] == "Back Door"
    assert escalation["event"]["summary"] == ALARMING

    print(f"\n  escalated in {elapsed:.1f}s")
    print(f"  threat     : {escalation['threat_level']}")
    print(f"  class      : {escalation['classification']}")
    print(f"  suggestion : {escalation['suggested_action']}")


async def test_the_small_model_absorbs_routine_activity(live):
    """The tier only earns its place if it stops things. It used to return an
    empty string and escalate everything."""
    outcome = await TriagePipeline(live).process(await _event(BENIGN, 1))
    assert outcome["tier"] == "small_model"
    assert outcome["verdict"] == "benign"
    assert await db.fetchval("SELECT count(*) FROM escalations") == 0


async def test_triage_answers_in_one_word_without_thinking(live):
    """Regression: with thinking on, the whole token budget went to reasoning
    and content came back empty."""
    s = get_settings()
    message = await live.chat(
        [
            {"role": "system", "content": "Reply with exactly one word: yes or no."},
            {"role": "user", "content": "Is a glass break at 3am worth investigating?"},
        ],
        model=s.model_triage, temperature=0.0, max_tokens=16, no_think=True,
    )
    text = message_text(message).strip().lower()
    assert text, "the triage model returned nothing"
    assert len(text.split()) <= 3, f"expected a word, got {text!r}"
    assert not (message.get("reasoning_content") or "").strip()


# --- the daily self-review -------------------------------------------------

async def test_the_primary_model_reviews_its_own_prompts(live):
    """It must return a usable review structure, and any edit it proposes must
    survive the gate before going anywhere near the live prompt."""
    for summary, verdict in [
        ("Cat crossing the garden at 02:10", "false_positive"),
        ("Cat on the patio at 01:40", "false_positive"),
        ("Unfamiliar person trying the handle at 02:55", "true_positive"),
    ]:
        event = await _event(summary, 3, "person")
        esc = await db.execute(
            """INSERT INTO escalations (event_id, threat_level, classification,
                                        reasoning, suggested_action)
               VALUES (?, 'high', 'Possible intruder', 'movement at night', 'check')""",
            (event["id"],),
        )
        await escalations.record_verdict(esc, verdict, "cats set this off nightly")

    before = (await promptstore.active("triage"))["text"]
    outcome = await DailyReview(live).run()

    assert outcome["ran"] is True
    assert isinstance(outcome["observations"], str)
    print(f"\n  observations: {outcome['observations'][:300]}")

    for edit in outcome["edits"]:
        print(f"  edit -> {edit['prompt']}: {edit['status']} ({edit.get('reason','')})")
        assert edit["prompt"] in ("system", "triage")
        assert edit["status"] in (
            "activated", "awaiting_approval", "held", "rejected"
        )
        if edit["status"] != "rejected":
            # Whatever it decided, it is a recorded, reversible version.
            stored = await promptstore.get(edit["version_id"])
            assert stored["author"] == "rsi"
            assert stored["rationale"]

    # The golden set here is far below the minimum, so nothing may go live.
    assert (await promptstore.active("triage"))["text"] == before
