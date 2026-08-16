"""Live packet capture, when the process happens to be privileged.

Everything else in this plugin works from what the OS will tell an unprivileged
user. Capture is the one place that genuinely cannot: rogue DHCP offers and a
sweep across other people's addresses are not traffic addressed to us, so
nothing short of a promiscuous socket sees them.

So it is strictly an upgrade. Absent scapy, absent root, or on a switched
network with no mirror port, the plugin loses these three detections and keeps
everything else. `mode()` says which of those is the case rather than leaving
the dashboard to imply the network is quiet.
"""

from __future__ import annotations

import contextlib
from typing import Any

from . import net

BPF = "arp or (udp and (port 67 or port 68)) or (tcp[tcpflags] & (tcp-syn|tcp-ack) == tcp-syn)"
# A sniffer that never forgets is a memory leak with a security theme.
MAX_TRACKED_SOURCES = 512
MAX_TARGETS_PER_SOURCE = 256


def scapy_available() -> bool:
    try:
        import scapy.all  # noqa: F401
    except Exception:
        return False
    return True


class Capture:
    """Accumulates three narrow facts, and forgets them once drained."""

    def __init__(self) -> None:
        self.sniffer: Any = None
        self.dhcp_servers: dict[str, str] = {}     # server ip -> its MAC
        self.arp_claims: set[tuple[str, str]] = set()
        self.syn_targets: dict[str, set[str]] = {}  # source ip -> hosts it SYNed
        self.packets = 0
        self.reason = "not started"

    # --- availability -------------------------------------------------------

    def mode(self) -> str:
        if self.sniffer is not None:
            return "live"
        return self.reason

    def _why_not(self) -> str:
        if not net.is_root():
            return "unavailable: needs root"
        if not scapy_available():
            return "unavailable: scapy not installed"
        return ""

    # --- lifecycle ----------------------------------------------------------

    def start(self, interfaces: list[str] | None = None) -> str:
        """Begin sniffing, or record why we cannot. Never raises."""
        blocked = self._why_not()
        if blocked:
            self.reason = blocked
            return self.reason
        try:
            from scapy.all import AsyncSniffer

            self.sniffer = AsyncSniffer(
                filter=BPF, prn=self._observe, store=False,
                iface=interfaces or None,
            )
            self.sniffer.start()
        except Exception as exc:
            self.sniffer = None
            self.reason = f"unavailable: {type(exc).__name__}: {exc}"[:160]
            return self.reason
        self.reason = "live"
        return self.reason

    def stop(self) -> None:
        """Idempotent, and called on failure paths."""
        sniffer, self.sniffer = self.sniffer, None
        if sniffer is None:
            return
        with contextlib.suppress(Exception):
            sniffer.stop()
        self.reason = "stopped"

    # --- observation ---------------------------------------------------------

    def _observe(self, packet: Any) -> None:
        """Called on the sniffer's own thread. Must never raise into scapy."""
        try:
            self.packets += 1
            self._dhcp(packet)
            self._arp(packet)
            self._syn(packet)
        except Exception:
            return

    def _dhcp(self, packet: Any) -> None:
        from scapy.layers.dhcp import DHCP
        from scapy.layers.inet import IP, UDP

        if not (packet.haslayer(DHCP) and packet.haslayer(UDP)):
            return
        if packet[UDP].sport != 67:
            return  # only a server speaks from 67
        message = {opt[1] for opt in packet[DHCP].options
                   if isinstance(opt, tuple) and opt[0] == "message-type"}
        if not message & {2, 5}:  # OFFER, ACK
            return
        server = packet[IP].src if packet.haslayer(IP) else ""
        if server and len(self.dhcp_servers) < MAX_TRACKED_SOURCES:
            self.dhcp_servers[server] = getattr(packet, "src", "")

    def _arp(self, packet: Any) -> None:
        from scapy.layers.l2 import ARP

        if not packet.haslayer(ARP):
            return
        arp = packet[ARP]
        if arp.op != 2:  # replies only; a request claims nothing
            return
        if len(self.arp_claims) < MAX_TRACKED_SOURCES * 4:
            self.arp_claims.add((str(arp.psrc), str(arp.hwsrc).lower()))

    def _syn(self, packet: Any) -> None:
        from scapy.layers.inet import IP, TCP

        if not (packet.haslayer(TCP) and packet.haslayer(IP)):
            return
        source, target = packet[IP].src, packet[IP].dst
        seen = self.syn_targets.setdefault(source, set())
        if len(self.syn_targets) > MAX_TRACKED_SOURCES:
            self.syn_targets.pop(next(iter(self.syn_targets)), None)
        if len(seen) < MAX_TARGETS_PER_SOURCE:
            seen.add(target)

    # --- draining -------------------------------------------------------------

    def drain(self) -> dict[str, Any]:
        """Hand over everything seen since the last drain, and reset."""
        out = {
            "dhcp_servers": dict(self.dhcp_servers),
            "arp_claims": [(ip, mac) for ip, mac in sorted(self.arp_claims)],
            "syn_targets": {src: set(targets) for src, targets in self.syn_targets.items()},
            "packets": self.packets,
        }
        self.dhcp_servers.clear()
        self.arp_claims.clear()
        self.syn_targets.clear()
        self.packets = 0
        return out
