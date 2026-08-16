"""REST surface. Every handler delegates to the service layer -- the same
functions the LLM calls as tools."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException, status

from ..config import get_settings
from ..llm.harness import harness
from ..models import Trust
from ..plugins.registry import registry
from ..plugins.supervisor import PluginFailure
from ..services import alarms, escalations, events, groups, sensors
from ..services.groups import GroupError
from .auth import require_user

router = APIRouter(prefix="/api", dependencies=[Depends(require_user)])


@router.get("/me")
async def me(user: str = Depends(require_user)) -> dict:
    """Session check. The dashboard gates its whole tree on this."""
    return {"username": user, "assistant": get_settings().assistant_name}


def _found(row: Any, what: str = "Not found") -> Any:
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, what)
    return row


def _ok(fn):
    """Turn service-level errors into 400s rather than 500s."""

    async def wrapper(*a, **kw):
        try:
            return await fn(*a, **kw)
        except (GroupError, alarms.AlarmError, escalations.EscalationError) as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
        except PluginFailure as exc:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc

    return wrapper


# --- sensors and groups ----------------------------------------------------

@router.get("/sensors")
async def list_sensors(q: str | None = None, group_id: int | None = None,
                       limit: int = 200, offset: int = 0):
    return await sensors.list_sensors(q, group_id, limit, offset)


@router.get("/sensors/{sensor_id}")
async def get_sensor(sensor_id: str):
    return _found(await sensors.get_sensor(sensor_id), "Unknown sensor")


@router.get("/sensors/{sensor_id}/widgets/{source}")
async def widget_data(sensor_id: str, source: str):
    """Back a plugin-declared widget with its data."""
    sup = registry.plugin_of(sensor_id)
    if sup is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown sensor")
    return {"data": await _ok(registry.query)(sup.name, source)}


@router.get("/groups")
async def list_groups():
    return await groups.list_groups()


@router.post("/groups")
async def create_group(name: Annotated[str, Body(embed=True)]):
    return await _ok(groups.create_group)(name)


@router.patch("/groups/{group_id}")
async def update_group(group_id: int, name: str | None = Body(None, embed=True),
                       collapsed: bool | None = Body(None, embed=True)):
    result = {}
    if name is not None:
        result |= await _ok(groups.rename_group)(group_id, name)
    if collapsed is not None:
        result |= await _ok(groups.set_collapsed)(group_id, collapsed)
    return result


@router.delete("/groups/{group_id}")
async def delete_group(group_id: int):
    return await groups.delete_group(group_id)


@router.post("/groups/{group_id}/sensors/{sensor_id}")
async def add_to_group(group_id: int, sensor_id: str):
    return await _ok(groups.add_sensor)(group_id, sensor_id)


@router.delete("/groups/{group_id}/sensors/{sensor_id}")
async def remove_from_group(group_id: int, sensor_id: str):
    return await groups.remove_sensor(group_id, sensor_id)


# --- events and escalations ------------------------------------------------

@router.get("/events")
async def list_events(q: str | None = None, start: str | None = None,
                      end: str | None = None, sensor_id: str | None = None,
                      limit: int = 100, offset: int = 0):
    return await events.list_events(q, start, end, sensor_id, limit, offset)


@router.get("/events/{event_id}")
async def get_event(event_id: int):
    return _found(await events.get(event_id), "Unknown event")


@router.get("/escalations")
async def list_escalations(q: str | None = None, start: str | None = None,
                           end: str | None = None, status_filter: str | None = None,
                           limit: int = 100, offset: int = 0):
    return await escalations.list_escalations(
        q, start, end, status_filter, limit, offset)


@router.get("/escalations/{escalation_id}")
async def get_escalation(escalation_id: int):
    return _found(await escalations.get_escalation(escalation_id), "Unknown escalation")


@router.patch("/escalations/{escalation_id}")
async def update_escalation(escalation_id: int,
                            status_value: Annotated[str, Body(embed=True, alias="status")]):
    return await _ok(escalations.set_status)(escalation_id, status_value)


@router.post("/escalations/{escalation_id}/verdict")
async def add_verdict(escalation_id: int,
                      verdict: Annotated[str, Body(embed=True)],
                      note: str | None = Body(None, embed=True)):
    return await _ok(escalations.record_verdict)(escalation_id, verdict, note)


# --- alarms ----------------------------------------------------------------

@router.get("/alarms")
async def list_alarms(q: str | None = None, armed: bool | None = None,
                      plugin: str | None = None):
    return await alarms.list_alarms(q, armed, plugin)


@router.post("/alarms/{rule_id}/armed")
async def set_armed(rule_id: int, armed: Annotated[bool, Body(embed=True)]):
    return await _ok(alarms.set_armed)(rule_id, armed)


@router.post("/alarms/all")
async def set_all_alarms(armed: Annotated[bool, Body(embed=True)]):
    return await alarms.set_all(armed)


# --- assistant console -----------------------------------------------------

@router.post("/chat")
async def chat(message: Annotated[str, Body(embed=True)],
               session_id: str = Body("console", embed=True)):
    reply = await harness.run(
        message, channel="console", trust=Trust.USER, session_id=session_id
    )
    return {"reply": reply, "session_id": session_id}


@router.get("/plugins")
async def plugin_health():
    return registry.health()
