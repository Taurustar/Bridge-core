"""WebSocket/HTTP integration tests via FastAPI TestClient (no live server/Redis).

Covers the milestone 0.1.0 acceptance criteria: two-device sync, non-owner
rejection, heartbeat semantics, terminal error behavior, flags-off inertness,
and production startup refusal.
"""

from __future__ import annotations

import threading
import time
import unittest

from fastapi import WebSocketDisconnect
from fastapi.testclient import TestClient

from core.app import create_app
from core.cache import RedisCache
from core.config import Config
from core.constants import VERSION, companion_history_key
from core.tailscale import TailscaleValidationError

from fakes import FakeLLM, FakeRedis, make_config


def build_app(config: Config | None = None, llm: FakeLLM | None = None):
    fake_redis = FakeRedis()
    config = config or make_config()
    llm = llm or FakeLLM()
    app = create_app(config, cache=RedisCache(fake_redis), llm=llm,
                     tailscale_addresses=set())
    return app, fake_redis, llm


def wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class ConnectedFrameTest(unittest.TestCase):
    def test_connected_frame_shape(self):
        app, _, _ = build_app()
        with TestClient(app) as client:
            with client.websocket_connect("/ws/owner?client_type=desktop") as ws:
                frame = ws.receive_json()
                self.assertEqual(frame["type"], "connected")
                self.assertTrue(frame["connection_id"].startswith("conn_"))
                self.assertEqual(frame["server_version"], VERSION)
                self.assertEqual(frame["capabilities"], ["text", "heartbeat", "chat_sync"])
                self.assertTrue(frame["server_time"])

    def test_non_owner_rejected(self):
        app, _, _ = build_app()
        with TestClient(app) as client:
            with client.websocket_connect("/ws/intruder") as ws:
                frame = ws.receive_json()
                self.assertEqual(frame["type"], "error")
                self.assertEqual(frame["error"]["code"], "forbidden_user")
                with self.assertRaises(WebSocketDisconnect):
                    ws.receive_json()


class TwoDeviceSyncTest(unittest.TestCase):
    def test_turn_fans_out_to_other_device(self):
        app, fake_redis, llm = build_app()
        with TestClient(app) as client:
            with client.websocket_connect("/ws/owner?client_type=desktop") as a:
                conn_a = a.receive_json()
                with client.websocket_connect("/ws/owner?client_type=mobile") as b:
                    conn_b = b.receive_json()
                    self.assertNotEqual(
                        conn_a["connection_id"], conn_b["connection_id"]
                    )
                    a.send_json({"type": "text", "text": "hello there", "mode": "companion"})

                    sync_user = b.receive_json()
                    self.assertEqual(sync_user["type"], "chat_sync")
                    self.assertEqual(sync_user["role"], "user")
                    self.assertEqual(sync_user["text"], "hello there")
                    self.assertEqual(sync_user["origin_connection_id"], conn_a["connection_id"])

                    sync_assistant = b.receive_json()
                    self.assertEqual(sync_assistant["type"], "chat_sync")
                    self.assertEqual(sync_assistant["role"], "assistant")
                    self.assertEqual(sync_assistant["initiated_by"], "character")

                    done = a.receive_json()
                    self.assertEqual(done["type"], "done")
                    self.assertTrue(done["id"].startswith("msg_"))
                    self.assertEqual(done["text"], "Hello.")
                    self.assertEqual(done["emotion"], "neutral")
                    self.assertEqual(done["mode"], "companion")
                    self.assertEqual(done["provider"], "fake")
                    self.assertEqual(done["tokens"]["total"], 15)
                    self.assertEqual(sync_assistant["id"], done["id"])

        rows = fake_redis.store.get(companion_history_key("owner"), [])
        self.assertEqual(len(rows), 2)
        self.assertIn('"delivery_state": "delivered"', rows[0])
        self.assertIn('"delivery_state": "delivered"', rows[1])
        self.assertEqual(len(llm.calls), 1)


class HeartbeatTest(unittest.TestCase):
    def connect(self, client):
        ws = client.websocket_connect("/ws/owner")
        return ws

    def test_heartbeat_ack_shape(self):
        app, _, _ = build_app()
        with TestClient(app) as client:
            with client.websocket_connect("/ws/owner") as ws:
                ws.receive_json()
                ws.send_json({"type": "heartbeat", "sequence": 1,
                              "last_input_at": time.time()})
                ack = ws.receive_json()
                self.assertEqual(ack["type"], "heartbeat_ack")
                self.assertTrue(ack["server_time"])
                self.assertEqual(ack["initiative_counter"], 0)
                self.assertTrue(ack["counted"])

    def test_missing_sequence_errors(self):
        app, _, _ = build_app()
        with TestClient(app) as client:
            with client.websocket_connect("/ws/owner") as ws:
                ws.receive_json()
                ws.send_json({"type": "heartbeat"})
                frame = ws.receive_json()
                self.assertEqual(frame["type"], "error")
                self.assertEqual(frame["error"]["code"], "invalid_heartbeat")

    def test_negative_sequence_errors(self):
        app, _, _ = build_app()
        with TestClient(app) as client:
            with client.websocket_connect("/ws/owner") as ws:
                ws.receive_json()
                ws.send_json({"type": "heartbeat", "sequence": -1})
                frame = ws.receive_json()
                self.assertEqual(frame["error"]["code"], "invalid_heartbeat")

    def test_stale_timestamp_errors(self):
        app, _, _ = build_app()
        with TestClient(app) as client:
            with client.websocket_connect("/ws/owner") as ws:
                ws.receive_json()
                ws.send_json({"type": "heartbeat", "sequence": 1,
                              "last_input_at": time.time() - 3600})
                frame = ws.receive_json()
                self.assertEqual(frame["error"]["code"], "invalid_heartbeat")

    def test_replay_acked_but_not_counted(self):
        app, _, _ = build_app()
        with TestClient(app) as client:
            with client.websocket_connect("/ws/owner") as ws:
                ws.receive_json()
                ws.send_json({"type": "heartbeat", "sequence": 5})
                first = ws.receive_json()
                self.assertTrue(first["counted"])
                ws.send_json({"type": "heartbeat", "sequence": 5})
                replay = ws.receive_json()
                self.assertEqual(replay["type"], "heartbeat_ack")
                self.assertFalse(replay["counted"])
                ws.send_json({"type": "heartbeat", "sequence": 3})
                out_of_order = ws.receive_json()
                self.assertFalse(out_of_order["counted"])

    def test_heartbeat_acked_during_active_turn(self):
        gate = threading.Event()
        llm = FakeLLM()
        llm.block_gate = gate
        app, _, _ = build_app(llm=llm)
        with TestClient(app) as client:
            with client.websocket_connect("/ws/owner?device_id=a") as a:
                a.receive_json()
                with client.websocket_connect("/ws/owner?device_id=b") as b:
                    b.receive_json()
                    a.send_json({"type": "text", "text": "block inside the llm"})
                    self.assertTrue(wait_until(lambda: len(llm.calls) == 1))

                    b.send_json({"type": "heartbeat", "sequence": 1})
                    # B first gets the user chat_sync, then the ack — while the
                    # turn is still blocked inside the fake provider.
                    frames = [b.receive_json() for _ in range(2)]
                    types = [f["type"] for f in frames]
                    self.assertEqual(types[0], "chat_sync")
                    self.assertEqual(types[1], "heartbeat_ack")
                    self.assertFalse(gate.is_set())

                    gate.set()
                    done = a.receive_json()
                    self.assertEqual(done["type"], "done")
                    self.assertEqual(b.receive_json()["type"], "chat_sync")


class TurnSemanticsTest(unittest.TestCase):
    def test_empty_text_terminal_error(self):
        app, fake_redis, llm = build_app()
        with TestClient(app) as client:
            with client.websocket_connect("/ws/owner") as ws:
                ws.receive_json()
                ws.send_json({"type": "text", "text": "   "})
                frame = ws.receive_json()
                self.assertEqual(frame["type"], "error")
                self.assertEqual(frame["error"]["code"], "empty_input")
                self.assertTrue(frame.get("terminal"))
        self.assertEqual(fake_redis.store, {})
        self.assertEqual(llm.calls, [])

    def test_unknown_mode_error_never_silent_companion(self):
        app, _, llm = build_app()
        with TestClient(app) as client:
            with client.websocket_connect("/ws/owner") as ws:
                ws.receive_json()
                ws.send_json({"type": "text", "text": "hi", "mode": "sleep"})
                frame = ws.receive_json()
                self.assertEqual(frame["error"]["code"], "unknown_mode")
                self.assertTrue(frame.get("terminal"))
        self.assertEqual(llm.calls, [])

    def test_work_mode_unavailable_error(self):
        app, _, llm = build_app()
        with TestClient(app) as client:
            with client.websocket_connect("/ws/owner") as ws:
                ws.receive_json()
                ws.send_json({"type": "text", "text": "fix the bug", "mode": "work"})
                frame = ws.receive_json()
                self.assertEqual(frame["error"]["code"], "work_unavailable")
                self.assertTrue(frame.get("terminal"))
        self.assertEqual(llm.calls, [])

    def test_llm_failure_terminates_with_error_frame(self):
        from core.llm import LLMChainExhausted

        llm = FakeLLM([LLMChainExhausted("all routes failed")])
        app, fake_redis, _ = build_app(llm=llm)
        with TestClient(app) as client:
            with client.websocket_connect("/ws/owner") as ws:
                ws.receive_json()
                ws.send_json({"type": "text", "text": "hello"})
                frame = ws.receive_json()
                self.assertEqual(frame["type"], "error")
                self.assertEqual(frame["error"]["code"], "llm_unavailable")
                self.assertTrue(frame.get("terminal"))
        # user row persists; no fabricated assistant reply
        rows = fake_redis.store.get(companion_history_key("owner"), [])
        self.assertEqual(len(rows), 1)
        self.assertIn('"role": "user"', rows[0])

    def test_emotion_only_reply_retried_once(self):
        llm = FakeLLM(["[EMOTION: happy]", "[EMOTION: excited]\nReal answer."])
        app, _, _ = build_app(llm=llm)
        with TestClient(app) as client:
            with client.websocket_connect("/ws/owner") as ws:
                ws.receive_json()
                ws.send_json({"type": "text", "text": "hi"})
                done = ws.receive_json()
                self.assertEqual(done["type"], "done")
                self.assertEqual(done["text"], "Real answer.")
                self.assertEqual(done["emotion"], "excited")
                self.assertEqual(done["tokens"]["total"], 30)  # usage additive
        self.assertEqual(len(llm.calls), 2)

    def test_unknown_emotion_normalizes_to_neutral(self):
        llm = FakeLLM(["[EMOTION: ecstatic]\nWheee."])
        app, _, _ = build_app(llm=llm)
        with TestClient(app) as client:
            with client.websocket_connect("/ws/owner") as ws:
                ws.receive_json()
                ws.send_json({"type": "text", "text": "hi"})
                done = ws.receive_json()
                self.assertEqual(done["emotion"], "neutral")
                self.assertEqual(done["text"], "Wheee.")

    def test_asterisk_roleplay_stripped(self):
        llm = FakeLLM(["[EMOTION: happy]\n*waves* Hi there."])
        app, _, _ = build_app(llm=llm)
        with TestClient(app) as client:
            with client.websocket_connect("/ws/owner") as ws:
                ws.receive_json()
                ws.send_json({"type": "text", "text": "hi"})
                done = ws.receive_json()
                self.assertEqual(done["text"], "Hi there.")

    def test_flags_off_no_optional_engine_keys(self):
        app, fake_redis, _ = build_app()
        with TestClient(app) as client:
            with client.websocket_connect("/ws/owner") as ws:
                ws.receive_json()
                ws.send_json({"type": "text", "text": "hello"})
                done = ws.receive_json()
                self.assertEqual(done["type"], "done")
        keys = list(fake_redis.store.keys())
        self.assertEqual(keys, [companion_history_key("owner")])


class HttpEndpointsTest(unittest.TestCase):
    def test_health_ok_and_degraded(self):
        app, fake_redis, _ = build_app()
        with TestClient(app) as client:
            response = client.get("/health")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["redis"], True)
            fake_redis.up = False
            degraded = client.get("/health")
            self.assertEqual(degraded.status_code, 503)
            self.assertEqual(degraded.json()["status"], "degraded")

    def test_status_has_no_secrets(self):
        config = make_config(FIREWORKS_API_KEY="supersecretvalue",
                             FIREWORKS_MODEL="fw-model")
        app, _, _ = build_app(config=config)
        with TestClient(app) as client:
            response = client.get("/status")
            self.assertEqual(response.status_code, 200)
            self.assertNotIn("supersecretvalue", response.text)
            body = response.json()
            self.assertEqual(body["version"], VERSION)
            self.assertTrue(body["providers"]["fireworks"]["configured"])
            self.assertFalse(body["providers"]["chutes"]["configured"])
            self.assertIn("identity_files", body)
            self.assertIn("features", body)

    def test_emotions_endpoint(self):
        app, _, _ = build_app()
        with TestClient(app) as client:
            body = client.get("/emotions").json()
            self.assertEqual(body["version"], 1)
            self.assertEqual(len(body["emotions"]), 18)
            self.assertEqual(
                body["status_emotions"],
                ["thinking", "working", "question", "request_permission"],
            )

    def test_http_message_turn(self):
        app, fake_redis, _ = build_app()
        with TestClient(app) as client:
            response = client.post("/message", json={"text": "hello over http"})
            self.assertEqual(response.status_code, 200)
            body = response.json()
            self.assertEqual(body["type"], "done")
            self.assertEqual(body["text"], "Hello.")
            self.assertTrue(body["id"].startswith("msg_"))
        rows = fake_redis.store.get(companion_history_key("owner"), [])
        self.assertEqual(len(rows), 2)

    def test_http_message_rejects_non_owner(self):
        app, _, _ = build_app()
        with TestClient(app) as client:
            response = client.post("/message", json={"user_id": "intruder", "text": "hi"})
            self.assertEqual(response.status_code, 403)
            self.assertEqual(response.json()["error"]["code"], "forbidden_user")

    def test_http_message_work_unavailable(self):
        app, _, _ = build_app()
        with TestClient(app) as client:
            response = client.post("/message", json={"text": "x", "mode": "work"})
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.json()["error"]["code"], "work_unavailable")

    def test_http_message_empty_input(self):
        app, _, _ = build_app()
        with TestClient(app) as client:
            response = client.post("/message", json={"text": " "})
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.json()["error"]["code"], "empty_input")


class StartupValidationTest(unittest.TestCase):
    def test_production_startup_refused_for_all_interfaces_bind(self):
        config = make_config(BRIDGE_HOST="0.0.0.0", TAILSCALE_REQUIRED=True,
                             TAILSCALE_FIREWALL_ACK=False)
        app, _, _ = build_app(config=config)
        with self.assertRaises(TailscaleValidationError):
            with TestClient(app):
                pass

    def test_startup_allowed_with_ack(self):
        config = make_config(BRIDGE_HOST="0.0.0.0", TAILSCALE_REQUIRED=True,
                             TAILSCALE_FIREWALL_ACK=True)
        app, _, _ = build_app(config=config)
        with TestClient(app) as client:
            self.assertEqual(client.get("/health").status_code, 200)

    def test_startup_refused_when_redis_down(self):
        config = make_config()
        fake_redis = FakeRedis()
        fake_redis.up = False
        app = create_app(config, cache=RedisCache(fake_redis), llm=FakeLLM(),
                         tailscale_addresses=set())
        with self.assertRaises(RuntimeError):
            with TestClient(app):
                pass


if __name__ == "__main__":
    unittest.main()
