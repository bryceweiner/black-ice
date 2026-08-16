"""Serving the built dashboard and captured media from the API process, so a
single `blackice serve` is the whole appliance.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, FastAPI, HTTPException, status
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from ..config import get_settings
from .auth import require_user

log = logging.getLogger("blackice.static")

DIST = Path(__file__).resolve().parents[2] / "dashboard" / "dist"

media_router = APIRouter(prefix="/media", dependencies=[Depends(require_user)])


@media_router.get("/{path:path}")
async def media(path: str) -> FileResponse:
    """Serve a captured file.

    Authenticated: this is footage from inside someone's home, and a plain
    static mount would hand it to anyone on the LAN.
    """
    root = get_settings().media_dir.resolve()
    target = (root / path).resolve()
    # resolve() first, then containment-check: '..' must not escape the root.
    if not target.is_relative_to(root) or not target.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such media")
    return FileResponse(target)


class SPAStaticFiles(StaticFiles):
    """Static files with a single-page-app fallback.

    The dashboard routes client-side, so a hard refresh on /escalations asks the
    server for a file that does not exist. Unknown paths get index.html.
    """

    async def get_response(self, path: str, scope):
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            # Starlette's exception, not FastAPI's subclass -- catching the
            # subclass here silently never matches and every deep link 404s.
            if exc.status_code != status.HTTP_404_NOT_FOUND:
                raise
            # An unknown API path is a real 404. Serving index.html there would
            # hand fetch() an HTML page to parse as JSON.
            if path.startswith(("api/", "api", "ws")):
                raise
            return await super().get_response("index.html", scope)


def mount(app: FastAPI) -> bool:
    """Attach media and the built dashboard. Returns whether a build was found."""
    app.include_router(media_router)

    if not (DIST / "index.html").exists():
        log.info(
            "no dashboard build at %s; run `npm run build` in dashboard/, "
            "or use `npm run dev` for the dev server", DIST,
        )
        return False

    # Last, so it cannot shadow /api or /ws.
    app.mount("/", SPAStaticFiles(directory=DIST, html=True), name="dashboard")
    log.info("serving dashboard from %s", DIST)
    return True
