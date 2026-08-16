"""Reading the wall clock, and naming the zone it was read in."""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

#: IANA name (e.g. "Europe/London") used when a caller does not name one.
#: Unset means "whatever this machine is set to".
TZ_ENV = "CLOCK_TIMEZONE"


def zone(name: str | None = None) -> Any:
    """Resolve a timezone name, or raise ValueError with a speakable message."""
    name = (name or os.environ.get(TZ_ENV) or "").strip()
    if not name:
        return datetime.now().astimezone().tzinfo  # system local, DST applied
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"unknown timezone {name!r}") from exc


def spoken_time(now: datetime) -> str:
    """"7:05 PM" -- how a clock is read aloud, not how it is stored."""
    return f"{now.hour % 12 or 12}:{now.minute:02d} {'AM' if now.hour < 12 else 'PM'}"


def reading(now: datetime) -> dict[str, Any]:
    """Every field either surface might want, from one instant."""
    weekday, month = now.strftime("%A"), now.strftime("%B")
    return {
        "time_12": spoken_time(now),
        "time_24": now.strftime("%H:%M"),
        "seconds": now.second,
        "date": now.strftime("%Y-%m-%d"),
        "date_long": f"{weekday}, {month} {now.day}, {now.year}",
        "weekday": weekday,
        "day": now.day,
        "month": month,
        "year": now.year,
        "timezone": str(now.tzname() or ""),
        "utc_offset": now.strftime("%z"),
        "iso": now.isoformat(timespec="seconds"),
    }
