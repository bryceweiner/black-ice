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
from ..voice.announce import Announcer
from ..voice.voice2_backend import Voice2Backend
from . import auth
from .routes import router
from .static import mount as mount_static
from .ws import hub, websocket_endpoint

log = logging.getLogger("blackice")


async def start_voice() -> tuple[Voice2Backend | None, Announcer | None]:
    """Bring up the speaker, and the announcer that speaks without being asked.

    This process runs both the reminder scheduler and the only speaker, so a
    reminder that comes due becomes audible here or nowhere. Returns
    `(None, None)` when voice is switched off or fails to start: it is an
    accessory, and the dashboard must still come up without it.
    """
    if not get_settings().voice_enabled:
        return None, None

    voice = Voice2Backend()
    try:
        await voice.start()
    except Exception as exc:
        log.error("voice did not start: %s", exc)
        return None, None

    announcer = Announcer(voice)
    await announcer.start()
    return voice, announcer


async def stop_voice(voice: Voice2Backend | None, announcer: Announcer | None) -> None:
    """Shutdown must not raise: the rest of the teardown still has to run."""
    for name, component in (("announcer", announcer), ("voice", voice)):
        if component is None:
            continue
        try:
            await component.stop()
        except Exception:
            log.exception("%s did not stop cleanly", name)


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

    voice, announcer = await start_voice()

    log.info(
        "black-ice up: assistant=%s plugins=%s tools=%d voice=%s",
        s.assistant_name, list(registry.supervisors), len(tool_registry.tools),
        bool(voice),
    )
    app.state.voice = voice
    app.state.announcer = announcer
    try:
        yield
    finally:
        await review_scheduler.stop()
        await stop_voice(voice, announcer)
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
