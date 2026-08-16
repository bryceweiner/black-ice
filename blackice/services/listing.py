"""Search + date-range listing, written once and reused by every list page
(sensors, events, escalations, alarms) and by the LLM's search tools."""

from __future__ import annotations

import re
from typing import Any

from .. import db

_TOKEN = re.compile(r"[\w'-]+", re.UNICODE)


def fts_query(text: str) -> str:
    """Turn free user input into a safe FTS5 MATCH expression.

    User text goes straight into MATCH, and FTS5 has its own operator syntax --
    a stray `"` or `*` is a syntax error, and `NOT`/`OR` silently change intent.
    Quoting each token as a phrase neutralises all of it.
    """
    tokens = _TOKEN.findall(text or "")
    return " ".join(f'"{t}"' for t in tokens)


async def list_rows(
    *,
    table: str,
    columns: str = "*",
    fts_table: str | None = None,
    q: str | None = None,
    start: str | None = None,
    end: str | None = None,
    ts_col: str = "ts",
    where: list[str] | None = None,
    params: list[Any] | None = None,
    order: str = "DESC",
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    clauses = list(where or [])
    args: list[Any] = list(params or [])
    joins = ""

    if q and fts_table:
        match = fts_query(q)
        if match:
            joins = f" JOIN {fts_table} f ON f.rowid = t.id"
            clauses.append(f"{fts_table} MATCH ?")
            args.append(match)
    elif q:
        clauses.append("t.name LIKE ?")
        args.append(f"%{q}%")

    if start:
        clauses.append(f"t.{ts_col} >= ?")
        args.append(start)
    if end:
        clauses.append(f"t.{ts_col} <= ?")
        args.append(end)

    sql_where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    total = await db.fetchval(
        f"SELECT count(*) FROM {table} t{joins}{sql_where}", tuple(args)
    )
    rows = await db.fetchall(
        f"SELECT {columns} FROM {table} t{joins}{sql_where}"
        f" ORDER BY t.{ts_col} {order}, t.id {order} LIMIT ? OFFSET ?",
        (*args, limit, offset),
    )
    return {"rows": rows, "total": total or 0, "limit": limit, "offset": offset}
