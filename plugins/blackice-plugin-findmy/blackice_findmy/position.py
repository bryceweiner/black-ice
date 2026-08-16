"""Where *this Mac* is, which is what "how far away is she?" measures from.

CoreLocation is the accurate answer and the awkward one. Location Services
authorisation is granted to an application bundle, and Black Ice runs as a bare
`python` process under a terminal, so the prompt frequently never appears and
`location()` simply stays nil forever. Worse, CLLocationManager only delivers
through a run loop, which an asyncio service does not have.

So this module treats CoreLocation as best-effort: it runs in a worker thread
with its own short-lived run loop, gives up after a few seconds, and falls back
to this Mac's own record in the Find My cache -- already being read for
everything else, and needing no permission CoreLocation did not already have.
The last good fix is kept either way, because a laptop that has gone to sleep
still has a more useful last known position than nothing at all.
"""

from __future__ import annotations

import asyncio
import os
import socket
import time
from dataclasses import dataclass
from datetime import UTC, datetime

from .cache import Subject

THIS_DEVICE_ENV = "FINDMY_THIS_DEVICE"
TIMEOUT_ENV = "FINDMY_LOCATION_TIMEOUT"
DEFAULT_TIMEOUT_S = 4.0


@dataclass
class Fix:
    lat: float
    lon: float
    accuracy_m: float | None
    source: str  # "corelocation" | "findmy" | "cached"
    ts: datetime


def _timeout() -> float:
    try:
        return max(0.5, float(os.environ.get(TIMEOUT_ENV, "") or DEFAULT_TIMEOUT_S))
    except ValueError:
        return DEFAULT_TIMEOUT_S


def _corelocation_blocking(timeout: float) -> Fix | None:
    """Ask CoreLocation, pumping a run loop until it answers or time runs out.

    Returns None for every failure -- not installed, not macOS, denied, or
    simply never answered. The caller has a fallback for all of them.
    """
    try:
        from CoreLocation import CLLocationManager  # type: ignore[import-not-found]
        from Foundation import NSDate, NSRunLoop  # type: ignore[import-not-found]
    except Exception:  # pragma: no cover - depends on host packages
        return None

    try:
        if not CLLocationManager.locationServicesEnabled():
            return None
        manager = CLLocationManager.alloc().init()
        # Authorisation is per-bundle; a bare interpreter usually lands on
        # "denied" or "not determined" and never moves off it.
        if hasattr(manager, "requestWhenInUseAuthorization"):
            manager.requestWhenInUseAuthorization()
        manager.startUpdatingLocation()

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            location = manager.location()
            if location is not None:
                coord = location.coordinate()
                accuracy = float(location.horizontalAccuracy())
                if accuracy >= 0:  # negative means the fix is invalid
                    manager.stopUpdatingLocation()
                    return Fix(
                        lat=float(coord.latitude),
                        lon=float(coord.longitude),
                        accuracy_m=accuracy,
                        source="corelocation",
                        ts=datetime.now(UTC),
                    )
            NSRunLoop.currentRunLoop().runUntilDate_(
                NSDate.dateWithTimeIntervalSinceNow_(0.2)
            )
        manager.stopUpdatingLocation()
    except Exception:  # pragma: no cover - pyobjc surfaces ObjC errors as these
        return None
    return None


def this_mac(subjects: list[Subject]) -> Subject | None:
    """This Mac's own record in the Find My cache.

    Apple flags it on some records and not others, so fall back to matching the
    computer's name -- and let the owner override outright when neither works.
    """
    devices = [s for s in subjects if s.kind == "device"]

    override = os.environ.get(THIS_DEVICE_ENV, "").strip().lower()
    if override:
        for device in devices:
            if device.name.strip().lower() == override:
                return device

    for device in devices:
        if device.is_this_device:
            return device

    hostname = socket.gethostname().removesuffix(".local").strip().lower()
    if hostname:
        for device in devices:
            if device.name.strip().lower() == hostname:
                return device
        # "Bryce's MacBook Pro" against a hostname of "bryces-macbook-pro"
        collapsed = hostname.replace("-", "")
        for device in devices:
            if "".join(c for c in device.name.lower() if c.isalnum()) == collapsed:
                return device
    return None


class Locator:
    """Answers "where am I?", remembering the last good answer."""

    def __init__(self) -> None:
        self.last: Fix | None = None

    async def fix(self, subjects: list[Subject]) -> Fix | None:
        """Best available position for this Mac, freshest source first."""
        found = await asyncio.to_thread(_corelocation_blocking, _timeout())

        if found is None:
            device = this_mac(subjects)
            if device is not None and device.located:
                found = Fix(
                    lat=device.lat,  # type: ignore[arg-type]
                    lon=device.lon,  # type: ignore[arg-type]
                    accuracy_m=device.accuracy_m,
                    source="findmy",
                    ts=device.fix_ts or datetime.now(UTC),
                )

        if found is not None:
            self.last = found
            return found

        # Nothing fresh. A stale fix still beats refusing to answer, but it is
        # labelled so callers can say so.
        if self.last is not None:
            return Fix(
                lat=self.last.lat,
                lon=self.last.lon,
                accuracy_m=self.last.accuracy_m,
                source="cached",
                ts=self.last.ts,
            )
        return None
