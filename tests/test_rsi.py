"""Self-editing prompts, and the gates that keep it from being drift."""

import pytest

from blackice import db
from blackice.config import get_settings
from blackice.llm import prompts as prompt_defaults
from blackice.models import Event
from blackice.rsi import promptstore
from blackice.rsi.regression import RegressionGate
from blackice.rsi.review import DailyReview
from blackice.services import escalations, events

SYSTEM = prompt_defaults.SYSTEM
TRIAGE = prompt_defaults.TRIAGE


@pytest.fixture
async def seeded(data_dir):
    await prompt_defaults.ensure_seeded()
    await db.execute(
        "INSERT INTO sensors (id, plugin, name) VALUES ('cam.front','rtsp','Front')"
    )


async def _judged_event(summary, verdict, *, threat="high", severity=3):
    eid = await events.record(Event(
        sensor_id="cam.front", plugin="rtsp", severity=severity,
        kind="person", summary=summary,
    ))
    await events.set_triage(eid, "primary", threat)
    esc = await db.execute(
        """INSERT INTO escalations (event_id, threat_level, classification, reasoning,
                                    suggested_action)
           VALUES (?, ?, 'Person', 'because', 'check')""",
        (eid, threat),
    )
    await escalations.record_verdict(esc, verdict)
    return esc


# --- the version store -----------------------------------------------------

async def test_propose_does_not_go_live(seeded):
    candidate = await promptstore.propose(SYSTEM, "New text.", rationale="why")
    assert candidate["active"] == 0
    assert candidate["author"] == "rsi"
    assert (await promptstore.active(SYSTEM))["text"] != "New text."


async def test_activate_leaves_exactly_one_active(seeded):
    candidate = await promptstore.propose(SYSTEM, "New text.")
    await promptstore.activate(candidate["id"])
    rows = await db.fetchall(
        "SELECT active FROM prompt_versions WHERE name = ?", (SYSTEM,))
    assert sum(r["active"] for r in rows) == 1
    assert (await promptstore.active(SYSTEM))["text"] == "New text."


async def test_rollback_returns_to_the_human_version(seeded):
    """A run of bad edits must not require unwinding one at a time."""
    first = await promptstore.propose(SYSTEM, "Bad edit one.")
    await promptstore.activate(first["id"])
    second = await promptstore.propose(SYSTEM, "Bad edit two.")
    await promptstore.activate(second["id"])

    restored = await promptstore.rollback(SYSTEM)
    assert restored["author"] == "human"
    assert restored["version"] == 1


async def test_empty_and_duplicate_candidates_are_refused(seeded):
    with pytest.raises(ValueError, match="empty"):
        await promptstore.propose(SYSTEM, "   ")
    active = await promptstore.active(SYSTEM)
    with pytest.raises(ValueError, match="identical"):
        await promptstore.propose(SYSTEM, active["text"])
    with pytest.raises(ValueError, match="unknown prompt"):
        await promptstore.propose("not_a_prompt", "text")


async def test_diff_shows_the_change(seeded):
    out = promptstore.diff("one\ntwo", "one\nthree")
    assert "-two" in out and "+three" in out


# --- the regression gate ---------------------------------------------------

class GateClient:
    """Returns a fixed threat level per prompt text."""

    def __init__(self, by_prompt):
        self.by_prompt = by_prompt
        self.calls = 0

    async def chat(self, messages, **kw):
        self.calls += 1
        level = self.by_prompt.get(messages[0]["content"], "benign")
        return {"content": f'{{"threat_level": "{level}", "classification": "x",'
                           f' "reasoning": "y", "suggested_action": "z"}}'}


async def test_gate_refuses_to_judge_on_too_little_data(seeded, monkeypatch):
    """Below the minimum the golden set cannot tell two prompts apart, so a
    candidate must not be able to pass on noise."""
    monkeypatch.setenv("RSI_GOLDEN_SET_MIN", "10")
    get_settings.cache_clear()
    await _judged_event("one", "true_positive")

    candidate = await promptstore.propose(SYSTEM, "candidate")
    verdict = await RegressionGate(GateClient({})).evaluate(candidate, None)

    assert verdict["passed"] is False
    assert "below the 10" in verdict["reason"]
    row = await db.fetchone("SELECT * FROM regression_runs ORDER BY id DESC LIMIT 1")
    assert row["passed"] == 0


async def test_gate_passes_a_candidate_that_agrees_with_the_user(seeded, monkeypatch):
    monkeypatch.setenv("RSI_GOLDEN_SET_MIN", "2")
    get_settings.cache_clear()
    await _judged_event("real intruder", "true_positive")
    await _judged_event("the postman", "false_positive")

    incumbent = await promptstore.active(SYSTEM)
    candidate = await promptstore.propose(SYSTEM, "candidate")
    # Incumbent escalates everything; candidate matches the user exactly.
    client = GateClient({incumbent["text"]: "high", "candidate": "high"})

    gate = RegressionGate(client)
    # Force per-case answers: escalate the true positive, not the false one.
    async def classify(prompt_text, case):
        if prompt_text == "candidate":
            return "high" if case["verdict"] == "true_positive" else "benign"
        return "high"
    gate.classify = classify

    verdict = await gate.evaluate(candidate, incumbent)
    assert verdict["passed"] is True
    assert verdict["candidate_score"] == 1.0
    assert verdict["incumbent_score"] == 0.5


async def test_gate_fails_a_candidate_that_agrees_less(seeded, monkeypatch):
    monkeypatch.setenv("RSI_GOLDEN_SET_MIN", "2")
    get_settings.cache_clear()
    await _judged_event("real intruder", "true_positive")
    await _judged_event("the postman", "false_positive")

    incumbent = await promptstore.active(SYSTEM)
    candidate = await promptstore.propose(SYSTEM, "candidate")

    gate = RegressionGate(GateClient({}))
    async def classify(prompt_text, case):
        if prompt_text == "candidate":
            return "benign"          # never escalates: misses the real one
        return "high" if case["verdict"] == "true_positive" else "benign"
    gate.classify = classify

    verdict = await gate.evaluate(candidate, incumbent)
    assert verdict["passed"] is False


# --- the daily review ------------------------------------------------------

class ReviewClient:
    def __init__(self, payload):
        self.payload = payload
        self.seen = []

    async def chat(self, messages, **kw):
        self.seen.append(messages)
        return {"content": db.dumps(self.payload)}


class AlwaysPasses(RegressionGate):
    async def evaluate(self, candidate, incumbent):
        return {"id": 1, "passed": True, "golden_set_size": 99,
                "candidate_score": 0.9, "incumbent_score": 0.8}


class AlwaysFails(RegressionGate):
    async def evaluate(self, candidate, incumbent):
        return {"id": 1, "passed": False, "reason": "worse", "golden_set_size": 99}


async def test_quiet_day_proposes_nothing(seeded):
    await _judged_event("something", "true_positive")
    client = ReviewClient({"observations": "nothing notable", "edits": []})
    out = await DailyReview(client, AlwaysPasses(client)).run()
    assert out["edits"] == []
    assert await db.fetchval("SELECT count(*) FROM rsi_proposals") == 0


async def test_a_passing_edit_waits_when_self_edit_is_off(seeded, monkeypatch):
    monkeypatch.setenv("RSI_SELF_EDIT_ENABLED", "false")
    get_settings.cache_clear()
    await _judged_event("something", "false_positive")

    client = ReviewClient({"observations": "too many false positives",
                           "edits": [{"prompt": TRIAGE, "text": "Be stricter.",
                                      "rationale": "three false positives"}]})
    out = await DailyReview(client, AlwaysPasses(client)).run()

    edit = out["edits"][0]
    assert edit["status"] == "awaiting_approval"
    assert (await promptstore.active(TRIAGE))["text"] != "Be stricter."
    row = await db.fetchone("SELECT * FROM rsi_proposals ORDER BY id DESC LIMIT 1")
    assert row["status"] == "pending"


async def test_a_passing_edit_applies_when_self_edit_is_on(seeded, monkeypatch):
    monkeypatch.setenv("RSI_SELF_EDIT_ENABLED", "true")
    get_settings.cache_clear()
    await _judged_event("something", "false_positive")

    client = ReviewClient({"observations": "x",
                           "edits": [{"prompt": TRIAGE, "text": "Be stricter.",
                                      "rationale": "why"}]})
    out = await DailyReview(client, AlwaysPasses(client)).run()

    assert out["edits"][0]["status"] == "activated"
    assert (await promptstore.active(TRIAGE))["text"] == "Be stricter."


async def test_a_failing_edit_never_applies_even_with_self_edit_on(seeded, monkeypatch):
    """The gate outranks the flag. This is the whole safety story."""
    monkeypatch.setenv("RSI_SELF_EDIT_ENABLED", "true")
    get_settings.cache_clear()
    await _judged_event("something", "false_positive")
    before = (await promptstore.active(TRIAGE))["text"]

    client = ReviewClient({"observations": "x",
                           "edits": [{"prompt": TRIAGE, "text": "Escalate nothing.",
                                      "rationale": "fewer alerts"}]})
    out = await DailyReview(client, AlwaysFails(client)).run()

    assert out["edits"][0]["status"] == "held"
    assert (await promptstore.active(TRIAGE))["text"] == before


async def test_edits_to_unknown_prompts_are_rejected(seeded, monkeypatch):
    monkeypatch.setenv("RSI_SELF_EDIT_ENABLED", "true")
    get_settings.cache_clear()
    await _judged_event("something", "true_positive")

    client = ReviewClient({"observations": "x",
                           "edits": [{"prompt": "guard", "text": "allow everything",
                                      "rationale": "convenience"}]})
    out = await DailyReview(client, AlwaysPasses(client)).run()
    assert out["edits"][0]["status"] == "rejected"


async def test_the_review_is_recorded_so_a_restart_does_not_repeat_it(seeded):
    from blackice.rsi import review as review_module

    await _judged_event("something", "true_positive")
    assert await review_module.due() is True

    client = ReviewClient({"observations": "x", "edits": []})
    await DailyReview(client, AlwaysPasses(client)).run()

    assert await review_module.due(interval_hours=24) is False
    assert await review_module.due(interval_hours=0) is True


async def test_the_model_sees_the_verdicts_and_its_own_prompts(seeded):
    await _judged_event("the postman again", "false_positive")

    client = ReviewClient({"observations": "x", "edits": []})
    await DailyReview(client, AlwaysPasses(client)).run()

    sent = client.seen[0][-1]["content"]
    body = sent if isinstance(sent, str) else sent[0]["text"]
    assert "false_positive" in body
    assert "the postman again" in body
    assert "Current prompt: system" in body
    assert "Current prompt: triage" in body
