"""Finding V380 cameras on the LAN.

The cameras answer a UDP broadcast with a caret-delimited record. This is the
only part of the system that works without credentials, so it is also how the
plugin knows a camera exists before anyone has configured a password for it.

Two details worth knowing:

* the reply arrives on port 10009, which we must be *bound* to — the camera
  does not reply to the sender's ephemeral port;
* the probe is sent several times, because a single UDP broadcast is routinely
  lost and a camera that misses it is invisible for the whole scan.
"""

from __future__ import annotations

import asyncio
import logging
import socket
from dataclasses import dataclass

log = logging.getLogger("blackice.plugin.v380.discovery")

DISCOVERY_PROBE = b"NVDEVSEARCH^100"
DISCOVERY_SEND_PORT = 10008
DISCOVERY_LISTEN_PORT = 10009
DISCOVERY_REPLY_PREFIX = "NVDEVRESULT"

#: Field positions in the caret-delimited reply.
_FIELD_MAC = 2
_FIELD_IP = 3
_FIELD_DEVICE_ID = 12
_MIN_FIELDS = 13

DEFAULT_ATTEMPTS = 5
DEFAULT_WAIT = 0.25


@dataclass(frozen=True, slots=True)
class DiscoveredCamera:
    device_id: str
    ip: str
    mac: str


def parse_reply(data: bytes) -> DiscoveredCamera | None:
    """Parse one NVDEVRESULT record, or None if this is not one.

    Everything in here is attacker-supplied — anything on the LAN can send a
    well-formed reply — so callers must treat the fields as untrusted.
    """
    try:
        text = data.decode("ascii", "replace")
    except (UnicodeDecodeError, AttributeError):
        return None

    parts = text.split("^")
    if len(parts) < _MIN_FIELDS or parts[0] != DISCOVERY_REPLY_PREFIX:
        return None

    device_id = parts[_FIELD_DEVICE_ID].strip()
    ip = parts[_FIELD_IP].strip()
    if not device_id or not ip:
        return None
    return DiscoveredCamera(device_id=device_id, ip=ip, mac=parts[_FIELD_MAC].strip())


class _DiscoveryProtocol(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self.found: dict[str, DiscoveredCamera] = {}

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        camera = parse_reply(data)
        if camera is None:
            return
        # Keyed by MAC: a camera that answers every probe would otherwise
        # appear five times, and its IP can differ between replies on a
        # multi-homed host.
        self.found.setdefault(camera.mac or camera.device_id, camera)

    def error_received(self, exc: Exception) -> None:
        log.debug("discovery socket error: %s", exc)


def _bind_socket() -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    # Two Black Ice processes (serve and voice) may both hold this port.
    if hasattr(socket, "SO_REUSEPORT"):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    sock.bind(("0.0.0.0", DISCOVERY_LISTEN_PORT))
    sock.setblocking(False)
    return sock


async def discover(
    *,
    attempts: int = DEFAULT_ATTEMPTS,
    wait: float = DEFAULT_WAIT,
    broadcast: str = "255.255.255.255",
) -> list[DiscoveredCamera]:
    """Broadcast for cameras and collect replies for `attempts * wait` seconds.

    Raises OSError if the listen port cannot be bound — that is a real fault
    worth surfacing (something else is using it), not an empty result.
    """
    loop = asyncio.get_running_loop()
    sock = _bind_socket()
    transport, protocol = await loop.create_datagram_endpoint(
        _DiscoveryProtocol, sock=sock
    )
    try:
        for _ in range(attempts):
            transport.sendto(DISCOVERY_PROBE, (broadcast, DISCOVERY_SEND_PORT))
            await asyncio.sleep(wait)
    finally:
        transport.close()

    cameras = sorted(protocol.found.values(), key=lambda c: c.device_id)
    log.debug("discovery found %d camera(s)", len(cameras))
    return cameras
