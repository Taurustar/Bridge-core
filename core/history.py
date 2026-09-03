"""Companion history persistence (plan sections 12, 28).

Rows are JSON objects in the Redis list ``core:history:{owner}:companion``.
Every mutator runs under the per-owner turn lock (plan section 11), so the
read-modify-write used for delivery-state updates is safe.

Milestone 0.1.0 crash-recovery note: the full ``delivery_unknown`` startup
reconciliation arrives with history APIs in a later milestone; the row schema
carries the field from day one (documented in BRIDGE_CORE_ENGINE_SPEC.md).
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone

from .cache import RedisCache
from .constants import companion_history_key

log = logging.getLogger("bridge.history")

# Delivery states: user rows arrive delivered; assistant rows start pending
# and become delivered/undelivered per plan section 12 steps 26-28.
DELIVERED = "delivered"
PENDING = "pending"
UNDELIVERED = "undelivered"
DELIVERY_UNKNOWN = "delivery_unknown"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_message_id() -> str:
    return f"msg_{uuid.uuid4().hex}"


def make_row(
    role: str,
    text: str,
    delivery_state: str,
    emotion: str = "neutral",
    mode: str = "companion",
) -> dict:
    return {
        "id": new_message_id(),
        "role": role,
        "text": text,
        "emotion": emotion,
        "mode": mode,
        "ts": utc_now_iso(),
        "delivery_state": delivery_state,
    }


def companion_history_key(owner_user_id: str) -> str:
    return f"core:history:{owner_user_id}:companion"


def session_history_key(owner_user_id: str, session_id: str) -> str:
    """Work-session history; never mixed with companion history (25.2)."""
    return f"core:history:{owner_user_id}:session:{session_id}"


async def append_row_to(
    cache: RedisCache, key: str, row: dict, max_rows: int
) -> dict:
    await cache.append_row(key, json.dumps(row), max_rows)
    return row


async def append_row(
    cache: RedisCache, owner: str, row: dict, max_rows: int
) -> dict:
    return await append_row_to(
        cache, companion_history_key(owner), row, max_rows
    )


async def load_rows_from(cache: RedisCache, key: str) -> list[dict]:
    raw_rows = await cache.get_rows(key)
    rows: list[dict] = []
    for raw in raw_rows:
        try:
            row = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("Skipping malformed history row")
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


async def load_rows(cache: RedisCache, owner: str) -> list[dict]:
    return await load_rows_from(cache, companion_history_key(owner))


def delivered_rows(
    rows: list[dict], exclude_id: str | None = None, exclude_ids: set[str] | None = None
) -> list[dict]:
    """Only delivered rows may enter prompts (plan section 12 steps 12, 28)."""
    excluded = exclude_ids or set()
    return [
        row
        for row in rows
        if row.get("delivery_state") == DELIVERED
        and row.get("id") != exclude_id
        and row.get("id") not in excluded
    ]


async def load_prompt_history(
    cache: RedisCache,
    owner: str,
    budget: int,
    exclude_id: str | None = None,
    exclude_ids: set[str] | None = None,
    key: str | None = None,
) -> list[dict]:
    """Bounded prior delivered history for the prompt (any channel)."""
    rows = delivered_rows(
        await load_rows_from(cache, key or companion_history_key(owner)),
        exclude_id=exclude_id,
        exclude_ids=exclude_ids,
    )
    return rows[-budget:] if budget > 0 else []


async def mark_delivery_state_key(
    cache: RedisCache, key: str, message_id: str, state: str
) -> bool:
    """Update one row's delivery state in place, any channel."""
    raw_rows = await cache.get_rows(key)
    for index, raw in enumerate(raw_rows):
        try:
            row = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and row.get("id") == message_id:
            row["delivery_state"] = state
            await cache.set_row(key, index, json.dumps(row))
            return True
    return False


async def mark_delivery_state(
    cache: RedisCache, owner: str, message_id: str, state: str
) -> bool:
    """Update one row's delivery state in place. Caller holds the turn lock."""
    return await mark_delivery_state_key(
        cache, companion_history_key(owner), message_id, state
    )
