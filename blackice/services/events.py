from __future__ import annotations

from typing import Any

from .. import db
from ..bus import bus
from ..models import Event
from .listing import list_rows


async def record(event: Event) -> int:
    """Persist a sensor event and publish it. The single write path for events."""
    event_id = await db.execute(
        """INSERT INTO events (sensor_id, plugin, ts, severity, kind, summary,
                               sensor_text, payload)
           VALUES (?, ?, COALESCE(?, datetime('now')), ?, ?, ?, ?, ?)""",
        (
            event.sensor_id,
            event.plugin,
            event.ts.isoformat(sep=" ", timespec="seconds") if event.ts else None,
            event.severity,
            event.kind,
            event.summary,
            event.sensor_text,
            db.dumps(event.payload),
        ),
    )
    for m in event.media:
        await db.execute(
            """INSERT INTO event_media (event_id, path, mime, sha256, bytes, duration_ms)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (event_id, m.path, m.mime, m.sha256, m.bytes, m.duration_ms),
        )
    await db.execute(
        "UPDATE sensors SET last_seen = datetime('now') WHERE id = ?", (event.sensor_id,)
    )
    stored = await get(event_id)
    await bus.publish("event", stored)
    return event_id


async def get(event_id: int) -> dict[str, Any] | None:
    row = await db.fetchone("SELECT * FROM events WHERE id = ?", (event_id,))
    if row is None:
        return None
    row["payload"] = db.loads(row["payload"])
    row["media"] = await db.fetchall(
        "SELECT id, path, mime, bytes, duration_ms, pruned_at FROM event_media"
        " WHERE event_id = ? ORDER BY id",
        (event_id,),
    )
    return row


async def list_events(
    q: str | None = None,
    start: str | None = None,
    end: str | None = None,
    sensor_id: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    where, params = [], []
    if sensor_id:
        where.append("t.sensor_id = ?")
        params.append(sensor_id)
    return await list_rows(
        table="events",
        columns="t.id, t.sensor_id, t.plugin, t.ts, t.severity, t.kind,"
                " t.summary, t.tier, t.verdict",
        fts_table="events_fts",
        q=q, start=start, end=end, where=where, params=params,
        limit=limit, offset=offset,
    )


async def set_triage(event_id: int, tier: str, verdict: str) -> None:
    await db.execute(
        "UPDATE events SET tier = ?, verdict = ? WHERE id = ?", (tier, verdict, event_id)
    )
