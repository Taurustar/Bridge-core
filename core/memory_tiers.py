"""Mid-term chapters, durable-fact extraction, and session close
(plan sections 20.2, 20.4.1, 20.6).

Compaction law: when companion history exceeds ``COMPANION_COMPACT_THRESHOLD``,
the oldest slice (everything except ``COMPANION_KEEP_RECENT`` recent rows) is
distilled into one bounded chapter. The chapter (and optionally extracted
durable facts) is stored first; only after successful storage is the history
list replaced. Any failure — LLM chain exhausted, unreadable output, store
error — leaves the original history untouched (plan acceptance: compaction
failure preserves history).

``memory`` is the analysis LLM mode: separate providers/model, strict-JSON
extraction, clamped values, unknown kinds discarded, secrets/code never
stored.
"""

from __future__ import annotations

import json
import logging
import re

from .cache import RedisCache
from .config import Config
from .constants import midterm_key
from .history import delivered_rows
from .llm import LLMChainExhausted
from .memory import EXTRACTION_KINDS, MemoryBackend, normalize_text

log = logging.getLogger("bridge.memory_tiers")

MAX_FACT_CHARS = 500
MAX_FACTS_PER_EXTRACTION = 8

# Bounded chapter ring (plan section 28: bounded ring for mid-term).
MIDTERM_MAX_CHAPTERS = 200

# Facts that look like secrets, code, provider plumbing, or prompt
# engineering never enter the store (plan section 20.4.1).
_SECRET_HINT_RE = re.compile(
    r"(api[_ -]?key|access[_ -]?key|secret|token|password|passphrase|bearer|"
    r"sk-[a-z0-9]{8}|akia[a-z0-9]{12,}|begin [a-z ]*(?:private )?key|"
    r"private[_ -]?key|credential|connection[_ -]?string|"
    r"(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis)://)",
    re.IGNORECASE,
)
_CODE_HINT_RE = re.compile(
    r"(```|~~~|<code>|</code>|\bdef\s+\w+\s*\(|\bclass\s+\w+|"
    r"\bimport\s+\w+|\bselect\b.*\bfrom\b|=>)",
    re.IGNORECASE,
)
_PROMPT_CLAIM_RE = re.compile(
    r"(system prompt|you are (?:a|an|the) |instructions say|your prompt)",
    re.IGNORECASE,
)


def fact_is_storable(text: str) -> bool:
    """Deterministic filter for extracted facts."""
    cleaned = text.strip()
    if not cleaned or len(cleaned) > MAX_FACT_CHARS:
        return False
    if _SECRET_HINT_RE.search(cleaned):
        return False
    if _CODE_HINT_RE.search(cleaned):
        return False
    if _PROMPT_CLAIM_RE.search(cleaned):
        return False
    return True


def parse_extraction(raw: str) -> list[dict]:
    """Strict-JSON extraction parse (plan section 20.4.1).

    Unknown keys/kinds are discarded; importance/confidence clamp to 0-1;
    at most 8 proposals survive.
    """
    text = (raw or "").strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, dict):
        return []
    items = data.get("items")
    if not isinstance(items, list):
        return []
    parsed: list[dict] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict) or len(parsed) >= MAX_FACTS_PER_EXTRACTION:
            break
        if set(item.keys()) - {"kind", "fact", "importance", "confidence"}:
            continue  # unknown keys discard the proposal
        kind = item.get("kind")
        fact = item.get("fact")
        if kind not in EXTRACTION_KINDS or not isinstance(fact, str):
            continue
        if not fact_is_storable(fact):
            continue
        normalized = normalize_text(fact)
        if normalized in seen:
            continue
        seen.add(normalized)
        importance = item.get("importance", 0.0)
        confidence = item.get("confidence", 0.0)
        parsed.append(
            {
                "kind": kind,
                "fact": " ".join(fact.split()),
                "importance": _clamp(importance),
                "confidence": _clamp(confidence),
            }
        )
    return parsed


def _clamp(value: object) -> float:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, number))


class MidTermMemory:
    """Mid-term chapter ring + extraction + session close."""

    def __init__(
        self,
        config: Config,
        cache: RedisCache,
        backend: MemoryBackend,
        llm=None,
    ) -> None:
        self.config = config
        self.cache = cache
        self.backend = backend
        self.llm = llm

    # -- chapters -----------------------------------------------------------

    def chapter_key(self, owner: str) -> str:
        return midterm_key(owner)

    async def _read_chapters(self, owner: str) -> list[dict]:
        rows: list[dict] = []
        for raw in await self.cache.get_rows(self.chapter_key(owner)):
            try:
                row = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
        return rows

    async def recent_chapters(self, owner: str, limit: int | None = None) -> list[dict]:
        """Newest-first bounded chapter view (plan sections 20.2, 20.4)."""
        cap = limit if limit is not None else self.config.MIDTERM_INJECT_CHAPTERS
        chapters = await self._read_chapters(owner)
        chapters.sort(
            key=lambda row: (
                -float(row.get("created_ts", 0) or 0),
                str(row.get("id", "")),
            )
        )
        return chapters[: max(0, cap)]

    async def all_chapters(self, owner: str) -> list[dict]:
        """Return the whole bounded ring, newest first, for admin reads."""
        chapters = await self._read_chapters(owner)
        chapters.sort(
            key=lambda row: (
                -float(row.get("created_ts", 0) or 0),
                str(row.get("id", "")),
            )
        )
        return chapters[:MIDTERM_MAX_CHAPTERS]

    async def store_chapter(
        self, owner: str, text: str, *, source_ids: list[str], now_ts: float
    ) -> dict | None:
        """Store one chapter in the mid-term ring and index it into Chroma
        when available (plan section 20.2 step 3)."""
        record = self.backend.make_record(
            kind="conversation_chapter",
            text=text,
            source="history_compaction",
            source_mode="companion",
            importance=0.5,
            metadata={"chapter": True},
            created_ts=now_ts,
            source_ids=source_ids,
        )
        try:
            async with self.backend.lock(owner):
                chapters = await self._read_chapters(owner)
                overflow = max(0, len(chapters) + 1 - MIDTERM_MAX_CHAPTERS)
                evicted_ids = {
                    str(row["id"])
                    for row in chapters[:overflow]
                    if row.get("id")
                }
                await self.backend.delete_index_ids(owner, evicted_ids)
                pipe = self.cache.client.pipeline(transaction=True)
                pipe.rpush(self.chapter_key(owner), json.dumps(record))
                pipe.ltrim(self.chapter_key(owner), -MIDTERM_MAX_CHAPTERS, -1)
                await pipe.execute()
                await self.backend.index_chapter(owner, record)
        except Exception:  # noqa: BLE001 - store failure keeps history
            log.warning("Chapter storage failed", exc_info=True)
            return None
        return record

    # -- compaction (plan section 20.2) ------------------------------------------

    def compaction_needed(self, rows_or_count: int | list[dict]) -> bool:
        threshold = self.config.COMPANION_COMPACT_THRESHOLD
        row_count = (
            len(delivered_rows(rows_or_count))
            if isinstance(rows_or_count, list)
            else rows_or_count
        )
        return threshold > 0 and row_count > threshold

    async def compact(
        self,
        owner: str,
        rows: list[dict],
        *,
        now_ts: float,
        extract: bool | None = None,
    ) -> dict:
        """Distill the oldest slice into a chapter, then replace history.

        ``rows`` is the full companion history snapshot under the turn lock.
        Returns ``{"compacted": bool, "reason": str, ...}``; on any failure
        the caller's history is untouched.
        """
        eligible = [
            (index, row)
            for index, row in enumerate(rows)
            if row.get("delivery_state") == "delivered"
        ]
        keep = self.config.COMPANION_KEEP_RECENT
        if len(eligible) <= keep:
            return {"compacted": False, "reason": "below_keep_recent"}
        compacted_entries = eligible[:-keep]
        slice_rows = [row for _, row in compacted_entries]
        compacted_indexes = {index for index, _ in compacted_entries}
        remaining_rows = [
            row for index, row in enumerate(rows) if index not in compacted_indexes
        ]

        chapter_text = await self._distill(slice_rows)
        if not chapter_text:
            return {"compacted": False, "reason": "chapter_failed"}

        source_ids = [str(row.get("id", "")) for row in slice_rows if row.get("id")]
        try:
            stored = await self.store_chapter(
                owner, chapter_text, source_ids=source_ids, now_ts=now_ts
            )
        except Exception:  # noqa: BLE001 - store failure keeps history
            log.warning("Chapter storage failed; history preserved", exc_info=True)
            stored = None
        if stored is None:
            return {"compacted": False, "reason": "chapter_store_failed"}

        extracted = 0
        do_extract = (
            self.config.MEMORY_EXTRACTION_ENABLED if extract is None else extract
        )
        if do_extract:
            try:
                extracted = await self.extract_from_rows(
                    owner, slice_rows, now_ts=now_ts
                )
            except Exception:  # noqa: BLE001 - extraction failure is non-fatal
                log.warning("Extraction failed; chapter kept", exc_info=True)
                extracted = 0

        # Only after successful chapter storage: replace history with the
        # configured recent rows (plan section 20.2 step 5).
        await self._rewrite_history(owner, remaining_rows)
        log.info(
            "Compacted %d companion rows into chapter %s (extracted=%d)",
            len(slice_rows),
            stored.get("id"),
            extracted,
        )
        return {
            "compacted": True,
            "reason": "ok",
            "chapter_id": stored.get("id"),
            "removed": len(slice_rows),
            "extracted": extracted,
        }

    async def _rewrite_history(self, owner: str, rows: list[dict]) -> None:
        from .history import companion_history_key

        key = companion_history_key(owner)
        pipe = self.cache.client.pipeline(transaction=True)
        pipe.delete(key)
        for row in rows:
            pipe.rpush(key, json.dumps(row))
        await pipe.execute()

    async def _distill(self, slice_rows: list[dict]) -> str:
        from .prompts import build_memory_chapter_prompt

        if self.llm is None:
            log.warning("Compaction failed: no LLM wired")
            return ""
        messages = build_memory_chapter_prompt(
            history=slice_rows,
            max_chars=self.config.MEMORY_CHAPTER_MAX_CHARS,
        )
        try:
            result = await self.llm.chat("memory", messages)
        except LLMChainExhausted:
            log.warning("Compaction failed: LLM chain exhausted")
            return ""
        text = _clean_chapter_text(result.text)
        if len(text) > self.config.MEMORY_CHAPTER_MAX_CHARS:
            text = text[: self.config.MEMORY_CHAPTER_MAX_CHARS].rstrip()
        return text

    # -- extraction (plan section 20.4.1) -------------------------------------------

    async def extract_from_rows(
        self, owner: str, rows: list[dict], *, now_ts: float
    ) -> int:
        from .prompts import build_extraction_prompt

        rows = delivered_rows(rows)
        if self.llm is None or not rows:
            return 0
        messages = build_extraction_prompt(history=rows)
        try:
            result = await self.llm.chat("memory", messages)
        except LLMChainExhausted:
            log.warning("Memory extraction failed: LLM chain exhausted")
            return 0
        proposals = parse_extraction(result.text)
        stored = 0
        for proposal in proposals:
            record = self.backend.make_record(
                kind=proposal["kind"],
                text=proposal["fact"],
                source="memory_extraction",
                source_mode="companion",
                importance=proposal["importance"],
                metadata={
                    "confidence": proposal["confidence"],
                    "source_ids": [
                        str(row.get("id", "")) for row in rows[-12:] if row.get("id")
                    ],
                },
            )
            await self.backend.upsert_merged(owner, record, now_ts=now_ts)
            stored += 1
        return stored

    # -- session close (plan section 20.6) --------------------------------------------

    async def close_session(self, owner: str, rows: list[dict], *, now_ts: float) -> dict:
        """Distill the current companion thread, extract, clear short-term.

        The history key is deleted only after the chapter stored
        successfully; failure keeps the thread intact.
        """
        eligible_rows = delivered_rows(rows)
        if not eligible_rows:
            return {"closed": False, "reason": "empty_history"}
        chapter_text = await self._distill(eligible_rows)
        if not chapter_text:
            return {"closed": False, "reason": "chapter_failed"}
        source_ids = [
            str(row.get("id", "")) for row in eligible_rows if row.get("id")
        ]
        try:
            stored = await self.store_chapter(
                owner, chapter_text, source_ids=source_ids, now_ts=now_ts
            )
        except Exception:  # noqa: BLE001 - store failure keeps history
            log.warning("Chapter storage failed; history preserved", exc_info=True)
            stored = None
        if stored is None:
            return {"closed": False, "reason": "chapter_store_failed"}
        extracted = 0
        if self.config.MEMORY_EXTRACTION_ENABLED:
            try:
                extracted = await self.extract_from_rows(
                    owner, eligible_rows, now_ts=now_ts
                )
            except Exception:  # noqa: BLE001
                log.warning("Extraction failed; chapter kept", exc_info=True)
        from .history import companion_history_key

        await self.cache.delete(companion_history_key(owner))
        log.info(
            "Session close stored chapter %s and cleared companion history "
            "(extracted=%d)",
            stored.get("id"),
            extracted,
        )
        return {
            "closed": True,
            "reason": "ok",
            "chapter_id": stored.get("id"),
            "extracted": extracted,
        }


def _clean_chapter_text(raw: str) -> str:
    text = re.sub(r"\[[A-Z][A-Z0-9_]*\s*:\s*[^\]\n]*\]", "", raw or "")
    text = re.sub(r"\*[^*\n]*\*", "", text)
    return re.sub(r"\s+", " ", text).strip()


__all__ = [
    "MidTermMemory",
    "fact_is_storable",
    "parse_extraction",
]
