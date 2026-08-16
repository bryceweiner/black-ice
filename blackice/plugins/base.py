"""The plugin contract. A plugin is a Python package exposing a SensorPlugin
subclass via the `blackice.plugins` entry-point group."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any

import aiosqlite

from ..models import Event, SensorDescriptor


class PluginContext:
    """Everything a plugin is allowed to touch. Plugins never import core
    services directly — they go through this."""

    def __init__(
        self,
        plugin: str,
        db: aiosqlite.Connection,
        emit: Any,
        media: Any = None,
    ) -> None:
        self.plugin = plugin
        self.db = db
        self.media = media
        self.log = logging.getLogger(f"blackice.plugin.{plugin}")
        self._emit = emit

    async def emit(self, event: Event) -> int:
        """Publish a sensor event. Returns the stored event id."""
        event.plugin = self.plugin
        return await self._emit(event)


class SensorPlugin(ABC):
    name: str = ""
    version: str = "0.0.0"

    @abstractmethod
    async def start(self, ctx: PluginContext) -> None:
        """Open devices, create tables, spawn polling tasks."""

    @abstractmethod
    async def stop(self) -> None:
        """Release everything start() acquired. Must be idempotent."""

    @abstractmethod
    def describe(self) -> list[SensorDescriptor]:
        """The sensors this plugin provides, and how to render them."""

    async def handle_command(self, cmd: str, **kwargs: Any) -> Any:
        """Invoked when the LLM calls one of this plugin's declared tools."""
        raise NotImplementedError(f"{self.name} has no command {cmd!r}")

    async def query(self, source: str, **kwargs: Any) -> Any:
        """Back a WidgetSpec.data_source with data."""
        raise NotImplementedError(f"{self.name} has no data source {source!r}")
