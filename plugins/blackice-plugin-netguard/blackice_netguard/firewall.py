"""Cutting a host off, and putting it back.

One honest caveat, surfaced in every result rather than buried here: a packet
filter on this machine drops traffic *to and from this machine*. Unless this
machine is the gateway, a blocked device is still on the network and can still
reach everything else on it. Blocking is containment of your own attack
surface, not eviction from the LAN -- that takes the router.

The whole rule set is rewritten from the block table on every change, so
applying, releasing, and reconciling after a restart are all the same operation
and none of them can drift.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass

from . import net

ANCHOR = "com.apple/blackice-netguard"   # macOS evaluates com.apple/* by default
CHAIN = "BLACKICE_NETGUARD"              # Linux
PFCTL = "/sbin/pfctl"

CAVEAT = (
    "A block here filters traffic to and from this machine only. Unless this "
    "machine is the network's gateway, the device remains on the LAN and can "
    "still reach other devices."
)


@dataclass
class Outcome:
    ok: bool
    detail: str
    backend: str = ""


def valid_target(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return not (addr.is_loopback or addr.is_unspecified or addr.is_multicast)


async def backend() -> str:
    """"pf", "iptables", or "" when neither is usable here."""
    if net.IS_DARWIN and (net.have("pfctl") or net.have(PFCTL)):
        return "pf"
    if net.have("iptables"):
        return "iptables"
    return ""


NO_PRIVILEGE = (
    "not running as root, and passwordless sudo is not configured for this user — "
    "the block was recorded but no rule was installed"
)


async def _run_privileged(*argv: str, stdin: bytes | None = None,
                          timeout: float = 10.0) -> tuple[bool, str]:
    """Run a command that needs root, directly or through a passwordless sudo.

    `sudo -n` fails immediately rather than prompting, so this never hangs
    waiting for a password nobody is there to type.
    """
    if net.is_root():
        status, out, err = await net.run_status(*argv, stdin=stdin, timeout=timeout)
        return status == 0, (out or err).strip()
    if not net.have("sudo"):
        return False, NO_PRIVILEGE
    status, out, err = await net.run_status(
        "sudo", "-n", *argv, stdin=stdin, timeout=timeout
    )
    if status != 0 and "password is required" in err.lower():
        return False, NO_PRIVILEGE
    return status == 0, (out or err).strip()


async def can_apply() -> tuple[bool, str]:
    """Whether a block would actually reach the packet filter, and why not."""
    kind = await backend()
    if not kind:
        return False, "no supported packet filter on this system"
    if net.is_root():
        return True, kind
    if not net.have("sudo"):
        return False, NO_PRIVILEGE
    status, _, _ = await net.run_status("sudo", "-n", "true", timeout=5.0)
    return (True, kind) if status == 0 else (False, NO_PRIVILEGE)


def _pf_rules(ips: list[str]) -> bytes:
    lines = ["# managed by black-ice netguard; edits here are overwritten"]
    for ip in ips:
        lines.append(f"block drop quick from {ip} to any")
        lines.append(f"block drop quick from any to {ip}")
    return ("\n".join(lines) + "\n").encode()


async def sync(ips: list[str]) -> Outcome:
    """Make the packet filter match exactly this list of blocked addresses."""
    ips = sorted({ip for ip in ips if valid_target(ip)})
    kind = await backend()
    if kind == "pf":
        return await _sync_pf(ips)
    if kind == "iptables":
        return await _sync_iptables(ips)
    return Outcome(False, "no supported packet filter on this system")


async def _sync_pf(ips: list[str]) -> Outcome:
    pfctl = PFCTL if net.have(PFCTL) else "pfctl"
    ok, detail = await _run_privileged(pfctl, "-a", ANCHOR, "-f", "-", stdin=_pf_rules(ips))
    if not ok:
        return Outcome(False, detail, "pf")
    if ips:
        # -E is reference counted, so enabling an already-enabled pf is safe.
        await _run_privileged(pfctl, "-E")
    return Outcome(True, f"{len(ips)} address(es) blocked via pf anchor {ANCHOR}", "pf")


async def _sync_iptables(ips: list[str]) -> Outcome:
    # The chain may already exist; that is a success for our purposes.
    created, detail = await _run_privileged("iptables", "-N", CHAIN)
    if not created and detail == NO_PRIVILEGE:
        return Outcome(False, detail, "iptables")
    # Idempotent: -C tests for the jump before -I installs it.
    for table in ("INPUT", "OUTPUT", "FORWARD"):
        present, _ = await _run_privileged("iptables", "-C", table, "-j", CHAIN, timeout=5.0)
        if not present:
            await _run_privileged("iptables", "-I", table, "-j", CHAIN)
    await _run_privileged("iptables", "-F", CHAIN)
    for ip in ips:
        await _run_privileged("iptables", "-A", CHAIN, "-s", ip, "-j", "DROP")
        await _run_privileged("iptables", "-A", CHAIN, "-d", ip, "-j", "DROP")
    return Outcome(True, f"{len(ips)} address(es) blocked in chain {CHAIN}", "iptables")


async def active_rules() -> list[str]:
    """What the filter currently has, so a restart can reconcile against it."""
    kind = await backend()
    if kind == "pf":
        pfctl = PFCTL if net.have(PFCTL) else "pfctl"
        _, text = await _run_privileged(pfctl, "-a", ANCHOR, "-s", "rules", timeout=8.0)
    elif kind == "iptables":
        _, text = await _run_privileged("iptables", "-S", CHAIN, timeout=8.0)
    else:
        return []
    found: list[str] = []
    for match in net.IPV4_RE.finditer(text or ""):
        ip = match.group(1)
        if ip not in found and valid_target(ip):
            found.append(ip)
    return found
