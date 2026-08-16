"""The crypto plugin: watching addresses, and knowing when funds leave one."""

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from blackice_crypto import SENSOR_ID, CryptoPlugin, adapters, chains, prices
from blackice_crypto import plugin as plugin_module
from blackice_crypto.adapters import Holding

from blackice import db
from blackice.llm.tools import ToolRegistry, project_plugin_tools
from blackice.plugins.registry import Registry
from blackice.services import events

# A real Ethereum-shaped address; `base` and `ethereum` both accept it.
ADDR = "0x" + "ab" * 20
BTC = "bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq"
USDC = "0x" + "cd" * 20

ETH_PRICE = "coin:ethereum"
USDC_PRICE = f"base:{USDC}"


def holding(asset, quantity, symbol="", contract=None):
    return Holding(asset, Decimal(str(quantity)), symbol, symbol, contract)


@pytest.fixture
def chain_data(monkeypatch):
    """Stand in for every block explorer. Keyed by (network, address)."""
    book: dict[tuple[str, str], list[Holding]] = {}

    async def fetch(client, chain, address):
        try:
            return book[(chain.slug, address)]
        except KeyError:
            raise adapters.ChainUnavailable(f"{chain.name} is not answering") from None

    monkeypatch.setattr(adapters, "fetch", fetch)
    return book


@pytest.fixture
def quotes(monkeypatch):
    """Stand in for CoinGecko. Missing keys mean an unpriced asset."""
    prices_by_key: dict[str, float] = {ETH_PRICE: 1000.0, USDC_PRICE: 1.0}

    async def quote(self, client, keys):
        return {k: prices_by_key[k] for k in keys if k in prices_by_key}

    async def reconcile(client):
        return 0

    monkeypatch.setattr(prices.PriceBook, "quote", quote)
    monkeypatch.setattr(prices, "reconcile", reconcile)
    return prices_by_key


@pytest.fixture
async def reg(data_dir, chain_data, quotes, monkeypatch):
    monkeypatch.setenv("BLACKICE_CRYPTO_POLL_SECONDS", "3600")
    r = Registry()
    await r.start_plugin(CryptoPlugin, events.record)
    yield r
    await r.stop_all()


def plugin_of(reg):
    return reg.supervisors["crypto"].plugin


def healthy(reg):
    return reg.supervisors["crypto"].health()["state"] == "healthy"


async def events_of(kind):
    return await db.fetchall("SELECT * FROM events WHERE kind = ? ORDER BY id", (kind,))


async def watch_one(reg, chain_data, holdings, network="base", address=ADDR):
    """Add a watch whose first reading is `holdings`, and return the moment."""
    chain_data[(network, address)] = holdings
    result = await reg.command("crypto", "add_address", network=network,
                               address=address)
    assert result.get("added") is True, result
    return datetime.now(UTC)


# --- discovery and projection ----------------------------------------------

async def test_discovery_finds_installed_plugin(data_dir):
    assert "crypto" in [c.name for c in Registry().discover()]


async def test_start_projects_the_sensor_and_both_rules(reg, data_dir):
    ids = [r["id"] for r in await db.fetchall("SELECT id FROM sensors")]
    assert SENSOR_ID in ids

    rules = await db.fetchall(
        "SELECT r.key, s.armed FROM alarm_rules r"
        " LEFT JOIN alarm_state s ON s.rule_id = r.id WHERE r.plugin = 'crypto'"
        " ORDER BY r.key"
    )
    assert [(r["key"], r["armed"]) for r in rules] == [("drain", 1), ("value_drop", 0)]


# --- the chain registry ----------------------------------------------------

def test_registry_covers_the_top_hundred_chains():
    assert len(chains.CHAINS) >= 100
    assert sum(1 for c in chains.CHAINS.values() if c.layer == 2) >= 25
    # Every chain names an adapter that actually exists.
    assert {c.via for c in chains.CHAINS.values()} <= set(adapters.ADAPTERS)


def test_networks_are_found_by_alias_and_ticker():
    assert chains.get("ETH").slug == "ethereum"
    assert chains.get("Arbitrum One").slug == "arbitrum"
    assert chains.get("bep20").slug == "bsc"
    assert chains.get("nonesuch") is None


async def test_list_networks_searches(reg):
    everything = await reg.command("crypto", "list_networks")
    assert everything["count"] >= 100

    hits = await reg.command("crypto", "list_networks", query="arbitrum")
    assert {n["network"] for n in hits["networks"]} == {"arbitrum", "arbitrum-nova"}


# --- adding and removing ---------------------------------------------------

async def test_add_watches_the_address_and_reads_it_once(reg, chain_data):
    await watch_one(reg, chain_data, [holding("native", 3, "ETH")])

    listed = await reg.command("crypto", "list_addresses")
    assert listed["count"] == 1
    assert listed["addresses"][0]["usd_value"] == 3000.0

    raised = await events_of("watch_added")
    assert len(raised) == 1
    assert raised[0]["sensor_id"] == SENSOR_ID


async def test_an_unknown_network_is_an_error_not_a_failure(reg):
    result = await reg.command("crypto", "add_address", network="dogechain-xyz",
                               address=ADDR)

    assert "unknown network" in result["error"]
    assert healthy(reg)


async def test_an_address_of_the_wrong_shape_is_refused(reg):
    result = await reg.command("crypto", "add_address", network="base",
                               address="not-an-address")

    assert "not a valid" in result["error"]
    assert healthy(reg)
    # The Bitcoin address is fine on Bitcoin and wrong on Base.
    assert "not a valid" in (await reg.command(
        "crypto", "add_address", network="base", address=BTC))["error"]


async def test_the_same_address_is_not_watched_twice(reg, chain_data):
    await watch_one(reg, chain_data, [holding("native", 1)])
    again = await reg.command("crypto", "add_address", network="base", address=ADDR)

    assert "already being watched" in again["error"]
    assert healthy(reg)


async def test_removing_keeps_the_history_and_re_adding_resumes_it(reg, chain_data):
    await watch_one(reg, chain_data, [holding("native", 5)])
    store = plugin_of(reg).store
    watch_id = (await store.find("base", ADDR))["id"]

    removed = await reg.command("crypto", "remove_address", watch_id=watch_id)
    assert removed["removed"] is True
    assert (await reg.command("crypto", "list_addresses"))["count"] == 0
    assert len(await store.snapshots(watch_id, limit=10)) == 1  # history survived
    assert len(await events_of("watch_removed")) == 1

    await reg.command("crypto", "add_address", network="base", address=ADDR)
    assert (await store.find("base", ADDR))["id"] == watch_id  # same series


async def test_removing_something_unwatched_is_an_error_not_a_failure(reg):
    result = await reg.command("crypto", "remove_address", watch_id=404)

    assert result["error"] == "no such watched address"
    assert healthy(reg)


# --- deposits and withdrawals ----------------------------------------------

async def test_a_deposit_and_a_withdrawal_are_reported_separately(reg, chain_data):
    await watch_one(reg, chain_data, [holding("native", 10, "ETH"),
                                      holding(USDC, 500, "USDC", USDC)])

    chain_data[("base", ADDR)] = [holding("native", 12, "ETH"),
                                  holding(USDC, 100, "USDC", USDC)]
    await plugin_of(reg).poll()

    deposits = await events_of("deposit")
    withdrawals = await events_of("withdrawal")
    assert len(deposits) == 1 and len(withdrawals) == 1
    assert "$2,000.00" in deposits[0]["summary"]
    assert "$400.00" in withdrawals[0]["summary"]
    # The token's own symbol is attacker-chosen, so it is sensor input.
    assert withdrawals[0]["sensor_text"] == "USDC"
    assert "USDC" not in withdrawals[0]["summary"]


async def test_dust_does_not_reach_the_timeline(reg, chain_data):
    await watch_one(reg, chain_data, [holding("native", 10, "ETH")])

    chain_data[("base", ADDR)] = [holding("native", 10, "ETH"),
                                  holding(USDC, "0.25", "SPAM", USDC)]
    await plugin_of(reg).poll()

    assert await events_of("deposit") == []


async def test_the_first_reading_is_not_a_deposit(reg, chain_data):
    await watch_one(reg, chain_data, [holding("native", 10, "ETH")])
    assert await events_of("deposit") == []


# --- the security rule -----------------------------------------------------

async def test_a_drain_over_the_window_raises_a_high_severity_alert(reg, chain_data):
    start = await watch_one(reg, chain_data, [holding("native", 10, "ETH")])

    chain_data[("base", ADDR)] = [holding("native", 4, "ETH")]
    await plugin_of(reg).poll(now=start + timedelta(hours=25))

    alerts = await events_of("drain")
    assert len(alerts) == 1
    assert alerts[0]["severity"] == 3
    payload = db.loads(alerts[0]["payload"])
    assert payload["percent"] == pytest.approx(60.0)
    assert payload["threshold"] == 20.0
    assert "60.0% of holdings left" in alerts[0]["summary"]


async def test_a_price_crash_is_not_a_drain(reg, chain_data, quotes):
    start = await watch_one(reg, chain_data, [holding("native", 10, "ETH")])

    quotes[ETH_PRICE] = 100.0  # the market fell 90%; nothing left the address
    await plugin_of(reg).poll(now=start + timedelta(hours=25))

    assert await events_of("drain") == []


async def test_a_token_emptied_completely_still_counts_as_a_drain(reg, chain_data):
    """The emptied asset is absent from today's holdings. If it were priced only
    from what is held now, both sides would value at zero and hide the drain."""
    start = await watch_one(reg, chain_data, [holding("native", 1, "ETH"),
                                              holding(USDC, 100_000, "USDC", USDC)])

    chain_data[("base", ADDR)] = [holding("native", 1, "ETH")]
    await plugin_of(reg).poll(now=start + timedelta(hours=25))

    alerts = await events_of("drain")
    assert len(alerts) == 1
    assert db.loads(alerts[0]["payload"])["percent"] == pytest.approx(99.0, abs=0.1)


async def test_a_withdrawal_under_the_threshold_stays_quiet(reg, chain_data):
    start = await watch_one(reg, chain_data, [holding("native", 10, "ETH")])

    chain_data[("base", ADDR)] = [holding("native", 9, "ETH")]  # 10%, under 20
    await plugin_of(reg).poll(now=start + timedelta(hours=25))

    assert await events_of("drain") == []
    assert len(await events_of("withdrawal")) == 1  # still reported as a movement


async def test_nothing_fires_before_the_window_has_elapsed(reg, chain_data):
    start = await watch_one(reg, chain_data, [holding("native", 10, "ETH")])

    chain_data[("base", ADDR)] = [holding("native", 1, "ETH")]
    await plugin_of(reg).poll(now=start + timedelta(hours=1))

    assert await events_of("drain") == []


async def test_one_drain_alerts_once_per_window(reg, chain_data):
    start = await watch_one(reg, chain_data, [holding("native", 10, "ETH")])
    chain_data[("base", ADDR)] = [holding("native", 1, "ETH")]

    await plugin_of(reg).poll(now=start + timedelta(hours=25))
    await plugin_of(reg).poll(now=start + timedelta(hours=26))

    assert len(await events_of("drain")) == 1


async def test_an_unpriced_chain_falls_back_to_the_native_quantity(reg, chain_data):
    chain_data[("kaspa", "kaspa-address")] = [holding("native", 100, "KAS")]
    added = await reg.command("crypto", "add_address", network="kaspa",
                              address="kaspa-address")
    assert added["added"] is True
    start = datetime.now(UTC)

    chain_data[("kaspa", "kaspa-address")] = [holding("native", 30, "KAS")]
    await plugin_of(reg).poll(now=start + timedelta(hours=25))

    alerts = await events_of("drain")
    assert len(alerts) == 1
    assert db.loads(alerts[0]["payload"])["measured_in"] == "native units"


# --- thresholds ------------------------------------------------------------

async def test_a_per_address_threshold_overrides_the_default(reg, chain_data):
    start = await watch_one(reg, chain_data, [holding("native", 10, "ETH")])

    set_ = await reg.command("crypto", "set_thresholds", network="base",
                             address=ADDR, drain_percent=5)
    assert set_["updated"] == "address"

    chain_data[("base", ADDR)] = [holding("native", 9, "ETH")]  # 10%: under the
    await plugin_of(reg).poll(now=start + timedelta(hours=25))  # default, over 5

    assert len(await events_of("drain")) == 1


async def test_the_default_threshold_applies_to_every_address(reg, chain_data):
    start = await watch_one(reg, chain_data, [holding("native", 10, "ETH")])
    await reg.command("crypto", "set_thresholds", drain_percent=50, drain_hours=1)

    chain_data[("base", ADDR)] = [holding("native", 4, "ETH")]
    await plugin_of(reg).poll(now=start + timedelta(hours=2))

    assert len(await events_of("drain")) == 1


async def test_impossible_thresholds_are_refused(reg):
    assert "greater than zero" in (await reg.command(
        "crypto", "set_thresholds", drain_percent=0))["error"]
    assert "100 or less" in (await reg.command(
        "crypto", "set_thresholds", drain_percent=140))["error"]
    assert "must be numbers" in (await reg.command(
        "crypto", "set_thresholds", drain_hours="soon"))["error"]
    assert "at least one of" in (await reg.command("crypto", "set_thresholds"))["error"]
    assert healthy(reg)


async def test_thresholds_for_an_unwatched_address_are_refused(reg):
    result = await reg.command("crypto", "set_thresholds", network="base",
                               address=ADDR, drain_percent=10)

    assert "is not being watched" in result["error"]
    assert healthy(reg)


# --- the portfolio-value rule ----------------------------------------------

async def test_the_value_rule_is_off_until_it_is_switched_on(reg, chain_data, quotes):
    start = await watch_one(reg, chain_data, [holding("native", 10, "ETH")])
    quotes[ETH_PRICE] = 100.0

    await plugin_of(reg).poll(now=start + timedelta(hours=25))
    assert await events_of("value_drop") == []

    # Measured against the snapshot taken before the crash, an hour later.
    await reg.command("crypto", "set_value_rule", enabled=True)
    await plugin_of(reg).poll(now=start + timedelta(hours=26))

    alerts = await events_of("value_drop")
    assert len(alerts) == 1
    assert alerts[0]["severity"] == 1  # LOW: the market is not an intrusion
    assert db.loads(alerts[0]["payload"])["percent"] == pytest.approx(90.0)
    # ...and the quantity rule stayed silent, because nothing actually moved.
    assert await events_of("drain") == []


# --- failure containment ---------------------------------------------------

async def test_an_unreachable_chain_is_recorded_without_going_unhealthy(reg,
                                                                        chain_data):
    await watch_one(reg, chain_data, [holding("native", 1)])
    del chain_data[("base", ADDR)]  # the explorer stops answering

    result = await plugin_of(reg).poll()

    assert result["errors"][0]["error"] == "Base is not answering"
    assert healthy(reg)
    listed = await reg.command("crypto", "list_addresses")
    assert "not answering" in listed["addresses"][0]["error"]


async def test_a_slow_explorer_does_not_blow_the_supervisor_budget(reg, chain_data,
                                                                   monkeypatch):
    """An on-demand read runs inside the supervisor's 30s call timeout. Giving up
    first keeps a slow explorer from marking the whole plugin unhealthy."""
    await watch_one(reg, chain_data, [holding("native", 1)])
    monkeypatch.setattr(plugin_module, "ON_DEMAND_BUDGET", 0.05)

    async def crawl(client, chain, address):
        await asyncio.sleep(10)

    monkeypatch.setattr(adapters, "fetch", crawl)
    result = await reg.command("crypto", "check_balance", network="base", address=ADDR)

    assert "did not answer in time" in result["error"]
    assert healthy(reg)


async def test_one_bad_chain_does_not_stop_the_others(reg, chain_data):
    await watch_one(reg, chain_data, [holding("native", 1)])
    chain_data[("bitcoin", BTC)] = [holding("native", 2, "BTC")]
    await reg.command("crypto", "add_address", network="bitcoin", address=BTC)
    del chain_data[("base", ADDR)]

    result = await plugin_of(reg).poll()

    assert len(result["errors"]) == 1 and len(result["balances"]) == 1
    assert result["balances"][0]["network"] == "bitcoin"
    assert healthy(reg)


# --- the dashboard ---------------------------------------------------------

async def test_every_widget_data_source_answers(reg, chain_data):
    descriptor = reg.descriptor_for(SENSOR_ID)
    assert all(w.data_source for w in descriptor.widgets)

    await watch_one(reg, chain_data, [holding("native", 10, "ETH")])
    chain_data[("base", ADDR)] = [holding("native", 8, "ETH")]
    await plugin_of(reg).poll()

    for widget in descriptor.widgets:
        assert await reg.query("crypto", widget.data_source) is not None

    assert (await reg.query("crypto", "total_value"))["value"] == "$8,000.00"
    assert (await reg.query("crypto", "watch_count"))["value"] == 1
    assert (await reg.query("crypto", "poll_status"))["state"] == "healthy"
    assert (await reg.query("crypto", "watchlist"))[0]["Alert at"] == "20% / 24h"
    assert (await reg.query("crypto", "by_network"))[0]["network"] == "base"
    assert (await reg.query("crypto", "recent_moves"))[0]["change"].startswith("-2")


async def test_the_forms_declare_the_fields_the_dashboard_renders(reg, chain_data):
    add = await reg.query("crypto", "add_form")
    assert add["command"] == "add_address"
    fields = {f["name"]: f for f in add["fields"]}
    assert fields["address"]["required"] is True
    assert len(fields["network"]["options"]) >= 100

    empty = await reg.query("crypto", "remove_form")
    assert empty["state"] == "missing" and empty["fields"][0]["options"] == []

    await watch_one(reg, chain_data, [holding("native", 1)])
    remove = await reg.query("crypto", "remove_form")
    assert remove["state"] == "ready"
    assert remove["confirm"]
    # The option value is what the command is called with.
    watch_id = remove["fields"][0]["options"][0]["value"]
    assert (await reg.command("crypto", "remove_address",
                              watch_id=watch_id))["removed"] is True


async def test_the_threshold_form_round_trips_through_its_own_command(reg, chain_data):
    await watch_one(reg, chain_data, [holding("native", 1)])
    form = await reg.query("crypto", "threshold_form")
    target = {f["name"]: f for f in form["fields"]}["network_address"]["options"][1]

    result = await reg.command("crypto", form["command"],
                               network_address=target["value"], drain_percent=7)

    assert result["updated"] == "address"
    assert (await reg.query("crypto", "watchlist"))[0]["Alert at"] == "7% / 24h"


# --- the assistant ---------------------------------------------------------

async def test_every_tool_reaches_the_llm(reg):
    tools = ToolRegistry()
    project_plugin_tools(reg, tools)

    assert {
        "crypto.list_networks", "crypto.list_addresses", "crypto.check_balance",
        "crypto.add_address", "crypto.set_thresholds",
    } <= set(tools.tools)
    assert (await tools.dispatch("crypto.list_networks", {"query": "base"}))["count"]


async def test_the_assistant_cannot_stop_the_monitoring(reg, chain_data):
    """Removal blinds the sensor, so no chain-supplied string may reach it."""
    tools = ToolRegistry()
    project_plugin_tools(reg, tools)

    assert "crypto.remove_address" not in tools.tools
    assert "crypto.set_value_rule" not in tools.tools
    # The dashboard's own button still reaches it, through the supervisor.
    await watch_one(reg, chain_data, [holding("native", 1)])
    watch_id = (await plugin_of(reg).store.find("base", ADDR))["id"]
    assert (await reg.command("crypto", "remove_address",
                              watch_id=watch_id))["removed"] is True


async def test_check_balance_reads_the_chain_on_demand(reg, chain_data):
    await watch_one(reg, chain_data, [holding("native", 2, "ETH")])
    chain_data[("base", ADDR)] = [holding("native", 7, "ETH")]

    result = await reg.command("crypto", "check_balance", network="base", address=ADDR)

    assert result["checked"] == 1
    assert result["addresses"][0]["usd_value"] == 7000.0


async def test_checking_an_unwatched_address_is_an_error_not_a_failure(reg):
    result = await reg.command("crypto", "check_balance", network="base", address=ADDR)

    assert "is not being watched" in result["error"]
    assert healthy(reg)


# --- talking to real explorer shapes ---------------------------------------

def mock_client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_blockscout_returns_the_native_coin_and_every_token():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token-balances"):
            return httpx.Response(200, json=[
                {"token": {"address": USDC, "symbol": "USDC", "name": "USD Coin",
                           "decimals": "6", "type": "ERC-20"},
                 "value": "250000000"},
                {"token": {"address": "0x" + "ee" * 20, "symbol": "APE",
                           "name": "Ape", "decimals": "0", "type": "ERC-721"},
                 "value": "3"},
            ])
        return httpx.Response(200, json={"coin_balance": "1500000000000000000"})

    async with mock_client(handler) as client:
        held = await adapters.evm(client, chains.get("base"), ADDR)

    assert [(h.asset, str(h.quantity)) for h in held] == [
        ("native", "1.5"), (USDC, "250"),  # the NFT is not a balance
    ]


async def test_blockchair_reads_a_bitcoin_address():
    def handler(request):
        return httpx.Response(200, json={"data": {BTC: {"address":
                                                        {"balance": 12345678}}}})

    async with mock_client(handler) as client:
        held = await adapters.blockchair(client, chains.get("bitcoin"), BTC)

    assert str(held[0].quantity) == "0.12345678"


async def test_a_failing_explorer_becomes_chain_unavailable():
    async with mock_client(lambda r: httpx.Response(503)) as client:
        with pytest.raises(adapters.ChainUnavailable):
            await adapters.blockchair(client, chains.get("bitcoin"), BTC)


async def test_price_keys_follow_the_chain_registry():
    base = chains.get("base")
    assert prices.key_for_native(base.cg_native) == ETH_PRICE
    assert prices.key_for_token(base.cg_platform, USDC.upper()) == USDC_PRICE
    # An unpriceable chain simply has no key, and its holdings go unvalued.
    assert prices.key_for_token(None, USDC) is None
