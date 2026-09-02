"""Connection bids and bond refill (plan section 15.5).

Bids are character-initiated attempts at connection. They register only after
confirmed initiative delivery, which arrives with milestone 0.7.0 — in 0.3.0
the store, deterministic reply satisfaction, and bond refill exist and are
inert behind ``BIDS_ENABLED``.

The record is bounded and never stores message text: each bid keeps only id,
kind, size, sent timestamp, expiry, answered timestamp, and result. Amounts
and caps live in ``needs.json`` under ``bids``.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any

from .cache import RedisCache
from .constants import BID_KINDS, bids_key

log = logging.getLogger("bridge.bids")

RESULT_OPEN = "open"
RESULT_ANSWERED = "answered"
RESULT_EXPIRED = "expired"

_MAX_BID_RECORD = 64


def _new_bid_id() -> str:
    return f"bid_{uuid.uuid4().hex}"


def make_bid(
    kind: str,
    now_ts: float | None = None,
    size: float = 1.0,
    lifetime_seconds: float = 1209600.0,
) -> dict:
    if kind not in BID_KINDS:
        raise ValueError(f"Unknown bid kind: {kind!r}")
    sent = time.time() if now_ts is None else float(now_ts)
    return {
        "id": _new_bid_id(),
        "kind": kind,
        "size": float(size),
        "sent_ts": sent,
        "expire_ts": sent + float(lifetime_seconds),
        "answered_ts": 0,
        "result": RESULT_OPEN,
    }


class BidsEngine:
    """Bounded owner bid record under ``core:bids:{owner}`` (plan 15.5)."""

    def __init__(self, config: Any, cache: RedisCache) -> None:
        self.config = config
        self.cache = cache

    @property
    def available(self) -> bool:
        return bool(self.config.BIDS_ENABLED)

    def key(self, owner: str) -> str:
        return bids_key(owner)

    async def _load(self, owner: str) -> list[dict]:
        raw = await self.cache.get_value(self.key(owner))
        if not raw:
            return []
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("Corrupt bids record ignored for owner")
            return []
        return data if isinstance(data, list) else []

    async def _save(self, owner: str, bids: list[dict]) -> None:
        bounded = bids[-_MAX_BID_RECORD:]
        await self.cache.set_value(self.key(owner), json.dumps(bounded))

    async def open_bids(self, owner: str, now_ts: float | None = None) -> list[dict]:
        current = time.time() if now_ts is None else float(now_ts)
        return [
            bid
            for bid in await self._load(owner)
            if bid.get("result") == RESULT_OPEN and float(bid.get("expire_ts", 0)) > current
        ]

    async def register_bid(
        self, owner: str, kind: str, size: float = 1.0, now_ts: float | None = None,
        lifetime_seconds: float = 1209600.0,
    ) -> dict:
        """Open one bid. Callers count initiative only after delivery (0.7.0)."""
        bids = await self._load(owner)
        bid = make_bid(kind, now_ts=now_ts, size=size, lifetime_seconds=lifetime_seconds)
        bids.append(bid)
        await self._save(owner, bids)
        return bid

    async def satisfy_open_bids(
        self, owner: str, text: str, bonus: float = 2.0, min_reply_length: int = 8
    ) -> int:
        """Deterministic owner-reply satisfaction; no LLM (plan 15.5).

        A substantive reply (at or above the minimum length) answers every
        open bid; tiny replies answer none. Returns the count answered.
        """
        if len(text.strip()) < int(min_reply_length):
            return 0
        now_ts = time.time()
        bids = await self._load(owner)
        answered = 0
        for bid in bids:
            if bid.get("result") != RESULT_OPEN:
                continue
            if float(bid.get("expire_ts", 0)) <= now_ts:
                continue
            bid["result"] = RESULT_ANSWERED
            bid["answered_ts"] = now_ts
            bid["size"] = float(bid.get("size", 1.0)) + float(bonus)
            answered += 1
        if answered:
            await self._save(owner, bids)
        return answered

    async def sweep_expired(self, owner: str, now_ts: float | None = None) -> int:
        """Mark lapsed open bids expired; runs in lifespan/heartbeat maintenance."""
        current = time.time() if now_ts is None else float(now_ts)
        bids = await self._load(owner)
        changed = 0
        for bid in bids:
            if bid.get("result") == RESULT_OPEN and float(bid.get("expire_ts", 0)) <= current:
                bid["result"] = RESULT_EXPIRED
                changed += 1
        if changed:
            await self._save(owner, bids)
        return changed
