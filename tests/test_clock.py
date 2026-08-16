"""The clock plugin: telling the time, and the reminders the owner sets."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import aiosqlite
import pytest
from blackice_clock import REMINDER_SENSOR_ID, SENSOR_ID, ClockPlugin, ReminderStore
from blackice_clock import reminders as rem

from blackice import db
from blackice.llm.tools import ToolRegistry, project_plugin_tools
from blackice.plugins.registry import Registry
from blackice.services import events

# A Friday, far enough out that no test ever races the real clock.
DUE = datetime(2031, 4, 11, 7, 0, tzinfo=UTC)
DUE_ISO = "2031-04-11T07:00"


@pytest.fixture
async def reg(data_dir):
    r = Registry()
    await r.start_plugin(ClockPlugin, events.record)
    yield r
    await r.stop_all()


@pytest.fixture
def utc_tz(monkeypatch):
    """Pin the plugin's default zone so due times are exactly as written."""
    monkeypatch.setenv("CLOCK_TIMEZONE", "UTC")


def plugin_of(reg):
    return reg.supervisors["clock"].plugin


async def reminder_events():
    return await db.fetchall("SELECT * FROM events WHERE kind = 'reminder' ORDER BY id")


# --- telling the time ------------------------------------------------------

async def test_discovery_finds_installed_plugin(data_dir):
    assert "clock" in [c.name for c in Registry().discover()]


async def test_start_projects_both_sensors(reg, data_dir):
    ids = [r["id"] for r in await db.fetchall("SELECT id FROM sensors ORDER BY id")]
    assert SENSOR_ID in ids
    assert REMINDER_SENSOR_ID in ids


async def test_get_time_reports_the_current_time(reg):
    result = await reg.command("clock", "get_time")
    now = datetime.now().astimezone()

    assert result["date"] == now.strftime("%Y-%m-%d")
    assert result["time_24"] == now.strftime("%H:%M")
    assert result["weekday"] == now.strftime("%A")
    # One call answers "what time is it?" and "what's the date?" together.
    assert result["spoken"].startswith(result["time_12"])
    assert result["date_long"] in result["spoken"]


async def test_get_date_reports_today(reg):
    result = await reg.command("clock", "get_date")
    now = datetime.now().astimezone()

    assert result["day"] == now.day
    assert result["year"] == now.year
    assert result["spoken"] == result["date_long"] == (
        f"{now.strftime('%A')}, {now.strftime('%B')} {now.day}, {now.year}"
    )


async def test_named_timezone_is_honoured(reg):
    result = await reg.command("clock", "get_time", timezone="Asia/Tokyo")
    tokyo = datetime.now(ZoneInfo("Asia/Tokyo"))

    assert result["time_24"] == tokyo.strftime("%H:%M")
    assert result["utc_offset"] == "+0900"


async def test_env_default_timezone(reg, monkeypatch):
    monkeypatch.setenv("CLOCK_TIMEZONE", "Asia/Tokyo")
    result = await reg.command("clock", "get_time")
    assert result["time_24"] == datetime.now(ZoneInfo("Asia/Tokyo")).strftime("%H:%M")


async def test_bad_timezone_reports_an_error_without_going_unhealthy(reg):
    result = await reg.command("clock", "get_time", timezone="Mars/Olympus_Mons")

    assert "unknown timezone" in result["error"]
    assert reg.supervisors["clock"].health()["state"] == "healthy"


# --- setting reminders -----------------------------------------------------

async def test_create_and_list_a_reminder(reg, utc_tz):
    created = await reg.command(
        "clock", "create_reminder", reason="call Mum", at=DUE_ISO
    )
    assert created["created"] is True
    assert "call Mum" in created["spoken"]
    assert "7:00 AM" in created["when"]

    listed = await reg.command("clock", "list_reminders")
    assert listed["count"] == 1
    assert listed["reminders"][0]["id"] == created["id"]
    assert listed["reminders"][0]["status"] == "scheduled"


async def test_bare_clock_time_means_the_next_time_it_comes_round(reg, utc_tz):
    created = await reg.command(
        "clock", "create_reminder", reason="take the tablets", at="07:00"
    )
    due = rem.from_sql((await plugin_of(reg).store.get(created["id"]))["due_at"])

    assert due > datetime.now(UTC)
    assert due <= datetime.now(UTC) + timedelta(days=1)
    assert due.astimezone(ZoneInfo("UTC")).strftime("%H:%M") == "07:00"


async def test_a_time_already_past_is_refused(reg, utc_tz):
    result = await reg.command(
        "clock", "create_reminder", reason="too late", at="2020-01-01T07:00"
    )

    assert "in the past" in result["error"]
    assert reg.supervisors["clock"].health()["state"] == "healthy"
    assert (await reg.command("clock", "list_reminders"))["count"] == 0


async def test_unreadable_time_and_repeat_are_refused(reg, utc_tz):
    bad_time = await reg.command(
        "clock", "create_reminder", reason="whenever", at="half past sevenish"
    )
    assert "could not read" in bad_time["error"]

    bad_repeat = await reg.command(
        "clock", "create_reminder", reason="x", at=DUE_ISO, repeat="fortnightly"
    )
    assert "repeat must be one of" in bad_repeat["error"]
    assert reg.supervisors["clock"].health()["state"] == "healthy"


async def test_edit_changes_time_and_reason(reg, utc_tz):
    created = await reg.command("clock", "create_reminder", reason="call Mum", at=DUE_ISO)

    edited = await reg.command(
        "clock", "edit_reminder", id=created["id"],
        reason="call Mum back", at="2031-04-12T09:30",
    )

    assert edited["updated"] is True
    assert edited["reason"] == "call Mum back"
    assert "9:30 AM" in edited["when"]
    assert edited["status"] == "scheduled"


async def test_editing_an_unknown_reminder_is_an_error_not_a_failure(reg, utc_tz):
    result = await reg.command("clock", "edit_reminder", id=404, reason="nope")

    assert result["error"] == "no reminder with id 404"
    assert reg.supervisors["clock"].health()["state"] == "healthy"


async def test_delete_cancels_but_keeps_the_reminder(reg, utc_tz):
    created = await reg.command("clock", "create_reminder", reason="call Mum", at=DUE_ISO)

    cancelled = await reg.command("clock", "delete_reminder", id=created["id"])
    assert cancelled["cancelled"] is True
    assert (await reg.command("clock", "list_reminders"))["count"] == 0

    # "No, put it back."
    restored = await reg.command(
        "clock", "edit_reminder", id=created["id"], status="scheduled"
    )
    assert restored["status"] == "scheduled"
    assert (await reg.command("clock", "list_reminders"))["count"] == 1


async def test_purge_removes_finished_reminders_only(reg, utc_tz):
    keep = await reg.command("clock", "create_reminder", reason="keep me", at=DUE_ISO)
    drop = await reg.command("clock", "create_reminder", reason="drop me", at=DUE_ISO)
    await reg.command("clock", "delete_reminder", id=drop["id"])

    purged = await reg.command("clock", "purge_reminders", older_than_days=0)

    assert purged["deleted"] == 1
    assert await plugin_of(reg).store.get(drop["id"]) is None
    assert await plugin_of(reg).store.get(keep["id"]) is not None


# --- firing ----------------------------------------------------------------

async def test_a_due_reminder_fires_once_and_records_the_reason(reg, utc_tz):
    created = await reg.command("clock", "create_reminder", reason="call Mum", at=DUE_ISO)

    fired = await plugin_of(reg).tick(now=DUE)
    assert fired == [created["id"]]

    rows = await reminder_events()
    assert len(rows) == 1
    payload = db.loads(rows[0]["payload"])
    assert payload["reason"] == "call Mum"          # the reason the owner gave
    assert payload["time_now"] == "7:00 AM"          # and the time it fired
    assert rows[0]["sensor_id"] == REMINDER_SENSOR_ID
    assert rows[0]["summary"] == "Reminder: call Mum"
    # The owner's own words are not sensor input, so they are not wrapped.
    assert rows[0]["sensor_text"] is None

    # A second pass has nothing left to do.
    assert await plugin_of(reg).tick(now=DUE + timedelta(seconds=30)) == []
    assert (await plugin_of(reg).store.get(created["id"]))["status"] == "fired"


async def test_a_repeating_reminder_reschedules_itself(reg, utc_tz):
    created = await reg.command(
        "clock", "create_reminder", reason="tablets", at=DUE_ISO, repeat="daily"
    )

    await plugin_of(reg).tick(now=DUE)
    row = await plugin_of(reg).store.get(created["id"])

    assert row["status"] == "scheduled"
    assert row["occurrences"] == 1
    assert rem.from_sql(row["due_at"]) == DUE + timedelta(days=1)


async def test_a_reminder_missed_while_down_fires_inside_the_grace_window(reg, utc_tz):
    created = await reg.command("clock", "create_reminder", reason="call Mum", at=DUE_ISO)

    # The service was down; it comes back twenty minutes late.
    fired = await plugin_of(reg).tick(now=DUE + timedelta(minutes=20))

    assert fired == [created["id"]]
    payload = db.loads((await reminder_events())[0]["payload"])
    assert payload["late_seconds"] == 20 * 60


async def test_a_long_missed_reminder_is_marked_missed_not_announced(reg, utc_tz):
    created = await reg.command("clock", "create_reminder", reason="call Mum", at=DUE_ISO)

    assert await plugin_of(reg).tick(now=DUE + timedelta(hours=3)) == []

    assert (await plugin_of(reg).store.get(created["id"]))["status"] == "missed"
    assert await reminder_events() == []


async def test_downtime_skips_occurrences_without_killing_the_repeat(reg, utc_tz):
    created = await reg.command(
        "clock", "create_reminder", reason="tablets", at=DUE_ISO, repeat="daily"
    )

    # Three days off. The mornings that went by are lost, not the alarm.
    assert await plugin_of(reg).tick(now=DUE + timedelta(days=3)) == []

    row = await plugin_of(reg).store.get(created["id"])
    assert row["status"] == "scheduled"
    assert rem.from_sql(row["due_at"]) == DUE + timedelta(days=4)
    assert await reminder_events() == []


async def test_two_schedulers_over_one_file_fire_it_once(reg, utc_tz, data_dir):
    """`blackice serve` and `blackice voice` both run this loop."""
    created = await reg.command("clock", "create_reminder", reason="call Mum", at=DUE_ISO)

    other = await aiosqlite.connect(data_dir / "plugins" / "clock.db")
    other.row_factory = aiosqlite.Row
    try:
        mine = plugin_of(reg).store
        theirs = ReminderStore(other)
        claims = [
            await mine.claim(created["id"], DUE),
            await theirs.claim(created["id"], DUE),
        ]
    finally:
        await other.close()

    assert claims.count(True) == 1


async def test_a_reminder_stuck_mid_fire_is_recovered(reg, utc_tz):
    created = await reg.command("clock", "create_reminder", reason="call Mum", at=DUE_ISO)
    store = plugin_of(reg).store
    await store.claim(created["id"], DUE)  # a process that then died

    fired = await plugin_of(reg).tick(now=DUE + timedelta(seconds=rem.STUCK_SECONDS + 1))

    assert fired == [created["id"]]


# --- recurrence arithmetic -------------------------------------------------

def test_weekdays_repeat_skips_the_weekend():
    friday = datetime(2031, 4, 11, 7, 0, tzinfo=UTC)
    assert rem.next_occurrence(friday, "weekdays", UTC).weekday() == 0  # Monday


def test_monthly_repeat_clamps_to_a_short_month():
    jan31 = datetime(2031, 1, 31, 7, 0, tzinfo=UTC)
    assert rem.next_occurrence(jan31, "monthly", UTC).day == 28  # February


def test_a_daily_repeat_keeps_its_wall_clock_time_across_a_dst_change():
    london = ZoneInfo("Europe/London")
    # 07:00 the morning before British Summer Time begins.
    before = datetime(2031, 3, 29, 7, 0, tzinfo=london)

    after = rem.next_occurrence(before, "daily", london)

    assert after.astimezone(london).strftime("%H:%M") == "07:00"
    # The clocks went forward, so it is an hour earlier in UTC than yesterday.
    assert after - before == timedelta(hours=23)


# --- dashboard and the model ----------------------------------------------

async def test_widget_data_sources(reg, utc_tz):
    d = reg.descriptor_for(SENSOR_ID)
    assert {w.type for w in d.widgets} == {"stat", "kv"}
    assert (await reg.query("clock", "clock"))["value"]
    assert (await reg.query("clock", "now"))["Date"]

    reminders = reg.descriptor_for(REMINDER_SENSOR_ID)
    assert {w.type for w in reminders.widgets} == {"stat", "table", "log"}
    assert all(w.data_source for w in reminders.widgets)

    created = await reg.command("clock", "create_reminder", reason="call Mum", at=DUE_ISO)
    assert (await reg.query("clock", "reminder_count"))["value"] == 1
    upcoming = await reg.query("clock", "upcoming")
    assert upcoming[0]["Reason"] == "call Mum"
    assert upcoming[0]["Repeat"] == "once"

    await reg.command("clock", "delete_reminder", id=created["id"])
    assert (await reg.query("clock", "reminder_log"))[0]["Status"] == "cancelled"


async def test_every_tool_reaches_the_llm(reg):
    tools = ToolRegistry()
    project_plugin_tools(reg, tools)

    assert {
        "clock.get_time", "clock.get_date", "clock.create_reminder",
        "clock.list_reminders", "clock.edit_reminder", "clock.delete_reminder",
        "clock.purge_reminders",
    } <= set(tools.tools)
    assert (await tools.dispatch("clock.get_date", {}))["weekday"]
    assert (await tools.dispatch("clock.list_reminders", {}))["spoken"] == "Nothing is set."
