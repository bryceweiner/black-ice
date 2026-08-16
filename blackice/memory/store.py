"""kokoro-memory wrapper. kokoro stays the authority on recall and on its own
authenticator pipeline; SQLite carries the audit trail.

Absent weights disable memory loudly. They never leave it running unlogged.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from .. import db
from ..config import get_settings
from ..models import Trust

log = logging.getLogger("blackice.memory")

# kokoro's Japanese categories. English aliases resolve to these, but naming
# them explicitly keeps our call sites readable.
TRUTH = "truth"       # 真実 — verified facts
EVENT = "event"       # 出来事 — timestamped occurrences
IDENTITY = "identity"  # 心 — core self
RELATION = "relationship"  # 関係


class MemoryStore:
    def __init__(self) -> None:
        self._km: Any = None
        self._tried = False

    @property
    def available(self) -> bool:
        return self._load() is not None

    def _load(self):
        if self._tried:
            return self._km
        self._tried = True
        s = get_settings()
        if not s.memory_enabled:
            log.info("memory disabled by configuration")
            return None
        # kokoro reads MEMORY_ROOT at import time, so these must be set first.
        os.environ.setdefault("KOKORO_MEMORY_ROOT", str(s.kokoro_memory_root))
        os.environ.setdefault("KOKORO_AGENT_NAME", s.assistant_name)
        os.environ.setdefault("KOKORO_OWNER_NAME", s.owner_name)
        try:
            import kokoro_memory

            self._km = kokoro_memory
            log.info("kokoro-memory loaded at %s", kokoro_memory.MEMORY_ROOT)
        except Exception as exc:
            log.error(
                "kokoro-memory unavailable (%s): %s. Memory is DISABLED; "
                "install from https://huggingface.co/AIIT-Threshold/kokoro-memory",
                type(exc).__name__, exc,
            )
            self._km = None
        return self._km

    async def _mirror(self, op: str, **fields: Any) -> None:
        await db.execute(
            """INSERT INTO memory_ops (op, category, key, value, source, confidence,
                                       fact_id, detail)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (op, fields.get("category"), fields.get("key"), fields.get("value"),
             fields.get("source"), fields.get("confidence"), fields.get("fact_id"),
             db.dumps(fields.get("detail") or {})),
        )

    async def add_fact(
        self,
        category: str,
        key: str,
        value: str,
        *,
        source: str = "user_explicit",
        confidence: float = 0.8,
        trust: Trust = Trust.USER,
    ) -> bool:
        """Write a durable fact. Returns whether kokoro stored it.

        SENSOR-trust content is refused here as well as upstream. Anyone who
        controls what a camera sees would otherwise be able to write persistent
        instructions into the assistant's long-term memory. kokoro has its own
        public-origin path, but for a monitoring system the safe answer is that
        sensor text is never eligible at all.
        """
        if trust is Trust.SENSOR:
            await self._mirror(
                "refused", category=category, key=key, value=value, source=source,
                detail={"reason": "sensor-trust content cannot write memory"},
            )
            log.warning("refused memory write from sensor-trust source %r", source)
            return False

        km = self._load()
        if km is None:
            await self._mirror("skipped", category=category, key=key, value=value,
                               source=source, detail={"reason": "memory unavailable"})
            return False
        try:
            stored = await asyncio.to_thread(
                km.add_fact, category, key, value,
                source=source, confidence=confidence,
                origin_surface="local", authority_class="internal",
                trusted_by_default=True,
            )
        except Exception as exc:
            log.exception("memory add_fact failed")
            await self._mirror("error", category=category, key=key, value=value,
                               source=source, detail={"error": str(exc)})
            return False

        # kokoro returns False when its own filters reject a fact (junk,
        # duplicate, identity guard). That is a real outcome, so record it.
        await self._mirror(
            "add_fact" if stored else "rejected",
            category=category, key=key, value=value,
            source=source, confidence=confidence,
        )
        return bool(stored)

    async def recall(self, query: str, limit: int = 8) -> list[dict[str, Any]]:
        """Search memory. Also exposed to the model as a tool."""
        km = self._load()
        if km is None or not query:
            return []
        try:
            rows = await asyncio.to_thread(km.recall, query, limit)
        except Exception:
            log.exception("memory recall failed")
            return []
        rows = list(rows or [])
        await self._mirror("recall", key=query, detail={"hits": len(rows)})
        return [r if isinstance(r, dict) else {"value": str(r)} for r in rows]

    async def startup_block(self) -> str:
        """Memory summary injected into the system prompt at session start."""
        km = self._load()
        if km is None:
            return ""
        try:
            block = await asyncio.to_thread(km.build_startup_memory_block)
        except Exception:
            log.exception("memory startup block failed")
            return ""
        block = block or ""
        await self._mirror("startup_block", detail={"chars": len(block)})
        return block

    async def append_turn(self, user_text: str, agent_text: str) -> None:
        """Record a conversation turn for later consolidation.

        Only ever called with USER-trust text; see memory/consolidate.py.
        """
        km = self._load()
        if km is None:
            return
        try:
            await asyncio.to_thread(km.append_raw_turn, user_text, agent_text)
        except Exception:
            log.exception("memory append_raw_turn failed")

    async def remove_fact(self, category: str, key: str) -> bool:
        km = self._load()
        if km is None:
            return False
        removed = await asyncio.to_thread(km.remove_fact, category, key)
        await self._mirror("remove_fact", category=category, key=key,
                           detail={"removed": bool(removed)})
        return bool(removed)


memory = MemoryStore()
