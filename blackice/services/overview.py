"""What the house looks like right now, in one round trip.

The home screen would otherwise open six endpoints and stitch them together in
the browser. Everything here is a count over an index; nothing walks a table.
"""

from __future__ import annotations

import time
from typing import Any

from .. import db
from ..plugins.registry import registry

# Set once at import, which is process start for every path that serves the API.
_STARTED = time.monotonic()

HOURS = 24


def uptime_seconds() -> int:
    return int(time.monotonic() - _STARTED)


async def _by(column: str, table: str, where: str = "") -> dict[str, int]:
    rows = await db.fetchall(
        f"SELECT {column} AS k, count(*) AS n FROM {table} {where} GROUP BY {column}"
    )
    return {str(r["k"] or "unknown"): r["n"] for r in rows}


async def overview() -> dict[str, Any]:
    """Uptime, throughput and the current state of every subsystem."""
    sensors_by_state = await _by("state", "sensors")
    events_by_tier = await _by("tier", "events", "WHERE ts >= datetime('now', '-24 hours')")
    escalations_by_threat = await _by("threat_level", "escalations", "WHERE status = 'open'")

    # One row per hour for the last 24, zero-filled: SQLite only returns hours
    # that actually have events, and a chart with holes in it lies about quiet
    # periods.
    buckets = {
        r["h"]: r["n"]
        for r in await db.fetchall(
            "SELECT strftime('%Y-%m-%d %H:00', ts) AS h, count(*) AS n FROM events"
            " WHERE ts >= datetime('now', ?) GROUP BY h", (f"-{HOURS - 1} hours",)
        )
    }
    hours = await db.fetchall(
        "WITH RECURSIVE h(i) AS (SELECT 0 UNION ALL SELECT i + 1 FROM h WHERE i < ?)"
        " SELECT strftime('%Y-%m-%d %H:00', datetime('now', '-' || (? - i) || ' hours')) AS h"
        " FROM h ORDER BY i", (HOURS - 1, HOURS - 1),
    )
    histogram = [{"hour": r["h"], "count": buckets.get(r["h"], 0)} for r in hours]

    alarms = await db.fetchone(
        "SELECT count(*) AS total, COALESCE(sum(s.armed), 0) AS armed"
        " FROM alarm_rules r LEFT JOIN alarm_state s ON s.rule_id = r.id"
    )
    latency = await db.fetchval(
        "SELECT avg(latency_ms) FROM llm_turns"
        " WHERE latency_ms IS NOT NULL AND ts >= datetime('now', '-24 hours')"
    )

    return {
        "uptime_seconds": uptime_seconds(),
        "sensors": {
            "total": sum(sensors_by_state.values()),
            "online": sensors_by_state.get("online", 0),
            "by_state": sensors_by_state,
        },
        "events": {
            "total": await db.fetchval("SELECT count(*) FROM events") or 0,
            "last_24h": sum(b["count"] for b in histogram),
            "by_tier": events_by_tier,
            "histogram": histogram,
        },
        "escalations": {
            "open": sum(escalations_by_threat.values()),
            "by_threat": escalations_by_threat,
        },
        "alarms": {"total": alarms["total"] or 0, "armed": alarms["armed"] or 0},
        "plugins": registry.health(),
        "llm": {"avg_latency_ms": round(latency) if latency else None},
    }
