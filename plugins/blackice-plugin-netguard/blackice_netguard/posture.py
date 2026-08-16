"""The hardening audit: how defensible this machine and this network are.

Three scopes -- the host, the LAN's attack surface, and the gateway -- each a
list of weighted checks, combined into a score out of a hundred and a letter.

Two rules keep the grade honest. A check that could not run is `unknown` and is
left out of the denominator entirely, so "we could not reach the router" never
reads as "the router is fine". And every failure carries the specific thing to
do about it: a grade with no remediation is a number, not a report.

Checks run concurrently under individual timeouts, because the whole audit has
to fit inside the supervisor's thirty seconds when it is invoked as a tool.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import uuid
from dataclasses import dataclass, field
from typing import Any

from blackice.models import (
    SEVERITY_CRITICAL,
    SEVERITY_HIGH,
    SEVERITY_LOW,
    SEVERITY_MEDIUM,
)

from . import hostinfo, net
from . import settings as config
from .store import Store

PASS, FAIL, UNKNOWN = "pass", "fail", "unknown"

# Per-check ceiling. `softwareupdate` and `system_profiler` are the slow ones
# and are given room; nothing is allowed to hold up the whole audit.
CHECK_TIMEOUT = 9.0

GRADES: tuple[tuple[int, str], ...] = (
    (95, "A+"), (90, "A"), (85, "A-"), (80, "B+"), (75, "B"), (70, "B-"),
    (65, "C+"), (60, "C"), (50, "C-"), (40, "D"), (0, "F"),
)

# Ports that mean "an administrator can log in here".
ADMIN_PORTS = (22, 23, 80, 443, 3389, 5900, 8080, 8443)


@dataclass
class Check:
    key: str
    scope: str
    title: str
    status: str = UNKNOWN
    weight: int = 1
    severity: int = SEVERITY_LOW
    detail: str = ""
    remediation: str = ""
    # Evidence that came off the network rather than from us.
    sensor_text: str | None = None

    def as_row(self) -> dict[str, Any]:
        return {
            "key": self.key, "scope": self.scope, "title": self.title,
            "status": self.status, "weight": self.weight, "severity": self.severity,
            "detail": self.detail, "remediation": self.remediation,
        }


@dataclass
class Report:
    scope: str
    score: int
    grade: str
    checks: list[Check] = field(default_factory=list)

    @property
    def failures(self) -> list[Check]:
        """Worst first: that is the order someone fixes them in."""
        return sorted(
            (c for c in self.checks if c.status == FAIL),
            key=lambda c: (-c.severity, -c.weight),
        )


def grade_for(score: int) -> str:
    return next(letter for floor, letter in GRADES if score >= floor)


def score_checks(checks: list[Check]) -> tuple[int, str]:
    """Weighted pass rate over the checks that actually ran."""
    graded = [c for c in checks if c.status in {PASS, FAIL}]
    total = sum(c.weight for c in graded)
    if not total:
        return 0, "F"
    earned = sum(c.weight for c in graded if c.status == PASS)
    score = round(100 * earned / total)
    return score, grade_for(score)


def _verdict(value: bool | None) -> str:
    return UNKNOWN if value is None else (PASS if value else FAIL)


async def _guard(coro: Any, fallback: Any) -> Any:
    """Run a check, or fall back to it having said nothing.

    The fallback is left at `unknown`, never `fail`: a check that could not run
    tells you nothing about the thing it was checking, and scoring it as a
    failure would quietly punish a slow router.
    """
    try:
        return await asyncio.wait_for(coro, CHECK_TIMEOUT)
    except TimeoutError:
        if isinstance(fallback, Check):
            fallback.detail = "check timed out"
        return fallback
    except Exception as exc:
        if isinstance(fallback, Check):
            fallback.detail = f"check failed: {type(exc).__name__}"
        return fallback


# --- host --------------------------------------------------------------------

async def _firewall() -> Check:
    enabled, _ = await hostinfo.firewall_state()
    return Check(
        "firewall", "host", "Application firewall enabled", _verdict(enabled), 5,
        SEVERITY_HIGH,
        detail="" if enabled else "inbound connections are unfiltered",
        remediation="System Settings › Network › Firewall — turn it on.",
    )


async def _stealth() -> Check:
    _, stealth = await hostinfo.firewall_state()
    return Check(
        "stealth", "host", "Firewall stealth mode", _verdict(stealth), 2, SEVERITY_LOW,
        detail="" if stealth else "this machine answers probes it could ignore",
        remediation="System Settings › Network › Firewall › Options — enable stealth mode.",
    )


async def _filevault() -> Check:
    on = await hostinfo.filevault_on()
    return Check(
        "filevault", "host", "Disk encryption (FileVault)", _verdict(on), 5, SEVERITY_HIGH,
        detail="" if on else "the disk is readable to anyone who takes the machine",
        remediation="System Settings › Privacy & Security › FileVault — turn it on.",
    )


async def _sip() -> Check:
    on = await hostinfo.sip_on()
    return Check(
        "sip", "host", "System Integrity Protection", _verdict(on), 5, SEVERITY_CRITICAL,
        detail="" if on else "SIP is off, so nothing protects the system files",
        remediation="Reboot to Recovery and run `csrutil enable`.",
    )


async def _gatekeeper() -> Check:
    on = await hostinfo.gatekeeper_on()
    return Check(
        "gatekeeper", "host", "Gatekeeper assessments", _verdict(on), 4, SEVERITY_HIGH,
        detail="" if on else "unsigned code runs without a check",
        remediation="Run `sudo spctl --master-enable`.",
    )


async def _auto_login() -> Check:
    user = await hostinfo.auto_login_user()
    ok = not user
    return Check(
        "auto_login", "host", "No automatic login", PASS if ok else FAIL, 3, SEVERITY_MEDIUM,
        detail="" if ok else "the machine logs itself in at boot, so the password buys nothing",
        remediation="System Settings › Users & Groups › Login Options — set automatic "
                    "login to Off.",
    )


async def _updates() -> Check:
    pending = await hostinfo.pending_updates()
    if pending is None:
        return Check("updates", "host", "Software up to date", UNKNOWN, 4, SEVERITY_MEDIUM,
                     detail="could not reach the update service")
    return Check(
        "updates", "host", "Software up to date", PASS if pending == 0 else FAIL, 4,
        SEVERITY_MEDIUM,
        detail="" if pending == 0 else f"{pending} update(s) waiting",
        remediation="System Settings › General › Software Update — install them.",
    )


async def _remote_access(listeners: list[hostinfo.Listener]) -> list[Check]:
    """SSH and Screen Sharing, called by name rather than by port number."""
    checks: list[Check] = []
    for port, key, title, fix in (
        (22, "remote_login", "Remote Login (SSH) not exposed",
         "System Settings › General › Sharing — turn off Remote Login."),
        (5900, "screen_sharing", "Screen Sharing not exposed",
         "System Settings › General › Sharing — turn off Screen Sharing."),
    ):
        listening = [item for item in listeners if item.port == port and item.world_reachable]
        checks.append(Check(
            key, "host", title, FAIL if listening else PASS, 3, SEVERITY_HIGH,
            detail=(f"listening on every interface ({listening[0].process or 'unknown process'})"
                    if listening else ""),
            remediation=fix,
        ))
    return checks


def _world_listeners(listeners: list[hostinfo.Listener]) -> Check:
    exposed = [item for item in listeners if item.world_reachable and item.port not in (22, 5900)]
    named = ", ".join(
        f"{item.port}/{item.process or config.service_name(item.port)}" for item in exposed[:8]
    )
    return Check(
        "world_listeners", "host", "No unexpected services on every interface",
        PASS if not exposed else FAIL, 3, SEVERITY_MEDIUM,
        detail="" if not exposed else f"{len(exposed)} service(s) bound to 0.0.0.0: {named}",
        remediation="Bind these to 127.0.0.1, or stop them. Run "
                    "`lsof -nP -iTCP -sTCP:LISTEN` to see what they are.",
    )


async def host_report() -> Report:
    listeners = await hostinfo.listeners()
    checks = await asyncio.gather(
        _guard(_firewall(), Check("firewall", "host", "Application firewall enabled",
                                  weight=5, severity=SEVERITY_HIGH)),
        _guard(_stealth(), Check("stealth", "host", "Firewall stealth mode", weight=2)),
        _guard(_filevault(), Check("filevault", "host", "Disk encryption (FileVault)",
                                   weight=5, severity=SEVERITY_HIGH)),
        _guard(_sip(), Check("sip", "host", "System Integrity Protection",
                             weight=5, severity=SEVERITY_CRITICAL)),
        _guard(_gatekeeper(), Check("gatekeeper", "host", "Gatekeeper assessments",
                                    weight=4, severity=SEVERITY_HIGH)),
        _guard(_auto_login(), Check("auto_login", "host", "No automatic login", weight=3)),
        _guard(_updates(), Check("updates", "host", "Software up to date", weight=4)),
    )
    out = list(checks)
    out.extend(await _remote_access(listeners))
    out.append(_world_listeners(listeners))
    score, grade = score_checks(out)
    return Report("host", score, grade, out)


# --- the LAN's attack surface -------------------------------------------------

async def lan_report(store: Store, ports: tuple[int, ...]) -> Report:
    counts = await store.device_counts()
    exposed = await store.exposed(tuple(config.RISKY_SERVICES))
    checks: list[Check] = []

    if counts["total"] == 0:
        checks.append(Check("lan_seen", "lan", "Network has been surveyed", UNKNOWN, 1,
                            detail="no scan has completed yet"))
        return Report("lan", *score_checks(checks), checks)

    by_port: dict[int, list[dict[str, Any]]] = {}
    for row in exposed:
        by_port.setdefault(row["port"], []).append(row)

    worst = ", ".join(
        f"{config.service_name(port)} on {len(rows)} device(s)"
        for port, rows in sorted(by_port.items())[:6]
    )
    checks.append(Check(
        "risky_services", "lan", "No legacy or unauthenticated services on the LAN",
        PASS if not by_port else FAIL, 6,
        SEVERITY_CRITICAL if any(p in {23, 445, 6379, 27017} for p in by_port) else SEVERITY_HIGH,
        detail="" if not by_port else worst,
        remediation="; ".join(
            f"{config.service_name(port)} — {config.RISKY_SERVICES[port]}"
            for port in sorted(by_port)
        )[:600] or "",
    ))

    untrusted = counts["untrusted"]
    checks.append(Check(
        "unknown_devices", "lan", "Every device on the network is accounted for",
        PASS if untrusted == 0 else FAIL, 3,
        SEVERITY_MEDIUM if untrusted < 5 else SEVERITY_HIGH,
        detail="" if untrusted == 0 else f"{untrusted} of {counts['total']} devices unnamed",
        remediation="Name what you recognise with `trust_device` so the rest stands out.",
    ))

    plaintext_admin = [
        row for row in await store.exposed((23, 21, 80))
        if row["port"] in (23, 21)
    ]
    checks.append(Check(
        "plaintext_admin", "lan", "No cleartext management protocols",
        PASS if not plaintext_admin else FAIL, 4, SEVERITY_HIGH,
        detail="" if not plaintext_admin
               else f"{len(plaintext_admin)} device(s) offering telnet or FTP",
        remediation="Disable telnet and FTP on those devices; use SSH and SFTP instead.",
    ))

    randomised = [d for d in await store.devices(seen_within_hours=24.0) if d.randomised]
    checks.append(Check(
        "stable_identity", "lan", "Devices have stable hardware addresses",
        PASS if len(randomised) <= max(2, counts["total"] // 4) else FAIL, 1, SEVERITY_LOW,
        detail=f"{len(randomised)} device(s) using randomised MACs",
        remediation="Expected from phones with private Wi-Fi addressing. Turn it off for "
                    "your own devices on your own network so they stay recognisable.",
    ))
    return Report("lan", *score_checks(checks), checks)


# --- the gateway ---------------------------------------------------------------

async def _dns_hijack_check(resolvers: list[str]) -> Check:
    """A resolver that answers for a name that does not exist is intercepting.

    The name is generated fresh so it cannot be in anyone's cache.
    """
    canary = f"{uuid.uuid4().hex}.invalid-canary.example"
    loop = asyncio.get_running_loop()
    hijacked = False
    with contextlib.suppress(Exception):
        await asyncio.wait_for(
            loop.run_in_executor(None, socket.gethostbyname, canary), 5.0
        )
        hijacked = True  # only reached if a nonexistent name resolved
    return Check(
        "dns_hijack", "router", "DNS returns NXDOMAIN for names that do not exist",
        FAIL if hijacked else PASS, 4, SEVERITY_HIGH,
        detail=(f"a made-up name resolved via {', '.join(resolvers) or 'the system resolver'}"
                if hijacked else ""),
        remediation="Your resolver or ISP is rewriting NXDOMAIN. Point DNS at a resolver "
                    "you choose, or enable encrypted DNS.",
    )


async def _wifi_check() -> Check:
    encryption = await hostinfo.wifi_encryption()
    if not encryption:
        return Check("wifi", "router", "Wi-Fi uses modern encryption", UNKNOWN, 5,
                     detail="not on Wi-Fi, or could not read the adapter")
    weak = any(bad in encryption.upper() for bad in ("NONE", "WEP", "OPEN", "TKIP"))
    return Check(
        "wifi", "router", "Wi-Fi uses modern encryption",
        FAIL if weak else PASS, 5, SEVERITY_CRITICAL if weak else SEVERITY_LOW,
        detail=f"current security: {encryption}",
        remediation="Move the network to WPA3, or WPA2-AES if a device cannot manage it.",
    )


async def _upnp_check(gateway: str) -> Check:
    upnp = await net.tcp_open(gateway, 1900, timeout=1.5) or bool(await net.ssdp_probe(2.0))
    return Check(
        "upnp", "router", "UPnP is not opening ports on demand",
        FAIL if upnp else PASS, 3, SEVERITY_MEDIUM,
        detail="UPnP/SSDP is answering — any device can punch a hole in the firewall"
               if upnp else "",
        remediation="Disable UPnP in the router's settings and forward ports by hand.",
    )


async def _gateway_ports_checks(gateway: str) -> list[Check]:
    ports = await net.scan_ports(gateway, (*ADMIN_PORTS, 7547), timeout=1.5, budget=10.0)
    admin = [p for p in ports if p in (80, 8080, 23, 21)]
    remote = [p for p in ports if p in (22, 3389)]
    return [
        Check("router_admin", "router", "Router admin interface is not on plain HTTP",
              PASS if not admin else FAIL, 4, SEVERITY_HIGH,
              detail="" if not admin else
                     f"{gateway} answers on "
                     f"{', '.join(config.service_name(p) for p in admin)}",
              remediation="Use the HTTPS admin page, and disable the plain-HTTP and "
                          "telnet ones."),
        Check("router_remote", "router", "Router does not expose SSH or RDP to the LAN",
              PASS if not remote else FAIL, 2, SEVERITY_MEDIUM,
              detail="" if not remote
                     else "remote administration is reachable from any device on the network",
              remediation="Turn off remote administration unless you use it."),
        Check("tr069", "router", "TR-069 remote management is closed",
              FAIL if 7547 in ports else PASS, 3, SEVERITY_HIGH,
              detail="port 7547 is open on the gateway" if 7547 in ports else "",
              remediation="TR-069 lets the ISP — and anyone who reaches it — reconfigure "
                          "the router. Disable it if the router allows it."),
    ]


async def router_report(gateway: str, resolvers: list[str]) -> Report:
    """Run concurrently: sequentially these would outlast a tool call."""
    if not gateway:
        checks = [Check("gateway_found", "router", "Gateway reachable", UNKNOWN, 1,
                        detail="no default route")]
        return Report("router", *score_checks(checks), checks)

    ports, upnp, wifi, dns = await asyncio.gather(
        _guard(_gateway_ports_checks(gateway), []),
        _guard(_upnp_check(gateway),
               Check("upnp", "router", "UPnP is not opening ports on demand", weight=3)),
        _guard(_wifi_check(),
               Check("wifi", "router", "Wi-Fi uses modern encryption", weight=5)),
        _guard(_dns_hijack_check(resolvers),
               Check("dns_hijack", "router",
                     "DNS returns NXDOMAIN for names that do not exist", weight=4)),
    )
    checks = [*(ports or []), upnp, wifi, dns]
    return Report("router", *score_checks(checks), checks)


# --- everything ----------------------------------------------------------------

async def full_report(store: Store, ports: tuple[int, ...]) -> dict[str, Report]:
    gateway = await net.default_gateway()
    resolvers = await net.dns_servers()
    host, lan, router = await asyncio.gather(
        host_report(), lan_report(store, ports), router_report(gateway, resolvers),
    )
    combined = [*host.checks, *lan.checks, *router.checks]
    score, grade = score_checks(combined)
    return {
        "host": host, "lan": lan, "router": router,
        "overall": Report("overall", score, grade, combined),
    }
