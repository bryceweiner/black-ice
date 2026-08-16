"""Runs the daily review inside the API process.

Checks often, runs rarely: the due-time lives in the database, so restarting
the service neither repeats a review nor skips a day.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from ..config import get_settings
from . import review as review_module

log = logging.getLogger("blackice.rsi.scheduler")

CHECK_INTERVAL_S = 900.0
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
            try:
                s = get_settings()
                if s.rsi_review_enabled and await review_module.due(s.rsi_review_hours):
                    log.info("running the daily self-review")
                    outcome = await self.reviewer.run()
                    log.info(
                        "self-review complete: %d edit(s) considered",
                        len(outcome.get("edits", [])),
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                # A failed review must never take the monitoring service down.
                log.exception("daily review raised")
            await asyncio.sleep(CHECK_INTERVAL_S)


scheduler = ReviewScheduler()
