"""Watching cryptocurrency addresses for movement.

One sensor covers every chain. A watch is a `(network, address)` pair, so the
per-network configuration lives in the data rather than in a sensor each --
`describe()` is mirrored into core at start, so a sensor per chain would only
appear after a restart, which is the wrong shape for something the user adds
from a widget.

Two rules run over every reading:

* **drain** -- quantities fell X% against the balance Y hours ago, both sides
  valued at today's prices so a market move cancels out. This is the security
  rule, and it is the one that reaches HIGH.
* **value_drop** -- the USD total fell, whatever the cause. Off by default, and
  never above LOW, because a bad afternoon in the market fires it.

One endpoint among a hundred chains being unreachable is a fact about that
chain, not a fault in the plugin: it is recorded against the watch and shown in
the widget, and the plugin stays healthy. Only a genuine bug here should raise.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from blackice.models import (
    SEVERITY_HIGH,
    SEVERITY_INFO,
    SEVERITY_LOW,
    AlarmRuleSpec,
    Event,
    SensorDescriptor,
    ToolSpec,
    WidgetSpec,
)
from blackice.plugins.base import PluginContext, SensorPlugin

from . import adapters, chains, prices, store
from .adapters import BadAddress, ChainUnavailable, MissingCredential
from .store import Store

SENSOR_ID = "crypto.balances"
DEFAULT_POLL_SECONDS = 300.0
KEEP_DAYS = 30
# Several public explorers reject requests without one.
USER_AGENT = "black-ice-crypto/0.1"
# Thresholds a single address may override; the rest are global only.
OVERRIDABLE = ("drain_percent", "drain_hours", "value_percent", "value_hours")
# A command runs under the supervisor's 30s timeout, and reading one address can
# cost several requests to a slow explorer. Overrunning that would mark the whole
# plugin unhealthy, so an on-demand read gives up first and says so.
ON_DEMAND_BUDGET = 22.0


def _poll_seconds() -> float:
    try:
        return max(30.0, float(os.getenv("BLACKICE_CRYPTO_POLL_SECONDS", "")))
    except ValueError:
        return DEFAULT_POLL_SECONDS


def _short(address: str) -> str:
    return f"{address[:8]}…{address[-6:]}" if len(address) > 18 else address


def _who(watch: Any) -> str:
    """How a watch is named in plugin-authored text. The label is the owner's
    own words and the address is what they typed, so both are trusted."""
    label = (watch["label"] or "").strip()
    return f"{label} ({_short(watch['address'])})" if label else _short(watch["address"])


class CryptoPlugin(SensorPlugin):
    name = "crypto"
    version = "0.1.0"

    def __init__(self) -> None:
        self.ctx: PluginContext | None = None
        self.store: Store | None = None
        self.client: httpx.AsyncClient | None = None
        self.prices = prices.PriceBook()
        self.task: asyncio.Task | None = None

    # --- lifecycle ---------------------------------------------------------

    async def start(self, ctx: PluginContext) -> None:
        self.ctx = ctx
        self.store = Store(ctx.db)
        await self.store.setup()
        self.client = httpx.AsyncClient(headers={"user-agent": USER_AGENT},
                                        follow_redirects=True)
        # Both of these talk to the network, so neither runs inside start()'s
        # 30s budget.
        self.task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self.task:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
            self.task = None
        if self.client:
            await self.client.aclose()
            self.client = None

    def describe(self) -> list[SensorDescriptor]:
        return [
            SensorDescriptor(
                id=SENSOR_ID,
                name="Crypto balances",
                kind="finance",
                widgets=[
                    WidgetSpec(type="stat", title="Portfolio value",
                               data_source="total_value", span=3),
                    WidgetSpec(type="stat", title="Addresses watched",
                               data_source="watch_count", span=3),
                    WidgetSpec(type="status", title="Polling",
                               data_source="poll_status", span=3),
                    WidgetSpec(type="stat", title="Movements (24h)",
                               data_source="alert_count", span=3),
                    WidgetSpec(type="table", title="Watched addresses",
                               data_source="watchlist", span=12),
                    WidgetSpec(type="timeseries", title="Portfolio value (USD)",
                               data_source="value_history", span=8),
                    WidgetSpec(type="donut", title="Value by network",
                               data_source="by_network", span=4),
                    WidgetSpec(type="action", title="Add an address",
                               data_source="add_form", span=4),
                    WidgetSpec(type="action", title="Stop watching an address",
                               data_source="remove_form", span=4),
                    WidgetSpec(type="action", title="Alert thresholds",
                               data_source="threshold_form", span=4),
                    WidgetSpec(type="log", title="Recent movements",
                               data_source="recent_moves", span=12),
                ],
                alarm_rules=[
                    AlarmRuleSpec(
                        key="drain",
                        name="Large withdrawal",
                        description="Holdings fell by more than the configured "
                                    "percentage within the configured window, "
                                    "measured on quantities so market moves do not "
                                    "trigger it.",
                        default_armed=True,
                    ),
                    AlarmRuleSpec(
                        key="value_drop",
                        name="Portfolio value drop",
                        description="The USD value of a watched address fell sharply "
                                    "for any reason, including the market. Off until "
                                    "switched on in the plugin's own settings.",
                        default_armed=False,
                    ),
                ],
                tools=[
                    ToolSpec(
                        name="list_networks",
                        description=(
                            "List the blockchain networks that can be watched. Pass "
                            "`query` to search by name or ticker. Use this to find "
                            "the exact network id before calling add_address."
                        ),
                        parameters={"type": "object", "properties": {
                            "query": {"type": "string",
                                      "description": "Filter, e.g. 'arbitrum' or 'BTC'."},
                        }},
                    ),
                    ToolSpec(
                        name="list_addresses",
                        description=(
                            "List the cryptocurrency addresses currently being "
                            "watched, with each one's latest known balance in USD and "
                            "when it was last checked."
                        ),
                        parameters={"type": "object", "properties": {
                            "network": {"type": "string",
                                        "description": "Only this network."},
                        }},
                    ),
                    ToolSpec(
                        name="check_balance",
                        description=(
                            "Read an address's balance from the chain right now, "
                            "rather than waiting for the next scheduled check. Give "
                            "both network and address for one address, or neither to "
                            "refresh every watched address."
                        ),
                        parameters={"type": "object", "properties": {
                            "network": {"type": "string"},
                            "address": {"type": "string"},
                        }},
                    ),
                    ToolSpec(
                        name="add_address",
                        description=(
                            "Start watching a cryptocurrency address on a given "
                            "network. Call list_networks first if unsure of the "
                            "network id. To stop watching an address, tell the owner "
                            "to remove it from the dashboard -- removal is "
                            "deliberately not available here."
                        ),
                        parameters={
                            "type": "object",
                            "properties": {
                                "network": {"type": "string",
                                            "description": "Network id, e.g. 'base'."},
                                "address": {"type": "string",
                                            "description": "The address to watch."},
                                "label": {"type": "string",
                                          "description": "Optional name for it."},
                            },
                            "required": ["network", "address"],
                        },
                    ),
                    ToolSpec(
                        name="set_thresholds",
                        description=(
                            "Change when a withdrawal raises an alert. Percent is how "
                            "much of the balance may leave, hours is the window it is "
                            "measured over. Applies to every address unless both "
                            "network and address name one."
                        ),
                        parameters={"type": "object", "properties": {
                            "network": {"type": "string"},
                            "address": {"type": "string"},
                            "drain_percent": {"type": "number",
                                              "description": "X: percent of holdings."},
                            "drain_hours": {"type": "number",
                                            "description": "Y: window in hours."},
                            "value_percent": {"type": "number"},
                            "value_hours": {"type": "number"},
                        }},
                    ),
                ],
            )
        ]

    # --- tools -------------------------------------------------------------

    async def handle_command(self, cmd: str, **kwargs: Any) -> Any:
        handler = {
            "list_networks": self._list_networks,
            "list_addresses": self._list_addresses,
            "check_balance": self._check_balance,
            "add_address": self._add_address,
            "set_thresholds": self._set_thresholds,
            # Not a ToolSpec, and so not reachable by the assistant: removal is
            # the one action that blinds the sensor, and no string arriving from
            # a chain should be able to reach it.
            "remove_address": self._remove_address,
            "set_value_rule": self._set_value_rule,
        }.get(cmd)
        if handler is None:
            return await super().handle_command(cmd, **kwargs)
        return await handler(**kwargs)

    async def _list_networks(self, query: str = "", **_: Any) -> dict:
        rows = chains.listing()
        needle = (query or "").strip().lower()
        if needle:
            rows = [r for r in rows
                    if needle in r["network"] or needle in r["name"].lower()
                    or needle == r["symbol"].lower()]
        return {"count": len(rows), "networks": rows[:120]}

    async def _list_addresses(self, network: str = "", **_: Any) -> dict:
        assert self.store is not None
        out = []
        for watch in await self.store.watches():
            if network and watch["network"] != chains_slug(network):
                continue
            latest = await self.store.snapshots(watch["id"], limit=1)
            out.append({
                "network": watch["network"],
                "address": watch["address"],
                "label": watch["label"],
                "usd_value": latest[0]["usd_value"] if latest else None,
                "last_checked": watch["polled_at"],
                "error": watch["last_error"],
            })
        return {"count": len(out), "addresses": out}

    async def _check_balance(self, network: str = "", address: str = "",
                             **_: Any) -> dict:
        assert self.store is not None
        if network or address:
            watch, error = await self._lookup(network, address)
            if error:
                return {"error": error}
            ids = [watch["id"]]
        else:
            ids = None

        result = await self._poll_now(ids)
        if result is None:
            return {"error": "the explorer did not answer in time; the next "
                             "scheduled check will try again"}
        return {"checked": result["checked"], "addresses": result["balances"],
                "errors": result["errors"]}

    async def _poll_now(self, watch_ids: list[int] | None) -> dict | None:
        """Read on demand, abandoning the attempt rather than the plugin's health."""
        try:
            return await asyncio.wait_for(self.poll(watch_ids=watch_ids),
                                          ON_DEMAND_BUDGET)
        except TimeoutError:
            return None

    async def _lookup(self, network: str, address: str) -> tuple[Any, str | None]:
        """Find an existing watch from a network/address pair as the caller typed it."""
        assert self.store is not None
        chain = chains.get(network)
        if chain is None:
            return None, (f"unknown network {network!r}; "
                          "call list_networks to see the options")
        addr = address.strip().lower() if chain.is_evm else address.strip()
        watch = await self.store.find(chain.slug, addr)
        if watch is None or not watch["active"]:
            return None, f"{addr} on {chain.name} is not being watched"
        return watch, None

    async def _add_address(self, network: str = "", address: str = "",
                           label: str = "", **_: Any) -> dict:
        assert self.store is not None and self.ctx is not None
        chain = chains.get(network)
        if chain is None:
            return {"error": f"unknown network {network!r}; "
                             "call list_networks to see the options"}
        try:
            addr = adapters.validate(chain, address)
        except BadAddress as exc:
            return {"error": str(exc)}

        existing = await self.store.find(chain.slug, addr)
        if existing is not None and existing["active"]:
            return {"error": f"{addr} on {chain.name} is already being watched"}

        watch_id = await self.store.add(chain.slug, addr, (label or "").strip(),
                                        datetime.now(UTC))
        await self.ctx.emit(Event(
            sensor_id=SENSOR_ID, severity=SEVERITY_INFO, kind="watch_added",
            summary=f"Now watching {_short(addr)} on {chain.name}",
            payload={"network": chain.slug, "address": addr, "label": label},
        ))
        result = await self._poll_now([watch_id])
        if result is None:
            return {"added": True, "network": chain.slug, "address": addr,
                    "note": f"Watching {_short(addr)} on {chain.name}. The first "
                            "reading is taking a while; it will land on the next "
                            "scheduled check."}
        return {
            "added": True, "network": chain.slug, "address": addr,
            "balance": (result["balances"] or [None])[0],
            "note": f"Watching {_short(addr)} on {chain.name}.",
        }

    async def _remove_address(self, watch_id: int | str = 0, network: str = "",
                              address: str = "", **_: Any) -> dict:
        assert self.store is not None and self.ctx is not None
        watch = None
        if watch_id:
            try:
                watch = await self.store.get(int(watch_id))
            except (TypeError, ValueError):
                return {"error": f"{watch_id!r} is not a watch id"}
        elif network and address:
            watch, error = await self._lookup(network, address)
            if error:
                return {"error": error}
        if watch is None:
            return {"error": "no such watched address"}

        if not await self.store.deactivate(int(watch["id"])):
            return {"error": f"{_short(watch['address'])} is already not being watched"}
        await self.ctx.emit(Event(
            sensor_id=SENSOR_ID, severity=SEVERITY_INFO, kind="watch_removed",
            summary=f"Stopped watching {_short(watch['address'])} on "
                    f"{watch['network']}",
            payload={"network": watch["network"], "address": watch["address"]},
        ))
        return {"removed": True, "note": f"Stopped watching "
                                         f"{_short(watch['address'])}. History kept."}

    async def _set_thresholds(self, network: str = "", address: str = "",
                              network_address: str = "", **values: Any) -> dict:
        assert self.store is not None
        # The dashboard form picks a target from one select, so it arrives as a
        # single "network|address" value; the assistant sends the two fields.
        if network_address:
            network, _, address = network_address.partition("|")
        fields = {k: v for k, v in values.items()
                  if k in ("drain_percent", "drain_hours", "value_percent",
                           "value_hours", "min_move_usd") and v not in (None, "")}
        if not fields:
            return {"error": "give at least one of drain_percent, drain_hours, "
                             "value_percent, value_hours"}
        try:
            numbers = {k: float(v) for k, v in fields.items()}
        except (TypeError, ValueError):
            return {"error": "thresholds must be numbers"}
        for key, value in numbers.items():
            if value <= 0:
                return {"error": f"{key} must be greater than zero"}
            if key.endswith("_percent") and value > 100:
                return {"error": f"{key} must be 100 or less"}

        if network and address:
            watch, error = await self._lookup(network, address)
            if error:
                return {"error": error}
            # Only the four rule thresholds exist per address; the dust floor is
            # a single global, so asking to override it here is a caller error.
            per_watch = {k: v for k, v in numbers.items() if k in OVERRIDABLE}
            if not per_watch:
                return {"error": f"{', '.join(sorted(numbers))} can only be set as a "
                                 "default, not per address"}
            columns = ", ".join(f"{k} = ?" for k in per_watch)
            await self.store.db.execute(
                f"UPDATE watches SET {columns} WHERE id = ?",
                (*per_watch.values(), watch["id"]),
            )
            await self.store.db.commit()
            return {"updated": "address", "network": watch["network"],
                    "address": watch["address"], **per_watch,
                    "note": f"Thresholds set for {_short(watch['address'])}."}

        for key, value in numbers.items():
            await self.store.set_setting(key, value)
        return {"updated": "default", **numbers, "note": "Default thresholds updated."}

    async def _set_value_rule(self, enabled: Any = None, **_: Any) -> dict:
        assert self.store is not None
        on = str(enabled).strip().lower() in ("1", "true", "yes", "on")
        await self.store.set_setting("value_drop_enabled", 1.0 if on else 0.0)
        return {"value_drop_enabled": on,
                "note": f"Portfolio value alerts {'on' if on else 'off'}."}

    # --- polling -----------------------------------------------------------

    async def _loop(self) -> None:
        assert self.ctx is not None
        # Correct the registry's guessed CoinGecko ids once, off the start path.
        with contextlib.suppress(Exception):
            fixed = await prices.reconcile(self.client)
            if fixed:
                self.ctx.log.info("corrected CoinGecko ids for %d chains", fixed)
        while True:
            try:
                await self.poll()
                await self.store.prune(KEEP_DAYS, datetime.now(UTC))
            except asyncio.CancelledError:
                raise
            except Exception:
                self.ctx.log.exception("crypto poll failed")
            await asyncio.sleep(_poll_seconds())

    async def poll(self, now: datetime | None = None,
                   watch_ids: list[int] | None = None) -> dict:
        """Read every active watch once, and act on what changed."""
        assert self.store is not None
        moment = now or datetime.now(UTC)
        watches = [w for w in await self.store.watches()
                   if watch_ids is None or w["id"] in watch_ids]

        balances, errors, alerts = [], [], []
        for watch in watches:
            try:
                reading = await self._poll_one(watch, moment)
            except (ChainUnavailable, MissingCredential, BadAddress) as exc:
                # One chain being unreachable is news about that chain.
                await self.store.mark_polled(int(watch["id"]), moment, str(exc))
                errors.append({"network": watch["network"],
                               "address": watch["address"], "error": str(exc)})
                continue
            balances.append(reading["balance"])
            alerts.extend(reading["alerts"])

        return {"checked": len(watches), "balances": balances, "errors": errors,
                "alerts": alerts}

    async def _poll_one(self, watch: Any, moment: datetime) -> dict:
        assert self.store is not None and self.ctx is not None
        watch_id = int(watch["id"])
        chain = chains.get(watch["network"])
        if chain is None:
            raise ChainUnavailable(f"network {watch['network']!r} is no longer known")

        settings = await self.store.thresholds_for(watch)
        holdings = await adapters.fetch(self.client, chain, watch["address"])
        keys = {
            h.asset: (prices.key_for_native(chain.cg_native) if h.asset == "native"
                      else prices.key_for_token(chain.cg_platform, h.contract))
            for h in holdings
        }

        previous = await self.store.snapshots(watch_id, limit=1)
        previous_rows = (await self.store.holdings(int(previous[0]["id"]))
                         if previous else [])
        base = await self.store.baseline(watch_id, settings["drain_hours"], moment)
        base_rows = await self.store.holdings(int(base["id"])) if base else []

        # Every asset on either side of every comparison has to be priced from
        # the same book. An asset that has been emptied is absent from today's
        # holdings, so quoting only what is held now would value the drained
        # side at zero on both sides and hide the drain completely.
        wanted = {k for k in keys.values() if k}
        wanted |= {row["price_key"] for row in (*previous_rows, *base_rows)
                   if row["price_key"]}
        quotes = await self.prices.quote(self.client, wanted)

        snapshot_id = await self.store.record(watch_id, holdings, keys, quotes, moment)
        current_rows = await self.store.holdings(snapshot_id)
        await self.store.mark_polled(watch_id, moment, None)

        alerts = []
        if previous_rows:
            alerts += await self._report_moves(watch, previous_rows, current_rows,
                                               quotes, settings, moment)
        alerts += await self._check_rules(watch, current_rows, base, base_rows, quotes,
                                          settings, moment)

        total = sum(row["usd_value"] or 0.0 for row in current_rows)
        return {
            "balance": {
                "network": watch["network"], "address": watch["address"],
                "label": watch["label"], "usd_value": round(total, 2),
                "assets": len(current_rows),
                "native": next((row["quantity"] for row in current_rows
                                if row["asset"] == "native"), "0"),
                "symbol": chain.symbol,
            },
            "alerts": alerts,
        }

    async def _report_moves(self, watch: Any, before: list, after: list,
                            quotes: dict, settings: dict, moment: datetime) -> list:
        assert self.store is not None and self.ctx is not None
        moves = store.movements(before, after, quotes, settings["min_move_usd"])
        if not moves:
            return []
        await self.store.log_moves(int(watch["id"]), moves, moment)

        raised = []
        for deposit in (True, False):
            side = [m for m in moves if m.deposit is deposit]
            if not side:
                continue
            kind = "deposit" if deposit else "withdrawal"
            await self.ctx.emit(Event(
                sensor_id=SENSOR_ID,
                severity=SEVERITY_INFO if deposit else SEVERITY_LOW,
                kind=kind,
                summary=(f"{'Deposit to' if deposit else 'Withdrawal from'} "
                         f"{_who(watch)} on {watch['network']}: "
                         f"{store.summarise(side)}"),
                # Token symbols and names are chosen by whoever deployed the
                # token, so they are sensor input, not our own words.
                sensor_text=_symbols(side) or None,
                payload={
                    "network": watch["network"], "address": watch["address"],
                    "moves": [{"asset": m.asset, "symbol": m.symbol,
                               "delta": str(m.delta), "usd": m.usd} for m in side],
                },
            ))
            raised.append(kind)
        return raised

    async def _check_rules(self, watch: Any, current_rows: list, base: Any,
                           base_rows: list, quotes: dict, settings: dict,
                           moment: datetime) -> list:
        assert self.store is not None and self.ctx is not None
        watch_id = int(watch["id"])
        raised = []

        if base is not None and await self._quiet(watch_id, "drain",
                                                  settings["drain_hours"], moment):
            breach = store.drain(
                base_rows, current_rows, quotes,
                settings["drain_percent"], settings["drain_hours"],
            )
            if breach:
                await self._raise(watch, breach, SEVERITY_HIGH, moment)
                raised.append("drain")

        if settings.get("value_drop_enabled"):
            since = await self.store.baseline(watch_id, settings["value_hours"], moment)
            latest = await self.store.snapshots(watch_id, limit=1)
            if (since is not None and latest
                    and await self._quiet(watch_id, "value_drop",
                                          settings["value_hours"], moment)):
                breach = store.value_drop(since, latest[0], settings["value_percent"],
                                          settings["value_hours"])
                if breach:
                    await self._raise(watch, breach, SEVERITY_LOW, moment)
                    raised.append("value_drop")
        return raised

    async def _quiet(self, watch_id: int, rule: str, hours: float,
                     moment: datetime) -> bool:
        """True when this rule has not already fired inside its own window."""
        assert self.store is not None
        last = await self.store.alerted_at(watch_id, rule)
        return last is None or (moment.timestamp() - last) >= hours * 3600.0

    async def _raise(self, watch: Any, breach: store.Breach, severity: int,
                     moment: datetime) -> None:
        assert self.store is not None and self.ctx is not None
        unit = "USD" if breach.basis == "usd" else "native units"
        wording = ("of holdings left" if breach.rule == "drain"
                   else "of portfolio value was lost by")
        await self.ctx.emit(Event(
            sensor_id=SENSOR_ID, severity=severity, kind=breach.rule,
            summary=(f"{breach.percent:.1f}% {wording} {_who(watch)} on "
                     f"{watch['network']} within {breach.hours:g}h "
                     f"(alerts above {breach.threshold:g}%)"),
            payload={
                "network": watch["network"], "address": watch["address"],
                "rule": breach.rule, "percent": round(breach.percent, 2),
                "threshold": breach.threshold, "hours": breach.hours,
                "before": round(breach.before, 6), "after": round(breach.after, 6),
                "measured_in": unit,
            },
        ))
        await self.store.mark_alerted(int(watch["id"]), breach.rule, moment)

    # --- widgets -----------------------------------------------------------

    async def query(self, source: str, **kwargs: Any) -> Any:
        assert self.store is not None
        handler = getattr(self, f"_w_{source}", None)
        if handler is None:
            return await super().query(source, **kwargs)
        return await handler()

    async def _w_total_value(self) -> dict:
        total, priced = 0.0, False
        for watch in await self.store.watches():
            latest = await self.store.snapshots(int(watch["id"]), limit=1)
            if latest and latest[0]["usd_value"] is not None:
                total += latest[0]["usd_value"]
                priced = True
        return {"value": f"${total:,.2f}" if priced else "—",
                "label": "across all watched addresses"}

    async def _w_watch_count(self) -> dict:
        watches = await self.store.watches()
        networks = {w["network"] for w in watches}
        return {"value": len(watches),
                "label": f"on {len(networks)} network{'s' if len(networks) != 1 else ''}"}

    async def _w_poll_status(self) -> dict:
        watches = await self.store.watches()
        if not watches:
            return {"state": "offline"}
        failing = [w for w in watches if w["last_error"]]
        if not any(w["polled_at"] for w in watches):
            return {"state": "degraded"}
        return {"state": "degraded" if failing else "healthy"}

    async def _w_alert_count(self) -> dict:
        cutoff = store.to_sql(datetime.now(UTC) - timedelta(days=1))
        rows = await self.store.recent_moves(limit=500)
        return {"value": sum(1 for r in rows if r["ts"] >= cutoff),
                "label": "movements in the last 24h"}

    async def _w_watchlist(self) -> list[dict]:
        out = []
        for watch in await self.store.watches():
            chain = chains.get(watch["network"])
            latest = await self.store.snapshots(int(watch["id"]), limit=1)
            value = latest[0]["usd_value"] if latest else None
            thresholds = await self.store.thresholds_for(watch)
            out.append({
                "Network": chain.name if chain else watch["network"],
                "Label": watch["label"] or "—",
                "Address": _short(watch["address"]),
                "Value": f"${value:,.2f}" if value is not None else "unpriced",
                "Alert at": f"{thresholds['drain_percent']:g}% / "
                            f"{thresholds['drain_hours']:g}h",
                "Checked": watch["polled_at"] or "never",
                "Status": watch["last_error"] or "ok",
            })
        return out

    async def _w_value_history(self) -> list[dict]:
        # One point per address per hour -- the last reading in that hour --
        # so an address polled twice in an hour is not counted twice.
        rows = await self.store.db.execute(
            "WITH ranked AS ("
            "  SELECT watch_id, usd_value,"
            "         strftime('%Y-%m-%d %H:00', ts) AS bucket,"
            "         row_number() OVER (PARTITION BY watch_id,"
            "             strftime('%Y-%m-%d %H:00', ts)"
            "             ORDER BY ts DESC, id DESC) AS rn"
            "    FROM snapshots WHERE usd_value IS NOT NULL)"
            " SELECT bucket, sum(usd_value) AS usd FROM ranked WHERE rn = 1"
            " GROUP BY bucket ORDER BY bucket DESC LIMIT 72"
        )
        return [{"bucket": r["bucket"], "usd": round(r["usd"] or 0, 2)}
                for r in await rows.fetchall()]

    async def _w_by_network(self) -> list[dict]:
        out: dict[str, float] = {}
        for watch in await self.store.watches():
            latest = await self.store.snapshots(int(watch["id"]), limit=1)
            if latest and latest[0]["usd_value"]:
                out[watch["network"]] = out.get(watch["network"], 0.0) + \
                    latest[0]["usd_value"]
        return [{"network": k, "usd": round(v, 2)}
                for k, v in sorted(out.items(), key=lambda kv: -kv[1])]

    async def _w_recent_moves(self) -> list[dict]:
        out = []
        for row in await self.store.recent_moves(limit=50):
            delta = row["delta"]
            out.append({
                "ts": row["ts"],
                "where": f"{row['network']} {_short(row['address'])}",
                "asset": row["symbol"] or store.short_asset(row["asset"]),
                "change": f"{'+' if not delta.startswith('-') else ''}{delta}",
                "usd": f"${abs(row['usd']):,.2f}" if row["usd"] is not None else "",
            })
        return out

    # The three forms. `fields` is read by the `action` renderer, which turns
    # each entry into an input and sends the values as the command's arguments.

    async def _w_add_form(self) -> dict:
        return {
            "label": "Watch this address",
            "command": "add_address",
            "detail": "Pick the network, then paste the address.",
            "state": "ready",
            "fields": [
                {"name": "network", "label": "Network", "type": "select",
                 "options": [{"value": c["network"],
                              "label": f"{c['name']} ({c['symbol']})"}
                             for c in chains.listing()]},
                {"name": "address", "label": "Address", "type": "text",
                 "placeholder": "0x… / bc1… / …", "required": True},
                {"name": "label", "label": "Name (optional)", "type": "text",
                 "placeholder": "Cold storage"},
            ],
        }

    async def _w_remove_form(self) -> dict:
        watches = await self.store.watches()
        return {
            "label": "Stop watching",
            "command": "remove_address",
            "state": "ready" if watches else "missing",
            "detail": ("Polling stops; the recorded history is kept."
                       if watches else "No addresses are being watched yet."),
            "confirm": "Stop monitoring this address? Alerting for it stops "
                       "immediately. Its history is kept.",
            "fields": [{
                "name": "watch_id", "label": "Address", "type": "select",
                "options": [
                    {"value": str(w["id"]),
                     "label": f"{w['label'] or _short(w['address'])} — {w['network']}"}
                    for w in watches
                ],
            }],
        }

    async def _w_threshold_form(self) -> dict:
        settings = await self.store.settings()
        watches = await self.store.watches()
        return {
            "label": "Save thresholds",
            "command": "set_thresholds",
            "state": "ready",
            "detail": (f"Alerting when more than {settings['drain_percent']:g}% of "
                       f"holdings leave within {settings['drain_hours']:g} hours. "
                       "Leave the address on 'All addresses' to change the default."),
            "fields": [
                {"name": "network_address", "label": "Applies to", "type": "select",
                 "options": [{"value": "", "label": "All addresses (default)"}] + [
                     {"value": f"{w['network']}|{w['address']}",
                      "label": f"{w['label'] or _short(w['address'])} — {w['network']}"}
                     for w in watches
                 ]},
                {"name": "drain_percent", "label": "Withdrawal alert at (%)",
                 "type": "number", "value": settings["drain_percent"],
                 "min": 0.1, "max": 100, "step": 0.1},
                {"name": "drain_hours", "label": "Measured over (hours)",
                 "type": "number", "value": settings["drain_hours"],
                 "min": 0.1, "step": 0.5},
            ],
        }


def _symbols(moves: list[store.Move]) -> str:
    """The chain's own words for the assets that moved. Untrusted by definition."""
    named = [m.symbol for m in moves if m.symbol]
    return ", ".join(dict.fromkeys(named))


def chains_slug(network: str) -> str:
    chain = chains.get(network)
    return chain.slug if chain else network
