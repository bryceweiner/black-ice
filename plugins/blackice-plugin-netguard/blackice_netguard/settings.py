"""Configuration, read from the environment on every use.

Everything here has a working default. Nothing here is a secret, and nothing
here can make `start()` fail -- a malformed value falls back rather than
raising, because a typo in a subnet should not cost you the whole sensor.
"""

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass, field

SUBNETS_ENV = "BLACKICE_NETGUARD_SUBNETS"
SCAN_INTERVAL_ENV = "BLACKICE_NETGUARD_SCAN_INTERVAL"
PASSIVE_INTERVAL_ENV = "BLACKICE_NETGUARD_PASSIVE_INTERVAL"
IDS_LOG_ENV = "BLACKICE_NETGUARD_IDS_LOG"
FEEDS_ENV = "BLACKICE_NETGUARD_FEEDS"
FEED_REFRESH_ENV = "BLACKICE_NETGUARD_FEED_REFRESH_HOURS"
BLOCK_MODE_ENV = "BLACKICE_NETGUARD_BLOCK_MODE"
BLOCK_TTL_ENV = "BLACKICE_NETGUARD_BLOCK_TTL_MINUTES"
PORTS_ENV = "BLACKICE_NETGUARD_PORTS"
CAPTURE_ENV = "BLACKICE_NETGUARD_CAPTURE"
NMAP_ENV = "BLACKICE_NETGUARD_NMAP"
BASELINE_HOURS_ENV = "BLACKICE_NETGUARD_BASELINE_HOURS"

DEFAULT_SCAN_INTERVAL = 900.0     # a full active sweep, every fifteen minutes
DEFAULT_PASSIVE_INTERVAL = 60.0   # cheap reads of what the OS already knows
DEFAULT_FEED_REFRESH_HOURS = 6.0
DEFAULT_BLOCK_TTL_MINUTES = 0     # 0 means the block stands until released
DEFAULT_BASELINE_HOURS = 24.0     # how long "everything is new" lasts

# Two block modes, because the answer differs by household. `confirm` stages a
# block and waits for a second, explicit call; `immediate` lets the assistant
# apply one on its own judgement. Staging is the default: a false positive in
# immediate mode knocks a real device off a real network.
BLOCK_CONFIRM = "confirm"
BLOCK_IMMEDIATE = "immediate"

# Ports worth knowing about on a home network. Deliberately short -- this is a
# posture check, not a pentest, and every entry costs a connect per host.
DEFAULT_PORTS: tuple[int, ...] = (
    21, 22, 23, 25, 53, 80, 110, 111, 135, 139, 143, 443, 445, 515, 548, 554,
    587, 631, 993, 995, 1433, 1723, 1883, 1900, 3306, 3389, 5000, 5060, 5432,
    5900, 6379, 7547, 8000, 8008, 8080, 8081, 8443, 8883, 9000, 9100, 27017,
)

SERVICES: dict[int, str] = {
    21: "ftp", 22: "ssh", 23: "telnet", 25: "smtp", 53: "dns", 80: "http",
    110: "pop3", 111: "rpcbind", 135: "msrpc", 139: "netbios", 143: "imap",
    443: "https", 445: "smb", 515: "lpd", 548: "afp", 554: "rtsp",
    587: "submission", 631: "ipp", 993: "imaps", 995: "pop3s", 1433: "mssql",
    1723: "pptp", 1883: "mqtt", 1900: "ssdp", 3306: "mysql", 3389: "rdp",
    5000: "upnp", 5060: "sip", 5432: "postgres", 5900: "vnc", 6379: "redis",
    7547: "tr069", 8000: "http-alt", 8008: "http-alt", 8080: "http-proxy",
    8081: "http-alt", 8443: "https-alt", 8883: "mqtts", 9000: "http-alt",
    9100: "jetdirect", 27017: "mongodb",
}

# Services that should not be reachable on a LAN in 2026. Weighted into the
# attack-surface grade and called out by name in remediation.
RISKY_SERVICES: dict[int, str] = {
    21: "FTP is unencrypted and often anonymous",
    23: "Telnet sends credentials in clear text",
    111: "rpcbind exposes RPC service enumeration",
    135: "msrpc is a remote code execution surface",
    139: "NetBIOS leaks names and shares",
    445: "SMB is the most exploited LAN service there is",
    1433: "MSSQL should never be LAN-reachable",
    1723: "PPTP's encryption is broken",
    3306: "MySQL should never be LAN-reachable",
    3389: "RDP is brute-forced constantly",
    5432: "PostgreSQL should never be LAN-reachable",
    5900: "VNC is frequently unauthenticated",
    6379: "Redis defaults to no authentication at all",
    7547: "TR-069 lets an ISP -- or anyone -- reconfigure the device",
    27017: "MongoDB defaults to no authentication at all",
}

# Plain-text one-IP-per-line feeds. All three are free, public, and widely used;
# none needs a key. A feed that will not load is recorded and skipped.
DEFAULT_FEEDS: tuple[tuple[str, str], ...] = (
    ("tor-exits", "https://check.torproject.org/torbulkexitlist"),
    ("feodo-c2", "https://feodotracker.abuse.ch/downloads/ipblocklist.txt"),
    ("ipsum-l3", "https://raw.githubusercontent.com/stamparm/ipsum/master/levels/3.txt"),
)

# Where Suricata and Zeek usually put their JSON, on macOS and Linux both.
IDS_LOG_CANDIDATES: tuple[str, ...] = (
    "/var/log/suricata/eve.json",
    "/usr/local/var/log/suricata/eve.json",
    "/opt/homebrew/var/log/suricata/eve.json",
    "/var/log/zeek/current/notice.log",
    "/usr/local/var/log/zeek/current/notice.log",
    "/opt/homebrew/var/log/zeek/current/notice.log",
)


@dataclass(frozen=True)
class Settings:
    subnets: tuple[str, ...] = ()
    scan_interval: float = DEFAULT_SCAN_INTERVAL
    passive_interval: float = DEFAULT_PASSIVE_INTERVAL
    ids_log: str = ""
    feeds: tuple[tuple[str, str], ...] = DEFAULT_FEEDS
    feed_refresh_hours: float = DEFAULT_FEED_REFRESH_HOURS
    block_mode: str = BLOCK_CONFIRM
    block_ttl_minutes: int = DEFAULT_BLOCK_TTL_MINUTES
    ports: tuple[int, ...] = DEFAULT_PORTS
    capture: bool = True
    nmap: bool = True
    baseline_hours: float = DEFAULT_BASELINE_HOURS
    warnings: tuple[str, ...] = field(default=())


def _float(name: str, default: float, minimum: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(minimum, float(raw))
    except ValueError:
        return default


def _int(name: str, default: int, minimum: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(minimum, int(raw))
    except ValueError:
        return default


def _flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    if raw in {"0", "off", "false", "no", "never"}:
        return False
    return raw not in {"auto"} or default


def _subnets(warnings: list[str]) -> tuple[str, ...]:
    """Explicit CIDRs, or empty to mean "work it out from the interfaces"."""
    out: list[str] = []
    for clause in os.environ.get(SUBNETS_ENV, "").split(","):
        clause = clause.strip()
        if not clause:
            continue
        try:
            net = ipaddress.ip_network(clause, strict=False)
        except ValueError:
            warnings.append(f"ignored unreadable subnet {clause!r}")
            continue
        if net.num_addresses > 65536:
            warnings.append(f"ignored {clause}: larger than a /16")
            continue
        out.append(str(net))
    return tuple(out)


def _feeds(warnings: list[str]) -> tuple[tuple[str, str], ...]:
    raw = os.environ.get(FEEDS_ENV, "").strip()
    if not raw:
        return DEFAULT_FEEDS
    if raw.lower() in {"off", "none", "false"}:
        return ()
    out: list[tuple[str, str]] = []
    for clause in raw.split(","):
        clause = clause.strip()
        if not clause:
            continue
        name, _, url = clause.partition("=")
        url = (url or name).strip()
        if not url.startswith(("http://", "https://")):
            warnings.append(f"ignored feed {clause!r}: not an http url")
            continue
        out.append(((name.strip() if url != name else url.rsplit("/", 1)[-1]), url))
    return tuple(out)


def _ports(warnings: list[str]) -> tuple[int, ...]:
    raw = os.environ.get(PORTS_ENV, "").strip()
    if not raw:
        return DEFAULT_PORTS
    out: list[int] = []
    for clause in raw.replace(";", ",").split(","):
        clause = clause.strip()
        if not clause:
            continue
        try:
            port = int(clause)
        except ValueError:
            warnings.append(f"ignored unreadable port {clause!r}")
            continue
        if 1 <= port <= 65535:
            out.append(port)
    return tuple(dict.fromkeys(out)) or DEFAULT_PORTS


def _block_mode(warnings: list[str]) -> str:
    raw = os.environ.get(BLOCK_MODE_ENV, "").strip().lower()
    if not raw:
        return BLOCK_CONFIRM
    if raw in {BLOCK_CONFIRM, BLOCK_IMMEDIATE}:
        return raw
    warnings.append(f"ignored block mode {raw!r}; staying on {BLOCK_CONFIRM}")
    return BLOCK_CONFIRM


def _ids_log() -> str:
    explicit = os.environ.get(IDS_LOG_ENV, "").strip()
    if explicit:
        return "" if explicit.lower() in {"off", "none", "false"} else explicit
    for candidate in IDS_LOG_CANDIDATES:
        if os.path.exists(candidate) and os.access(candidate, os.R_OK):
            return candidate
    return ""


def load() -> Settings:
    """Read the environment. Never raises."""
    warnings: list[str] = []
    return Settings(
        subnets=_subnets(warnings),
        scan_interval=_float(SCAN_INTERVAL_ENV, DEFAULT_SCAN_INTERVAL, 60.0),
        passive_interval=_float(PASSIVE_INTERVAL_ENV, DEFAULT_PASSIVE_INTERVAL, 10.0),
        ids_log=_ids_log(),
        feeds=_feeds(warnings),
        feed_refresh_hours=_float(FEED_REFRESH_ENV, DEFAULT_FEED_REFRESH_HOURS, 0.25),
        block_mode=_block_mode(warnings),
        block_ttl_minutes=_int(BLOCK_TTL_ENV, DEFAULT_BLOCK_TTL_MINUTES, 0),
        ports=_ports(warnings),
        capture=_flag(CAPTURE_ENV, True),
        nmap=_flag(NMAP_ENV, True),
        baseline_hours=_float(BASELINE_HOURS_ENV, DEFAULT_BASELINE_HOURS, 0.0),
        warnings=tuple(warnings),
    )


def service_name(port: int) -> str:
    return SERVICES.get(port, f"port-{port}")
