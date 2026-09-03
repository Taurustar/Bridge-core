"""Character life engine (plan section 17).

Life events are past experiences generated at schedule-block entry only —
never on heartbeat count, never for prior or synthetic (gap) blocks.

- Templates live in ``LIFE_EVENTS_DIR`` as JSON; only ``enabled: true``
  templates participate. The bundled ``life_events/schema_example.disabled.json``
  ships inert and establishes no backstory (plan section 17.1).
- Generation: one lightweight ``life``-mode LLM call per event with bounded
  output, skipped activities, one event max per block, daily min/max, and a
  cooldown. ``LIFE_DAILY_MIN`` affects chance selection only: when the
  remaining eligible blocks are no greater than the remaining minimum, the
  next eligible block is forced. It never generates outside block entry.
- The poll loop claims a new block id before generation so overlapping polls
  cannot duplicate an event. On failure the claimed block is retained with
  ``generation_failed=true`` and is not retried by the poll loop; admin
  force generation is the explicit retry path.
- Events persist through the durable Redis long-term fallback
  (``core:longterm:{owner}``); pending mentions (``core:life:pending:{owner}``)
  clear only after a successful companion response that received the context.
- ``LIFE_MISSED_BLOCK_POLICY=current_only`` evaluates only the current block;
  events are never fabricated for blocks missed while offline.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from .cache import RedisCache
from .config import Config
from .constants import life_last_block_key, life_pending_key
from .schedule import Schedule, time_of_day_bucket
from .llm import LLMChainExhausted
from .memory import LongTermMemory
from .schedule import Schedule

log = logging.getLogger("bridge.life")

MAX_EVENT_TEXT_CHARS = 500

TEMPLATE_FIELDS_REQUIRED = ("id", "enabled", "weight", "importance")
TEMPLATE_FIELDS_OPTIONAL = {
    "description",
    "tags",
    "activities",
    "places",
    "schedule_tags",
    "time_of_day",
    "examples",
}

_CONTROL_TAG_RE = re.compile(r"\[[A-Z][A-Z0-9_]*\s*:\s*[^\]\n]*\]")


class LifeTemplateError(RuntimeError):
    """A life-event template file is malformed or invalid."""


def validate_template(data: object, source: str) -> dict:
    if not isinstance(data, dict):
        raise LifeTemplateError(f"{source}: template must be a JSON object")
    for field in TEMPLATE_FIELDS_REQUIRED:
        if field not in data:
            raise LifeTemplateError(f"{source}: missing required field {field!r}")
    if not isinstance(data["id"], str) or not data["id"].strip():
        raise LifeTemplateError(f"{source}: 'id' must be a non-empty string")
    if not isinstance(data["enabled"], bool):
        raise LifeTemplateError(f"{source}: 'enabled' must be a boolean")
    weight = data["weight"]
    importance = data["importance"]
    if not isinstance(weight, (int, float)) or isinstance(weight, bool) or weight <= 0:
        raise LifeTemplateError(f"{source}: 'weight' must be a positive number")
    if (
        not isinstance(importance, (int, float))
        or isinstance(importance, bool)
        or not (0.0 <= importance <= 1.0)
    ):
        raise LifeTemplateError(f"{source}: 'importance' must be between 0 and 1")
    for field in ("tags", "activities", "places", "schedule_tags", "time_of_day", "examples"):
        value = data.get(field, [])
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise LifeTemplateError(f"{source}: {field!r} must be a list of strings")
    for key in data:
        if key not in TEMPLATE_FIELDS_REQUIRED and key not in TEMPLATE_FIELDS_OPTIONAL:
            raise LifeTemplateError(f"{source}: unknown field {key!r}")
    return data


def load_templates(dir_path: Path | None) -> list[dict]:
    """Load enabled templates from a directory; disabled/invalid-disabled
    files are skipped silently only when marked ``enabled: false`` — a
    malformed file fails startup (plan 17.1: templates are inspiration, and
    the shipped example is disabled)."""
    if dir_path is None or not dir_path.is_dir():
        return []
    templates: list[dict] = []
    for path in sorted(dir_path.glob("*.json")):
        try:
            raw = path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            raise LifeTemplateError(f"{path}: unreadable or invalid JSON: {exc}") from None
        template = validate_template(data, str(path))
        if template.get("enabled"):
            templates.append(template)
    return templates


def template_matches(template: dict, block: dict, local_hour: int) -> bool:
    """Deterministic match of a template against the current block."""
    activities = template.get("activities") or []
    if activities and block.get("activity") not in activities:
        return False
    places = template.get("places") or []
    if places and block.get("place") not in places:
        return False
    schedule_tags = template.get("schedule_tags") or []
    if schedule_tags and not set(schedule_tags) & set(block.get("tags") or []):
        return False
    buckets = template.get("time_of_day") or []
    if buckets and time_of_day_bucket(local_hour) not in buckets:
        return False
    return True


def choose_template(
    templates: list[dict], block: dict, local_hour: int, rng: random.Random
) -> dict | None:
    matching = [t for t in templates if template_matches(t, block, local_hour)]
    if not matching:
        return None
    weights = [float(t.get("weight", 1.0)) for t in matching]
    return rng.choices(matching, weights=weights, k=1)[0]


def sanitize_event_text(raw: str) -> str:
    """Bounded, plain-text event body: scrub control tags and asterisk
    actions, collapse whitespace, clamp length."""
    text = _CONTROL_TAG_RE.sub("", raw or "")
    text = re.sub(r"\*[^*\n]*\*", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > MAX_EVENT_TEXT_CHARS:
        text = text[:MAX_EVENT_TEXT_CHARS].rstrip()
    return text


class LifeEngine:
    """Block-entry-driven character life (plan section 17)."""

    def __init__(
        self,
        config: Config,
        cache: RedisCache,
        schedule: Schedule,
        longterm: LongTermMemory,
        llm=None,
        *,
        rng: random.Random | None = None,
        now_func=time.time,
    ) -> None:
        self.config = config
        self.cache = cache
        self.schedule = schedule
        self.longterm = longterm
        self.llm = llm
        self.rng = rng or random.Random()
        self.now = now_func
        self.templates: list[dict] = []
        self.lock = asyncio.Lock()
        self.skip_activities: frozenset[str] = frozenset(
            part.strip().lower()
            for part in config.LIFE_SKIP_ACTIVITIES.split(",")
            if part.strip()
        )

    @property
    def available(self) -> bool:
        return bool(
            self.config.LIFE_ENABLED
            and self.schedule.available
            and self.templates is not None
        )

    def load_templates(self) -> None:
        override = self.config.LIFE_EVENTS_DIR.strip()
        self.templates = load_templates(Path(override) if override else None)
        log.info(
            "Life templates loaded: %d enabled (dir=%s)",
            len(self.templates),
            override or "<bundled default: none>",
        )

    # -- state document ---------------------------------------------------------

    def _key(self, owner: str) -> str:
        return life_last_block_key(owner)

    async def read_state(self, owner: str) -> dict:
        raw = await self.cache.get_value(self._key(owner))
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    async def write_state(self, owner: str, state: dict) -> None:
        await self.cache.set_value(self._key(owner), json.dumps(state))

    # -- eligibility ------------------------------------------------------------

    def _skip_reason(
        self, state: dict, block: dict, now_ts: float, *, force: bool
    ) -> str | None:
        if block.get("source") != "authored":
            return "gap_block"
        activity = str(block.get("activity", "")).lower()
        if activity in self.skip_activities:
            return "skipped_activity"
        day_key = block.get("ymd")
        count_today = int(state.get("count_today", 0) or 0)
        if state.get("day") != day_key:
            count_today = 0
        if count_today >= self.config.LIFE_DAILY_MAX:
            return "daily_max"
        if block.get("block_id") == state.get("block_id"):
            return "block_already_claimed"
        last_ts = float(state.get("last_event_ts", 0) or 0)
        cooldown = self.config.LIFE_EVENT_COOLDOWN_MINUTES * 60
        if not force and last_ts and now_ts - last_ts < cooldown:
            return "cooldown"
        return None

    def _min_force_due(self, state: dict, block: dict, now: datetime) -> bool:
        """Plan 17.2: when the remaining eligible blocks are no greater than
        the remaining minimum, the next eligible block is forced."""
        minimum = self.config.LIFE_DAILY_MIN
        if minimum <= 0:
            return False
        day_key = block.get("ymd")
        count_today = int(state.get("count_today", 0) or 0)
        if state.get("day") != day_key:
            count_today = 0
        remaining_min = minimum - count_today
        if remaining_min <= 0:
            return False
        eligible_remaining = 0
        for entry in self.schedule.remaining_blocks(now):
            if str(entry.get("activity", "")).lower() in self.skip_activities:
                continue
            eligible_remaining += 1
        return eligible_remaining <= remaining_min

    # -- generation ---------------------------------------------------------------

    async def generate_for_block(
        self,
        owner: str,
        block: dict,
        *,
        force: bool = False,
        build_prompt=None,
    ) -> dict:
        """Claim, generate, persist, and mark pending for one block.

        Returns a result dict: ``{"generated": bool, "reason": str, ...}``.
        The caller (poll loop or admin route) supplies the LLM through the
        bridge; ``build_prompt`` is injectable for tests.
        """
        async with self.lock:
            now_ts = self.now()
            now_utc = datetime.fromtimestamp(now_ts, tz=timezone.utc)
            state = await self.read_state(owner)
            reason = self._skip_reason(state, block, now_ts, force=force)
            if reason == "block_already_claimed":
                if state.get("generation_failed") and force:
                    pass  # admin force is the explicit retry path (plan 17.2)
                else:
                    return {"generated": False, "reason": "block_already_claimed"}
            elif reason is not None:
                return {"generated": False, "reason": reason}

            # Claim the block before generation so overlapping polls cannot
            # duplicate it (plan section 17.2).
            day_key = block.get("ymd")
            count_today = int(state.get("count_today", 0) or 0)
            if state.get("day") != day_key:
                count_today = 0
            await self.write_state(
                owner,
                {
                    "block_id": block.get("block_id"),
                    "day": day_key,
                    "count_today": count_today,
                    "last_event_ts": float(state.get("last_event_ts", 0) or 0),
                    "generation_failed": False,
                },
            )

            template = choose_template(
                self.templates,
                block,
                self._local_hour(now_utc),
                self.rng,
            )
            if template is None:
                return {"generated": False, "reason": "no_matching_template"}

            record = await self._generate(owner, block, template, build_prompt)
            if record is None:
                failed = await self.read_state(owner)
                failed["generation_failed"] = True
                await self.write_state(owner, failed)
                return {"generated": False, "reason": "generation_failed"}

            await self.longterm.add(owner, record)
            await self._mark_pending(owner, record["id"])
            latest = await self.read_state(owner)
            latest.update(
                {
                    "count_today": count_today + 1,
                    "last_event_ts": now_ts,
                    "generation_failed": False,
                }
            )
            await self.write_state(owner, latest)
            log.info(
                "Life event generated for block %s (count_today=%d)",
                block.get("block_id"),
                count_today + 1,
            )
            return {"generated": True, "reason": "ok", "record_id": record["id"]}

    def _local_hour(self, now_utc: datetime) -> int:
        return now_utc.astimezone(self.schedule.tz).hour

    async def _generate(self, owner: str, block: dict, template: dict, build_prompt) -> dict | None:
        from .prompts import build_life_prompt

        prompt_builder = build_prompt or build_life_prompt
        messages = prompt_builder(
            template=template,
            block=block,
            language=self.config.DEFAULT_LANGUAGE,
        )
        if self.llm is None:
            log.warning("Life generation failed: no LLM wired")
            return None
        try:
            result = await self.llm.chat("life", messages)
        except LLMChainExhausted:
            log.warning("Life generation failed: LLM chain exhausted")
            return None
        text = sanitize_event_text(result.text)
        if not text:
            log.warning("Life generation failed: empty LLM output")
            return None
        return self.longterm.make_record(
            kind="character_life_event",
            text=text,
            source="life_engine",
            source_mode="life",
            importance=float(template.get("importance", 0.4)),
            metadata={
                "block_id": block.get("block_id"),
                "place": block.get("place"),
                "activity": block.get("activity"),
                "tags": list(block.get("tags") or []),
                "template_id": template.get("id"),
                "day": block.get("ymd"),
                "past": True,
            },
        )

    # -- pending mentions (plan section 17.3) -------------------------------------

    async def _mark_pending(self, owner: str, record_id: str) -> None:
        key = life_pending_key(owner)
        pending = await self.pending_ids(owner)
        if record_id in pending:
            return
        pipe = self.cache.client.pipeline(transaction=True)
        pipe.rpush(key, record_id)
        pipe.ltrim(key, -64, -1)
        await pipe.execute()

    async def pending_ids(self, owner: str) -> list[str]:
        raw_ids = await self.cache.get_rows(life_pending_key(owner))
        return [raw for raw in raw_ids if isinstance(raw, str) and raw]

    async def clear_pending(self, owner: str, record_ids: list[str]) -> int:
        """Clear pending mentions after the response received them."""
        if not record_ids:
            return 0
        drop = set(record_ids)
        remaining = [rid for rid in await self.pending_ids(owner) if rid not in drop]
        await self.cache.delete(life_pending_key(owner))
        pipe = self.cache.client.pipeline(transaction=True)
        for rid in remaining:
            pipe.rpush(life_pending_key(owner), rid)
        await pipe.execute()
        return len(record_ids) - len(remaining)

    # -- read views (plan section 17.4) ---------------------------------------------

    async def today(self, owner: str) -> list[dict]:
        day = self.schedule.current_block().get("ymd")
        rows = await self.longterm.records(owner, kind="character_life_event")
        return [row for row in rows if row.get("metadata", {}).get("day") == day]

    async def recent(self, owner: str, limit: int = 10) -> list[dict]:
        return await self.longterm.records(
            owner, kind="character_life_event", limit=limit
        )

    # -- background poll (plan section 17.2) ------------------------------------------

    async def poll_once(self, owner: str) -> dict:
        """One scheduler tick: current_only policy (plan section 17.2)."""
        self.schedule.maybe_reload()
        block = self.schedule.current_block()
        state = await self.read_state(owner)
        # Plan 17.2: the minimum rule may force the next eligible block.
        now_utc = datetime.fromtimestamp(self.now(), tz=timezone.utc)
        forced = self._min_force_due(state, block, now_utc)
        if forced:
            # A forced block bypasses nothing but chance — generation is
            # deterministic in 0.4.0, so this only affects logging.
            log.debug("Life daily minimum forces the next eligible block")
        return await self.generate_for_block(owner, block)

    async def poll_loop(self, owner: str, stop: asyncio.Event) -> None:
        """Cancel-safe background poll task (plan sections 6.1, 17.2)."""
        while not stop.is_set():
            try:
                await self.poll_once(owner)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - background task owns its errors
                log.warning("Life poll failed", exc_info=True)
            try:
                await asyncio.wait_for(stop.wait(), timeout=max(
                    self.config.LIFE_POLL_INTERVAL_SECONDS, 1
                ))
            except asyncio.TimeoutError:
                continue
