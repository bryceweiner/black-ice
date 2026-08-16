from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Form, Response, status
from fastapi.responses import JSONResponse

from .. import db, logging_setup
from ..config import get_settings
from ..llm import prompts
from ..llm.coretools import register_core_tools
from ..llm.tools import project_plugin_tools
from ..llm.tools import registry as tool_registry
from ..memory import consolidate
from ..plugins.registry import registry
from ..rsi.scheduler import scheduler as review_scheduler
from ..services import events
from ..triage import pipeline as triage
from ..voice.voice2_backend import Voice2Backend
from . import auth
from .routes import router
from .static import mount as mount_static
from .ws import hub, websocket_endpoint

log = logging.getLogger("blackice")


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    s.ensure_dirs()
    logging_setup.configure()
    await db.connect()
    await prompts.ensure_seeded()

    hub.wire()
    triage.install()
    register_core_tools()
    await registry.start_all(events.record)
    project_plugin_tools(registry, tool_registry)

    if s.memory_enabled and not await consolidate.install_generator():
        log.warning("memory is enabled but kokoro-memory is unavailable")

    await review_scheduler.start()

    voice = None
    if s.voice_enabled:
        voice = Voice2Backend()
        try:
            await voice.start()
        except Exception as exc:
            # Voice is an accessory; the dashboard must still come up without it.
            log.error("voice did not start: %s", exc)
            await review_scheduler.start()

    voice = None

    log.info(
        "black-ice up: assistant=%s plugins=%s tools=%d voice=%s",
        s.assistant_name, list(registry.supervisors), len(tool_registry.tools),
        bool(voice),
    )
    app.state.voice = voice
    try:
        yield
    finally:
        await review_scheduler.stop()
        if voice is not None:
            await voice.stop()
        await registry.stop_all()
        await db.close()


def create_app() -> FastAPI:
    s = get_settings()
    app = FastAPI(title=f"Black Ice ({s.assistant_name})", lifespan=lifespan)

    @app.get("/api/health")
    async def health() -> dict:
        return {
            "ok": True,
            "assistant": s.assistant_name,
            "sensors": await db.fetchval("SELECT count(*) FROM sensors") or 0,
            "events": await db.fetchval("SELECT count(*) FROM events") or 0,
            "escalations": await db.fetchval(
                "SELECT count(*) FROM escalations WHERE status = 'open'") or 0,
        }

    @app.post("/api/login")
    async def login(response: Response, username: str = Form(), password: str = Form()):
        if not auth.authenticate(username, password):
            return JSONResponse(
                {"detail": "Invalid credentials"}, status.HTTP_401_UNAUTHORIZED
            )
        token, csrf = auth.issue_session(username)
        secure = False  # LAN-only over http; flip when fronted by TLS
        response.set_cookie(
            auth.SESSION_COOKIE, token, httponly=True, samesite="lax",
            max_age=auth.MAX_AGE, secure=secure,
        )
        response.set_cookie(
            auth.CSRF_COOKIE, csrf, httponly=False, samesite="lax",
            max_age=auth.MAX_AGE, secure=secure,
        )
        return {"ok": True, "username": username}

    @app.post("/api/logout")
    async def logout(response: Response):
        response.delete_cookie(auth.SESSION_COOKIE)
        response.delete_cookie(auth.CSRF_COOKIE)
        return {"ok": True}

    app.include_router(router)
    app.add_api_websocket_route("/ws", websocket_endpoint)
    mount_static(app)  # last: the SPA catch-all must not shadow /api or /ws
    return app


app = create_app()
