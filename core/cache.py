"""Async Redis cache wrapper (plan sections 4, 6.1, 28).

Redis is a required service. The wrapper only exposes the small async subset
milestone 0.1.0 needs; tests substitute an in-memory fake preserving the same
store contract (see ``tests.fakes.FakeRedis``).
"""

from __future__ import annotations

import logging
from typing import Any, Callable

import redis.asyncio as aioredis
from redis.exceptions import WatchError

log = logging.getLogger("bridge.cache")


class RedisCache:
    """Thin async wrapper over redis.asyncio for the 0.1.0 store contract."""

    def __init__(self, client: Any) -> None:
        self._client = client

    @classmethod
    def connect(cls, host: str, port: int, db: int) -> "RedisCache":
        client = aioredis.Redis(host=host, port=port, db=db, decode_responses=True)
        return cls(client)

    @property
    def client(self) -> Any:
        return self._client

    async def ping(self) -> bool:
        try:
            return bool(await self._client.ping())
        except Exception as exc:  # noqa: BLE001 - degraded, not fatal
            log.warning("Redis ping failed: %s", type(exc).__name__)
            return False

    async def require_ping(self) -> None:
        """Startup check: Redis is a required service."""
        if not await self.ping():
            raise RuntimeError(
                "Redis is unavailable at startup; Bridge Core Engine requires Redis"
            )

    async def append_row(self, key: str, row_json: str, max_rows: int) -> None:
        """Append a history row and cap the list at ``max_rows`` atomically."""
        pipe = self._client.pipeline(transaction=True)
        pipe.rpush(key, row_json)
        pipe.ltrim(key, -max_rows, -1)
        await pipe.execute()

    async def get_rows(self, key: str) -> list[str]:
        return list(await self._client.lrange(key, 0, -1))

    async def atomic_replace_list_once(
        self,
        key: str,
        idempotency_key: str,
        token: str,
        max_tokens: int,
        update: Callable[[list[str]], tuple[list[str] | None, Any]],
    ) -> tuple[bool, bool, Any]:
        """Replace a list and record its mutation token in one transaction.

        Returns ``(applied, duplicate, result)``. An updater can return
        ``None`` for the replacement to leave both lists unchanged.
        """
        while True:
            pipe = self._client.pipeline(transaction=True)
            try:
                await pipe.watch(key, idempotency_key)
                if token in await pipe.lrange(idempotency_key, 0, -1):
                    return False, True, None
                replacement, result = update(list(await pipe.lrange(key, 0, -1)))
                if replacement is None:
                    return False, False, result
                pipe.multi()
                pipe.delete(key)
                for row in replacement:
                    pipe.rpush(key, row)
                pipe.rpush(idempotency_key, token)
                pipe.ltrim(idempotency_key, -max_tokens, -1)
                await pipe.execute()
                return True, False, result
            except WatchError:
                continue
            finally:
                await pipe.reset()

    async def atomic_transform_value_once(
        self,
        key: str,
        idempotency_key: str,
        token: str,
        max_tokens: int,
        update: Callable[[str | None], tuple[str, Any]],
    ) -> tuple[bool, bool, Any]:
        """Transform one string value and commit its mutation token atomically."""
        while True:
            pipe = self._client.pipeline(transaction=True)
            try:
                await pipe.watch(key, idempotency_key)
                if token in await pipe.lrange(idempotency_key, 0, -1):
                    return False, True, None
                replacement, result = update(await pipe.get(key))
                pipe.multi()
                pipe.set(key, replacement)
                pipe.rpush(idempotency_key, token)
                pipe.ltrim(idempotency_key, -max_tokens, -1)
                await pipe.execute()
                return True, False, result
            except WatchError:
                continue
            finally:
                await pipe.reset()

    async def set_row(self, key: str, index: int, row_json: str) -> None:
        await self._client.lset(key, index, row_json)

    async def row_count(self, key: str) -> int:
        return int(await self._client.llen(key))

    async def set_value(self, key: str, value: str) -> None:
        """Store one JSON document (needs/bids/rhythm/profile state)."""
        await self._client.set(key, value)

    async def get_value(self, key: str) -> str | None:
        return await self._client.get(key)

    async def delete(self, key: str) -> None:
        await self._client.delete(key)

    async def keys(self, pattern: str = "*") -> list[str]:
        return list(await self._client.keys(pattern))

    async def close(self) -> None:
        await self._client.aclose()
