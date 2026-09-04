"""WebSocket/HTTP integration tests via FastAPI TestClient (no live server/Redis).

Covers the milestone 0.1.0 acceptance criteria (two-device sync, non-owner
rejection, heartbeat semantics, terminal errors, flags-off inertness,
production startup refusal) and the milestone 0.2.0 speech pipeline (status
frames, STT turns, TTS chunk streams, static lines, language pins).
"""

from __future__ import annotations

import base64
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from fastapi import WebSocketDisconnect
from fastapi.testclient import TestClient

from core.app import create_app
from core.cache import RedisCache
from core.config import Config
from core.constants import VERSION, companion_history_key
from core.speech import SpeechProviderError
from core.tailscale import TailscaleValidationError

from fakes import FakeLLM, FakeSTT, FakeTTS, FakeRedis, make_config


def build_app(config: Config | None = None, llm: FakeLLM | None = None,
              stt: FakeSTT | None = None, tts: FakeTTS | None = None):
    fake_redis = FakeRedis()
    config = config or make_config()
    llm = llm or FakeLLM()
    app = create_app(config, cache=RedisCache(fake_redis), llm=llm, stt=stt,
                     tts=tts, tailscale_addresses=set())
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
                self.assertEqual(frame["capabilities"], ["text", "heartbeat", "chat_sync", "work", "mcp"])
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

                    thinking = a.receive_json()
                    self.assertEqual(thinking["type"], "status")
                    self.assertEqual(thinking["status"], "thinking")
                    self.assertEqual(thinking["emotion"], "thinking")
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
                    self.assertEqual(done["type"], "status")  # thinking, sent at turn start
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
        app, _, llm = build_app(config=make_config(WORK_ENABLED=False,
                                                   SESSIONS_ENABLED=False))
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
                self.assertEqual(ws.receive_json()["type"], "status")
                frame = ws.receive_json()
                self.assertEqual(frame["type"], "error")
                self.assertEqual(frame["error"]["code"], "llm_unavailable")
                self.assertTrue(frame.get("terminal"))
        # user row persists; no fabricated assistant reply
        rows = fake_redis.store.get(companion_history_key("owner"), [])
        self.assertEqual(len(rows), 1)
        self.assertIn('"role": "user"', rows[0])

    def _turn_done(self, ws):
        """Receive the thinking status then the done frame from a source."""
        status = ws.receive_json()
        self.assertEqual(status["type"], "status")
        return ws.receive_json()

    def test_emotion_only_reply_retried_once(self):
        llm = FakeLLM(["[EMOTION: happy]", "[EMOTION: excited]\nReal answer."])
        app, _, _ = build_app(llm=llm)
        with TestClient(app) as client:
            with client.websocket_connect("/ws/owner") as ws:
                ws.receive_json()
                ws.send_json({"type": "text", "text": "hi"})
                done = self._turn_done(ws)
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
                done = self._turn_done(ws)
                self.assertEqual(done["emotion"], "neutral")
                self.assertEqual(done["text"], "Wheee.")

    def test_asterisk_roleplay_stripped(self):
        llm = FakeLLM(["[EMOTION: happy]\n*waves* Hi there."])
        app, _, _ = build_app(llm=llm)
        with TestClient(app) as client:
            with client.websocket_connect("/ws/owner") as ws:
                ws.receive_json()
                ws.send_json({"type": "text", "text": "hi"})
                done = self._turn_done(ws)
                self.assertEqual(done["text"], "Hi there.")

    def test_flags_off_no_optional_engine_keys(self):
        app, fake_redis, _ = build_app()
        with TestClient(app) as client:
            with client.websocket_connect("/ws/owner") as ws:
                ws.receive_json()
                ws.send_json({"type": "text", "text": "hello"})
                done = self._turn_done(ws)
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
        app, _, _ = build_app(config=make_config(WORK_ENABLED=False,
                                                 SESSIONS_ENABLED=False))
        with TestClient(app) as client:
            response = client.post("/message", json={"text": "x", "mode": "work"})
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.json()["error"]["code"], "work_unavailable")

    def test_http_message_work_tool_request_requires_websocket(self):
        llm = FakeLLM([{
            "text": "",
            "tool_calls": [{
                "id": "call_1",
                "name": "mcp__fs__read_file",
                "arguments": '{"path":"a.txt"}',
            }],
        }])
        app, _, _ = build_app(llm=llm)
        with TestClient(app) as client:
            response = client.post(
                "/message", json={"text": "read a.txt", "mode": "work"}
            )

        self.assertEqual(response.status_code, 502)
        self.assertEqual(
            response.json()["error"]["code"], "tools_require_websocket"
        )
        self.assertEqual(len(llm.calls), 1)

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


class LanguagePinTest(unittest.TestCase):
    def test_unsupported_language_text_turn(self):
        app, _, llm = build_app()
        with TestClient(app) as client:
            with client.websocket_connect("/ws/owner") as ws:
                ws.receive_json()
                ws.send_json({"type": "text", "text": "hi", "language": "fr"})
                frame = ws.receive_json()
                self.assertEqual(frame["error"]["code"], "unsupported_language")
                self.assertTrue(frame.get("terminal"))
        self.assertEqual(llm.calls, [])

    def test_unsupported_language_http_message(self):
        app, _, _ = build_app()
        with TestClient(app) as client:
            response = client.post("/message", json={"text": "hi", "language": "de"})
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.json()["error"]["code"], "unsupported_language")

    def test_language_pin_reaches_prompt(self):
        llm = FakeLLM(["[EMOTION: neutral]\nHola."])
        app, _, _ = build_app(llm=llm)
        with TestClient(app) as client:
            with client.websocket_connect("/ws/owner") as ws:
                ws.receive_json()
                ws.send_json({"type": "text", "text": "hola", "language": "es"})
                self.assertEqual(ws.receive_json()["type"], "status")
                self.assertEqual(ws.receive_json()["type"], "done")
        reminder = llm.calls[0][1][-2]["content"]
        self.assertIn("Reply in language: es.", reminder)


class SpeechPipelineTest(unittest.TestCase):
    def speech_config(self, **extra) -> Config:
        base = {
            "TTS_ENABLED": True,
            "ELEVENLABS_API_KEY": "el-key",
            "ELEVENLABS_VOICE_ID": "voice-9",
            "TTS_CHUNK_THRESHOLD": 150,
            "TTS_CHUNK_SIZE": 150,
            "TTS_CHUNK_SPACING_MS": 0,
            "STT_ENABLED": True,
            "STT_PROVIDER": "deepgram",
            "DEEPGRAM_API_KEY": "dg-key",
        }
        base.update(extra)
        return make_config(**base)

    def _receive_done(self, ws):
        status = ws.receive_json()
        self.assertEqual(status["type"], "status")
        done = ws.receive_json()
        self.assertEqual(done["type"], "done")
        return done

    # -- capabilities ---------------------------------------------------------

    def test_capabilities_advertise_speech_when_available(self):
        app, _, _ = build_app(config=self.speech_config(), stt=FakeSTT(), tts=FakeTTS())
        with TestClient(app) as client:
            with client.websocket_connect("/ws/owner") as ws:
                frame = ws.receive_json()
                self.assertEqual(
                    frame["capabilities"],
                    ["text", "audio", "voice_input", "heartbeat", "chat_sync",
                     "work", "mcp"],
                )

    def test_status_section_reports_speech(self):
        app, _, _ = build_app(config=self.speech_config(), stt=FakeSTT(), tts=FakeTTS())
        with TestClient(app) as client:
            body = client.get("/status").json()
            self.assertTrue(body["speech"]["tts"]["configured"])
            self.assertTrue(body["speech"]["stt"]["configured"])
            self.assertEqual(body["speech"]["stt"]["provider"], "deepgram")
            self.assertNotIn("el-key", json.dumps(body))
            self.assertNotIn("dg-key", json.dumps(body))

    # -- TTS chunk stream -----------------------------------------------------

    def test_done_precedes_chunks_in_order(self):
        llm = FakeLLM(
            ["[EMOTION: happy]\nFirst sentence here.\n[EMOTION: serious]\nSecond sentence here now."]
        )
        config = self.speech_config(TTS_CHUNK_THRESHOLD=10, TTS_CHUNK_SIZE=15)
        tts = FakeTTS()
        app, fake_redis, _ = build_app(config=config, llm=llm, tts=tts)
        with TestClient(app) as client:
            with client.websocket_connect("/ws/owner") as ws:
                ws.receive_json()
                ws.send_json({"type": "text", "text": "hi", "wants_audio": True})
                status = ws.receive_json()
                self.assertEqual(status["type"], "status")
                done = ws.receive_json()
                self.assertEqual(done["type"], "done")
                self.assertEqual(len(done["segments"]), 2)
                self.assertEqual(done["segments"][0]["emotion"], "happy")

                chunks = [ws.receive_json(), ws.receive_json()]
                self.assertEqual([c["type"] for c in chunks], ["audio_chunk", "audio_chunk"])
                self.assertEqual([c["chunk_index"] for c in chunks], [0, 1])
                self.assertTrue(all(c["total_chunks"] == 2 for c in chunks))
                self.assertEqual([c["is_final"] for c in chunks], [False, True])
                self.assertEqual([c["emotion"] for c in chunks], ["happy", "serious"])
                self.assertEqual([c["id"] for c in chunks], [done["id"]] * 2)
                self.assertEqual(chunks[0]["audio_format"], "mp3")
                self.assertEqual(
                    base64.b64decode(chunks[0]["audio"]), b"audio:happy:First sentence here."
                )

                complete = ws.receive_json()
                self.assertEqual(complete["type"], "audio_complete")
                self.assertEqual(complete["id"], done["id"])
                self.assertEqual(complete["succeeded_chunks"], 2)
                self.assertEqual(complete["failed_chunks"], 0)
        # chunking respected per-segment emotions (deterministic order)
        self.assertEqual(
            [(text, emotion) for text, emotion in tts.calls],
            [("First sentence here.", "happy"), ("Second sentence here now.", "serious")],
        )
        self.assertEqual(list(fake_redis.store.keys()), [companion_history_key("owner")])

    def test_short_reply_single_chunk(self):
        tts = FakeTTS()
        app, _, _ = build_app(config=self.speech_config(), tts=tts)
        with TestClient(app) as client:
            with client.websocket_connect("/ws/owner") as ws:
                ws.receive_json()
                ws.send_json({"type": "text", "text": "hi", "wants_audio": True})
                done = self._receive_done(ws)
                chunk = ws.receive_json()
                self.assertEqual(chunk["type"], "audio_chunk")
                self.assertTrue(chunk["is_final"])
                self.assertEqual(chunk["total_chunks"], 1)
                complete = ws.receive_json()
                self.assertEqual(complete["succeeded_chunks"], 1)
        self.assertEqual(len(tts.calls), 1)

    def test_failed_tts_chunk_keeps_text(self):
        llm = FakeLLM(
            ["[EMOTION: happy]\nFirst sentence here.\n[EMOTION: serious]\nSecond sentence here now."]
        )
        config = self.speech_config(TTS_CHUNK_THRESHOLD=10, TTS_CHUNK_SIZE=15)
        tts = FakeTTS(fail_texts={"Second sentence here now."})
        app, _, _ = build_app(config=config, llm=llm, tts=tts)
        with TestClient(app) as client:
            with client.websocket_connect("/ws/owner") as ws:
                ws.receive_json()
                ws.send_json({"type": "text", "text": "hi", "wants_audio": True})
                self.assertEqual(ws.receive_json()["type"], "status")
                done = ws.receive_json()
                # text reply intact despite failed synthesis
                self.assertIn("First sentence here.", done["text"])
                self.assertIn("Second sentence here now.", done["text"])

                chunk = ws.receive_json()
                self.assertEqual(chunk["type"], "audio_chunk")
                audio_error = ws.receive_json()
                self.assertEqual(audio_error["type"], "status")
                self.assertEqual(audio_error["status"], "error")
                complete = ws.receive_json()
                self.assertEqual(complete["type"], "audio_complete")
                self.assertEqual(complete["succeeded_chunks"], 1)
                self.assertEqual(complete["failed_chunks"], 1)
        self.assertEqual(len(tts.calls), 2)

    def test_wants_audio_with_tts_unavailable_terminates_stream(self):
        app, _, _ = build_app(config=make_config())  # TTS flag off
        with TestClient(app) as client:
            with client.websocket_connect("/ws/owner") as ws:
                ws.receive_json()
                ws.send_json({"type": "text", "text": "hi", "wants_audio": True})
                self.assertEqual(ws.receive_json()["type"], "status")
                self.assertEqual(ws.receive_json()["type"], "done")
                unavailable = ws.receive_json()
                self.assertEqual(unavailable["type"], "status")
                self.assertEqual(unavailable["status"], "error")
                complete = ws.receive_json()
                self.assertEqual(complete["type"], "audio_complete")
                self.assertEqual(complete["succeeded_chunks"], 0)
                self.assertEqual(complete["failed_chunks"], 0)

    def test_no_audio_without_wants_audio(self):
        tts = FakeTTS()
        app, _, _ = build_app(config=self.speech_config(), tts=tts)
        with TestClient(app) as client:
            with client.websocket_connect("/ws/owner") as ws:
                ws.receive_json()
                ws.send_json({"type": "text", "text": "hi"})
                done = self._receive_done(ws)
        self.assertEqual(tts.calls, [])

    def test_audio_only_sent_to_requesting_connection(self):
        llm = FakeLLM(
            ["[EMOTION: happy]\nFirst sentence here.\n[EMOTION: serious]\nSecond sentence here now."]
        )
        config = self.speech_config(TTS_CHUNK_THRESHOLD=10, TTS_CHUNK_SIZE=15)
        stt = FakeSTT(["hello from voice"])
        tts = FakeTTS()
        app, _, _ = build_app(config=config, llm=llm, stt=stt, tts=tts)
        with TestClient(app) as client:
            with client.websocket_connect("/ws/owner?device_id=a") as a:
                a.receive_json()
                with client.websocket_connect("/ws/owner?device_id=b") as b:
                    b.receive_json()
                    a.send_json(self._audio_frame(wants_audio=True))
                    # Device b sees only chat_sync frames — never audio/status/stt.
                    self.assertEqual(b.receive_json()["type"], "chat_sync")
                    self.assertEqual(b.receive_json()["type"], "chat_sync")

                    self.assertEqual(a.receive_json()["type"], "stt")
                    self.assertEqual(a.receive_json()["type"], "status")
                    self.assertEqual(a.receive_json()["type"], "done")
                    self.assertEqual(a.receive_json()["type"], "audio_chunk")
                    self.assertEqual(a.receive_json()["type"], "audio_chunk")
                    self.assertEqual(a.receive_json()["type"], "audio_complete")

    # -- voice input (STT) turns ----------------------------------------------

    @staticmethod
    def _audio_frame(**extra) -> dict:
        frame = {
            "type": "audio",
            "audio_base64": base64.b64encode(b"\x1a\x45\xdf\xa3" + b"0" * 8).decode("ascii"),
            "audio_content_type": "audio/webm",
            "stt_language": "en",
            "mode": "companion",
        }
        frame.update(extra)
        return frame

    def test_audio_turn_happy_path(self):
        stt = FakeSTT(["hello from voice"])
        llm = FakeLLM()
        app, fake_redis, _ = build_app(
            config=self.speech_config(), llm=llm, stt=stt, tts=FakeTTS()
        )
        with TestClient(app) as client:
            with client.websocket_connect("/ws/owner") as ws:
                ws.receive_json()
                ws.send_json(self._audio_frame(wants_audio=True))
                stt_frame = ws.receive_json()
                self.assertEqual(stt_frame["type"], "stt")
                self.assertEqual(stt_frame["text"], "hello from voice")
                self.assertEqual(stt_frame["provider"], "fake")
                self.assertEqual(stt_frame["language"], "en")
                done = self._receive_done(ws)
                self.assertEqual(done["text"], "Hello.")
                self.assertEqual(ws.receive_json()["type"], "audio_chunk")
                self.assertEqual(ws.receive_json()["type"], "audio_complete")
        # transcript entered the turn: one LLM call with it last, history rows
        self.assertEqual(len(llm.calls), 1)
        self.assertEqual(llm.calls[0][1][-1]["content"], "hello from voice")
        rows = fake_redis.store.get(companion_history_key("owner"), [])
        self.assertEqual(len(rows), 2)
        self.assertIn("hello from voice", rows[0])
        # STT received validated bytes and per-message language
        audio, content_type, language = stt.calls[0]
        self.assertTrue(audio.startswith(b"\x1a\x45\xdf\xa3"))
        self.assertEqual(content_type, "audio/webm")
        self.assertEqual(language, "en")

    def test_audio_turn_stt_language_pins_reply_language(self):
        stt = FakeSTT(["hola mundo"])
        llm = FakeLLM(["[EMOTION: neutral]\nHola a ti."])
        app, _, _ = build_app(config=self.speech_config(), llm=llm, stt=stt)
        with TestClient(app) as client:
            with client.websocket_connect("/ws/owner") as ws:
                ws.receive_json()
                ws.send_json(self._audio_frame(stt_language="es"))
                self.assertEqual(ws.receive_json()["type"], "stt")
                self._receive_done(ws)
        reminder = llm.calls[0][1][-2]["content"]
        self.assertIn("Reply in language: es.", reminder)

    def test_empty_stt_makes_no_llm_or_history_call(self):
        stt = FakeSTT([""])
        llm = FakeLLM()
        app, fake_redis, _ = build_app(config=self.speech_config(), llm=llm, stt=stt)
        with TestClient(app) as client:
            with client.websocket_connect("/ws/owner") as ws:
                ws.receive_json()
                ws.send_json(self._audio_frame())
                stt_frame = ws.receive_json()
                self.assertEqual(stt_frame["text"], "")
                done = ws.receive_json()
                self.assertEqual(done["type"], "done")
                self.assertTrue(done["ignored"])
                self.assertEqual(done["reason"], "stt_empty")
                self.assertNotIn("text", done)  # bundled lines are blank
                self.assertNotIn("segments", done)
        self.assertEqual(llm.calls, [])
        self.assertEqual(fake_redis.store, {})

    def test_empty_stt_uses_authored_static_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            lines_file = Path(tmp) / "lines.json"
            lines_file.write_text(json.dumps({
                "version": 1,
                "en": {"stt_empty": "I didn't catch that."},
                "es": {"stt_empty": ""},
                "ja": {"stt_empty": ""},
            }))
            stt = FakeSTT([""])
            llm = FakeLLM()
            app, fake_redis, _ = build_app(
                config=self.speech_config(STATIC_LINES_FILE=str(lines_file)),
                llm=llm, stt=stt,
            )
            with TestClient(app) as client:
                with client.websocket_connect("/ws/owner") as ws:
                    ws.receive_json()
                    ws.send_json(self._audio_frame())
                    self.assertEqual(ws.receive_json()["type"], "stt")
                    done = ws.receive_json()
                    self.assertEqual(done["type"], "done")
                    self.assertTrue(done["ignored"])
                    self.assertEqual(done["reason"], "stt_empty")
                    self.assertEqual(done["text"], "I didn't catch that.")
                    self.assertEqual(
                        done["segments"],
                        [{"text": "I didn't catch that.", "emotion": "neutral"}],
                    )
            self.assertEqual(llm.calls, [])
            self.assertEqual(fake_redis.store, {})

    def test_stt_failure_terminal_done_no_stt_frame(self):
        stt = FakeSTT(error=SpeechProviderError("deepgram returned HTTP 500"))
        llm = FakeLLM()
        app, fake_redis, _ = build_app(config=self.speech_config(), llm=llm, stt=stt)
        with TestClient(app) as client:
            with client.websocket_connect("/ws/owner") as ws:
                ws.receive_json()
                ws.send_json(self._audio_frame())
                done = ws.receive_json()  # first frame is the terminal done
                self.assertEqual(done["type"], "done")
                self.assertTrue(done["ignored"])
                self.assertEqual(done["reason"], "stt_failed")
        self.assertEqual(llm.calls, [])
        self.assertEqual(fake_redis.store, {})

    def test_stt_disabled_terminal_error(self):
        app, fake_redis, llm = build_app()  # STT flag off, real service
        with TestClient(app) as client:
            with client.websocket_connect("/ws/owner") as ws:
                ws.receive_json()
                ws.send_json(self._audio_frame())
                frame = ws.receive_json()
                self.assertEqual(frame["type"], "error")
                self.assertEqual(frame["error"]["code"], "stt_unavailable")
                self.assertTrue(frame.get("terminal"))
        self.assertEqual(llm.calls, [])
        self.assertEqual(fake_redis.store, {})

    def test_invalid_audio_variants(self):
        cases = [
            ("!!!not-base64!!!", "audio/webm", "invalid_audio"),
            (base64.b64encode(b"OggS" + b"0" * 8).decode(), "audio/webm",
             "unsupported_audio_type"),
            (base64.b64encode(b"\x1a\x45\xdf\xa3" + b"0" * 8).decode(), "audio/flac",
             "unsupported_audio_type"),
            (base64.b64encode(b"RANDOMGARBAGE").decode(), "audio/wav",
             "unsupported_audio_type"),
        ]
        for payload, content_type, code in cases:
            app, fake_redis, llm = build_app(
                config=self.speech_config(MAX_AUDIO_BYTES=1024 * 1024)
            )
            with TestClient(app) as client:
                with client.websocket_connect("/ws/owner") as ws:
                    ws.receive_json()
                    ws.send_json({
                        "type": "audio",
                        "audio_base64": payload,
                        "audio_content_type": content_type,
                    })
                    frame = ws.receive_json()
                    self.assertEqual(frame["error"]["code"], code, payload)
                    self.assertTrue(frame.get("terminal"))
            self.assertEqual(llm.calls, [])
            self.assertEqual(fake_redis.store, {})

    def test_oversized_audio_rejected(self):
        app, fake_redis, llm = build_app(
            config=self.speech_config(MAX_AUDIO_BYTES=4)
        )
        with TestClient(app) as client:
            with client.websocket_connect("/ws/owner") as ws:
                ws.receive_json()
                ws.send_json(self._audio_frame())
                frame = ws.receive_json()
                self.assertEqual(frame["error"]["code"], "audio_too_large")
                self.assertTrue(frame.get("terminal"))
        self.assertEqual(llm.calls, [])
        self.assertEqual(fake_redis.store, {})

    def test_audio_turn_unknown_mode_rejected(self):
        app, _, llm = build_app(config=self.speech_config(), stt=FakeSTT())
        with TestClient(app) as client:
            with client.websocket_connect("/ws/owner") as ws:
                ws.receive_json()
                ws.send_json(self._audio_frame(mode="sleep"))
                frame = ws.receive_json()
                self.assertEqual(frame["error"]["code"], "unknown_mode")
                self.assertTrue(frame.get("terminal"))
        self.assertEqual(llm.calls, [])

    def test_audio_turn_work_mode_rejected(self):
        config = self.speech_config()
        config.WORK_ENABLED = False
        config.SESSIONS_ENABLED = False
        app, _, llm = build_app(config=config, stt=FakeSTT())
        with TestClient(app) as client:
            with client.websocket_connect("/ws/owner") as ws:
                ws.receive_json()
                ws.send_json(self._audio_frame(mode="work"))
                frame = ws.receive_json()
                self.assertEqual(frame["error"]["code"], "work_unavailable")
                self.assertTrue(frame.get("terminal"))
        self.assertEqual(llm.calls, [])

    def test_audio_turn_unsupported_language(self):
        app, _, llm = build_app(config=self.speech_config(), stt=FakeSTT())
        with TestClient(app) as client:
            with client.websocket_connect("/ws/owner") as ws:
                ws.receive_json()
                ws.send_json(self._audio_frame(language="fr"))
                frame = ws.receive_json()
                self.assertEqual(frame["error"]["code"], "unsupported_language")
                self.assertTrue(frame.get("terminal"))
        self.assertEqual(llm.calls, [])


if __name__ == "__main__":
    unittest.main()
