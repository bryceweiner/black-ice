"""The plugin's private database: inventory, alerts, posture history, blocks.

One file per plugin, opened by core. Every timestamp is UTC in SQLite's own
`YYYY-MM-DD HH:MM:SS` text form so that `datetime('now')` comparisons work
without a conversion at either end.

Device identity is the MAC when we have one and the IP otherwise, because a MAC
survives a DHCP lease change and an IP does not. Randomised MACs make that
imperfect -- a phone with private Wi-Fi addressing is a new device every time it
associates -- which is why `oui.is_randomised` is recorded rather than hidden.
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass
from typing import Any

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    id         INTEGER PRIMARY KEY,
    key        TEXT NOT NULL UNIQUE,
    ip         TEXT NOT NULL DEFAULT '',
    mac        TEXT NOT NULL DEFAULT '',
    hostname   TEXT NOT NULL DEFAULT '',
    vendor     TEXT NOT NULL DEFAULT '',
    label      TEXT NOT NULL DEFAULT '',
    trusted    INTEGER NOT NULL DEFAULT 0,
    randomised INTEGER NOT NULL DEFAULT 0,
    first_seen TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen  TEXT NOT NULL DEFAULT (datetime('now')),
    notes      TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS devices_ip ON devices(ip);
CREATE INDEX IF NOT EXISTS devices_last_seen ON devices(last_seen);

CREATE TABLE IF NOT EXISTS device_ports (
    id         INTEGER PRIMARY KEY,
    device_id  INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    port       INTEGER NOT NULL,
    service    TEXT NOT NULL DEFAULT '',
    first_seen TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(device_id, port)
);

CREATE TABLE IF NOT EXISTS alerts (
    id           INTEGER PRIMARY KEY,
    ts           TEXT NOT NULL DEFAULT (datetime('now')),
    kind         TEXT NOT NULL,
    severity     INTEGER NOT NULL DEFAULT 0,
    target       TEXT NOT NULL DEFAULT '',
    summary      TEXT NOT NULL DEFAULT '',
    sensor_text  TEXT,
    payload      TEXT NOT NULL DEFAULT '{}',
    event_id     INTEGER,
    acknowledged INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS alerts_ts ON alerts(ts);
CREATE INDEX IF NOT EXISTS alerts_kind ON alerts(kind);

CREATE TABLE IF NOT EXISTS conn_samples (
    id          INTEGER PRIMARY KEY,
    ts          TEXT NOT NULL DEFAULT (datetime('now')),
    remote_ip   TEXT NOT NULL,
    remote_port INTEGER NOT NULL DEFAULT 0,
    local_port  INTEGER NOT NULL DEFAULT 0,
    process     TEXT NOT NULL DEFAULT '',
    state       TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS conn_remote ON conn_samples(remote_ip, ts);
CREATE INDEX IF NOT EXISTS conn_ts ON conn_samples(ts);

CREATE TABLE IF NOT EXISTS intel (
    ip     TEXT NOT NULL,
    source TEXT NOT NULL,
    PRIMARY KEY (ip, source)
);

CREATE TABLE IF NOT EXISTS intel_meta (
    source     TEXT PRIMARY KEY,
    url        TEXT NOT NULL DEFAULT '',
    fetched_at TEXT,
    entries    INTEGER NOT NULL DEFAULT 0,
    error      TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS posture_runs (
    id    INTEGER PRIMARY KEY,
    ts    TEXT NOT NULL DEFAULT (datetime('now')),
    scope TEXT NOT NULL,
    score INTEGER NOT NULL DEFAULT 0,
    grade TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS posture_runs_ts ON posture_runs(scope, ts);

CREATE TABLE IF NOT EXISTS posture_findings (
    id          INTEGER PRIMARY KEY,
    run_id      INTEGER NOT NULL REFERENCES posture_runs(id) ON DELETE CASCADE,
    key         TEXT NOT NULL,
    scope       TEXT NOT NULL,
    title       TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'fail',
    weight      INTEGER NOT NULL DEFAULT 1,
    severity    INTEGER NOT NULL DEFAULT 1,
    detail      TEXT NOT NULL DEFAULT '',
    remediation TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS blocks (
    id         INTEGER PRIMARY KEY,
    ts         TEXT NOT NULL DEFAULT (datetime('now')),
    ip         TEXT NOT NULL DEFAULT '',
    mac        TEXT NOT NULL DEFAULT '',
    reason     TEXT NOT NULL DEFAULT '',
    state      TEXT NOT NULL DEFAULT 'staged',
    applied_at TEXT,
    expires_at TEXT,
    ended_at   TEXT,
    detail     TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS blocks_state ON blocks(state);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);
"""

# A device seen only during the learning window is part of the furniture, not a
# discovery. `detect` uses this to decide whether "new" is worth an alert.
BASELINE_STARTED = "baseline_started"


@dataclass
class Device:
    id: int
    key: str
    ip: str
    mac: str
    hostname: str
    vendor: str
    label: str
    trusted: bool
    randomised: bool
    first_seen: str
    last_seen: str

    @property
    def display(self) -> str:
        """How this device is named in plugin-authored text.

        The label is the owner's own words, so it is safe to inline. The
        hostname and vendor are not, so they never appear here -- they travel
        as sensor text instead.
        """
        return f"{self.label} ({self.ip})" if self.label else self.ip


def _device(row: aiosqlite.Row) -> Device:
    return Device(
        id=row["id"], key=row["key"], ip=row["ip"], mac=row["mac"],
        hostname=row["hostname"], vendor=row["vendor"], label=row["label"],
        trusted=bool(row["trusted"]), randomised=bool(row["randomised"]),
        first_seen=row["first_seen"], last_seen=row["last_seen"],
    )


class Store:
    def __init__(self, db: aiosqlite.Connection) -> None:
        self.db = db

    async def setup(self) -> None:
        await self.db.executescript(SCHEMA)
        await self.db.commit()
        if await self.meta_get(BASELINE_STARTED) is None:
            await self.meta_set(BASELINE_STARTED, "now")
            await self.db.execute(
                "UPDATE meta SET value = datetime('now') WHERE key = ?", (BASELINE_STARTED,)
            )
            await self.db.commit()

    # --- meta --------------------------------------------------------------

    async def meta_get(self, key: str) -> str | None:
        cur = await self.db.execute("SELECT value FROM meta WHERE key = ?", (key,))
        row = await cur.fetchone()
        return row["value"] if row else None

    async def meta_set(self, key: str, value: str) -> None:
        await self.db.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        await self.db.commit()

    async def in_baseline_window(self, hours: float) -> bool:
        """True while the plugin is still learning what normal looks like."""
        if hours <= 0:
            return False
        cur = await self.db.execute(
            "SELECT (julianday('now') - julianday(value)) * 24 AS age"
            " FROM meta WHERE key = ?", (BASELINE_STARTED,),
        )
        row = await cur.fetchone()
        return bool(row) and row["age"] is not None and row["age"] < hours

    # --- devices -----------------------------------------------------------

    async def upsert_device(
        self, ip: str, mac: str = "", hostname: str = "",
        vendor: str = "", randomised: bool = False,
    ) -> tuple[Device, bool]:
        """Record a sighting. Returns the device and whether it is newly known."""
        key = mac or f"ip:{ip}"
        cur = await self.db.execute("SELECT * FROM devices WHERE key = ?", (key,))
        row = await cur.fetchone()
        if row is None and mac:
            # It had no MAC last time and does now: adopt the row rather than
            # forking the history of a device that never actually changed.
            cur = await self.db.execute(
                "SELECT * FROM devices WHERE key = ? AND mac = ''", (f"ip:{ip}",)
            )
            row = await cur.fetchone()
            if row is not None:
                await self.db.execute(
                    "UPDATE devices SET key = ?, mac = ? WHERE id = ?", (key, mac, row["id"])
                )

        if row is None:
            cur = await self.db.execute(
                "INSERT INTO devices (key, ip, mac, hostname, vendor, randomised)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (key, ip, mac, hostname, vendor, int(randomised)),
            )
            await self.db.commit()
            got = await self.db.execute("SELECT * FROM devices WHERE id = ?", (cur.lastrowid,))
            return _device(await got.fetchone()), True

        await self.db.execute(
            "UPDATE devices SET ip = ?, last_seen = datetime('now'),"
            " hostname = CASE WHEN ? != '' THEN ? ELSE hostname END,"
            " vendor = CASE WHEN ? != '' THEN ? ELSE vendor END,"
            " randomised = ? WHERE id = ?",
            (ip, hostname, hostname, vendor, vendor, int(randomised), row["id"]),
        )
        await self.db.commit()
        got = await self.db.execute("SELECT * FROM devices WHERE id = ?", (row["id"],))
        return _device(await got.fetchone()), False

    async def record_ports(self, device_id: int, ports: dict[int, str]) -> list[int]:
        """Store an open-port set. Returns the ports that were not open before."""
        cur = await self.db.execute(
            "SELECT port FROM device_ports WHERE device_id = ?", (device_id,)
        )
        known = {row["port"] for row in await cur.fetchall()}
        for port, service in ports.items():
            await self.db.execute(
                "INSERT INTO device_ports (device_id, port, service) VALUES (?, ?, ?)"
                " ON CONFLICT(device_id, port) DO UPDATE SET"
                " last_seen = datetime('now'),"
                " service = CASE WHEN excluded.service != '' THEN excluded.service"
                "                ELSE device_ports.service END",
                (device_id, port, service),
            )
        await self.db.commit()
        return sorted(set(ports) - known)

    async def ports_of(self, device_id: int) -> list[dict[str, Any]]:
        cur = await self.db.execute(
            "SELECT port, service, first_seen, last_seen FROM device_ports"
            " WHERE device_id = ? ORDER BY port", (device_id,),
        )
        return [dict(row) for row in await cur.fetchall()]

    async def devices(
        self, trusted: bool | None = None, seen_within_hours: float | None = None,
        limit: int = 500,
    ) -> list[Device]:
        clauses, params = ["1=1"], []
        if trusted is not None:
            clauses.append("trusted = ?")
            params.append(int(trusted))
        if seen_within_hours is not None:
            clauses.append("last_seen >= datetime('now', ?)")
            params.append(f"-{max(0.0, seen_within_hours)} hours")
        cur = await self.db.execute(
            f"SELECT * FROM devices WHERE {' AND '.join(clauses)}"
            " ORDER BY last_seen DESC LIMIT ?", (*params, limit),
        )
        return [_device(row) for row in await cur.fetchall()]

    async def find_device(self, target: str) -> Device | None:
        """Resolve an IP, a MAC, a label, or a hostname to one device."""
        target = (target or "").strip()
        if not target:
            return None
        cur = await self.db.execute(
            "SELECT * FROM devices WHERE ip = ? OR mac = ? OR key = ?"
            " OR lower(label) = lower(?) OR lower(hostname) = lower(?)"
            " ORDER BY last_seen DESC LIMIT 1",
            (target, target.lower(), target, target, target),
        )
        row = await cur.fetchone()
        if row is not None:
            return _device(row)
        cur = await self.db.execute(
            "SELECT * FROM devices WHERE lower(label) LIKE lower(?)"
            " OR lower(hostname) LIKE lower(?) ORDER BY last_seen DESC LIMIT 2",
            (f"%{target}%", f"%{target}%"),
        )
        rows = await cur.fetchall()
        return _device(rows[0]) if len(rows) == 1 else None

    async def set_trust(self, device_id: int, trusted: bool, label: str | None = None) -> None:
        if label is None:
            await self.db.execute(
                "UPDATE devices SET trusted = ? WHERE id = ?", (int(trusted), device_id)
            )
        else:
            await self.db.execute(
                "UPDATE devices SET trusted = ?, label = ? WHERE id = ?",
                (int(trusted), label, device_id),
            )
        await self.db.commit()

    async def forget_device(self, device_id: int) -> None:
        await self.db.execute("DELETE FROM device_ports WHERE device_id = ?", (device_id,))
        await self.db.execute("DELETE FROM devices WHERE id = ?", (device_id,))
        await self.db.commit()

    async def device_counts(self) -> dict[str, int]:
        cur = await self.db.execute(
            "SELECT count(*) AS total,"
            " sum(CASE WHEN trusted = 1 THEN 1 ELSE 0 END) AS trusted,"
            " sum(CASE WHEN last_seen >= datetime('now', '-1 hour') THEN 1 ELSE 0 END) AS active"
            " FROM devices"
        )
        row = await cur.fetchone()
        total = row["total"] or 0
        trusted = row["trusted"] or 0
        return {
            "total": total, "trusted": trusted, "untrusted": total - trusted,
            "active": row["active"] or 0,
        }

    async def service_spread(self) -> list[dict[str, Any]]:
        cur = await self.db.execute(
            "SELECT port, count(DISTINCT device_id) AS devices FROM device_ports"
            " GROUP BY port ORDER BY devices DESC, port LIMIT 12"
        )
        return [dict(row) for row in await cur.fetchall()]

    async def exposed(self, ports: tuple[int, ...]) -> list[dict[str, Any]]:
        """Devices offering any of these ports. Backs the attack-surface grade."""
        if not ports:
            return []
        marks = ",".join("?" * len(ports))
        cur = await self.db.execute(
            f"SELECT d.id, d.ip, d.mac, d.label, d.hostname, d.trusted, p.port, p.service"
            f" FROM device_ports p JOIN devices d ON d.id = p.device_id"
            f" WHERE p.port IN ({marks}) AND d.last_seen >= datetime('now', '-7 days')"
            f" ORDER BY p.port, d.ip", ports,
        )
        return [dict(row) for row in await cur.fetchall()]

    # --- alerts ------------------------------------------------------------

    async def record_alert(
        self, kind: str, severity: int, target: str, summary: str,
        sensor_text: str | None = None, payload: dict[str, Any] | None = None,
        event_id: int | None = None,
    ) -> int:
        cur = await self.db.execute(
            "INSERT INTO alerts (kind, severity, target, summary, sensor_text, payload, event_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (kind, severity, target, summary, sensor_text,
             json.dumps(payload or {}, default=str), event_id),
        )
        await self.db.commit()
        return int(cur.lastrowid or 0)

    async def recent_alert(self, kind: str, target: str, within_minutes: float) -> bool:
        """Has this exact thing already been said recently?

        Without this the scan loop re-reports the same open port every fifteen
        minutes and triage learns to ignore the sensor.
        """
        cur = await self.db.execute(
            "SELECT 1 FROM alerts WHERE kind = ? AND target = ?"
            " AND ts >= datetime('now', ?) LIMIT 1",
            (kind, target, f"-{max(0.0, within_minutes)} minutes"),
        )
        return await cur.fetchone() is not None

    async def alerts(
        self, limit: int = 50, kind: str | None = None,
        unacknowledged_only: bool = False, min_severity: int = 0,
    ) -> list[dict[str, Any]]:
        clauses, params = ["severity >= ?"], [min_severity]
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if unacknowledged_only:
            clauses.append("acknowledged = 0")
        cur = await self.db.execute(
            f"SELECT * FROM alerts WHERE {' AND '.join(clauses)}"
            f" ORDER BY id DESC LIMIT ?", (*params, limit),
        )
        return [dict(row) for row in await cur.fetchall()]

    async def acknowledge(self, alert_id: int) -> bool:
        cur = await self.db.execute(
            "UPDATE alerts SET acknowledged = 1 WHERE id = ? AND acknowledged = 0", (alert_id,)
        )
        await self.db.commit()
        return bool(cur.rowcount)

    async def alert_severity_spread(self, hours: float = 168.0) -> list[dict[str, Any]]:
        cur = await self.db.execute(
            "SELECT severity, count(*) AS n FROM alerts WHERE ts >= datetime('now', ?)"
            " GROUP BY severity ORDER BY severity DESC", (f"-{hours} hours",),
        )
        return [dict(row) for row in await cur.fetchall()]

    async def open_alert_load(self, hours: float = 24.0) -> dict[str, int]:
        cur = await self.db.execute(
            "SELECT count(*) AS n, coalesce(max(severity), -1) AS worst FROM alerts"
            " WHERE acknowledged = 0 AND ts >= datetime('now', ?)", (f"-{hours} hours",),
        )
        row = await cur.fetchone()
        return {"count": row["n"] or 0, "worst": row["worst"]}

    # --- connections -------------------------------------------------------

    async def record_connections(self, rows: list[tuple[str, int, int, str, str]]) -> None:
        if not rows:
            return
        await self.db.executemany(
            "INSERT INTO conn_samples (remote_ip, remote_port, local_port, process, state)"
            " VALUES (?, ?, ?, ?, ?)", rows,
        )
        await self.db.commit()

    async def peers(self, hours: float = 24.0, limit: int = 100) -> list[dict[str, Any]]:
        cur = await self.db.execute(
            "SELECT remote_ip, count(*) AS samples, max(ts) AS last_seen,"
            " group_concat(DISTINCT process) AS processes"
            " FROM conn_samples WHERE ts >= datetime('now', ?)"
            " GROUP BY remote_ip ORDER BY samples DESC LIMIT ?",
            (f"-{hours} hours", limit),
        )
        return [dict(row) for row in await cur.fetchall()]

    async def peers_of(self, ip: str, hours: float = 168.0) -> list[dict[str, Any]]:
        cur = await self.db.execute(
            "SELECT remote_port, count(*) AS samples, max(ts) AS last_seen, process"
            " FROM conn_samples WHERE remote_ip = ? AND ts >= datetime('now', ?)"
            " GROUP BY remote_port, process ORDER BY samples DESC LIMIT 25",
            (ip, f"-{hours} hours"),
        )
        return [dict(row) for row in await cur.fetchall()]

    async def beacon_candidates(
        self, min_samples: int = 8, hours: float = 24.0,
    ) -> list[dict[str, Any]]:
        """Peers we contact on a suspiciously regular clock.

        Command-and-control keeps time; a person does not. The measure is the
        coefficient of variation of the gaps between sightings -- low variance
        against a meaningful mean is the signature. Real chat and sync
        protocols also poll on a timer, so this is evidence, not a verdict,
        which is why the rule ships disarmed.
        """
        cur = await self.db.execute(
            "SELECT remote_ip, count(*) AS n FROM conn_samples"
            " WHERE ts >= datetime('now', ?) GROUP BY remote_ip HAVING n >= ?",
            (f"-{hours} hours", min_samples),
        )
        out: list[dict[str, Any]] = []
        for row in await cur.fetchall():
            times = await self.db.execute(
                "SELECT strftime('%s', ts) AS epoch, remote_port, process FROM conn_samples"
                " WHERE remote_ip = ? AND ts >= datetime('now', ?) ORDER BY ts",
                (row["remote_ip"], f"-{hours} hours"),
            )
            samples = await times.fetchall()
            stamps = sorted({int(s["epoch"]) for s in samples if s["epoch"]})
            if len(stamps) < min_samples:
                continue
            gaps = [b - a for a, b in zip(stamps, stamps[1:], strict=False) if b > a]
            if len(gaps) < min_samples - 1:
                continue
            mean = statistics.fmean(gaps)
            if mean < 20:      # chatty, not beaconing
                continue
            spread = statistics.pstdev(gaps) / mean if mean else 1.0
            out.append({
                "remote_ip": row["remote_ip"],
                "samples": len(stamps),
                "interval_seconds": round(mean, 1),
                "jitter": round(spread, 3),
                "process": next((s["process"] for s in samples if s["process"]), ""),
                "port": samples[0]["remote_port"] if samples else 0,
            })
        return sorted(out, key=lambda item: item["jitter"])

    async def prune(self, keep_days: int = 14) -> None:
        await self.db.execute(
            "DELETE FROM conn_samples WHERE ts < datetime('now', ?)", (f"-{keep_days} days",)
        )
        await self.db.execute(
            "DELETE FROM alerts WHERE ts < datetime('now', ?) AND acknowledged = 1",
            (f"-{keep_days * 4} days",),
        )
        await self.db.commit()

    # --- threat intelligence ------------------------------------------------

    async def replace_feed(self, source: str, url: str, ips: list[str], error: str = "") -> int:
        if not error:
            await self.db.execute("DELETE FROM intel WHERE source = ?", (source,))
            await self.db.executemany(
                "INSERT OR IGNORE INTO intel (ip, source) VALUES (?, ?)",
                [(ip, source) for ip in ips],
            )
        await self.db.execute(
            "INSERT INTO intel_meta (source, url, fetched_at, entries, error)"
            " VALUES (?, ?, datetime('now'), ?, ?)"
            " ON CONFLICT(source) DO UPDATE SET url = excluded.url,"
            " fetched_at = excluded.fetched_at,"
            " entries = CASE WHEN excluded.error = '' THEN excluded.entries"
            "                ELSE intel_meta.entries END,"
            " error = excluded.error",
            (source, url, len(ips), error),
        )
        await self.db.commit()
        return len(ips)

    async def intel_hits(self, ip: str) -> list[str]:
        cur = await self.db.execute("SELECT source FROM intel WHERE ip = ?", (ip,))
        return [row["source"] for row in await cur.fetchall()]

    async def intel_status(self) -> list[dict[str, Any]]:
        cur = await self.db.execute("SELECT * FROM intel_meta ORDER BY source")
        return [dict(row) for row in await cur.fetchall()]

    async def intel_stale(self, hours: float) -> bool:
        cur = await self.db.execute(
            "SELECT min(fetched_at) AS oldest FROM intel_meta WHERE error = ''"
        )
        row = await cur.fetchone()
        if row is None or not row["oldest"]:
            return True
        cur = await self.db.execute(
            "SELECT (julianday('now') - julianday(?)) * 24 AS age", (row["oldest"],)
        )
        age = (await cur.fetchone())["age"]
        return age is None or age >= hours

    async def intel_size(self) -> int:
        cur = await self.db.execute("SELECT count(*) AS n FROM intel")
        return (await cur.fetchone())["n"] or 0

    # --- posture ------------------------------------------------------------

    async def save_posture(
        self, scope: str, score: int, grade: str, findings: list[dict[str, Any]],
    ) -> int:
        cur = await self.db.execute(
            "INSERT INTO posture_runs (scope, score, grade) VALUES (?, ?, ?)",
            (scope, score, grade),
        )
        run_id = int(cur.lastrowid or 0)
        await self.db.executemany(
            "INSERT INTO posture_findings"
            " (run_id, key, scope, title, status, weight, severity, detail, remediation)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(run_id, f["key"], f["scope"], f["title"], f["status"], f["weight"],
              f["severity"], f.get("detail", ""), f.get("remediation", ""))
             for f in findings],
        )
        await self.db.commit()
        return run_id

    async def latest_posture(self, scope: str = "overall") -> dict[str, Any] | None:
        cur = await self.db.execute(
            "SELECT * FROM posture_runs WHERE scope = ? ORDER BY id DESC LIMIT 1", (scope,)
        )
        row = await cur.fetchone()
        if row is None:
            return None
        found = await self.db.execute(
            "SELECT * FROM posture_findings WHERE run_id = ?"
            " ORDER BY status = 'pass', severity DESC, weight DESC", (row["id"],),
        )
        return {**dict(row), "findings": [dict(f) for f in await found.fetchall()]}

    async def previous_posture(self, scope: str = "overall") -> dict[str, Any] | None:
        cur = await self.db.execute(
            "SELECT * FROM posture_runs WHERE scope = ? ORDER BY id DESC LIMIT 1 OFFSET 1",
            (scope,),
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def posture_trend(self, days: int = 30, scope: str = "overall") -> list[dict[str, Any]]:
        cur = await self.db.execute(
            "SELECT strftime('%Y-%m-%d %H:00', ts) AS bucket, round(avg(score)) AS score"
            " FROM posture_runs WHERE scope = ? AND ts >= datetime('now', ?)"
            " GROUP BY bucket ORDER BY bucket DESC LIMIT 168", (scope, f"-{days} days"),
        )
        return [dict(row) for row in await cur.fetchall()]

    # --- blocks -------------------------------------------------------------

    async def stage_block(
        self, ip: str, mac: str, reason: str, ttl_minutes: int, detail: str = "",
    ) -> int:
        expires = None
        if ttl_minutes > 0:
            cur = await self.db.execute(
                "SELECT datetime('now', ?) AS expires", (f"+{ttl_minutes} minutes",)
            )
            expires = (await cur.fetchone())["expires"]
        cur = await self.db.execute(
            "INSERT INTO blocks (ip, mac, reason, state, expires_at, detail)"
            " VALUES (?, ?, ?, 'staged', ?, ?)",
            (ip, mac, reason, expires, detail),
        )
        await self.db.commit()
        return int(cur.lastrowid or 0)

    async def block(self, block_id: int) -> dict[str, Any] | None:
        cur = await self.db.execute("SELECT * FROM blocks WHERE id = ?", (block_id,))
        row = await cur.fetchone()
        return dict(row) if row else None

    async def mark_block(self, block_id: int, state: str, detail: str = "") -> None:
        if state == "active":
            await self.db.execute(
                "UPDATE blocks SET state = 'active', applied_at = datetime('now'),"
                " detail = ? WHERE id = ?", (detail, block_id),
            )
        else:
            await self.db.execute(
                "UPDATE blocks SET state = ?, ended_at = datetime('now'),"
                " detail = CASE WHEN ? != '' THEN ? ELSE detail END WHERE id = ?",
                (state, detail, detail, block_id),
            )
        await self.db.commit()

    async def blocks(self, state: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        if state:
            cur = await self.db.execute(
                "SELECT * FROM blocks WHERE state = ? ORDER BY id DESC LIMIT ?", (state, limit)
            )
        else:
            cur = await self.db.execute(
                "SELECT * FROM blocks ORDER BY id DESC LIMIT ?", (limit,)
            )
        return [dict(row) for row in await cur.fetchall()]

    async def active_block_for(self, ip: str) -> dict[str, Any] | None:
        cur = await self.db.execute(
            "SELECT * FROM blocks WHERE ip = ? AND state = 'active' ORDER BY id DESC LIMIT 1",
            (ip,),
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def expired_blocks(self) -> list[dict[str, Any]]:
        cur = await self.db.execute(
            "SELECT * FROM blocks WHERE state = 'active' AND expires_at IS NOT NULL"
            " AND expires_at <= datetime('now')"
        )
        return [dict(row) for row in await cur.fetchall()]

    async def drop_stale_staged(self, minutes: int = 30) -> None:
        await self.db.execute(
            "UPDATE blocks SET state = 'lapsed', ended_at = datetime('now')"
            " WHERE state = 'staged' AND ts < datetime('now', ?)", (f"-{minutes} minutes",)
        )
        await self.db.commit()
