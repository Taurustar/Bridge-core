"""Real-time character day schedule (plan section 16).

Civil calendar only — there is no loop, accelerated, or simulated time.

- Day resolution: ``mon.json``..``sun.json``, then ``weekday.json``
  (Mon-Fri), then ``weekend.json`` (Sat-Sun). Empty/missing day files use
  one all-day default block and warn; they never invent routines.
- Blocks have inclusive starts and exclusive ends; ``24:00`` is valid only
  as an end value; overnight blocks must be authored as two blocks split at
  midnight; normalized blocks must not overlap. An invalid day is rejected
  at startup/reload and the last valid schedule is kept.
- Gaps resolve to the safe default block (place=unknown,
  activity=unplanned, availability=free). Gap blocks are synthetic: they
  carry no authored block id and never trigger life generation.
- DST semantics (plan 16.2): nonexistent local times advance to the next
  valid instant; repeated local times choose the first occurrence for a
  block start and the second for a block end, so a block is never negative.
- Files hot-reload by mtime; ``reload()`` swaps atomically on full validity.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import Config
from .constants import AVAILABILITIES, GAP_BLOCK

log = logging.getLogger("bridge.schedule")

DAY_NAMES: tuple[str, ...] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

MINUTES_PER_DAY = 24 * 60


class ScheduleError(RuntimeError):
    """Schedule files are missing in a way that cannot default, or invalid."""


def parse_hhmm(value: object, source: str, *, allow_end: bool = False) -> int:
    """Parse ``HH:MM`` into minutes since midnight. ``24:00`` only as end."""
    if not isinstance(value, str):
        raise ScheduleError(f"{source}: block time must be a HH:MM string")
    parts = value.strip().split(":")
    if len(parts) != 2:
        raise ScheduleError(f"{source}: invalid time {value!r} (expected HH:MM)")
    try:
        hour = int(parts[0])
        minute = int(parts[1])
    except ValueError:
        raise ScheduleError(f"{source}: invalid time {value!r} (expected HH:MM)") from None
    if hour == 24 and minute == 0:
        if not allow_end:
            raise ScheduleError(f"{source}: 24:00 is only allowed as a block end")
        return MINUTES_PER_DAY
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ScheduleError(f"{source}: invalid time {value!r}")
    return hour * 60 + minute


def validate_blocks(source: str, blocks: object) -> list[dict]:
    """Validate and normalize one day's blocks; returns a sorted copy."""
    if not isinstance(blocks, list) or not blocks:
        raise ScheduleError(f"{source}: day file must be a non-empty JSON array")
    normalized: list[dict] = []
    for index, raw in enumerate(blocks):
        label = f"{source} block[{index}]"
        if not isinstance(raw, dict):
            raise ScheduleError(f"{label}: must be an object")
        for key in ("start", "end", "place", "activity", "availability"):
            if key not in raw:
                raise ScheduleError(f"{label}: missing required field {key!r}")
        start = parse_hhmm(raw["start"], label)
        end = parse_hhmm(raw["end"], label, allow_end=True)
        if start >= end:
            raise ScheduleError(
                f"{label}: start must be before end; author overnight blocks "
                f"as two blocks split at midnight"
            )
        availability = raw["availability"]
        if availability not in AVAILABILITIES:
            raise ScheduleError(
                f"{label}: availability must be one of {', '.join(AVAILABILITIES)}"
            )
        place = raw["place"]
        activity = raw["activity"]
        if not isinstance(place, str) or not place.strip():
            raise ScheduleError(f"{label}: 'place' must be a non-empty string")
        if not isinstance(activity, str) or not activity.strip():
            raise ScheduleError(f"{label}: 'activity' must be a non-empty string")
        tags = raw.get("tags", [])
        if not isinstance(tags, list) or not all(
            isinstance(tag, str) and tag.strip() for tag in tags
        ):
            raise ScheduleError(f"{label}: 'tags' must be a list of non-empty strings")
        normalized.append(
            {
                "start": raw["start"],
                "end": raw["end"],
                "start_min": start,
                "end_min": end,
                "place": place.strip(),
                "activity": activity.strip(),
                "availability": availability,
                "tags": [tag.strip() for tag in tags],
            }
        )
    normalized.sort(key=lambda block: block["start_min"])
    for previous, current in zip(normalized, normalized[1:]):
        if current["start_min"] < previous["end_min"]:
            raise ScheduleError(
                f"{source}: blocks {previous['start']}-{previous['end']} and "
                f"{current['start']}-{current['end']} overlap"
            )
    return normalized


def load_day_file(path: Path) -> list[dict]:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ScheduleError(f"Schedule file not found: {path}") from None
    except OSError as exc:
        raise ScheduleError(f"Schedule file unreadable: {path}: {exc}") from None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ScheduleError(f"Schedule file is not valid JSON: {path}: {exc}") from None
    return validate_blocks(str(path), data)


def default_all_day_block() -> dict:
    """The all-day fallback for empty/missing day files (plan 16.2)."""
    return {
        "start": "00:00",
        "end": "24:00",
        "start_min": 0,
        "end_min": MINUTES_PER_DAY,
        "place": "unknown",
        "activity": "unplanned",
        "availability": "free",
        "tags": [],
        "default": True,
    }


def local_to_utc(tz: ZoneInfo, naive: datetime, *, is_end: bool) -> datetime:
    """Convert a local wall-clock time to UTC with plan 16.2 DST semantics.

    - Unambiguous times convert directly.
    - Repeated (ambiguous) local times: starts pick the first occurrence,
      ends the second, so a block is never negative.
    - Nonexistent (gap) local times advance to the next valid instant.
    """
    aware0 = naive.replace(tzinfo=tz, fold=0)
    aware1 = naive.replace(tzinfo=tz, fold=1)
    if aware0.utcoffset() == aware1.utcoffset():
        return aware0.astimezone(timezone.utc)
    # Real, repeated local time when the wall clock round-trips; otherwise
    # the time fell in a spring-forward gap.
    roundtrip = aware0.astimezone(timezone.utc).astimezone(tz)
    if roundtrip.replace(tzinfo=None) == naive:
        chosen = aware1 if is_end else aware0
        return chosen.astimezone(timezone.utc)
    return aware0.astimezone(timezone.utc)


def time_of_day_bucket(hour: int) -> str:
    """Coarse civil bucket for life-template matching."""
    if 5 <= hour <= 11:
        return "morning"
    if 12 <= hour <= 16:
        return "afternoon"
    if 17 <= hour <= 20:
        return "evening"
    return "night"


class Schedule:
    """File-backed real-time day schedule (plan section 16)."""

    def __init__(self, config: Config, *, dir_path: str | None = None) -> None:
        self.config = config
        self.dir = Path(dir_path) if dir_path else (
            Path(config.SCHEDULE_DIR) if config.SCHEDULE_DIR.strip() else None
        )
        self.timezone_name = config.CHARACTER_TIMEZONE
        try:
            self.tz = ZoneInfo(self.timezone_name)
        except (ZoneInfoNotFoundError, ValueError):
            raise ScheduleError(
                f"CHARACTER_TIMEZONE is not a valid IANA timezone: "
                f"{self.timezone_name!r}"
            ) from None
        self._days: dict[str, list[dict]] = {}
        self._mtimes: dict[str, float] = {}
        self._loaded = False

    @property
    def available(self) -> bool:
        return bool(self.config.SCHEDULE_ENABLED and self._loaded)

    # -- loading ---------------------------------------------------------------

    def _resolve_day_files(self, weekday_index: int) -> list[Path]:
        day_name = DAY_NAMES[weekday_index]
        candidates = [self.dir / f"{day_name}.json"]
        if weekday_index <= 4:
            candidates.append(self.dir / "weekday.json")
        else:
            candidates.append(self.dir / "weekend.json")
        return [path for path in candidates if path.exists()]

    def _load_day(self, weekday_index: int) -> list[dict]:
        files = self._resolve_day_files(weekday_index)
        if not files:
            return [default_all_day_block()]
        return load_day_file(files[0])

    def load(self) -> None:
        """Initial load. Invalid days fail startup (plan section 16.2)."""
        if self.dir is None:
            log.warning(
                "SCHEDULE_ENABLED with no SCHEDULE_DIR; using all-day default blocks"
            )
            self._days = {name: [default_all_day_block()] for name in DAY_NAMES}
            self._mtimes = {}
            self._loaded = True
            return
        if not self.dir.is_dir():
            log.warning(
                "SCHEDULE_DIR %s does not exist; using all-day default blocks",
                self.dir,
            )
            self._days = {name: [default_all_day_block()] for name in DAY_NAMES}
            self._mtimes = {}
            self._loaded = True
            return
        self._days = {}
        self._mtimes = {}
        for index, name in enumerate(DAY_NAMES):
            self._days[name] = self._load_day(index)
            for path in self._resolve_day_files(index):
                try:
                    self._mtimes[str(path)] = path.stat().st_mtime
                except OSError:
                    pass
        self._loaded = True
        log.info(
            "Schedule loaded from %s (tz=%s)", self.dir, self.timezone_name
        )

    def reload(self) -> dict:
        """Explicit admin reload. On any invalid day the previous schedule
        is kept and the error raised (plan section 16.2)."""
        if self.dir is None:
            return {"reloaded": False, "reason": "no_schedule_dir"}
        candidate_days: dict[str, list[dict]] = {}
        candidate_mtimes: dict[str, float] = {}
        for index, name in enumerate(DAY_NAMES):
            files = self._resolve_day_files(index)
            if files:
                candidate_days[name] = load_day_file(files[0])
                try:
                    candidate_mtimes[str(files[0])] = files[0].stat().st_mtime
                except OSError:
                    pass
            else:
                candidate_days[name] = [default_all_day_block()]
        self._days = candidate_days
        self._mtimes = candidate_mtimes
        self._loaded = True
        log.info("Schedule reloaded from %s", self.dir)
        return {"reloaded": True, "days": sorted(candidate_days)}

    def maybe_reload(self) -> bool:
        """mtime hot-reload; keeps the last valid schedule on failures."""
        if self.dir is None or not self._loaded:
            return False
        try:
            current: dict[str, float] = {}
            for path in sorted(self.dir.glob("*.json")):
                current[str(path)] = path.stat().st_mtime
        except OSError:
            return False
        if current == self._mtimes:
            return False
        try:
            self.reload()
            return True
        except ScheduleError as exc:
            log.warning("Schedule reload rejected, keeping last valid: %s", exc)
            return False

    # -- resolution --------------------------------------------------------------

    def day_blocks(self, local_date: date) -> list[dict]:
        """Copy of the resolved day file for a local civil date."""
        blocks = self._days[DAY_NAMES[local_date.weekday()]]
        return [dict(block) for block in blocks]

    def block_bounds_utc(
        self, ymd: str, block: dict
    ) -> tuple[datetime, datetime]:
        local_date = date.fromisoformat(ymd)
        naive_start = datetime.combine(
            local_date, datetime.min.time()
        ) + timedelta(minutes=int(block["start_min"]))
        naive_end = datetime.combine(
            local_date, datetime.min.time()
        ) + timedelta(minutes=int(block["end_min"]))
        start = local_to_utc(self.tz, naive_start, is_end=False)
        end = local_to_utc(self.tz, naive_end, is_end=True)
        return start, end

    def current_block(self, now: datetime | None = None) -> dict:
        """The block containing ``now`` (UTC), or the synthetic gap block."""
        now = now or datetime.now(timezone.utc)
        local_now = now.astimezone(self.tz)
        ymd = local_now.date().isoformat()
        for index, block in enumerate(self.day_blocks(local_now.date())):
            if block.get("default"):
                continue
            start, end = self.block_bounds_utc(ymd, block)
            if start <= now < end:
                return {
                    "block_id": f"{ymd}:{index}:{block['start']}-{block['end']}",
                    "ymd": ymd,
                    "index": index,
                    "start": block["start"],
                    "end": block["end"],
                    "start_utc": start.isoformat(),
                    "end_utc": end.isoformat(),
                    "place": block["place"],
                    "activity": block["activity"],
                    "availability": block["availability"],
                    "tags": list(block["tags"]),
                    "source": "authored",
                }
        return {
            "block_id": f"{ymd}:gap",
            "ymd": ymd,
            "index": -1,
            "start": None,
            "end": None,
            "place": GAP_BLOCK["place"],
            "activity": GAP_BLOCK["activity"],
            "availability": GAP_BLOCK["availability"],
            "tags": [],
            "source": "gap",
        }

    def remaining_blocks(self, now: datetime | None = None) -> list[dict]:
        """Authored blocks today that end after ``now`` (UTC, DST-safe)."""
        now = now or datetime.now(timezone.utc)
        local_now = now.astimezone(self.tz)
        ymd = local_now.date().isoformat()
        remaining: list[dict] = []
        for index, block in enumerate(self.day_blocks(local_now.date())):
            if block.get("default"):
                continue
            _, end = self.block_bounds_utc(ymd, block)
            if end > now:
                entry = dict(block)
                entry["index"] = index
                remaining.append(entry)
        return remaining

    def peek(self, now: datetime | None = None) -> dict:
        """Read-only view for ``GET /schedule``; never writes (plan 6.4)."""
        now = now or datetime.now(timezone.utc)
        local_now = now.astimezone(self.tz)
        ymd = local_now.date().isoformat()
        blocks = self.day_blocks(local_now.date())
        current = self.current_block(now)
        return {
            "timezone": self.timezone_name,
            "date": ymd,
            "local_time": local_now.strftime("%H:%M"),
            "now": {
                key: current[key]
                for key in (
                    "block_id",
                    "place",
                    "activity",
                    "availability",
                    "source",
                    "start",
                    "end",
                )
            },
            "blocks": [
                {
                    "start": block["start"],
                    "end": block["end"],
                    "place": block["place"],
                    "activity": block["activity"],
                    "availability": block["availability"],
                    "tags": list(block["tags"]),
                    "default": bool(block.get("default")),
                }
                for block in blocks
            ],
        }
