"""Runs the daily review inside the API process.

Checks often, runs rarely: the due-time lives in the database, so restarting
the service neither repeats a review nor skips a day.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from .. import jobs
from ..config import get_settings
from ..memory import consolidate
from . import review as review_module

log = logging.getLogger("blackice.rsi.scheduler")

CHECK_INTERVAL_S = 900.0
MEMORY_JOB = "memory_consolidate"
STARTUP_GRACE_S = 60.0


class ReviewScheduler:
    def __init__(self, reviewer: review_module.DailyReview | None = None) -> None:
        self.reviewer = reviewer or review_module.review
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="rsi-daily-review")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _loop(self) -> None:
        # Let the service finish coming up before competing for the model.
        await asyncio.sleep(STARTUP_GRACE_S)
        while True:
            for job in (self._consolidate_memory, self._run_review):
                try:
                    await job()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # Neither job may take the monitoring service down, and a
                    # failure in one must not stop the other.
                    log.exception("%s raised", job.__name__)
            await asyncio.sleep(CHECK_INTERVAL_S)

    async def _consolidate_memory(self) -> None:
        """Turn recent conversation and event patterns into durable facts.

        Was previously only reachable through `blackice consolidate`, so a
        running system never actually consolidated anything.
        """
        s = get_settings()
        if not s.memory_enabled:
            return
        if not await jobs.due(MEMORY_JOB, s.memory_consolidate_hours):
            return
        log.info("consolidating memory")
        result = await consolidate.consolidate_all(s.memory_consolidate_hours)
        await jobs.mark(MEMORY_JOB, result)
        log.info("memory consolidation: %s event fact(s)", result.get("event_facts", 0))

    async def _run_review(self) -> None:
        s = get_settings()
        if not s.rsi_review_enabled or not await review_module.due(s.rsi_review_hours):
            return
        log.info("running the daily self-review")
        outcome = await self.reviewer.run()
        log.info(
            "self-review complete: %d edit(s) considered",
            len(outcome.get("edits", [])),
        )


scheduler = ReviewScheduler()
