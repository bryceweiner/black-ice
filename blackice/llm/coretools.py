"""Register the service layer as LLM tools.

Every dashboard action is a service function, and every service function is a
tool. That is what makes "anything you can click, you can say" true by
construction rather than by keeping two command surfaces in sync.
"""

from __future__ import annotations

from typing import Any

from ..memory.store import memory
from ..services import alarms, escalations, events, groups, sensors
from .tools import ToolRegistry
from .tools import registry as default_registry


def register_core_tools(reg: ToolRegistry | None = None) -> ToolRegistry:
    reg = reg or default_registry

    # Direct registrations: signature and docstring are already tool-shaped.
    for fn in (
        sensors.list_sensors,
        sensors.get_sensor,
        groups.list_groups,
        groups.create_group,
        groups.rename_group,
        groups.delete_group,
        escalations.list_escalations,
        escalations.get_escalation,
        escalations.set_status,
        alarms.list_alarms,
    ):
        reg.register(fn)

    reg.register(events.list_events, name="search_events",
                 description="Search recorded sensor events by text and date range.")
    reg.register(escalations.list_escalations, name="search_escalations",
                 description="Search escalated events by text and date range.")

    # Wrappers where the LLM-facing signature should differ from the service.
    @reg.tool(description="Arm an alarm, by numeric id or by name.")
    async def arm_alarm(rule: str) -> dict[str, Any]:
        return await alarms.arm_alarm(rule)

    @reg.tool(description="Disarm an alarm, by numeric id or by name.")
    async def disarm_alarm(rule: str) -> dict[str, Any]:
        return await alarms.disarm_alarm(rule)

    @reg.tool(description="Arm or disarm every alarm at once.")
    async def set_all_alarms(armed: bool) -> dict[str, Any]:
        return await alarms.set_all(armed)

    @reg.tool(description="Add a sensor to a group.")
    async def add_sensor_to_group(group_id: int, sensor_id: str) -> dict[str, Any]:
        return await groups.add_sensor(group_id, sensor_id)

    @reg.tool(description="Remove a sensor from a group.")
    async def remove_sensor_from_group(group_id: int, sensor_id: str) -> dict[str, Any]:
        return await groups.remove_sensor(group_id, sensor_id)

    @reg.tool(
        description="Record whether an escalation was correct: "
                    "true_positive, false_positive, or unclear."
    )
    async def record_verdict(
        escalation_id: int, verdict: str, note: str | None = None
    ) -> dict[str, Any]:
        return await escalations.record_verdict(escalation_id, verdict, note)

    @reg.tool(description="Search your own long-term memory for relevant facts.")
    async def recall_memory(query: str, limit: int = 8) -> list[dict[str, Any]]:
        return await memory.recall(query, limit)

    return reg
