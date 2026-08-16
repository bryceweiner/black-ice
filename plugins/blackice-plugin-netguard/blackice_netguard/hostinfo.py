"""What this machine is doing on the network, and how it is configured.

Read-only, unprivileged, and quiet: everything here comes from what the OS will
already tell any user. Process names come from `lsof` when it is available,
because "port 5900 is open" and "Screen Sharing is listening on port 5900" are
very different sentences to put in front of someone.
"""

from __future__ import annotations

import contextlib
import ipaddress
import re
from dataclasses import dataclass

from . import net

LISTEN_RE = re.compile(r"^(?P<addr>.+?)[.:](?P<port>\d+)$")


@dataclass
class Listener:
    port: int
    proto: str
    address: str
    process: str = ""

    @property
    def world_reachable(self) -> bool:
        """Bound to every interface rather than to loopback."""
        return self.address in {"*", "0.0.0.0", "::", "[::]", "", "0"}


@dataclass
class Connection:
    local_port: int
    remote_ip: str
    remote_port: int
    state: str
    process: str = ""


def _split_hostport(text: str) -> tuple[str, int]:
    """`*:22`, `127.0.0.1.51234`, `[::1]:631` -> (address, port)."""
    text = text.strip()
    match = LISTEN_RE.match(text)
    if not match:
        return text, 0
    addr = match.group("addr").strip("[]")
    with contextlib.suppress(ValueError):
        return addr, int(match.group("port"))
    return addr, 0


async def listeners() -> list[Listener]:
    """Every TCP port this machine is accepting connections on."""
    found: dict[tuple[int, str], Listener] = {}

    text = await net.run("lsof", "-nP", "-iTCP", "-sTCP:LISTEN", timeout=8.0)
    for line in text.splitlines()[1:]:
        fields = line.split()
        if len(fields) < 9:
            continue
        addr, port = _split_hostport(fields[8])
        if port:
            found[(port, addr)] = Listener(port, "tcp", addr, fields[0])

    if not found:
        text = await net.run("netstat", "-an", "-p", "tcp", timeout=8.0)
        for line in text.splitlines():
            if "LISTEN" not in line:
                continue
            fields = line.split()
            if len(fields) < 4:
                continue
            addr, port = _split_hostport(fields[3])
            if port:
                found[(port, addr)] = Listener(port, "tcp", addr)

    return sorted(found.values(), key=lambda item: (item.port, item.address))


async def connections() -> list[Connection]:
    """Established TCP connections, with the process where we can get it."""
    found: list[Connection] = []
    by_endpoint: dict[tuple[str, int], str] = {}

    text = await net.run("lsof", "-nP", "-iTCP", "-sTCP:ESTABLISHED", timeout=8.0)
    for line in text.splitlines()[1:]:
        fields = line.split()
        if len(fields) < 9 or "->" not in fields[8]:
            continue
        _, _, remote = fields[8].partition("->")
        remote_ip, remote_port = _split_hostport(remote)
        if remote_port:
            by_endpoint[(remote_ip, remote_port)] = fields[0]

    text = await net.run("netstat", "-an", "-p", "tcp", timeout=8.0)
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 6 or not fields[0].startswith("tcp"):
            continue
        state = fields[-1]
        if state not in {"ESTABLISHED", "SYN_RCVD", "SYN_RECV", "SYN_SENT"}:
            continue
        _, local_port = _split_hostport(fields[3])
        remote_ip, remote_port = _split_hostport(fields[4])
        if not remote_port:
            continue
        found.append(Connection(
            local_port, remote_ip, remote_port, state,
            by_endpoint.get((remote_ip, remote_port), ""),
        ))
    return found


def is_external(ip: str) -> bool:
    """True for addresses out on the internet -- the ones worth checking
    against a threat feed."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return not (addr.is_private or addr.is_loopback or addr.is_link_local
                or addr.is_multicast or addr.is_reserved or addr.is_unspecified)


# --- macOS security posture -------------------------------------------------

FIREWALL_BIN = "/usr/libexec/ApplicationFirewall/socketfilterfw"


async def _say(*argv: str, timeout: float = 6.0) -> str:
    """Whatever a status command said, on either stream, exit code ignored.

    `spctl --status` exits non-zero precisely when Gatekeeper is off, which is
    the answer -- treating that as "command failed" would report the worst case
    as unknown.
    """
    _, out, err = await net.run_status(*argv, timeout=timeout)
    return f"{out}\n{err}".strip().lower()


async def firewall_state() -> tuple[bool | None, bool | None]:
    """(enabled, stealth_mode). None where we could not tell."""
    if not net.IS_DARWIN:
        return None, None
    enabled = stealth = None
    text = await _say(FIREWALL_BIN, "--getglobalstate")
    if text:
        enabled = "disabled" not in text and "state = 0" not in text
    text = await _say(FIREWALL_BIN, "--getstealthmode")
    if text:
        stealth = "disabled" not in text and "off" not in text
    return enabled, stealth


async def filevault_on() -> bool | None:
    if not net.IS_DARWIN:
        return None
    text = await _say("fdesetup", "status", timeout=8.0)
    if "filevault is" not in text:
        return None
    return "filevault is on" in text


async def sip_on() -> bool | None:
    if not net.IS_DARWIN:
        return None
    text = await _say("csrutil", "status")
    if "integrity protection status" not in text:
        return None
    return "enabled" in text


async def gatekeeper_on() -> bool | None:
    if not net.IS_DARWIN:
        return None
    text = await _say("spctl", "--status")
    if "assessments" not in text:
        return None
    return "assessments enabled" in text


async def auto_login_user() -> str:
    if not net.IS_DARWIN:
        return ""
    text = await net.run(
        "defaults", "read", "/Library/Preferences/com.apple.loginwindow",
        "autoLoginUser", timeout=5.0,
    )
    return text.strip()


async def wifi_encryption() -> str:
    """The security mode of the Wi-Fi network we are on, if any.

    `system_profiler` is slow, so callers should treat "" as "did not find out"
    rather than "not encrypted".
    """
    if not net.IS_DARWIN:
        return ""
    text = await net.run("system_profiler", "SPAirPortDataType", timeout=8.0)
    if not text:
        return ""
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if "Current Network Information:" in line:
            for follow in lines[index:index + 30]:
                if "Security:" in follow:
                    return follow.split(":", 1)[1].strip()
    return ""


async def pending_updates() -> int | None:
    """How many software updates are waiting. None if we could not check --
    this one talks to Apple and is allowed to be slow or blocked."""
    if not net.IS_DARWIN:
        return None
    # `softwareupdate` reports its findings on stderr, so both streams matter.
    text = await net.run_both("softwareupdate", "-l", "--no-scan", timeout=20.0)
    if not text.strip():
        return None
    if "no new software available" in text.lower():
        return 0
    return sum(1 for line in text.splitlines() if line.strip().startswith("* Label:"))
