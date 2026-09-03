"""Milestone 0.4.0 integration tests: schedule, life, awareness, catch-up.

Acceptance coverage (plan section 31, milestone 0.4.0):
- No loop/accelerated-time code or keys exist (flag-off parity).
- Busy/unavailable paths skip LLM correctly.
- Catch-up sends once and clears only after success.
- Work/companion deferred hooks remain separated.
- Life event generated at block entry only (see test_life.py for the
  engine-level proof; here the admin route path is covered).
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from core.app import create_app
from core.cache import RedisCache
from core.config import Config
from core.constants import (
    GENERATE_LIFE_TOKEN,
    RELOAD_SCHEDULE_TOKEN,
    UPDATE_USER_SCHEDULE_TOKEN,
    busy_count_key,
    companion_history_key,
    deferred_key,
    life_last_block_key,
    life_pending_key,
    longterm_key,
    user_schedule_key,
)
from core.life import LifeEngine
from core.memory import LongTermMemory

from fakes import FakeLLM, FakeRedis, FakeSchedule, make_config


def static_lines_file() -> str:
    tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    json.dump(
        {
            "version": 1,
            "en": {"busy": "Bit busy — one moment.", "unavailable": "",
                   "soft_block": "", "stt_empty": ""},
            "es": {"busy": "", "unavailable": "", "soft_block": "", "stt_empty": ""},
            "ja": {"busy": "", "unavailable": "", "soft_block": "", "stt_empty": ""},
        },
        tmp,
    )
    tmp.close()
    return tmp.name


def template_dir() -> str:
    tmp = tempfile.mkdtemp()
    (Path(tmp) / "a.json").write_text(
        json.dumps(
            {
                "id": "example",
                "enabled": True,
                "description": "A small incident.",
                "weight": 1.0,
                "importance": 0.4,
            }
        ),
        encoding="utf-8",
    )
    return tmp


def build_app(config: Config | None = None, llm: FakeLLM | None = None):
    fake_redis = FakeRedis()
    config = config or make_config()
    llm = llm or FakeLLM()
    app = create_app(config, cache=RedisCache(fake_redis), llm=llm,
                     tailscale_addresses=set())
    return app, fake_redis, llm


class FlagOffParityTest(unittest.TestCase):
    def test_new_routes_disabled_and_turn_creates_only_history(self):
        app, fake_redis, llm = build_app()
        with TestClient(app) as client:
            self.assertEqual(client.get("/schedule").status_code, 403)
            self.assertEqual(client.get("/life/today").status_code, 403)
            self.assertEqual(client.get("/user-schedule").status_code, 403)
            self.assertEqual(client.get("/awareness").status_code, 403)
            response = client.post(
                "/admin/reload-schedule", json={"confirm": RELOAD_SCHEDULE_TOKEN}
            )
            self.assertEqual(response.status_code, 403)
            generate = client.post(
                "/life/generate", json={"confirm": GENERATE_LIFE_TOKEN}
            )
            self.assertEqual(generate.status_code, 403)
            with client.websocket_connect("/ws/owner") as ws:
                connected = ws.receive_json()
                self.assertEqual(connected["type"], "connected")
                ws.send_json({"type": "text", "text": "hi", "mode": "companion"})
                while True:
                    frame = ws.receive_json()
                    if frame.get("type") in ("done", "error"):
                        break
        expected = {companion_history_key("owner")}
        self.assertEqual(set(fake_redis.store.keys()), expected)
        for absent in (
            deferred_key("owner"), busy_count_key("owner"), longterm_key("owner"),
            life_last_block_key("owner"), life_pending_key("owner"),
            user_schedule_key("owner"),
        ):
            self.assertNotIn(absent, fake_redis.strings)
        system = llm.calls[0][1][0]["content"]
        self.assertNotIn("[AWARENESS]", system)
        self.assertNotIn("[LIFE CONTEXT]", system)

    def test_status_reports_new_sections(self):
        app, _, _ = build_app()
        with TestClient(app) as client:
            body = client.get("/status").json()
        self.assertFalse(body["schedule"]["enabled"])
        self.assertIsNone(body["schedule"]["now"])
        self.assertFalse(body["life"]["enabled"])
        self.assertEqual(body["life"]["longterm_backend"], "redis_fallback")
        self.assertFalse(body["user_schedule"]["enabled"])


class ScheduleAwarenessIntegrationTest(unittest.TestCase):
    def _config(self, **overrides) -> Config:
        return make_config(
            SCHEDULE_ENABLED=True,
            STATIC_LINES_FILE=static_lines_file(),
            **overrides,
        )

    def _swap_fake_schedule(self, app, config, availability: str) -> FakeSchedule:
        fake_schedule = FakeSchedule(config, availability=availability)
        app.state.bridge.schedule = fake_schedule
        return fake_schedule

    def test_awareness_block_injected(self):
        config = self._config()
        llm = FakeLLM()
        app, _, _ = build_app(config, llm)
        with TestClient(app) as client:
            self._swap_fake_schedule(app, config, "free")
            with client.websocket_connect("/ws/owner") as ws:
                ws.receive_json()
                ws.send_json({"type": "text", "text": "hey", "mode": "companion"})
                while True:
                    frame = ws.receive_json()
                    if frame.get("type") in ("done", "error"):
                        break
        system = llm.calls[0][1][0]["content"]
        self.assertIn("[AWARENESS]", system)
        self.assertIn("Your local time:", system)
        self.assertIn("free_time at home (free)", system)

    def test_soft_busy_short_policy_note(self):
        config = self._config(SCHEDULE_SOFT_BUSY_POLICY="short")
        llm = FakeLLM()
        app, _, _ = build_app(config, llm)
        with TestClient(app) as client:
            self._swap_fake_schedule(app, config, "soft_busy")
            with client.websocket_connect("/ws/owner") as ws:
                ws.receive_json()
                ws.send_json({"type": "text", "text": "hey", "mode": "companion"})
                while True:
                    frame = ws.receive_json()
                    if frame.get("type") in ("done", "error"):
                        break
        system = llm.calls[0][1][0]["content"]
        self.assertIn("[AVAILABILITY]", system)

    def test_schedule_and_awareness_endpoints(self):
        config = self._config()
        app, _, _ = build_app(config)
        with TestClient(app) as client:
            self._swap_fake_schedule(app, config, "busy")
            body = client.get("/schedule").json()
            self.assertEqual(body["now"]["availability"], "busy")
            awareness = client.get("/awareness").json()
            self.assertIn("[AWARENESS]", awareness["awareness_block"])
            reload_response = client.post(
                "/admin/reload-schedule", json={"confirm": RELOAD_SCHEDULE_TOKEN}
            )
            self.assertEqual(reload_response.status_code, 200)
            bad_token = client.post(
                "/admin/reload-schedule", json={"confirm": "NOPE"}
            )
            self.assertEqual(bad_token.status_code, 400)


class BusyDeferCatchupWSTest(unittest.TestCase):
    def test_busy_defer_then_catchup_over_ws(self):
        config = make_config(
            SCHEDULE_ENABLED=True,
            STATIC_LINES_FILE=static_lines_file(),
        )
        llm = FakeLLM([
            "[EMOTION: neutral]\nI'm here now!",
            "[EMOTION: happy]\nGot your messages — I'm back!",
        ])
        app, fake_redis, _ = build_app(config, llm)
        with TestClient(app) as client:
            bridge = app.state.bridge
            fake_schedule = FakeSchedule(config, availability="busy")
            bridge.schedule = fake_schedule
            with client.websocket_connect("/ws/owner") as ws:
                ws.receive_json()
                # Turn 1: busy defer, no LLM call, no history write.
                ws.send_json({"type": "text", "text": "you there?", "mode": "companion"})
                defer_done = ws.receive_json()
                self.assertEqual(defer_done["type"], "done")
                self.assertTrue(defer_done["ignored"])
                self.assertEqual(defer_done["reason"], "busy")
                self.assertTrue(defer_done["deferred"])
                self.assertEqual(defer_done["text"], "Bit busy — one moment.")
                self.assertEqual(llm.calls, [])
                self.assertNotIn(companion_history_key("owner"), fake_redis.store)
                self.assertEqual(fake_redis.strings[busy_count_key("owner")], "1")
                # Flip availability to free and send a normal message; the
                # delivered turn triggers the catch-up of the held message.
                fake_schedule.availability = "free"
                ws.send_json({"type": "text", "text": "now free?", "mode": "companion"})
                frames = []
                while True:
                    frame = ws.receive_json()
                    frames.append(frame)
                    done_frames = [f for f in frames if f.get("type") == "done"
                                   and not f.get("ignored")]
                    if len(done_frames) >= 2:
                        break
                catchup_done = done_frames[-1]
                self.assertTrue(catchup_done["catchup"])
                self.assertEqual(catchup_done["initiated_by"], "character")
        # One normal call + one catch-up call with the batch framing.
        self.assertEqual(len(llm.calls), 2)
        catchup_messages = llm.calls[1][1]
        self.assertTrue(
            any("[CATCH-UP]" in m["content"] for m in catchup_messages
                if m["role"] == "system")
        )
        self.assertIn("you there?", catchup_messages[-1]["content"])
        # Queue cleared, busy window reset, history has turn 2 plus the
        # deferred user row answered by the catch-up (processing order).
        self.assertNotIn(deferred_key("owner"), fake_redis.strings)
        self.assertNotIn(busy_count_key("owner"), fake_redis.strings)
        rows = fake_redis.store[companion_history_key("owner")]
        roles = [json.loads(row)["role"] for row in rows]
        self.assertEqual(roles, ["user", "assistant", "user", "assistant"])
        texts = [json.loads(row)["text"] for row in rows]
        self.assertEqual(texts[0], "now free?")
        self.assertEqual(texts[2], "you there?")
        states = [json.loads(row)["delivery_state"] for row in rows]
        self.assertEqual(states, ["delivered"] * 4)


class UserScheduleRouteTest(unittest.TestCase):
    def test_get_patch_flow(self):
        config = make_config(USER_SCHEDULE_ENABLED=True)
        app, fake_redis, _ = build_app(config)
        with TestClient(app) as client:
            body = client.get("/user-schedule").json()
            self.assertFalse(body["materialized"])
            self.assertNotIn(user_schedule_key("owner"), fake_redis.strings)
            no_token = client.patch(
                "/user-schedule", json={"days": {"mon": []}}
            )
            self.assertEqual(no_token.status_code, 400)
            bad_tz = client.patch(
                "/user-schedule",
                json={"timezone": "Nope/Nope"},
                headers={"X-Confirm-Token": UPDATE_USER_SCHEDULE_TOKEN},
            )
            self.assertEqual(bad_tz.status_code, 400)
            ok = client.patch(
                "/user-schedule",
                json={
                    "timezone": "UTC",
                    "days": {"mon": [
                        {"start": "09:00", "end": "17:00", "state": "busy"}
                    ]},
                },
                headers={"X-Confirm-Token": UPDATE_USER_SCHEDULE_TOKEN},
            )
            self.assertEqual(ok.status_code, 200)
            self.assertEqual(ok.json()["schedule"]["timezone"], "UTC")
        self.assertIn(user_schedule_key("owner"), fake_redis.strings)


class LifeRouteTest(unittest.TestCase):
    def test_generate_route_generates_and_persists(self):
        events_dir = template_dir()
        config = make_config(
            SCHEDULE_ENABLED=True,
            LIFE_ENABLED=False,  # startup stays inert; engine wired manually
            LIFE_EVENTS_DIR=events_dir,
        )
        llm = FakeLLM(["I watered the balcony plants."])
        app, fake_redis, _ = build_app(config, llm)
        with TestClient(app) as client:
            bridge = app.state.bridge
            bridge.config.LIFE_ENABLED = True
            bridge.schedule = FakeSchedule(config, availability="free")
            bridge.life = LifeEngine(
                config,
                bridge.cache,
                bridge.schedule,
                bridge.longterm,
                llm,
            )
            bridge.life.load_templates()
            no_token = client.post("/life/generate", json={"confirm": "X"})
            self.assertEqual(no_token.status_code, 400)
            result = client.post(
                "/life/generate",
                json={"confirm": GENERATE_LIFE_TOKEN, "force": False},
            )
            self.assertEqual(result.status_code, 200)
            self.assertTrue(result.json()["generated"])
        records = fake_redis.store[longterm_key("owner")]
        self.assertEqual(len(records), 1)
        record = json.loads(records[0])
        self.assertEqual(record["kind"], "character_life_event")
        self.assertEqual(record["text"], "I watered the balcony plants.")
        self.assertEqual(
            fake_redis.store[life_pending_key("owner")],
            [record["id"]],
        )
        # The only LLM call was the life-mode call.
        self.assertEqual(llm.calls[0][0], "life")

    def test_life_read_views(self):
        events_dir = template_dir()
        config = make_config(
            SCHEDULE_ENABLED=True,
            LIFE_EVENTS_DIR=events_dir,
        )
        llm = FakeLLM(["An event."])
        app, _, _ = build_app(config, llm)
        with TestClient(app) as client:
            bridge = app.state.bridge
            bridge.config.LIFE_ENABLED = True
            bridge.schedule = FakeSchedule(config, availability="free")
            bridge.life = LifeEngine(
                config, bridge.cache, bridge.schedule, bridge.longterm, llm
            )
            bridge.life.load_templates()
            import asyncio

            block = bridge.schedule.current_block()
            asyncio.run(bridge.life.generate_for_block("owner", block))
            today = client.get("/life/today").json()
            self.assertEqual(today["total"], 1)
            recent = client.get("/life/recent?limit=5").json()
            self.assertEqual(len(recent["items"]), 1)


if __name__ == "__main__":
    unittest.main()
