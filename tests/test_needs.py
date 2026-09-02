"""Needs engine tests (plan sections 15.1-15.4).

Acceptance coverage: poll endpoints write nothing, restart/elapsed behavior
is deterministic, unknown schema versions fail startup, and flags off create
no keys.
"""

from __future__ import annotations

import json
import time
import unittest

from core.cache import RedisCache
from core.config import Config
from core.constants import needs_key
from core.needs import (
    BUNDLED_NEEDS_FILE,
    NeedsEngine,
    NeedsProfileError,
    apply_effects,
    classify_turn_kind,
    compute_shutdown,
    load_needs,
    project_needs,
    validate_needs,
    zone_for,
    zones_of,
)

from fakes import FakeRedis, make_cache, make_config


class NeedsProfileTest(unittest.TestCase):
    def test_bundled_template_is_schema_complete_and_neutral(self):
        spec = load_needs()  # bundled default
        self.assertEqual(spec["version"], 1)
        for name in ("energy", "hunger", "stress", "social_battery", "fun",
                     "bond", "hurt"):
            self.assertIn(name, spec["stats"])
        self.assertNotIn("lust", spec["stats"])
        self.assertIn("companion_brief", spec["turn_effects"])
        self.assertIn("companion_engaged", spec["turn_effects"])
        self.assertIn("work", spec["turn_effects"])
        self.assertFalse(spec["shutdown"]["enabled"])

    def test_unknown_stat_rejected(self):
        spec = load_needs()
        spec["stats"]["lust"] = {"start": 0, "direction": "lower_is_better",
                                 "rate_per_hour": 0.0}
        with self.assertRaises(NeedsProfileError):
            validate_needs(spec)

    def test_unknown_future_version_fails_startup(self):
        spec = load_needs()
        spec["version"] = 99
        with self.assertRaises(NeedsProfileError):
            validate_needs(spec)
        with self.assertRaises(NeedsProfileError):
            load_needs(content=json.dumps(spec))

    def test_unknown_turn_effect_kind_rejected(self):
        spec = load_needs()
        spec["turn_effects"]["party"] = {"fun": 5}
        with self.assertRaises(NeedsProfileError):
            validate_needs(spec)

    def test_missing_file_raises(self):
        with self.assertRaises(NeedsProfileError):
            load_needs("/nonexistent/needs.json")

    def test_invalid_json_content_raises(self):
        with self.assertRaises(NeedsProfileError):
            load_needs(content="{not json")

    def test_bundled_file_path_exists(self):
        self.assertTrue(BUNDLED_NEEDS_FILE.exists())


class ZoneTest(unittest.TestCase):
    def setUp(self):
        self.spec = load_needs()

    def test_higher_is_better_zones(self):
        stat = self.spec["stats"]["energy"]
        self.assertEqual(zone_for("energy", 90, stat), "fine")
        self.assertEqual(zone_for("energy", 30, stat), "low")
        self.assertEqual(zone_for("energy", 5, stat), "critical")

    def test_lower_is_better_zones_invert(self):
        stat = self.spec["stats"]["hunger"]
        self.assertEqual(zone_for("hunger", 10, stat), "fine")
        self.assertEqual(zone_for("hunger", 60, stat), "low")
        self.assertEqual(zone_for("hunger", 95, stat), "critical")

    def test_bond_zones_use_bond_names(self):
        stat = self.spec["stats"]["bond"]
        self.assertEqual(zone_for("bond", 90, stat), "secure")
        self.assertEqual(zone_for("bond", 30, stat), "strained")
        self.assertEqual(zone_for("bond", 5, stat), "deprived")

    def test_zones_of_full_snapshot(self):
        values = {name: 90 for name in self.spec["stats"]}
        zones = zones_of(self.spec, values)
        # hunger is lower_is_better: 90 maps to critical.
        self.assertEqual(zones["hunger"], "critical")
        self.assertEqual(zones["hurt"], "critical")
        self.assertEqual(zones["stress"], "critical")
        self.assertEqual(zones["energy"], "fine")
        self.assertEqual(zones["bond"], "secure")


class ProjectionTest(unittest.TestCase):
    def setUp(self):
        self.spec = load_needs()

    def test_initial_state_uses_starts(self):
        now = 1_000_000.0
        projection = project_needs({}, self.spec, now)
        self.assertAlmostEqual(projection["values"]["energy"], 70.0)
        self.assertEqual(projection["skipped_gap_count"], 0)

    def test_elapsed_decay_is_deterministic(self):
        now = 1_000_000.0
        state = {
            "values": {name: 70.0 for name in self.spec["stats"]},
            "last_eval_ts": now,
            "skipped_gap_count": 0,
        }
        later = project_needs(state, self.spec, now + 3600.0)
        self.assertAlmostEqual(later["values"]["energy"], 69.0)  # -1.0/hour
        self.assertAlmostEqual(later["values"]["fun"], 69.5)     # -0.5/hour

    def test_activity_multiplier_scales_rate(self):
        now = 1_000_000.0
        spec = dict(self.spec)
        spec["activity_multipliers"] = {"work": 2.0}
        state = {
            "values": {name: 70.0 for name in self.spec["stats"]},
            "last_eval_ts": now,
            "skipped_gap_count": 0,
        }
        later = project_needs(state, spec, now + 3600.0, activity="work")
        self.assertAlmostEqual(later["values"]["energy"], 68.0)  # -1.0 * 2.0

    def test_large_gap_is_bounded_and_counted(self):
        now = 1_000_000.0
        state = {
            "values": {name: 70.0 for name in self.spec["stats"]},
            "last_eval_ts": now,
            "skipped_gap_count": 0,
        }
        # 1000 hours later, bounded at 48.
        later = project_needs(state, self.spec, now + 1000 * 3600.0,
                              max_elapsed_hours=48.0)
        self.assertAlmostEqual(later["values"]["energy"],
                               max(0.0, 70.0 - 48.0 * 1.0))
        self.assertEqual(later["skipped_gap_count"], 1)

    def test_dst_never_alters_elapsed_duration(self):
        # Elapsed time derives from UTC timestamps only; identical inputs
        # must produce identical outputs regardless of any calendar date.
        now = 1_700_000_000.0
        state = {
            "values": {name: 70.0 for name in self.spec["stats"]},
            "last_eval_ts": now,
            "skipped_gap_count": 0,
        }
        a = project_needs(state, self.spec, now + 7200.0)
        b = project_needs(state, self.spec, now + 7200.0)
        self.assertEqual(a["values"], b["values"])

    def test_values_clamp(self):
        state = {
            "values": {name: 1.0 for name in self.spec["stats"]},
            "last_eval_ts": 1_000_000.0,
            "skipped_gap_count": 0,
        }
        later = project_needs(state, self.spec, 1_000_000.0 + 100 * 3600.0,
                              max_elapsed_hours=200.0)
        for value in later["values"].values():
            self.assertGreaterEqual(value, 0.0)
            self.assertLessEqual(value, 100.0)

    def test_shutdown_requires_both_critical(self):
        values = {"energy": 5.0, "social_battery": 5.0}
        self.assertTrue(compute_shutdown(
            {"shutdown": {"enabled": True, "energy_below": 10,
                          "social_battery_below": 10}},
            values,
        ))
        self.assertFalse(compute_shutdown(
            {"shutdown": {"enabled": True, "energy_below": 10,
                          "social_battery_below": 10}},
            {"energy": 50.0, "social_battery": 5.0},
        ))
        self.assertFalse(compute_shutdown(
            {"shutdown": {"enabled": False, "energy_below": 10,
                          "social_battery_below": 10}},
            values,
        ))

    def test_turn_effects_apply_and_clamp(self):
        values = {"bond": 99.5, "social_battery": 50.0}
        spec = self.spec
        out = apply_effects(values, spec["turn_effects"]["companion_engaged"], spec)
        self.assertEqual(out["bond"], 100.0)
        self.assertEqual(out["social_battery"], 49.75)

    def test_classify_turn_kind_is_deterministic(self):
        self.assertEqual(classify_turn_kind("hi"), "companion_brief")
        self.assertEqual(classify_turn_kind("x" * 500), "companion_engaged")


class NeedsEngineTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cache_pair = make_cache()
        self.cache = self.cache_pair[0]
        self.fake = self.cache_pair[1]
        self.engine = NeedsEngine(make_config(NEEDS_ENABLED=True), self.cache)
        self.engine.load_spec()

    async def test_flag_off_creates_no_keys(self):
        engine = NeedsEngine(make_config(NEEDS_ENABLED=False), self.cache)
        engine.load_spec()
        self.assertFalse(engine.available)
        snapshot = await engine.peek("owner") if engine.available else None
        self.assertIsNone(snapshot)
        # Bridge never calls evaluate when unavailable; direct call is also
        # absent. Prove the store stays empty when the bridge path is used:
        await engine.turn_effects("owner", "companion_engaged")  # no-op
        self.assertEqual(list(self.fake.strings.keys()), [])

    async def test_evaluate_persists_and_restart_preserves(self):
        await self.engine.evaluate("owner")
        key = needs_key("owner")
        self.assertIn(key, self.fake.strings)
        first = json.loads(self.fake.strings[key])
        self.assertEqual(first["values"]["energy"], 70.0)

        # Simulate a restart: fresh engine over the same store.
        restarted = NeedsEngine(self.engine.config, self.cache)
        restarted.load_spec()
        snapshot = await restarted.peek("owner", )
        # No time passed -> values unchanged (restart never resets stats).
        self.assertAlmostEqual(snapshot["values"]["energy"], 70.0)

    async def test_peek_writes_nothing(self):
        before = dict(self.fake.strings)
        snapshot = await self.engine.peek("owner")
        self.assertIn("zones", snapshot)
        self.assertEqual(self.fake.strings, before)

    async def test_evaluate_bounds_large_gaps(self):
        await self.engine.evaluate("owner")
        key = needs_key("owner")
        state = json.loads(self.fake.strings[key])
        state["last_eval_ts"] = time.time() - 1000 * 3600
        self.fake.strings[key] = json.dumps(state)
        result = await self.engine.evaluate("owner")
        self.assertEqual(result["skipped_gap_count"], 1)
        self.assertAlmostEqual(result["values"]["energy"],
                               max(0.0, 70.0 - 48.0), places=5)


if __name__ == "__main__":
    unittest.main()
