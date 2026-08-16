"""Cloud relay lookup, for cameras not reachable on the LAN.

The vendor runs a dispatch service that maps a device id to a relay server the
camera is already connected to. This is the one part of the plugin that talks
to the internet, and it hands a device id to a third party, so it is only used
when a camera is explicitly configured for cloud mode — never as a fallback for
a LAN camera that happens to be offline.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time

import httpx

log = logging.getLogger("blackice.plugin.v380.relay")

DISPATCH_URL = "http://dispa1.av380.net:8001/api/v1/get_stream_server"
#: 10001 is a normal camera; 20001 identifies a panoramic device.
PLATFORM_CAMERA = 10001
#: Salt appended before hashing. Part of the vendor's signature scheme.
_SIGN_SALT = "hsdata2022"
_OK_CODE = 2000

REQUEST_TIMEOUT = 10.0
REACHABILITY_TIMEOUT = 3.0


def sign(device_id: int, platform: int, timestamp: int) -> str:
    base = f"dev_id={device_id}&platform={platform}&timestamp={timestamp}{_SIGN_SALT}"
    return hashlib.sha1(base.encode("utf-8")).hexdigest()


async def _reachable(ip: str, port: int) -> bool:
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(ip, port), REACHABILITY_TIMEOUT
        )
    except (TimeoutError, OSError):
        return False
    writer.close()
    return True


async def find_relay(device_id: int, *, port: int = 8800) -> str | None:
    """The IP of a reachable relay carrying this device, or None.

    The dispatcher returns several candidates and not all of them accept
    connections, so each is probed before being offered to the caller.
    """
    timestamp = int(time.time())
    payload = {
        "dev_id": device_id,
        "platform": PLATFORM_CAMERA,
        "timestamp": timestamp,
        "sign": sign(device_id, PLATFORM_CAMERA, timestamp),
    }

    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as http:
            response = await http.post(DISPATCH_URL, json=payload)
            response.raise_for_status()
            body = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("relay lookup failed for %s: %s", device_id, exc)
        return None

    if body.get("code") != _OK_CODE:
        log.warning("relay lookup rejected for %s: code %s", device_id, body.get("code"))
        return None

    for server in body.get("data") or []:
        ip = server.get("ip")
        if ip and await _reachable(ip, port):
            return ip

    log.warning("no reachable relay server for %s", device_id)
    return None
