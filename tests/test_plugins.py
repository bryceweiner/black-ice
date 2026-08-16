"""The plugin contract: discovery, private storage, projection into core,
and supervisor isolation."""

import asyncio

import pytest
from blackice_heartbeat import SENSOR_ID, HeartbeatPlugin

from blackice import db
from blackice.plugins import store
from blackice.plugins.registry import Registry
from blackice.plugins.supervisor import PluginFailure, Supervisor
from blackice.services import events


@pytest.fixture
async def reg(data_dir):
    r = Registry()
    await r.start_plugin(HeartbeatPlugin, events.record)
    yield r
    await r.stop_all()


async def test_discovery_finds_installed_plugin(data_dir):
    names = [c.name for c in Registry().discover()]
    assert "heartbeat" in names


async def test_start_projects_sensors_and_alarm_rules(reg, data_dir):
    sensor = await db.fetchone("SELECT * FROM sensors WHERE id = ?", (SENSOR_ID,))
    assert sensor is not None
    assert sensor["plugin"] == "heartbeat"

    rule = await db.fetchone(
        "SELECT * FROM alarm_rules WHERE plugin='heartbeat' AND key='missed_beat'"
    )
    assert rule is not None
    state = await db.fetchone(
        "SELECT armed FROM alarm_state WHERE rule_id = ?", (rule["id"],)
    )
    assert state["armed"] == 1  # plugin declared default_armed


async def test_widgets_are_declared(reg):
    d = reg.descriptor_for(SENSOR_ID)
    assert d is not None
    assert {w.type for w in d.widgets} == {"stat", "timeseries", "log"}
    assert all(w.data_source for w in d.widgets)


async def test_emit_records_event_and_updates_sensor(reg, data_dir):
    result = await reg.command("heartbeat", "beat_now")
    event_id = result["event_id"]

    row = await events.get(event_id)
    assert row["sensor_id"] == SENSOR_ID
    assert row["plugin"] == "heartbeat"  # stamped by PluginContext, not the plugin
    assert row["payload"]["manual"] is True

    sensor = await db.fetchone("SELECT last_seen FROM sensors WHERE id = ?", (SENSOR_ID,))
    assert sensor["last_seen"] is not None


async def test_plugin_writes_to_its_own_database(reg, data_dir):
    await reg.command("heartbeat", "beat_now")
    assert (data_dir / "plugins" / "heartbeat.db").exists()

    conn = await store.open_store("heartbeat")
    row = await (await conn.execute("SELECT count(*) FROM beats")).fetchone()
    assert row[0] >= 1

    # ...and that table exists nowhere in the core database.
    core = await db.fetchall("SELECT name FROM sqlite_master WHERE name='beats'")
    assert core == []


async def test_query_backs_widget_data_sources(reg):
    await reg.command("heartbeat", "beat_now")
    assert (await reg.query("heartbeat", "beat_count"))["value"] >= 1
    assert isinstance(await reg.query("heartbeat", "recent_beats"), list)


async def test_failure_is_contained_and_recorded(reg, data_dir):
    await reg.command("heartbeat", "simulate_failure")

    with pytest.raises(PluginFailure):
        await reg.command("heartbeat", "beat_now")

    health = reg.supervisors["heartbeat"].health()
    assert health["state"] == "unhealthy"
    assert "simulated plugin failure" in health["last_error"]

    row = await db.fetchone("SELECT * FROM plugin_health WHERE plugin='heartbeat'")
    assert row["state"] == "unhealthy"

    # The service survives, and the plugin recovers on its next good call.
    await reg.command("heartbeat", "beat_now")
    assert reg.supervisors["heartbeat"].health()["state"] == "healthy"


async def test_hanging_plugin_times_out(data_dir):
    class Hanger(HeartbeatPlugin):
        name = "hanger"

        async def handle_command(self, cmd, **kw):
            await asyncio.sleep(60)

    from blackice.plugins.base import PluginContext

    plugin = Hanger()
    ctx = PluginContext("hanger", await store.open_store("hanger"), events.record)
    sup = Supervisor(plugin, ctx, timeout=0.1)
    await sup.start()

    with pytest.raises(PluginFailure, match="timed out"):
        await sup.call("handle_command", "beat_now")
    assert sup.health()["state"] == "unhealthy"
    await sup.stop()


async def test_unknown_command_and_source_raise(reg):
    with pytest.raises(PluginFailure):
        await reg.command("heartbeat", "no_such_command")
    with pytest.raises(PluginFailure):
        await reg.query("heartbeat", "no_such_source")
