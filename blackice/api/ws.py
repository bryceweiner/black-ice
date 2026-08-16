"""Single websocket carrying every live update to the dashboard."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

from ..bus import bus
from ..db import dumps
from .auth import SESSION_COOKIE, read_session

log = logging.getLogger("blackice.ws")

TOPICS = (
    "event", "escalation", "sensor_state", "alarm_state",
    "plugin_health", "llm_token", "voice", "rsi_proposal",
)


class Hub:
    def __init__(self) -> None:
        self.clients: set[WebSocket] = set()
        self._wired = False

    def wire(self) -> None:
        if not self._wired:
            bus.subscribe(self._on_bus)
            self._wired = True

    async def _on_bus(self, topic: str, payload: Any) -> None:
        if topic in TOPICS:
            await self.broadcast(topic, payload)

    async def connect(self, ws: WebSocket) -> bool:
        if not read_session(ws.cookies.get(SESSION_COOKIE, "")):
            await ws.close(code=4401)
            return False
        await ws.accept()
        self.clients.add(ws)
        return True

    def disconnect(self, ws: WebSocket) -> None:
        self.clients.discard(ws)

    async def broadcast(self, topic: str, payload: Any) -> None:
        if not self.clients:
            return
        message = dumps({"topic": topic, "payload": payload})
        for ws in list(self.clients):
            try:
                await ws.send_text(message)
            except Exception:
                self.disconnect(ws)


hub = Hub()


async def websocket_endpoint(ws: WebSocket) -> None:
    if not await hub.connect(ws):
        return
    try:
        while True:
            await ws.receive_text()  # client keepalive; no inbound protocol
    except WebSocketDisconnect:
        pass
    finally:
        hub.disconnect(ws)
