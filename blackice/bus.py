"""Async pub/sub. Plugins publish; triage and the websocket layer subscribe.

One subscriber failing must never stop the others, and must never propagate
back to the plugin that emitted.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

log = logging.getLogger("blackice.bus")

Handler = Callable[[str, Any], Awaitable[None]]


class EventBus:
    def __init__(self) -> None:
        self._subs: list[tuple[str | None, Handler]] = []

    def subscribe(self, handler: Handler, topic: str | None = None) -> None:
        """Subscribe to one topic, or all topics when topic is None."""
        self._subs.append((topic, handler))

    def unsubscribe(self, handler: Handler) -> None:
        self._subs = [(t, h) for t, h in self._subs if h is not handler]

    async def publish(self, topic: str, payload: Any) -> None:
        targets = [h for t, h in self._subs if t is None or t == topic]
        if not targets:
            return
        results = await asyncio.gather(
            *(h(topic, payload) for h in targets), return_exceptions=True
        )
        for h, r in zip(targets, results, strict=True):
            if isinstance(r, BaseException):
                log.exception(
                    "subscriber %s failed on topic %s", getattr(h, "__qualname__", h),
                    topic, exc_info=r,
                )


bus = EventBus()
