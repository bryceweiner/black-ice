"""Versioned prompt storage: propose, diff, activate, roll back.

The assistant is allowed to rewrite its own prompts, so every version is kept
with its parent, its rationale and its author. Nothing is ever overwritten in
place -- that is the difference between a change you can undo and a change you
can only regret.
"""

from __future__ import annotations

import difflib
import logging
from typing import Any

from .. import db
from ..llm import prompts as prompt_defaults

log = logging.getLogger("blackice.rsi.prompts")

HUMAN = "human"
RSI = "rsi"


async def history(name: str) -> list[dict[str, Any]]:
    return await db.fetchall(
        "SELECT id, name, version, parent_id, rationale, author, active, created_at"
        " FROM prompt_versions WHERE name = ? ORDER BY version DESC",
        (name,),
    )


async def get(version_id: int) -> dict[str, Any] | None:
    return await db.fetchone("SELECT * FROM prompt_versions WHERE id = ?", (version_id,))


async def active(name: str) -> dict[str, Any] | None:
    return await db.fetchone(
        "SELECT * FROM prompt_versions WHERE name = ? AND active = 1", (name,)
    )


async def propose(
    name: str, text: str, *, rationale: str = "", author: str = RSI
) -> dict[str, Any]:
    """Record a candidate. Inactive until it passes the regression gate."""
    if name not in prompt_defaults.DEFAULTS:
        raise ValueError(f"unknown prompt {name!r}")
    text = (text or "").strip()
    if not text:
        raise ValueError("refusing to store an empty prompt")

    current = await active(name)
    if current and current["text"].strip() == text:
        raise ValueError("candidate is identical to the active version")

    next_version = (await db.fetchval(
        "SELECT COALESCE(max(version), 0) + 1 FROM prompt_versions WHERE name = ?",
        (name,),
    )) or 1
    version_id = await db.execute(
        """INSERT INTO prompt_versions (name, version, text, parent_id, rationale,
                                        author, active)
           VALUES (?, ?, ?, ?, ?, ?, 0)""",
        (name, next_version, text, current["id"] if current else None,
         rationale, author),
    )
    log.info("proposed %s v%s by %s", name, next_version, author)
    return await get(version_id)


async def activate(version_id: int) -> dict[str, Any]:
    """Make a version live. Exactly one version per prompt is active."""
    row = await get(version_id)
    if row is None:
        raise ValueError(f"unknown prompt version {version_id}")
    await db.execute(
        "UPDATE prompt_versions SET active = 0 WHERE name = ?", (row["name"],)
    )
    await db.execute("UPDATE prompt_versions SET active = 1 WHERE id = ?", (version_id,))
    log.info("activated %s v%s (%s)", row["name"], row["version"], row["author"])
    return await get(version_id)


async def rollback(name: str) -> dict[str, Any] | None:
    """Return to the newest human-authored version.

    Falling back to "the previous version" would be wrong: a run of bad RSI
    edits would only step back one at a time. A human-approved version is the
    known-good floor.
    """
    row = await db.fetchone(
        "SELECT * FROM prompt_versions WHERE name = ? AND author = ?"
        " ORDER BY version DESC LIMIT 1",
        (name, HUMAN),
    )
    if row is None:
        log.error("no human-authored version of %s to roll back to", name)
        return None
    log.warning("rolling %s back to v%s", name, row["version"])
    return await activate(row["id"])


def diff(before: str, after: str, *, label: str = "prompt") -> str:
    return "\n".join(
        difflib.unified_diff(
            (before or "").splitlines(), (after or "").splitlines(),
            fromfile=f"{label} (active)", tofile=f"{label} (candidate)",
            lineterm="",
        )
    )
