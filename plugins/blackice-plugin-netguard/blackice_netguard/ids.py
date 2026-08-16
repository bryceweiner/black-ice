"""Reading someone else's IDS.

If Suricata or Zeek is already running on this network, its alerts are better
than anything this plugin can infer from the ARP cache, and the right thing to
do is carry them onto the timeline rather than compete with them.

The tailer is offset-based and rotation-aware. Everything in an alert -- the
signature name, the category, the addresses -- is written by a third-party
process about traffic an attacker shaped, so all of it is untrusted text.
"""

from __future__ import annotations

import contextlib
import json
import os
from dataclasses import dataclass

from blackice.models import (
    SEVERITY_HIGH,
    SEVERITY_LOW,
    SEVERITY_MEDIUM,
)

MAX_LINES_PER_PASS = 400   # a busy sensor should not be able to flood the loop
MAX_LINE_BYTES = 64 * 1024


@dataclass
class IdsAlert:
    signature: str
    category: str
    severity: int
    source_ip: str
    dest_ip: str
    dest_port: int
    raw: dict


# Suricata numbers its priorities 1 (worst) to 4. Ours run the other way.
SURICATA_SEVERITY = {1: SEVERITY_HIGH, 2: SEVERITY_MEDIUM, 3: SEVERITY_LOW}


def parse_line(line: str) -> IdsAlert | None:
    """One JSON line from Suricata EVE or Zeek's JSON logs, or None."""
    line = line.strip()
    if not line or not line.startswith("{"):
        return None
    try:
        record = json.loads(line)
    except (ValueError, TypeError):
        return None
    if not isinstance(record, dict):
        return None

    if record.get("event_type") == "alert":
        alert = record.get("alert") or {}
        if not isinstance(alert, dict):
            return None
        return IdsAlert(
            signature=str(alert.get("signature", "") or "unnamed signature")[:200],
            category=str(alert.get("category", "") or "")[:120],
            severity=SURICATA_SEVERITY.get(alert.get("severity"), SEVERITY_MEDIUM),
            source_ip=str(record.get("src_ip", "") or "")[:64],
            dest_ip=str(record.get("dest_ip", "") or "")[:64],
            dest_port=_port(record.get("dest_port")),
            raw=record,
        )

    # Zeek notice.log in JSON mode.
    if "note" in record and "_path" not in record.get("event_type", ""):
        note = str(record.get("note", ""))
        if not note:
            return None
        return IdsAlert(
            signature=str(record.get("msg", "") or note)[:200],
            category=note[:120],
            severity=SEVERITY_MEDIUM,
            source_ip=str(record.get("src", "") or "")[:64],
            dest_ip=str(record.get("dst", "") or "")[:64],
            dest_port=_port(record.get("p")),
            raw=record,
        )
    return None


def _port(value: object) -> int:
    with contextlib.suppress(TypeError, ValueError):
        return int(value)  # type: ignore[arg-type]
    return 0


class Tailer:
    """Follows one append-only log across rotations."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.offset = 0
        self.inode = 0

    def state(self) -> str:
        return f"{self.inode}:{self.offset}"

    def restore(self, state: str | None) -> None:
        if not state or ":" not in state:
            return
        inode, _, offset = state.partition(":")
        with contextlib.suppress(ValueError):
            self.inode, self.offset = int(inode), int(offset)

    def read(self) -> list[IdsAlert]:
        """New alerts since the last call. Never raises."""
        try:
            stat = os.stat(self.path)
        except OSError:
            return []

        if stat.st_ino != self.inode:
            # First sight, or the log rotated under us. Start from where it is
            # now rather than replaying a week of history onto the timeline.
            self.inode = stat.st_ino
            self.offset = stat.st_size if self.offset == 0 else 0
        elif stat.st_size < self.offset:
            self.offset = 0

        alerts: list[IdsAlert] = []
        try:
            with open(self.path, encoding="utf-8", errors="replace") as handle:
                handle.seek(self.offset)
                # The offset only ever advances past a *complete* line. Reading
                # to `tell()` instead would step over the front half of a record
                # the sensor was still writing, and lose it for good.
                position = self.offset
                for _ in range(MAX_LINES_PER_PASS):
                    line = handle.readline(MAX_LINE_BYTES)
                    if not line:
                        break
                    if not line.endswith("\n"):
                        if len(line) >= MAX_LINE_BYTES:
                            # Absurdly long: skip it rather than stall here for
                            # ever waiting for a newline that is not coming.
                            handle.readline()
                            position = handle.tell()
                            continue
                        break  # a partial write; pick it up next pass
                    position = handle.tell()
                    alert = parse_line(line)
                    if alert is not None:
                        alerts.append(alert)
                self.offset = position
        except OSError:
            return alerts
        return alerts
