"""Turning observations into findings.

Pure functions over already-collected data: no I/O, no database, no clock. That
is deliberate -- detection logic is the part most worth testing, and it is only
testable if you can hand it a situation instead of having to produce one.

Each function returns `Finding`s. A finding is a claim the plugin is willing to
make in its own voice; the evidence behind it, which came off the wire, travels
separately in `sensor_text` so that triage keeps treating it as hostile input.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field
from typing import Any

from blackice.models import (
    SEVERITY_CRITICAL,
    SEVERITY_HIGH,
    SEVERITY_MEDIUM,
)

# One remote address holding this many half-open connections to distinct local
# ports is not a client that lost its connection. It is a port scan.
PORTSCAN_PORTS = 5
# Distinct *listening* ports one remote touched across the whole window. Lower
# than the burst threshold because it only ever counts services we actually
# run: six of them from one address, over an hour, is not a client.
PORTSCAN_WINDOW_PORTS = 6
# Beaconing: this regular, over this many samples, is a clock and not a person.
BEACON_MAX_JITTER = 0.12
BEACON_MIN_SAMPLES = 10


@dataclass
class Finding:
    kind: str
    severity: int
    target: str
    summary: str
    sensor_text: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    # Suppression window: how long before the same claim about the same target
    # is worth making again.
    quiet_minutes: float = 60.0


# --- ARP ---------------------------------------------------------------------

def arp_findings(pairs: list[tuple[str, str]], gateway: str = "") -> list[Finding]:
    """Spot a machine answering ARP for addresses that are not its own.

    The attack is old and still the most effective thing on a flat home
    network: claim the gateway's address, and every device's traffic comes to
    you first. It shows up in the ARP cache as one MAC bound to several IPs.

    A router that is also the DNS server and the NAS is *not* this -- that is
    several services on one address. What matters is one hardware address
    claiming several network addresses at once.
    """
    by_mac: dict[str, set[str]] = {}
    by_ip: dict[str, set[str]] = {}
    for ip, mac in pairs:
        by_mac.setdefault(mac, set()).add(ip)
        by_ip.setdefault(ip, set()).add(mac)

    findings: list[Finding] = []
    for mac, ips in by_mac.items():
        if len(ips) < 2:
            continue
        claims_gateway = bool(gateway) and gateway in ips
        # Two addresses on one NIC is ordinary (a static lease plus a DHCP one,
        # or a host with an alias). Three is not, and any pairing that includes
        # the gateway is not.
        if len(ips) < 3 and not claims_gateway:
            continue
        findings.append(Finding(
            kind="arp_spoof",
            severity=SEVERITY_CRITICAL if claims_gateway else SEVERITY_HIGH,
            target=mac,
            summary=(
                f"One device is answering for {len(ips)} addresses"
                + (" including the gateway" if claims_gateway else "")
                + " — this is what ARP spoofing looks like"
            ),
            sensor_text=f"MAC {mac} claims: {', '.join(sorted(ips))}",
            payload={"mac": mac, "addresses": sorted(ips), "gateway": gateway,
                     "claims_gateway": claims_gateway},
            quiet_minutes=30.0,
        ))

    for ip, macs in by_ip.items():
        if len(macs) > 1:
            findings.append(Finding(
                kind="arp_spoof",
                severity=SEVERITY_CRITICAL if ip == gateway else SEVERITY_HIGH,
                target=ip,
                summary=f"Two devices are both claiming {ip}",
                sensor_text=f"{ip} answered by: {', '.join(sorted(macs))}",
                payload={"ip": ip, "macs": sorted(macs), "is_gateway": ip == gateway},
                quiet_minutes=30.0,
            ))
    return findings


# --- scanning ----------------------------------------------------------------

def portscan_findings(
    connections: list[tuple[str, int, str]],
    window_ports: dict[str, set[int]] | None = None,
    listening: set[int] | None = None,
) -> list[Finding]:
    """Someone probing this machine.

    `connections` is (remote_ip, local_port, state) as the socket table has it.
    A burst of SYN_RCVD across distinct ports is a scan caught mid-flight;
    `window_ports` carries the same measure accumulated over a longer window,
    which catches the slow scans that never look like a burst.

    `listening` is the set of ports this machine actually accepts on, and
    filtering by it is not an optimisation. The socket table does not record
    who opened a connection, and an *outbound* one puts a fresh ephemeral port
    in the local column every time -- so a busy API client racks up dozens of
    distinct local ports against one remote address and looks exactly like a
    slow scan. Counting only our own listening ports removes the whole class of
    false positive, at the cost of not seeing probes of closed ports, which
    leave no socket behind to observe anyway.
    """
    def ours(port: int) -> bool:
        return listening is None or port in listening

    half_open: dict[str, set[int]] = {}
    for remote_ip, local_port, state in connections:
        if state in {"SYN_RCVD", "SYN_RECV"} and ours(local_port):
            half_open.setdefault(remote_ip, set()).add(local_port)

    findings: list[Finding] = []
    seen: set[str] = set()
    for remote_ip, ports in half_open.items():
        if len(ports) >= PORTSCAN_PORTS:
            seen.add(remote_ip)
            findings.append(Finding(
                kind="port_scan",
                severity=SEVERITY_HIGH,
                target=remote_ip,
                summary=f"{remote_ip} is probing {len(ports)} ports on this machine",
                payload={"remote_ip": remote_ip, "ports": sorted(ports), "phase": "in_flight"},
                quiet_minutes=15.0,
            ))

    for remote_ip, raw in (window_ports or {}).items():
        ports = {port for port in raw if ours(port)}
        if remote_ip in seen or len(ports) < PORTSCAN_WINDOW_PORTS:
            continue
        findings.append(Finding(
            kind="port_scan",
            severity=SEVERITY_HIGH,
            target=remote_ip,
            summary=(f"{remote_ip} has touched {len(ports)} different ports "
                     "on this machine — a slow port scan"),
            payload={"remote_ip": remote_ip, "ports": sorted(ports)[:60], "phase": "windowed"},
            quiet_minutes=180.0,
        ))
    return findings


def sweep_findings(probes: dict[str, set[str]], threshold: int = 12) -> list[Finding]:
    """One source touching many different hosts: a sweep across the subnet.

    Only capture and a real IDS can see this, because it is traffic addressed
    to other people's machines.
    """
    findings: list[Finding] = []
    for source, targets in probes.items():
        if len(targets) >= threshold:
            findings.append(Finding(
                kind="host_sweep",
                severity=SEVERITY_HIGH,
                target=source,
                summary=f"{source} is sweeping the network — {len(targets)} hosts probed",
                payload={"source": source, "hosts": sorted(targets)[:60],
                         "count": len(targets)},
                quiet_minutes=30.0,
            ))
    return findings


# --- beaconing ----------------------------------------------------------------

def beacon_findings(candidates: list[dict[str, Any]]) -> list[Finding]:
    """Connections that keep time.

    Deliberately conservative, and shipped disarmed, because software that
    polls on a fixed interval is extremely common and entirely innocent. The
    finding says "worth a look", not "you are compromised".
    """
    findings: list[Finding] = []
    for row in candidates:
        if row["jitter"] > BEACON_MAX_JITTER or row["samples"] < BEACON_MIN_SAMPLES:
            continue
        interval = row["interval_seconds"]
        pretty = f"{interval / 60:.1f} minutes" if interval >= 90 else f"{interval:.0f} seconds"
        findings.append(Finding(
            kind="beacon_suspected",
            severity=SEVERITY_MEDIUM,
            target=row["remote_ip"],
            summary=(f"Something here contacts {row['remote_ip']} every {pretty}, "
                     f"almost to the second ({row['samples']} times)"),
            sensor_text=(f"process {row['process']}, port {row['port']}"
                         if row.get("process") else None),
            payload=dict(row),
            quiet_minutes=720.0,
        ))
    return findings


# --- threat intelligence -------------------------------------------------------

def intel_findings(hits: list[dict[str, Any]]) -> list[Finding]:
    """Outbound traffic to an address a public feed considers hostile."""
    findings: list[Finding] = []
    for hit in hits:
        sources = hit.get("sources") or []
        findings.append(Finding(
            kind="threat_hit",
            severity=SEVERITY_HIGH,
            target=hit["ip"],
            summary=(f"This machine is talking to {hit['ip']}, which appears on "
                     f"{len(sources)} threat feed{'s' if len(sources) != 1 else ''}"),
            sensor_text=f"listed by: {', '.join(sources)}"
                        + (f"; process {hit['process']}" if hit.get("process") else ""),
            payload={"ip": hit["ip"], "sources": sources, "port": hit.get("port", 0),
                     "process": hit.get("process", ""), "samples": hit.get("samples", 0)},
            quiet_minutes=360.0,
        ))
    return findings


# --- DHCP -----------------------------------------------------------------------

def dhcp_findings(servers: dict[str, str], gateway: str = "") -> list[Finding]:
    """More than one machine handing out leases, or one that is not the router.

    A second DHCP server takes over the network's DNS and default route for
    every device that renews after it, which is the quietest possible way to
    own a household.
    """
    if not servers:
        return []
    unexpected = {ip: mac for ip, mac in servers.items() if gateway and ip != gateway}
    if len(servers) < 2 and not unexpected:
        return []
    return [Finding(
        kind="rogue_dhcp",
        severity=SEVERITY_CRITICAL,
        target=next(iter(sorted(unexpected or servers))),
        summary=(f"{len(servers)} DHCP servers are answering on this network"
                 if len(servers) > 1 else
                 "A DHCP server that is not the router is answering on this network"),
        sensor_text="offers seen from: "
                    + ", ".join(f"{ip} ({mac})" for ip, mac in sorted(servers.items())),
        payload={"servers": sorted(servers), "gateway": gateway,
                 "unexpected": sorted(unexpected)},
        quiet_minutes=60.0,
    )]


# --- inventory ---------------------------------------------------------------

def is_lan(ip: str) -> bool:
    try:
        return ipaddress.ip_address(ip).is_private
    except ValueError:
        return False
