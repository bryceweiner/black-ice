"""The home screen's single round trip."""

from blackice import db
from blackice.models import Event
from blackice.services import events, overview


async def test_zero_filled_histogram_covers_every_hour(data_dir):
    """A quiet hour has to appear as a zero, not go missing.

    SQLite only returns hours that have rows, and a chart drawn from those
    alone silently compresses quiet periods into busy ones.
    """
    await events.record(Event(sensor_id="cam.front", plugin="rtsp", summary="now"))

    result = await overview.overview()
    histogram = result["events"]["histogram"]

    assert len(histogram) == overview.HOURS
    assert sum(b["count"] for b in histogram) == 1
    assert [b["hour"] for b in histogram] == sorted(b["hour"] for b in histogram)


async def test_counts_group_by_state_and_threat(data_dir):
    for sid, state in [("a", "online"), ("b", "online"), ("c", "offline")]:
        await db.execute(
            "INSERT INTO sensors (id, plugin, name, state) VALUES (?, 'p', ?, ?)",
            (sid, sid, state),
        )
    eid = await events.record(Event(sensor_id="a", plugin="p", summary="x"))
    for threat, status in [("high", "open"), ("low", "open"), ("high", "closed")]:
        await db.execute(
            "INSERT INTO escalations (event_id, threat_level, status) VALUES (?, ?, ?)",
            (eid, threat, status),
        )

    result = await overview.overview()

    assert result["sensors"] == {
        "total": 3, "online": 2, "by_state": {"online": 2, "offline": 1},
    }
    # Closed escalations are not the operator's problem any more.
    assert result["escalations"] == {"open": 2, "by_threat": {"high": 1, "low": 1}}


async def test_empty_install_reports_zeroes_not_nulls(data_dir):
    """A fresh install still has to render: every tile reads a number."""
    result = await overview.overview()

    assert result["sensors"]["total"] == 0
    assert result["events"]["last_24h"] == 0
    assert result["escalations"]["open"] == 0
    assert result["alarms"] == {"total": 0, "armed": 0}
    assert result["llm"]["avg_latency_ms"] is None
    assert result["uptime_seconds"] >= 0


async def test_alarms_count_armed_rules(data_dir):
    for key, armed in [("k1", 1), ("k2", 0), ("k3", 1)]:
        rule_id = await db.execute(
            "INSERT INTO alarm_rules (plugin, key, name) VALUES ('p', ?, ?)", (key, key)
        )
        await db.execute(
            "INSERT INTO alarm_state (rule_id, armed) VALUES (?, ?)", (rule_id, armed)
        )

    result = await overview.overview()

    assert result["alarms"] == {"total": 3, "armed": 2}
