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
from core.speech import TTSError


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


class FakeSTT:
    """Scriptable STT service substitute.

    ``transcripts`` is a queue of transcripts returned in order (missing ->
    ""). ``error`` raises before consuming a transcript, simulating provider
    failure. ``available_flag`` toggles the availability answer.
    """

    provider_name = "fake"

    def __init__(
        self,
        transcripts: list[str] | None = None,
        error: Exception | None = None,
        available_flag: bool = True,
    ) -> None:
        self._transcripts: deque = deque(transcripts or [])
        self._error = error
        self.available_flag = available_flag
        self.calls: list[tuple[bytes, str, str]] = []

    def available(self) -> bool:
        return self.available_flag

    async def transcribe(self, audio: bytes, content_type: str, language: str) -> str:
        self.calls.append((audio, content_type, language))
        if self._error is not None:
            raise self._error
        if self._transcripts:
            return self._transcripts.popleft()
        return ""

    async def aclose(self) -> None:
        pass


class FakeTTS:
    """Scriptable TTS service substitute.

    Synthesis returns deterministic bytes per chunk; ``fail_texts`` makes the
    matching chunk texts raise ``TTSError`` so tests can prove failed TTS
    never removes the text reply.
    """

    def __init__(
        self,
        fail_texts: frozenset[str] | set[str] = frozenset(),
        available_flag: bool = True,
    ) -> None:
        self.fail_texts = set(fail_texts)
        self.available_flag = available_flag
        self.calls: list[tuple[str, str]] = []

    def available(self) -> bool:
        return self.available_flag

    @property
    def audio_format(self) -> str:
        return "mp3"

    def attach_manifest(self, manifest: dict) -> None:
        pass

    def set_voice_profile(self, profile: dict) -> None:
        pass

    @property
    def has_voice_profile(self) -> bool:
        return False

    async def synthesize(self, text: str, emotion: str) -> bytes:
        self.calls.append((text, emotion))
        if text in self.fail_texts:
            raise TTSError(f"fake tts failure for chunk {text!r}")
        return f"audio:{emotion}:{text}".encode("utf-8")

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
