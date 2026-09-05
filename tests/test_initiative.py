"""Heartbeat initiative tests (plan sections 23, 12; milestone 0.7.0).

Acceptance coverage (plan section 31, milestone 0.7.0):
- Counters advance only on delivery.
- Daily max/min gap are hard-enforced before any generation.
- Soft block/schedule/active turn suppress initiative.
- Owner-global buckets count once no matter how many devices send.
- The cadence roll is deterministic and never mechanically every Nth beat.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

from core import history as hist
from core.bridge import Bridge
from core.connections import ConnectionManager
from core.constants import bids_key, owner_profile_key
from core.initiative import (
    InitiativeEngine,
    SeedUnavailable,
    cadence_roll,
    fresh_state,
    owner_day_key,
)
from core.owner_profile import default_profile
from core.user_schedule import DAY_KEYS

from fakes import FakeLLM, FakeRedis, FakeSchedule, make_cache, make_config


class RecordingWS:
    def __init__(self) -> None:
        self.frames: list[dict] = []

    async def send_json(self, frame: dict) -> None:
        self.frames.append(frame)


class DeadWS:
    async def send_json(self, frame: dict) -> None:
        raise RuntimeError("disconnected")


def engine_config(**overrides):
    base = {
        "INITIATIVE_ENABLED": True,
        "INITIATIVE_MIN_HEARTBEATS": 2,
        "INITIATIVE_HEARTBEAT_WINDOW_SECONDS": 900,
        "INITIATIVE_HEARTBEAT_COUNT_INTERVAL_SECONDS": 60,
        "INITIATIVE_MIN_GAP_SECONDS": 0,
        "INITIATIVE_DAILY_MAX": 3,
        "INITIATIVE_ELIGIBILITY_CHANCE": 0.0,
        "INITIATIVE_SEED_FILE": tempfile.mkdtemp() + "/initiative_seed",
    }
    base.update(overrides)
    return make_config(**base)


def make_engine(config=None):
    cache, fake = make_cache()
    config = config or engine_config()
    return InitiativeEngine(config, cache), cache, fake


def make_bridge(config=None, llm=None):
    cache, fake = make_cache()
    config = config or engine_config()
    llm = llm or FakeLLM()
    bridge = Bridge(config, cache, llm=llm, connections=ConnectionManager())
    return bridge, fake, llm


async def seed_thread_history(bridge, owner: str = "owner") -> None:
    """One delivered exchange right now, so the thread reason applies."""
    user_row = hist.make_row("user", "hello there friend", hist.DELIVERED)
    await hist.append_row(
        bridge.cache, owner, user_row, bridge.config.MAX_HISTORY_TURNS
    )
    assistant_row = hist.make_row(
        "assistant", "Hello! How are you doing today?", hist.DELIVERED
    )
    await hist.append_row(
        bridge.cache, owner, assistant_row, bridge.config.MAX_HISTORY_TURNS
    )


class EngineCountingTest(unittest.IsolatedAsyncioTestCase):
    async def test_available_requires_both_flags(self):
        self.assertFalse(InitiativeEngine(make_config(), make_cache()[0]).available)
        self.assertTrue(make_engine()[0].available)
        disabled = make_engine(engine_config(HEARTBEAT_ENABLED=False))[0]
        self.assertFalse(disabled.available)

    async def test_owner_bucket_counts_once_across_devices(self):
        engine, _, _ = make_engine(engine_config(INITIATIVE_MIN_HEARTBEATS=5))
        first = await engine.count("owner", "conn_a", now_ts=60000.0)
        self.assertTrue(first["counted"])
        self.assertEqual(first["heartbeat_count"], 1)
        # A different device in the same bucket updates presence only.
        second = await engine.count("owner", "conn_b", now_ts=60030.0)
        self.assertFalse(second["counted"])
        self.assertEqual(second["heartbeat_count"], 1)
        third = await engine.count("owner", "conn_b", now_ts=6061.0)
        self.assertTrue(third["counted"])
        self.assertEqual(third["heartbeat_count"], 2)

    async def test_threshold_tracks_first_sender_of_crossing_bucket(self):
        engine, _, _ = make_engine(
            engine_config(INITIATIVE_ELIGIBILITY_CHANCE=1.0)
        )
        await engine.count("owner", "conn_a", now_ts=1000.0)
        crossed = await engine.count("owner", "conn_b", now_ts=1061.0)
        self.assertTrue(crossed["candidate"])
        self.assertEqual(crossed["target_connection_id"], "conn_b")

    async def test_window_expiry_resets_count(self):
        engine, _, _ = make_engine(engine_config(INITIATIVE_MIN_HEARTBEATS=5))
        await engine.count("owner", "conn_a", now_ts=1000.0)
        await engine.count("owner", "conn_a", now_ts=1061.0)
        expired = await engine.count("owner", "conn_a", now_ts=1000.0 + 901)
        self.assertTrue(expired["counted"])
        self.assertEqual(expired["heartbeat_count"], 1)

    async def test_daily_reset_on_owner_day_change(self):
        engine, _, _ = make_engine(engine_config(INITIATIVE_DAILY_MAX=1))
        state = fresh_state("2000-01-01")
        state["initiative_count_today"] = 1
        await engine.save("owner", state)
        result = await engine.count("owner", "conn_a", now_ts=time.time())
        self.assertEqual(result["reason"], "below_threshold")
        stored = await engine.load("owner")
        self.assertEqual(stored["initiative_count_today"], 0)
        self.assertEqual(stored["day_key"], owner_day_key("UTC", time.time()))

    async def test_daily_max_hard_gate(self):
        engine, _, _ = make_engine(engine_config(INITIATIVE_ELIGIBILITY_CHANCE=1.0))
        state = fresh_state(owner_day_key("UTC", time.time()))
        state["heartbeat_count"] = 2
        state["window_started_ts"] = time.time()
        state["initiative_count_today"] = 3
        await engine.save("owner", state)
        result = await engine.count("owner", "conn_a", now_ts=time.time())
        self.assertFalse(result["candidate"])
        self.assertEqual(result["reason"], "daily_max")

    async def test_min_gap_hard_gate(self):
        engine, _, _ = make_engine(
            engine_config(
                INITIATIVE_ELIGIBILITY_CHANCE=1.0,
                INITIATIVE_MIN_GAP_SECONDS=3600,
            )
        )
        state = fresh_state(owner_day_key("UTC", time.time()))
        state["heartbeat_count"] = 2
        state["window_started_ts"] = time.time()
        state["last_initiative_ts"] = time.time() - 100
        await engine.save("owner", state)
        result = await engine.count("owner", "conn_a", now_ts=time.time())
        self.assertFalse(result["candidate"])
        self.assertEqual(result["reason"], "min_gap")

    async def test_chance_zero_never_candidate(self):
        engine, _, _ = make_engine(
            engine_config(INITIATIVE_ELIGIBILITY_CHANCE=0.0)
        )
        state = fresh_state(owner_day_key("UTC", time.time()))
        state["heartbeat_count"] = 3
        state["window_started_ts"] = time.time()
        state["threshold_connection_id"] = "conn_a"
        await engine.save("owner", state)
        result = await engine.count("owner", "conn_a", now_ts=time.time())
        self.assertFalse(result["candidate"])
        self.assertEqual(result["reason"], "cadence_roll")

    async def test_cadence_roll_deterministic_and_bounded(self):
        a = cadence_roll("seed", "2026-09-05", 3)
        b = cadence_roll("seed", "2026-09-05", 3)
        c = cadence_roll("seed", "2026-09-05", 4)
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)
        self.assertTrue(0.0 <= a < 1.0)

    async def test_seed_created_once_and_stable(self):
        engine, _, _ = make_engine()
        first = engine.ensure_seed()
        path = Path(engine.config.INITIATIVE_SEED_FILE)
        self.assertTrue(path.exists())
        self.assertEqual(path.read_text(encoding="utf-8").strip(), first)
        self.assertEqual(engine.ensure_seed(), first)
        # A fresh engine instance reads the same seed from disk.
        other, _, _ = make_engine(
            engine_config(INITIATIVE_SEED_FILE=engine.config.INITIATIVE_SEED_FILE)
        )
        self.assertEqual(other.ensure_seed(), first)


class SeedFailureTest(unittest.IsolatedAsyncioTestCase):
    async def test_unwritable_seed_disables_engine(self):
        blocker = tempfile.mkdtemp()
        engine, _, _ = make_engine(engine_config(INITIATIVE_SEED_FILE=blocker))
        with self.assertRaises(SeedUnavailable):
            engine.ensure_seed()
        # The failure is sticky for this engine instance.
        with self.assertRaises(SeedUnavailable):
            engine.ensure_seed()


class DeliveryAccountingTest(unittest.IsolatedAsyncioTestCase):
    async def test_record_delivery_advances_counters_only(self):
        engine, _, _ = make_engine(engine_config())
        state = fresh_state(owner_day_key("UTC", time.time()))
        state["heartbeat_count"] = 2
        state["threshold_connection_id"] = "conn_a"
        state["initiative_count_today"] = 1
        await engine.save("owner", state)
        await engine.record_delivery("owner")
        stored = await engine.load("owner")
        self.assertEqual(stored["initiative_count_today"], 2)
        self.assertEqual(stored["heartbeat_count"], 0)
        self.assertEqual(stored["threshold_connection_id"], "")
        self.assertGreater(stored["last_initiative_ts"], 0)
        self.assertEqual(stored["last_decision"]["reason"], "delivered")


class InitiativeDeliveryTest(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def candidate(connection_id: str) -> dict:
        return {"target_connection_id": connection_id, "heartbeat_count": 2}

    async def test_delivered_initiative_full_protocol(self):
        config = engine_config(
            INITIATIVE_ELIGIBILITY_CHANCE=1.0, BIDS_ENABLED=True
        )
        llm = FakeLLM(["[EMOTION: happy]\nHey, I was just thinking of you."])
        bridge, fake, _ = make_bridge(config, llm)
        conn = bridge.connections.connect(RecordingWS(), "owner")
        await seed_thread_history(bridge)

        await bridge._deliver_initiative(
            "owner", self.candidate(conn.connection_id)
        )

        dones = [f for f in conn.websocket.frames if f.get("type") == "done"]
        self.assertEqual(len(dones), 1)
        done = dones[0]
        self.assertTrue(done["initiative"])
        self.assertEqual(done["initiative_action"], "thread")
        self.assertEqual(done["initiated_by"], "character")
        self.assertEqual(done["emotion"], "happy")

        rows = await hist.load_rows(bridge.cache, "owner")
        assistant = [r for r in rows if r["role"] == "assistant"][-1]
        self.assertEqual(assistant["delivery_state"], hist.DELIVERED)
        self.assertTrue(assistant["initiative"])
        self.assertEqual(assistant["initiative_action"], "thread")
        self.assertEqual(assistant["initiated_by"], "character")

        state = await bridge.initiative.load("owner")
        self.assertEqual(state["initiative_count_today"], 1)
        self.assertEqual(state["heartbeat_count"], 0)

        bids = json.loads(fake.strings[bids_key("owner")])
        self.assertEqual(len(bids), 1)
        self.assertEqual(bids[0]["kind"], "initiative_thread")
        self.assertEqual(bids[0]["result"], "open")
        self.assertEqual(llm.calls[0][0], "proactive")

    async def test_silence_delivers_nothing_and_counts_nothing(self):
        config = engine_config(INITIATIVE_ELIGIBILITY_CHANCE=1.0)
        llm = FakeLLM(["[EMOTION: neutral]\nSILENCE"])
        bridge, fake, _ = make_bridge(config, llm)
        conn = bridge.connections.connect(RecordingWS(), "owner")
        await seed_thread_history(bridge)
        before = await hist.load_rows(bridge.cache, "owner")

        await bridge._deliver_initiative(
            "owner", self.candidate(conn.connection_id)
        )

        self.assertEqual(
            [f for f in conn.websocket.frames if f.get("type") == "done"], []
        )
        after = await hist.load_rows(bridge.cache, "owner")
        self.assertEqual(len(after), len(before))
        state = await bridge.initiative.load("owner")
        self.assertEqual(state["initiative_count_today"], 0)
        self.assertEqual(state["last_decision"]["reason"], "silence")

    async def test_active_turn_suppresses_before_llm(self):
        config = engine_config(INITIATIVE_ELIGIBILITY_CHANCE=1.0)
        llm = FakeLLM(["[EMOTION: happy]\nHi again."])
        bridge, fake, _ = make_bridge(config, llm)
        conn = bridge.connections.connect(RecordingWS(), "owner")
        await seed_thread_history(bridge)
        lock = bridge.connections.turn_lock("owner")
        await lock.acquire()
        try:
            await bridge._deliver_initiative(
                "owner", self.candidate(conn.connection_id)
            )
        finally:
            lock.release()
        self.assertEqual(llm.calls, [])
        self.assertEqual(
            [f for f in conn.websocket.frames if f.get("type") == "done"], []
        )
        state = await bridge.initiative.load("owner")
        self.assertEqual(state["initiative_count_today"], 0)
        self.assertEqual(state["last_decision"]["reason"], "active_turn")

    async def test_failed_delivery_counts_nothing(self):
        config = engine_config(INITIATIVE_ELIGIBILITY_CHANCE=1.0)
        llm = FakeLLM(["[EMOTION: happy]\nHi again."])
        bridge, fake, _ = make_bridge(config, llm)
        dead = bridge.connections.connect(DeadWS(), "owner")
        await seed_thread_history(bridge)
        state = fresh_state(owner_day_key("UTC", time.time()))
        state["heartbeat_count"] = 2
        await bridge.initiative.save("owner", state)

        await bridge._deliver_initiative(
            "owner", self.candidate(dead.connection_id)
        )

        rows = await hist.load_rows(bridge.cache, "owner")
        assistant = [r for r in rows if r["role"] == "assistant"][-1]
        self.assertEqual(assistant["delivery_state"], hist.UNDELIVERED)
        stored = await bridge.initiative.load("owner")
        self.assertEqual(stored["initiative_count_today"], 0)
        self.assertEqual(stored["heartbeat_count"], 2)
        self.assertEqual(stored["last_decision"]["reason"], "undelivered")
        self.assertNotIn(bids_key("owner"), fake.strings)

    async def test_busy_schedule_suppresses_when_free_required(self):
        config = engine_config(
            INITIATIVE_ELIGIBILITY_CHANCE=1.0, SCHEDULE_ENABLED=True
        )
        llm = FakeLLM(["[EMOTION: happy]\nHi again."])
        bridge, fake, _ = make_bridge(config, llm)
        bridge.schedule = FakeSchedule(config, availability="busy")
        conn = bridge.connections.connect(RecordingWS(), "owner")
        await seed_thread_history(bridge)

        await bridge._deliver_initiative(
            "owner", self.candidate(conn.connection_id)
        )

        self.assertEqual(llm.calls, [])
        state = await bridge.initiative.load("owner")
        self.assertEqual(state["last_decision"]["reason"], "schedule")

    async def test_soft_busy_allowed_when_free_not_required(self):
        config = engine_config(
            INITIATIVE_ELIGIBILITY_CHANCE=1.0,
            SCHEDULE_ENABLED=True,
            INITIATIVE_REQUIRE_SCHEDULE_FREE=False,
        )
        llm = FakeLLM(["[EMOTION: happy]\nHi again."])
        bridge, fake, _ = make_bridge(config, llm)
        bridge.schedule = FakeSchedule(config, availability="soft_busy")
        conn = bridge.connections.connect(RecordingWS(), "owner")
        await seed_thread_history(bridge)

        await bridge._deliver_initiative(
            "owner", self.candidate(conn.connection_id)
        )

        self.assertEqual(len(llm.calls), 1)
        dones = [f for f in conn.websocket.frames if f.get("type") == "done"]
        self.assertEqual(len(dones), 1)

    async def test_critical_needs_suppress(self):
        config = engine_config(
            INITIATIVE_ELIGIBILITY_CHANCE=1.0, NEEDS_ENABLED=True
        )
        llm = FakeLLM(["[EMOTION: happy]\nHi again."])
        bridge, fake, _ = make_bridge(config, llm)
        bridge.needs.load_spec()
        conn = bridge.connections.connect(RecordingWS(), "owner")
        await seed_thread_history(bridge)
        await bridge.needs.write_state(
            "owner",
            {"values": {"fun": 5.0}, "last_eval_ts": time.time()},
        )

        await bridge._deliver_initiative(
            "owner", self.candidate(conn.connection_id)
        )

        self.assertEqual(llm.calls, [])
        state = await bridge.initiative.load("owner")
        self.assertEqual(state["last_decision"]["reason"], "needs")

    async def test_soft_block_suppresses(self):
        config = engine_config(
            INITIATIVE_ELIGIBILITY_CHANCE=1.0,
            OWNER_PROFILE_ENABLED=True,
            OWNER_SOFT_BLOCK_ENABLED=True,
        )
        llm = FakeLLM(["[EMOTION: happy]\nHi again."])
        bridge, fake, _ = make_bridge(config, llm)
        conn = bridge.connections.connect(RecordingWS(), "owner")
        await seed_thread_history(bridge)
        profile = default_profile(config)
        profile["soft_blocked"] = True
        profile["soft_blocked_until_ts"] = time.time() + 3600
        await bridge.cache.set_value(
            owner_profile_key("owner"), json.dumps(profile)
        )

        await bridge._deliver_initiative(
            "owner", self.candidate(conn.connection_id)
        )

        self.assertEqual(llm.calls, [])
        state = await bridge.initiative.load("owner")
        self.assertEqual(state["last_decision"]["reason"], "soft_block")

    async def test_owner_schedule_sleep_suppresses_only_when_enabled(self):
        config = engine_config(
            INITIATIVE_ELIGIBILITY_CHANCE=1.0,
            USER_SCHEDULE_ENABLED=True,
        )
        llm = FakeLLM(
            ["[EMOTION: happy]\nHi again.", "[EMOTION: happy]\nHi again."]
        )
        bridge, fake, _ = make_bridge(config, llm)
        conn = bridge.connections.connect(RecordingWS(), "owner")
        await seed_thread_history(bridge)
        today = datetime.now(timezone.utc).date().weekday()
        await bridge.user_schedule.apply_patch(
            "owner",
            {
                "days": {
                    DAY_KEYS[today]: [
                        {"start": "00:00", "end": "24:00", "state": "sleep"}
                    ]
                }
            },
        )

        # Flag off (default): an expected sleep window does not suppress.
        await bridge._deliver_initiative(
            "owner", self.candidate(conn.connection_id)
        )
        self.assertEqual(len(llm.calls), 1)

        # Flag on: sleep suppresses before generation.
        bridge.config.INITIATIVE_RESPECT_OWNER_SCHEDULE = True
        await bridge._deliver_initiative(
            "owner", self.candidate(conn.connection_id)
        )
        self.assertEqual(len(llm.calls), 1)
        state = await bridge.initiative.load("owner")
        self.assertEqual(state["last_decision"]["reason"], "owner_schedule")

    async def test_reason_priority_life_over_bond_over_fun(self):
        config = engine_config(NEEDS_ENABLED=True)

        class FakeLife:
            available = True

            async def pending_ids(self, owner: str):
                return ["row_1"]

        bridge, fake, _ = make_bridge(config)
        bridge.needs.load_spec()
        bridge.life = FakeLife()
        self.assertEqual(
            await bridge._select_initiative_action("owner"), "life"
        )
        bridge.life = None
        await bridge.needs.write_state(
            "owner",
            {"values": {"bond": 20.0, "fun": 5.0}, "last_eval_ts": time.time()},
        )
        self.assertEqual(
            await bridge._select_initiative_action("owner"), "bond"
        )
        await bridge.needs.write_state(
            "owner",
            {"values": {"bond": 80.0, "fun": 20.0}, "last_eval_ts": time.time()},
        )
        self.assertEqual(
            await bridge._select_initiative_action("owner"), "fun"
        )

    async def test_no_reason_selects_none(self):
        bridge, fake, _ = make_bridge(engine_config())
        self.assertIsNone(await bridge._select_initiative_action("owner"))


class ReconciliationTest(unittest.IsolatedAsyncioTestCase):
    async def test_message_ack_moves_unknown_to_delivered(self):
        bridge, fake, _ = make_bridge(engine_config())
        row = hist.make_row("assistant", "lost message", hist.DELIVERY_UNKNOWN)
        await hist.append_row(bridge.cache, "owner", row, 80)

        await bridge._reconcile_message_ack("owner", row["id"])

        rows = await hist.load_rows(bridge.cache, "owner")
        self.assertEqual(rows[0]["delivery_state"], hist.DELIVERED)
        # Idempotent duplicate.
        await bridge._reconcile_message_ack("owner", row["id"])
        rows = await hist.load_rows(bridge.cache, "owner")
        self.assertEqual(rows[0]["delivery_state"], hist.DELIVERED)

    async def test_message_ack_ignores_delivered_and_unknown_ids(self):
        bridge, fake, _ = make_bridge(engine_config())
        row = hist.make_row("assistant", "already there", hist.DELIVERED)
        await hist.append_row(bridge.cache, "owner", row, 80)
        await bridge._reconcile_message_ack("owner", row["id"])
        rows = await hist.load_rows(bridge.cache, "owner")
        self.assertEqual(rows[0]["delivery_state"], hist.DELIVERED)
        await bridge._reconcile_message_ack("owner", "msg_missing")

    async def test_startup_pending_becomes_unknown_after_threshold(self):
        bridge, fake, _ = make_bridge(engine_config())
        old = hist.make_row("assistant", "stale pending", hist.PENDING)
        old["ts"] = "2020-01-01T00:00:00+00:00"
        fresh = hist.make_row("assistant", "fresh pending", hist.PENDING)
        await hist.append_row(bridge.cache, "owner", old, 80)
        await hist.append_row(bridge.cache, "owner", fresh, 80)

        await bridge._reconcile_startup_pending("owner")

        rows = await hist.load_rows(bridge.cache, "owner")
        self.assertEqual(rows[0]["delivery_state"], hist.DELIVERY_UNKNOWN)
        self.assertEqual(rows[1]["delivery_state"], hist.PENDING)


if __name__ == "__main__":
    unittest.main()
