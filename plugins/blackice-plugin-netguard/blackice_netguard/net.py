"""Talking to the network, and to the OS about the network.

Everything here is best-effort and non-raising: a missing binary, an interface
that vanished, a host that ignores us are all ordinary, and each returns empty
rather than throwing. Deciding what the emptiness *means* belongs upstairs.

Nothing here needs root. A TCP connect to a LAN address makes the kernel ARP
for it whether or not the port is open, so a connect sweep populates the ARP
cache exactly as a raw ARP sweep would -- without a raw socket, and without the
plugin having to be privileged to see the network it is guarding.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import os
import re
import shutil
import socket
import struct
import sys
from dataclasses import dataclass, field
from xml.etree import ElementTree

# Probing every host at once buries the interface and starts dropping replies,
# which reads as "the device is gone" rather than "we asked too fast".
SWEEP_CONCURRENCY = 96
PORT_CONCURRENCY = 256
CONNECT_TIMEOUT = 1.0
SWEEP_PORTS = (80, 443, 22)  # only there to force ARP; open-ness is incidental

MAC_RE = re.compile(r"([0-9a-f]{1,2}(?::[0-9a-f]{1,2}){5})", re.I)
IPV4_RE = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b")

IS_DARWIN = sys.platform == "darwin"


@dataclass
class Interface:
    name: str
    ip: str
    cidr: str


@dataclass
class Host:
    ip: str
    mac: str = ""
    name: str = ""
    ports: list[int] = field(default_factory=list)
    responded: bool = False


# --- running other people's programs ---------------------------------------

NOT_RUN = -1  # the command never started, or never finished


async def run_status(
    *argv: str, timeout: float = 5.0, stdin: bytes | None = None,
) -> tuple[int, str, str]:
    """Run a command; return (exit status, stdout, stderr).

    `NOT_RUN` covers "no such binary", "would not launch", and "took too long"
    alike -- three ways of not getting an answer, none of which is a failure of
    the thing being asked about. Streams stay separate because some of the
    tools here emit parseable XML on stdout and chatter on stderr.
    """
    if not shutil.which(argv[0]) and not os.path.exists(argv[0]):
        return NOT_RUN, "", "no such command"
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE if stdin else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (OSError, ValueError) as exc:
        return NOT_RUN, "", str(exc)
    try:
        out, err = await asyncio.wait_for(proc.communicate(stdin), timeout)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()
        return NOT_RUN, "", f"timed out after {timeout}s"
    except Exception as exc:
        return NOT_RUN, "", str(exc)
    status = proc.returncode if proc.returncode is not None else NOT_RUN
    return status, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


async def run(*argv: str, timeout: float = 5.0, stdin: bytes | None = None) -> str:
    """Stdout of a command that succeeded. "" on any failure at all."""
    status, out, _ = await run_status(*argv, timeout=timeout, stdin=stdin)
    return out if status == 0 else ""


async def run_both(*argv: str, timeout: float = 5.0) -> str:
    """Both streams of a command that succeeded, for the tools that report
    on stderr as a matter of course."""
    status, out, err = await run_status(*argv, timeout=timeout)
    return f"{out}\n{err}" if status == 0 else ""


def have(binary: str) -> bool:
    return shutil.which(binary) is not None


def is_root() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0


# --- what this machine is attached to --------------------------------------

def _mask_to_prefix(mask: str) -> int:
    """0xffffff00 or 255.255.255.0 -> 24."""
    try:
        value = int(mask, 16) if mask.startswith("0x") else int(ipaddress.IPv4Address(mask))
    except ValueError:
        return 24
    return bin(value).count("1")


async def interfaces() -> list[Interface]:
    """Every up IPv4 interface with a private address, loopback excluded."""
    out: list[Interface] = []
    if IS_DARWIN:
        text = await run("ifconfig", "-a", timeout=5.0)
        current = ""
        for line in text.splitlines():
            if line and not line[0].isspace():
                current = line.split(":", 1)[0]
                continue
            fields = line.split()
            if len(fields) >= 4 and fields[0] == "inet" and fields[2] == "netmask":
                out.append(Interface(current, fields[1],
                                     f"{fields[1]}/{_mask_to_prefix(fields[3])}"))
    else:
        text = await run("ip", "-o", "-4", "addr", "show", timeout=5.0)
        for line in text.splitlines():
            fields = line.split()
            if len(fields) >= 4 and fields[2] == "inet":
                out.append(Interface(fields[1], fields[3].split("/")[0], fields[3]))

    keep: list[Interface] = []
    for iface in out:
        try:
            addr = ipaddress.IPv4Address(iface.ip)
        except ValueError:
            continue
        if addr.is_loopback or addr.is_link_local:
            continue
        keep.append(iface)
    return keep


async def local_subnets(limit: int = 4) -> list[str]:
    """The networks we are actually on, narrowed to something scannable.

    A /8 handed out by a badly configured DHCP server would otherwise mean
    sixteen million connect attempts, so anything wider than a /22 is clamped
    to the /24 around our own address.
    """
    nets: list[str] = []
    for iface in await interfaces():
        try:
            net = ipaddress.ip_network(iface.cidr, strict=False)
        except ValueError:
            continue
        if net.prefixlen < 22:
            net = ipaddress.ip_network(f"{iface.ip}/24", strict=False)
        text = str(net)
        if text not in nets:
            nets.append(text)
    return nets[:limit]


async def default_gateway() -> str:
    if IS_DARWIN:
        text = await run("route", "-n", "get", "default", timeout=4.0)
        for line in text.splitlines():
            if "gateway:" in line:
                return line.split(":", 1)[1].strip()
        return ""
    text = await run("ip", "route", "show", "default", timeout=4.0)
    match = IPV4_RE.search(text)
    return match.group(1) if match else ""


async def dns_servers() -> list[str]:
    """The resolvers this machine is actually using."""
    servers: list[str] = []
    if IS_DARWIN:
        text = await run("scutil", "--dns", timeout=5.0)
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("nameserver["):
                _, _, value = line.partition(":")
                value = value.strip()
                if value and value not in servers:
                    servers.append(value)
    if not servers:
        with contextlib.suppress(OSError), open("/etc/resolv.conf") as handle:
            for line in handle:
                if line.startswith("nameserver"):
                    value = line.split()[-1].strip()
                    if value not in servers:
                        servers.append(value)
    return servers


async def arp_table() -> dict[str, str]:
    """IP -> MAC, as the kernel currently believes it."""
    table: dict[str, str] = {}
    text = await run("arp", "-an", timeout=6.0)
    if not text:
        text = await run("ip", "neigh", "show", timeout=6.0)
    for line in text.splitlines():
        ip_match = IPV4_RE.search(line)
        mac_match = MAC_RE.search(line)
        if not (ip_match and mac_match):
            continue
        parts = [f"{int(p, 16):02x}" for p in mac_match.group(1).split(":")]
        mac = ":".join(parts)
        if mac == "ff:ff:ff:ff:ff:ff" or mac == "00:00:00:00:00:00":
            continue
        table[ip_match.group(1)] = mac
    return table


async def arp_pairs() -> list[tuple[str, str]]:
    """Every (ip, mac) the kernel knows, duplicates included.

    `arp_table` collapses to one entry per IP, which is exactly the information
    an ARP spoofing check needs to keep.
    """
    pairs: list[tuple[str, str]] = []
    text = await run("arp", "-an", timeout=6.0) or await run("ip", "neigh", "show", timeout=6.0)
    for line in text.splitlines():
        ip_match = IPV4_RE.search(line)
        mac_match = MAC_RE.search(line)
        if ip_match and mac_match:
            mac = ":".join(f"{int(p, 16):02x}" for p in mac_match.group(1).split(":"))
            if mac not in {"ff:ff:ff:ff:ff:ff", "00:00:00:00:00:00"}:
                pairs.append((ip_match.group(1), mac))
    return pairs


# --- probing other machines -------------------------------------------------

async def tcp_open(ip: str, port: int, timeout: float = CONNECT_TIMEOUT) -> bool:
    """True if the port accepts a connection. Refusals and timeouts are False,
    but a refusal still proved the host is there -- see `sweep`."""
    try:
        fut = asyncio.open_connection(ip, port)
        reader, writer = await asyncio.wait_for(fut, timeout)
    except (TimeoutError, OSError):
        return False
    except Exception:
        return False
    writer.close()
    with contextlib.suppress(Exception):
        await writer.wait_closed()
    _ = reader
    return True


async def _touch(ip: str, sem: asyncio.Semaphore) -> str:
    """Poke a host enough to make the kernel ARP for it."""
    async with sem:
        for port in SWEEP_PORTS:
            try:
                fut = asyncio.open_connection(ip, port)
                _, writer = await asyncio.wait_for(fut, CONNECT_TIMEOUT)
            except ConnectionRefusedError:
                return ip          # refused is a reply: something is home
            except (TimeoutError, OSError):
                continue
            except Exception:
                continue
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            return ip
        return ""


async def sweep(cidr: str, budget: float = 0.0) -> list[Host]:
    """Find what is on a subnet. Rootless: connect probes to populate the ARP
    cache, then believe the cache."""
    try:
        net = ipaddress.ip_network(cidr, strict=False)
    except ValueError:
        return []
    if net.num_addresses > 65536:
        return []

    hosts = [str(h) for h in net.hosts()]
    sem = asyncio.Semaphore(SWEEP_CONCURRENCY)
    coros = [_touch(ip, sem) for ip in hosts]
    try:
        if budget > 0:
            replies = await asyncio.wait_for(asyncio.gather(*coros), budget)
        else:
            replies = await asyncio.gather(*coros)
    except TimeoutError:
        replies = []
    except Exception:
        replies = []

    responded = {ip for ip in replies if ip}
    table = await arp_table()
    found: dict[str, Host] = {}
    for ip in responded | {ip for ip in table if ip in set(hosts)}:
        found[ip] = Host(ip=ip, mac=table.get(ip, ""), responded=ip in responded)
    return sorted(found.values(), key=lambda h: ipaddress.IPv4Address(h.ip))


async def scan_ports(ip: str, ports: tuple[int, ...], timeout: float = CONNECT_TIMEOUT,
                     budget: float = 0.0, concurrency: int = PORT_CONCURRENCY) -> list[int]:
    """Which of these ports accept a connection.

    `concurrency` is per call, so a caller scanning many hosts at once has to
    divide it down -- the socket budget is shared, and exhausting it makes open
    ports look closed.
    """
    sem = asyncio.Semaphore(max(1, concurrency))

    async def probe(port: int) -> int:
        async with sem:
            return port if await tcp_open(ip, port, timeout) else 0

    try:
        coros = [probe(p) for p in ports]
        results = (await asyncio.wait_for(asyncio.gather(*coros), budget)
                   if budget > 0 else await asyncio.gather(*coros))
    except TimeoutError:
        return []
    except Exception:
        return []
    return sorted(p for p in results if p)


async def reverse_name(ip: str, timeout: float = 2.0) -> str:
    """Whatever the network says this address is called.

    On macOS this resolver also answers for mDNS, so it picks up `.local` names
    without us having to speak mDNS ourselves. The answer is device-supplied
    and therefore untrusted text.
    """
    loop = asyncio.get_running_loop()
    try:
        result = await asyncio.wait_for(
            loop.run_in_executor(None, socket.gethostbyaddr, ip), timeout
        )
    except Exception:
        return ""
    name = (result[0] or "").strip()
    return "" if name == ip else name


# --- NetBIOS, for the Windows and SMB devices that answer nothing else ------

def _nbstat_query() -> bytes:
    # Name "*" padded to sixteen bytes, first-level encoded a nibble per byte.
    raw = b"*" + b"\x00" * 15
    encoded = bytes(c for byte in raw for c in (0x41 + (byte >> 4), 0x41 + (byte & 0x0F)))
    return (struct.pack(">HHHHHH", 0x4E47, 0x0000, 1, 0, 0, 0)
            + bytes([len(encoded)]) + encoded + b"\x00"
            + struct.pack(">HH", 0x0021, 0x0001))


def _parse_nbstat(data: bytes) -> str:
    """First non-group NetBIOS name in a node status reply."""
    # header 12 + encoded name 34 + type/class/ttl/rdlength 10 = 56
    if len(data) < 57:
        return ""
    count = data[56]
    offset = 57
    for _ in range(count):
        if offset + 18 > len(data):
            break
        name = data[offset:offset + 15].decode("ascii", "replace").strip()
        flags = struct.unpack(">H", data[offset + 16:offset + 18])[0]
        offset += 18
        if name and not flags & 0x8000:  # 0x8000 marks a group name
            return name
    return ""


async def netbios_name(ip: str, timeout: float = 1.2) -> str:
    """Ask a host what it calls itself over NetBIOS. Untrusted text."""
    loop = asyncio.get_running_loop()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setblocking(False)
    try:
        await loop.sock_sendto(sock, _nbstat_query(), (ip, 137))
        data, _ = await asyncio.wait_for(loop.sock_recvfrom(sock, 2048), timeout)
    except Exception:
        return ""
    finally:
        sock.close()
    with contextlib.suppress(Exception):
        return _parse_nbstat(data)
    return ""


# --- SSDP, for the IoT devices that announce themselves --------------------

SSDP_ADDR = ("239.255.255.250", 1900)
SSDP_SEARCH = (
    b"M-SEARCH * HTTP/1.1\r\n"
    b"HOST: 239.255.255.250:1900\r\n"
    b'MAN: "ssdp:discover"\r\n'
    b"MX: 1\r\n"
    b"ST: ssdp:all\r\n\r\n"
)


async def ssdp_probe(timeout: float = 3.0) -> dict[str, str]:
    """IP -> the SERVER/USN string it advertised. All of it untrusted text."""
    loop = asyncio.get_running_loop()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    sock.setblocking(False)
    found: dict[str, str] = {}
    try:
        await loop.sock_sendto(sock, SSDP_SEARCH, SSDP_ADDR)
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            remaining = deadline - loop.time()
            try:
                data, addr = await asyncio.wait_for(
                    loop.sock_recvfrom(sock, 4096), remaining
                )
            except TimeoutError:
                break
            except Exception:
                break
            text = data.decode("utf-8", "replace")
            server = ""
            for line in text.splitlines():
                if line.lower().startswith(("server:", "usn:")) and not server:
                    server = line.split(":", 1)[1].strip()
            if addr[0] not in found and server:
                found[addr[0]] = server[:200]
    except Exception:
        return found
    finally:
        sock.close()
    return found


# --- nmap, when the operator has installed it ------------------------------

def nmap_available() -> bool:
    return have("nmap")


async def nmap_discover(cidr: str, timeout: float = 60.0) -> list[Host]:
    """`nmap -sn`, parsed. Empty if nmap is absent or unhappy."""
    text = await run("nmap", "-sn", "-n", "-oX", "-", cidr, timeout=timeout)
    return _parse_nmap(text)


async def nmap_services(ip: str, ports: tuple[int, ...], timeout: float = 25.0) -> dict[int, str]:
    """Service and version banners for one host. Untrusted text, all of it."""
    spec = ",".join(str(p) for p in ports) if ports else "1-1024"
    text = await run("nmap", "-Pn", "-sT", "-sV", "--version-light",
                     "-p", spec, "-oX", "-", ip, timeout=timeout)
    services: dict[int, str] = {}
    try:
        root = ElementTree.fromstring(text) if text.strip() else None
    except ElementTree.ParseError:
        return services
    if root is None:
        return services
    for port in root.iter("port"):
        state = port.find("state")
        if state is None or state.get("state") != "open":
            continue
        service = port.find("service")
        label = ""
        if service is not None:
            label = " ".join(
                filter(None, (service.get("name", ""), service.get("product", ""),
                              service.get("version", "")))
            ).strip()
        with contextlib.suppress(ValueError, TypeError):
            services[int(port.get("portid", "0"))] = label[:120]
    return services


def _parse_nmap(text: str) -> list[Host]:
    hosts: list[Host] = []
    try:
        root = ElementTree.fromstring(text) if text.strip() else None
    except ElementTree.ParseError:
        return hosts
    if root is None:
        return hosts
    for node in root.iter("host"):
        status = node.find("status")
        if status is not None and status.get("state") != "up":
            continue
        ip = mac = ""
        for address in node.iter("address"):
            kind = address.get("addrtype", "")
            if kind == "ipv4":
                ip = address.get("addr", "")
            elif kind == "mac":
                mac = (address.get("addr", "") or "").lower()
        if ip:
            hosts.append(Host(ip=ip, mac=mac, responded=True))
    return hosts
