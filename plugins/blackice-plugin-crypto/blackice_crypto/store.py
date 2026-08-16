"""The plugin's own SQLite: what is watched, what it held, and when.

Quantities are stored as text and read back as `Decimal`. A float would lose
precision on an 18-decimal token balance, and the whole point of this plugin is
noticing when a balance changed.

`now` is a parameter on everything time-dependent so the tests can drive a
window forwards without waiting for one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import aiosqlite

from .adapters import Holding

SCHEMA = """
CREATE TABLE IF NOT EXISTS watches (
    id            INTEGER PRIMARY KEY,
    network       TEXT NOT NULL,
    address       TEXT NOT NULL,
    label         TEXT NOT NULL DEFAULT '',
    active        INTEGER NOT NULL DEFAULT 1,
    drain_percent REAL,
    drain_hours   REAL,
    value_percent REAL,
    value_hours   REAL,
    added_at      TEXT NOT NULL,
    polled_at     TEXT,
    last_error    TEXT,
    UNIQUE (network, address)
);

CREATE TABLE IF NOT EXISTS snapshots (
    id        INTEGER PRIMARY KEY,
    watch_id  INTEGER NOT NULL REFERENCES watches(id) ON DELETE CASCADE,
    ts        TEXT NOT NULL,
    usd_value REAL,
    priced    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS snapshots_watch_ts ON snapshots (watch_id, ts);

CREATE TABLE IF NOT EXISTS holdings (
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
    asset       TEXT NOT NULL,
    symbol      TEXT NOT NULL DEFAULT '',
    name        TEXT NOT NULL DEFAULT '',
    contract    TEXT,
    quantity    TEXT NOT NULL,
    price_key   TEXT,
    usd_price   REAL,
    usd_value   REAL,
    PRIMARY KEY (snapshot_id, asset)
);

-- A plugin cannot read core's event table, so the movement log the dashboard
-- shows is kept here as well as emitted.
CREATE TABLE IF NOT EXISTS movements (
    id       INTEGER PRIMARY KEY,
    watch_id INTEGER NOT NULL REFERENCES watches(id) ON DELETE CASCADE,
    ts       TEXT NOT NULL,
    asset    TEXT NOT NULL,
    symbol   TEXT NOT NULL DEFAULT '',
    delta    TEXT NOT NULL,
    usd      REAL
);
CREATE INDEX IF NOT EXISTS movements_ts ON movements (ts);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

DEFAULTS = {
    "drain_percent": 20.0,   # X: quantity drop that trips the security alert
    "drain_hours": 24.0,     # Y: the window it is measured over
    "value_percent": 30.0,   # the USD-only rule, deliberately looser
    "value_hours": 24.0,
    "min_move_usd": 1.0,     # below this, a movement is dust and stays quiet
    # A plugin cannot read core's arm state, so the noisier rule gates itself.
    "value_drop_enabled": 0.0,
}

ALERT = "alerted:"  # settings-table prefix for per-watch alert bookkeeping


def to_sql(when: datetime) -> str:
    return when.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")


def from_sql(raw: str) -> datetime:
    return datetime.strptime(raw, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)


@dataclass(frozen=True)
class Move:
    """One asset's quantity change between two consecutive snapshots."""

    asset: str
    symbol: str          # untrusted: chain-supplied
    delta: Decimal
    quantity: Decimal    # quantity now
    usd: float | None    # signed USD value of the change, if priceable

    @property
    def deposit(self) -> bool:
        return self.delta > 0


@dataclass(frozen=True)
class Breach:
    """A threshold that has been crossed."""

    rule: str            # "drain" | "value_drop"
    percent: float       # how far it actually fell
    threshold: float     # the configured X
    hours: float         # the configured Y
    before: float        # baseline magnitude (USD, or native quantity)
    after: float
    basis: str           # "usd" | "native-quantity"


class Store:
    def __init__(self, db: aiosqlite.Connection) -> None:
        self.db = db

    async def setup(self) -> None:
        await self.db.executescript(SCHEMA)
        await self.db.commit()

    # --- settings ----------------------------------------------------------

    async def settings(self) -> dict[str, float]:
        rows = await (await self.db.execute(
            "SELECT key, value FROM settings WHERE key NOT LIKE ?", (f"{ALERT}%",)
        )).fetchall()
        out = dict(DEFAULTS)
        for row in rows:
            try:
                out[row["key"]] = float(row["value"])
            except (TypeError, ValueError):
                continue
        return out

    # A fired rule stays quiet for the length of its own window; otherwise a
    # single drain re-alerts on every poll until the window slides past it.
    async def alerted_at(self, watch_id: int, rule: str) -> float | None:
        raw = await (await self.db.execute(
            "SELECT value FROM settings WHERE key = ?", (f"{ALERT}{watch_id}:{rule}",)
        )).fetchone()
        return float(raw["value"]) if raw else None

    async def mark_alerted(self, watch_id: int, rule: str, when: datetime) -> None:
        await self.set_setting(f"{ALERT}{watch_id}:{rule}", when.timestamp())

    async def set_setting(self, key: str, value: float) -> None:
        await self.db.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(float(value))),
        )
        await self.db.commit()

    async def thresholds_for(self, watch: aiosqlite.Row) -> dict[str, float]:
        """Global defaults, with this watch's overrides on top."""
        values = await self.settings()
        for field in ("drain_percent", "drain_hours", "value_percent", "value_hours"):
            if watch[field] is not None:
                values[field] = float(watch[field])
        return values

    # --- watches -----------------------------------------------------------

    async def add(self, network: str, address: str, label: str, now: datetime) -> int:
        """Add, or revive a soft-deleted watch so its history carries on."""
        await self.db.execute(
            "INSERT INTO watches (network, address, label, added_at) VALUES (?,?,?,?)"
            " ON CONFLICT(network, address) DO UPDATE SET"
            "   active = 1, label = CASE WHEN excluded.label != '' THEN excluded.label"
            "                            ELSE watches.label END",
            (network, address, label, to_sql(now)),
        )
        await self.db.commit()
        row = await self.find(network, address)
        return int(row["id"])

    async def deactivate(self, watch_id: int) -> bool:
        cur = await self.db.execute(
            "UPDATE watches SET active = 0 WHERE id = ? AND active = 1", (watch_id,)
        )
        await self.db.commit()
        return cur.rowcount > 0

    async def find(self, network: str, address: str) -> aiosqlite.Row | None:
        return await (await self.db.execute(
            "SELECT * FROM watches WHERE network = ? AND address = ?",
            (network, address),
        )).fetchone()

    async def get(self, watch_id: int) -> aiosqlite.Row | None:
        return await (await self.db.execute(
            "SELECT * FROM watches WHERE id = ?", (watch_id,)
        )).fetchone()

    async def watches(self, active_only: bool = True) -> list[aiosqlite.Row]:
        sql = "SELECT * FROM watches"
        if active_only:
            sql += " WHERE active = 1"
        return list(await (await self.db.execute(sql + " ORDER BY network, address"))
                    .fetchall())

    async def mark_polled(self, watch_id: int, now: datetime,
                          error: str | None) -> None:
        await self.db.execute(
            "UPDATE watches SET polled_at = ?, last_error = ? WHERE id = ?",
            (to_sql(now), error, watch_id),
        )
        await self.db.commit()

    # --- snapshots ---------------------------------------------------------

    async def record(self, watch_id: int, holdings: list[Holding],
                     price_keys: dict[str, str | None], prices: dict[str, float],
                     now: datetime) -> int:
        """Store one reading, valuing whatever can be valued."""
        total, priced = 0.0, 0
        rows = []
        for holding in holdings:
            key = price_keys.get(holding.asset)
            price = prices.get(key) if key else None
            value = float(holding.quantity) * price if price is not None else None
            if value is not None:
                total += value
                priced += 1
            rows.append((holding.asset, holding.symbol, holding.name, holding.contract,
                         str(holding.quantity), key, price, value))

        cur = await self.db.execute(
            "INSERT INTO snapshots (watch_id, ts, usd_value, priced) VALUES (?,?,?,?)",
            (watch_id, to_sql(now), total if priced else None, priced),
        )
        snapshot_id = int(cur.lastrowid)
        await self.db.executemany(
            "INSERT INTO holdings (snapshot_id, asset, symbol, name, contract,"
            " quantity, price_key, usd_price, usd_value) VALUES (?,?,?,?,?,?,?,?,?)",
            [(snapshot_id, *row) for row in rows],
        )
        await self.db.commit()
        return snapshot_id

    async def snapshots(self, watch_id: int, limit: int = 2) -> list[aiosqlite.Row]:
        return list(await (await self.db.execute(
            "SELECT * FROM snapshots WHERE watch_id = ? ORDER BY ts DESC, id DESC"
            " LIMIT ?", (watch_id, limit),
        )).fetchall())

    async def baseline(self, watch_id: int, hours: float,
                       now: datetime) -> aiosqlite.Row | None:
        """The most recent snapshot at least `hours` old -- the rolling window's
        far edge. None when the watch is younger than the window."""
        cutoff = to_sql(now - timedelta(hours=hours))
        return await (await self.db.execute(
            "SELECT * FROM snapshots WHERE watch_id = ? AND ts <= ?"
            " ORDER BY ts DESC, id DESC LIMIT 1", (watch_id, cutoff),
        )).fetchone()

    async def holdings(self, snapshot_id: int) -> list[aiosqlite.Row]:
        return list(await (await self.db.execute(
            "SELECT * FROM holdings WHERE snapshot_id = ?", (snapshot_id,)
        )).fetchall())

    async def log_moves(self, watch_id: int, moves: list[Move], now: datetime) -> None:
        await self.db.executemany(
            "INSERT INTO movements (watch_id, ts, asset, symbol, delta, usd)"
            " VALUES (?,?,?,?,?,?)",
            [(watch_id, to_sql(now), m.asset, m.symbol, str(m.delta), m.usd)
             for m in moves],
        )
        await self.db.commit()

    async def recent_moves(self, limit: int = 50) -> list[aiosqlite.Row]:
        return list(await (await self.db.execute(
            "SELECT m.ts, m.asset, m.symbol, m.delta, m.usd, w.network, w.address,"
            "       w.label"
            "  FROM movements m JOIN watches w ON w.id = m.watch_id"
            " ORDER BY m.id DESC LIMIT ?", (limit,),
        )).fetchall())

    async def prune(self, keep_days: int, now: datetime) -> int:
        """Drop snapshots older than the longest window anyone could ask for."""
        cutoff = to_sql(now - timedelta(days=keep_days))
        cur = await self.db.execute("DELETE FROM snapshots WHERE ts < ?", (cutoff,))
        await self.db.commit()
        return cur.rowcount


# --- comparing two readings -------------------------------------------------

def movements(before: list[aiosqlite.Row], after: list[aiosqlite.Row],
              prices: dict[str, float], min_usd: float) -> list[Move]:
    """Per-asset quantity changes worth telling anyone about.

    Dust is filtered by USD where a price exists and by proportion where one
    does not, so a spam airdrop on an unpriced chain stays as quiet as one on a
    priced chain.
    """
    old = {row["asset"]: row for row in before}
    new = {row["asset"]: row for row in after}
    out: list[Move] = []

    for asset in old.keys() | new.keys():
        was = Decimal(old[asset]["quantity"]) if asset in old else Decimal(0)
        is_ = Decimal(new[asset]["quantity"]) if asset in new else Decimal(0)
        delta = is_ - was
        if delta == 0:
            continue

        row = new.get(asset) or old[asset]
        price = prices.get(row["price_key"]) if row["price_key"] else None
        usd = float(delta) * price if price is not None else None

        if usd is not None:
            if abs(usd) < min_usd:
                continue
        elif was and abs(delta / was) < Decimal("0.01"):
            continue  # unpriced and a change of under 1%: noise

        out.append(Move(asset, row["symbol"] or "", delta, is_, usd))

    out.sort(key=lambda m: abs(m.usd if m.usd is not None else 0), reverse=True)
    return out


def _repriced(rows: list[aiosqlite.Row], prices: dict[str, float]) -> tuple[float, int]:
    """Value a set of holdings at one consistent set of prices."""
    total, counted = 0.0, 0
    for row in rows:
        price = prices.get(row["price_key"]) if row["price_key"] else None
        if price is None:
            continue
        total += float(Decimal(row["quantity"])) * price
        counted += 1
    return total, counted


def _native(rows: list[aiosqlite.Row]) -> float:
    row = next((r for r in rows if r["asset"] == "native"), None)
    return float(Decimal(row["quantity"])) if row else 0.0


def drain(base_rows: list[aiosqlite.Row], now_rows: list[aiosqlite.Row],
          prices: dict[str, float], percent: float, hours: float) -> Breach | None:
    """The security rule: did *quantities* fall by X% over the window?

    Both sides are valued at the same current prices, so a market crash cancels
    out and only coins actually leaving the address register. Where nothing can
    be priced at all, the native quantity stands in -- it is the one holding
    every chain reports.
    """
    before, counted = _repriced(base_rows, prices)
    after, _ = _repriced(now_rows, prices)
    basis = "usd"

    if counted == 0 or before <= 0:
        before, after, basis = _native(base_rows), _native(now_rows), "native-quantity"
    if before <= 0:
        return None

    dropped = (before - after) / before * 100.0
    if dropped < percent:
        return None
    return Breach("drain", dropped, percent, hours, before, after, basis)


def value_drop(base: aiosqlite.Row, now: aiosqlite.Row, percent: float,
               hours: float) -> Breach | None:
    """The portfolio rule: did the USD total fall X%, whatever the cause?

    Each side keeps its own prices, so this fires on a market move as readily as
    on a withdrawal. That is the point of it being a separate, quieter rule.
    """
    before = base["usd_value"]
    after = now["usd_value"]
    if before is None or after is None or before <= 0:
        return None
    dropped = (before - after) / before * 100.0
    if dropped < percent:
        return None
    return Breach("value_drop", dropped, percent, hours, before, after, "usd")


def summarise(moves: list[Move], limit: int = 3) -> str:
    """A short, plugin-authored description of a set of moves.

    Deliberately built from quantities and asset keys only -- never from the
    chain's symbol strings, which are untrusted and go to `sensor_text`.
    """
    parts = []
    for move in moves[:limit]:
        if move.usd is not None:
            parts.append(f"${abs(move.usd):,.2f}")
        else:
            parts.append(f"{abs(move.delta):.6f} of {short_asset(move.asset)}")
    if len(moves) > limit:
        parts.append(f"and {len(moves) - limit} more")
    return ", ".join(parts)


def short_asset(asset: str) -> str:
    if asset == "native":
        return "the native coin"
    return f"{asset[:6]}…{asset[-4:]}" if len(asset) > 14 else asset


def as_dict(value: Any) -> dict:
    """aiosqlite.Row -> plain dict, for payloads and widgets."""
    return dict(value) if value is not None else {}
