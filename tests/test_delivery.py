"""Turn delivery-state tests driven directly against the Bridge (no WS needed)."""

from __future__ import annotations

import unittest

from core import history as hist
from core.bridge import Bridge
from core.constants import companion_history_key

from fakes import FakeLLM, FakeRedis, make_cache, make_config


class DeadConnection:
    """Source connection whose sends always fail (post-disconnect delivery)."""

    connection_id = "conn_dead"

    async def send_json(self, frame: dict) -> bool:
        return False


class DeliveryStateTest(unittest.IsolatedAsyncioTestCase):
    async def test_failed_source_delivery_marks_undelivered_no_fanout(self):
        cache, fake = make_cache()
        bridge = Bridge(make_config(), cache, llm=FakeLLM())
        done = await bridge.run_companion_turn(
            text="hi", language="en", source_conn=DeadConnection()
        )
        self.assertEqual(done["type"], "done")
        rows = await hist.load_rows(cache, "owner")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]["delivery_state"], hist.UNDELIVERED)
        # undelivered rows never re-enter prompt history
        prompt_rows = await hist.load_prompt_history(cache, "owner", 40)
        self.assertEqual([r["role"] for r in prompt_rows], ["user"])

    async def test_http_turn_marks_delivered(self):
        cache, _ = make_cache()
        bridge = Bridge(make_config(), cache, llm=FakeLLM())
        done = await bridge.run_companion_turn(
            text="hi", language="en", source_conn=None
        )
        self.assertEqual(done["type"], "done")
        rows = await hist.load_rows(cache, "owner")
        self.assertEqual(rows[1]["delivery_state"], hist.DELIVERED)

    async def test_history_cap_enforced_through_turns(self):
        config = make_config(MAX_HISTORY_TURNS=4)
        cache, fake = make_cache()
        bridge = Bridge(config, cache, llm=FakeLLM(["[EMOTION: neutral]\nok."] * 10))
        for i in range(5):
            await bridge.run_companion_turn(
                text=f"m{i}", language="en", source_conn=None
            )
        self.assertEqual(
            await cache.row_count(companion_history_key("owner")), 4
        )


if __name__ == "__main__":
    unittest.main()
