"""The Find My plugin: people, devices and items, and how far each is from us."""

from __future__ import annotations

import asyncio
import contextlib
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from blackice.models import (
    SEVERITY_INFO,
    SEVERITY_LOW,
    SEVERITY_MEDIUM,
    AlarmRuleSpec,
    Event,
    SensorDescriptor,
    ToolSpec,
    WidgetSpec,
)
from blackice.plugins.base import PluginContext, SensorPlugin

from . import cache, geo
from .cache import CacheUnavailable, Subject
from .position import Fix, Locator

PEOPLE_SENSOR_ID = "findmy.people"
DEVICES_SENSOR_ID = "findmy.devices"
ITEMS_SENSOR_ID = "findmy.items"

SENSOR_FOR_KIND = {
    "person": PEOPLE_SENSOR_ID,
    "device": DEVICES_SENSOR_ID,
    "item": ITEMS_SENSOR_ID,
}

POLL_ENV = "FINDMY_POLL_SECONDS"
STALE_ENV = "FINDMY_STALE_MINUTES"
SEPARATION_ENV = "FINDMY_SEPARATION_M"
HISTORY_DAYS_ENV = "FINDMY_HISTORY_DAYS"

DEFAULT_POLL_S = 60.0
DEFAULT_STALE_MINUTES = 30.0
DEFAULT_SEPARATION_M = 300.0
DEFAULT_HISTORY_DAYS = 14

SCHEMA = """
CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL DEFAULT (datetime('now')),
    subject_key TEXT NOT NULL,
    kind TEXT NOT NULL,
    name TEXT,
    lat REAL,
    lon REAL,
    accuracy_m REAL,
    fix_ts TEXT,
    distance_home_m REAL,
    distance_me_m REAL,
    battery REAL
);
CREATE INDEX IF NOT EXISTS positions_by_subject ON positions (subject_key, id DESC);

-- One row per subject, holding only what we need to notice a change. Without
-- this every restart would re-announce everyone's current state as if it were
-- news.
CREATE TABLE IF NOT EXISTS subject_state (
    subject_key TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    name TEXT,
    at_home INTEGER,
    battery_low INTEGER NOT NULL DEFAULT 0,
    stale INTEGER NOT NULL DEFAULT 0,
    separated INTEGER NOT NULL DEFAULT 0,
    updated TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def _env_float(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, "") or default)
    except ValueError:
        return default
    return value if value > 0 else default


@dataclass
class Reading:
    """A subject plus everything we computed about it this poll."""

    subject: Subject
    distance_home_m: float | None
    distance_me_m: float | None
    at_home: bool | None
    stale: bool
    separated: bool

    @property
    def name(self) -> str:
        return self.subject.name


class FindMyPlugin(SensorPlugin):
    name = "findmy"
    version = "0.1.0"

    def __init__(self) -> None:
        self.ctx: PluginContext | None = None
        self.task: asyncio.Task | None = None
        self.locator = Locator()
        self.readings: list[Reading] = []
        self.me: Fix | None = None
        self.last_ok: datetime | None = None
        self.last_error: str | None = None

    # --- lifecycle ---------------------------------------------------------

    async def start(self, ctx: PluginContext) -> None:
        self.ctx = ctx
        await ctx.db.executescript(SCHEMA)
        await ctx.db.commit()

        # One read up front. A missing cache or a missing Full Disk Access grant
        # is a configuration problem the owner must fix, and a red badge saying
        # so beats three widgets that are quietly always empty.
        try:
            await asyncio.to_thread(cache.read_all)
        except CacheUnavailable as exc:
            if exc.reason in {"no_permission", "no_cache"}:
                raise
            ctx.log.warning("find my cache not readable yet: %s", exc)

        self.task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self.task:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
            self.task = None

    def describe(self) -> list[SensorDescriptor]:
        known = ", ".join(sorted(geo.aliases())) or "none configured"
        subject_arg = {
            "type": "object",
            "properties": {
                "subject": {
                    "type": "string",
                    "description": (
                        "Who or what to locate. Accepts a configured alias "
                        f"({known}), a name as it appears in Find My, or a phone "
                        "number or email the person shares location from."
                    ),
                }
            },
            "required": ["subject"],
        }

        return [
            SensorDescriptor(
                id=PEOPLE_SENSOR_ID,
                name="Find My — People",
                kind="location",
                widgets=[
                    WidgetSpec(
                        type="map", title="Where everyone is",
                        data_source="people_map", span=12,
                    ),
                    WidgetSpec(
                        type="kv", title="Home / away",
                        data_source="home_away", span=4,
                    ),
                    WidgetSpec(
                        type="table", title="People",
                        data_source="people_table", span=8,
                    ),
                    WidgetSpec(
                        type="timeseries", title="Distance from home (km)",
                        data_source="distance_history", span=12,
                    ),
                ],
                alarm_rules=[
                    AlarmRuleSpec(
                        key="home_arrival",
                        name="Arrivals and departures",
                        description="Someone tracked crosses the home geofence",
                        sensor_id=PEOPLE_SENSOR_ID,
                        default_armed=False,  # several a day per person
                    ),
                    AlarmRuleSpec(
                        key="location_stale",
                        name="Location went stale",
                        description=(
                            "Nothing heard from a tracked subject for more than "
                            f"{_env_float(STALE_ENV, DEFAULT_STALE_MINUTES):.0f} minutes"
                        ),
                        sensor_id=PEOPLE_SENSOR_ID,
                        default_armed=True,
                    ),
                ],
                tools=[
                    ToolSpec(
                        name="where_is",
                        description=(
                            "Locate a person, device or item tracked by Find My. "
                            "Returns the place, how far it is from this Mac and "
                            "from home, whether it is home, the battery level, "
                            "and how long ago the position was reported. Use this "
                            "for questions like 'where is my wife?' or 'are my "
                            "keys at home?'."
                        ),
                        parameters=subject_arg,
                    ),
                    ToolSpec(
                        name="who_is_home",
                        description=(
                            "List which tracked people are currently within the "
                            "home geofence and which are away. Use for 'is anyone "
                            "home?' or 'who is in the house?'."
                        ),
                        parameters={"type": "object", "properties": {}},
                    ),
                    ToolSpec(
                        name="list_subjects",
                        description=(
                            "List everything Find My is tracking. Use when the "
                            "owner asks what can be located, or when a name did "
                            "not resolve and you need to offer the real options."
                        ),
                        parameters={
                            "type": "object",
                            "properties": {
                                "kind": {
                                    "type": "string",
                                    "enum": ["person", "device", "item"],
                                    "description": "Optional filter by kind.",
                                }
                            },
                        },
                    ),
                ],
            ),
            SensorDescriptor(
                id=DEVICES_SENSOR_ID,
                name="Find My — Devices",
                kind="location",
                widgets=[
                    WidgetSpec(
                        type="table", title="Devices",
                        data_source="devices_table", span=8,
                    ),
                    WidgetSpec(
                        type="status", title="Find My cache",
                        data_source="cache_state", span=4,
                    ),
                ],
                alarm_rules=[
                    AlarmRuleSpec(
                        key="battery_low",
                        name="Device battery low",
                        description="A tracked device's battery is low — it will stop reporting",
                        sensor_id=DEVICES_SENSOR_ID,
                        default_armed=True,
                    )
                ],
            ),
            SensorDescriptor(
                id=ITEMS_SENSOR_ID,
                name="Find My — Items",
                kind="location",
                widgets=[
                    WidgetSpec(
                        type="table", title="Items",
                        data_source="items_table", span=12,
                    )
                ],
                alarm_rules=[
                    AlarmRuleSpec(
                        key="item_separated",
                        name="Item left behind",
                        description=(
                            "A tracked item is more than "
                            f"{_env_float(SEPARATION_ENV, DEFAULT_SEPARATION_M):.0f}m "
                            "from everyone in the household"
                        ),
                        sensor_id=ITEMS_SENSOR_ID,
                        default_armed=True,
                    )
                ],
            ),
        ]

    # --- polling -----------------------------------------------------------

    async def _loop(self) -> None:
        while True:
            try:
                await self.poll()
            except asyncio.CancelledError:
                raise
            except CacheUnavailable as exc:
                self.last_error = str(exc)
                self.ctx.log.warning("find my cache unavailable: %s", exc)
            except Exception:
                self.ctx.log.exception("find my poll failed")
            await asyncio.sleep(_env_float(POLL_ENV, DEFAULT_POLL_S))

    async def poll(self, now: datetime | None = None) -> list[Reading]:
        """Read the cache once: compute, store, and emit what changed."""
        assert self.ctx is not None
        now = now or datetime.now(UTC)

        subjects = await asyncio.to_thread(cache.read_all)
        self.me = await self.locator.fix(subjects)
        self.readings = self._measure(subjects, now)
        self.last_ok = now
        self.last_error = None

        await self._store(self.readings, now)
        await self._emit_changes(self.readings, now)
        return self.readings

    def _measure(self, subjects: list[Subject], now: datetime) -> list[Reading]:
        home = geo.home()
        radius = geo.home_radius_m()
        stale_after = _env_float(STALE_ENV, DEFAULT_STALE_MINUTES) * 60
        separation = _env_float(SEPARATION_ENV, DEFAULT_SEPARATION_M)

        # An item is "separated" when it is far from every person *and* every
        # device we can see -- a bag by the door is with the house, not lost.
        anchors = [
            s for s in subjects if s.kind in {"person", "device"} and s.located
        ]

        readings: list[Reading] = []
        for subject in subjects:
            distance_home = distance_me = None
            at_home = None
            if subject.located:
                if home:
                    distance_home = geo.distance_m(subject.lat, subject.lon, *home)
                    at_home = distance_home <= radius
                if self.me:
                    distance_me = geo.distance_m(
                        subject.lat, subject.lon, self.me.lat, self.me.lon
                    )

            age = subject.age_seconds(now)
            separated = False
            if subject.kind == "item" and subject.located:
                others = [
                    geo.distance_m(subject.lat, subject.lon, a.lat, a.lon)
                    for a in anchors
                    if a.key != subject.key
                ]
                separated = bool(others) and min(others) > separation

            readings.append(
                Reading(
                    subject=subject,
                    distance_home_m=distance_home,
                    distance_me_m=distance_me,
                    at_home=at_home,
                    stale=age is not None and age > stale_after,
                    separated=separated,
                )
            )
        return readings

    async def _store(self, readings: list[Reading], now: datetime) -> None:
        assert self.ctx is not None
        db = self.ctx.db
        await db.executemany(
            "INSERT INTO positions"
            " (ts, subject_key, kind, name, lat, lon, accuracy_m, fix_ts,"
            "  distance_home_m, distance_me_m, battery)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    now.isoformat(),
                    r.subject.key,
                    r.subject.kind,
                    r.subject.name,
                    r.subject.lat,
                    r.subject.lon,
                    r.subject.accuracy_m,
                    r.subject.fix_ts.isoformat() if r.subject.fix_ts else None,
                    r.distance_home_m,
                    r.distance_me_m,
                    r.subject.battery,
                )
                for r in readings
                if r.subject.located
            ],
        )
        days = int(_env_float(HISTORY_DAYS_ENV, DEFAULT_HISTORY_DAYS))
        await db.execute(
            f"DELETE FROM positions WHERE ts < datetime('now', '-{days} days')"
        )
        await db.commit()

    async def _emit_changes(self, readings: list[Reading], now: datetime) -> None:
        """Compare against last poll and emit only the transitions."""
        assert self.ctx is not None
        db = self.ctx.db
        cur = await db.execute("SELECT * FROM subject_state")
        previous = {row["subject_key"]: dict(row) for row in await cur.fetchall()}

        for reading in readings:
            subject = reading.subject
            was = previous.get(subject.key)

            # First sight is not news: record the state, announce nothing.
            if was is not None:
                await self._emit_for(reading, was, now)

            await db.execute(
                "INSERT INTO subject_state"
                " (subject_key, kind, name, at_home, battery_low, stale, separated, updated)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(subject_key) DO UPDATE SET"
                "  kind=excluded.kind, name=excluded.name, at_home=excluded.at_home,"
                "  battery_low=excluded.battery_low, stale=excluded.stale,"
                "  separated=excluded.separated, updated=excluded.updated",
                (
                    subject.key,
                    subject.kind,
                    subject.name,
                    None if reading.at_home is None else int(reading.at_home),
                    int(subject.battery_low),
                    int(reading.stale),
                    int(reading.separated),
                    now.isoformat(),
                ),
            )
        await db.commit()

    async def _emit_for(self, reading: Reading, was: dict[str, Any], now: datetime) -> None:
        assert self.ctx is not None
        subject = reading.subject
        sensor_id = SENSOR_FOR_KIND[subject.kind]
        # Names and place labels come from Apple and from other people's
        # devices, so they are sensor input, not our own words.
        where = f"Find My calls this {subject.kind} {subject.name!r}"
        if subject.place:
            where += f", last seen at {subject.place!r}"

        base = {
            "subject_key": subject.key,
            "kind": subject.kind,
            "name": subject.name,
            "place": subject.place,
            "distance_home_m": reading.distance_home_m,
            "distance_me_m": reading.distance_me_m,
        }

        if reading.at_home is not None and was["at_home"] is not None:
            if reading.at_home and not was["at_home"]:
                await self.ctx.emit(Event(
                    sensor_id=sensor_id, severity=SEVERITY_INFO, kind="arrival",
                    summary=f"A tracked {subject.kind} arrived home",
                    sensor_text=where, payload=base,
                ))
            elif not reading.at_home and was["at_home"]:
                await self.ctx.emit(Event(
                    sensor_id=sensor_id, severity=SEVERITY_LOW, kind="departure",
                    summary=f"A tracked {subject.kind} left home",
                    sensor_text=where, payload=base,
                ))

        if subject.battery_low and not was["battery_low"]:
            await self.ctx.emit(Event(
                sensor_id=sensor_id, severity=SEVERITY_LOW, kind="battery_low",
                summary=f"A tracked {subject.kind}'s battery is low; it may stop reporting",
                sensor_text=where,
                payload={**base, "battery": subject.battery},
            ))

        if reading.stale and not was["stale"]:
            age = subject.age_seconds(now)
            await self.ctx.emit(Event(
                sensor_id=sensor_id, severity=SEVERITY_LOW, kind="location_stale",
                summary=(
                    f"No position from a tracked {subject.kind} for "
                    f"{geo.spoken_age(age)}"
                ),
                sensor_text=where,
                payload={**base, "age_seconds": age},
            ))

        if reading.separated and not was["separated"]:
            await self.ctx.emit(Event(
                sensor_id=sensor_id, severity=SEVERITY_MEDIUM, kind="item_separated",
                summary="A tracked item is away from everyone in the household",
                sensor_text=where, payload=base,
            ))

    # --- tools -------------------------------------------------------------

    async def _current(self) -> list[Reading]:
        """Readings for a tool call, polling first if we have none yet."""
        if not self.readings:
            await self.poll()
        return self.readings

    def _describe(self, reading: Reading) -> dict[str, Any]:
        subject = reading.subject
        age = subject.age_seconds()
        out: dict[str, Any] = {
            "name": subject.name,
            "kind": subject.kind,
            "place": subject.place,
            "located": subject.located,
            "at_home": reading.at_home,
            "battery": subject.battery,
            "battery_low": subject.battery_low,
            "stale": reading.stale,
            "reported": geo.spoken_age(age),
            "age_seconds": age,
        }
        if subject.located:
            out |= {"lat": subject.lat, "lon": subject.lon, "accuracy_m": subject.accuracy_m}
        if reading.distance_home_m is not None:
            out["distance_home_m"] = round(reading.distance_home_m)
        if reading.distance_me_m is not None:
            out["distance_me_m"] = round(reading.distance_me_m)
            out["distance_from_me"] = geo.spoken_distance(reading.distance_me_m)
        return out

    def _spoken(self, reading: Reading) -> str:
        """One sentence an assistant can say out loud."""
        subject = reading.subject
        if not subject.located:
            return f"{subject.name} has no location — Find My has not heard from them."

        where = f"at {subject.place}" if subject.place else "somewhere unnamed"
        if reading.at_home:
            sentence = f"{subject.name} is home"
        elif reading.distance_me_m is not None:
            sentence = f"{subject.name} is {where}, {geo.spoken_distance(reading.distance_me_m)}"
        elif reading.distance_home_m is not None:
            sentence = (
                f"{subject.name} is {where}, "
                f"{geo.spoken_distance(reading.distance_home_m)} from home"
            )
        else:
            sentence = f"{subject.name} is {where}"

        sentence += f", reported {geo.spoken_age(subject.age_seconds())}"
        if reading.stale:
            sentence += " — that is old enough to be out of date"
        if self.me and self.me.source == "cached" and reading.distance_me_m is not None:
            sentence += " (measured from this Mac's last known position)"
        return sentence + "."

    async def handle_command(self, cmd: str, **kwargs: Any) -> Any:
        try:
            readings = await self._current()
        except CacheUnavailable as exc:
            # A permissions problem is the owner's to fix, and the assistant
            # should say so rather than the plugin going unhealthy mid-question.
            return {"error": str(exc), "spoken": _cache_advice(exc)}

        if cmd == "where_is":
            query = str(kwargs.get("subject") or "").strip()
            if not query:
                return {"error": "no subject given", "spoken": "Who would you like me to find?"}

            match = geo.resolve(query, [r.subject for r in readings])
            if match is None:
                names = sorted(r.subject.name for r in readings)
                return {
                    "error": f"no single match for {query!r}",
                    "known": names,
                    "spoken": (
                        f"I could not tell who {query} is. I can locate: "
                        + (", ".join(names) if names else "nothing yet")
                    ),
                }
            reading = next(r for r in readings if r.subject.key == match.key)
            return {**self._describe(reading), "spoken": self._spoken(reading)}

        if cmd == "who_is_home":
            people = [r for r in readings if r.subject.kind == "person"]
            if not geo.home():
                return {
                    "error": "no home coordinate configured",
                    "spoken": "I do not know where home is — set HOME_LAT and HOME_LON.",
                }
            home_now = [r.subject.name for r in people if r.at_home]
            away = [r.subject.name for r in people if r.at_home is False]
            return {
                "home": home_now,
                "away": away,
                "unknown": [r.subject.name for r in people if r.at_home is None],
                "spoken": (
                    (", ".join(home_now) + (" is" if len(home_now) == 1 else " are") + " home")
                    if home_now else "Nobody tracked is home"
                ) + (f". {', '.join(away)} away." if away else "."),
            }

        if cmd == "list_subjects":
            kind = kwargs.get("kind")
            if kind is not None and kind not in SENSOR_FOR_KIND:
                return {
                    "error": f"unknown kind {kind!r}; expected person, device or item",
                    "spoken": "I can list people, devices or items.",
                }
            wanted = [r for r in readings if kind is None or r.subject.kind == kind]
            return {
                "count": len(wanted),
                "subjects": [self._describe(r) for r in wanted],
                "aliases": geo.aliases(),
            }

        return await super().handle_command(cmd, **kwargs)

    # --- widgets -----------------------------------------------------------

    async def query(self, source: str, **kwargs: Any) -> Any:
        assert self.ctx is not None

        if source == "cache_state":
            if self.last_error:
                return {"state": "unhealthy"}
            if self.last_ok is None:
                return {"state": "unknown"}
            age = (datetime.now(UTC) - self.last_ok).total_seconds()
            stale_after = _env_float(POLL_ENV, DEFAULT_POLL_S) * 3
            return {"state": "healthy" if age <= stale_after else "degraded"}

        if source == "distance_history":
            # One series only, so chart the household's primary person: the
            # first configured alias, or whoever is named in ?subject=.
            wanted = kwargs.get("subject") or next(iter(geo.aliases().values()), None)
            rows = await self._history(wanted)
            return rows

        try:
            readings = await self._current()
        except CacheUnavailable as exc:
            return {"error": str(exc)} if source in {"people_map", "home_away"} else []

        if source == "people_map":
            points = [
                {"lat": r.subject.lat, "lon": r.subject.lon, "label": r.subject.name}
                for r in readings
                if r.subject.located and r.subject.kind == "person"
            ]
            home = geo.home()
            if home:
                points.append({"lat": home[0], "lon": home[1], "label": "Home"})
            if self.me:
                points.append({"lat": self.me.lat, "lon": self.me.lon, "label": "Me"})
            if not points:
                return {}
            return {"points": points, **points[0]}  # lat/lon keep old renderers working

        if source == "home_away":
            out: dict[str, str] = {}
            for r in readings:
                if r.subject.kind != "person":
                    continue
                out[r.subject.name] = (
                    "unknown" if r.at_home is None else ("home" if r.at_home else "away")
                )
            return out

        if source in {"people_table", "devices_table", "items_table"}:
            kind = {"people_table": "person", "devices_table": "device",
                    "items_table": "item"}[source]
            return [self._row(r) for r in readings if r.subject.kind == kind]

        return await super().query(source, **kwargs)

    def _row(self, reading: Reading) -> dict[str, Any]:
        subject = reading.subject
        battery = "—" if subject.battery is None else f"{subject.battery * 100:.0f}%"
        if subject.battery_low:
            battery += " (low)"
        return {
            "Name": subject.name,
            "Place": subject.place or ("unknown" if not subject.located else "—"),
            "Home": "—" if reading.at_home is None else ("yes" if reading.at_home else "no"),
            "From me": (
                "—" if reading.distance_me_m is None
                else geo.spoken_distance(reading.distance_me_m)
            ),
            "Battery": battery,
            "Reported": geo.spoken_age(subject.age_seconds()),
        }

    async def _history(self, subject_query: str | None) -> list[dict[str, Any]]:
        """Distance from home over time, newest first, as the renderer wants."""
        assert self.ctx is not None
        if not subject_query:
            return []
        key = None
        if self.readings:
            match = geo.resolve(subject_query, [r.subject for r in self.readings])
            key = match.key if match else None
        if key is None:
            row = await (await self.ctx.db.execute(
                "SELECT subject_key FROM positions WHERE name = ? ORDER BY id DESC LIMIT 1",
                (subject_query,),
            )).fetchone()
            key = row["subject_key"] if row else None
        if key is None:
            return []

        cur = await self.ctx.db.execute(
            "SELECT strftime('%H:%M', ts) AS bucket,"
            "       round(avg(distance_home_m) / 1000.0, 2) AS km"
            " FROM positions"
            " WHERE subject_key = ? AND distance_home_m IS NOT NULL"
            "   AND ts > datetime('now', '-1 day')"
            " GROUP BY strftime('%Y-%m-%d %H:%M', ts)"
            " ORDER BY max(id) DESC LIMIT 96",
            (key,),
        )
        return [dict(r) for r in await cur.fetchall()]


def _cache_advice(exc: CacheUnavailable) -> str:
    if exc.reason == "no_permission":
        return (
            "I cannot read Find My. Grant Full Disk Access to the app running "
            "Black Ice in System Settings, Privacy & Security."
        )
    if exc.reason == "no_cache":
        return "Find My has no local cache on this Mac. Is it signed in and running?"
    return "Find My's cache was unreadable just now; I will try again shortly."
