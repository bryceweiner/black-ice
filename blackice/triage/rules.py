"""Tier 1: deterministic filtering, no model.

Everything is recorded regardless. This tier decides only what is worth paying
inference for -- so a chatty motion sensor cannot starve the queue.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time
from typing import Any

from .. import db

DEFAULT_KEY = "*"


@dataclass(slots=True)
class TriageConfig:
    sensor_id: str = DEFAULT_KEY
    dedup_seconds: int = 60
    rate_limit_per_hour: int = 120
    severity_floor: int = 0
    quiet_start: str | None = None
    quiet_end: str | None = None
    quiet_severity_floor: int = 0


@dataclass(slots=True)
class Decision:
    passed: bool
    reason: str

    @property
    def verdict(self) -> str:
        return "escalate" if self.passed else self.reason


async def config_for(sensor_id: str) -> TriageConfig:
    row = await db.fetchone(
        "SELECT * FROM triage_config WHERE sensor_id IN (?, ?)"
        " ORDER BY sensor_id = ? DESC LIMIT 1",
        (sensor_id, DEFAULT_KEY, sensor_id),
    )
    if not row:
        return TriageConfig(sensor_id)
    return TriageConfig(**{
        k: row[k] for k in TriageConfig.__slots__ if k in row
    })


def _in_quiet_hours(now: time, start: str | None, end: str | None) -> bool:
    if not start or not end:
        return False
    s = time.fromisoformat(start)
    e = time.fromisoformat(end)
    return s <= now < e if s <= e else (now >= s or now < e)


async def _armed_alarm_matches(event: dict[str, Any]) -> bool:
    """True when an armed alarm rule actually responds to this event.

    Matching is by declared event kind, not merely by sensor. A watchdog rule
    about the *absence* of events ("heartbeat stopped") declares no kinds, and
    must not force every incoming event through the models -- which is what
    "any armed rule on this sensor" would do.
    """
    rows = await db.fetchall(
        """SELECT r.spec FROM alarm_rules r
             JOIN alarm_state s ON s.rule_id = r.id
            WHERE r.sensor_id = ? AND s.armed = 1""",
        (event["sensor_id"],),
    )
    for row in rows:
        spec = db.loads(row["spec"])
        if spec.get("match_all"):
            return True
        if event["kind"] in (spec.get("kinds") or []):
            return True
    return False


async def evaluate(event: dict[str, Any], cfg: TriageConfig | None = None) -> Decision:
    cfg = cfg or await config_for(event["sensor_id"])

    # An armed alarm overrides every suppression below it. Being noisy is not a
    # reason to ignore the one sensor the user explicitly asked to watch.
    if await _armed_alarm_matches(event):
        return Decision(True, "armed_alarm")

    floor = cfg.severity_floor
    ts = str(event.get("ts") or "")
    if len(ts) >= 16 and _in_quiet_hours(
        time.fromisoformat(ts[11:16]), cfg.quiet_start, cfg.quiet_end
    ):
        floor = max(floor, cfg.quiet_severity_floor)

    if event.get("severity", 0) < floor:
        return Decision(False, "below_severity_floor")

    duplicate = await db.fetchval(
        """SELECT count(*) FROM events
            WHERE sensor_id = ? AND kind = ? AND id <> ?
              AND ts >= datetime(?, ?)""",
        (event["sensor_id"], event["kind"], event["id"],
         event["ts"], f"-{cfg.dedup_seconds} seconds"),
    )
    if duplicate:
        return Decision(False, "duplicate")

    recent = await db.fetchval(
        "SELECT count(*) FROM events WHERE sensor_id = ? AND ts >= datetime(?, '-1 hour')",
        (event["sensor_id"], event["ts"]),
    )
    if recent and recent > cfg.rate_limit_per_hour:
        return Decision(False, "rate_limited")

    return Decision(True, "passed")
