"""Milestone 0.7.0 WS integration tests: heartbeat initiative, reconciliation.

Acceptance coverage (plan section 31, milestone 0.7.0):
- Counters advance only on delivery (undelivered/SILENCE never increment).
- Daily max/min gap hard-enforced (engine-level tests in test_initiative.py).
- Soft block/schedule/active turn suppress initiative (test_initiative.py).
- Store-enabled CRUD with behavior inert; store-disabled creates no keys
  (test_external_profiles.py).
- Here: WS heartbeat counting, initiative origin frames/chat sync, and
  message_ack reconciliation.
"""

from __future__ import annotations

import json
import tempfile
import time
import unittest

from fastapi.testclient import TestClient

from core import history as hist
from core.app import create_app
from core.cache import RedisCache
from core.config import Config
from core.constants import companion_history_key, initiative_key

from fakes import FakeLLM, FakeRedis, make_config


def build_app(config: Config | None = None, llm: FakeLLM | None = None):
    fake_redis = FakeRedis()
    config = config or make_config()
    llm = llm or FakeLLM()
    app = create_app(config, cache=RedisCache(fake_redis), llm=llm,
                     tailscale_addresses=set())
    return app, fake_redis, llm


def initiative_config(**overrides):
    base = {
        "INITIATIVE_ENABLED": True,
        "INITIATIVE_MIN_HEARTBEATS": 2,
        "INITIATIVE_HEARTBEAT_WINDOW_SECONDS": 900,
        "INITIATIVE_HEARTBEAT_COUNT_INTERVAL_SECONDS": 1,
        "INITIATIVE_MIN_GAP_SECONDS": 0,
        "INITIATIVE_DAILY_MAX": 3,
        "INITIATIVE_ELIGIBILITY_CHANCE": 1.0,
        "INITIATIVE_SEED_FILE": tempfile.mkdtemp() + "/initiative_seed",
        "BIDS_ENABLED": True,
    }
    base.update(overrides)
    return make_config(**base)


def drain_until(ws, predicate, limit: int = 30):
    """Read frames until one satisfies the predicate; returns it."""
    for _ in range(limit):
        frame = ws.receive_json()
        if predicate(frame):
            return frame
    raise AssertionError("expected frame never arrived")


class HeartbeatFlagOffParityTest(unittest.TestCase):
    def test_disabled_engine_keeps_stub_and_creates_no_key(self):
        app, fake_redis, _ = build_app()
        with TestClient(app) as client:
            with client.websocket_connect("/ws/owner") as ws:
                self.assertEqual(ws.receive_json()["type"], "connected")
                ws.send_json({"type": "heartbeat", "sequence": 1})
                ack = ws.receive_json()
                self.assertEqual(ack["type"], "heartbeat_ack")
                self.assertEqual(ack["initiative_counter"], 0)
                self.assertTrue(ack["counted"])
        self.assertNotIn(initiative_key("owner"), fake_redis.strings)


class HeartbeatCountingTest(unittest.TestCase):
    def test_owner_counter_crosses_buckets_and_acks(self):
        config = initiative_config(INITIATIVE_ELIGIBILITY_CHANCE=0.0)
        llm = FakeLLM()
        app, fake_redis, _ = build_app(config, llm)
        with TestClient(app) as client:
            with client.websocket_connect("/ws/owner") as ws:
                self.assertEqual(ws.receive_json()["type"], "connected")
                ws.send_json({"type": "heartbeat", "sequence": 1})
                first = ws.receive_json()
                self.assertEqual(first["type"], "heartbeat_ack")
                self.assertEqual(first["initiative_counter"], 1)
                # Same bucket: the sequence counts but the bucket does not.
                ws.send_json({"type": "heartbeat", "sequence": 2})
                replay_bucket = ws.receive_json()
                self.assertTrue(replay_bucket["counted"])
                self.assertEqual(replay_bucket["initiative_counter"], 1)
                # Next bucket: the owner counter advances.
                time.sleep(1.1)
                ws.send_json({"type": "heartbeat", "sequence": 3})
                second = ws.receive_json()
                self.assertEqual(second["initiative_counter"], 2)
        state = json.loads(fake_redis.strings[initiative_key("owner")])
        self.assertEqual(state["heartbeat_count"], 2)
        self.assertEqual(state["last_decision"]["reason"], "cadence_roll")
        self.assertEqual(llm.calls, [])


class InitiativeDeliveryWsTest(unittest.TestCase):
    def test_delivered_initiative_origin_frames_and_sync(self):
        config = initiative_config()
        llm = FakeLLM([
            "[EMOTION: happy]\nA normal friendly reply to your hello!",
            "[EMOTION: playful]\nHey you — I just remembered something funny.",
        ])
        app, fake_redis, _ = build_app(config, llm)
        with TestClient(app) as client:
            with client.websocket_connect("/ws/owner") as ws1, \
                    client.websocket_connect("/ws/owner") as ws2:
                self.assertEqual(ws1.receive_json()["type"], "connected")
                self.assertEqual(ws2.receive_json()["type"], "connected")

                # One ordinary turn first: thread history for the reason
                # selection, and proof that replies carry no initiative flag.
                ws1.send_json({"type": "text", "text": "hello there, friend"})
                turn_done = drain_until(ws1, lambda f: f.get("type") == "done")
                self.assertNotIn("initiative", turn_done)
                user_sync = drain_until(
                    ws2, lambda f: f.get("type") == "chat_sync"
                )
                self.assertEqual(user_sync["initiated_by"], "user")
                drain_until(ws2, lambda f: f.get("type") == "chat_sync")

                ws1.send_json({"type": "heartbeat", "sequence": 1})
                ack1 = ws1.receive_json()
                self.assertEqual(ack1["initiative_counter"], 1)
                time.sleep(1.1)
                ws1.send_json({"type": "heartbeat", "sequence": 2})

                initiative_done = drain_until(
                    ws1,
                    lambda f: f.get("type") == "done" and f.get("initiative") is True,
                )
                self.assertEqual(initiative_done["initiative_action"], "thread")
                self.assertEqual(initiative_done["initiated_by"], "character")
                initiative_sync = drain_until(
                    ws2,
                    lambda f: f.get("type") == "chat_sync"
                    and f.get("initiative") is True,
                )
                self.assertEqual(initiative_sync["id"], initiative_done["id"])
                self.assertEqual(initiative_sync["initiated_by"], "character")

        state = json.loads(fake_redis.strings[initiative_key("owner")])
        self.assertEqual(state["initiative_count_today"], 1)
        self.assertEqual(state["heartbeat_count"], 0)
        self.assertEqual(state["last_decision"]["reason"], "delivered")
        rows = [
            json.loads(raw)
            for raw in fake_redis.store[companion_history_key("owner")]
        ]
        initiative_rows = [r for r in rows if r.get("initiative")]
        self.assertEqual(len(initiative_rows), 1)
        self.assertEqual(initiative_rows[0]["delivery_state"], "delivered")
        bids = json.loads(fake_redis.strings["core:bids:owner"])
        self.assertEqual(bids[0]["kind"], "initiative_thread")

    def test_silence_initiative_delivers_nothing(self):
        config = initiative_config()
        llm = FakeLLM([
            "[EMOTION: happy]\nA normal friendly reply to your hello!",
            "[EMOTION: neutral]\nSILENCE",
        ])
        app, fake_redis, _ = build_app(config, llm)
        with TestClient(app) as client:
            with client.websocket_connect("/ws/owner") as ws:
                self.assertEqual(ws.receive_json()["type"], "connected")
                ws.send_json({"type": "text", "text": "hello there, friend"})
                drain_until(ws, lambda f: f.get("type") == "done")

                ws.send_json({"type": "heartbeat", "sequence": 1})
                ws.receive_json()
                time.sleep(1.1)
                ws.send_json({"type": "heartbeat", "sequence": 2})
                ws.receive_json()
                # Give the background delivery task a moment to resolve.
                deadline = time.time() + 3
                while time.time() < deadline:
                    state = json.loads(
                        fake_redis.strings.get(initiative_key("owner"), "{}")
                    )
                    if state.get("last_decision", {}).get("reason") in (
                        "silence",
                        "llm_failed",
                    ):
                        break
                    time.sleep(0.05)
        self.assertEqual(state["last_decision"]["reason"], "silence")
        self.assertEqual(state["initiative_count_today"], 0)


class MessageAckTest(unittest.TestCase):
    def test_startup_pending_becomes_unknown_then_ack_delivers(self):
        app, fake_redis, _ = build_app()
        cache = RedisCache(fake_redis)
        stale = hist.make_row("assistant", "stale pending row", hist.PENDING)
        stale["ts"] = "2020-01-01T00:00:00+00:00"

        def seed():
            rows = fake_redis.store.setdefault(
                companion_history_key("owner"), []
            )
            rows.append(json.dumps(stale))

        seed()
        with TestClient(app) as client:
            # Startup reconciliation ran inside the lifespan: stale pending
            # rows older than the threshold are now delivery_unknown.
            stored = json.loads(
                fake_redis.store[companion_history_key("owner")][0]
            )
            self.assertEqual(stored["delivery_state"], "delivery_unknown")

            with client.websocket_connect("/ws/owner") as ws:
                self.assertEqual(ws.receive_json()["type"], "connected")
                ws.send_json(
                    {"type": "message_ack", "id": stale["id"]}
                )
                deadline = time.time() + 3
                while time.time() < deadline:
                    stored = json.loads(
                        fake_redis.store[companion_history_key("owner")][0]
                    )
                    if stored["delivery_state"] == "delivered":
                        break
                    time.sleep(0.05)
        self.assertEqual(stored["delivery_state"], "delivered")

    def test_message_ack_unknown_id_is_ignored(self):
        app, fake_redis, _ = build_app()
        with TestClient(app) as client:
            with client.websocket_connect("/ws/owner") as ws:
                self.assertEqual(ws.receive_json()["type"], "connected")
                ws.send_json({"type": "message_ack", "id": "msg_missing"})
                ws.send_json({"type": "heartbeat", "sequence": 1})
                ack = ws.receive_json()
                self.assertEqual(ack["type"], "heartbeat_ack")
        self.assertNotIn(companion_history_key("owner"), fake_redis.store)


if __name__ == "__main__":
    unittest.main()
