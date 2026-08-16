"""Find My plugin. Locates the people, devices and AirTags this Mac's iCloud
account can see, and measures each against home and against where we are.

Everything is read from Apple's own on-disk cache -- no credentials, no calls
to iCloud, nothing off the LAN. The cost of that choice is that the readings
are only as fresh as the last time Find My itself refreshed them, which is why
staleness is a first-class thing here rather than an afterthought: a monitoring
system must never let "she is home" and "we stopped hearing from her" look the
same.
"""

from __future__ import annotations

from .cache import CacheUnavailable, Subject, read_all
from .geo import distance_m, resolve
from .plugin import (
    DEVICES_SENSOR_ID,
    ITEMS_SENSOR_ID,
    PEOPLE_SENSOR_ID,
    FindMyPlugin,
)
from .position import Locator

__all__ = [
    "DEVICES_SENSOR_ID",
    "ITEMS_SENSOR_ID",
    "PEOPLE_SENSOR_ID",
    "CacheUnavailable",
    "FindMyPlugin",
    "Locator",
    "Subject",
    "distance_m",
    "read_all",
    "resolve",
]
