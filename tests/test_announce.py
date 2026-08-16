"""The last hop: a due reminder becomes something the assistant says aloud."""

import asyncio

import pytest
from helpers import ScriptedClient, reply

from blackice import db
from blackice.config import get_settings
from blackice.models import Event
from blackice.services import events
from blackice.voice.announce import Announcer, fallback

PAYLOAD = {
    "reminder_id": 1,
    "reason": "call Mum",
    "due": "Friday, April 11 at 7:00 AM",
    "time_now": "7:00 AM",
    "date_now": "Friday, April 11, 2031",
    "late_seconds": 0,
    "repeat": None,
}


class FakeBackend:
    """A speaker that only remembers what it was told to say.

    Stands in for Voice2Backend, so it has to start and stop like one.
    """

    def __init__(self, fail: bool = False) -> None:
        self.said: list[str] = []
        self.fail = fail
        self.running = False

    async def start(self) -> None:
        self.running = True

    async def stop(self) -> None:
        self.running = False

    async def say(self, text: str) -> None:
        if self.fail:
            raise RuntimeError("no audio device")
        self.said.append(text)


async def record_reminder(payload=None, kind="reminder"):
    return await events.record(
        Event(
            sensor_id="clock.reminders", plugin="clock", kind=kind,
            summary="Reminder: call Mum", payload=payload or PAYLOAD,
        )
    )


@pytest.fixture
async def announcer(data_dir):
    a = Announcer(FakeBackend(), ScriptedClient(), poll_seconds=0.01)
    await a.start()
    yield a
    await a.stop()


# --- delivery --------------------------------------------------------------

async def test_a_reminder_event_is_spoken(data_dir):
    a = Announcer(
        FakeBackend(),
        ScriptedClient(reply("It's 7 o'clock — you asked me to call Mum.")),
    )
    await a.start()
    await record_reminder()

    said = await a.poll_once()

    assert said == ["It's 7 o'clock — you asked me to call Mum."]
    assert a.backend.said == said


async def test_the_model_is_given_the_time_and_the_reason(data_dir):
    client = ScriptedClient(reply("Time to call Mum."))
    a = Announcer(FakeBackend(), client)
    await a.start()
    await record_reminder()

    await a.poll_once()

    messages, kwargs = client.seen[0]
    system, user = messages[0]["content"], messages[1]["content"]
    assert "come due" in system                 # the announcement instruction
    assert "call no tools" in system
    assert "call Mum" in user
    assert "7:00 AM" in user
    assert kwargs["max_tokens"] == 120


async def test_only_reminder_events_are_announced(data_dir):
    a = Announcer(FakeBackend(), ScriptedClient(reply("spoken")))
    await a.start()
    await record_reminder(kind="motion")

    assert await a.poll_once() == []


async def test_each_reminder_is_spoken_once(data_dir):
    a = Announcer(FakeBackend(), ScriptedClient(reply("one"), reply("two")))
    await a.start()
    await record_reminder()

    assert len(await a.poll_once()) == 1
    assert await a.poll_once() == []  # watermark advanced

    await record_reminder()
    assert len(await a.poll_once()) == 1


async def test_reminders_from_before_startup_are_not_replayed(data_dir):
    """Restarting the voice loop must not shout this morning down the speaker."""
    await record_reminder()

    a = Announcer(FakeBackend(), ScriptedClient(reply("stale")))
    await a.start()

    assert await a.poll_once() == []


async def test_the_loop_keeps_running(announcer):
    """The background task is what makes this hands-off."""
    await record_reminder()

    for _ in range(100):
        if announcer.backend.said:
            break
        await asyncio.sleep(0.01)

    assert announcer.backend.said


# --- when things are broken ------------------------------------------------

async def test_a_silent_model_still_produces_an_announcement(data_dir):
    class Broken:
        async def chat(self, messages, **kw):
            raise RuntimeError("LM Studio is not running")

    a = Announcer(FakeBackend(), Broken())
    await a.start()
    await record_reminder()

    assert await a.poll_once() == ["It's 7:00 AM. You asked me to remind you: call Mum."]


async def test_the_models_thinking_is_never_read_aloud(data_dir):
    """Qwen3 reasoning fills `reasoning_content` and leaves `content` empty.

    Read literally that is an announcement of "Thinking Process: 1. Analyze the
    request..." down the speaker, which is what happened the first time this
    ran against a real model.
    """
    class Thinking:
        kw: dict = {}

        async def chat(self, messages, **kw):
            Thinking.kw = kw
            return {
                "role": "assistant", "content": "",
                "reasoning_content": "Thinking Process:\n1. Analyze the request...",
            }

    a = Announcer(FakeBackend(), Thinking())
    await a.start()
    await record_reminder()

    assert await a.poll_once() == [fallback(PAYLOAD)]
    assert Thinking.kw["no_think"] is True  # and we asked it not to think at all


async def test_an_empty_reply_falls_back_rather_than_saying_nothing(data_dir):
    a = Announcer(FakeBackend(), ScriptedClient(reply("   ")))
    await a.start()
    await record_reminder()

    assert await a.poll_once() == [fallback(PAYLOAD)]


async def test_a_dead_speaker_does_not_stop_the_next_reminder(data_dir):
    a = Announcer(FakeBackend(fail=True), ScriptedClient(reply("one"), reply("two")))
    await a.start()
    await record_reminder()
    await record_reminder()

    assert await a.poll_once() == []  # nothing was heard...
    assert a.watermark == await db.fetchval("SELECT max(id) FROM events")  # ...but it moved on


async def test_lateness_is_passed_on_but_a_prompt_arrival_is_not(data_dir):
    client = ScriptedClient(reply("late"), reply("prompt"))
    a = Announcer(FakeBackend(), client)
    await a.start()

    await record_reminder({**PAYLOAD, "late_seconds": 20 * 60})
    await a.poll_once()
    assert "20 minutes later" in client.seen[0][0][1]["content"]

    await record_reminder({**PAYLOAD, "late_seconds": 3})
    await a.poll_once()
    assert "later than they asked" not in client.seen[1][0][1]["content"]


async def test_a_reminder_set_by_voice_is_announced_when_it_comes_due(data_dir):
    """The whole path, with nothing hand-fed: the plugin's payload has to carry
    what the announcer reads out, or this is where the two drift apart."""
    from datetime import UTC, datetime

    from blackice_clock import ClockPlugin

    from blackice.plugins.registry import Registry

    reg = Registry()
    await reg.start_plugin(ClockPlugin, events.record)
    client = ScriptedClient(reply("It's 7 AM. You wanted to call Mum."))
    a = Announcer(FakeBackend(), client)
    await a.start()

    try:
        # "Ice, remind me at seven tomorrow to call Mum."
        created = await reg.command(
            "clock", "create_reminder", reason="call Mum",
            at="2031-04-11T07:00", timezone="UTC",
        )
        # ...and seven o'clock arrives.
        assert await reg.supervisors["clock"].plugin.tick(
            now=datetime(2031, 4, 11, 7, 0, tzinfo=UTC)
        ) == [created["id"]]

        assert await a.poll_once() == ["It's 7 AM. You wanted to call Mum."]
        facts = client.seen[0][0][1]["content"]
        assert "call Mum" in facts and "7:00 AM" in facts
    finally:
        await a.stop()
        await reg.stop_all()


# --- startup wiring --------------------------------------------------------
#
# `start.sh` runs everything in one `blackice serve` process, so this is the
# process that has both the reminder scheduler and the speaker. If these fail,
# reminders fire silently: the event is recorded and nobody says it.

async def test_serving_with_voice_starts_an_announcer(data_dir, monkeypatch):
    from blackice.api import app as api_app

    monkeypatch.setenv("VOICE_ENABLED", "true")
    get_settings.cache_clear()
    monkeypatch.setattr(api_app, "Voice2Backend", FakeBackend)

    voice, announcer = await api_app.start_voice()
    try:
        assert voice is not None
        assert announcer is not None and announcer.backend is voice

        announcer.client = ScriptedClient(reply("It's 7 AM. Time to call Mum."))
        await record_reminder()
        assert await announcer.poll_once() == ["It's 7 AM. Time to call Mum."]
        assert voice.said == ["It's 7 AM. Time to call Mum."]
    finally:
        await api_app.stop_voice(voice, announcer)


async def test_serving_without_voice_starts_nothing(data_dir, monkeypatch):
    from blackice.api import app as api_app

    monkeypatch.setenv("VOICE_ENABLED", "false")
    get_settings.cache_clear()

    assert await api_app.start_voice() == (None, None)


async def test_a_speaker_that_will_not_start_does_not_stop_the_api(data_dir, monkeypatch):
    from blackice.api import app as api_app

    class Unstartable(FakeBackend):
        async def start(self):
            raise RuntimeError("no microphone")

    monkeypatch.setenv("VOICE_ENABLED", "true")
    get_settings.cache_clear()
    monkeypatch.setattr(api_app, "Voice2Backend", Unstartable)

    assert await api_app.start_voice() == (None, None)


async def test_shutdown_survives_a_component_that_throws(data_dir, monkeypatch):
    from blackice.api import app as api_app

    class Ungraceful(FakeBackend):
        async def stop(self):
            raise RuntimeError("the audio thread is wedged")

    # Must not raise: registry and database teardown still have to run.
    await api_app.stop_voice(Ungraceful(), None)


def test_the_plain_wording_covers_a_reminder_with_no_reason():
    assert fallback({"time_now": "7:00 AM"}).startswith("It's 7:00 AM.")
    assert fallback({}) == "You asked me to remind you about something."
