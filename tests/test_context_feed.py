"""Awareness block and context feed tests (plan section 21)."""

from __future__ import annotations

import unittest

from core.context_feed import build_awareness_block, build_context_feed, estimate_tokens


def life_record(record_id: str, text: str, day: str = "2026-09-02") -> dict:
    return {
        "id": record_id,
        "kind": "character_life_event",
        "text": text,
        "source": "life_engine",
        "source_mode": "life",
        "importance": 0.4,
        "created_ts": 0.0,
        "updated_ts": 0.0,
        "pinned": False,
        "metadata": {"day": day, "place": "home", "past": True},
    }


class EstimateTokensTest(unittest.TestCase):
    def test_deterministic_estimate(self):
        self.assertEqual(estimate_tokens(""), 0)
        self.assertEqual(estimate_tokens("abcd"), 1)
        self.assertEqual(estimate_tokens("a" * 400), 100)
        self.assertGreater(estimate_tokens("abc"), 0)


class AwarenessTest(unittest.TestCase):
    def test_full_block(self):
        block = build_awareness_block(
            owner_local="Wednesday 14:30 (UTC)",
            character_local="Wednesday 14:30 (UTC)",
            character_schedule_now="work at studio (busy)",
            since_last_conversation="3h 20m",
            owner_schedule_now="busy (09:00-17:00)",
        )
        self.assertTrue(block.startswith("[AWARENESS]"))
        self.assertIn("Owner local time: Wednesday 14:30", block)
        self.assertIn("Your local time:", block)
        self.assertIn("work at studio (busy)", block)
        self.assertIn("3h 20m", block)
        self.assertIn("informational only", block)

    def test_no_content_no_block(self):
        self.assertEqual(build_awareness_block(
            owner_local="", character_local=""), "")

    def test_owner_schedule_flagged_informational(self):
        block = build_awareness_block(
            owner_local="t",
            character_local="t",
            owner_schedule_now="sleep",
        )
        self.assertIn("Owner's expected schedule now: sleep", block)


class ContextFeedTest(unittest.TestCase):
    def test_empty_when_no_events(self):
        self.assertEqual(build_context_feed(), ("", []))

    def test_past_and_pending_markers(self):
        events = [life_record("m1", "Fixed the lamp."), life_record("m2", "Baked bread.")]
        feed, included = build_context_feed(
            life_events=events, pending_ids=["m2"], max_tokens=700
        )
        self.assertIn("[LIFE CONTEXT]", feed)
        self.assertIn("[PAST]", feed)
        self.assertIn("[PENDING]", feed)
        self.assertIn("Fixed the lamp.", feed)
        self.assertIn("Baked bread.", feed)
        self.assertEqual(included, ["m2"])

    def test_same_event_not_injected_twice(self):
        events = [life_record("m1", "One event.")]
        feed, _ = build_context_feed(life_events=events, pending_ids=["m1"])
        self.assertEqual(feed.count("One event."), 1)

    def test_token_budget_enforced(self):
        events = [life_record(f"m{i}", "x" * 400) for i in range(10)]
        feed, _ = build_context_feed(life_events=events, max_tokens=200)
        # Estimate: ~4 chars per token; the rendered block stays well under
        # double the budget characters.
        self.assertLess(len(feed), 200 * 4 * 2)

    def test_pending_wins_slot(self):
        events = [
            life_record("old", "old event"),
            life_record("new", "new event"),
        ]
        feed, included = build_context_feed(
            life_events=events, pending_ids=["new"], max_tokens=40
        )
        self.assertIn("new event", feed)
        self.assertEqual(included, ["new"])


if __name__ == "__main__":
    unittest.main()
