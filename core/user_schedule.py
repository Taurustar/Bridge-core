"""Contextual owner schedule (plan section 22).

Represents the owner's expected civil-day blocks so the character can speak
with time awareness. It is informational only: it never blocks messages,
never books anything, and never creates appointments.

- Storage: baseline weekly schedule at ``core:user_schedule:{owner}`` and
  optional per-date overrides at ``core:user_schedule:day:{owner}:{ymd}``
  (plan section 28). Owner timezone only, held inside the store; timezone
  changes go exclusively through ``PATCH /user-schedule`` with the
  ``UPDATE_USER_SCHEDULE`` mistake guard (plan section 22.4).
- States: ``busy``, ``free``, ``sleep``, ``unknown``. Unknown is not free.
- Missing store: every day resolves ``unknown``. Nothing materializes on
  read (plan section 6.4).
- DST semantics match the character schedule: nonexistent local times
  advance to the next valid instant; repeated times take the first
  occurrence for a start and the second for an end.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .cache import RedisCache
from .config import Config
from .constants import user_schedule_day_key, user_schedule_key
from .schedule import local_to_utc, parse_hhmm

log = logging.getLogger("bridge.user_schedule")

DAY_KEYS: tuple[str, ...] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

STORE_VERSION = 1


class UserScheduleError(ValueError):
    """Invalid owner-schedule patch or store."""


def validate_schedule_timezone(name: str) -> str:
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise UserScheduleError(f"Invalid IANA timezone: {name!r}") from None
    return name


def _validate_blocks(blocks: object) -> list[dict]:
    if not isinstance(blocks, list):
        raise UserScheduleError("'blocks' must be a list")
    normalized: list[dict] = []
    for index, raw in enumerate(blocks):
        label = f"block[{index}]"
        if not isinstance(raw, dict):
            raise UserScheduleError(f"{label}: must be an object")
        for key in ("start", "end", "state"):
            if key not in raw:
                raise UserScheduleError(f"{label}: missing required field {key!r}")
        start = parse_hhmm(raw["start"], label)
        end = parse_hhmm(raw["end"], label, allow_end=True)
        if start >= end:
            raise UserScheduleError(
                f"{label}: start must be before end; author overnight spans "
                f"as two blocks split at midnight"
            )
        state = raw["state"]
        if state not in ("busy", "free", "sleep", "unknown"):
            raise UserScheduleError(
                f"{label}: state must be one of busy, free, sleep, unknown"
            )
        normalized.append(
            {
                "start": raw["start"],
                "end": raw["end"],
                "start_min": start,
                "end_min": end,
                "state": state,
            }
        )
    normalized.sort(key=lambda block: block["start_min"])
    for previous, current in zip(normalized, normalized[1:]):
        if current["start_min"] < previous["end_min"]:
            raise UserScheduleError(
                f"blocks {previous['start']}-{previous['end']} and "
                f"{current['start']}-{current['end']} overlap"
            )
    return normalized


def default_store(timezone_name: str) -> dict:
    return {
        "version": STORE_VERSION,
        "timezone": timezone_name,
        "days": {day: [] for day in DAY_KEYS},
    }


class UserSchedule:
    """Owner civil-day expectation store (plan section 22)."""

    def __init__(self, config: Config, cache: RedisCache) -> None:
        self.config = config
        self.cache = cache

    @property
    def available(self) -> bool:
        return bool(self.config.USER_SCHEDULE_ENABLED)

    # -- store access ------------------------------------------------------------

    async def read_store(self, owner: str) -> dict | None:
        raw = await self.cache.get_value(user_schedule_key(owner))
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("Corrupt owner-schedule store ignored")
            return None
        return data if isinstance(data, dict) else None

    async def write_store(self, owner: str, store: dict) -> None:
        await self.cache.set_value(user_schedule_key(owner), json.dumps(store))

    async def read_override(self, owner: str, ymd: str) -> dict | None:
        raw = await self.cache.get_value(user_schedule_day_key(owner, ymd))
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return data if isinstance(data, dict) else None

    async def write_override(self, owner: str, ymd: str, blocks: list[dict]) -> None:
        await self.cache.set_value(
            user_schedule_day_key(owner, ymd),
            json.dumps({"date": ymd, "blocks": blocks}),
        )

    # -- patching (admin/daily-tool path) -------------------------------------------

    def validate_patch(self, patch: object) -> dict:
        """Validate a PATCH body; returns the normalized updates.

        Accepted fields: ``timezone`` (IANA, this endpoint is the only path
        that changes it), ``days`` (mapping day key -> blocks, replaces the
        given days), ``date`` + ``blocks`` for one per-date override.
        Unknown fields raise. Partial patches are allowed.
        """
        if not isinstance(patch, dict) or not patch:
            raise UserScheduleError("PATCH body must be a non-empty JSON object")
        updates: dict = {}
        for key in patch:
            if key not in ("timezone", "days", "date", "blocks"):
                raise UserScheduleError(f"Unknown field {key!r}")
        if "timezone" in patch:
            name = patch["timezone"]
            if not isinstance(name, str) or not name.strip():
                raise UserScheduleError("'timezone' must be an IANA name")
            updates["timezone"] = validate_schedule_timezone(name.strip())
        if "days" in patch:
            days = patch["days"]
            if not isinstance(days, dict):
                raise UserScheduleError("'days' must be an object")
            normalized_days: dict[str, list[dict]] = {}
            for day, blocks in days.items():
                if day not in DAY_KEYS:
                    raise UserScheduleError(
                        f"Unknown day key {day!r} (allowed: {', '.join(DAY_KEYS)})"
                    )
                normalized_days[day] = _validate_blocks(blocks)
            updates["days"] = normalized_days
        if "date" in patch or "blocks" in patch:
            if "date" not in patch or "blocks" not in patch:
                raise UserScheduleError(
                    "Per-date overrides require both 'date' and 'blocks'"
                )
            ymd = patch["date"]
            if not isinstance(ymd, str):
                raise UserScheduleError("'date' must be an ISO date (YYYY-MM-DD)")
            try:
                date.fromisoformat(ymd)
            except ValueError:
                raise UserScheduleError(
                    "'date' must be an ISO date (YYYY-MM-DD)"
                ) from None
            updates["date"] = ymd
            updates["blocks"] = _validate_blocks(patch["blocks"])
        return updates

    async def apply_patch(self, owner: str, updates: dict) -> dict:
        store = await self.read_store(owner)
        if store is None:
            store = default_store(
                updates.get("timezone", self.config.OWNER_TIMEZONE)
            )
        elif "timezone" in updates:
            store["timezone"] = updates["timezone"]
        if "days" in updates:
            store.setdefault("days", {})
            store["days"].update(updates["days"])
        await self.write_store(owner, store)
        if "date" in updates:
            await self.write_override(owner, updates["date"], updates["blocks"])
        return store

    # -- resolution -------------------------------------------------------------------

    def _tz(self, store: dict | None) -> ZoneInfo:
        name = (store or {}).get("timezone") or self.config.OWNER_TIMEZONE
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            return ZoneInfo("UTC")

    async def current_block(
        self, owner: str, now: datetime | None = None
    ) -> dict | None:
        """Current expected-owner block, or None when disabled/absent.

        Never materializes the store (plan section 6.4).
        """
        if not self.available:
            return None
        store = await self.read_store(owner)
        now = now or datetime.now(timezone.utc)
        tz = self._tz(store)
        local_now = now.astimezone(tz)
        ymd = local_now.date().isoformat()
        override = await self.read_override(owner, ymd)
        if override is not None:
            blocks = override.get("blocks", [])
        elif store is not None:
            blocks = (store.get("days") or {}).get(
                DAY_KEYS[local_now.date().weekday()], []
            )
        else:
            blocks = []
        for block in blocks:
            start_min = block.get("start_min")
            if start_min is None:
                start_min = parse_hhmm(block["start"], "<user schedule>")
            end_min = block.get("end_min")
            if end_min is None:
                end_min = parse_hhmm(block["end"], "<user schedule>", allow_end=True)
            start = local_to_utc(
                tz,
                datetime.combine(local_now.date(), datetime.min.time())
                + timedelta(minutes=int(start_min)),
                is_end=False,
            )
            end = local_to_utc(
                tz,
                datetime.combine(local_now.date(), datetime.min.time())
                + timedelta(minutes=int(end_min)),
                is_end=True,
            )
            if start <= now < end:
                return {
                    "date": ymd,
                    "state": block["state"],
                    "start": block["start"],
                    "end": block["end"],
                }
        return {"date": ymd, "state": "unknown", "start": None, "end": None}

    async def view(self, owner: str) -> dict:
        """Read-only projection for ``GET /user-schedule``; never writes."""
        store = await self.read_store(owner)
        now = datetime.now(timezone.utc)
        tz = self._tz(store)
        ymd = now.astimezone(tz).date().isoformat()
        override = await self.read_override(owner, ymd)
        days = (store or {}).get("days") or {}
        return {
            "timezone": tz.key,
            "materialized": store is not None,
            "days": {
                day: [
                    {"start": b["start"], "end": b["end"], "state": b["state"]}
                    for b in days.get(day, [])
                ]
                for day in DAY_KEYS
            },
            "today": {
                "date": ymd,
                "override": override is not None,
                "blocks": (
                    [
                        {"start": b["start"], "end": b["end"], "state": b["state"]}
                        for b in override.get("blocks", [])
                    ]
                    if override
                    else None
                ),
            },
        }
