"""Schedule engine tests (plan section 16).

Covers validation rules, gap defaults, day-file fallbacks, DST semantics,
hot reload, and the last-valid-schedule retention on invalid reloads.
"""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from core.config import ConfigError
from core.schedule import (
    Schedule,
    ScheduleError,
    local_to_utc,
    validate_blocks,
)

from fakes import make_config


def write_day(dir_path: Path, name: str, blocks: list[dict]) -> None:
    (dir_path / f"{name}.json").write_text(json.dumps(blocks), encoding="utf-8")


def block(start: str, end: str, *, availability: str = "free",
          place: str = "home", activity: str = "chilling",
          tags: list[str] | None = None) -> dict:
    return {
        "start": start,
        "end": end,
        "place": place,
        "activity": activity,
        "availability": availability,
        "tags": tags or [],
    }


class ValidationTest(unittest.TestCase):
    def test_valid_blocks_normalize_sorted(self):
        blocks = validate_blocks(
            "<test>",
            [block("10:00", "12:00"), block("08:00", "10:00")],
        )
        self.assertEqual([b["start"] for b in blocks], ["08:00", "10:00"])

    def test_overlap_rejected(self):
        with self.assertRaises(ScheduleError):
            validate_blocks("<test>", [block("08:00", "11:00"), block("10:00", "12:00")])

    def test_overnight_single_block_rejected(self):
        with self.assertRaises(ScheduleError):
            validate_blocks("<test>", [block("22:00", "02:00")])

    def test_2400_only_as_end(self):
        validate_blocks("<test>", [block("00:00", "24:00")])
        with self.assertRaises(ScheduleError):
            validate_blocks("<test>", [block("24:00", "23:00")])
        with self.assertRaises(ScheduleError):
            validate_blocks("<test>", [block("23:00", "24:30")])

    def test_bad_availability_rejected(self):
        with self.assertRaises(ScheduleError):
            validate_blocks("<test>", [block("08:00", "09:00", availability="sorta")])

    def test_missing_fields_rejected(self):
        with self.assertRaises(ScheduleError):
            validate_blocks("<test>", [{"start": "08:00", "end": "09:00"}])

    def test_empty_array_rejected(self):
        with self.assertRaises(ScheduleError):
            validate_blocks("<test>", [])


class ResolutionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _schedule(self, **config_overrides) -> Schedule:
        config = make_config(SCHEDULE_ENABLED=True, **config_overrides)
        schedule = Schedule(config, dir_path=str(self.dir))
        schedule.load()
        return schedule

    def test_missing_files_all_day_default(self):
        schedule = self._schedule()
        now = datetime(2026, 9, 2, 15, 0, tzinfo=timezone.utc)  # a Wednesday
        view = schedule.peek(now)
        self.assertEqual(view["now"]["availability"], "free")
        self.assertEqual(view["now"]["activity"], "unplanned")
        self.assertEqual(view["now"]["source"], "gap")
        self.assertTrue(view["blocks"][0]["default"])

    def test_gap_between_blocks_uses_default(self):
        write_day(self.dir, "wed", [block("09:00", "10:00", availability="busy")])
        schedule = self._schedule()
        now = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)
        current = schedule.current_block(now)
        self.assertEqual(current["source"], "gap")
        self.assertEqual(current["availability"], "free")

    def test_block_containment_inclusive_start_exclusive_end(self):
        write_day(self.dir, "wed", [block("09:00", "10:00", availability="busy")])
        schedule = self._schedule()
        tz = ZoneInfo("UTC")
        at_start = datetime(2026, 9, 2, 9, 0, tzinfo=tz)
        at_end = datetime(2026, 9, 2, 10, 0, tzinfo=tz)
        self.assertEqual(schedule.current_block(at_start)["availability"], "busy")
        self.assertEqual(schedule.current_block(at_end)["source"], "gap")

    def test_day_specific_beats_weekday(self):
        write_day(self.dir, "wed", [block("09:00", "10:00", availability="busy")])
        write_day(self.dir, "weekday", [block("09:00", "10:00", availability="unavailable")])
        schedule = self._schedule()
        now = datetime(2026, 9, 2, 9, 30, tzinfo=timezone.utc)
        self.assertEqual(schedule.current_block(now)["availability"], "busy")
        # Thursday falls back to weekday.json.
        thu = datetime(2026, 9, 3, 9, 30, tzinfo=timezone.utc)
        self.assertEqual(schedule.current_block(thu)["availability"], "unavailable")

    def test_weekend_file_used_for_saturday(self):
        write_day(self.dir, "weekend", [block("11:00", "13:00", availability="soft_busy")])
        schedule = self._schedule()
        sat = datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(schedule.current_block(sat)["availability"], "soft_busy")

    def test_non_utc_character_timezone(self):
        write_day(self.dir, "mon", [block("09:00", "10:00", availability="busy")])
        schedule = self._schedule(CHARACTER_TIMEZONE="America/Santiago")
        # 09:00-10:00 Santiago mid-winter (UTC-4) == 13:00-14:00 UTC.
        inside = datetime(2026, 6, 15, 13, 30, tzinfo=timezone.utc)  # Monday
        self.assertEqual(schedule.current_block(inside)["availability"], "busy")
        outside = datetime(2026, 6, 15, 15, 0, tzinfo=timezone.utc)
        self.assertEqual(schedule.current_block(outside)["source"], "gap")


class DSTTest(unittest.TestCase):
    def _bounds(self, tz_name: str, ymd: str, start: str, end: str) -> tuple:
        config = make_config(SCHEDULE_ENABLED=True, CHARACTER_TIMEZONE=tz_name)
        schedule = Schedule(config, dir_path=None)
        return schedule.block_bounds_utc(ymd, {
            "start": start,
            "end": end,
            "start_min": int(start[:2]) * 60 + int(start[3:]),
            "end_min": int(end[:2]) * 60 + int(end[3:]),
        })

    def test_spring_forward_gap_end_advances(self):
        # US spring forward 2026-03-08: 02:00-02:59 does not exist. A block
        # ending at 02:30 advances to the next valid instant (03:30 EDT).
        start, end = self._bounds("America/New_York", "2026-03-08", "01:00", "02:30")
        self.assertEqual(start, datetime(2026, 3, 8, 6, 0, tzinfo=timezone.utc))
        self.assertEqual(end, datetime(2026, 3, 8, 7, 30, tzinfo=timezone.utc))
        self.assertLess(start, end)

    def test_spring_forward_nonnegative(self):
        # Whole block inside the gap: start 02:10, end 02:20 both advance;
        # start folds forward to 03:10 EDT, end to 03:20 EDT.
        start, end = self._bounds("America/New_York", "2026-03-08", "02:10", "02:20")
        self.assertEqual(start, datetime(2026, 3, 8, 7, 10, tzinfo=timezone.utc))
        self.assertEqual(end, datetime(2026, 3, 8, 7, 20, tzinfo=timezone.utc))

    def test_fall_back_start_first_end_second(self):
        # US fall back 2026-11-01: 01:00-01:59 happens twice. A block
        # 01:00-01:45 takes the first occurrence for the start and the
        # second for the end, staying positive.
        start, end = self._bounds("America/New_York", "2026-11-01", "01:00", "01:45")
        self.assertEqual(start, datetime(2026, 11, 1, 5, 0, tzinfo=timezone.utc))
        self.assertEqual(end, datetime(2026, 11, 1, 6, 45, tzinfo=timezone.utc))

    def test_fall_back_unambiguous_end(self):
        # 01:30-02:30: start is first occurrence (EDT), end is unambiguous.
        start, end = self._bounds("America/New_York", "2026-11-01", "01:30", "02:30")
        self.assertEqual(start, datetime(2026, 11, 1, 5, 30, tzinfo=timezone.utc))
        self.assertEqual(end, datetime(2026, 11, 1, 7, 30, tzinfo=timezone.utc))


class ReloadTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_invalid_reload_keeps_last_valid(self):
        write_day(self.dir, "mon", [block("09:00", "10:00", availability="busy")])
        config = make_config(SCHEDULE_ENABLED=True)
        schedule = Schedule(config, dir_path=str(self.dir))
        schedule.load()
        now = datetime(2026, 9, 7, 9, 30, tzinfo=timezone.utc)
        self.assertEqual(schedule.current_block(now)["availability"], "busy")
        # Author an invalid Monday and force a reload.
        write_day(self.dir, "mon", [block("09:00", "12:00"), block("10:00", "13:00")])
        with self.assertRaises(ScheduleError):
            schedule.reload()
        self.assertEqual(schedule.current_block(now)["availability"], "busy")

    def test_mtime_hot_reload(self):
        write_day(self.dir, "tue", [block("09:00", "10:00", availability="busy")])
        config = make_config(SCHEDULE_ENABLED=True)
        schedule = Schedule(config, dir_path=str(self.dir))
        schedule.load()
        now = datetime(2026, 9, 8, 9, 30, tzinfo=timezone.utc)
        self.assertEqual(schedule.current_block(now)["availability"], "busy")
        path = self.dir / "tue.json"
        future = time.time() + 5
        os_utime = future
        import os

        os.utime(path, (os_utime, os_utime))
        write_day(self.dir, "tue", [block("09:00", "10:00", availability="free")])
        os.utime(path, (os_utime + 2, os_utime + 2))
        self.assertTrue(schedule.maybe_reload())
        self.assertEqual(schedule.current_block(now)["availability"], "free")


class LocalToUtcTest(unittest.TestCase):
    def test_unambiguous_passthrough(self):
        tz = ZoneInfo("UTC")
        naive = datetime(2026, 9, 2, 12, 0)
        self.assertEqual(
            local_to_utc(tz, naive, is_end=False),
            datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc),
        )


class ConfigValidationTest(unittest.TestCase):
    def _from_env(self, **overrides) -> None:
        from core.config import Config

        environ = {"OWNER_USER_ID": "owner", **overrides}
        Config.from_env(env_file=None, environ=environ)

    def test_invalid_character_timezone_fails(self):
        with self.assertRaises(ConfigError):
            self._from_env(CHARACTER_TIMEZONE="Not/AZone")

    def test_invalid_owner_timezone_fails(self):
        with self.assertRaises(ConfigError):
            self._from_env(OWNER_TIMEZONE="Mars/Olympus")

    def test_invalid_life_policy_fails(self):
        with self.assertRaises(ConfigError):
            self._from_env(LIFE_MISSED_BLOCK_POLICY="replay_all")

    def test_invalid_soft_busy_policy_fails(self):
        with self.assertRaises(ConfigError):
            self._from_env(SCHEDULE_SOFT_BUSY_POLICY="silent")

    def test_daily_min_above_max_fails(self):
        with self.assertRaises(ConfigError):
            self._from_env(LIFE_DAILY_MIN="5", LIFE_DAILY_MAX="2")


if __name__ == "__main__":
    unittest.main()
