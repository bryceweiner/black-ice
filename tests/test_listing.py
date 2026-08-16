"""Search + date-range filtering, shared by every list page and the LLM tools."""

import pytest

from blackice import db
from blackice.models import Event
from blackice.services import events
from blackice.services.listing import fts_query


@pytest.fixture
async def seeded(data_dir):
    await db.execute(
        "INSERT INTO sensors (id, plugin, name) VALUES ('cam.front','rtsp','Front Door')"
    )
    await db.execute(
        "INSERT INTO sensors (id, plugin, name) VALUES ('cam.rear','rtsp','Back Gate')"
    )
    rows = [
        ("cam.front", "2026-01-01 09:00:00", "Person at front door"),
        ("cam.front", "2026-02-15 22:30:00", "Package delivered"),
        ("cam.rear", "2026-03-20 03:15:00", "Motion at back gate"),
        ("cam.rear", "2026-04-01 12:00:00", "Cat crossing the garden"),
    ]
    for sensor_id, ts, summary in rows:
        await events.record(
            Event(sensor_id=sensor_id, plugin="rtsp", kind="motion", summary=summary)
        )
        await db.execute(
            "UPDATE events SET ts = ? WHERE summary = ?", (ts, summary)
        )
    return rows


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("front door", '"front" "door"'),
        ('bad "quote', '"bad" "quote"'),
        ("a AND b OR c", '"a" "AND" "b" "OR" "c"'),
        ("wildcard*", '"wildcard"'),
        ("", ""),
        ("   ", ""),
    ],
)
def test_fts_query_neutralises_operator_syntax(raw, expected):
    """User text reaches MATCH directly, so FTS5 operators and stray quotes
    must be reduced to plain phrase terms rather than changing the query."""
    assert fts_query(raw) == expected


async def test_search_matches_summary(seeded):
    r = await events.list_events(q="package")
    assert r["total"] == 1
    assert r["rows"][0]["summary"] == "Package delivered"


async def test_search_with_hostile_input_does_not_error(seeded):
    for q in ['" OR 1=1 --', "NOT front", "*", '"""']:
        assert await events.list_events(q=q) is not None


async def test_date_range_filter(seeded):
    r = await events.list_events(start="2026-02-01", end="2026-03-31")
    assert r["total"] == 2
    assert {row["summary"] for row in r["rows"]} == {
        "Package delivered", "Motion at back gate"
    }


async def test_search_and_date_range_combine(seeded):
    assert (await events.list_events(q="motion", start="2026-01-01"))["total"] == 1
    assert (await events.list_events(q="motion", end="2026-01-31"))["total"] == 0


async def test_sensor_filter(seeded):
    assert (await events.list_events(sensor_id="cam.rear"))["total"] == 2


async def test_pagination_reports_total_not_page_size(seeded):
    r = await events.list_events(limit=2)
    assert len(r["rows"]) == 2
    assert r["total"] == 4


async def test_results_are_newest_first(seeded):
    rows = (await events.list_events())["rows"]
    assert [r["ts"] for r in rows] == sorted((r["ts"] for r in rows), reverse=True)
