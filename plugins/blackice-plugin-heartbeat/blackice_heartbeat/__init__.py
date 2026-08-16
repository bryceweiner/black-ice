"""Reference plugin. Deliberately trivial: it exists to prove the contract --
entry-point discovery, private SQLite, widget declaration, alarm rules, tools,
and supervisor isolation -- without any hardware.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from blackice.models import (
    SEVERITY_INFO,
    SEVERITY_MEDIUM,
    AlarmRuleSpec,
    Event,
    SensorDescriptor,
    ToolSpec,
    WidgetSpec,
)
from blackice.plugins.base import PluginContext, SensorPlugin

SENSOR_ID = "heartbeat.pulse"
INTERVAL = 30.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS beats (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL DEFAULT (datetime('now')),
    seq INTEGER NOT NULL
);
"""


class HeartbeatPlugin(SensorPlugin):
    name = "heartbeat"
    version = "0.1.0"

    def __init__(self) -> None:
        self.ctx: PluginContext | None = None
        self.task: asyncio.Task | None = None
        self.seq = 0
        self.failing = False

    async def start(self, ctx: PluginContext) -> None:
        self.ctx = ctx
        await ctx.db.executescript(SCHEMA)
        await ctx.db.commit()
        row = await (await ctx.db.execute("SELECT max(seq) FROM beats")).fetchone()
        self.seq = (row[0] or 0) if row else 0
        self.task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self.task:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
            self.task = None

    def describe(self) -> list[SensorDescriptor]:
        return [
            SensorDescriptor(
                id=SENSOR_ID,
                name="Heartbeat",
                kind="diagnostic",
                widgets=[
                    WidgetSpec(
                        type="stat", title="Beats recorded",
                        data_source="beat_count", span=3,
                    ),
                    WidgetSpec(
                        type="timeseries", title="Beats per minute",
                        data_source="beat_rate", span=9,
                    ),
                    WidgetSpec(
                        type="log", title="Recent beats",
                        data_source="recent_beats", span=12,
                    ),
                ],
                alarm_rules=[
                    AlarmRuleSpec(
                        key="missed_beat",
                        name="Heartbeat stopped",
                        description=f"No pulse recorded for more than {INTERVAL * 3:.0f}s",
                        default_armed=True,
                    )
                ],
                tools=[
                    ToolSpec(
                        name="beat_now",
                        description="Emit a heartbeat event immediately.",
                        parameters={"type": "object", "properties": {}},
                    ),
                    ToolSpec(
                        name="simulate_failure",
                        description="Make the plugin raise on its next call. Testing only.",
                        parameters={"type": "object", "properties": {}},
                    ),
                ],
            )
        ]

    async def handle_command(self, cmd: str, **kwargs: Any) -> Any:
        if self.failing:
            self.failing = False
            raise RuntimeError("simulated plugin failure")
        if cmd == "beat_now":
            return {"event_id": await self._beat(manual=True)}
        if cmd == "simulate_failure":
            self.failing = True
            return {"ok": True, "note": "next call will raise"}
        return await super().handle_command(cmd, **kwargs)

    async def query(self, source: str, **kwargs: Any) -> Any:
        assert self.ctx is not None
        db = self.ctx.db
        if source == "beat_count":
            row = await (await db.execute("SELECT count(*) FROM beats")).fetchone()
            return {"value": row[0] if row else 0, "label": "beats"}
        if source == "recent_beats":
            cur = await db.execute("SELECT ts, seq FROM beats ORDER BY id DESC LIMIT 50")
            return [dict(r) for r in await cur.fetchall()]
        if source == "beat_rate":
            cur = await db.execute(
                "SELECT strftime('%Y-%m-%d %H:%M', ts) AS bucket, count(*) AS n"
                " FROM beats GROUP BY bucket ORDER BY bucket DESC LIMIT 60"
            )
            return [dict(r) for r in await cur.fetchall()]
        return await super().query(source, **kwargs)

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(INTERVAL)
            try:
                await self._beat()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.ctx.log.exception("heartbeat emit failed")

    async def _beat(self, manual: bool = False) -> int:
        assert self.ctx is not None
        self.seq += 1
        await self.ctx.db.execute("INSERT INTO beats (seq) VALUES (?)", (self.seq,))
        await self.ctx.db.commit()
        return await self.ctx.emit(
            Event(
                sensor_id=SENSOR_ID,
                severity=SEVERITY_MEDIUM if manual else SEVERITY_INFO,
                kind="heartbeat",
                summary=f"Heartbeat {self.seq}" + (" (manual)" if manual else ""),
                payload={"seq": self.seq, "manual": manual},
            )
        )
