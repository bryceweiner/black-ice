from __future__ import annotations

from typing import Any

from .. import db
from ..bus import bus


class AlarmError(Exception):
    """An alarm operation could not be completed."""


SELECT = """SELECT r.id, r.plugin, r.key, r.name, r.description, r.sensor_id,
                   s.name AS sensor_name, COALESCE(a.armed, 0) AS armed,
                   a.updated_at, a.updated_by
              FROM alarm_rules r
              LEFT JOIN alarm_state a ON a.rule_id = r.id
              LEFT JOIN sensors s ON s.id = r.sensor_id"""


async def list_alarms(
    q: str | None = None, armed: bool | None = None, plugin: str | None = None
) -> list[dict[str, Any]]:
    """List alarm rules and whether each is armed."""
    where, params = [], []
    if q:
        where.append("(r.name LIKE ? OR r.description LIKE ? OR s.name LIKE ?)")
        params += [f"%{q}%"] * 3
    if armed is not None:
        where.append("COALESCE(a.armed, 0) = ?")
        params.append(int(armed))
    if plugin:
        where.append("r.plugin = ?")
        params.append(plugin)
    sql = SELECT + (f" WHERE {' AND '.join(where)}" if where else "")
    return await db.fetchall(sql + " ORDER BY r.plugin, r.name", tuple(params))


async def get_alarm(rule_id: int) -> dict[str, Any] | None:
    return await db.fetchone(SELECT + " WHERE r.id = ?", (rule_id,))


async def _resolve(rule: int | str) -> int:
    """Accept a numeric id or a name/key, so voice can say 'the garage alarm'."""
    if isinstance(rule, int) or str(rule).isdigit():
        return int(rule)
    matches = await db.fetchall(
        "SELECT id, name FROM alarm_rules WHERE key = ? OR name LIKE ?",
        (rule, f"%{rule}%"),
    )
    if not matches:
        raise AlarmError(f"No alarm matching {rule!r}")
    if len(matches) > 1:
        names = ", ".join(m["name"] for m in matches)
        raise AlarmError(f"{rule!r} matches several alarms: {names}")
    return matches[0]["id"]


async def set_armed(rule: int | str, armed: bool, by: str = "user") -> dict[str, Any]:
    rule_id = await _resolve(rule)
    if not await db.fetchval("SELECT 1 FROM alarm_rules WHERE id = ?", (rule_id,)):
        raise AlarmError(f"Unknown alarm rule {rule_id}")
    await db.execute(
        """INSERT INTO alarm_state (rule_id, armed, updated_at, updated_by)
           VALUES (?, ?, datetime('now'), ?)
           ON CONFLICT(rule_id) DO UPDATE SET
             armed=excluded.armed, updated_at=excluded.updated_at,
             updated_by=excluded.updated_by""",
        (rule_id, int(armed), by),
    )
    row = await get_alarm(rule_id)
    await bus.publish("alarm_state", row)
    return row


async def arm_alarm(rule: int | str) -> dict[str, Any]:
    """Arm an alarm rule, by id or by name."""
    return await set_armed(rule, True)


async def disarm_alarm(rule: int | str) -> dict[str, Any]:
    """Disarm an alarm rule, by id or by name."""
    return await set_armed(rule, False)


async def set_all(armed: bool) -> dict[str, Any]:
    """Arm or disarm every alarm rule at once."""
    rules = await db.fetchall("SELECT id FROM alarm_rules")
    for r in rules:
        await set_armed(r["id"], armed)
    return {"count": len(rules), "armed": armed}
