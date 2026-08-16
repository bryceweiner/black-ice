"""USD prices, from CoinGecko's free tier.

Two things keep this inside ~30 requests/minute: quotes are batched into one
call for every native coin plus one call per token platform, and answers are
cached briefly, so a poll that sweeps twenty addresses on the same chain prices
them together.

Failure here is never fatal. An unpriced holding keeps its quantity and loses
its value, which is exactly what the quantity-based drain rule needs anyway.
"""

from __future__ import annotations

import contextlib
import os
import time
from typing import Any

import httpx

from . import chains

BASE = "https://api.coingecko.com/api/v3"
CACHE_SECONDS = 60.0
# CoinGecko truncates very long query strings; batch contracts well inside it.
CONTRACTS_PER_CALL = 100
TIMEOUT = 15.0

COIN = "coin"  # price-key namespace for native assets


def key_for_native(cg_native: str | None) -> str | None:
    return f"{COIN}:{cg_native}" if cg_native else None


def key_for_token(cg_platform: str | None, contract: str | None) -> str | None:
    if not cg_platform or not contract:
        return None
    return f"{cg_platform}:{contract.lower()}"


def _headers() -> dict[str, str]:
    key = os.getenv("COINGECKO_API_KEY", "").strip()
    return {"x-cg-demo-api-key": key} if key else {}


class PriceBook:
    """Batched, briefly-cached USD quotes keyed by `coin:<id>` / `<platform>:<contract>`."""

    def __init__(self) -> None:
        self._cache: dict[str, tuple[float, float]] = {}  # key -> (usd, fetched_at)

    def _fresh(self, key: str, now: float) -> float | None:
        hit = self._cache.get(key)
        if hit and now - hit[1] < CACHE_SECONDS:
            return hit[0]
        return None

    def _store(self, key: str, usd: Any, now: float) -> None:
        # A null price is CoinGecko saying it does not know this asset.
        with contextlib.suppress(TypeError, ValueError):
            self._cache[key] = (float(usd), now)

    async def quote(self, client: httpx.AsyncClient, keys: set[str]) -> dict[str, float]:
        """Resolve as many keys as possible. Missing keys are simply absent."""
        now = time.monotonic()
        out = {k: v for k in keys if (v := self._fresh(k, now)) is not None}
        wanted = keys - out.keys()
        if not wanted:
            return out

        coins = sorted(k.split(":", 1)[1] for k in wanted if k.startswith(f"{COIN}:"))
        platforms: dict[str, list[str]] = {}
        for key in wanted:
            platform, _, contract = key.partition(":")
            if platform != COIN:
                platforms.setdefault(platform, []).append(contract)

        if coins:
            data = await self._get(client, f"{BASE}/simple/price", {
                "ids": ",".join(coins), "vs_currencies": "usd",
            })
            for coin_id, row in (data or {}).items():
                self._store(f"{COIN}:{coin_id}", (row or {}).get("usd"), now)

        for platform, contracts in platforms.items():
            for start in range(0, len(contracts), CONTRACTS_PER_CALL):
                batch = contracts[start:start + CONTRACTS_PER_CALL]
                data = await self._get(client, f"{BASE}/simple/token_price/{platform}", {
                    "contract_addresses": ",".join(batch), "vs_currencies": "usd",
                })
                for contract, row in (data or {}).items():
                    self._store(f"{platform}:{contract.lower()}",
                                (row or {}).get("usd"), now)

        out.update({k: v for k in wanted if (v := self._fresh(k, now)) is not None})
        return out

    async def _get(self, client: httpx.AsyncClient, url: str,
                   params: dict[str, str]) -> Any:
        """A price we cannot get is a missing value, never an error."""
        try:
            resp = await client.get(url, params=params, headers=_headers(),
                                    timeout=TIMEOUT)
            if resp.status_code == 429:
                return None  # rate limited: this poll goes unpriced
            resp.raise_for_status()
            return resp.json()
        except (httpx.HTTPError, ValueError):
            return None


async def reconcile(client: httpx.AsyncClient) -> int:
    """Correct the registry's CoinGecko ids from CoinGecko itself.

    `/asset_platforms` keys each platform by EVM chain id and names its native
    coin, so every EVM entry's guessed ids can be replaced with the real ones.
    Returns how many chains were corrected; zero if the call did not land.
    """
    try:
        resp = await client.get(f"{BASE}/asset_platforms", headers=_headers(),
                                timeout=TIMEOUT)
        resp.raise_for_status()
        rows = resp.json()
    except (httpx.HTTPError, ValueError):
        return 0

    fixed = 0
    for row in rows if isinstance(rows, list) else []:
        chain_id = row.get("chain_identifier")
        if not isinstance(chain_id, int):
            continue
        chain = chains.by_chain_id(chain_id)
        if chain is None:
            continue
        platform, native = row.get("id"), row.get("native_coin_id")
        if (platform, native) != (chain.cg_platform, chain.cg_native):
            chains.apply_price_ids(chain.slug, platform, native)
            fixed += 1
    return fixed
