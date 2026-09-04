"""Optional Chroma semantic index over the durable memory rows (plan 20.3).

Chroma is an optional dependency: this module imports it lazily and any
startup or runtime failure only degrades semantic search to the
deterministic token-overlap ranking in ``core.memory`` — Redis stays the
store of record and nothing in the chat path depends on Chroma.

One collection holds every owner (single-owner deployments in practice);
the owner id rides in row metadata so wipe/delete are precise. All methods
are synchronous; callers wrap them in ``asyncio.to_thread``.
"""

from __future__ import annotations

import logging

from .config import Config

log = logging.getLogger("bridge.chroma")

COLLECTION_NAME = "bridge_memories"


class ChromaMemoryStore:
    """Lazy, failure-tolerant Chroma collection wrapper."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self._client = None
        self._collection = None
        self.available = False

    def start(self) -> None:
        """Connect once at startup; failure leaves the backend degraded."""
        try:
            import chromadb  # noqa: PLC0415 - optional dependency

            self._client = chromadb.PersistentClient(path=self.config.CHROMA_PATH)
            self._collection = self._client.get_or_create_collection(
                COLLECTION_NAME,
                metadata={"hnsw:space": "cosine"},
            )
            self.available = True
        except Exception:  # noqa: BLE001 - degraded mode boundary
            self._client = None
            self._collection = None
            self.available = False
            log.warning("Chroma unavailable at startup; long-term memory degrades")

    def upsert(self, owner: str, rows: list[dict]) -> None:
        if self._collection is None:
            return
        self._collection.upsert(
            ids=[str(row["id"]) for row in rows],
            documents=[str(row.get("text", "")) for row in rows],
            metadatas=[self._metadata(owner, row) for row in rows],
        )

    def delete(self, record_ids: list[str]) -> None:
        if self._collection is None:
            return
        self._collection.delete(ids=[str(record_id) for record_id in record_ids])

    def delete_owner(self, owner: str) -> None:
        if self._collection is None:
            return
        self._collection.delete(where={"owner": owner})

    def query(
        self, owner: str, text: str, kinds: list[str] | None, limit: int
    ) -> list[str]:
        """Semantic id ordering (best match first)."""
        return [
            record_id
            for record_id, _ in self.query_candidates(owner, text, kinds, limit)
        ]

    def query_candidates(
        self, owner: str, text: str, kinds: list[str] | None, limit: int
    ) -> list[tuple[str, float]]:
        """Semantic ids with cosine similarity (best match first)."""
        if self._collection is None or not text.strip() or limit <= 0:
            return []
        where: dict = {"owner": owner}
        if kinds:
            where = {"$and": [{"owner": owner}, {"kind": {"$in": list(kinds)}}]}
        result = self._collection.query(
            query_texts=[text],
            n_results=max(1, min(limit, 200)),
            where=where,
            include=["distances"],
        )
        ids = (result or {}).get("ids") or []
        distances = (result or {}).get("distances") or []
        if ids and isinstance(ids[0], list):
            ids = ids[0]
        if distances and isinstance(distances[0], list):
            distances = distances[0]
        return [
            (str(record_id), max(-1.0, min(1.0, 1.0 - float(distance))))
            for record_id, distance in zip(ids or [], distances or [])
        ]

    def ids(self, owner: str) -> list[str]:
        if self._collection is None:
            return []
        result = self._collection.get(where={"owner": owner}, include=[])
        return [str(record_id) for record_id in (result or {}).get("ids") or []]

    def count(self, owner: str) -> int:
        if self._collection is None:
            return 0
        result = self._collection.get(where={"owner": owner}, include=[])
        return len((result or {}).get("ids") or [])

    @staticmethod
    def _metadata(owner: str, row: dict) -> dict:
        return {
            "owner": owner,
            "kind": str(row.get("kind", "")),
            "source_mode": str(row.get("source_mode", "")),
            "importance": float(row.get("importance", 0.0)),
            "pinned": bool(row.get("pinned")),
            "created_ts": float(row.get("created_ts", 0.0)),
            "updated_ts": float(row.get("updated_ts", 0.0)),
        }


__all__ = ["ChromaMemoryStore"]
