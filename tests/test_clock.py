"""The clock plugin: tool answers, widget data, and timezone handling."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from blackice_clock import SENSOR_ID, ClockPlugin

from blackice import db
from blackice.llm.tools import ToolRegistry, project_plugin_tools
from blackice.plugins.registry import Registry
from blackice.services import events


@pytest.fixture
async def reg(data_dir):
    r = Registry()
    await r.start_plugin(ClockPlugin, events.record)
    yield r
    await r.stop_all()


async def test_discovery_finds_installed_plugin(data_dir):
    assert "clock" in [c.name for c in Registry().discover()]


async def test_start_projects_the_sensor(reg, data_dir):
    sensor = await db.fetchone("SELECT * FROM sensors WHERE id = ?", (SENSOR_ID,))
    assert sensor is not None
    assert sensor["plugin"] == "clock"


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


async def test_widget_data_sources(reg):
    d = reg.descriptor_for(SENSOR_ID)
    assert {w.type for w in d.widgets} == {"stat", "kv"}

    stat = await reg.query("clock", "clock")
    assert stat["value"] and stat["label"]

    kv = await reg.query("clock", "now")
    assert kv["Date"] and kv["Time"]


async def test_tools_are_offered_to_the_llm(reg):
    tools = ToolRegistry()
    project_plugin_tools(reg, tools)

    assert {"clock.get_time", "clock.get_date"} <= set(tools.tools)
    assert (await tools.dispatch("clock.get_date", {}))["weekday"]
