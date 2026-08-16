"""Every call into a plugin crosses this. A plugin that hangs, raises, or dies
degrades to a health badge on the dashboard instead of taking down the service.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from .. import db
from .base import PluginContext, SensorPlugin

log = logging.getLogger("blackice.supervisor")

DEFAULT_TIMEOUT = 30.0
MAX_RESTARTS = 5


class PluginFailure(Exception):
    """A guarded plugin call did not succeed."""


class Supervisor:
    def __init__(
        self,
        plugin: SensorPlugin,
        ctx: PluginContext,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.plugin = plugin
        self.ctx = ctx
        self.timeout = timeout
        self.state = "stopped"
        self.last_error: str | None = None
        self.restarts = 0

    @property
    def name(self) -> str:
        return self.plugin.name

    async def call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        """Invoke a plugin method under timeout and exception capture."""
        fn = getattr(self.plugin, method, None)
        if fn is None:
            raise PluginFailure(f"{self.name} has no method {method!r}")
        try:
            result = await asyncio.wait_for(fn(*args, **kwargs), self.timeout)
        except TimeoutError as exc:
            await self._fail(f"{method} timed out after {self.timeout}s")
            raise PluginFailure(str(self.last_error)) from exc
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._fail(f"{method} raised {type(exc).__name__}: {exc}")
            log.exception("plugin %s failed in %s", self.name, method)
            raise PluginFailure(str(self.last_error)) from exc
        await self._ok()
        return result

    async def start(self) -> bool:
        try:
            await self.call("start", self.ctx)
        except PluginFailure:
            return False
        self.state = "healthy"
        await self._persist()
        return True

    async def stop(self) -> None:
        # Failure is already recorded by call(); stopping must not raise.
        with contextlib.suppress(PluginFailure):
            await self.call("stop")
        self.state = "stopped"
        await self._persist()

    async def restart(self) -> bool:
        if self.restarts >= MAX_RESTARTS:
            await self._fail(f"exceeded {MAX_RESTARTS} restarts; giving up")
            return False
        self.restarts += 1
        log.warning("restarting plugin %s (attempt %d)", self.name, self.restarts)
        await self.stop()
        await asyncio.sleep(min(2**self.restarts, 30))
        return await self.start()

    def describe(self) -> list:
        """Sync, so it cannot be timed out — but it must never raise."""
        try:
            return self.plugin.describe()
        except Exception as exc:
            log.exception("plugin %s describe() failed", self.name)
            self.state = "unhealthy"
            self.last_error = f"describe raised {type(exc).__name__}: {exc}"
            return []

    async def _ok(self) -> None:
        if self.state != "healthy":
            self.state = "healthy"
            self.last_error = None
        await self._persist(ok=True)

    async def _fail(self, message: str) -> None:
        self.state = "unhealthy"
        self.last_error = message
        await self._persist()

    async def _persist(self, ok: bool = False) -> None:
        await db.execute(
            """INSERT INTO plugin_health (plugin, state, last_error, last_ok, restarts, updated_at)
               VALUES (?, ?, ?, CASE WHEN ? THEN datetime('now') END, ?, datetime('now'))
               ON CONFLICT(plugin) DO UPDATE SET
                 state=excluded.state,
                 last_error=excluded.last_error,
                 last_ok=COALESCE(excluded.last_ok, plugin_health.last_ok),
                 restarts=excluded.restarts,
                 updated_at=excluded.updated_at""",
            (self.name, self.state, self.last_error, ok, self.restarts),
        )

    def health(self) -> dict[str, Any]:
        return {
            "plugin": self.name,
            "version": self.plugin.version,
            "state": self.state,
            "last_error": self.last_error,
            "restarts": self.restarts,
        }
