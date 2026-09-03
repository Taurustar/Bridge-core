"""Minimal durable long-term memory backend (plan sections 20.3, 28).

Milestone 0.4.0 ships only what character life events need: a durable Redis
fallback under ``core:longterm:{owner}`` storing normalized memory records.
Milestone 0.6.0 generalizes this into the complete three-tier backend with
the optional Chroma adapter. When Chroma is off, durable rows live here —
never in process-only RAM advertised as persistence.

Records are JSON rows in a bounded Redis list; upserts scan the bounded list
under the caller's engine lock (single-writer: the life engine serializes
generation per owner).

Schema per plan section 20.3: id, kind, text, source, source_mode,
importance, created_ts, updated_ts, pinned, metadata.
"""

from __future__ import annotations

import json
import logging
import uuid

from .cache import RedisCache
from .config import Config
from .constants import longterm_key

log = logging.getLogger("bridge.memory")

# Kinds from plan section 20.3. Only character_life_event rows are written in
# 0.4.0; the full kind set arrives with the memory milestone.
MEMORY_KINDS: tuple[str, ...] = (
    "user_profile",
    "relationship",
    "conversation",
    "conversation_chapter",
    "character_life_event",
    "character_life_chapter",
    "project",
    "commitment",
)


class LongTermMemory:
    """Durable Redis long-term fallback (``core:longterm:{owner}``)."""

    def __init__(self, config: Config, cache: RedisCache) -> None:
        self.config = config
        self.cache = cache

    def key(self, owner: str) -> str:
        return longterm_key(owner)

    def make_record(
        self,
        *,
        kind: str,
        text: str,
        source: str,
        source_mode: str,
        importance: float = 0.0,
        metadata: dict | None = None,
        record_id: str | None = None,
        created_ts: float | None = None,
    ) -> dict:
        if kind not in MEMORY_KINDS:
            raise ValueError(f"unknown memory kind: {kind!r}")
        now = created_ts if created_ts is not None else 0.0
        return {
            "id": record_id or f"mem_{uuid.uuid4().hex}",
            "kind": kind,
            "text": text,
            "source": source,
            "source_mode": source_mode,
            "importance": float(max(0.0, min(1.0, importance))),
            "created_ts": now,
            "updated_ts": now,
            "pinned": False,
            "metadata": metadata or {},
        }

    async def add(self, owner: str, record: dict) -> dict:
        """Upsert by id; bounded at MEMORY_MAX_PER_USER rows."""
        rows = await self.records(owner)
        updated_ts = float(record.get("updated_ts", 0) or 0)
        replaced = False
        result: list[dict] = []
        for existing in rows:
            if existing.get("id") == record.get("id"):
                result.append(record)
                replaced = True
            else:
                result.append(existing)
        if not replaced:
            result.append(record)
        if len(result) > self.config.MEMORY_MAX_PER_USER:
            # Oldest updated rows drop first; pinned rows never delete.
            droppable = [row for row in result if not row.get("pinned")]
            overflow = len(result) - self.config.MEMORY_MAX_PER_USER
            drop_ids = {
                row["id"]
                for row in sorted(droppable, key=lambda r: float(r.get("updated_ts", 0) or 0))[:overflow]
            }
            result = [row for row in result if row["id"] not in drop_ids]
        pipe = self.cache.client.pipeline(transaction=True)
        pipe.delete(self.key(owner))
        for row in result:
            pipe.rpush(self.key(owner), json.dumps(row))
        await pipe.execute()
        return record

    async def records(
        self, owner: str, kind: str | None = None, limit: int | None = None
    ) -> list[dict]:
        raw_rows = await self.cache.get_rows(self.key(owner))
        rows: list[dict] = []
        for raw in raw_rows:
            try:
                row = json.loads(raw)
            except json.JSONDecodeError:
                log.warning("Skipping malformed long-term memory row")
                continue
            if isinstance(row, dict) and (kind is None or row.get("kind") == kind):
                rows.append(row)
        if limit is not None and limit >= 0:
            rows = rows[-limit:]
        return rows

    async def get(self, owner: str, record_id: str) -> dict | None:
        for row in await self.records(owner):
            if row.get("id") == record_id:
                return row
        return None

    async def count(self, owner: str) -> int:
        return len(await self.records(owner))
