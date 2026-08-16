"""Threat-intelligence feeds.

Plain-text IP lists, refreshed on a timer and matched against the external
addresses this machine talks to. A feed that will not load is recorded against
that feed and skipped: an abuse.ch outage is not a fault in the sensor, and
should not cost you the other two lists.

The feeds are third-party data. A label from one of them is untrusted text, and
a hit is evidence rather than a verdict -- shared hosting and CDN addresses turn
up on aggregate blocklists all the time.
"""

from __future__ import annotations

import ipaddress
import re

import httpx

from .store import Store

USER_AGENT = "black-ice-netguard/0.1"
FETCH_TIMEOUT = 20.0
MAX_ENTRIES = 200_000  # a feed larger than this is a mistake, not intelligence

IP_LINE = re.compile(r"^\s*(\d{1,3}(?:\.\d{1,3}){3})")


def parse_feed(text: str) -> list[str]:
    """One IPv4 address per line, comments and columns tolerated."""
    out: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", ";", "//")):
            continue
        match = IP_LINE.match(line)
        if not match:
            continue
        try:
            addr = ipaddress.IPv4Address(match.group(1))
        except ValueError:
            continue
        # A feed listing private space is broken or hostile; either way it would
        # light up every device in the house.
        if addr.is_private or addr.is_loopback or addr.is_reserved:
            continue
        out.append(str(addr))
        if len(out) >= MAX_ENTRIES:
            break
    return out


async def refresh(store: Store, feeds: tuple[tuple[str, str], ...]) -> dict[str, int | str]:
    """Pull every feed into the local table. Never raises."""
    summary: dict[str, int | str] = {}
    if not feeds:
        return summary
    async with httpx.AsyncClient(
        timeout=FETCH_TIMEOUT, follow_redirects=True,
        headers={"User-Agent": USER_AGENT},
    ) as client:
        for name, url in feeds:
            try:
                response = await client.get(url)
                response.raise_for_status()
            except Exception as exc:
                await store.replace_feed(name, url, [], error=f"{type(exc).__name__}: {exc}"[:200])
                summary[name] = "unavailable"
                continue
            ips = parse_feed(response.text)
            if not ips:
                await store.replace_feed(name, url, [], error="no addresses in response")
                summary[name] = "empty"
                continue
            summary[name] = await store.replace_feed(name, url, ips)
    return summary
