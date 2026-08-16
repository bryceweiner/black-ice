"""Discover, start, and expose plugins."""

from __future__ import annotations

import logging
from importlib.metadata import entry_points
from typing import Any

from .. import db
from ..models import SensorDescriptor
from . import store
from .base import PluginContext, SensorPlugin
from .supervisor import PluginFailure, Supervisor

log = logging.getLogger("blackice.registry")

ENTRY_POINT_GROUP = "blackice.plugins"


class Registry:
    def __init__(self) -> None:
        self.supervisors: dict[str, Supervisor] = {}

    # --- discovery ---------------------------------------------------------

    def discover(self) -> list[type[SensorPlugin]]:
        found: list[type[SensorPlugin]] = []
        for ep in entry_points(group=ENTRY_POINT_GROUP):
            try:
                cls = ep.load()
            except Exception:
                log.exception("failed loading plugin entry point %s", ep.name)
                continue
            if not (isinstance(cls, type) and issubclass(cls, SensorPlugin)):
                log.error("%s is not a SensorPlugin subclass; skipped", ep.name)
                continue
            cls.name = cls.name or ep.name
            found.append(cls)
        return found

    async def start_all(self, emit: Any) -> None:
        for cls in self.discover():
            await self.start_plugin(cls, emit)

    async def start_plugin(self, cls: type[SensorPlugin], emit: Any) -> Supervisor | None:
        name = cls.name
        if name in self.supervisors:
            log.warning("plugin %s already registered; skipped", name)
            return None
        try:
            plugin = cls()
        except Exception:
            log.exception("plugin %s failed to construct", name)
            return None
        ctx = PluginContext(name, await store.open_store(name), emit)
        sup = Supervisor(plugin, ctx)
        self.supervisors[name] = sup
        if await sup.start():
            await self._sync_sensors(sup)
        return sup

    async def stop_all(self) -> None:
        for sup in self.supervisors.values():
            await sup.stop()
        await store.close_all()
        self.supervisors.clear()

    # --- projection into core ---------------------------------------------

    async def _sync_sensors(self, sup: Supervisor) -> None:
        """Mirror a plugin's declared sensors and alarm rules into core tables."""
        for d in sup.describe():
            await db.execute(
                """INSERT INTO sensors (id, plugin, name, kind, descriptor, last_seen)
                   VALUES (?, ?, ?, ?, ?, datetime('now'))
                   ON CONFLICT(id) DO UPDATE SET
                     plugin=excluded.plugin, name=excluded.name, kind=excluded.kind,
                     descriptor=excluded.descriptor, last_seen=excluded.last_seen""",
                (d.id, sup.name, d.name, d.kind, db.dumps(d.model_dump(mode="json"))),
            )
            for rule in d.alarm_rules:
                rule_id = await db.execute(
                    """INSERT INTO alarm_rules (plugin, key, name, description, sensor_id, spec)
                       VALUES (?, ?, ?, ?, ?, ?)
                       ON CONFLICT(plugin, key) DO UPDATE SET
                         name=excluded.name, description=excluded.description,
                         sensor_id=excluded.sensor_id, spec=excluded.spec""",
                    (sup.name, rule.key, rule.name, rule.description,
                     rule.sensor_id or d.id, db.dumps(rule.spec)),
                )
                # Only seed arm state on first sight; never clobber the user's toggle.
                await db.execute(
                    """INSERT INTO alarm_state (rule_id, armed, updated_by)
                       VALUES ((SELECT id FROM alarm_rules WHERE plugin=? AND key=?), ?, 'plugin')
                       ON CONFLICT(rule_id) DO NOTHING""",
                    (sup.name, rule.key, int(rule.default_armed)),
                )
                _ = rule_id

    def descriptors(self) -> list[SensorDescriptor]:
        return [d for sup in self.supervisors.values() for d in sup.describe()]

    def descriptor_for(self, sensor_id: str) -> SensorDescriptor | None:
        return next((d for d in self.descriptors() if d.id == sensor_id), None)

    def plugin_of(self, sensor_id: str) -> Supervisor | None:
        for sup in self.supervisors.values():
            if any(d.id == sensor_id for d in sup.describe()):
                return sup
        return None

    def health(self) -> list[dict[str, Any]]:
        return [sup.health() for sup in self.supervisors.values()]

    # --- dispatch ----------------------------------------------------------

    async def command(self, plugin: str, cmd: str, **kwargs: Any) -> Any:
        sup = self.supervisors.get(plugin)
        if sup is None:
            raise PluginFailure(f"unknown plugin {plugin!r}")
        return await sup.call("handle_command", cmd, **kwargs)

    async def query(self, plugin: str, source: str, **kwargs: Any) -> Any:
        sup = self.supervisors.get(plugin)
        if sup is None:
            raise PluginFailure(f"unknown plugin {plugin!r}")
        return await sup.call("query", source, **kwargs)


registry = Registry()
