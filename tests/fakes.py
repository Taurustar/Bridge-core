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
from zoneinfo import ZoneInfo

from core.cache import RedisCache
from core.config import Config
from core.llm import LLMChainExhausted, LLMResult
from core.speech import TTSError
from redis.exceptions import WatchError


class FakePipeline:
    def __init__(self, client: "FakeRedis") -> None:
        self._client = client
        self._store = client.store
        self._ops: list[tuple] = []
        self._watched: dict[str, int] = {}

    def rpush(self, key: str, value: str) -> "FakePipeline":
        self._ops.append(("rpush", key, value))
        return self

    def ltrim(self, key: str, start: int, end: int) -> "FakePipeline":
        self._ops.append(("ltrim", key, start, end))
        return self

    def delete(self, key: str) -> "FakePipeline":
        self._ops.append(("delete", key))
        return self

    def set(self, key: str, value: str) -> "FakePipeline":
        self._ops.append(("set", key, value))
        return self

    async def watch(self, *keys: str) -> None:
        self._watched = {key: self._client.versions.get(key, 0) for key in keys}

    async def lrange(self, key: str, start: int, end: int) -> list[str]:
        await asyncio.sleep(0)
        return await self._client.lrange(key, start, end)

    async def get(self, key: str) -> str | None:
        await asyncio.sleep(0)
        return await self._client.get(key)

    def multi(self) -> None:
        pass

    async def reset(self) -> None:
        self._ops.clear()
        self._watched.clear()

    async def execute(self) -> list[Any]:
        if any(
            self._client.versions.get(key, 0) != version
            for key, version in self._watched.items()
        ):
            raise WatchError("watched key changed")
        results: list[Any] = []
        for op in self._ops:
            if op[0] == "rpush":
                _, key, value = op
                self._store.setdefault(key, []).append(value)
                self._client.versions[key] = self._client.versions.get(key, 0) + 1
                results.append(len(self._store[key]))
            elif op[0] == "ltrim":
                _, key, start, end = op
                rows = self._store.get(key, [])
                n = len(rows)
                lo = start if start >= 0 else max(n + start, 0)
                hi = end if end >= 0 else n + end
                self._store[key] = rows[lo : hi + 1]
                self._client.versions[key] = self._client.versions.get(key, 0) + 1
                results.append(True)
            elif op[0] == "delete":
                _, key = op
                results.append(1 if self._store.pop(key, None) is not None else 0)
                self._client.versions[key] = self._client.versions.get(key, 0) + 1
            elif op[0] == "set":
                _, key, value = op
                self._client.strings[key] = value
                self._client.versions[key] = self._client.versions.get(key, 0) + 1
                results.append(True)
        self._ops.clear()
        return results


class FakeRedis:
    """In-memory substitute for redis.asyncio.Redis (decode_responses=True)."""

    def __init__(self) -> None:
        self.store: dict[str, list[str]] = {}
        self.strings: dict[str, str] = {}
        self.versions: dict[str, int] = {}
        self.up = True

    async def ping(self) -> bool:
        if not self.up:
            raise ConnectionError("fake redis is down")
        return True

    def pipeline(self, transaction: bool = True) -> FakePipeline:
        return FakePipeline(self)

    async def lrange(self, key: str, start: int, end: int) -> list[str]:
        rows = self.store.get(key, [])
        n = len(rows)
        lo = start if start >= 0 else max(n + start, 0)
        hi = end if end >= 0 else n + end
        return rows[lo : hi + 1]

    async def lset(self, key: str, index: int, value: str) -> bool:
        self.store[key][index] = value
        self.versions[key] = self.versions.get(key, 0) + 1
        return True

    async def llen(self, key: str) -> int:
        return len(self.store.get(key, []))

    async def set(self, key: str, value: str) -> bool:
        self.strings[key] = value
        self.versions[key] = self.versions.get(key, 0) + 1
        return True

    async def get(self, key: str) -> str | None:
        return self.strings.get(key)

    async def delete(self, key: str) -> int:
        removed = 0
        if self.store.pop(key, None) is not None:
            removed += 1
        if self.strings.pop(key, None) is not None:
            removed += 1
        self.versions[key] = self.versions.get(key, 0) + 1
        return removed

    async def keys(self, pattern: str = "*") -> list[str]:
        all_keys = set(self.store) | set(self.strings)
        return [k for k in all_keys if fnmatch.fnmatch(k, pattern)]

    async def aclose(self) -> None:
        pass


class FakeLLM:
    """Scriptable LLM router substitute.

    ``replies`` is a queue; each item is either a raw reply string, an
    Exception instance to raise, a callable awaited with
    ``(mode, messages)`` returning a raw reply string, or a dict shaped
    ``{"text": str, "tool_calls": [{"id", "name", "arguments"}]}`` for
    tool-call scripting.

    ``block_gate`` is an optional ``threading.Event``: while unset, chat()
    polls it asynchronously, simulating a provider call that is still running.
    """

    def __init__(self, replies: list | None = None) -> None:
        self._replies: deque = deque(replies or ["[EMOTION: neutral]\nHello."])
        self.calls: list[tuple[str, list[dict]]] = []
        self.block_gate: Any = None

    async def chat(
        self,
        mode: str,
        messages: list[dict],
        tools: list[dict] | None = None,
        pinned: Any = None,
    ) -> LLMResult:
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
        tool_calls = None
        if isinstance(item, dict):
            tool_calls = item.get("tool_calls")
            item = item.get("text", "")
        return LLMResult(
            text=item,
            provider="fake",
            model="fake-model",
            usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            attempts=1,
            tool_calls=tool_calls,
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


class FakeChroma:
    """Scriptable Chroma substitute for the memory backend tests.

    ``fail=True`` simulates an unavailable/outaged Chroma: start() degrades
    and every later call raises, mirroring the failure-tolerant adapter
    contract. ``indexed`` exposes upserted rows keyed by id for assertions.
    """

    def __init__(self, fail: bool = False, retain_deletes: bool = False) -> None:
        self.fail = fail
        self.retain_deletes = retain_deletes
        self.available = False
        self.indexed: dict[str, dict] = {}
        self.semantic_candidates: list[tuple[str, float]] = []

    def start(self) -> None:
        if self.fail:
            self.available = False
            return
        self.available = True

    def _require(self) -> None:
        if self.fail or not self.available:
            raise RuntimeError("fake chroma is down")

    def upsert(self, owner: str, rows: list[dict]) -> None:
        self._require()
        for row in rows:
            self.indexed[str(row["id"])] = {
                "owner": owner,
                "kind": row.get("kind", ""),
                "text": row.get("text", ""),
            }

    def delete(self, record_ids: list[str]) -> None:
        self._require()
        if self.retain_deletes:
            return
        for record_id in record_ids:
            self.indexed.pop(str(record_id), None)

    def delete_owner(self, owner: str) -> None:
        self._require()
        if self.retain_deletes:
            return
        self.indexed = {
            key: value
            for key, value in self.indexed.items()
            if value.get("owner") != owner
        }

    def query(self, owner: str, text: str, kinds: list[str] | None, limit: int) -> list[str]:
        return [
            record_id
            for record_id, _ in self.query_candidates(owner, text, kinds, limit)
        ]

    def query_candidates(
        self, owner: str, text: str, kinds: list[str] | None, limit: int
    ) -> list[tuple[str, float]]:
        self._require()
        return self.semantic_candidates[:limit]

    def ids(self, owner: str) -> list[str]:
        self._require()
        return [
            record_id
            for record_id, value in self.indexed.items()
            if value.get("owner") == owner
        ]

    def count(self, owner: str) -> int:
        self._require()
        return sum(1 for value in self.indexed.values() if value.get("owner") == owner)


class FakeSchedule:
    """Scriptable schedule substitute for WS integration tests.

    ``availability`` answers ``current_block()['availability']``; the block
    is a synthetic authored block so the life engine treats it as eligible.
    """

    def __init__(self, config: Config, availability: str = "free") -> None:
        self.config = config
        self.availability = availability
        self._loaded = True
        self.tz = ZoneInfo("UTC")

    @property
    def available(self) -> bool:
        return bool(self.config.SCHEDULE_ENABLED and self._loaded)

    def load(self) -> None:
        self._loaded = True

    def maybe_reload(self) -> bool:
        return False

    def reload(self) -> dict:
        return {"reloaded": False, "reason": "fake"}

    def current_block(self, now: Any = None) -> dict:
        import datetime as _dt

        ymd = (_dt.datetime.now(_dt.timezone.utc)).date().isoformat()
        return {
            "block_id": f"{ymd}:0:00:00-24:00",
            "ymd": ymd,
            "index": 0,
            "start": "00:00",
            "end": "24:00",
            "place": "home",
            "activity": "free_time",
            "availability": self.availability,
            "tags": [],
            "source": "authored",
        }

    def remaining_blocks(self, now: Any = None) -> list[dict]:
        return []

    def peek(self, now: Any = None) -> dict:
        import datetime as _dt

        ymd = (_dt.datetime.now(_dt.timezone.utc)).date().isoformat()
        return {
            "timezone": "UTC",
            "date": ymd,
            "local_time": "00:00",
            "now": {
                "block_id": f"{ymd}:0:00:00-24:00",
                "place": "home",
                "activity": "free_time",
                "availability": self.availability,
                "source": "authored",
                "start": "00:00",
                "end": "24:00",
            },
            "blocks": [],
        }


def make_config(**overrides: Any) -> Config:
    """A valid config without touching the real environment."""
    base = {"OWNER_USER_ID": "owner", "TAILSCALE_REQUIRED": True}
    base.update(overrides)
    return Config(**base)


def make_cache(fake: FakeRedis | None = None) -> tuple[RedisCache, FakeRedis]:
    fake = fake or FakeRedis()
    return RedisCache(fake), fake


class FakeNeeds:
    """Scriptable needs-engine substitute for WS integration tests.

    Surfaces only what ``core.bridge`` consumes: availability, soft-block
    status/line, bid satisfaction, turn effects, boundary recording, and the
    per-turn ``evaluate`` used to build the state block.
    """

    def __init__(self, config: Config, cache: RedisCache, available: bool = False) -> None:
        self.config = config
        self.cache = cache
        self.available_flag = available
        self.spec: dict = {}
        self.zones: dict = {}
        self.evaluate_calls = 0

    @property
    def available(self) -> bool:
        return self.available_flag

    def load_spec(self) -> None:
        self.spec = {"version": 1}

    async def evaluate(self, owner: str, activity: str = "default") -> dict:
        self.evaluate_calls += 1
        return {
            "values": {},
            "zones": dict(self.zones),
            "shutdown": False,
            "skipped_gap_count": 0,
            "last_eval_ts": 0.0,
        }

    async def turn_effects(self, owner: str, kind: str) -> None:
        return None


class FakeOwnerProfile:
    """Scriptable owner-profile substitute for WS integration tests."""

    def __init__(self, config: Config, cache: RedisCache, available: bool = False,
                 blocked: bool = False) -> None:
        self.config = config
        self.cache = cache
        self.available_flag = available
        self.blocked = blocked
        self.soft_block_line_text: str | None = None
        self.recorded_boundaries: list[tuple[str, str]] = []
        self.tuning: dict = {}

    @property
    def available(self) -> bool:
        return self.available_flag

    async def soft_block_status(self, owner: str) -> dict:
        return {"blocked": self.blocked}

    def soft_block_line(self, static_lines: dict, language: str) -> str | None:
        return self.soft_block_line_text

    async def record_boundary(self, owner: str, text: str, language: str,
                              activity: str = "companion"):
        self.recorded_boundaries.append((text, language))
        return []

    async def get(self, owner: str):
        return None

    async def mark_soft_block_notice(self, owner: str) -> None:
        return None
