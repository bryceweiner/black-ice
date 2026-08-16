from __future__ import annotations

from typing import Any

from .. import db
from ..bus import bus
from .listing import list_rows


async def list_sensors(
    q: str | None = None, group_id: int | None = None,
    limit: int = 200, offset: int = 0,
) -> dict[str, Any]:
    """List monitoring sensors, optionally filtered by name or group."""
    where, params = [], []
    if group_id is not None:
        where.append(
            "t.id IN (SELECT sensor_id FROM sensor_group_members WHERE group_id = ?)"
        )
        params.append(group_id)
    return await list_rows(
        table="sensors",
        columns="t.id, t.plugin, t.name, t.kind, t.state, t.first_seen, t.last_seen",
        q=q, where=where, params=params, ts_col="last_seen",
        limit=limit, offset=offset,
    )


async def get_sensor(sensor_id: str) -> dict[str, Any] | None:
    """Full detail for one sensor, including its declared widgets and streams."""
    row = await db.fetchone("SELECT * FROM sensors WHERE id = ?", (sensor_id,))
    if row is None:
        return None
    row["descriptor"] = db.loads(row["descriptor"])
    row["groups"] = await db.fetchall(
        "SELECT g.id, g.name FROM sensor_groups g"
        " JOIN sensor_group_members m ON m.group_id = g.id WHERE m.sensor_id = ?",
        (sensor_id,),
    )
    row["alarms"] = await db.fetchall(
        "SELECT r.id, r.key, r.name, r.description, COALESCE(s.armed, 0) AS armed"
        " FROM alarm_rules r LEFT JOIN alarm_state s ON s.rule_id = r.id"
        " WHERE r.sensor_id = ? ORDER BY r.name",
        (sensor_id,),
    )
    return row


async def set_state(sensor_id: str, state: str) -> None:
    await db.execute(
        "UPDATE sensors SET state = ?, last_seen = datetime('now') WHERE id = ?",
        (state, sensor_id),
    )
    await bus.publish("sensor_state", {"sensor_id": sensor_id, "state": state})
