"""Core SQLite access. One shared WAL connection; aiosqlite serialises calls."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import aiosqlite

from .config import get_settings

SCHEMA = Path(__file__).with_name("schema.sql")

_conn: aiosqlite.Connection | None = None


async def connect() -> aiosqlite.Connection:
    global _conn
    if _conn is not None:
        return _conn
    s = get_settings()
    s.ensure_dirs()
    _conn = await aiosqlite.connect(s.db_path)
    _conn.row_factory = aiosqlite.Row
    await _conn.executescript(
        "PRAGMA journal_mode=WAL;"
        "PRAGMA foreign_keys=ON;"
        "PRAGMA synchronous=NORMAL;"
    )
    await _conn.executescript(SCHEMA.read_text())
    await _conn.commit()
    return _conn


async def close() -> None:
    global _conn
    if _conn is not None:
        await _conn.close()
        _conn = None


def conn() -> aiosqlite.Connection:
    if _conn is None:
        raise RuntimeError("database not connected; call db.connect() first")
    return _conn


async def execute(sql: str, params: tuple = ()) -> int:
    """Run a write and return lastrowid."""
    cur = await conn().execute(sql, params)
    await conn().commit()
    return cur.lastrowid or 0


async def executemany(sql: str, seq: list[tuple]) -> None:
    await conn().executemany(sql, seq)
    await conn().commit()


async def fetchall(sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    cur = await conn().execute(sql, params)
    return [dict(r) for r in await cur.fetchall()]


async def fetchone(sql: str, params: tuple = ()) -> dict[str, Any] | None:
    cur = await conn().execute(sql, params)
    row = await cur.fetchone()
    return dict(row) if row else None


async def fetchval(sql: str, params: tuple = ()) -> Any:
    row = await fetchone(sql, params)
    return next(iter(row.values())) if row else None


def loads(value: Any, default: Any = None) -> Any:
    """Decode a JSON column, tolerating NULL and malformed rows."""
    if not value:
        return default if default is not None else {}
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default if default is not None else {}


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)
