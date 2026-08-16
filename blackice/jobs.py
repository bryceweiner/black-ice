"""Due-time bookkeeping for periodic work.

Kept in the database rather than in memory so restarting the service neither
repeats a job nor skips its turn.
"""

from __future__ import annotations

from typing import Any

from . import db


async def due(job: str, interval_hours: float) -> bool:
    last = await db.fetchval("SELECT last_run FROM job_runs WHERE job = ?", (job,))
    if not last:
        return True
    return bool(await db.fetchval(
        "SELECT datetime(?, ?) <= datetime('now')",
        (last, f"+{interval_hours} hours"),
    ))


async def mark(job: str, detail: Any = None) -> None:
    await db.execute(
        """INSERT INTO job_runs (job, last_run, detail)
           VALUES (?, datetime('now'), ?)
           ON CONFLICT(job) DO UPDATE SET
             last_run = excluded.last_run, detail = excluded.detail""",
        (job, db.dumps(detail or {})),
    )


async def last_run(job: str) -> str | None:
    return await db.fetchval("SELECT last_run FROM job_runs WHERE job = ?", (job,))
