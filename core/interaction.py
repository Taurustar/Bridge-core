"""Deferred-message queue and busy ladder counters (plan section 16.3).

When the character's effective availability is ``busy`` or ``unavailable``,
companion messages are stored — never answered with fabricated speech — and
one catch-up response answers them when availability returns to
free/soft_busy.

Queue rules (plan section 16.3):

- Maximum 5 entries and 4,000 UTF-8 characters total; oldest entries drop
  first with a warning.
- Deduplicated by original ``message_id`` across devices.
- Arrival order preserved.
- Expired entries delete without answer; expiry counts as bounded
  diagnostics.
- Entries carry their mode so work and companion hooks stay separated.
- No message text is ever written to audit stores; the queue exists solely
  to answer the owner, and entries expire after 48 hours.

All mutators run under the per-owner catch-up lock held by the bridge.
Storage is one JSON document at ``core:deferred:{owner}``. The busy ladder's
first-message tracker lives at ``core:busy_count:{owner}``.
"""

from __future__ import annotations

import json
import logging
import time
import uuid

from .cache import RedisCache
from .config import Config
from .constants import (
    DEFERRED_MAX_ENTRIES,
    DEFERRED_MAX_TOTAL_CHARS,
    DEFERRED_TTL_SECONDS,
    busy_count_key,
    deferred_key,
)

log = logging.getLogger("bridge.interaction")


def new_defer_id() -> str:
    return f"defer_{uuid.uuid4().hex}"


class DeferredQueue:
    """Bounded deferred-message store (plan section 16.3)."""

    def __init__(self, config: Config, cache: RedisCache) -> None:
        self.config = config
        self.cache = cache

    # -- queue document ---------------------------------------------------------

    async def _read(self, owner: str) -> dict:
        raw = await self.cache.get_value(deferred_key(owner))
        if not raw:
            return {"entries": [], "expired_count": 0, "dropped_count": 0}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("Corrupt deferred queue ignored")
            return {"entries": [], "expired_count": 0, "dropped_count": 0}
        if not isinstance(data, dict):
            return {"entries": [], "expired_count": 0, "dropped_count": 0}
        data.setdefault("entries", [])
        data.setdefault("expired_count", 0)
        data.setdefault("dropped_count", 0)
        return data

    async def _write(self, owner: str, doc: dict) -> None:
        await self.cache.set_value(deferred_key(owner), json.dumps(doc))

    # -- mutations ----------------------------------------------------------------

    async def append(
        self,
        owner: str,
        *,
        message_id: str,
        mode: str,
        text: str,
        source_connection_id: str,
        now_ts: float | None = None,
    ) -> dict:
        """Store one deferred message. Returns the stored entry.

        Oldest entries drop first when the count or total-character caps are
        exceeded (plan section 16.3). Deduplication by message id.
        """
        now_ts = now_ts if now_ts is not None else time.time()
        doc = await self._read(owner)
        entries: list[dict] = list(doc["entries"])
        text = text[:DEFERRED_MAX_TOTAL_CHARS]
        entry = {
            "id": new_defer_id(),
            "message_id": message_id,
            "mode": mode,
            "text": text,
            "created_ts": now_ts,
            "expires_ts": now_ts + DEFERRED_TTL_SECONDS,
            "source_connection_id": source_connection_id,
            "state": "held",
        }
        # Dedupe by original message id, preserving arrival order (plan 16.3).
        for index, existing in enumerate(entries):
            if existing.get("message_id") == message_id:
                entries[index] = entry
                break
        else:
            entries.append(entry)
        # Enforce caps: total UTF-8 characters, then count; oldest drop first.
        total = sum(len(e["text"].encode("utf-8")) for e in entries)
        while entries and (
            len(entries) > DEFERRED_MAX_ENTRIES or total > DEFERRED_MAX_TOTAL_CHARS
        ):
            dropped = entries.pop(0)
            total -= len(dropped["text"].encode("utf-8"))
            doc["dropped_count"] = int(doc["dropped_count"]) + 1
            log.warning(
                "Deferred queue cap reached; dropped oldest entry (mode=%s)",
                dropped.get("mode"),
            )
        doc["entries"] = entries
        await self._write(owner, doc)
        return entry

    async def sweep_expired(self, owner: str, now_ts: float | None = None) -> int:
        now_ts = now_ts if now_ts is not None else time.time()
        doc = await self._read(owner)
        live = [e for e in doc["entries"] if float(e.get("expires_ts", 0)) > now_ts]
        expired = len(doc["entries"]) - len(live)
        if expired:
            doc["expired_count"] = int(doc["expired_count"]) + expired
            doc["entries"] = live
            await self._write(owner, doc)
        return expired

    async def held(self, owner: str, mode: str, now_ts: float | None = None) -> list[dict]:
        now_ts = now_ts if now_ts is not None else time.time()
        doc = await self._read(owner)
        return [
            e
            for e in doc["entries"]
            if e.get("mode") == mode
            and e.get("state") == "held"
            and float(e.get("expires_ts", 0)) > now_ts
        ]

    async def claim(self, owner: str, mode: str, now_ts: float | None = None) -> list[dict]:
        """Atomically claim held entries (``held -> delivering``).

        Caller holds the per-owner catch-up lock (plan section 16.3). Expired
        entries are dropped without answer first.
        """
        now_ts = now_ts if now_ts is not None else time.time()
        await self.sweep_expired(owner, now_ts)
        doc = await self._read(owner)
        claimed: list[dict] = []
        for entry in doc["entries"]:
            if entry.get("mode") == mode and entry.get("state") == "held":
                entry["state"] = "delivering"
                claimed.append(entry)
        if claimed:
            await self._write(owner, doc)
        return claimed

    async def restore(
        self, owner: str, entries: list[dict], now_ts: float | None = None
    ) -> None:
        """Return claimed entries to ``held`` unless expired (plan 16.3)."""
        now_ts = now_ts if now_ts is not None else time.time()
        doc = await self._read(owner)
        by_id = {e["id"]: e for e in entries}
        restored = 0
        for entry in doc["entries"]:
            if entry.get("id") in by_id and entry.get("state") == "delivering":
                if float(entry.get("expires_ts", 0)) > now_ts:
                    entry["state"] = "held"
                    restored += 1
                # Expired entries simply never return to held; the next
                # sweep deletes them.
        if restored:
            await self._write(owner, doc)

    async def remove(self, owner: str, entry_ids: list[str]) -> int:
        doc = await self._read(owner)
        drop = set(entry_ids)
        before = len(doc["entries"])
        doc["entries"] = [e for e in doc["entries"] if e.get("id") not in drop]
        removed = before - len(doc["entries"])
        if removed:
            if doc["entries"]:
                await self._write(owner, doc)
            else:
                # An emptied queue leaves no key behind.
                await self.cache.delete(deferred_key(owner))
        return removed

    async def held_count(self, owner: str, mode: str) -> int:
        return len(await self.held(owner, mode))

    # -- busy ladder counter --------------------------------------------------------

    async def busy_count(self, owner: str) -> int:
        raw = await self.cache.get_value(busy_count_key(owner))
        try:
            return max(0, int(raw)) if raw else 0
        except ValueError:
            return 0

    async def increment_busy(self, owner: str) -> int:
        count = await self.busy_count(owner) + 1
        await self.cache.set_value(busy_count_key(owner), str(count))
        return count

    async def reset_busy(self, owner: str) -> None:
        await self.cache.delete(busy_count_key(owner))
