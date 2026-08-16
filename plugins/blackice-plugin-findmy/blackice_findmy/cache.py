"""Reading Apple's Find My cache off the local disk.

Find My keeps its last known state as plain JSON under
`~/Library/Caches/com.apple.findmy.fmipcore/`: `Devices.data` for the
household's Apple hardware, `Items.data` for AirTags and network accessories,
and `Friends.data` for people sharing their location with this account.

Two things shape everything in this module.

The first is that the format is undocumented. Key spellings differ between the
three files, between macOS releases, and sometimes between record types in one
file, so nothing here indexes a key it has not tried several spellings of. A
reading we cannot parse becomes a subject with no position -- never an
exception, and never a silently wrong coordinate.

The second is that the path is TCC-protected. Without Full Disk Access the open
fails with EPERM, which is a configuration problem the owner has to fix and not
a plugin fault, so `read_all` reports it as a state rather than raising.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

CACHE_DIR_ENV = "FINDMY_CACHE_DIR"
DEFAULT_CACHE_DIR = "~/Library/Caches/com.apple.findmy.fmipcore"

DEVICES_FILE = "Devices.data"
ITEMS_FILE = "Items.data"
FRIENDS_FILE = "Friends.data"

# Every spelling of a field we have seen or been told to expect. Order matters
# only in that the first key actually present wins.
_LAT_KEYS = ("latitude", "lat")
_LON_KEYS = ("longitude", "longitude_", "lon", "lng")
_TS_KEYS = ("timeStamp", "timestamp", "locationTimestamp", "lastUpdateTimestamp")
_ACCURACY_KEYS = ("horizontalAccuracy", "accuracy")
_LOCATION_KEYS = ("location", "lastKnownLocation", "safeLocation")
_NAME_KEYS = ("name", "deviceDisplayName", "displayName", "rawDeviceModel")

# Apple reports item batteries as a small enum and device batteries as a float.
_ITEM_BATTERY_STATES = {0: "normal", 1: "low", 2: "very_low", 3: "critical"}
_LOW_BATTERY_WORDS = {"low", "verylow", "very_low", "critical"}


class CacheUnavailable(Exception):
    """The cache could not be read. Carries why, for the owner's benefit."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(reason if not detail else f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


@dataclass
class Subject:
    """One person, device or item, normalised out of whichever file held it."""

    key: str
    kind: str  # "person" | "device" | "item"
    name: str  # Apple-supplied -- untrusted, belongs in Event.sensor_text
    lat: float | None = None
    lon: float | None = None
    accuracy_m: float | None = None
    fix_ts: datetime | None = None
    place: str | None = None  # Apple's address label -- also untrusted
    battery: float | None = None  # 0.0-1.0 where known
    battery_low: bool = False
    handles: list[str] = field(default_factory=list)  # phone/email, for aliases
    is_this_device: bool = False

    @property
    def located(self) -> bool:
        return self.lat is not None and self.lon is not None

    def age_seconds(self, now: datetime | None = None) -> float | None:
        if self.fix_ts is None:
            return None
        now = now or datetime.now(UTC)
        return max(0.0, (now - self.fix_ts).total_seconds())


def cache_dir() -> Path:
    return Path(os.environ.get(CACHE_DIR_ENV) or DEFAULT_CACHE_DIR).expanduser()


# --- primitive coercion ----------------------------------------------------
# Everything below takes "whatever Apple put there" and returns either a clean
# value or None. None is always an acceptable answer.

def _first(d: Any, keys: tuple[str, ...]) -> Any:
    if not isinstance(d, dict):
        return None
    for key in keys:
        value = d.get(key)
        if value is not None:
            return value
    return None


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out and abs(out) != float("inf") else None  # drop NaN/inf


def _as_timestamp(value: Any) -> datetime | None:
    """Apple stamps in milliseconds; tolerate seconds and ISO-8601 anyway."""
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)

    number = _as_float(value)
    if number is None or number <= 0:
        return None
    # A seconds-epoch that far out would be the year 5138; anything larger is ms.
    if number > 1e11:
        number /= 1000.0
    try:
        return datetime.fromtimestamp(number, UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _as_place(record: dict[str, Any]) -> str | None:
    """A short human place name, from whichever address shape is present."""
    address = record.get("address")
    if not isinstance(address, dict):
        return None
    for key in ("label", "streetAddress", "locality", "administrativeArea", "country"):
        value = address.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    lines = address.get("formattedAddressLines")
    if isinstance(lines, list):
        joined = ", ".join(str(x).strip() for x in lines if str(x).strip())
        if joined:
            return joined
    return None


def _as_location(record: dict[str, Any]) -> dict[str, Any]:
    """Pull the freshest usable location dict out of a record.

    A record can carry several -- `location` alongside a `safeLocation` -- and
    they are not always both populated, so prefer the one that actually has
    coordinates and, failing a tie, the newer stamp.
    """
    candidates = []
    for key in _LOCATION_KEYS:
        value = record.get(key)
        if isinstance(value, dict) and _as_float(_first(value, _LAT_KEYS)) is not None:
            candidates.append(value)
    # Some device records inline the coordinates rather than nesting them.
    if _as_float(_first(record, _LAT_KEYS)) is not None:
        candidates.append(record)
    if not candidates:
        return {}
    return max(
        candidates,
        key=lambda loc: (_as_timestamp(_first(loc, _TS_KEYS)) or datetime.min.replace(tzinfo=UTC)),
    )


def _battery(record: dict[str, Any]) -> tuple[float | None, bool]:
    """Return (level 0-1 or None, is_low). The two files disagree on format."""
    level = _as_float(record.get("batteryLevel"))
    if level is not None and level > 1.0:  # occasionally a percentage
        level = level / 100.0

    status = record.get("batteryStatus")
    low = False
    if isinstance(status, str):
        low = status.strip().lower().replace(" ", "") in _LOW_BATTERY_WORDS
    elif isinstance(status, int) and not isinstance(status, bool):
        low = _ITEM_BATTERY_STATES.get(status, "normal") != "normal"
    if level is not None and level <= 0.20:
        low = True
    return level, low


def _handles(record: dict[str, Any]) -> list[str]:
    """Phone numbers and emails a friend record is keyed by."""
    out: list[str] = []
    for key in ("invitationAcceptedHandles", "invitationFromHandles", "handles", "emails"):
        value = record.get(key)
        if isinstance(value, str):
            out.append(value)
        elif isinstance(value, list):
            out.extend(str(x) for x in value if isinstance(x, str | int))
    for key in ("appleId", "email", "phone", "phoneNumber"):
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            out.append(value.strip())
    seen: dict[str, None] = {}
    for handle in out:
        cleaned = handle.strip()
        if cleaned:
            seen.setdefault(cleaned, None)
    return list(seen)


def _name_of(record: dict[str, Any], fallback: str) -> str:
    for key in _NAME_KEYS:
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    # Friends have their names in Contacts, not here; a first/last pair is the
    # best we ever get, and often there is nothing at all.
    parts = [record.get("firstName"), record.get("lastName")]
    joined = " ".join(str(p).strip() for p in parts if isinstance(p, str) and p.strip())
    return joined or fallback


def _identity(record: dict[str, Any], kind: str, index: int) -> str:
    for key in ("id", "identifier", "baUUID", "deviceDiscoveryId", "serialNumber"):
        value = record.get(key)
        if isinstance(value, str | int) and str(value).strip():
            return f"{kind}:{str(value).strip()}"
    return f"{kind}:#{index}"


def _is_this_device(record: dict[str, Any]) -> bool:
    return any(record.get(key) is True for key in ("isThisDevice", "thisDevice", "isMac"))


# --- records ---------------------------------------------------------------

def _records(blob: Any) -> list[dict[str, Any]]:
    """Find the list of records in a file whose top level shape varies.

    `Devices.data` and `Items.data` are bare arrays; `Friends.data` is an object
    that keeps the people under one of several keys.
    """
    if isinstance(blob, list):
        return [r for r in blob if isinstance(r, dict)]
    if isinstance(blob, dict):
        for key in ("following", "items", "friends", "locations", "content"):
            value = blob.get(key)
            if isinstance(value, list):
                return [r for r in value if isinstance(r, dict)]
        # A dict of records keyed by id.
        values = [v for v in blob.values() if isinstance(v, dict)]
        if values and all(_as_location(v) or _first(v, _NAME_KEYS) for v in values):
            return values
    return []


def parse(blob: Any, kind: str) -> list[Subject]:
    """Normalise one decoded cache file into subjects."""
    subjects: list[Subject] = []
    for index, record in enumerate(_records(blob)):
        location = _as_location(record)
        level, low = _battery(record)
        key = _identity(record, kind, index)
        subjects.append(
            Subject(
                key=key,
                kind=kind,
                name=_name_of(record, fallback=key.split(":", 1)[1]),
                lat=_as_float(_first(location, _LAT_KEYS)),
                lon=_as_float(_first(location, _LON_KEYS)),
                accuracy_m=_as_float(_first(location, _ACCURACY_KEYS)),
                fix_ts=_as_timestamp(_first(location, _TS_KEYS)),
                place=_as_place(location) or _as_place(record),
                battery=level,
                battery_low=low,
                handles=_handles(record),
                is_this_device=_is_this_device(record),
            )
        )
    return subjects


def _read_file(path: Path, kind: str) -> list[Subject]:
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return []
    except PermissionError as exc:
        raise CacheUnavailable(
            "no_permission",
            f"{path} is protected. Grant Full Disk Access to the app running Black Ice",
        ) from exc
    except OSError as exc:
        raise CacheUnavailable("unreadable", f"{path}: {exc}") from exc

    if not raw.strip():
        return []
    try:
        return parse(json.loads(raw), kind)
    except json.JSONDecodeError as exc:
        # Find My rewrites these files in place, so a read can land mid-write.
        # That is transient: report it and let the next poll succeed.
        raise CacheUnavailable("corrupt", f"{path.name} is not valid JSON: {exc}") from exc


def read_all(directory: Path | None = None) -> list[Subject]:
    """Every subject across all three files.

    Raises `CacheUnavailable` when the directory itself cannot be used -- the
    owner needs to know about that. A single missing file is not fatal: not
    everyone owns AirTags.
    """
    directory = directory or cache_dir()
    if not directory.exists():
        raise CacheUnavailable(
            "no_cache",
            f"{directory} does not exist. Is Find My set up and signed in on this Mac?",
        )
    if not os.access(directory, os.R_OK | os.X_OK):
        raise CacheUnavailable(
            "no_permission",
            f"{directory} is protected. Grant Full Disk Access to the app running Black Ice",
        )

    subjects: list[Subject] = []
    for filename, kind in (
        (FRIENDS_FILE, "person"),
        (DEVICES_FILE, "device"),
        (ITEMS_FILE, "item"),
    ):
        subjects.extend(_read_file(directory / filename, kind))
    return subjects
