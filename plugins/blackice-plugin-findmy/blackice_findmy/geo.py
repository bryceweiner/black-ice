"""Distance, the home geofence, and turning "my wife" into a Find My record.

The naming problem is the awkward one. Find My gets friends' names from
Contacts, not from the cache file, so a person record often arrives with no
name at all -- just an account id and the phone number the invitation went to.
That is why aliases resolve against handles as well as names: `wife=Jane` works
when Apple gives us a name, and `wife=+15551234567` works when it does not.
"""

from __future__ import annotations

import math
import os

from .cache import Subject

ALIASES_ENV = "FINDMY_ALIASES"
HOME_LAT_ENV = "HOME_LAT"
HOME_LON_ENV = "HOME_LON"
HOME_RADIUS_ENV = "HOME_RADIUS_M"
DEFAULT_HOME_RADIUS_M = 150.0

EARTH_RADIUS_M = 6371008.8


def distance_m(a_lat: float, a_lon: float, b_lat: float, b_lon: float) -> float:
    """Great-circle distance in metres."""
    phi1, phi2 = math.radians(a_lat), math.radians(b_lat)
    d_phi = phi2 - phi1
    d_lambda = math.radians(b_lon - a_lon)
    h = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(min(1.0, h)))


def home() -> tuple[float, float] | None:
    lat = _env_float(HOME_LAT_ENV)
    lon = _env_float(HOME_LON_ENV)
    return (lat, lon) if lat is not None and lon is not None else None


def home_radius_m() -> float:
    return _env_float(HOME_RADIUS_ENV) or DEFAULT_HOME_RADIUS_M


def _env_float(name: str) -> float | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


# --- aliases ---------------------------------------------------------------

def aliases() -> dict[str, str]:
    """Parse `FINDMY_ALIASES="wife=Jane Doe,mom=Susan"` into a lookup."""
    out: dict[str, str] = {}
    for clause in os.environ.get(ALIASES_ENV, "").split(","):
        alias, sep, target = clause.partition("=")
        if sep and alias.strip() and target.strip():
            out[alias.strip().lower()] = target.strip()
    return out


_PHONE_PUNCTUATION = str.maketrans("", "", " ()-./+")


def _norm(text: str) -> str:
    """Compare names loosely, and phone numbers by digits alone."""
    lowered = text.strip().lower()
    # A handle is a phone number if nothing but punctuation separates its
    # digits. Compare the last ten so +1 (555) 123-4567 and 5551234567 are the
    # same person, whichever way the alias was written.
    bare = lowered.translate(_PHONE_PUNCTUATION)
    if bare.isdigit() and len(bare) >= 7:
        return bare[-10:]
    return " ".join(lowered.split())


def resolve(
    query: str, subjects: list[Subject], alias_map: dict[str, str] | None = None
) -> Subject | None:
    """Find the subject a spoken word refers to, or None if it is ambiguous.

    Exact matches beat prefix matches beat substring matches, and a tie at the
    best available tier returns None rather than guessing between two people.
    """
    if not query or not query.strip():
        return None
    alias_map = aliases() if alias_map is None else alias_map

    wanted = _norm(alias_map.get(query.strip().lower(), query))
    if not wanted:
        return None

    exact: list[Subject] = []
    prefix: list[Subject] = []
    partial: list[Subject] = []
    for subject in subjects:
        for candidate in [subject.name, *subject.handles]:
            token = _norm(str(candidate))
            if not token:
                continue
            if token == wanted:
                exact.append(subject)
                break
            if token.startswith(wanted) or wanted.startswith(token):
                prefix.append(subject)
                break
            if wanted in token:
                partial.append(subject)
                break

    for tier in (exact, prefix, partial):
        unique = list({s.key: s for s in tier}.values())
        if len(unique) == 1:
            return unique[0]
        if len(unique) > 1:
            return None  # ambiguous at this tier: say so rather than pick
    return None


# --- phrasing --------------------------------------------------------------

def spoken_distance(metres: float) -> str:
    """Distance as a person would say it, not as a sensor would log it."""
    if metres < 100:
        return "right here"
    if metres < 1000:
        return f"{round(metres / 10) * 10:.0f} metres away"
    if metres < 10_000:
        return f"{metres / 1000:.1f} km away"
    return f"{metres / 1000:.0f} km away"


def spoken_age(seconds: float | None) -> str:
    if seconds is None:
        return "at an unknown time"
    if seconds < 120:
        return "just now"
    if seconds < 3600:
        return f"{seconds / 60:.0f} minutes ago"
    if seconds < 86400:
        hours = seconds / 3600
        return f"{hours:.0f} hour{'s' if hours >= 2 else ''} ago"
    days = seconds / 86400
    return f"{days:.0f} day{'s' if days >= 2 else ''} ago"
