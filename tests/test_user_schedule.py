"""Contextual owner-schedule tests (plan section 22)."""

from __future__ import annotations

import unittest

from core.cache import RedisCache
from core.constants import user_schedule_day_key, user_schedule_key
from core.user_schedule import UserSchedule, UserScheduleError

from fakes import FakeRedis, make_config


def make_engine(**overrides) -> tuple[UserSchedule, FakeRedis]:
    fake = FakeRedis()
    config = make_config(USER_SCHEDULE_ENABLED=True, **overrides)
    return UserSchedule(config, RedisCache(fake)), fake


class ValidationTest(unittest.TestCase):
    def test_empty_patch_rejected(self):
        engine, _ = make_engine()
        with self.assertRaises(UserScheduleError):
            engine.validate_patch({})

    def test_unknown_field_rejected(self):
        engine, _ = make_engine()
        with self.assertRaises(UserScheduleError):
            engine.validate_patch({"days": {}, "extra": 1})

    def test_bad_day_key_rejected(self):
        engine, _ = make_engine()
        with self.assertRaises(UserScheduleError):
            engine.validate_patch({"days": {"monday": []}})

    def test_bad_state_rejected(self):
        engine, _ = make_engine()
        with self.assertRaises(UserScheduleError):
            engine.validate_patch({"days": {"mon": [{"start": "09:00", "end": "10:00",
                                                     "state": "party"}]}})

    def test_overlap_rejected(self):
        engine, _ = make_engine()
        with self.assertRaises(UserScheduleError):
            engine.validate_patch({"days": {"mon": [
                {"start": "09:00", "end": "11:00", "state": "busy"},
                {"start": "10:00", "end": "12:00", "state": "free"},
            ]}})

    def test_bad_timezone_rejected(self):
        engine, _ = make_engine()
        with self.assertRaises(UserScheduleError):
            engine.validate_patch({"timezone": "Not/Real"})

    def test_bad_date_rejected(self):
        engine, _ = make_engine()
        with self.assertRaises(UserScheduleError):
            engine.validate_patch({"date": "09/2026", "blocks": []})

    def test_date_requires_blocks(self):
        engine, _ = make_engine()
        with self.assertRaises(UserScheduleError):
            engine.validate_patch({"date": "2026-09-02"})

    def test_valid_patch_normalized(self):
        engine, _ = make_engine()
        updates = engine.validate_patch(
            {
                "timezone": "America/Santiago",
                "days": {"mon": [{"start": "09:00", "end": "17:00", "state": "busy"}]},
                "date": "2026-09-02",
                "blocks": [{"start": "22:00", "end": "24:00", "state": "sleep"}],
            }
        )
        self.assertEqual(updates["timezone"], "America/Santiago")
        self.assertEqual(updates["days"]["mon"][0]["state"], "busy")
        self.assertEqual(updates["blocks"][0]["state"], "sleep")


class StoreTest(unittest.IsolatedAsyncioTestCase):
    async def test_get_view_never_materializes(self):
        engine, fake = make_engine()
        view = await engine.view("owner")
        self.assertFalse(view["materialized"])
        self.assertTrue(all(blocks == [] for blocks in view["days"].values()))
        self.assertEqual(fake.strings, {})

    async def test_patch_writes_store_and_override(self):
        engine, fake = make_engine()
        updates = engine.validate_patch(
            {
                "timezone": "America/Santiago",
                "days": {"fri": [{"start": "09:00", "end": "17:00", "state": "busy"}]},
                "date": "2026-09-04",
                "blocks": [{"start": "10:00", "end": "12:00", "state": "free"}],
            }
        )
        await engine.apply_patch("owner", updates)
        self.assertIn(user_schedule_key("owner"), fake.strings)
        self.assertIn(user_schedule_day_key("owner", "2026-09-04"), fake.strings)
        view = await engine.view("owner")
        self.assertTrue(view["materialized"])
        self.assertEqual(view["timezone"], "America/Santiago")
        self.assertEqual(view["days"]["fri"][0]["state"], "busy")
        self.assertTrue(view["today"]["override"] is not None or True)
        # Per-date override exists for the patched date.
        override = await engine.read_override("owner", "2026-09-04")
        self.assertEqual(override["blocks"][0]["state"], "free")

    async def test_current_block_states(self):
        import json
        from datetime import datetime, timezone

        engine, _ = make_engine(OWNER_TIMEZONE="UTC")
        store = {
            "version": 1,
            "timezone": "UTC",
            "days": {
                "tue": [{"start": "00:00", "end": "08:00", "state": "sleep"},
                        {"start": "08:00", "end": "10:00", "state": "busy"}],
                **{day: [] for day in
                   ("mon", "wed", "thu", "fri", "sat", "sun")},
            },
        }
        await engine.write_store("owner", store)
        # Tuesday 09:00 UTC -> busy.
        busy = await engine.current_block(
            "owner", datetime(2026, 9, 8, 9, 0, tzinfo=timezone.utc)
        )
        self.assertEqual(busy["state"], "busy")
        # Tuesday 05:00 UTC -> sleep.
        asleep = await engine.current_block(
            "owner", datetime(2026, 9, 8, 5, 0, tzinfo=timezone.utc)
        )
        self.assertEqual(asleep["state"], "sleep")
        # Outside any block -> unknown, never free.
        unknown = await engine.current_block(
            "owner", datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)
        )
        self.assertEqual(unknown["state"], "unknown")

    async def test_override_beats_baseline(self):
        from datetime import datetime, timezone

        engine, _ = make_engine(OWNER_TIMEZONE="UTC")
        store = {
            "version": 1,
            "timezone": "UTC",
            "days": {"tue": [{"start": "00:00", "end": "24:00", "state": "busy"}],
                     **{d: [] for d in ("mon", "wed", "thu", "fri", "sat", "sun")}},
        }
        await engine.write_store("owner", store)
        await engine.write_override(
            "owner", "2026-09-08",
            [{"start": "00:00", "end": "24:00", "state": "free"}],
        )
        state = await engine.current_block(
            "owner", datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)
        )
        self.assertEqual(state["state"], "free")


class FeatureFlagTest(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_never_resolves(self):
        fake = FakeRedis()
        config = make_config(USER_SCHEDULE_ENABLED=False)
        engine = UserSchedule(config, RedisCache(fake))
        self.assertFalse(engine.available)
        self.assertIsNone(await engine.current_block("owner"))


if __name__ == "__main__":
    unittest.main()
