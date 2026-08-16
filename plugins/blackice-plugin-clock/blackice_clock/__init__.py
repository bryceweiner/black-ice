"""Clock plugin. Answers "what time is it?" and "what's today's date?" for the
assistant, and puts the same reading on the dashboard.

There is no hardware and no polling loop: the clock is read on demand, so the
plugin emits no events and holds no state. Bad input (an unknown timezone)
comes back as an ``error`` field rather than an exception, because anything
raised out of a plugin call marks the whole plugin unhealthy.
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from blackice.models import SensorDescriptor, ToolSpec, WidgetSpec
from blackice.plugins.base import PluginContext, SensorPlugin

SENSOR_ID = "clock.local"

#: IANA name (e.g. "Europe/London") used when a caller does not name one.
#: Unset means "whatever this machine is set to".
TZ_ENV = "CLOCK_TIMEZONE"

TIMEZONE_PARAM = {
    "type": "object",
    "properties": {
        "timezone": {
            "type": "string",
            "description": (
                "Optional IANA timezone name, e.g. 'Europe/London' or "
                "'America/New_York'. Omit for local time."
            ),
        }
    },
}


def _zone(name: str | None) -> Any:
    """Resolve a timezone name, or raise ValueError with a speakable message."""
    name = (name or os.environ.get(TZ_ENV) or "").strip()
    if not name:
        return datetime.now().astimezone().tzinfo  # system local, DST applied
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"unknown timezone {name!r}") from exc


def _reading(now: datetime) -> dict[str, Any]:
    """Every field either surface might want, from one instant."""
    hour12 = now.hour % 12 or 12
    meridiem = "AM" if now.hour < 12 else "PM"
    time_12 = f"{hour12}:{now.minute:02d} {meridiem}"
    weekday, month = now.strftime("%A"), now.strftime("%B")
    date_long = f"{weekday}, {month} {now.day}, {now.year}"
    return {
        "time_12": time_12,
        "time_24": now.strftime("%H:%M"),
        "seconds": now.second,
        "date": now.strftime("%Y-%m-%d"),
        "date_long": date_long,
        "weekday": weekday,
        "day": now.day,
        "month": month,
        "year": now.year,
        "timezone": str(now.tzname() or ""),
        "utc_offset": now.strftime("%z"),
        "iso": now.isoformat(timespec="seconds"),
    }


class ClockPlugin(SensorPlugin):
    name = "clock"
    version = "0.1.0"

    def __init__(self) -> None:
        self.ctx: PluginContext | None = None

    async def start(self, ctx: PluginContext) -> None:
        self.ctx = ctx

    async def stop(self) -> None:
        self.ctx = None

    def describe(self) -> list[SensorDescriptor]:
        return [
            SensorDescriptor(
                id=SENSOR_ID,
                name="Clock",
                kind="clock",
                widgets=[
                    WidgetSpec(
                        type="stat", title="Now", data_source="clock", span=4,
                    ),
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
                        parameters=TIMEZONE_PARAM,
                    ),
                    ToolSpec(
                        name="get_date",
                        description="Today's date, including the day of the week.",
                        parameters=TIMEZONE_PARAM,
                    ),
                ],
            )
        ]

    async def handle_command(self, cmd: str, **kwargs: Any) -> Any:
        if cmd not in ("get_time", "get_date"):
            return await super().handle_command(cmd, **kwargs)
        try:
            reading = _reading(datetime.now(_zone(kwargs.get("timezone"))))
        except ValueError as exc:
            # A caller's typo is not a plugin fault; report it, stay healthy.
            return {"error": str(exc)}
        spoken = (
            f"{reading['time_12']} on {reading['date_long']}"
            if cmd == "get_time"
            else reading["date_long"]
        )
        return {**reading, "spoken": spoken}

    async def query(self, source: str, **kwargs: Any) -> Any:
        now = _reading(datetime.now(_zone(None)))
        if source == "clock":
            return {"value": now["time_12"], "label": now["date_long"]}
        if source == "now":
            return {
                "Time": now["time_12"],
                "24-hour": now["time_24"],
                "Date": now["date_long"],
                "ISO": now["iso"],
                "Time zone": now["timezone"],
                "UTC offset": now["utc_offset"],
            }
        return await super().query(source, **kwargs)
