"""Shared test doubles.

FakeRedis preserves the store contract used by ``core.cache.RedisCache``
(ping / transactional pipeline rpush+ltrim / lrange / lset / llen / delete /
keys) entirely in memory, so HTTP+WS tests need no live Redis.

FakeLLM is a scriptable stand-in for ``core.llm.LLMRouter``.
"""

from __future__ import annotations

import asyncio
import fnmatch
from collections import deque
from typing import Any

from core.cache import RedisCache
from core.config import Config
from core.llm import LLMChainExhausted, LLMResult


class FakePipeline:
    def __init__(self, store: dict[str, list[str]]) -> None:
        self._store = store
        self._ops: list[tuple] = []

    def rpush(self, key: str, value: str) -> "FakePipeline":
        self._ops.append(("rpush", key, value))
        return self

    def ltrim(self, key: str, start: int, end: int) -> "FakePipeline":
        self._ops.append(("ltrim", key, start, end))
        return self

    async def execute(self) -> list[Any]:
        results: list[Any] = []
        for op in self._ops:
            if op[0] == "rpush":
                _, key, value = op
                self._store.setdefault(key, []).append(value)
                results.append(len(self._store[key]))
            elif op[0] == "ltrim":
                _, key, start, end = op
                rows = self._store.get(key, [])
                n = len(rows)
                lo = start if start >= 0 else max(n + start, 0)
                hi = end if end >= 0 else n + end
                self._store[key] = rows[lo : hi + 1]
                results.append(True)
        self._ops.clear()
        return results


class FakeRedis:
    """In-memory substitute for redis.asyncio.Redis (decode_responses=True)."""

    def __init__(self) -> None:
        self.store: dict[str, list[str]] = {}
        self.up = True

    async def ping(self) -> bool:
        if not self.up:
            raise ConnectionError("fake redis is down")
        return True

    def pipeline(self, transaction: bool = True) -> FakePipeline:
        return FakePipeline(self.store)

    async def lrange(self, key: str, start: int, end: int) -> list[str]:
        rows = self.store.get(key, [])
        n = len(rows)
        lo = start if start >= 0 else max(n + start, 0)
        hi = end if end >= 0 else n + end
        return rows[lo : hi + 1]

    async def lset(self, key: str, index: int, value: str) -> bool:
        self.store[key][index] = value
        return True

    async def llen(self, key: str) -> int:
        return len(self.store.get(key, []))

    async def delete(self, key: str) -> int:
        return 1 if self.store.pop(key, None) is not None else 0

    async def keys(self, pattern: str = "*") -> list[str]:
        return [k for k in self.store if fnmatch.fnmatch(k, pattern)]

    async def aclose(self) -> None:
        pass


class FakeLLM:
    """Scriptable LLM router substitute.

    ``replies`` is a queue; each item is either a raw reply string, an
    Exception instance to raise, or a callable awaited with
    ``(mode, messages)`` returning a raw reply string.

    ``block_gate`` is an optional ``threading.Event``: while unset, chat()
    polls it asynchronously, simulating a provider call that is still running
    (used to prove heartbeat acks are not blocked by an active turn).
    """

    def __init__(self, replies: list | None = None) -> None:
        self._replies: deque = deque(replies or ["[EMOTION: neutral]\nHello."])
        self.calls: list[tuple[str, list[dict]]] = []
        self.block_gate: Any = None

    async def chat(self, mode: str, messages: list[dict]) -> LLMResult:
        self.calls.append((mode, messages))
        if self.block_gate is not None:
            while not self.block_gate.is_set():
                await asyncio.sleep(0.005)
        if not self._replies:
            raise LLMChainExhausted("FakeLLM: no scripted replies left")
        item = self._replies.popleft()
        if isinstance(item, Exception):
            raise item
        if callable(item):
            item = await item(mode, messages)
        return LLMResult(
            text=item,
            provider="fake",
            model="fake-model",
            usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            attempts=1,
        )

    def routes_for(self, mode: str) -> list:
        return []

    async def aclose(self) -> None:
        pass


def make_config(**overrides: Any) -> Config:
    """A valid config without touching the real environment."""
    base = {"OWNER_USER_ID": "owner", "TAILSCALE_REQUIRED": True}
    base.update(overrides)
    return Config(**base)


def make_cache(fake: FakeRedis | None = None) -> tuple[RedisCache, FakeRedis]:
    fake = fake or FakeRedis()
    return RedisCache(fake), fake
