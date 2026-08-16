"""Network monitoring, intrusion detection, and hardening posture.

Three sensors, because there are three jobs and they fail independently:

* `netguard.inventory` -- what is on this network, and what it is offering.
* `netguard.ids`       -- what is happening that should not be.
* `netguard.posture`   -- how defensible this machine and this network are.

Everything is auto-detecting and degrades rather than failing. nmap if it is
installed, a rootless connect sweep if not. Suricata's alerts if Suricata is
running, our own heuristics if not. Packet capture if the process is
privileged, the ARP cache if not. Each sensor says which mode it is in, on the
dashboard, because "quiet" and "not looking" are very different states and a
security tool that confuses them is worse than none.

Trust: every hostname, banner, vendor string, and third-party signature name
comes from the thing being watched. All of it travels as `sensor_text` so
triage keeps treating it as hostile input. Plugin-authored `summary` text
contains addresses, counts, and the owner's own labels -- nothing else.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import ipaddress
import socket
import time
from typing import Any

from blackice.models import (
    SEVERITY_CRITICAL,
    SEVERITY_HIGH,
    SEVERITY_INFO,
    SEVERITY_MEDIUM,
    AlarmRuleSpec,
    Event,
    SensorDescriptor,
    ToolSpec,
    WidgetSpec,
)
from blackice.plugins.base import PluginContext, SensorPlugin

from . import capture, detect, firewall, hostinfo, ids, intel, net, oui, posture
from . import settings as config
from .detect import Finding
from .store import Store

INVENTORY_SENSOR = "netguard.inventory"
IDS_SENSOR = "netguard.ids"
POSTURE_SENSOR = "netguard.posture"

# Which sensor an event belongs to. Anything unlisted lands on the IDS sensor,
# which is the right default for a detection we forgot to classify.
SENSOR_FOR_KIND = {
    "device_new": INVENTORY_SENSOR, "device_seen": INVENTORY_SENSOR,
    "port_opened": INVENTORY_SENSOR, "scan_complete": INVENTORY_SENSOR,
    "posture_regression": POSTURE_SENSOR, "posture_graded": POSTURE_SENSOR,
}

# A tool call runs under the supervisor's 30s timeout. Overrunning it marks the
# whole plugin unhealthy, so on-demand work gives up first and says so.
ON_DEMAND_BUDGET = 20.0
POSTURE_INTERVAL = 3600.0
IDS_POLL = 5.0
BOOT_PASSIVE_DELAY = 3.0
BOOT_SCAN_DELAY = 15.0
BOOT_POSTURE_DELAY = 45.0
# A drop of this many points between audits is a regression worth saying.
REGRESSION_POINTS = 5
# Hosts port-scanned at once. Times the per-host concurrency, that is the
# socket budget; too high and the replies start getting dropped.
HOST_CONCURRENCY = 8
PER_HOST_PORT_CONCURRENCY = 32
WINDOW_RESET_SECONDS = 3600.0
IDS_OFFSET_KEY = "ids_offset"


def _known_state(severity: int) -> str:
    if severity >= SEVERITY_CRITICAL:
        return "unhealthy"
    if severity >= SEVERITY_MEDIUM:
        return "degraded"
    return "healthy"


def _resolve(target: str) -> str:
    """A literal address stays as it is; a name is looked up. "" if neither."""
    target = (target or "").strip()
    if not target:
        return ""
    with contextlib.suppress(ValueError):
        return str(ipaddress.ip_address(target))
    with contextlib.suppress(OSError):
        return socket.gethostbyname(target)
    return ""


class NetguardPlugin(SensorPlugin):
    name = "netguard"
    version = "0.1.0"

    def __init__(self) -> None:
        self.ctx: PluginContext | None = None
        self.store: Store | None = None
        self.cfg = config.Settings()
        self.tasks: list[asyncio.Task] = []
        self.capture = capture.Capture()
        self.tailer: ids.Tailer | None = None
        self.gateway = ""
        self.subnets: list[str] = []
        self.modes: dict[str, str] = {}
        self.scanning = False
        self._window_ports: dict[str, set[int]] = {}
        self._window_started = 0.0
        self._reconciled = False

    # --- lifecycle ----------------------------------------------------------

    async def start(self, ctx: PluginContext) -> None:
        """Fast and local. Everything that touches the network is a task."""
        self.ctx = ctx
        self.cfg = config.load()
        for warning in self.cfg.warnings:
            ctx.log.warning("configuration: %s", warning)

        self.store = Store(ctx.db)
        await self.store.setup()

        if self.cfg.capture:
            ctx.log.info("packet capture: %s", self.capture.start())
        if self.cfg.ids_log:
            self.tailer = ids.Tailer(self.cfg.ids_log)
            self.tailer.restore(await self.store.meta_get(IDS_OFFSET_KEY))

        self.modes = {
            "Discovery": "nmap" if (self.cfg.nmap and net.nmap_available())
                         else "connect sweep (rootless)",
            "Packet capture": self.capture.mode() if self.cfg.capture else "disabled",
            "External IDS": self.cfg.ids_log or "none found",
            "Threat feeds": f"{len(self.cfg.feeds)} configured" if self.cfg.feeds else "off",
            "Blocking": f"staged, needs confirmation ({self.cfg.block_mode})"
                        if self.cfg.block_mode == config.BLOCK_CONFIRM
                        else "applied immediately",
        }
        self._window_started = time.monotonic()

        self.tasks = [
            asyncio.create_task(self._passive_loop()),
            asyncio.create_task(self._scan_loop()),
            asyncio.create_task(self._posture_loop()),
        ]
        if self.tailer is not None:
            self.tasks.append(asyncio.create_task(self._ids_loop()))

    async def stop(self) -> None:
        """Idempotent: called on failure paths as well as on shutdown."""
        tasks, self.tasks = self.tasks, []
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        with contextlib.suppress(Exception):
            self.capture.stop()
        if self.tailer is not None and self.store is not None:
            with contextlib.suppress(Exception):
                await self.store.meta_set(IDS_OFFSET_KEY, self.tailer.state())

    # --- description ---------------------------------------------------------

    def describe(self) -> list[SensorDescriptor]:
        """Static, synchronous, and incapable of raising."""
        return [self._ids_sensor(), self._inventory_sensor(), self._posture_sensor()]

    def _ids_sensor(self) -> SensorDescriptor:
        return SensorDescriptor(
            id=IDS_SENSOR,
            name="Intrusion detection",
            kind="security",
            widgets=[
                WidgetSpec(type="status", title="Threat level",
                           data_source="threat_level", span=3),
                WidgetSpec(type="stat", title="Open alerts (24h)",
                           data_source="alert_open", span=3),
                WidgetSpec(type="bar", title="Alerts by severity (7d)",
                           data_source="alert_severity", span=6),
                WidgetSpec(type="log", title="Recent alerts",
                           data_source="alert_log", span=12),
                WidgetSpec(type="table", title="Scans, beacons and threat hits",
                           data_source="detection_table", span=8),
                WidgetSpec(type="kv", title="Collection modes",
                           data_source="ids_modes", span=4),
            ],
            alarm_rules=[
                AlarmRuleSpec(
                    key="port_scan_detected", name="Port scan against this machine",
                    description="A single remote address probing many ports here, "
                                "either in a burst or slowly over hours.",
                    default_armed=True,
                ),
                AlarmRuleSpec(
                    key="arp_spoofing", name="ARP spoofing on the network",
                    description="One hardware address answering for several IPs — the "
                                "classic way to put yourself in the middle of a LAN. "
                                "Critical when the address claimed is the gateway.",
                    default_armed=True,
                ),
                AlarmRuleSpec(
                    key="rogue_dhcp", name="Unexpected DHCP server",
                    description="A second machine handing out leases takes over DNS and "
                                "the default route for every device that renews.",
                    default_armed=True,
                ),
                AlarmRuleSpec(
                    key="threat_intel_hit", name="Traffic to a listed address",
                    description="This machine connected to an address on a public threat "
                                "feed. Evidence, not proof — shared hosting gets listed.",
                    default_armed=True,
                ),
                AlarmRuleSpec(
                    key="beaconing", name="Beaconing to an external host",
                    description="Connections on a suspiciously exact interval, which is "
                                "what command-and-control looks like. Disarmed by default: "
                                "ordinary software polls on a timer too.",
                    default_armed=False,
                ),
                AlarmRuleSpec(
                    key="ids_alert", name="Alert from an external IDS",
                    description="Suricata or Zeek raised something on its own signatures.",
                    default_armed=True,
                ),
            ],
            tools=self._ids_tools(),
        )

    def _inventory_sensor(self) -> SensorDescriptor:
        return SensorDescriptor(
            id=INVENTORY_SENSOR,
            name="Network inventory",
            kind="network",
            widgets=[
                WidgetSpec(type="stat", title="Devices seen",
                           data_source="device_total", span=3),
                WidgetSpec(type="stat", title="Unrecognised",
                           data_source="device_unknown", span=3),
                WidgetSpec(type="donut", title="Open services",
                           data_source="service_spread", span=6),
                WidgetSpec(type="table", title="Devices",
                           data_source="device_table", span=12),
                WidgetSpec(type="log", title="Recently joined",
                           data_source="new_devices", span=6),
                WidgetSpec(type="table", title="Blocks",
                           data_source="block_table", span=6),
            ],
            alarm_rules=[
                AlarmRuleSpec(
                    key="new_unknown_device", name="Unrecognised device joined",
                    description="A device not previously seen, and not marked trusted, "
                                "appeared on the network.",
                    default_armed=True,
                ),
            ],
            tools=self._inventory_tools(),
        )

    def _posture_sensor(self) -> SensorDescriptor:
        return SensorDescriptor(
            id=POSTURE_SENSOR,
            name="Hardening posture",
            kind="security",
            widgets=[
                WidgetSpec(type="gauge", title="Hardening score",
                           data_source="posture_gauge", span=3),
                WidgetSpec(type="stat", title="Grade",
                           data_source="posture_grade", span=3),
                WidgetSpec(type="timeseries", title="Score over time",
                           data_source="posture_trend", span=6),
                WidgetSpec(type="table", title="What to fix, worst first",
                           data_source="posture_remediation", span=12),
            ],
            alarm_rules=[
                AlarmRuleSpec(
                    key="posture_regression", name="Hardening score dropped",
                    description=f"The overall grade fell by {REGRESSION_POINTS} points or "
                                "more between audits — something was turned off, or "
                                "something new was exposed.",
                    default_armed=True,
                ),
            ],
            tools=self._posture_tools(),
        )

    # --- tool declarations ----------------------------------------------------

    def _inventory_tools(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name="list_devices",
                description=(
                    "List the devices seen on the local network, most recently seen "
                    "first. Use this for 'what is on my network', 'who is connected', "
                    "or before naming a device. Names and vendor strings come from the "
                    "devices themselves and can be faked."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "trusted": {
                            "type": "boolean",
                            "description": "Only devices marked trusted (true) or only "
                                           "unrecognised ones (false). Omit for all.",
                        },
                        "seen_within_hours": {
                            "type": "number",
                            "description": "Only devices seen this recently. Omit for all.",
                        },
                        "limit": {"type": "integer", "description": "Default 100."},
                    },
                },
            ),
            ToolSpec(
                name="investigate_host",
                description=(
                    "Everything known about one device: addresses, vendor, when it was "
                    "first and last seen, which ports it currently has open, whether it "
                    "is on a threat feed, and whether this machine has been talking to "
                    "it. Accepts an IP, a MAC, or a name you have given a device. This "
                    "one actively probes the host, so it takes a few seconds."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "target": {"type": "string",
                                   "description": "IP address, MAC address, or device name."},
                    },
                    "required": ["target"],
                },
            ),
            ToolSpec(
                name="scan_now",
                description=(
                    "Sweep the network immediately instead of waiting for the next "
                    "scheduled scan. 'quick' finds which devices are present and returns "
                    "within the call. 'full' also port-scans every device and runs in the "
                    "background, so it returns straight away and results appear over the "
                    "following minutes."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "mode": {"type": "string", "enum": ["quick", "full"],
                                 "description": "Default 'quick'."},
                    },
                },
            ),
            ToolSpec(
                name="trust_device",
                description=(
                    "Give a device a name and mark it as known, so it stops being "
                    "reported as unrecognised. Use this when the owner identifies "
                    "something — 'that's the printer'."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "target": {"type": "string",
                                   "description": "IP, MAC, or current name."},
                        "name": {"type": "string",
                                 "description": "What to call it, in the owner's words."},
                    },
                    "required": ["target"],
                },
            ),
            ToolSpec(
                name="untrust_device",
                description=(
                    "Withdraw trust from a device so it counts as unrecognised again. "
                    "Its name and history are kept."
                ),
                parameters={
                    "type": "object",
                    "properties": {"target": {"type": "string"}},
                    "required": ["target"],
                },
            ),
            ToolSpec(
                name="forget_device",
                description=(
                    "Delete a device and its history entirely. It will be reported as new "
                    "if it appears again. Use for devices that are genuinely gone."
                ),
                parameters={
                    "type": "object",
                    "properties": {"target": {"type": "string"}},
                    "required": ["target"],
                },
            ),
        ]

    def _ids_tools(self) -> list[ToolSpec]:
        confirm_note = (
            "The block is staged and must then be confirmed with confirm_block."
            if self.cfg.block_mode == config.BLOCK_CONFIRM
            else "This applies the block immediately, with no confirmation step."
        )
        return [
            ToolSpec(
                name="list_connections",
                description=(
                    "The network connections this machine currently has open, with the "
                    "process behind each where it can be determined. Use for 'what is my "
                    "computer talking to'."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "external_only": {"type": "boolean",
                                          "description": "Skip connections to the local "
                                                         "network. Default true."},
                        "limit": {"type": "integer", "description": "Default 50."},
                    },
                },
            ),
            ToolSpec(
                name="check_destination",
                description=(
                    "Look up one address or hostname against the loaded threat feeds and "
                    "against this machine's own connection history. Use before deciding "
                    "whether traffic to somewhere is a problem."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "target": {"type": "string",
                                   "description": "IP address or hostname."},
                    },
                    "required": ["target"],
                },
            ),
            ToolSpec(
                name="acknowledge_alert",
                description=(
                    "Mark one alert as seen so it stops counting as open. Does not delete "
                    "it. Use when the owner has been told about it or it has been dealt "
                    "with."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "alert_id": {"type": "integer",
                                     "description": "The id from list of alerts."},
                    },
                    "required": ["alert_id"],
                },
            ),
            ToolSpec(
                name="block_device",
                description=(
                    "Cut a device off from this machine using the packet filter. "
                    f"{confirm_note} Important: this filters traffic to and from THIS "
                    "machine only — unless this machine is the router, the device stays "
                    "on the network and can still reach everything else on it. Blocking "
                    "the gateway or this machine's own address requires force=true "
                    "because it will take this machine off the network."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "target": {"type": "string",
                                   "description": "IP, MAC, or device name."},
                        "reason": {"type": "string",
                                   "description": "Why. Recorded and shown to the owner."},
                        "minutes": {"type": "integer",
                                    "description": "Release automatically after this long. "
                                                   "Omit for a block that stands until "
                                                   "released."},
                        "force": {"type": "boolean",
                                  "description": "Required to block the gateway or this "
                                                 "machine itself."},
                    },
                    "required": ["target", "reason"],
                },
            ),
            ToolSpec(
                name="confirm_block",
                description=(
                    "Apply a block that block_device staged. Call this only after the "
                    "owner has agreed, or when acting on an instruction that was already "
                    "explicit about cutting this device off."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "block_id": {"type": "integer",
                                     "description": "The id block_device returned."},
                    },
                    "required": ["block_id"],
                },
            ),
            ToolSpec(
                name="unblock_device",
                description="Release a block so the device can reach this machine again.",
                parameters={
                    "type": "object",
                    "properties": {
                        "target": {"type": "string",
                                   "description": "IP, MAC, or device name."},
                    },
                    "required": ["target"],
                },
            ),
            ToolSpec(
                name="list_blocks",
                description=(
                    "Every block, current and past, with why it was applied and whether "
                    "it actually reached the packet filter."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "state": {"type": "string",
                                  "enum": ["staged", "active", "released", "expired",
                                           "failed", "lapsed"],
                                  "description": "Omit for all."},
                    },
                },
            ),
        ]

    def _posture_tools(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name="hardening_report",
                description=(
                    "The security posture audit: a score out of 100, a letter grade, and "
                    "the specific things to fix in priority order. Scope 'host' is this "
                    "machine's own settings, 'lan' is what an attacker on the Wi-Fi would "
                    "find, 'router' is the gateway, 'overall' combines all three. Returns "
                    "the most recent audit; pass refresh=true to run a fresh one now."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "scope": {"type": "string",
                                  "enum": ["overall", "host", "lan", "router"],
                                  "description": "Default 'overall'."},
                        "refresh": {"type": "boolean",
                                    "description": "Run the checks again now instead of "
                                                   "returning the last result."},
                    },
                },
            ),
        ]

    # --- emitting --------------------------------------------------------------

    async def _report(self, finding: Finding) -> int | None:
        """Emit a finding, unless we have said the same thing recently.

        Without the suppression the scan loop re-reports the same open port
        every fifteen minutes, and triage learns to ignore the sensor -- which
        is a worse outcome than missing the repeat.
        """
        assert self.ctx is not None and self.store is not None
        if await self.store.recent_alert(finding.kind, finding.target, finding.quiet_minutes):
            return None
        event_id = await self.ctx.emit(Event(
            sensor_id=SENSOR_FOR_KIND.get(finding.kind, IDS_SENSOR),
            severity=finding.severity,
            kind=finding.kind,
            summary=finding.summary,
            sensor_text=finding.sensor_text,
            payload=finding.payload,
        ))
        await self.store.record_alert(
            finding.kind, finding.severity, finding.target, finding.summary,
            finding.sensor_text, finding.payload, event_id,
        )
        return event_id

    # --- the passive loop -------------------------------------------------------

    async def _passive_loop(self) -> None:
        # Every loop settles before its first pass, so that starting the plugin
        # is genuinely local work and nothing touches the network inside
        # `start()`'s timeout.
        await asyncio.sleep(BOOT_PASSIVE_DELAY)
        while True:
            try:
                await self._passive_pass()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.ctx.log.exception("passive pass failed")
            await asyncio.sleep(self.cfg.passive_interval)

    async def _passive_pass(self) -> None:
        assert self.ctx is not None and self.store is not None
        if not self._reconciled:
            await self._reconcile_blocks()
            self._reconciled = True

        self.gateway = await net.default_gateway() or self.gateway
        connections = await hostinfo.connections()
        await self.store.record_connections([
            (c.remote_ip, c.remote_port, c.local_port, c.process, c.state)
            for c in connections
        ])

        if time.monotonic() - self._window_started > WINDOW_RESET_SECONDS:
            self._window_ports.clear()
            self._window_started = time.monotonic()
        # Only ports we accept on: an outbound connection's local port is a
        # fresh ephemeral one each time, and counting those makes any busy
        # client look like a slow scan.
        listening = {item.port for item in await hostinfo.listeners()}
        for conn in connections:
            if conn.local_port in listening:
                self._window_ports.setdefault(conn.remote_ip, set()).add(conn.local_port)

        findings: list[Finding] = []
        findings += detect.portscan_findings(
            [(c.remote_ip, c.local_port, c.state) for c in connections],
            self._window_ports, listening,
        )

        pairs = await net.arp_pairs()
        seen = self.capture.drain() if self.cfg.capture else {}
        pairs += list(seen.get("arp_claims", []))
        findings += detect.arp_findings(pairs, self.gateway)
        findings += detect.dhcp_findings(seen.get("dhcp_servers", {}), self.gateway)
        findings += detect.sweep_findings(seen.get("syn_targets", {}))
        findings += await self._intel_findings(connections)
        findings += detect.beacon_findings(await self.store.beacon_candidates())

        for finding in findings:
            await self._report(finding)

        await self._expire_blocks()
        self.modes["Packet capture"] = (
            self.capture.mode() if self.cfg.capture else "disabled"
        )

    async def _intel_findings(self, connections: list[Any]) -> list[Finding]:
        assert self.store is not None
        hits: list[dict[str, Any]] = []
        for ip in {c.remote_ip for c in connections if hostinfo.is_external(c.remote_ip)}:
            sources = await self.store.intel_hits(ip)
            if not sources:
                continue
            match = next((c for c in connections if c.remote_ip == ip), None)
            hits.append({
                "ip": ip, "sources": sources,
                "port": match.remote_port if match else 0,
                "process": match.process if match else "",
                "samples": sum(1 for c in connections if c.remote_ip == ip),
            })
        return detect.intel_findings(hits)

    # --- the scan loop -----------------------------------------------------------

    async def _scan_loop(self) -> None:
        await asyncio.sleep(BOOT_SCAN_DELAY)
        while True:
            try:
                if self.cfg.feeds and await self.store.intel_stale(self.cfg.feed_refresh_hours):
                    summary = await intel.refresh(self.store, self.cfg.feeds)
                    self.ctx.log.info("threat feeds refreshed: %s", summary)
                await self._scan_pass()
                await self.store.prune()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.ctx.log.exception("scan pass failed")
            await asyncio.sleep(self.cfg.scan_interval)

    async def _subnets(self) -> list[str]:
        if self.cfg.subnets:
            return list(self.cfg.subnets)
        if not self.subnets:
            self.subnets = await net.local_subnets()
        return self.subnets

    async def _discover(self, budget: float = 0.0) -> list[net.Host]:
        """Every host on every subnet we watch, by whichever means works."""
        found: dict[str, net.Host] = {}
        use_nmap = self.cfg.nmap and net.nmap_available()
        for cidr in await self._subnets():
            hosts = (await net.nmap_discover(cidr, timeout=budget or 60.0)
                     if use_nmap else await net.sweep(cidr, budget=budget))
            if use_nmap and not hosts:
                hosts = await net.sweep(cidr, budget=budget)   # nmap unhappy; fall back
            for host in hosts:
                found.setdefault(host.ip, host)
        table = await net.arp_table()
        for ip, host in found.items():
            if not host.mac:
                host.mac = table.get(ip, "")
        return list(found.values())

    async def _scan_pass(self, quick: bool = False, budget: float = 0.0) -> dict[str, Any]:
        assert self.ctx is not None and self.store is not None
        if self.scanning:
            return {"skipped": "a scan is already running"}
        self.scanning = True
        try:
            return await self._do_scan(quick, budget)
        finally:
            self.scanning = False

    async def _do_scan(self, quick: bool, budget: float) -> dict[str, Any]:
        assert self.store is not None
        started = time.monotonic()
        hosts = await self._discover(budget=budget)
        names = await net.ssdp_probe(2.0) if not quick else {}

        new_devices = 0
        new_ports = 0
        baseline = await self.store.in_baseline_window(self.cfg.baseline_hours)

        for host in hosts:
            hostname = ""
            if not quick:
                hostname = await net.reverse_name(host.ip) or await net.netbios_name(host.ip)
            vendor = oui.vendor(host.mac)
            device, is_new = await self.store.upsert_device(
                host.ip, host.mac, hostname, vendor, oui.is_randomised(host.mac),
            )
            if is_new:
                new_devices += 1
                await self._report(self._device_finding(
                    device, host, hostname, vendor, names.get(host.ip, ""), baseline,
                ))

        if not quick:
            new_ports = await self._scan_ports_of(hosts, baseline)

        elapsed = round(time.monotonic() - started, 1)
        await self.ctx.emit(Event(
            sensor_id=INVENTORY_SENSOR, severity=SEVERITY_INFO, kind="scan_complete",
            summary=f"Swept {len(await self._subnets())} subnet(s): {len(hosts)} devices, "
                    f"{new_devices} new, in {elapsed}s",
            payload={"hosts": len(hosts), "new_devices": new_devices,
                     "new_ports": new_ports, "seconds": elapsed, "quick": quick},
        ))
        return {"devices": len(hosts), "new_devices": new_devices,
                "new_ports": new_ports, "seconds": elapsed}

    def _device_finding(
        self, device: Any, host: net.Host, hostname: str, vendor: str,
        advertised: str, baseline: bool,
    ) -> Finding:
        """A device we have not seen before.

        During the learning window everything is new, so the arrival is
        recorded at info rather than announced -- otherwise the first day is
        one long alarm and the owner turns the rule off.
        """
        evidence = "; ".join(filter(None, (
            f"hostname {hostname}" if hostname else "",
            f"vendor {vendor}" if vendor else "",
            f"advertised {advertised}" if advertised else "",
            f"mac {host.mac}" if host.mac else "no MAC (did not answer ARP)",
        )))
        return Finding(
            kind="device_seen" if baseline else "device_new",
            severity=SEVERITY_INFO if baseline else SEVERITY_MEDIUM,
            target=device.ip,
            summary=(f"Recorded {device.ip} while learning the network"
                     if baseline else
                     f"A device not seen before has joined the network at {device.ip}"),
            sensor_text=evidence or None,
            payload={"ip": device.ip, "mac": host.mac, "vendor": vendor,
                     "randomised": oui.is_randomised(host.mac), "baseline": baseline},
            quiet_minutes=1.0 if baseline else 60.0,
        )

    async def _scan_ports_of(self, hosts: list[net.Host], baseline: bool) -> int:
        """Port-scan every host, a few at a time, and report what opened."""
        assert self.store is not None
        semaphore = asyncio.Semaphore(HOST_CONCURRENCY)
        use_nmap = self.cfg.nmap and net.nmap_available()

        async def one(host: net.Host) -> int:
            async with semaphore:
                open_ports = await net.scan_ports(
                    host.ip, self.cfg.ports, budget=45.0,
                    concurrency=PER_HOST_PORT_CONCURRENCY,
                )
                if not open_ports:
                    return 0
                services = (await net.nmap_services(host.ip, tuple(open_ports))
                            if use_nmap else {})
                device = await self.store.find_device(host.ip)
                if device is None:
                    return 0
                opened = await self.store.record_ports(
                    device.id,
                    {port: services.get(port, config.service_name(port))
                     for port in open_ports},
                )
                if baseline:
                    return len(opened)
                for port in opened:
                    risky = port in config.RISKY_SERVICES
                    await self._report(Finding(
                        kind="port_opened",
                        severity=SEVERITY_HIGH if risky else SEVERITY_MEDIUM,
                        target=f"{host.ip}:{port}",
                        summary=(f"{device.display} has opened "
                                 f"{config.service_name(port)} (port {port})"
                                 + (f" — {config.RISKY_SERVICES[port]}" if risky else "")),
                        sensor_text=services.get(port) or None,
                        payload={"ip": host.ip, "port": port, "risky": risky},
                        quiet_minutes=1440.0,
                    ))
                return len(opened)

        results = await asyncio.gather(*(one(host) for host in hosts),
                                       return_exceptions=True)
        return sum(r for r in results if isinstance(r, int))

    # --- the posture loop ---------------------------------------------------------

    async def _posture_loop(self) -> None:
        await asyncio.sleep(BOOT_POSTURE_DELAY)
        while True:
            try:
                await self._audit()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.ctx.log.exception("hardening audit failed")
            await asyncio.sleep(POSTURE_INTERVAL)

    async def _audit(self) -> dict[str, posture.Report]:
        assert self.store is not None and self.ctx is not None
        # Read before writing: until this audit is saved, "latest" is the one
        # we are about to be compared against.
        previous = await self.store.latest_posture("overall")
        reports = await posture.full_report(self.store, self.cfg.ports)
        for scope, report in reports.items():
            await self.store.save_posture(
                scope, report.score, report.grade, [c.as_row() for c in report.checks]
            )

        overall = reports["overall"]
        if previous is not None and previous["score"] - overall.score >= REGRESSION_POINTS:
            worst = overall.failures[0] if overall.failures else None
            await self._report(Finding(
                kind="posture_regression",
                severity=SEVERITY_HIGH,
                target="overall",
                summary=(f"Hardening score fell from {previous['score']} to "
                         f"{overall.score} ({overall.grade})"
                         + (f" — worst open item: {worst.title}" if worst else "")),
                payload={"score": overall.score, "previous": previous["score"],
                         "grade": overall.grade,
                         "failures": [c.key for c in overall.failures[:10]]},
                quiet_minutes=180.0,
            ))
        return reports

    # --- blocks ---------------------------------------------------------------------

    async def _active_block_ips(self) -> list[str]:
        assert self.store is not None
        return [row["ip"] for row in await self.store.blocks(state="active") if row["ip"]]

    async def _reconcile_blocks(self) -> None:
        """Make the packet filter agree with the database after a restart."""
        assert self.store is not None
        await self.store.drop_stale_staged()
        wanted = await self._active_block_ips()
        if not wanted:
            return
        outcome = await firewall.sync(wanted)
        self.ctx.log.info("reconciled %d block(s): %s", len(wanted), outcome.detail)

    async def _expire_blocks(self) -> None:
        assert self.store is not None
        expired = await self.store.expired_blocks()
        if not expired:
            return
        for row in expired:
            await self.store.mark_block(row["id"], "expired", "reached its time limit")
        await firewall.sync(await self._active_block_ips())
        for row in expired:
            await self.ctx.emit(Event(
                sensor_id=IDS_SENSOR, severity=SEVERITY_MEDIUM, kind="block_expired",
                summary=f"The block on {row['ip']} has expired and been released",
                payload={"ip": row["ip"], "block_id": row["id"]},
            ))

    async def _apply_block(self, block_id: int) -> dict[str, Any]:
        assert self.store is not None and self.ctx is not None
        row = await self.store.block(block_id)
        if row is None:
            return {"error": f"no block with id {block_id}"}
        if row["state"] == "active":
            return {"ok": True, "already_active": True, "block_id": block_id,
                    "caveat": firewall.CAVEAT}
        if row["state"] not in {"staged", "failed"}:
            return {"error": f"block {block_id} is {row['state']}, not waiting to be applied"}

        outcome = await firewall.sync([*await self._active_block_ips(), row["ip"]])
        if not outcome.ok:
            await self.store.mark_block(block_id, "failed", outcome.detail)
            # An unprivileged process is an environment fact, not a plugin bug.
            return {"error": f"could not apply the block: {outcome.detail}",
                    "block_id": block_id, "state": "failed"}

        await self.store.mark_block(block_id, "active", outcome.detail)
        await self.ctx.emit(Event(
            sensor_id=IDS_SENSOR, severity=SEVERITY_CRITICAL, kind="device_blocked",
            summary=f"{row['ip']} has been cut off from this machine: {row['reason']}",
            payload={"ip": row["ip"], "mac": row["mac"], "reason": row["reason"],
                     "block_id": block_id, "backend": outcome.backend,
                     "expires_at": row["expires_at"]},
        ))
        return {"ok": True, "block_id": block_id, "state": "active",
                "ip": row["ip"], "backend": outcome.backend,
                "expires_at": row["expires_at"], "caveat": firewall.CAVEAT}

    # --- the IDS tail ------------------------------------------------------------------

    async def _ids_loop(self) -> None:
        assert self.tailer is not None
        while True:
            await asyncio.sleep(IDS_POLL)
            try:
                alerts = await asyncio.to_thread(self.tailer.read)
                for alert in alerts:
                    await self._report(Finding(
                        kind="ids_alert",
                        severity=alert.severity,
                        target=alert.source_ip or alert.dest_ip or "unknown",
                        summary=(f"{self.cfg.ids_log.rsplit('/', 1)[-1]} raised an alert "
                                 f"about {alert.source_ip or 'an unknown source'}"),
                        sensor_text=f"{alert.signature} [{alert.category}] "
                                    f"{alert.source_ip} -> {alert.dest_ip}:{alert.dest_port}",
                        payload={"source_ip": alert.source_ip, "dest_ip": alert.dest_ip,
                                 "dest_port": alert.dest_port},
                        quiet_minutes=5.0,
                    ))
                if alerts and self.store is not None:
                    await self.store.meta_set(IDS_OFFSET_KEY, self.tailer.state())
            except asyncio.CancelledError:
                raise
            except Exception:
                self.ctx.log.exception("reading %s failed", self.cfg.ids_log)

    # --- tool dispatch ------------------------------------------------------------------

    async def handle_command(self, cmd: str, **kwargs: Any) -> Any:
        """Route a tool call.

        Bad arguments from the model are caller error, not plugin failure:
        they come back as data so the model can correct itself and the health
        badge stays green.
        """
        if self.store is None:
            return {"error": "netguard is still starting up; try again in a moment"}
        handler = getattr(self, f"_cmd_{cmd}", None)
        if handler is None:
            return await super().handle_command(cmd, **kwargs)

        signature = inspect.signature(handler)
        allowed = set(signature.parameters)
        unexpected = sorted(set(kwargs) - allowed)
        if unexpected:
            return {"error": f"{cmd} does not take {', '.join(unexpected)}; "
                             f"it takes {', '.join(sorted(allowed)) or 'no arguments'}"}
        missing = sorted(
            name for name, param in signature.parameters.items()
            if param.default is inspect.Parameter.empty and name not in kwargs
        )
        if missing:
            return {"error": f"{cmd} needs {', '.join(missing)}"}
        return await handler(**kwargs)

    # --- inventory tools -------------------------------------------------------------

    def _device_dict(self, device: Any) -> dict[str, Any]:
        return {
            "ip": device.ip,
            "mac": device.mac or None,
            "name": device.label or None,
            "trusted": device.trusted,
            "first_seen": device.first_seen,
            "last_seen": device.last_seen,
            # Everything the device said about itself, kept apart from what we
            # know about it. Any of it can be chosen by whoever owns the device.
            "reported_by_device": {
                "hostname": device.hostname or None,
                "vendor": device.vendor or None,
                "randomised_mac": device.randomised,
            },
        }

    async def _cmd_list_devices(
        self, trusted: bool | None = None, seen_within_hours: float | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        assert self.store is not None
        try:
            limit = max(1, min(500, int(limit)))
            hours = float(seen_within_hours) if seen_within_hours is not None else None
        except (TypeError, ValueError):
            return {"error": "limit and seen_within_hours must be numbers"}
        devices = await self.store.devices(trusted=trusted, seen_within_hours=hours,
                                           limit=limit)
        counts = await self.store.device_counts()
        return {
            "count": len(devices),
            "totals": counts,
            "devices": [self._device_dict(d) for d in devices],
            "note": "Hostnames and vendor strings come from the devices themselves "
                    "and can be set to anything.",
        }

    async def _cmd_investigate_host(self, target: str) -> dict[str, Any]:
        assert self.store is not None
        device = await self.store.find_device(target)
        ip = device.ip if device else _resolve(target)
        if not ip:
            return {"error": f"no device matching {target!r}, and it is not an address "
                             "or a resolvable name"}

        open_ports = await net.scan_ports(ip, self.cfg.ports, budget=ON_DEMAND_BUDGET / 2)
        hostname = await net.reverse_name(ip) or await net.netbios_name(ip)
        result: dict[str, Any] = {
            "ip": ip,
            "known": device is not None,
            "open_ports": [
                {"port": port, "service": config.service_name(port),
                 "risky": config.RISKY_SERVICES.get(port)}
                for port in open_ports
            ],
            "reported_by_device": {"hostname": hostname or None},
            "on_threat_feeds": await self.store.intel_hits(ip),
            "blocked": bool(await self.store.active_block_for(ip)),
            "note": "Hostnames, banners and vendor strings are supplied by the host "
                    "being investigated.",
        }
        if device is not None:
            result.update(self._device_dict(device))
            result["ip"] = ip
            result["known_ports"] = await self.store.ports_of(device.id)
        if hostinfo.is_external(ip):
            result["this_machine_talks_to_it"] = await self.store.peers_of(ip)
        return result

    async def _cmd_scan_now(self, mode: str = "quick") -> dict[str, Any]:
        mode = (mode or "quick").strip().lower()
        if mode not in {"quick", "full"}:
            return {"error": "mode must be 'quick' or 'full'"}
        if self.scanning:
            return {"error": "a scan is already running; wait for it to finish"}
        if mode == "full":
            self.tasks = [t for t in self.tasks if not t.done()]
            self.tasks.append(asyncio.create_task(self._scan_pass()))
            return {"started": True, "mode": "full",
                    "note": "Running in the background. Devices and open ports will "
                            "appear over the next few minutes."}
        result = await self._scan_pass(quick=True, budget=ON_DEMAND_BUDGET)
        return {"mode": "quick", **result}

    async def _cmd_trust_device(self, target: str, name: str = "") -> dict[str, Any]:
        assert self.store is not None
        device = await self.store.find_device(target)
        if device is None:
            return {"error": f"no device matching {target!r}"}
        label = (name or device.label or "").strip()
        await self.store.set_trust(device.id, True, label)
        return {"ok": True, "ip": device.ip, "name": label or None, "trusted": True}

    async def _cmd_untrust_device(self, target: str) -> dict[str, Any]:
        assert self.store is not None
        device = await self.store.find_device(target)
        if device is None:
            return {"error": f"no device matching {target!r}"}
        await self.store.set_trust(device.id, False)
        return {"ok": True, "ip": device.ip, "trusted": False}

    async def _cmd_forget_device(self, target: str) -> dict[str, Any]:
        assert self.store is not None
        device = await self.store.find_device(target)
        if device is None:
            return {"error": f"no device matching {target!r}"}
        await self.store.forget_device(device.id)
        return {"ok": True, "forgotten": device.ip,
                "note": "It will be reported as a new device if it appears again."}

    # --- IDS tools ---------------------------------------------------------------------

    async def _cmd_list_connections(
        self, external_only: bool = True, limit: int = 50,
    ) -> dict[str, Any]:
        try:
            limit = max(1, min(500, int(limit)))
        except (TypeError, ValueError):
            return {"error": "limit must be a number"}
        assert self.store is not None
        connections = await hostinfo.connections()
        if external_only:
            connections = [c for c in connections if hostinfo.is_external(c.remote_ip)]
        rows = []
        for conn in connections[:limit]:
            hits = await self.store.intel_hits(conn.remote_ip)
            rows.append({
                "remote": f"{conn.remote_ip}:{conn.remote_port}",
                "local_port": conn.local_port,
                "state": conn.state,
                "process": conn.process or None,
                "on_threat_feeds": hits or None,
            })
        return {"count": len(rows), "external_only": external_only, "connections": rows}

    async def _cmd_check_destination(self, target: str) -> dict[str, Any]:
        assert self.store is not None
        ip = _resolve(target)
        if not ip:
            return {"error": f"{target!r} is not an address and would not resolve"}
        sources = await self.store.intel_hits(ip)
        return {
            "target": target,
            "ip": ip,
            "on_threat_feeds": sources,
            "verdict": "listed" if sources else "not listed",
            "external": hostinfo.is_external(ip),
            "this_machine_talks_to_it": await self.store.peers_of(ip),
            "feeds_loaded": await self.store.intel_status(),
            "note": "A feed listing is evidence, not proof — shared hosting and CDN "
                    "addresses appear on aggregate blocklists routinely.",
        }

    async def _cmd_acknowledge_alert(self, alert_id: int) -> dict[str, Any]:
        assert self.store is not None
        try:
            alert_id = int(alert_id)
        except (TypeError, ValueError):
            return {"error": "alert_id must be a number"}
        if await self.store.acknowledge(alert_id):
            return {"ok": True, "alert_id": alert_id, "acknowledged": True}
        return {"error": f"no open alert with id {alert_id}"}

    # --- blocking -------------------------------------------------------------------

    async def _cmd_block_device(
        self, target: str, reason: str, minutes: int | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        assert self.store is not None
        device = await self.store.find_device(target)
        ip = device.ip if device else _resolve(target)
        if not ip:
            return {"error": f"no device matching {target!r}, and it is not an address"}
        if not firewall.valid_target(ip):
            return {"error": f"{ip} is not something that can be blocked"}

        # Blocking the gateway takes this machine off the network, and the
        # network is how anyone would undo it. Possible, but not by accident.
        own = {iface.ip for iface in await net.interfaces()}
        if not force and (ip == self.gateway or ip in own):
            what = "the network's gateway" if ip == self.gateway else "this machine itself"
            return {"error": f"{ip} is {what}. Blocking it will disconnect this machine "
                             "from the network. Pass force=true if that is genuinely "
                             "what is wanted."}

        try:
            ttl = int(minutes) if minutes is not None else self.cfg.block_ttl_minutes
        except (TypeError, ValueError):
            return {"error": "minutes must be a number"}

        block_id = await self.store.stage_block(
            ip, device.mac if device else "", str(reason)[:400], max(0, ttl),
        )
        if self.cfg.block_mode == config.BLOCK_IMMEDIATE:
            return {**await self._apply_block(block_id), "mode": "immediate"}

        can, why = await firewall.can_apply()
        return {
            "staged": True,
            "block_id": block_id,
            "would_block": ip,
            "reason": reason,
            "expires_after_minutes": ttl or None,
            "next_step": f"call netguard.confirm_block with block_id={block_id}",
            "will_work": can or why,
            "caveat": firewall.CAVEAT,
        }

    async def _cmd_confirm_block(self, block_id: int) -> dict[str, Any]:
        try:
            block_id = int(block_id)
        except (TypeError, ValueError):
            return {"error": "block_id must be a number"}
        return await self._apply_block(block_id)

    async def _cmd_unblock_device(self, target: str) -> dict[str, Any]:
        assert self.store is not None and self.ctx is not None
        device = await self.store.find_device(target)
        ip = device.ip if device else _resolve(target)
        if not ip:
            return {"error": f"no device matching {target!r}, and it is not an address"}
        row = await self.store.active_block_for(ip)
        if row is None:
            return {"error": f"{ip} is not currently blocked"}

        await self.store.mark_block(row["id"], "released", "released on request")
        outcome = await firewall.sync(await self._active_block_ips())
        await self.ctx.emit(Event(
            sensor_id=IDS_SENSOR, severity=SEVERITY_MEDIUM, kind="block_released",
            summary=f"The block on {ip} has been released",
            payload={"ip": ip, "block_id": row["id"]},
        ))
        return {"ok": True, "released": ip, "block_id": row["id"], "detail": outcome.detail}

    async def _cmd_list_blocks(self, state: str | None = None) -> dict[str, Any]:
        assert self.store is not None
        allowed = {"staged", "active", "released", "expired", "failed", "lapsed"}
        if state is not None and state not in allowed:
            return {"error": f"state must be one of {', '.join(sorted(allowed))}"}
        rows = await self.store.blocks(state=state)
        can, why = await firewall.can_apply()
        return {
            "count": len(rows),
            "blocks": rows,
            "mode": self.cfg.block_mode,
            "packet_filter": why if not can else f"{why}, ready",
            "caveat": firewall.CAVEAT,
        }

    # --- posture tool ---------------------------------------------------------------

    async def _cmd_hardening_report(
        self, scope: str = "overall", refresh: bool = False,
    ) -> dict[str, Any]:
        assert self.store is not None
        scope = (scope or "overall").strip().lower()
        if scope not in {"overall", "host", "lan", "router"}:
            return {"error": "scope must be one of overall, host, lan, router"}

        if refresh:
            try:
                await asyncio.wait_for(self._audit(), ON_DEMAND_BUDGET)
            except TimeoutError:
                return {"error": "the audit did not finish in time; it is still running "
                                 "in the background — ask again shortly"}

        run = await self.store.latest_posture(scope)
        if run is None:
            return {"error": "no audit has completed yet; pass refresh=true to run one"}
        failures = [f for f in run["findings"] if f["status"] == "fail"]
        unknown = [f for f in run["findings"] if f["status"] == "unknown"]
        return {
            "scope": scope,
            "score": run["score"],
            "grade": run["grade"],
            "checked_at": run["ts"],
            "passing": sum(1 for f in run["findings"] if f["status"] == "pass"),
            "failing": len(failures),
            "could_not_check": [{"title": f["title"], "why": f["detail"]} for f in unknown],
            "fix_these": [
                {"title": f["title"], "detail": f["detail"],
                 "how": f["remediation"], "severity": f["severity"]}
                for f in failures
            ],
        }

    # --- widgets -----------------------------------------------------------------------

    async def query(self, source: str, **kwargs: Any) -> Any:
        if self.store is None:
            return {"error": "starting up"}
        handler = getattr(self, f"_widget_{source}", None)
        if handler is None:
            return await super().query(source, **kwargs)
        return await handler()

    async def _widget_threat_level(self) -> dict[str, Any]:
        load = await self.store.open_alert_load()
        return {"state": _known_state(load["worst"])}

    async def _widget_alert_open(self) -> dict[str, Any]:
        load = await self.store.open_alert_load()
        return {"value": load["count"], "label": "unacknowledged"}

    async def _widget_alert_severity(self) -> list[dict[str, Any]]:
        names = {4: "critical", 3: "high", 2: "medium", 1: "low", 0: "info"}
        return [
            {"severity": names.get(row["severity"], str(row["severity"])),
             "alerts": row["n"]}
            for row in await self.store.alert_severity_spread()
        ]

    async def _widget_alert_log(self) -> list[dict[str, Any]]:
        return [
            {"when": row["ts"], "kind": row["kind"], "target": row["target"],
             "what": row["summary"]}
            for row in await self.store.alerts(limit=50)
        ]

    async def _widget_detection_table(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for kind in ("port_scan", "host_sweep", "arp_spoof", "rogue_dhcp",
                     "threat_hit", "beacon_suspected", "ids_alert"):
            for row in await self.store.alerts(limit=8, kind=kind):
                rows.append({
                    "Detection": kind.replace("_", " "),
                    "Target": row["target"],
                    "When": row["ts"],
                    "Seen": row["summary"][:90],
                    "Acknowledged": "yes" if row["acknowledged"] else "no",
                })
        return sorted(rows, key=lambda item: item["When"], reverse=True)[:40]

    async def _widget_ids_modes(self) -> dict[str, str]:
        modes = dict(self.modes)
        modes["Threat feed entries"] = f"{await self.store.intel_size():,}"
        modes["Subnets watched"] = ", ".join(await self._subnets()) or "none found"
        return modes

    async def _widget_device_total(self) -> dict[str, Any]:
        counts = await self.store.device_counts()
        return {"value": counts["total"], "label": f"{counts['active']} active now"}

    async def _widget_device_unknown(self) -> dict[str, Any]:
        counts = await self.store.device_counts()
        return {"value": counts["untrusted"], "label": "not yet named"}

    async def _widget_service_spread(self) -> list[dict[str, Any]]:
        return [
            {"service": config.service_name(row["port"]), "devices": row["devices"]}
            for row in await self.store.service_spread()
        ]

    async def _widget_device_table(self) -> list[dict[str, Any]]:
        rows = []
        for device in await self.store.devices(limit=200):
            ports = await self.store.ports_of(device.id)
            rows.append({
                "Name": device.label or device.hostname or "—",
                "Address": device.ip,
                "Hardware": device.mac or "—",
                "Vendor": device.vendor or "—",
                "Ports": ", ".join(str(p["port"]) for p in ports[:8]) or "—",
                "Known": "yes" if device.trusted else "no",
                "Last seen": device.last_seen,
            })
        return rows

    async def _widget_new_devices(self) -> list[dict[str, Any]]:
        rows = []
        for kind in ("device_new", "device_seen"):
            for row in await self.store.alerts(limit=25, kind=kind):
                rows.append({"when": row["ts"], "address": row["target"],
                             "what": row["summary"]})
        return sorted(rows, key=lambda item: item["when"], reverse=True)[:25]

    async def _widget_block_table(self) -> list[dict[str, Any]]:
        return [
            {"Address": row["ip"], "State": row["state"], "Why": row["reason"][:60],
             "Applied": row["applied_at"] or "—", "Expires": row["expires_at"] or "—"}
            for row in await self.store.blocks(limit=25)
        ]

    async def _widget_posture_gauge(self) -> dict[str, Any]:
        run = await self.store.latest_posture("overall")
        return {"value": run["score"] if run else 0,
                "label": run["grade"] if run else "not audited yet"}

    async def _widget_posture_grade(self) -> dict[str, Any]:
        run = await self.store.latest_posture("overall")
        if run is None:
            return {"value": "—", "label": "no audit yet"}
        failing = sum(1 for f in run["findings"] if f["status"] == "fail")
        return {"value": run["grade"], "label": f"{failing} thing(s) to fix"}

    async def _widget_posture_trend(self) -> list[dict[str, Any]]:
        return await self.store.posture_trend()

    async def _widget_posture_remediation(self) -> list[dict[str, Any]]:
        run = await self.store.latest_posture("overall")
        if run is None:
            return []
        return [
            {"Area": f["scope"], "Check": f["title"], "Finding": f["detail"] or "—",
             "What to do": f["remediation"] or "—"}
            for f in run["findings"] if f["status"] == "fail"
        ]
