from __future__ import annotations

from typing import Any

from .. import db
from ..bus import bus


class GroupError(Exception):
    """A group operation could not be completed."""


async def list_groups(include_sensors: bool = True) -> list[dict[str, Any]]:
    """List sensor groups and their members."""
    groups = await db.fetchall("SELECT * FROM sensor_groups ORDER BY name")
    if include_sensors:
        for g in groups:
            g["sensors"] = await db.fetchall(
                "SELECT s.id, s.name, s.kind, s.state FROM sensors s"
                " JOIN sensor_group_members m ON m.sensor_id = s.id"
                " WHERE m.group_id = ? ORDER BY s.name",
                (g["id"],),
            )
    return groups


async def create_group(name: str) -> dict[str, Any]:
    """Create a sensor group."""
    name = name.strip()
    if not name:
        raise GroupError("Group name cannot be empty")
    if await db.fetchval("SELECT id FROM sensor_groups WHERE name = ?", (name,)):
        raise GroupError(f"A group named {name!r} already exists")
    group_id = await db.execute("INSERT INTO sensor_groups (name) VALUES (?)", (name,))
    await bus.publish("sensor_state", {"groups_changed": True})
    return {"id": group_id, "name": name}


async def rename_group(group_id: int, name: str) -> dict[str, Any]:
    """Rename a sensor group."""
    name = name.strip()
    if not name:
        raise GroupError("Group name cannot be empty")
    await db.execute("UPDATE sensor_groups SET name = ? WHERE id = ?", (name, group_id))
    await bus.publish("sensor_state", {"groups_changed": True})
    return {"id": group_id, "name": name}


async def delete_group(group_id: int) -> dict[str, Any]:
    """Delete a sensor group. The sensors themselves are untouched."""
    await db.execute("DELETE FROM sensor_groups WHERE id = ?", (group_id,))
    await bus.publish("sensor_state", {"groups_changed": True})
    return {"deleted": group_id}


async def set_collapsed(group_id: int, collapsed: bool) -> dict[str, Any]:
    """Remember whether a group is collapsed in the sidebar."""
    await db.execute(
        "UPDATE sensor_groups SET collapsed = ? WHERE id = ?", (int(collapsed), group_id)
    )
    return {"id": group_id, "collapsed": collapsed}


async def add_sensor(group_id: int, sensor_id: str) -> dict[str, Any]:
    """Add a sensor to a group."""
    if not await db.fetchval("SELECT 1 FROM sensors WHERE id = ?", (sensor_id,)):
        raise GroupError(f"Unknown sensor {sensor_id!r}")
    if not await db.fetchval("SELECT 1 FROM sensor_groups WHERE id = ?", (group_id,)):
        raise GroupError(f"Unknown group {group_id}")
    await db.execute(
        "INSERT INTO sensor_group_members (group_id, sensor_id) VALUES (?, ?)"
        " ON CONFLICT DO NOTHING",
        (group_id, sensor_id),
    )
    await bus.publish("sensor_state", {"groups_changed": True})
    return {"group_id": group_id, "sensor_id": sensor_id}


async def remove_sensor(group_id: int, sensor_id: str) -> dict[str, Any]:
    """Remove a sensor from a group."""
    await db.execute(
        "DELETE FROM sensor_group_members WHERE group_id = ? AND sensor_id = ?",
        (group_id, sensor_id),
    )
    await bus.publish("sensor_state", {"groups_changed": True})
    return {"group_id": group_id, "sensor_id": sensor_id, "removed": True}
