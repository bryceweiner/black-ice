"""Reading balances off each chain family.

Every adapter has the same shape -- ``(client, chain, address) -> [Holding]`` --
and every one of them raises only the three errors below. That matters for
health: a single flaky public endpoint among a hundred chains must not mark the
whole plugin unhealthy, so `plugin` catches all three and records them against
the individual watch instead.

Symbols and names come back from the chain and are therefore attacker-chosen
(anyone can deploy a token called whatever they like). They are carried here
unmodified and handed to `Event.sensor_text`, never to `summary`.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from .chains import BLOCKSCOUT, ETHERSCAN_V2, EVM_RPC, Chain

ETHERSCAN_V2_URL = "https://api.etherscan.io/v2/api"
# Deriving a token list from transfer history costs one request per contract,
# so the Etherscan path is capped. Blockscout returns the whole list in one.
ETHERSCAN_TOKEN_CAP = 20
# Blockscout's address endpoints are routinely slow. Kept well under the
# supervisor's 30s call budget, which a user-triggered read has to fit inside.
TIMEOUT = 15.0


class ChainUnavailable(RuntimeError):
    """The backend for this chain could not answer. Transient, per-chain."""


class BadAddress(ValueError):
    """The address is not valid for this chain. Caller error, not a fault."""


class MissingCredential(RuntimeError):
    """This chain needs an API key that is not configured."""


@dataclass(frozen=True)
class Holding:
    asset: str                  # stable key: "native", or the contract address
    quantity: Decimal
    symbol: str = ""            # untrusted: chain-supplied
    name: str = ""              # untrusted: chain-supplied
    contract: str | None = None


def _dec(raw: Any, decimals: int) -> Decimal:
    """Integer base units -> a decimal quantity, tolerating hex and junk."""
    try:
        if isinstance(raw, str) and raw.startswith("0x"):
            value = Decimal(int(raw, 16))
        else:
            value = Decimal(str(raw))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ChainUnavailable(f"unreadable balance {raw!r}") from exc
    return value / (Decimal(10) ** int(decimals or 0))


async def _json(client: httpx.AsyncClient, method: str, url: str, **kw: Any) -> Any:
    try:
        resp = await client.request(method, url, timeout=TIMEOUT, **kw)
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPStatusError as exc:
        raise ChainUnavailable(f"{url} returned {exc.response.status_code}") from exc
    except (httpx.HTTPError, ValueError) as exc:
        raise ChainUnavailable(f"{url} unreachable: {exc}") from exc


async def _get(client, url, **kw):
    return await _json(client, "GET", url, **kw)


async def _rpc(client, url, method, params) -> Any:
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    data = await _json(client, "POST", url, json=body)
    if isinstance(data, dict) and data.get("error"):
        raise ChainUnavailable(str(data["error"]))
    return (data or {}).get("result")


# --- address shapes --------------------------------------------------------

_SHAPES = {
    "evm": re.compile(r"^0x[0-9a-fA-F]{40}$"),
    "solana": re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$"),
    "tron": re.compile(r"^T[1-9A-HJ-NP-Za-km-z]{33}$"),
    "xrpl": re.compile(r"^r[1-9A-HJ-NP-Za-km-z]{24,34}$"),
    "starknet": re.compile(r"^0x[0-9a-fA-F]{60,64}$"),
    "hedera": re.compile(r"^\d+\.\d+\.\d+$"),
    "stellar": re.compile(r"^G[A-Z2-7]{55}$"),
    "algorand": re.compile(r"^[A-Z2-7]{58}$"),
    "tezos": re.compile(r"^(tz[123]|KT1)[1-9A-HJ-NP-Za-km-z]{33}$"),
}


def validate(chain: Chain, address: str) -> str:
    """Reject an obviously wrong address before spending a request on it."""
    addr = (address or "").strip()
    if not addr:
        raise BadAddress("address is required")
    shape = _SHAPES.get("evm" if chain.is_evm else chain.via)
    if shape and not shape.match(addr):
        raise BadAddress(f"{addr!r} is not a valid {chain.name} address")
    return addr.lower() if chain.is_evm else addr


# --- EVM -------------------------------------------------------------------

async def _blockscout(client, chain: Chain, address: str) -> list[Holding]:
    """Native balance and the complete token list, keyless, in two calls."""
    base = chain.api.rstrip("/")
    info = await _get(client, f"{base}/api/v2/addresses/{address}")
    out = [Holding("native", _dec(info.get("coin_balance") or 0, chain.decimals),
                   chain.symbol, chain.name)]
    try:
        rows = await _get(client, f"{base}/api/v2/addresses/{address}/token-balances")
    except ChainUnavailable:
        return out  # native is still a true reading; tokens simply went missing
    for row in rows if isinstance(rows, list) else []:
        token = row.get("token") or {}
        if token.get("type") not in (None, "ERC-20"):
            continue  # NFTs are not balances
        contract = (token.get("address") or "").lower()
        quantity = _dec(row.get("value") or 0, token.get("decimals") or 0)
        if contract and quantity > 0:
            out.append(Holding(contract, quantity, token.get("symbol") or "",
                               token.get("name") or "", contract))
    return out


async def _etherscan(client, chain: Chain, address: str, key: str) -> list[Holding]:
    async def call(**params) -> Any:
        data = await _get(client, ETHERSCAN_V2_URL, params={
            "chainid": chain.chain_id, "address": address, "apikey": key, **params
        })
        if str(data.get("status")) != "1":
            message = str(data.get("result") or data.get("message") or "error")
            if "rate limit" in message.lower() or "max" in message.lower():
                raise ChainUnavailable(f"Etherscan rate limit: {message}")
            if "No transactions found" in message:
                return []
            raise ChainUnavailable(f"Etherscan: {message}")
        return data.get("result")

    native = await call(module="account", action="balance", tag="latest")
    out = [Holding("native", _dec(native or 0, chain.decimals), chain.symbol, chain.name)]

    # The free tier has no "list this address's tokens" call, so the candidate
    # set comes from transfer history and each one is then priced individually.
    transfers = await call(module="account", action="tokentx", page=1, offset=200,
                           sort="desc")
    seen: dict[str, tuple[str, str, int]] = {}
    for tx in transfers if isinstance(transfers, list) else []:
        contract = (tx.get("contractAddress") or "").lower()
        if contract and contract not in seen:
            seen[contract] = (tx.get("tokenSymbol") or "", tx.get("tokenName") or "",
                              int(tx.get("tokenDecimal") or 0))
        if len(seen) >= ETHERSCAN_TOKEN_CAP:
            break
    for contract, (symbol, name, decimals) in seen.items():
        try:
            raw = await call(module="account", action="tokenbalance",
                             contractaddress=contract, tag="latest")
        except ChainUnavailable:
            continue
        quantity = _dec(raw or 0, decimals)
        if quantity > 0:
            out.append(Holding(contract, quantity, symbol, name, contract))
    return out


async def evm(client, chain: Chain, address: str) -> list[Holding]:
    key = os.getenv("ETHERSCAN_API_KEY", "").strip()
    errors: list[str] = []

    # Blockscout first: it is the only free route to a full token list.
    if chain.api:
        try:
            return await _blockscout(client, chain, address)
        except ChainUnavailable as exc:
            errors.append(str(exc))
    if key and chain.chain_id:
        try:
            return await _etherscan(client, chain, address, key)
        except ChainUnavailable as exc:
            errors.append(str(exc))
    if chain.rpc:
        raw = await _rpc(client, chain.rpc, "eth_getBalance", [address, "latest"])
        return [Holding("native", _dec(raw or 0, chain.decimals), chain.symbol,
                        chain.name)]
    if not key and not chain.api:
        raise MissingCredential(
            f"{chain.name} needs ETHERSCAN_API_KEY (no keyless explorer is known "
            "for it)"
        )
    raise ChainUnavailable("; ".join(errors) or f"no route to {chain.name}")


# --- UTXO and Cosmos: one adapter, many chains -----------------------------

async def blockchair(client, chain: Chain, address: str) -> list[Holding]:
    data = await _get(client, f"https://api.blockchair.com/{chain.api}"
                              f"/dashboards/address/{address}")
    record = ((data or {}).get("data") or {}).get(address)
    if record is None:
        raise BadAddress(f"{chain.name} does not know address {address!r}")
    balance = (record.get("address") or {}).get("balance") or 0
    return [Holding("native", _dec(balance, chain.decimals), chain.symbol, chain.name)]


async def cosmos(client, chain: Chain, address: str) -> list[Holding]:
    data = await _get(client, f"{chain.api.rstrip('/')}"
                              f"/cosmos/bank/v1beta1/balances/{address}")
    out: list[Holding] = []
    for row in (data or {}).get("balances") or []:
        denom = row.get("denom") or ""
        if denom == chain.rpc:
            out.append(Holding("native", _dec(row.get("amount") or 0, chain.decimals),
                               chain.symbol, chain.name))
        else:
            # IBC vouchers and CW20s: quantity is real, the denom is untrusted.
            out.append(Holding(denom, _dec(row.get("amount") or 0, 6), denom, denom,
                               denom))
    return out or [Holding("native", Decimal(0), chain.symbol, chain.name)]


# --- one family each -------------------------------------------------------

async def solana(client, chain: Chain, address: str) -> list[Holding]:
    lamports = await _rpc(client, chain.api, "getBalance", [address])
    out = [Holding("native", _dec((lamports or {}).get("value", 0), chain.decimals),
                   chain.symbol, chain.name)]
    accounts = await _rpc(client, chain.api, "getTokenAccountsByOwner", [
        address,
        {"programId": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"},
        {"encoding": "jsonParsed"},
    ])
    for entry in (accounts or {}).get("value") or []:
        info = (((entry.get("account") or {}).get("data") or {}).get("parsed")
                or {}).get("info") or {}
        amount = (info.get("tokenAmount") or {}).get("uiAmountString")
        mint = info.get("mint") or ""
        if mint and amount and Decimal(amount) > 0:
            out.append(Holding(mint, Decimal(amount), "", "", mint))
    return out


async def tron(client, chain: Chain, address: str) -> list[Holding]:
    data = await _get(client, f"{chain.api}/v1/accounts/{address}")
    rows = (data or {}).get("data") or []
    if not rows:
        raise BadAddress(f"TRON does not know address {address!r}")
    account = rows[0]
    out = [Holding("native", _dec(account.get("balance") or 0, chain.decimals),
                   chain.symbol, chain.name)]
    for entry in account.get("trc20") or []:
        for contract, raw in entry.items():
            quantity = _dec(raw, 6)  # decimals are not in this response
            if quantity > 0:
                out.append(Holding(contract, quantity, "", "", contract))
    return out


async def xrpl(client, chain: Chain, address: str) -> list[Holding]:
    data = await _json(client, "POST", chain.api, json={
        "method": "account_info",
        "params": [{"account": address, "ledger_index": "validated"}],
    })
    result = (data or {}).get("result") or {}
    if result.get("error"):
        raise BadAddress(f"XRP Ledger: {result['error']}")
    balance = ((result.get("account_data") or {}).get("Balance")) or 0
    return [Holding("native", _dec(balance, chain.decimals), chain.symbol, chain.name)]


async def ton(client, chain: Chain, address: str) -> list[Holding]:
    data = await _get(client, f"{chain.api}/api/v2/getAddressBalance",
                      params={"address": address})
    if not (data or {}).get("ok"):
        raise BadAddress(f"TON rejected address {address!r}")
    out = [Holding("native", _dec(data.get("result") or 0, chain.decimals),
                   chain.symbol, chain.name)]
    try:
        jettons = await _get(client, f"{chain.api}/api/v3/jetton/wallets",
                             params={"owner_address": address, "limit": 100})
    except ChainUnavailable:
        return out
    for wallet in (jettons or {}).get("jetton_wallets") or []:
        master = wallet.get("jetton") or ""
        quantity = _dec(wallet.get("balance") or 0, 9)
        if master and quantity > 0:
            out.append(Holding(master, quantity, "", "", master))
    return out


async def near(client, chain: Chain, address: str) -> list[Holding]:
    result = await _rpc(client, chain.api, "query", {
        "request_type": "view_account", "finality": "final", "account_id": address,
    })
    return [Holding("native", _dec((result or {}).get("amount") or 0, chain.decimals),
                    chain.symbol, chain.name)]


async def aptos(client, chain: Chain, address: str) -> list[Holding]:
    rows = await _get(client, f"{chain.api}/v1/accounts/{address}/resources")
    out: list[Holding] = []
    for row in rows if isinstance(rows, list) else []:
        kind = row.get("type") or ""
        if not kind.startswith("0x1::coin::CoinStore<"):
            continue
        coin = kind[len("0x1::coin::CoinStore<"):-1]
        raw = ((row.get("data") or {}).get("coin") or {}).get("value") or 0
        native = coin == "0x1::aptos_coin::AptosCoin"
        quantity = _dec(raw, chain.decimals if native else 8)
        if native:
            out.insert(0, Holding("native", quantity, chain.symbol, chain.name))
        elif quantity > 0:
            out.append(Holding(coin, quantity, "", "", coin))
    return out or [Holding("native", Decimal(0), chain.symbol, chain.name)]


async def sui(client, chain: Chain, address: str) -> list[Holding]:
    rows = await _rpc(client, chain.api, "suix_getAllBalances", [address])
    out: list[Holding] = []
    for row in rows or []:
        coin = row.get("coinType") or ""
        native = coin.endswith("::sui::SUI")
        quantity = _dec(row.get("totalBalance") or 0, chain.decimals if native else 9)
        if native:
            out.insert(0, Holding("native", quantity, chain.symbol, chain.name))
        elif quantity > 0:
            out.append(Holding(coin, quantity, "", "", coin))
    return out or [Holding("native", Decimal(0), chain.symbol, chain.name)]


async def stellar(client, chain: Chain, address: str) -> list[Holding]:
    data = await _get(client, f"{chain.api}/accounts/{address}")
    out: list[Holding] = []
    for row in (data or {}).get("balances") or []:
        quantity = Decimal(str(row.get("balance") or 0))
        if row.get("asset_type") == "native":
            out.insert(0, Holding("native", quantity, chain.symbol, chain.name))
        elif quantity > 0:
            code = row.get("asset_code") or ""
            issuer = row.get("asset_issuer") or ""
            out.append(Holding(f"{code}:{issuer}", quantity, code, code, issuer))
    return out or [Holding("native", Decimal(0), chain.symbol, chain.name)]


async def algorand(client, chain: Chain, address: str) -> list[Holding]:
    data = await _get(client, f"{chain.api}/v2/accounts/{address}")
    account = (data or {}).get("account") or {}
    out = [Holding("native", _dec(account.get("amount") or 0, chain.decimals),
                   chain.symbol, chain.name)]
    for asset in account.get("assets") or []:
        asset_id = str(asset.get("asset-id") or "")
        quantity = _dec(asset.get("amount") or 0, 6)
        if asset_id and quantity > 0:
            out.append(Holding(asset_id, quantity, "", "", asset_id))
    return out


async def tezos(client, chain: Chain, address: str) -> list[Holding]:
    data = await _get(client, f"{chain.api}/v1/accounts/{address}")
    out = [Holding("native", _dec((data or {}).get("balance") or 0, chain.decimals),
                   chain.symbol, chain.name)]
    try:
        rows = await _get(client, f"{chain.api}/v1/tokens/balances",
                          params={"account": address, "limit": 100})
    except ChainUnavailable:
        return out
    for row in rows if isinstance(rows, list) else []:
        token = row.get("token") or {}
        meta = token.get("metadata") or {}
        contract = ((token.get("contract") or {}).get("address")) or ""
        quantity = _dec(row.get("balance") or 0, int(meta.get("decimals") or 0))
        if contract and quantity > 0:
            out.append(Holding(contract, quantity, meta.get("symbol") or "",
                               meta.get("name") or "", contract))
    return out


async def hedera(client, chain: Chain, address: str) -> list[Holding]:
    data = await _get(client, f"{chain.api}/api/v1/accounts/{address}")
    balance = (data or {}).get("balance") or {}
    out = [Holding("native", _dec(balance.get("balance") or 0, chain.decimals),
                   chain.symbol, chain.name)]
    for token in balance.get("tokens") or []:
        token_id = token.get("token_id") or ""
        quantity = _dec(token.get("balance") or 0, 8)
        if token_id and quantity > 0:
            out.append(Holding(token_id, quantity, "", "", token_id))
    return out


async def stacks(client, chain: Chain, address: str) -> list[Holding]:
    data = await _get(client, f"{chain.api}/extended/v1/address/{address}/balances")
    stx = ((data or {}).get("stx") or {}).get("balance") or 0
    out = [Holding("native", _dec(stx, chain.decimals), chain.symbol, chain.name)]
    for key, row in ((data or {}).get("fungible_tokens") or {}).items():
        quantity = _dec(row.get("balance") or 0, 6)
        if quantity > 0:
            out.append(Holding(key, quantity, "", "", key))
    return out


async def multiversx(client, chain: Chain, address: str) -> list[Holding]:
    data = await _get(client, f"{chain.api}/accounts/{address}")
    out = [Holding("native", _dec((data or {}).get("balance") or 0, chain.decimals),
                   chain.symbol, chain.name)]
    try:
        rows = await _get(client, f"{chain.api}/accounts/{address}/tokens",
                          params={"size": 100})
    except ChainUnavailable:
        return out
    for row in rows if isinstance(rows, list) else []:
        ident = row.get("identifier") or ""
        quantity = _dec(row.get("balance") or 0, int(row.get("decimals") or 0))
        if ident and quantity > 0:
            out.append(Holding(ident, quantity, row.get("ticker") or "",
                               row.get("name") or "", ident))
    return out


async def kaspa(client, chain: Chain, address: str) -> list[Holding]:
    data = await _get(client, f"{chain.api}/addresses/{address}/balance")
    return [Holding("native", _dec((data or {}).get("balance") or 0, chain.decimals),
                    chain.symbol, chain.name)]


async def arweave(client, chain: Chain, address: str) -> list[Holding]:
    resp = await _json(client, "GET", f"{chain.api}/wallet/{address}/balance")
    return [Holding("native", _dec(resp, chain.decimals), chain.symbol, chain.name)]


async def filecoin(client, chain: Chain, address: str) -> list[Holding]:
    data = await _get(client, f"{chain.api}/address/{address}")
    return [Holding("native", _dec((data or {}).get("balance") or 0, chain.decimals),
                    chain.symbol, chain.name)]


async def vechain(client, chain: Chain, address: str) -> list[Holding]:
    data = await _get(client, f"{chain.api}/accounts/{address}")
    return [
        Holding("native", _dec((data or {}).get("balance") or 0, 18), "VET", "VeChain"),
        Holding("vtho", _dec((data or {}).get("energy") or 0, 18), "VTHO", "VeThor"),
    ]


# Starknet has no native token account: ETH is an ordinary ERC-20 at a fixed
# address, read through a contract call rather than a balance endpoint.
STARKNET_ETH = ("0x049d36570d4e46f48e99674bd3fcc84644ddd6b96f7c741b1562b82f9e004dc7")


async def starknet(client, chain: Chain, address: str) -> list[Holding]:
    result = await _rpc(client, chain.api, "starknet_call", [
        {"contract_address": STARKNET_ETH,
         "entry_point_selector": "0x2e4263afad30923c891518314c3c95dbe830a16874e8abc5"
                                 "777a9a20b54c76e",
         "calldata": [address]},
        "latest",
    ])
    low, high = (result or ["0x0", "0x0"])[:2]
    raw = int(low, 16) + (int(high, 16) << 128)
    return [Holding("native", _dec(raw, chain.decimals), chain.symbol, chain.name)]


async def cardano(client, chain: Chain, address: str) -> list[Holding]:
    key = os.getenv("BLOCKFROST_PROJECT_ID", "").strip()
    if not key:
        raise MissingCredential("Cardano needs BLOCKFROST_PROJECT_ID")
    data = await _get(client, f"{chain.api}/addresses/{address}",
                      headers={"project_id": key})
    out: list[Holding] = []
    for row in (data or {}).get("amount") or []:
        unit = row.get("unit") or ""
        if unit == "lovelace":
            out.insert(0, Holding("native", _dec(row.get("quantity") or 0,
                                                 chain.decimals),
                                  chain.symbol, chain.name))
        else:
            out.append(Holding(unit, _dec(row.get("quantity") or 0, 0), "", "", unit))
    return out or [Holding("native", Decimal(0), chain.symbol, chain.name)]


async def subscan(client, chain: Chain, address: str) -> list[Holding]:
    key = os.getenv("SUBSCAN_API_KEY", "").strip()
    if not key:
        raise MissingCredential(f"{chain.name} needs SUBSCAN_API_KEY")
    data = await _json(client, "POST", f"{chain.api}/api/v2/scan/search",
                       json={"key": address}, headers={"X-API-Key": key})
    account = ((data or {}).get("data") or {}).get("account") or {}
    if not account:
        raise BadAddress(f"{chain.name} does not know address {address!r}")
    # Subscan already applies the chain's decimals here.
    return [Holding("native", Decimal(str(account.get("balance") or 0)),
                    chain.symbol, chain.name)]


ADAPTERS = {
    ETHERSCAN_V2: evm, BLOCKSCOUT: evm, EVM_RPC: evm,
    "blockchair": blockchair, "cosmos": cosmos, "solana": solana, "tron": tron,
    "xrpl": xrpl, "ton": ton, "near": near, "aptos": aptos, "sui": sui,
    "stellar": stellar, "algorand": algorand, "tezos": tezos, "hedera": hedera,
    "stacks": stacks, "multiversx": multiversx, "kaspa": kaspa, "arweave": arweave,
    "filecoin": filecoin, "vechain": vechain, "starknet": starknet,
    "cardano": cardano, "subscan": subscan,
}


async def fetch(client: httpx.AsyncClient, chain: Chain, address: str) -> list[Holding]:
    """Read every holding at `address` on `chain`."""
    adapter = ADAPTERS.get(chain.via)
    if adapter is None:
        raise ChainUnavailable(f"no adapter for {chain.name}")
    if chain.key_env and not os.getenv(chain.key_env, "").strip():
        raise MissingCredential(f"{chain.name} needs {chain.key_env}")
    return await adapter(client, chain, address)
