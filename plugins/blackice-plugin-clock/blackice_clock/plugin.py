"""The clock plugin: what time it is, and what the owner asked to be told.

Two sensors from one plugin. `clock.local` is passive — it reads the wall clock
when asked and stores nothing. `clock.reminders` owns a table and a scheduler
loop, and emits an event the moment a reminder comes due; turning that event
into speech is `blackice/voice/announce.py`, not this file.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from datetime import datetime
from typing import Any

from blackice.models import (
    SEVERITY_INFO,
    Event,
    SensorDescriptor,
    ToolSpec,
    WidgetSpec,
)
from blackice.plugins.base import PluginContext, SensorPlugin

from . import reminders as rem
from .reading import reading, zone

SENSOR_ID = "clock.local"
REMINDER_SENSOR_ID = "clock.reminders"

GRACE_ENV = "CLOCK_REMINDER_GRACE_MINUTES"

#: How often the scheduler looks for due reminders. A reminder therefore fires
#: within this many seconds of its time, which is close enough for a spoken
#: alert and cheap enough to run in both processes.
POLL_SECONDS = 15.0

TIMEZONE_PARAM = {
    "type": "string",
    "description": (
        "Optional IANA timezone name, e.g. 'Europe/London' or 'America/New_York'. "
        "Omit for local time."
    ),
}

REPEAT_PARAM = {
    "type": "string",
    "enum": [*rem.REPEATS, "none"],
    "description": (
        "How often it recurs. Omit or 'none' for a one-off reminder. "
        "'weekdays' skips Saturday and Sunday."
    ),
}

AT_PARAM = {
    "type": "string",
    "description": (
        "When it should fire: ISO 8601 like '2026-08-17T07:00', or 'HH:MM' for the "
        "next time that clock time comes round. Work out relative times like "
        "'tomorrow morning' yourself — call clock.get_time first if you need to know "
        "what today is."
    ),
}


def _grace_minutes() -> int:
    try:
        return max(0, int(os.environ.get(GRACE_ENV, rem.DEFAULT_GRACE_MINUTES)))
    except ValueError:
        return rem.DEFAULT_GRACE_MINUTES


class ClockPlugin(SensorPlugin):
    name = "clock"
    version = "0.2.0"

    def __init__(self) -> None:
        self.ctx: PluginContext | None = None
        self.store: rem.ReminderStore | None = None
        self.task: asyncio.Task | None = None

    async def start(self, ctx: PluginContext) -> None:
        self.ctx = ctx
        self.store = rem.ReminderStore(ctx.db)
        await self.store.setup()
        self.task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self.task:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
            self.task = None
        self.ctx = None

    def describe(self) -> list[SensorDescriptor]:
        return [
            SensorDescriptor(
                id=SENSOR_ID,
                name="Clock",
                kind="clock",
                widgets=[
                    WidgetSpec(type="stat", title="Now", data_source="clock", span=4),
                    WidgetSpec(
                        type="kv", title="Date and time", data_source="now", span=8,
                    ),
                ],
                tools=[
                    ToolSpec(
                        name="get_time",
                        description=(
                            "The current time of day. Also returns today's date, so a "
                            "question about both needs only this call."
                        ),
                        parameters={
                            "type": "object",
                            "properties": {"timezone": TIMEZONE_PARAM},
                        },
                    ),
                    ToolSpec(
                        name="get_date",
                        description="Today's date, including the day of the week.",
                        parameters={
                            "type": "object",
                            "properties": {"timezone": TIMEZONE_PARAM},
                        },
                    ),
                ],
            ),
            SensorDescriptor(
                id=REMINDER_SENSOR_ID,
                name="Reminders",
                kind="reminder",
                widgets=[
                    WidgetSpec(
                        type="stat", title="Scheduled", data_source="reminder_count",
                        span=3,
                    ),
                    WidgetSpec(
                        type="table", title="Upcoming reminders",
                        data_source="upcoming", span=9,
                    ),
                    WidgetSpec(
                        type="log", title="Recently fired", data_source="reminder_log",
                        span=12,
                    ),
                ],
                tools=[
                    ToolSpec(
                        name="create_reminder",
                        description=(
                            "Set a reminder. At the given time the owner is told the "
                            "time and the reason, in their own words, so record the "
                            "reason as they said it."
                        ),
                        parameters={
                            "type": "object",
                            "properties": {
                                "reason": {
                                    "type": "string",
                                    "description": (
                                        "What to remind them of, phrased as they said "
                                        "it: 'call Mum', 'take the tablets'."
                                    ),
                                },
                                "at": AT_PARAM,
                                "repeat": REPEAT_PARAM,
                                "timezone": TIMEZONE_PARAM,
                            },
                            "required": ["reason", "at"],
                        },
                    ),
                    ToolSpec(
                        name="list_reminders",
                        description=(
                            "The reminders that are set. Call this before editing or "
                            "deleting one, to find its id and read it back."
                        ),
                        parameters={
                            "type": "object",
                            "properties": {
                                "status": {
                                    "type": "string",
                                    "enum": [*rem.STATUSES, "all"],
                                    "description":
                                        "Defaults to 'scheduled' — the ones still to come.",
                                },
                                "limit": {"type": "integer"},
                            },
                        },
                    ),
                    ToolSpec(
                        name="edit_reminder",
                        description=(
                            "Change a reminder's time, reason, or repeat. Also how a "
                            "cancelled one is restored: set status to 'scheduled'. "
                            "Only the fields you pass are changed."
                        ),
                        parameters={
                            "type": "object",
                            "properties": {
                                "id": {"type": "integer"},
                                "reason": {"type": "string"},
                                "at": AT_PARAM,
                                "repeat": REPEAT_PARAM,
                                "status": {"type": "string", "enum": list(rem.STATUSES)},
                                "timezone": TIMEZONE_PARAM,
                            },
                            "required": ["id"],
                        },
                    ),
                    ToolSpec(
                        name="delete_reminder",
                        description=(
                            "Cancel a reminder. It stops firing but is kept, so it can "
                            "be restored with edit_reminder if they change their mind."
                        ),
                        parameters={
                            "type": "object",
                            "properties": {"id": {"type": "integer"}},
                            "required": ["id"],
                        },
                    ),
                    ToolSpec(
                        name="purge_reminders",
                        description=(
                            "Permanently delete old cancelled, fired and missed "
                            "reminders. Never removes one that is still scheduled."
                        ),
                        parameters={
                            "type": "object",
                            "properties": {
                                "older_than_days": {
                                    "type": "integer",
                                    "description": "Defaults to 30.",
                                }
                            },
                        },
                    ),
                ],
            ),
        ]

    # --- tools -------------------------------------------------------------

    async def handle_command(self, cmd: str, **kwargs: Any) -> Any:
        handler = {
            "get_time": self._get_time,
            "get_date": self._get_date,
            "create_reminder": self._create_reminder,
            "list_reminders": self._list_reminders,
            "edit_reminder": self._edit_reminder,
            "delete_reminder": self._delete_reminder,
            "purge_reminders": self._purge_reminders,
        }.get(cmd)
        if handler is None:
            return await super().handle_command(cmd, **kwargs)
        try:
            return await handler(**kwargs)
        except ValueError as exc:
            # A caller's mistake — an unknown zone, a time in the past, a bad
            # id — is not a plugin fault. Report it and stay healthy.
            return {"error": str(exc)}
        except TypeError as exc:
            return {"error": f"bad arguments for {cmd}: {exc}"}

    async def _get_time(self, timezone: str | None = None) -> dict[str, Any]:
        now = reading(datetime.now(zone(timezone)))
        return {**now, "spoken": f"{now['time_12']} on {now['date_long']}"}

    async def _get_date(self, timezone: str | None = None) -> dict[str, Any]:
        now = reading(datetime.now(zone(timezone)))
        return {**now, "spoken": now["date_long"]}

    async def _create_reminder(
        self, reason: str = "", at: str = "", repeat: str | None = None,
        timezone: str | None = None,
    ) -> dict[str, Any]:
        row = await self._store().create(
            reason, at, None if repeat in ("", "none", "never") else repeat, timezone
        )
        described = rem.describe(row)
        return {**described, "created": True, "spoken": f"Set: {described['spoken']}"}

    async def _list_reminders(
        self, status: str | None = None, limit: int = 50
    ) -> dict[str, Any]:
        wanted = None if status == "all" else (status or "scheduled")
        if wanted is not None and wanted not in rem.STATUSES:
            raise ValueError(f"status must be one of {', '.join(rem.STATUSES)}, or 'all'")
        rows = [rem.describe(r) for r in await self._store().list(wanted, limit)]
        return {
            "reminders": rows,
            "count": len(rows),
            "spoken": (
                "; ".join(r["spoken"] for r in rows) if rows else "Nothing is set."
            ),
        }

    async def _edit_reminder(self, id: int = 0, **changes: Any) -> dict[str, Any]:
        allowed = {"reason", "at", "repeat", "status", "timezone"}
        if unknown := set(changes) - allowed:
            raise ValueError(f"cannot change {', '.join(sorted(unknown))}")
        row = await self._store().edit(int(id), **changes)
        described = rem.describe(row)
        return {**described, "updated": True, "spoken": f"Changed to: {described['spoken']}"}

    async def _delete_reminder(self, id: int = 0) -> dict[str, Any]:
        described = rem.describe(await self._store().cancel(int(id)))
        return {
            **described, "cancelled": True,
            "spoken": f"Cancelled: {described['spoken']}",
            "note": "Restorable with edit_reminder(id, status='scheduled').",
        }

    async def _purge_reminders(self, older_than_days: int = 30) -> dict[str, Any]:
        removed = await self._store().purge(int(older_than_days))
        return {
            "deleted": removed,
            "spoken": f"Deleted {removed} finished reminder{'' if removed == 1 else 's'}.",
        }

    # --- widgets -----------------------------------------------------------

    async def query(self, source: str, **kwargs: Any) -> Any:
        if source in ("clock", "now"):
            now = reading(datetime.now(zone(None)))
            if source == "clock":
                return {"value": now["time_12"], "label": now["date_long"]}
            return {
                "Time": now["time_12"],
                "24-hour": now["time_24"],
                "Date": now["date_long"],
                "ISO": now["iso"],
                "Time zone": now["timezone"],
                "UTC offset": now["utc_offset"],
            }

        if source == "reminder_count":
            rows = await self._store().list("scheduled", limit=1000)
            return {"value": len(rows), "label": "scheduled"}
        if source == "upcoming":
            rows = await self._store().list("scheduled", limit=20)
            return [
                {
                    "When": r["when"],
                    "Reason": r["reason"],
                    "Repeat": r["repeat"] or "once",
                }
                for r in (rem.describe(row) for row in rows)
            ]
        if source == "reminder_log":
            rows = await self._store().list(None, limit=50)
            return [
                {"Status": r["status"], "When": r["when"], "Reason": r["reason"]}
                for r in (rem.describe(row) for row in rows)
                if r["status"] in ("fired", "missed", "cancelled")
            ]
        return await super().query(source, **kwargs)

    # --- scheduler ---------------------------------------------------------

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(POLL_SECONDS)
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.ctx.log.exception("reminder tick failed")

    async def tick(self, now: datetime | None = None) -> list[int]:
        """One pass of the scheduler. Returns the ids fired, for tests.

        Sweeping the stale ones first means a reminder that is both overdue and
        past its grace window is retired rather than announced late.
        """
        store, now, grace = self._store(), now or rem.utc_now(), _grace_minutes()
        await store.recover_stuck(now)

        for row in await store.stale(now, grace):
            if await store.claim(row["id"], now):
                await store.miss(row, now)

        fired: list[int] = []
        for row in await store.due(now, grace):
            # Whoever wins the claim announces it; the other process sees
            # nothing to do. See ReminderStore.claim.
            if await store.claim(row["id"], now):
                await self._fire(row, now)
                fired.append(row["id"])
        return fired

    async def _fire(self, row: dict[str, Any], now: datetime) -> int:
        assert self.ctx is not None
        described = rem.describe(row)
        late = int((now - rem.from_sql(row["due_at"])).total_seconds())
        local_now = reading(now.astimezone(zone(row["timezone"])))

        # The reason is the owner's own words, arriving through the guarded
        # command channel — not sensor input — so it belongs in the summary
        # rather than in sensor_text. Neither can become a durable memory.
        event_id = await self.ctx.emit(
            Event(
                sensor_id=REMINDER_SENSOR_ID,
                severity=SEVERITY_INFO,
                kind="reminder",
                summary=f"Reminder: {row['reason']}",
                payload={
                    "reminder_id": row["id"],
                    "reason": row["reason"],
                    "due": described["when"],
                    "due_at": described["due_at"],
                    "repeat": row["repeat"],
                    "late_seconds": late,
                    "time_now": local_now["time_12"],
                    "date_now": local_now["date_long"],
                },
            )
        )
        await self._store().finish(row, now)
        return event_id

    def _store(self) -> rem.ReminderStore:
        if self.store is None:
            raise ValueError("the clock plugin is not running")
        return self.store
