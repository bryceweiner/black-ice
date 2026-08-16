"""Each plugin owns a private SQLite file and its own schema."""

from __future__ import annotations

import aiosqlite

from ..config import get_settings

_open: dict[str, aiosqlite.Connection] = {}


async def open_store(plugin: str) -> aiosqlite.Connection:
    if plugin in _open:
        return _open[plugin]
    s = get_settings()
    s.plugin_db_dir.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(s.plugin_db_dir / f"{plugin}.db")
    conn.row_factory = aiosqlite.Row
    await conn.executescript("PRAGMA journal_mode=WAL;PRAGMA foreign_keys=ON;")
    await conn.commit()
    _open[plugin] = conn
    return conn


async def close_store(plugin: str) -> None:
    conn = _open.pop(plugin, None)
    if conn is not None:
        await conn.close()


async def close_all() -> None:
    for plugin in list(_open):
        await close_store(plugin)
