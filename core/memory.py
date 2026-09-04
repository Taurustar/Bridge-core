"""Three-tier long-term memory backend (plan sections 20.3, 20.4, 20.5, 28).

The durable store of record is the bounded Redis list
``core:longterm:{owner}`` (never process-only RAM). When ``CHROMA_ENABLED``
is set, an optional Chroma collection acts as the semantic index over the
same rows: writes fan out to both, reads prefer Chroma semantic ordering and
map ids back to Redis rows, and any Chroma failure degrades to the
deterministic token-overlap search below without touching Redis chat paths
(plan acceptance: a Chroma outage reports degraded mode and never breaks
Redis).

Dedupe/merge (plan section 20.4.1): exact normalized text or near-duplicate
(similarity >= threshold) updates the existing row instead of appending.
Similarity uses embeddings when Chroma is available and deterministic
normalized-token overlap otherwise.

Records carry: id, kind, text, source, source_mode, importance, created_ts,
updated_ts, pinned, metadata (incl. provenance ``source_ids`` and
``schema_version``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid

from .cache import RedisCache
from .config import Config
from .constants import longterm_key, midterm_key
from .chroma_store import ChromaMemoryStore

log = logging.getLogger("bridge.memory")

# Kinds from plan section 20.3.
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

EXTRACTION_KINDS: tuple[str, ...] = (
    "user_profile",
    "relationship",
    "commitment",
    "project",
    "conversation",
)

# Conversation-family rows decay on the short TTL, life-family rows on the
# long one (plan section 20.5: conversation rows decay faster than life
# events).
_CONVERSATION_KINDS: frozenset[str] = frozenset(
    {"conversation", "conversation_chapter"}
)

SCHEMA_VERSION = 1

# Near-duplicate merge threshold for deterministic token overlap.
MERGE_SIMILARITY = 0.85

_WHITESPACE_RE = re.compile(r"\s+")
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def normalize_text(text: str) -> str:
    return _WHITESPACE_RE.sub(" ", (text or "").strip()).lower()


def text_tokens(text: str) -> set[str]:
    return set(_TOKEN_RE.findall(normalize_text(text)))


def token_overlap(a: str, b: str) -> float:
    """Jaccard overlap of normalized token sets (0.0 - 1.0)."""
    tokens_a = text_tokens(a)
    tokens_b = text_tokens(b)
    if not tokens_a or not tokens_b:
        return 0.0
    return len(tokens_a & tokens_b) / len(tokens_a | tokens_b)


class LongTermMemory:
    """Durable Redis long-term store (``core:longterm:{owner}``)."""

    def __init__(self, config: Config, cache: RedisCache) -> None:
        self.config = config
        self.cache = cache
        self._locks: dict[str, asyncio.Lock] = {}

    def key(self, owner: str) -> str:
        return longterm_key(owner)

    def lock(self, owner: str) -> asyncio.Lock:
        """Per-owner mutation lock (HTTP routes + engines share it)."""
        lock = self._locks.get(owner)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[owner] = lock
        return lock

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
        pinned: bool = False,
        source_ids: list[str] | None = None,
    ) -> dict:
        if kind not in MEMORY_KINDS:
            raise ValueError(f"unknown memory kind: {kind!r}")
        now = created_ts if created_ts is not None else time.time()
        meta = dict(metadata or {})
        if source_ids:
            meta["source_ids"] = list(source_ids)[:32]
        meta.setdefault("schema_version", SCHEMA_VERSION)
        return {
            "id": record_id or f"mem_{uuid.uuid4().hex}",
            "kind": kind,
            "text": text,
            "source": source,
            "source_mode": source_mode,
            "importance": float(max(0.0, min(1.0, importance))),
            "created_ts": now,
            "updated_ts": now,
            "pinned": bool(pinned),
            "metadata": meta,
        }

    # -- writes ----------------------------------------------------------------

    async def add(self, owner: str, record: dict) -> dict:
        """Upsert by id; bounded at MEMORY_MAX_PER_USER rows."""
        async with self.lock(owner):
            return await self._add_locked(owner, record)

    async def _add_locked(self, owner: str, record: dict) -> dict:
        rows = await self.records(owner)
        result, _ = self.rows_after_add(rows, record)
        await self._write_rows(owner, result)
        return record

    def rows_after_add(
        self, rows: list[dict], record: dict
    ) -> tuple[list[dict], set[str]]:
        """Build the bounded replacement and report ids it would evict."""
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
                for row in sorted(
                    droppable,
                    key=lambda r: float(r.get("updated_ts", 0) or 0),
                )[:overflow]
            }
            result = [row for row in result if row["id"] not in drop_ids]
        else:
            drop_ids = set()
        return result, drop_ids

    async def _write_rows(self, owner: str, rows: list[dict]) -> None:
        pipe = self.cache.client.pipeline(transaction=True)
        pipe.delete(self.key(owner))
        for row in rows:
            pipe.rpush(self.key(owner), json.dumps(row))
        await pipe.execute()

    async def upsert_merged(
        self,
        owner: str,
        record: dict,
        *,
        now_ts: float | None = None,
        semantic_ids: list[str] | None = None,
        token_fallback: bool = True,
    ) -> tuple[dict, bool]:
        """Merge-by-text upsert (plan section 20.4.1).

        Exact normalized text or a near-duplicate (similarity >=
        ``MERGE_SIMILARITY`` within the same kind) updates the existing row:
        new text wins, importance takes the max, provenance source ids
        accumulate, ``created_ts``/``pinned`` are preserved. Returns
        ``(record, merged)`` where ``merged`` says an existing row absorbed
        the proposal.
        """
        kind = record.get("kind")
        if kind not in MEMORY_KINDS:
            raise ValueError(f"unknown memory kind: {kind!r}")
        now = time.time() if now_ts is None else now_ts
        async with self.lock(owner):
            rows = await self.records(owner)
            stored, merged, result, _ = self.merge_rows(
                rows,
                record,
                now_ts=now,
                semantic_ids=semantic_ids,
                token_fallback=token_fallback,
            )
            await self._write_rows(owner, result)
            return stored, merged

    def merge_rows(
        self,
        rows: list[dict],
        record: dict,
        *,
        now_ts: float,
        semantic_ids: list[str] | None = None,
        token_fallback: bool = True,
    ) -> tuple[dict, bool, list[dict], set[str]]:
        kind = record.get("kind")
        normalized = normalize_text(record.get("text", ""))
        target = None
        for row in rows:
            if row.get("kind") != kind:
                continue
            existing_text = str(row.get("text", ""))
            if normalize_text(existing_text) == normalized:
                target = row
                break
        if target is None and semantic_ids is not None:
            rows_by_id = {
                str(row.get("id")): row
                for row in rows
                if row.get("kind") == kind
            }
            target = next(
                (
                    rows_by_id[record_id]
                    for record_id in semantic_ids
                    if record_id in rows_by_id
                ),
                None,
            )
        if target is None and token_fallback:
            for row in rows:
                if row.get("kind") != kind:
                    continue
                existing_text = str(row.get("text", ""))
                if (
                    token_overlap(existing_text, record.get("text", ""))
                    >= MERGE_SIMILARITY
                ):
                    target = row
                    break
        if target is None:
            stored = dict(record)
            stored["created_ts"] = float(record.get("created_ts", 0) or now_ts)
            stored["updated_ts"] = now_ts
            result, evicted = self.rows_after_add(rows, stored)
            return stored, False, result, evicted

        merged = dict(target)
        merged["text"] = str(record.get("text", target.get("text", "")))
        merged["importance"] = max(
            float(target.get("importance", 0.0)),
            float(record.get("importance", 0.0)),
        )
        merged["updated_ts"] = now_ts
        metadata = dict(target.get("metadata") or {})
        metadata.update(dict(record.get("metadata") or {}))
        prior_sources = list(metadata.get("source_ids") or [])
        for source_id in list(record.get("metadata", {}).get("source_ids") or []):
            if source_id and source_id not in prior_sources:
                prior_sources.append(source_id)
        metadata["source_ids"] = prior_sources[:32]
        metadata.setdefault("schema_version", SCHEMA_VERSION)
        merged["metadata"] = metadata
        result = [
            merged if row.get("id") == target.get("id") else row for row in rows
        ]
        return merged, True, result, set()

    async def patch(
        self,
        owner: str,
        record_id: str,
        *,
        text: str | None = None,
        importance: float | None = None,
        pinned: bool | None = None,
        metadata: dict | None = None,
    ) -> dict | None:
        """Partial admin update; unknown callers validate fields first."""
        async with self.lock(owner):
            rows = await self.records(owner)
            for index, row in enumerate(rows):
                if row.get("id") != record_id:
                    continue
                updated = dict(row)
                if text is not None:
                    updated["text"] = text
                if importance is not None:
                    updated["importance"] = float(max(0.0, min(1.0, importance)))
                if pinned is not None:
                    updated["pinned"] = bool(pinned)
                if metadata is not None:
                    merged_meta = dict(updated.get("metadata") or {})
                    merged_meta.update(metadata)
                    updated["metadata"] = merged_meta
                updated["updated_ts"] = time.time()
                rows[index] = updated
                await self._write_rows(owner, rows)
                return updated
        return None

    async def delete(self, owner: str, record_id: str) -> dict | None:
        """Delete one row. Pinned rows refuse with ``pinned_memory``."""
        async with self.lock(owner):
            rows = await self.records(owner)
            target = next((row for row in rows if row.get("id") == record_id), None)
            if target is None:
                return None
            if target.get("pinned"):
                raise PinnedMemoryError(record_id)
            await self._write_rows(
                owner, [row for row in rows if row.get("id") != record_id]
            )
            return target

    async def delete_many(self, owner: str, record_ids: list[str]) -> int:
        drop = set(record_ids)
        async with self.lock(owner):
            rows = await self.records(owner)
            remaining = [row for row in rows if row.get("id") not in drop]
            removed = len(rows) - len(remaining)
            if removed:
                await self._write_rows(owner, remaining)
            return removed

    # -- reads -----------------------------------------------------------------

    async def records(
        self,
        owner: str,
        kind: str | None = None,
        limit: int | None = None,
        kinds: tuple[str, ...] | list[str] | None = None,
        source_mode: str | None = None,
        pinned: bool | None = None,
    ) -> list[dict]:
        raw_rows = await self.cache.get_rows(self.key(owner))
        kind_filter = set(kinds) if kinds else None
        rows: list[dict] = []
        for raw in raw_rows:
            try:
                row = json.loads(raw)
            except json.JSONDecodeError:
                log.warning("Skipping malformed long-term memory row")
                continue
            if not isinstance(row, dict):
                continue
            if kind is not None and row.get("kind") != kind:
                continue
            if kind_filter is not None and row.get("kind") not in kind_filter:
                continue
            if source_mode is not None and row.get("source_mode") != source_mode:
                continue
            if pinned is not None and bool(row.get("pinned")) is not pinned:
                continue
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

    async def search(
        self,
        owner: str,
        query: str,
        *,
        kinds: tuple[str, ...] | list[str] | None = None,
        limit: int = 8,
    ) -> list[dict]:
        """Deterministic fallback ranking: token overlap + importance +
        recency (plan section 20.3 degraded mode)."""
        if not query.strip():
            return []
        scored: list[tuple[float, dict]] = []
        now = time.time()
        for row in await self.records(owner, kinds=kinds):
            overlap = token_overlap(str(row.get("text", "")), query)
            if overlap <= 0.0:
                continue
            age_days = max(
                0.0,
                (now - float(row.get("updated_ts", 0) or 0)) / 86400.0,
            )
            recency = max(0.0, 1.0 - age_days / 90.0)
            score = overlap + 0.3 * float(row.get("importance", 0.0)) + 0.2 * recency
            scored.append((score, row))
        scored.sort(key=lambda pair: (-pair[0], pair[1].get("id", "")))
        return [row for _, row in scored[: max(0, limit)]]

    # -- cleanup (plan section 20.5) ---------------------------------------------

    def cleanup_candidates(self, rows: list[dict], now_ts: float) -> list[dict]:
        """Deterministic cleanup proposal: age-expired conversation-family
        and life-family rows, never pinned, never protected kinds, never
        important project facts."""
        floor = self.config.MEMORY_PROTECTED_PROJECT_FLOOR
        conversation_ttl = self.config.MEMORY_CONVERSATION_TTL_DAYS * 86400.0
        life_ttl = self.config.MEMORY_LIFE_TTL_DAYS * 86400.0
        candidates: list[dict] = []
        for row in rows:
            if row.get("pinned"):
                continue
            kind = row.get("kind")
            if kind in ("user_profile", "relationship"):
                continue
            if kind == "project" and float(row.get("importance", 0.0)) >= floor:
                continue
            if kind in _CONVERSATION_KINDS:
                ttl = conversation_ttl
            elif kind in ("character_life_event", "character_life_chapter", "commitment", "project"):
                ttl = life_ttl
            else:
                continue
            updated = float(row.get("updated_ts", 0) or 0)
            if updated and now_ts - updated > ttl:
                candidates.append(row)
        return candidates

    async def cleanup(self, owner: str, *, dry_run: bool = False) -> dict:
        now_ts = time.time()
        async with self.lock(owner):
            rows = await self.records(owner)
            candidates = self.cleanup_candidates(rows, now_ts)
            if dry_run or not candidates:
                return {
                    "dry_run": dry_run,
                    "deleted": [],
                    "count": len(candidates),
                }
            drop = {row["id"] for row in candidates}
            remaining = [row for row in rows if row.get("id") not in drop]
            await self._write_rows(owner, remaining)
            return {
                "dry_run": False,
                "deleted": [row["id"] for row in candidates],
                "count": len(candidates),
            }


class PinnedMemoryError(RuntimeError):
    """Deleting a pinned row is refused (plan section 20.5)."""

    def __init__(self, record_id: str) -> None:
        super().__init__(f"memory row {record_id} is pinned")
        self.record_id = record_id


class ChromaWipeError(RuntimeError):
    """The optional index could not confirm deletion of an owner's rows."""


class ChromaDeleteError(RuntimeError):
    """The optional index could not confirm deletion of selected rows."""


class MemoryBackend:
    """Facade: durable Redis store of record + optional Chroma index.

    Every row always lives in Redis; Chroma only accelerates semantic
    ordering. Any Chroma failure flips the backend into degraded mode and
    Redis (deterministic ranking) answers queries instead.
    """

    def __init__(self, config: Config, cache: RedisCache) -> None:
        self.config = config
        self.redis = LongTermMemory(config, cache)
        self.chroma: ChromaMemoryStore | None = (
            ChromaMemoryStore(config)
            if config.CHROMA_ENABLED or config.CHROMA_REQUIRED
            else None
        )
        self._reconciled_owners: set[str] = set()

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        if self.chroma is not None:
            self.chroma.start()
            log.info(
                "Chroma long-term memory %s",
                "available" if (self.chroma and self.chroma.available) else "degraded",
            )
        if self.config.CHROMA_REQUIRED and (
            self.chroma is None or not self.chroma.available
        ):
            raise RuntimeError("Chroma is required but unavailable at startup")

    @property
    def degraded(self) -> bool:
        return self.chroma is not None and not self.chroma.available

    @property
    def backend_name(self) -> str:
        if self.chroma is None:
            return "redis_fallback"
        return "chroma+redis" if self.chroma.available else "chroma_degraded_redis"

    def lock(self, owner: str) -> asyncio.Lock:
        return self.redis.lock(owner)

    def make_record(self, **kwargs) -> dict:
        return self.redis.make_record(**kwargs)

    # -- writes ----------------------------------------------------------------

    async def add(self, owner: str, record: dict) -> dict:
        async with self.redis.lock(owner):
            rows = await self.redis.records(owner)
            result, evicted = self.redis.rows_after_add(rows, record)
            await self._delete_index_strict(owner, evicted)
            await self.redis._write_rows(owner, result)
            await self._index(owner, [record])
            return record

    async def upsert_merged(
        self, owner: str, record: dict, *, now_ts: float | None = None
    ) -> tuple[dict, bool]:
        now = time.time() if now_ts is None else now_ts
        async with self.redis.lock(owner):
            rows = await self.redis.records(owner)
            semantic_ids: list[str] | None = None
            token_fallback = True
            if self.chroma is not None and self.chroma.available:
                try:
                    await self.reconcile_owner(owner)
                    candidates = await asyncio.to_thread(
                        self.chroma.query_candidates,
                        owner,
                        str(record.get("text", "")),
                        [str(record.get("kind", ""))],
                        8,
                    )
                    semantic_ids = [
                        record_id
                        for record_id, similarity in candidates
                        if similarity >= MERGE_SIMILARITY
                    ]
                    token_fallback = False
                except Exception:  # noqa: BLE001
                    log.warning("Chroma merge query failed; degrading", exc_info=True)
                    self.chroma.available = False
                    self._reconciled_owners.discard(owner)
            stored, merged, result, evicted = self.redis.merge_rows(
                rows,
                record,
                now_ts=now,
                semantic_ids=semantic_ids,
                token_fallback=token_fallback,
            )
            await self._delete_index_strict(owner, evicted)
            await self.redis._write_rows(owner, result)
            await self._index(owner, [stored])
            return stored, merged

    async def patch(self, owner: str, record_id: str, **fields) -> dict | None:
        updated = await self.redis.patch(owner, record_id, **fields)
        if updated is not None:
            await self._index(owner, [updated])
        return updated

    async def delete(self, owner: str, record_id: str) -> dict | None:
        async with self.redis.lock(owner):
            rows = await self.redis.records(owner)
            removed = next((row for row in rows if row.get("id") == record_id), None)
            if removed is None:
                return None
            if removed.get("pinned"):
                raise PinnedMemoryError(record_id)
            await self._delete_index_strict(owner, {record_id})
            await self.redis._write_rows(
                owner, [row for row in rows if row.get("id") != record_id]
            )
            return removed

    async def delete_many(self, owner: str, record_ids: list[str]) -> int:
        drop = set(record_ids)
        async with self.redis.lock(owner):
            rows = await self.redis.records(owner)
            present = {
                str(row.get("id")) for row in rows if row.get("id") in drop
            }
            await self._delete_index_strict(owner, present)
            remaining = [row for row in rows if row.get("id") not in drop]
            if len(remaining) != len(rows):
                await self.redis._write_rows(owner, remaining)
            return len(rows) - len(remaining)

    async def cleanup(self, owner: str, *, dry_run: bool = False) -> dict:
        now_ts = time.time()
        async with self.redis.lock(owner):
            rows = await self.redis.records(owner)
            candidates = self.redis.cleanup_candidates(rows, now_ts)
            deleted = [str(row["id"]) for row in candidates]
            if dry_run or not deleted:
                return {"dry_run": dry_run, "deleted": [], "count": len(candidates)}
            await self._delete_index_strict(owner, set(deleted))
            drop = set(deleted)
            await self.redis._write_rows(
                owner, [row for row in rows if row.get("id") not in drop]
            )
            return {"dry_run": False, "deleted": deleted, "count": len(deleted)}

    async def wipe(self, owner: str) -> int:
        async with self.redis.lock(owner):
            rows = await self.redis.records(owner)
            if self.chroma is not None:
                if not self.chroma.available:
                    raise ChromaWipeError("Chroma is unavailable")
                try:
                    await asyncio.to_thread(self.chroma.delete_owner, owner)
                    if await asyncio.to_thread(self.chroma.count, owner):
                        raise RuntimeError("Chroma retained owner rows")
                except Exception as exc:  # noqa: BLE001
                    log.warning("Chroma owner wipe failed; degrading", exc_info=True)
                    self.chroma.available = False
                    self._reconciled_owners.discard(owner)
                    raise ChromaWipeError("Chroma owner wipe failed") from exc
            await self.redis._write_rows(owner, [])
            self._reconciled_owners.discard(owner)
            return len(rows)

    # -- reads -----------------------------------------------------------------

    async def records(self, owner: str, **filters) -> list[dict]:
        return await self.redis.records(owner, **filters)

    async def get(self, owner: str, record_id: str) -> dict | None:
        return await self.redis.get(owner, record_id)

    async def count(self, owner: str) -> int:
        return await self.redis.count(owner)

    async def search(
        self,
        owner: str,
        query: str,
        *,
        kinds: tuple[str, ...] | list[str] | None = None,
        limit: int = 8,
    ) -> list[dict]:
        if self.chroma is not None and self.chroma.available:
            try:
                await self.reconcile_owner(owner)
                order = await asyncio.to_thread(
                    self.chroma.query, owner, query, list(kinds or []), limit * 4
                )
                rows_by_id = {
                    row["id"]: row
                    for row in await self.redis.records(owner, kinds=kinds)
                }
                ranked = [
                    rows_by_id[record_id]
                    for record_id in order
                    if record_id in rows_by_id
                ]
                return ranked[: max(0, limit)]
            except Exception:  # noqa: BLE001 - degraded mode boundary
                log.warning("Chroma query failed; degrading", exc_info=True)
                self.chroma.available = False
                self._reconciled_owners.discard(owner)
        return await self.redis.search(owner, query, kinds=kinds, limit=limit)

    # -- internals ---------------------------------------------------------------

    async def _index(self, owner: str, rows: list[dict]) -> None:
        if self.chroma is None or not rows:
            return
        try:
            await asyncio.to_thread(self.chroma.upsert, owner, rows)
        except Exception:  # noqa: BLE001 - index-only failure degrades
            log.warning("Chroma upsert failed; degrading", exc_info=True)
            self.chroma.available = False
            self._reconciled_owners.discard(owner)

    async def reconcile_owner(
        self, owner: str, *, extra_rows: list[dict] | None = None
    ) -> None:
        """Upsert Redis rows missing from an available optional index."""
        if (
            self.chroma is None
            or not self.chroma.available
            or owner in self._reconciled_owners
        ):
            return
        rows = await self.redis.records(owner)
        for raw in await self.redis.cache.get_rows(midterm_key(owner)):
            try:
                chapter = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(chapter, dict):
                rows.append(chapter)
        rows.extend(extra_rows or [])
        try:
            desired_ids = {str(row["id"]) for row in rows if row.get("id")}
            stale_ids = (
                set(await asyncio.to_thread(self.chroma.ids, owner)) - desired_ids
            )
            if stale_ids:
                await asyncio.to_thread(self.chroma.delete, list(stale_ids))
                indexed_ids = set(await asyncio.to_thread(self.chroma.ids, owner))
                if stale_ids & indexed_ids:
                    raise RuntimeError("Chroma retained stale rows")
            if rows:
                await asyncio.to_thread(self.chroma.upsert, owner, rows)
            self._reconciled_owners.add(owner)
        except Exception:  # noqa: BLE001
            log.warning("Chroma reconciliation failed; degrading", exc_info=True)
            self.chroma.available = False
            self._reconciled_owners.discard(owner)
            if self.config.CHROMA_REQUIRED:
                raise RuntimeError("Chroma reconciliation failed")

    async def index_chapter(self, owner: str, record: dict) -> None:
        """Index a mid-term chapter row that lives in its own ring key
        (plan section 20.2 step 3: chapter in Redis and optionally Chroma)."""
        await self._index(owner, [record])

    async def delete_index_ids(self, owner: str, record_ids: set[str]) -> None:
        """Delete evicted index rows before their Redis source rows disappear."""
        await self._delete_index_strict(owner, record_ids)

    async def _delete_index_strict(self, owner: str, record_ids: set[str]) -> None:
        if not record_ids or self.chroma is None:
            return
        if not self.chroma.available:
            raise ChromaDeleteError("Chroma is unavailable")
        try:
            await asyncio.to_thread(self.chroma.delete, sorted(record_ids))
            indexed_ids = set(await asyncio.to_thread(self.chroma.ids, owner))
            if record_ids & indexed_ids:
                raise RuntimeError("Chroma retained deleted rows")
        except Exception as exc:  # noqa: BLE001
            log.warning("Chroma delete failed; preserving Redis rows", exc_info=True)
            self.chroma.available = False
            self._reconciled_owners.discard(owner)
            raise ChromaDeleteError("Chroma row deletion failed") from exc


__all__ = [
    "MEMORY_KINDS",
    "EXTRACTION_KINDS",
    "MERGE_SIMILARITY",
    "LongTermMemory",
    "MemoryBackend",
    "PinnedMemoryError",
    "ChromaWipeError",
    "ChromaDeleteError",
    "normalize_text",
    "token_overlap",
]
