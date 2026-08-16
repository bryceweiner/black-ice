"""Clock plugin. Answers "what time is it?" and "what's the date?", and keeps
the reminders the owner sets by voice.

Reading the clock needs no hardware and no storage, so that half is passive.
Reminders are the opposite: a table, a scheduler loop, and an event emitted the
moment one comes due.
"""

from __future__ import annotations

from .plugin import REMINDER_SENSOR_ID, SENSOR_ID, ClockPlugin
from .reading import TZ_ENV, reading, zone
from .reminders import ReminderStore

__all__ = [
    "REMINDER_SENSOR_ID",
    "SENSOR_ID",
    "TZ_ENV",
    "ClockPlugin",
    "ReminderStore",
    "reading",
    "zone",
]
