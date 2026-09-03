"""Deferred queue, busy ladder, and catch-up tests (plan section 16.3).

Acceptance coverage (plan section 31, milestone 0.4.0):
- Busy/unavailable paths skip the LLM correctly.
- Catch-up sends once and clears only after success.
- Work/companion deferred hooks remain separated by mode.
- The availability gate uses needs ``peek`` and writes nothing.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path

from core import history as hist
from core.bridge import Bridge
from core.constants import (
    busy_count_key,
    companion_history_key,
    deferred_key,
    needs_key,
)
from core.interaction import DeferredQueue
from core.needs import load_needs

from fakes import FakeLLM, FakeRedis, FakeSchedule, make_cache, make_config

BUSY_LINE = "I'm tied up right now."


def static_lines_file(line: str | None) -> str:
    tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    json.dump(
        {
            "version": 1,
            "en": {
                "busy": line or "",
                "unavailable": "",
                "soft_block": "",
                "stt_empty": "",
            },
            "es": {"busy": "", "unavailable": "", "soft_block": "", "stt_empty": ""},
            "ja": {"busy": "", "unavailable": "", "soft_block": "", "stt_empty": ""},
        },
        tmp,
    )
    tmp.close()
    return tmp.name


class RecordingWS:
    def __init__(self) -> None:
        self.frames: list[dict] = []

    async def send_json(self, frame: dict) -> None:
        self.frames.append(frame)


class DeadWS:
    async def send_json(self, frame: dict) -> None:
        raise RuntimeError("disconnected")


def deferred_done(frames: list[dict]) -> dict:
    dones = [f for f in frames if f.get("type") == "done"]
    return dones[-1] if dones else {}


class DeferredQueueTest(unittest.IsolatedAsyncioTestCase):
    async def test_append_dedupe_and_order(self):
        queue = DeferredQueue(make_config(), make_cache()[0])
        for i in range(3):
            await queue.append(
                "owner", message_id=f"m{i}", mode="companion",
                text=f"msg {i}", source_connection_id="conn_x",
            )
        await queue.append(
            "owner", message_id="m1", mode="companion",
            text="msg 1 again", source_connection_id="conn_y",
        )
        held = await queue.held("owner", "companion")
        self.assertEqual([e["message_id"] for e in held], ["m0", "m1", "m2"])
        self.assertEqual(held[1]["text"], "msg 1 again")
        self.assertTrue(all(e["state"] == "held" for e in held))

    async def test_caps_drop_oldest(self):
        queue = DeferredQueue(make_config(), make_cache()[0])
        for i in range(7):
            await queue.append(
                "owner", message_id=f"m{i}", mode="companion",
                text=f"message {i}", source_connection_id="conn_x",
            )
        held = await queue.held("owner", "companion")
        self.assertEqual(len(held), 5)
        self.assertEqual(held[0]["message_id"], "m2")

    async def test_total_character_cap(self):
        queue = DeferredQueue(make_config(), make_cache()[0])
        big = "x" * 1500
        for i in range(4):
            await queue.append(
                "owner", message_id=f"m{i}", mode="companion",
                text=big, source_connection_id="conn_x",
            )
        held = await queue.held("owner", "companion")
        # 4000-char total cap: 1500*3 = 4500 > 4000 after appending the
        # third, so entries drop until within bounds (2 entries remain).
        self.assertEqual(len(held), 2)
        self.assertEqual(held[0]["message_id"], "m2")

    async def test_expiry_sweep_and_diagnostics(self):
        config = make_config()
        cache, fake = make_cache()
        queue = DeferredQueue(config, cache)
        await queue.append(
            "owner", message_id="m0", mode="companion", text="old",
            source_connection_id="conn_x",
            now_ts=time.time() - 48 * 3600 - 10,
        )
        await queue.append(
            "owner", message_id="m1", mode="companion", text="new",
            source_connection_id="conn_x",
        )
        expired = await queue.sweep_expired("owner")
        self.assertEqual(expired, 1)
        # The fresh entry survives the sweep; only the expired one drops.
        held = await queue.held("owner", "companion")
        self.assertEqual([e["message_id"] for e in held], ["m1"])
        doc = json.loads(fake.strings[deferred_key("owner")])
        self.assertEqual(doc["expired_count"], 1)

    async def test_claim_restore_and_remove(self):
        queue = DeferredQueue(make_config(), make_cache()[0])
        await queue.append(
            "owner", message_id="m0", mode="companion", text="hello",
            source_connection_id="conn_x",
        )
        await queue.append(
            "owner", message_id="w0", mode="work", text="work thing",
            source_connection_id="conn_x",
        )
        claimed = await queue.claim("owner", "companion")
        self.assertEqual(len(claimed), 1)
        self.assertEqual(claimed[0]["mode"], "companion")
        self.assertEqual(claimed[0]["state"], "delivering")
        # Work entries are untouched: modes stay separated.
        self.assertEqual(len(await queue.held("owner", "work")), 1)
        await queue.restore("owner", claimed)
        self.assertEqual(len(await queue.held("owner", "companion")), 1)
        claimed = await queue.claim("owner", "companion")
        await queue.remove("owner", [claimed[0]["id"]])
        self.assertEqual(await queue.held("owner", "companion"), [])

    async def test_busy_counter(self):
        queue = DeferredQueue(make_config(), make_cache()[0])
        self.assertEqual(await queue.busy_count("owner"), 0)
        await queue.increment_busy("owner")
        await queue.increment_busy("owner")
        self.assertEqual(await queue.busy_count("owner"), 2)
        await queue.reset_busy("owner")
        self.assertEqual(await queue.busy_count("owner"), 0)


class BusyLadderTest(unittest.IsolatedAsyncioTestCase):
    def _bridge(self, *, availability: str = "busy", with_line: bool = True):
        config = make_config(
            SCHEDULE_ENABLED=True,
            STATIC_LINES_FILE=static_lines_file(BUSY_LINE if with_line else None),
        )
        cache, fake = make_cache()
        bridge = Bridge(config, cache, llm=FakeLLM())
        # Direct construction skips startup(); load the static lines table
        # the same way startup() would.
        from core.static_lines import load_static_lines

        bridge.static_lines = load_static_lines(config.STATIC_LINES_FILE)
        bridge.schedule = FakeSchedule(config, availability=availability)
        return bridge, fake

    async def test_busy_defers_without_llm(self):
        bridge, fake = self._bridge()
        done = await bridge.run_companion_turn(
            text="are you there?", language="en", source_conn=None
        )
        self.assertEqual(done["type"], "done")
        self.assertTrue(done["ignored"])
        self.assertEqual(done["reason"], "busy")
        self.assertTrue(done["deferred"])
        self.assertEqual(done["text"], BUSY_LINE)
        self.assertEqual(bridge.llm.calls, [])
        self.assertNotIn(companion_history_key("owner"), fake.store)
        held = await bridge.deferred.held("owner", "companion")
        self.assertEqual(len(held), 1)
        self.assertEqual(await bridge.deferred.busy_count("owner"), 1)

    async def test_repeated_messages_protocol_only(self):
        bridge, fake = self._bridge()
        first = await bridge.run_companion_turn(
            text="hello?", language="en", source_conn=None
        )
        second = await bridge.run_companion_turn(
            text="still there?", language="en", source_conn=None
        )
        self.assertEqual(first["text"], BUSY_LINE)
        self.assertNotIn("text", second)
        self.assertEqual(second["reason"], "busy")
        self.assertEqual(await bridge.deferred.busy_count("owner"), 2)

    async def test_unavailable_defers(self):
        bridge, fake = self._bridge(availability="unavailable", with_line=False)
        done = await bridge.run_companion_turn(
            text="hello", language="en", source_conn=None
        )
        self.assertEqual(done["reason"], "unavailable")
        self.assertNotIn("text", done)
        self.assertEqual(bridge.llm.calls, [])

    async def test_shutdown_defers_without_writes(self):
        config = make_config(
            SCHEDULE_ENABLED=True,
            NEEDS_ENABLED=True,
            STATE_EXPRESSION_ENABLED=False,
        )
        cache, fake = make_cache()
        bridge = Bridge(config, cache, llm=FakeLLM())
        bridge.schedule = FakeSchedule(config, availability="free")
        bridge.needs.spec = load_needs(
            content=json.dumps(
                {
                    "version": 1,
                    "stats": {
                        "energy": {"start": 5, "direction": "higher_is_better",
                                   "rate_per_hour": 0},
                        "social_battery": {"start": 5, "direction": "higher_is_better",
                                           "rate_per_hour": 0},
                    },
                    "shutdown": {"enabled": True, "energy_below": 10,
                                 "social_battery_below": 10},
                }
            )
        )
        done = await bridge.run_companion_turn(
            text="hello", language="en", source_conn=None
        )
        self.assertEqual(done["reason"], "unavailable")
        self.assertEqual(bridge.llm.calls, [])
        # The gate used peek: no needs key materialized.
        self.assertNotIn(needs_key("owner"), fake.strings)


class CatchupTest(unittest.IsolatedAsyncioTestCase):
    def _bridge(self, *, availability: str = "busy", replies=None):
        config = make_config(
            SCHEDULE_ENABLED=True,
            STATIC_LINES_FILE=static_lines_file(BUSY_LINE),
        )
        cache, fake = make_cache()
        llm = FakeLLM(replies if replies is not None else [])
        bridge = Bridge(config, cache, llm=llm)
        from core.static_lines import load_static_lines

        bridge.static_lines = load_static_lines(config.STATIC_LINES_FILE)
        bridge.schedule = FakeSchedule(config, availability=availability)
        return bridge, fake, llm

    def _connect(self, bridge) -> object:
        return bridge.connections.connect(RecordingWS(), "owner", client_type="test")

    async def _defer_two(self, bridge):
        await bridge.run_companion_turn(text="first held", language="en",
                                        source_conn=None)
        await bridge.run_companion_turn(text="second held", language="en",
                                        source_conn=None)

    async def test_catchup_answers_once_and_clears(self):
        bridge, fake, llm = self._bridge(
            replies=["[EMOTION: happy]\nI'm back — sorry, I was out!"]
        )
        conn = self._connect(bridge)
        await self._defer_two(bridge)
        bridge.schedule.availability = "free"
        delivered = await bridge.run_catchup("owner", trigger_conn=conn)
        self.assertTrue(delivered)
        # One LLM call, one catch-up answer.
        self.assertEqual(len(llm.calls), 1)
        system_contents = [m["content"] for m in llm.calls[0][1] if m["role"] == "system"]
        self.assertTrue(any("[CATCH-UP]" in c for c in system_contents))
        user_message = llm.calls[0][1][-1]["content"]
        self.assertIn("first held", user_message)
        self.assertIn("second held", user_message)
        # Queue cleared, busy window reset.
        self.assertEqual(await bridge.deferred.held("owner", "companion"), [])
        self.assertEqual(await bridge.deferred.busy_count("owner"), 0)
        # History: two user rows + one delivered assistant row.
        rows = await hist.load_rows(bridge.cache, "owner")
        self.assertEqual([r["role"] for r in rows], ["user", "user", "assistant"])
        self.assertEqual(rows[-1]["delivery_state"], hist.DELIVERED)
        # WS got status(thinking) + catch-up done.
        dones = deferred_done(conn.websocket.frames)
        self.assertTrue(dones.get("catchup"))
        self.assertEqual(dones.get("initiated_by"), "character")

    async def test_catchup_llm_failure_restores_entries(self):
        from core.llm import LLMChainExhausted

        bridge, fake, llm = self._bridge(replies=[LLMChainExhausted("no provider")])
        conn = self._connect(bridge)
        await self._defer_two(bridge)
        bridge.schedule.availability = "free"
        delivered = await bridge.run_catchup("owner", trigger_conn=conn)
        self.assertFalse(delivered)
        held = await bridge.deferred.held("owner", "companion")
        self.assertEqual(len(held), 2)
        # The user rows persisted before the provider call stay delivered...
        rows = await hist.load_rows(bridge.cache, "owner")
        self.assertEqual(len([r for r in rows if r["role"] == "user"]), 2)
        # ...and a retry does not duplicate them.
        bridge.llm = FakeLLM(replies=["[EMOTION: neutral]\nBack now!"])
        bridge.schedule.availability = "free"
        delivered = await bridge.run_catchup("owner", trigger_conn=conn)
        self.assertTrue(delivered)
        rows = await hist.load_rows(bridge.cache, "owner")
        self.assertEqual([r["role"] for r in rows], ["user", "user", "assistant"])

    async def test_failed_delivery_restores_entries(self):
        bridge, fake, llm = self._bridge(
            replies=["[EMOTION: neutral]\nSorry I vanished!"]
        )
        dead = bridge.connections.connect(DeadWS(), "owner", client_type="test")
        await self._defer_two(bridge)
        bridge.schedule.availability = "free"
        delivered = await bridge.run_catchup("owner", trigger_conn=dead)
        self.assertFalse(delivered)
        self.assertEqual(len(await bridge.deferred.held("owner", "companion")), 2)
        rows = await hist.load_rows(bridge.cache, "owner")
        self.assertEqual(rows[-1]["role"], "assistant")
        self.assertEqual(rows[-1]["delivery_state"], hist.UNDELIVERED)

    async def test_catchup_skipped_when_still_busy(self):
        bridge, fake, llm = self._bridge(replies=["[EMOTION: neutral]\nhi"])
        conn = self._connect(bridge)
        await self._defer_two(bridge)
        delivered = await bridge.run_catchup("owner", trigger_conn=conn)
        self.assertFalse(delivered)
        self.assertEqual(len(await bridge.deferred.held("owner", "companion")), 2)
        self.assertEqual(llm.calls, [])

    async def test_catchup_without_connections_restores(self):
        bridge, fake, llm = self._bridge(replies=["[EMOTION: neutral]\nhi"])
        await self._defer_two(bridge)
        bridge.schedule.availability = "free"
        delivered = await bridge.run_catchup("owner", trigger_conn=None)
        self.assertFalse(delivered)
        self.assertEqual(len(await bridge.deferred.held("owner", "companion")), 2)

    async def test_work_entries_not_claimed_by_companion_catchup(self):
        bridge, fake, llm = self._bridge(replies=["[EMOTION: neutral]\nhi"])
        conn = self._connect(bridge)
        await bridge.deferred.append(
            "owner", message_id="w0", mode="work", text="work hook",
            source_connection_id=conn.connection_id,
        )
        bridge.schedule.availability = "free"
        delivered = await bridge.run_catchup("owner", trigger_conn=conn)
        self.assertFalse(delivered)
        self.assertEqual(len(await bridge.deferred.held("owner", "work")), 1)


if __name__ == "__main__":
    unittest.main()
