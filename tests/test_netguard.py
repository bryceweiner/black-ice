"""Network monitoring, intrusion detection, and the hardening grade."""

import asyncio

import pytest
from blackice_netguard import (
    IDS_SENSOR,
    INVENTORY_SENSOR,
    POSTURE_SENSOR,
    NetguardPlugin,
    detect,
    firewall,
    ids,
    intel,
    net,
    oui,
    posture,
)
from blackice_netguard import plugin as netguard
from blackice_netguard import settings as config
from blackice_netguard.posture import FAIL, PASS, UNKNOWN, Check

from blackice import db
from blackice.llm.tools import ToolRegistry, project_plugin_tools
from blackice.models import SEVERITY_CRITICAL, SEVERITY_HIGH, SEVERITY_INFO
from blackice.plugins.registry import Registry
from blackice.services import events

GATEWAY = "192.168.1.1"


@pytest.fixture(autouse=True)
def quiet_environment(monkeypatch):
    """Nothing in the suite is allowed to reach the network or the wire."""
    monkeypatch.setenv("BLACKICE_NETGUARD_FEEDS", "off")
    monkeypatch.setenv("BLACKICE_NETGUARD_CAPTURE", "off")
    monkeypatch.setenv("BLACKICE_NETGUARD_IDS_LOG", "off")
    monkeypatch.setenv("BLACKICE_NETGUARD_SUBNETS", "192.168.1.0/24")


@pytest.fixture
async def reg(data_dir):
    r = Registry()
    await r.start_plugin(NetguardPlugin, events.record)
    yield r
    await r.stop_all()


def plugin_of(reg):
    return reg.supervisors["netguard"].plugin


def healthy(reg):
    return reg.supervisors["netguard"].health()["state"] == "healthy"


async def events_of(kind):
    return await db.fetchall("SELECT * FROM events WHERE kind = ? ORDER BY id", (kind,))


# --- discovery and projection ------------------------------------------------

async def test_discovery_finds_installed_plugin(data_dir):
    assert "netguard" in [c.name for c in Registry().discover()]


async def test_start_projects_all_three_sensors(reg, data_dir):
    ids_seen = [r["id"] for r in await db.fetchall("SELECT id FROM sensors ORDER BY id")]
    assert {INVENTORY_SENSOR, IDS_SENSOR, POSTURE_SENSOR} <= set(ids_seen)


async def test_alarm_rules_are_projected_with_their_arm_state(reg, data_dir):
    rows = await db.fetchall(
        "SELECT r.key, s.armed FROM alarm_rules r"
        " JOIN alarm_state s ON s.rule_id = r.id WHERE r.plugin = 'netguard'"
    )
    armed = {row["key"]: row["armed"] for row in rows}

    assert {"new_unknown_device", "port_scan_detected", "arp_spoofing", "rogue_dhcp",
            "threat_intel_hit", "beaconing", "posture_regression"} <= set(armed)
    assert armed["arp_spoofing"] == 1
    # Ordinary software polls on a timer, so this one waits to be switched on.
    assert armed["beaconing"] == 0


async def test_describe_never_raises_before_start():
    """It is called on every projection and cannot be timed out."""
    assert len(NetguardPlugin().describe()) == 3


async def test_every_tool_reaches_the_llm(reg):
    tools = ToolRegistry()
    project_plugin_tools(reg, tools)

    assert {
        "netguard.list_devices", "netguard.investigate_host", "netguard.list_connections",
        "netguard.scan_now", "netguard.hardening_report", "netguard.check_destination",
        "netguard.trust_device", "netguard.untrust_device", "netguard.forget_device",
        "netguard.acknowledge_alert", "netguard.block_device", "netguard.confirm_block",
        "netguard.unblock_device", "netguard.list_blocks",
    } <= set(tools.tools)
    assert (await tools.dispatch("netguard.list_devices", {}))["count"] == 0


async def test_every_widget_data_source_answers(reg, monkeypatch):
    monkeypatch.setattr(net, "local_subnets", _returning(["192.168.1.0/24"]))
    sources = [
        w.data_source
        for sensor in reg.descriptors() if sensor.id.startswith("netguard.")
        for w in sensor.widgets
    ]
    assert len(sources) == 16

    for source in sources:
        result = await reg.query("netguard", source)
        assert result is not None, source
    assert healthy(reg)


# --- inventory ----------------------------------------------------------------

def _returning(value):
    async def fake(*args, **kwargs):
        return value
    return fake


async def test_a_device_is_recorded_named_and_forgotten(reg):
    store = plugin_of(reg).store
    await store.upsert_device("192.168.1.50", "b8:27:eb:11:22:33", "pi.local", "Raspberry Pi")

    listed = await reg.command("netguard", "list_devices")
    assert listed["count"] == 1
    assert listed["devices"][0]["ip"] == "192.168.1.50"
    assert listed["devices"][0]["trusted"] is False
    # Device-supplied strings are kept apart from what we established ourselves.
    assert listed["devices"][0]["reported_by_device"]["hostname"] == "pi.local"

    named = await reg.command("netguard", "trust_device",
                              target="192.168.1.50", name="the pi")
    assert named["trusted"] is True

    # And the name is now a way to refer to it.
    assert (await reg.command("netguard", "untrust_device", target="the pi"))["trusted"] is False
    assert (await reg.command("netguard", "forget_device", target="the pi"))["ok"] is True
    assert (await reg.command("netguard", "list_devices"))["count"] == 0


async def test_unknown_device_is_an_error_not_a_plugin_failure(reg):
    result = await reg.command("netguard", "trust_device", target="10.9.9.9")

    assert "no device matching" in result["error"]
    assert healthy(reg)


async def test_an_argument_the_tool_does_not_take_is_refused(reg):
    result = await reg.command("netguard", "list_devices", colour="blue")

    assert "does not take colour" in result["error"]
    assert healthy(reg)


async def test_a_missing_required_argument_is_refused(reg):
    result = await reg.command("netguard", "trust_device")

    assert result["error"] == "trust_device needs target"
    assert healthy(reg)


async def test_a_scan_records_devices_and_announces_the_new_ones(reg, monkeypatch):
    monkeypatch.setenv("BLACKICE_NETGUARD_BASELINE_HOURS", "0")  # done learning
    plug = plugin_of(reg)
    plug.cfg = config.load()

    monkeypatch.setattr(net, "nmap_available", lambda: False)
    monkeypatch.setattr(net, "sweep", _returning([
        net.Host(ip="192.168.1.50", mac="b8:27:eb:11:22:33"),
        net.Host(ip="192.168.1.51", mac="ac:cf:23:aa:bb:cc"),
    ]))
    monkeypatch.setattr(net, "arp_table", _returning({}))
    monkeypatch.setattr(net, "ssdp_probe", _returning({}))
    monkeypatch.setattr(net, "reverse_name", _returning("printer.local"))
    monkeypatch.setattr(net, "netbios_name", _returning(""))
    monkeypatch.setattr(net, "scan_ports", _returning([23, 443]))
    monkeypatch.setattr(net, "nmap_services", _returning({}))

    result = await plug._scan_pass()

    assert result["devices"] == 2
    assert result["new_devices"] == 2
    assert (await reg.command("netguard", "list_devices"))["count"] == 2

    arrivals = await events_of("device_new")
    assert len(arrivals) == 2
    assert arrivals[0]["sensor_id"] == INVENTORY_SENSOR
    # The hostname and vendor came off the network, so they are sensor text and
    # never part of the summary the plugin wrote itself.
    assert "printer.local" not in arrivals[0]["summary"]
    assert "printer.local" in arrivals[0]["sensor_text"]

    # Telnet is called out by name and raised above an ordinary open port.
    opened = await events_of("port_opened")
    telnet = [e for e in opened if ":23" in e["summary"] or "telnet" in e["summary"]]
    assert telnet and telnet[0]["severity"] == SEVERITY_HIGH
    assert healthy(reg)


async def test_during_the_learning_window_arrivals_are_recorded_not_announced(reg, monkeypatch):
    plug = plugin_of(reg)   # default baseline window is 24h and start() just began it
    monkeypatch.setattr(net, "nmap_available", lambda: False)
    monkeypatch.setattr(net, "sweep", _returning([net.Host(ip="192.168.1.60", mac="")]))
    monkeypatch.setattr(net, "arp_table", _returning({}))

    await plug._scan_pass(quick=True)

    assert await events_of("device_new") == []
    seen = await events_of("device_seen")
    assert len(seen) == 1 and seen[0]["severity"] == SEVERITY_INFO


# --- detection ------------------------------------------------------------------

def test_one_mac_claiming_the_gateway_is_critical():
    findings = detect.arp_findings(
        [("192.168.1.1", "de:ad:be:ef:00:01"), ("192.168.1.77", "de:ad:be:ef:00:01")],
        gateway=GATEWAY,
    )

    assert len(findings) == 1
    assert findings[0].kind == "arp_spoof"
    assert findings[0].severity == SEVERITY_CRITICAL
    assert findings[0].payload["claims_gateway"] is True
    # The addresses came off the wire, so they are evidence, not our claim.
    assert "de:ad:be:ef:00:01" in findings[0].sensor_text


def test_a_host_with_two_addresses_is_not_ARP_spoofing():
    """A static lease alongside a DHCP one is ordinary, and must stay quiet."""
    assert detect.arp_findings(
        [("192.168.1.20", "aa:bb:cc:dd:ee:ff"), ("192.168.1.21", "aa:bb:cc:dd:ee:ff")],
        gateway=GATEWAY,
    ) == []


def test_two_macs_answering_for_one_address_is_a_conflict():
    findings = detect.arp_findings(
        [(GATEWAY, "aa:bb:cc:dd:ee:ff"), (GATEWAY, "11:22:33:44:55:66")], gateway=GATEWAY
    )

    assert [f.severity for f in findings] == [SEVERITY_CRITICAL]
    assert findings[0].payload["is_gateway"] is True


def test_half_open_connections_across_many_ports_are_a_port_scan():
    burst = [("203.0.113.9", port, "SYN_RCVD") for port in range(20, 20 + 6)]

    findings = detect.portscan_findings(burst)

    assert findings[0].kind == "port_scan"
    assert findings[0].payload["phase"] == "in_flight"
    assert len(findings[0].payload["ports"]) == 6


def test_a_slow_scan_is_caught_by_the_window_even_without_a_burst():
    findings = detect.portscan_findings(
        [], window_ports={"203.0.113.9": set(range(100, 120))},
        listening=set(range(100, 120)),
    )

    assert findings[0].payload["phase"] == "windowed"
    # Nothing was in flight, so the burst rule alone would have missed it.
    assert detect.portscan_findings([]) == []


def test_our_own_outbound_traffic_is_not_mistaken_for_a_scan():
    """The socket table does not say who dialled.

    A busy API client opens dozens of connections to one address, each from a
    fresh ephemeral local port. Counting those the way a scan is counted turns
    ordinary outbound traffic into a high-severity alert -- which is exactly
    what happened the first time this ran against a live network.
    """
    chatty = {"160.79.104.10": set(range(51000, 51040))}

    assert detect.portscan_findings([], window_ports=chatty, listening={8080, 8554}) == []
    # And the same shape against ports we really do serve is still a scan.
    assert detect.portscan_findings(
        [], window_ports={"160.79.104.10": {22, 80, 443, 8080, 8443, 9000}},
        listening={22, 80, 443, 8080, 8443, 9000},
    )[0].kind == "port_scan"


def test_a_half_open_burst_on_ports_we_do_not_serve_is_ignored():
    burst = [("203.0.113.9", port, "SYN_RCVD") for port in range(60000, 60006)]

    assert detect.portscan_findings(burst, listening={22}) == []


def test_a_regular_interval_is_a_beacon_and_a_ragged_one_is_not():
    regular = {"remote_ip": "198.51.100.5", "samples": 12, "interval_seconds": 300.0,
               "jitter": 0.01, "process": "unknown", "port": 443}
    ragged = {**regular, "remote_ip": "198.51.100.6", "jitter": 0.9}

    findings = detect.beacon_findings([regular, ragged])

    assert [f.target for f in findings] == ["198.51.100.5"]
    assert "5.0 minutes" in findings[0].summary


def test_a_dhcp_server_that_is_not_the_router_is_critical():
    findings = detect.dhcp_findings({"192.168.1.66": "de:ad:be:ef:00:01"}, gateway=GATEWAY)

    assert findings[0].kind == "rogue_dhcp"
    assert findings[0].severity == SEVERITY_CRITICAL


def test_the_router_alone_handing_out_leases_is_not_a_finding():
    assert detect.dhcp_findings({GATEWAY: "aa:bb:cc:dd:ee:ff"}, gateway=GATEWAY) == []


def test_one_source_touching_many_hosts_is_a_sweep():
    findings = detect.sweep_findings({"192.168.1.99": {f"192.168.1.{n}" for n in range(2, 40)}})

    assert findings[0].kind == "host_sweep"
    assert findings[0].payload["count"] == 38


def test_a_threat_feed_hit_names_the_feeds_as_evidence():
    findings = detect.intel_findings(
        [{"ip": "198.51.100.5", "sources": ["tor-exits"], "port": 9001, "process": "curl"}]
    )

    assert findings[0].severity == SEVERITY_HIGH
    assert "tor-exits" in findings[0].sensor_text
    assert "tor-exits" not in findings[0].summary


async def test_the_same_finding_is_not_reported_twice_in_a_row(reg):
    plug = plugin_of(reg)
    finding = detect.Finding(kind="port_scan", severity=SEVERITY_HIGH, target="203.0.113.9",
                             summary="probing", quiet_minutes=60.0)

    first = await plug._report(finding)
    second = await plug._report(finding)

    assert first is not None
    assert second is None          # suppressed, so triage keeps trusting the sensor
    assert len(await events_of("port_scan")) == 1


# --- blocking ---------------------------------------------------------------------

@pytest.fixture
def firewall_works(monkeypatch):
    applied: list[list[str]] = []

    async def fake_sync(ips):
        applied.append(list(ips))
        return firewall.Outcome(True, f"{len(ips)} blocked", "pf")

    monkeypatch.setattr(firewall, "sync", fake_sync)
    monkeypatch.setattr(firewall, "can_apply", _returning((True, "pf")))
    monkeypatch.setattr(net, "interfaces", _returning([]))
    return applied


async def test_a_block_is_staged_and_waits_for_confirmation(reg, firewall_works):
    staged = await reg.command("netguard", "block_device",
                               target="192.168.1.77", reason="port scanning us")

    assert staged["staged"] is True
    assert staged["would_block"] == "192.168.1.77"
    assert "confirm_block" in staged["next_step"]
    assert firewall.CAVEAT in staged["caveat"]
    assert firewall_works == []           # nothing has reached the packet filter yet

    applied = await reg.command("netguard", "confirm_block", block_id=staged["block_id"])

    assert applied["ok"] is True
    assert applied["state"] == "active"
    assert firewall_works == [["192.168.1.77"]]

    blocked = await events_of("device_blocked")
    assert len(blocked) == 1
    assert blocked[0]["severity"] == SEVERITY_CRITICAL
    assert "port scanning us" in blocked[0]["summary"]


async def test_immediate_mode_skips_the_confirmation_step(reg, firewall_works, monkeypatch):
    monkeypatch.setenv("BLACKICE_NETGUARD_BLOCK_MODE", "immediate")
    plug = plugin_of(reg)
    plug.cfg = config.load()

    result = await reg.command("netguard", "block_device",
                               target="192.168.1.77", reason="acting on its own")

    assert result["mode"] == "immediate"
    assert result["state"] == "active"
    assert firewall_works == [["192.168.1.77"]]


async def test_blocking_the_gateway_needs_to_be_asked_for_twice(reg, firewall_works):
    plug = plugin_of(reg)
    plug.gateway = GATEWAY

    refused = await reg.command("netguard", "block_device", target=GATEWAY, reason="testing")

    assert "gateway" in refused["error"]
    assert "force=true" in refused["error"]
    assert firewall_works == []
    assert healthy(reg)

    forced = await reg.command("netguard", "block_device", target=GATEWAY,
                               reason="testing", force=True)
    assert forced["staged"] is True


async def test_a_block_that_cannot_be_applied_reports_it_and_stays_healthy(reg, monkeypatch):
    monkeypatch.setattr(firewall, "sync",
                        _returning(firewall.Outcome(False, firewall.NO_PRIVILEGE)))
    monkeypatch.setattr(firewall, "can_apply", _returning((False, firewall.NO_PRIVILEGE)))
    monkeypatch.setattr(net, "interfaces", _returning([]))

    staged = await reg.command("netguard", "block_device",
                               target="192.168.1.77", reason="testing")
    result = await reg.command("netguard", "confirm_block", block_id=staged["block_id"])

    assert "could not apply" in result["error"]
    assert result["state"] == "failed"
    # An unprivileged process is a fact about the machine, not a broken plugin.
    assert healthy(reg)
    assert await events_of("device_blocked") == []


async def test_a_block_can_be_released(reg, firewall_works):
    staged = await reg.command("netguard", "block_device",
                               target="192.168.1.77", reason="testing")
    await reg.command("netguard", "confirm_block", block_id=staged["block_id"])

    released = await reg.command("netguard", "unblock_device", target="192.168.1.77")

    assert released["ok"] is True
    assert firewall_works[-1] == []       # the rule set is rewritten without it
    assert (await reg.command("netguard", "unblock_device",
                              target="192.168.1.77"))["error"].endswith("not currently blocked")


async def test_a_block_with_a_time_limit_expires_by_itself(reg, firewall_works):
    plug = plugin_of(reg)
    staged = await reg.command("netguard", "block_device", target="192.168.1.77",
                               reason="testing", minutes=5)
    await reg.command("netguard", "confirm_block", block_id=staged["block_id"])

    await plug.store.db.execute(
        "UPDATE blocks SET expires_at = datetime('now', '-1 minute') WHERE id = ?",
        (staged["block_id"],),
    )
    await plug.store.db.commit()
    await plug._expire_blocks()

    assert (await plug.store.block(staged["block_id"]))["state"] == "expired"
    assert firewall_works[-1] == []
    assert len(await events_of("block_expired")) == 1


# --- the hardening grade -------------------------------------------------------------

def test_a_check_that_could_not_run_is_left_out_of_the_score():
    graded = [Check("a", "host", "A", PASS, weight=5), Check("b", "host", "B", FAIL, weight=5)]

    assert posture.score_checks(graded) == (50, "C-")
    # Adding an unknown must not move the number in either direction.
    assert posture.score_checks([*graded, Check("c", "host", "C", UNKNOWN, weight=90)]) \
        == (50, "C-")


def test_weight_decides_how_much_a_failure_costs():
    assert posture.score_checks([
        Check("sip", "host", "SIP", FAIL, weight=5),
        Check("stealth", "host", "Stealth", PASS, weight=2),
    ])[0] == 29
    assert posture.grade_for(100) == "A+"
    assert posture.grade_for(0) == "F"


def test_no_checks_at_all_is_an_F_not_a_pass():
    assert posture.score_checks([]) == (0, "F")


def test_failures_come_back_worst_first():
    report = posture.Report("host", 50, "C-", [
        Check("minor", "host", "Minor", FAIL, weight=1, severity=1),
        Check("major", "host", "Major", FAIL, weight=5, severity=4),
        Check("fine", "host", "Fine", PASS, weight=5),
    ])

    assert [c.key for c in report.failures] == ["major", "minor"]


async def test_the_report_is_unavailable_until_an_audit_has_run(reg):
    result = await reg.command("netguard", "hardening_report")

    assert "no audit has completed yet" in result["error"]
    assert healthy(reg)


async def test_a_stored_audit_reads_back_with_its_remediation(reg):
    store = plugin_of(reg).store
    await store.save_posture("overall", 62, "C", [
        Check("firewall", "host", "Application firewall enabled", FAIL, 5, SEVERITY_HIGH,
              detail="inbound connections are unfiltered",
              remediation="Turn it on.").as_row(),
        Check("sip", "host", "System Integrity Protection", PASS, 5).as_row(),
        Check("wifi", "router", "Wi-Fi encryption", UNKNOWN, 5,
              detail="not on Wi-Fi").as_row(),
    ])

    report = await reg.command("netguard", "hardening_report", scope="overall")

    assert (report["score"], report["grade"]) == (62, "C")
    assert report["passing"] == 1 and report["failing"] == 1
    assert report["fix_these"][0]["how"] == "Turn it on."
    assert report["could_not_check"][0]["why"] == "not on Wi-Fi"


async def test_an_unknown_scope_is_refused(reg):
    result = await reg.command("netguard", "hardening_report", scope="everything")

    assert "scope must be one of" in result["error"]
    assert healthy(reg)


async def test_a_dropped_score_is_announced_once(reg, monkeypatch):
    plug = plugin_of(reg)
    await plug.store.save_posture("overall", 90, "A", [])

    async def fell(store, ports):
        return {"overall": posture.Report("overall", 70, "B-", [
            Check("firewall", "host", "Application firewall enabled", FAIL, 5, SEVERITY_HIGH),
        ])}

    monkeypatch.setattr(posture, "full_report", fell)
    await plug._audit()

    regressions = await events_of("posture_regression")
    assert len(regressions) == 1
    assert regressions[0]["sensor_id"] == POSTURE_SENSOR
    assert "90 to 70" in regressions[0]["summary"]
    assert "Application firewall" in regressions[0]["summary"]


# --- reading someone else's IDS -------------------------------------------------------

def test_a_suricata_alert_is_understood():
    line = ('{"event_type":"alert","src_ip":"203.0.113.9","dest_ip":"192.168.1.5",'
            '"dest_port":445,"alert":{"signature":"ET SCAN Nmap","category":"Attempted Recon",'
            '"severity":1}}')

    alert = ids.parse_line(line)

    assert alert.signature == "ET SCAN Nmap"
    assert alert.severity == SEVERITY_HIGH
    assert alert.source_ip == "203.0.113.9"
    assert alert.dest_port == 445


def test_lines_that_are_not_alerts_are_ignored():
    assert ids.parse_line('{"event_type":"flow","src_ip":"1.2.3.4"}') is None
    assert ids.parse_line("not json at all") is None
    assert ids.parse_line("") is None
    assert ids.parse_line("[1,2,3]") is None


def _alert_line(signature: str) -> str:
    return ('{"event_type":"alert","src_ip":"45.83.220.5","dest_ip":"10.0.0.1",'
            f'"alert":{{"signature":"{signature}","severity":2}}}}\n')


def test_the_tailer_reads_only_what_is_new_and_survives_a_rotation(tmp_path):
    log = tmp_path / "eve.json"
    log.write_text("")
    tailer = ids.Tailer(str(log))
    tailer.read()   # first sight: start at the end, do not replay history

    with open(log, "a") as handle:
        handle.write(_alert_line("one"))

    assert [a.signature for a in tailer.read()] == ["one"]
    assert tailer.read() == []              # nothing new

    # Rotated away and replaced: a different file, so start at its beginning.
    log.rename(tmp_path / "eve.json.1")
    log.write_text(_alert_line("two") + _alert_line("three"))
    assert [a.signature for a in tailer.read()] == ["two", "three"]

    # Truncated in place instead: same file, suddenly shorter than we had read.
    log.write_text(_alert_line("four"))
    assert [a.signature for a in tailer.read()] == ["four"]


def test_a_partial_line_is_left_for_the_next_pass(tmp_path):
    log = tmp_path / "eve.json"
    log.write_text("")
    tailer = ids.Tailer(str(log))
    tailer.read()

    with open(log, "a") as handle:
        handle.write('{"event_type":"alert","src_ip":"203.0.113.9",')

    assert tailer.read() == []
    with open(log, "a") as handle:
        handle.write('"alert":{"signature":"whole","severity":3}}\n')
    assert [a.signature for a in tailer.read()] == ["whole"]


# --- threat feeds -----------------------------------------------------------------------

def test_a_feed_is_parsed_and_private_space_is_thrown_away():
    text = "\n".join([
        "# a comment", "45.83.220.5", "185.220.101.1  # with a note",
        "192.168.1.1",      # a feed listing private space is broken or hostile
        "10.0.0.1", "127.0.0.1", "not an address", "",
    ])

    assert intel.parse_feed(text) == ["45.83.220.5", "185.220.101.1"]


async def test_a_feed_that_fails_is_recorded_against_that_feed_only(reg):
    store = plugin_of(reg).store
    await store.replace_feed("good", "https://example.invalid/a", ["203.0.113.9"])
    await store.replace_feed("bad", "https://example.invalid/b", [], error="HTTPError: 500")

    assert await store.intel_hits("203.0.113.9") == ["good"]
    status = {row["source"]: row["error"] for row in await store.intel_status()}
    assert status == {"good": "", "bad": "HTTPError: 500"}


async def test_check_destination_reports_a_listing_as_evidence(reg):
    await plugin_of(reg).store.replace_feed("tor-exits", "u", ["198.51.100.5"])

    result = await reg.command("netguard", "check_destination", target="198.51.100.5")

    assert result["verdict"] == "listed"
    assert result["on_threat_feeds"] == ["tor-exits"]
    assert "evidence, not proof" in result["note"]


async def test_a_destination_that_is_not_an_address_is_an_error(reg):
    result = await reg.command("netguard", "check_destination", target="not a host at all")

    assert "would not resolve" in result["error"]
    assert healthy(reg)


# --- storage ---------------------------------------------------------------------------

async def test_a_device_that_gains_a_mac_keeps_its_history(reg):
    """It answered no ARP the first time; that is not a second device."""
    store = plugin_of(reg).store
    first, was_new = await store.upsert_device("192.168.1.50")
    assert was_new

    second, was_new = await store.upsert_device("192.168.1.50", "b8:27:eb:11:22:33")

    assert was_new is False
    assert second.id == first.id
    assert second.mac == "b8:27:eb:11:22:33"
    assert len(await store.devices()) == 1


async def test_only_newly_opened_ports_are_returned(reg):
    store = plugin_of(reg).store
    device, _ = await store.upsert_device("192.168.1.50", "b8:27:eb:11:22:33")

    assert await store.record_ports(device.id, {22: "ssh", 443: "https"}) == [22, 443]
    assert await store.record_ports(device.id, {22: "ssh", 443: "https"}) == []
    assert await store.record_ports(device.id, {22: "ssh", 23: "telnet"}) == [23]


async def test_an_alert_can_be_acknowledged_exactly_once(reg):
    store = plugin_of(reg).store
    alert_id = await store.record_alert("port_scan", SEVERITY_HIGH, "203.0.113.9", "probing")

    assert (await reg.command("netguard", "acknowledge_alert",
                              alert_id=alert_id))["acknowledged"] is True
    repeat = await reg.command("netguard", "acknowledge_alert", alert_id=alert_id)

    assert repeat["error"] == f"no open alert with id {alert_id}"
    assert healthy(reg)
    assert (await store.open_alert_load())["count"] == 0


async def test_beacon_candidates_measure_regularity_not_volume(reg):
    store = plugin_of(reg).store
    for minute in range(12):
        await store.db.execute(
            "INSERT INTO conn_samples (ts, remote_ip, remote_port) "
            "VALUES (datetime('now', ?), '198.51.100.5', 443)", (f"-{minute * 5} minutes",)
        )
    for offset in (1, 2, 40, 41, 43, 90, 91, 200, 400, 401, 402, 403):
        await store.db.execute(
            "INSERT INTO conn_samples (ts, remote_ip, remote_port) "
            "VALUES (datetime('now', ?), '198.51.100.6', 443)", (f"-{offset} minutes",)
        )
    await store.db.commit()

    candidates = {row["remote_ip"]: row for row in await store.beacon_candidates()}

    assert candidates["198.51.100.5"]["jitter"] < detect.BEACON_MAX_JITTER
    assert candidates["198.51.100.6"]["jitter"] > detect.BEACON_MAX_JITTER
    assert candidates["198.51.100.5"]["interval_seconds"] == 300.0


# --- configuration ----------------------------------------------------------------------

def test_subnets_are_read_and_nonsense_is_dropped(monkeypatch):
    monkeypatch.setenv("BLACKICE_NETGUARD_SUBNETS",
                       "192.168.1.0/24, not-a-subnet, 10.0.0.0/8, 172.16.5.0/24")

    loaded = config.load()

    assert loaded.subnets == ("192.168.1.0/24", "172.16.5.0/24")   # /8 is too wide to sweep
    assert len(loaded.warnings) == 2


def test_an_unreadable_block_mode_falls_back_to_asking_first(monkeypatch):
    monkeypatch.setenv("BLACKICE_NETGUARD_BLOCK_MODE", "yolo")

    loaded = config.load()

    assert loaded.block_mode == config.BLOCK_CONFIRM
    assert "yolo" in loaded.warnings[0]


def test_block_mode_can_be_set_to_immediate(monkeypatch):
    monkeypatch.setenv("BLACKICE_NETGUARD_BLOCK_MODE", "immediate")
    assert config.load().block_mode == config.BLOCK_IMMEDIATE


def test_a_broken_interval_does_not_stop_the_plugin_loading(monkeypatch):
    monkeypatch.setenv("BLACKICE_NETGUARD_SCAN_INTERVAL", "soon")
    assert config.load().scan_interval == config.DEFAULT_SCAN_INTERVAL

    monkeypatch.setenv("BLACKICE_NETGUARD_SCAN_INTERVAL", "1")
    assert config.load().scan_interval == 60.0        # clamped, not obeyed


# --- hardware addresses -------------------------------------------------------------------

def test_mac_addresses_are_normalised_from_every_form_the_tools_print():
    assert oui.normalise("B8-27-EB-1-2-3") == "b8:27:eb:01:02:03"
    assert oui.normalise("b8:27:eb:11:22:33") == "b8:27:eb:11:22:33"
    assert oui.normalise("nonsense") == ""
    assert oui.normalise("") == ""


def test_a_randomised_address_is_named_as_one_rather_than_guessed_at():
    assert oui.is_randomised("b8:27:eb:11:22:33") is False
    assert oui.is_randomised("b6:27:eb:11:22:33") is True
    assert oui.vendor("b6:27:eb:11:22:33") == oui.LOCALLY_ADMINISTERED
    assert oui.vendor("b8:27:eb:11:22:33") == "Raspberry Pi"
    assert oui.vendor("00:00:00:00:00:01") == ""


# --- containment ------------------------------------------------------------------------

async def test_a_failing_background_pass_does_not_take_the_plugin_down(reg, monkeypatch):
    plug = plugin_of(reg)

    async def explode():
        raise RuntimeError("the network went away")

    monkeypatch.setattr(plug, "_passive_pass", explode)
    task = asyncio.create_task(plug._passive_loop())
    await asyncio.sleep(netguard.BOOT_PASSIVE_DELAY + 0.2)
    task.cancel()

    assert healthy(reg)


async def test_stopping_twice_is_safe(reg):
    plug = plugin_of(reg)

    await plug.stop()
    await plug.stop()

    assert plug.tasks == []
