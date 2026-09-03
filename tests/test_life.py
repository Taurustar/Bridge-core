"""Character life engine tests (plan section 17).

Covers template loading/validation/matching, block-entry idempotence,
daily max/cooldown/skip rules, failure retention with admin-force retry,
pending mentions, and durable long-term persistence.
"""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

from core.cache import RedisCache
from core.life import (
    LifeEngine,
    LifeTemplateError,
    load_templates,
    sanitize_event_text,
    template_matches,
)
from core.memory import LongTermMemory
from core.constants import life_last_block_key, life_pending_key, longterm_key

from fakes import FakeLLM, FakeRedis, FakeSchedule, make_config


def template_file(dir_path: Path, name: str, data: dict) -> None:
    (dir_path / name).write_text(json.dumps(data), encoding="utf-8")


def make_template(**overrides) -> dict:
    base = {
        "id": "tmpl_a",
        "enabled": True,
        "description": "Something small happened.",
        "tags": [],
        "activities": [],
        "places": [],
        "schedule_tags": [],
        "time_of_day": [],
        "weight": 1.0,
        "importance": 0.4,
        "examples": ["A quiet example."],
    }
    base.update(overrides)
    return base


def authored_block(block_id: str, *, activity: str = "free_time",
                   availability: str = "free") -> dict:
    return {
        "block_id": block_id,
        "ymd": block_id.split(":")[0],
        "index": 0,
        "start": "09:00",
        "end": "12:00",
        "place": "home",
        "activity": activity,
        "availability": availability,
        "tags": ["home"],
        "source": "authored",
    }


class TemplateLoadingTest(unittest.TestCase):
    def test_disabled_templates_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            template_file(path, "off.json", make_template(enabled=False))
            template_file(path, "on.json", make_template(id="on"))
            templates = load_templates(path)
        self.assertEqual([t["id"] for t in templates], ["on"])

    def test_invalid_template_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            template_file(path, "bad.json", {"id": "x"})
            with self.assertRaises(LifeTemplateError):
                load_templates(path)

    def test_unknown_field_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            template_file(path, "bad.json", make_template(lust_bonus=1))
            with self.assertRaises(LifeTemplateError):
                load_templates(path)

    def test_missing_dir_no_templates(self):
        self.assertEqual(load_templates(None), [])
        self.assertEqual(load_templates(Path("/nonexistent-dir-xyz")), [])


class MatchingTest(unittest.TestCase):
    def test_activity_place_tag_and_time_matching(self):
        block = authored_block("2026-09-02:0:09:00-12:00", activity="gaming")
        block["tags"] = ["evening_play"]
        self.assertTrue(template_matches(make_template(), block, 10))
        self.assertFalse(
            template_matches(make_template(activities=["work"]), block, 10)
        )
        self.assertTrue(template_matches(make_template(activities=["gaming"]), block, 10))
        self.assertFalse(template_matches(make_template(places=["work"]), block, 10))
        self.assertFalse(
            template_matches(make_template(schedule_tags=["work"]), block, 10)
        )
        self.assertTrue(
            template_matches(make_template(schedule_tags=["evening_play"]), block, 10)
        )
        self.assertFalse(template_matches(make_template(time_of_day=["night"]), block, 10))
        self.assertTrue(template_matches(make_template(time_of_day=["morning"]), block, 10))

    def test_sanitize_event_text(self):
        self.assertEqual(sanitize_event_text("[EMOTION: happy]Hi"), "Hi")
        self.assertEqual(sanitize_event_text("*waves* hello"), "hello")
        cleaned = sanitize_event_text("[STATUS: working]   A  thing   happened.")
        self.assertEqual(cleaned, "A thing happened.")
        self.assertEqual(sanitize_event_text("x" * 900), "x" * 500)
        self.assertEqual(sanitize_event_text(""), "")


class GenerationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        template_file(self.dir, "a.json", make_template())
        self.fake_redis = FakeRedis()
        self.cache = RedisCache(self.fake_redis)
        self.longterm = LongTermMemory(
            make_config(MEMORY_MAX_PER_USER=100), self.cache
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _engine(self, replies, **config_overrides) -> LifeEngine:
        config = make_config(
            SCHEDULE_ENABLED=True,
            LIFE_ENABLED=True,
            LIFE_EVENTS_DIR=str(self.dir),
            **config_overrides,
        )
        engine = LifeEngine(
            config,
            self.cache,
            FakeSchedule(config),
            self.longterm,
            FakeLLM(replies=replies),
            now_func=time.time,
        )
        engine.load_templates()
        return engine

    def test_generates_once_per_block(self):
        engine = self._engine(replies=["I finally fixed the balcony lamp."])
        block = authored_block("2026-09-02:0:09:00-12:00")
        result = self._run(engine.generate_for_block("owner", block))
        self.assertTrue(result["generated"])
        records = self._run(self.longterm.records("owner"))
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["kind"], "character_life_event")
        self.assertEqual(records[0]["text"], "I finally fixed the balcony lamp.")
        self.assertEqual(records[0]["metadata"]["block_id"], block["block_id"])
        pending = self._run(engine.pending_ids("owner"))
        self.assertEqual(pending, [records[0]["id"]])
        # Second attempt for the same block is a no-op.
        again = self._run(engine.generate_for_block("owner", block))
        self.assertFalse(again["generated"])
        self.assertEqual(again["reason"], "block_already_claimed")
        self.assertEqual(len(self._run(self.longterm.records("owner"))), 1)

    def test_new_block_generates_again_with_zero_cooldown(self):
        engine = self._engine(
            replies=["One thing.", "Another thing."],
            LIFE_EVENT_COOLDOWN_MINUTES=0,
        )
        first = self._run(
            engine.generate_for_block("owner", authored_block("2026-09-02:0:09:00-12:00"))
        )
        second = self._run(
            engine.generate_for_block("owner", authored_block("2026-09-02:1:13:00-15:00"))
        )
        self.assertTrue(first["generated"])
        self.assertTrue(second["generated"])
        self.assertEqual(len(self._run(self.longterm.records("owner"))), 2)

    def test_cooldown_blocks_then_force_bypasses(self):
        engine = self._engine(replies=["First.", "Second."])
        first = self._run(
            engine.generate_for_block("owner", authored_block("2026-09-02:0:09:00-12:00"))
        )
        second = self._run(
            engine.generate_for_block("owner", authored_block("2026-09-02:1:09:30-12:00"))
        )
        forced = self._run(
            engine.generate_for_block(
                "owner", authored_block("2026-09-02:1:09:30-12:00"), force=True
            )
        )
        self.assertTrue(first["generated"])
        self.assertEqual(second["reason"], "cooldown")
        self.assertTrue(forced["generated"])

    def test_daily_max_enforced_even_for_force(self):
        engine = self._engine(replies=["One."], LIFE_DAILY_MAX=1,
                              LIFE_EVENT_COOLDOWN_MINUTES=0)
        first = self._run(
            engine.generate_for_block("owner", authored_block("2026-09-02:0:09:00-12:00"))
        )
        second = self._run(
            engine.generate_for_block(
                "owner", authored_block("2026-09-02:1:13:00-15:00"), force=True
            )
        )
        self.assertTrue(first["generated"])
        self.assertEqual(second["reason"], "daily_max")

    def test_skip_activities_and_gap_blocks(self):
        engine = self._engine(replies=["Nope."])
        skipped = self._run(
            engine.generate_for_block(
                "owner", authored_block("2026-09-02:0:23:00-24:00", activity="sleep")
            )
        )
        gap = self._run(
            engine.generate_for_block("owner", {
                "block_id": "2026-09-02:gap", "ymd": "2026-09-02",
                "place": "unknown", "activity": "unplanned",
                "availability": "free", "tags": [], "source": "gap",
            })
        )
        self.assertEqual(skipped["reason"], "skipped_activity")
        self.assertEqual(gap["reason"], "gap_block")
        self.assertEqual(self._run(self.longterm.records("owner")), [])

    def test_no_matching_template(self):
        engine = self._engine(replies=["Unused."])
        engine.templates = [make_template(id="a", activities=["surfing"], enabled=True)]
        result = self._run(
            engine.generate_for_block(
                "owner", authored_block("2026-09-02:0:09:00-12:00", activity="gaming")
            )
        )
        self.assertEqual(result["reason"], "no_matching_template")

    def test_failure_retained_then_admin_force_retries(self):
        from core.llm import LLMChainExhausted

        engine = self._engine(replies=[LLMChainExhausted("fake exhaustion")])
        block = authored_block("2026-09-02:0:09:00-12:00")
        failed = self._run(engine.generate_for_block("owner", block))
        self.assertFalse(failed["generated"])
        self.assertEqual(failed["reason"], "generation_failed")
        state = json.loads(self.fake_redis.strings[life_last_block_key("owner")])
        self.assertTrue(state["generation_failed"])
        # Poll never retries a failed block...
        engine.llm = FakeLLM(replies=["Recovered."])
        retried = self._run(engine.generate_for_block("owner", block))
        self.assertEqual(retried["reason"], "block_already_claimed")
        # ...admin force is the explicit retry path.
        forced = self._run(engine.generate_for_block("owner", block, force=True))
        self.assertTrue(forced["generated"])
        self.assertFalse(
            json.loads(self.fake_redis.strings[life_last_block_key("owner")])[
                "generation_failed"
            ]
        )

    def test_pending_clear(self):
        engine = self._engine(replies=["An event."])
        result = self._run(
            engine.generate_for_block("owner", authored_block("2026-09-02:0:09:00-12:00"))
        )
        pending = self._run(engine.pending_ids("owner"))
        self.assertEqual(len(pending), 1)
        cleared = self._run(engine.clear_pending("owner", pending))
        self.assertEqual(cleared, 1)
        self.assertEqual(self._run(engine.pending_ids("owner")), [])

    def test_today_and_recent_views(self):
        engine = self._engine(replies=["An event."], LIFE_EVENT_COOLDOWN_MINUTES=0)
        today = datetime.now(timezone.utc).date().isoformat()
        self._run(
            engine.generate_for_block(
                "owner", authored_block(f"{today}:0:09:00-12:00")
            )
        )
        self.assertEqual(len(self._run(engine.today("owner"))), 1)
        self.assertEqual(len(self._run(engine.recent("owner", limit=5))), 1)

    def _run(self, coro):
        import asyncio

        return asyncio.run(coro)


if __name__ == "__main__":
    unittest.main()
