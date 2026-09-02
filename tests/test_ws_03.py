"""Milestone 0.3.0 integration tests: needs/owner-profile turn wiring.

Acceptance coverage (plan section 31, milestone 0.3.0):
- Poll endpoints write nothing.
- Soft block prevents companion LLM/bids/history writes (work bypass is a
  plan 12 step 5 mode-scope property; work itself ships in 0.5.0).
- Flag off creates no profile/needs keys (flag-off parity).
- Enabled profile injects the [CHARACTER STATE] and [OWNER RELATIONSHIP]
  blocks and materializes stores only through behavior paths.
"""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from core.app import create_app
from core.cache import RedisCache
from core.config import Config
from core.constants import (
    UPDATE_OWNER_PROFILE_TOKEN,
    bids_key,
    companion_history_key,
    needs_key,
    owner_profile_key,
    rhythm_key,
)

from fakes import FakeLLM, FakeRedis, make_config


def build_app(config: Config | None = None, llm: FakeLLM | None = None):
    fake_redis = FakeRedis()
    config = config or make_config()
    llm = llm or FakeLLM()
    app = create_app(config, cache=RedisCache(fake_redis), llm=llm,
                     tailscale_addresses=set())
    return app, fake_redis, llm


from contextlib import contextmanager


@contextmanager
def ws_connect(client):
    """Yield an owner WS session after consuming the connected frame."""
    with client.websocket_connect("/ws/owner") as ws:
        connected = ws.receive_json()
        assert connected["type"] == "connected"
        yield ws


def text_turn(ws, text: str, **extra):
    ws.send_json({"type": "text", "text": text, "mode": "companion", **extra})
    frames = []
    while True:
        frame = ws.receive_json()
        frames.append(frame)
        if frame.get("type") in ("done", "error"):
            return frames


class FlagOffParityTest(unittest.TestCase):
    """All 0.3.0 flags default OFF: a turn must create only history keys."""

    def test_turn_creates_only_history_key(self):
        app, fake_redis, llm = build_app()
        with TestClient(app) as client:
            with ws_connect(client) as ws:
                frames = text_turn(ws, "hello there")
        self.assertEqual(frames[-1]["type"], "done")
        expected = {companion_history_key("owner")}
        self.assertEqual(set(fake_redis.store.keys()), expected)
        self.assertEqual(set(fake_redis.strings.keys()), set())
        self.assertEqual(len(llm.calls), 1)
        system = llm.calls[0][1][0]["content"]
        self.assertNotIn("[CHARACTER STATE]", system)
        self.assertNotIn("[OWNER RELATIONSHIP", system)

    def test_poll_endpoints_disabled_are_403_and_write_nothing(self):
        app, fake_redis, _ = build_app()
        with TestClient(app) as client:
            self.assertEqual(client.get("/state").status_code, 403)
            self.assertEqual(client.get("/profiles/owner").status_code, 403)
        self.assertEqual(set(fake_redis.store.keys()), set())
        self.assertEqual(set(fake_redis.strings.keys()), set())


class EnabledProfileTurnTest(unittest.TestCase):
    def _config(self) -> Config:
        return make_config(
            NEEDS_ENABLED=True,
            STATE_EXPRESSION_ENABLED=True,
            OWNER_PROFILE_ENABLED=True,
            OWNER_PROFILE_INJECT=True,
            BIDS_ENABLED=True,
            RHYTHM_ENABLED=True,
        )

    def test_turn_injects_state_and_relationship_blocks(self):
        app, fake_redis, llm = build_app(self._config())
        with TestClient(app) as client:
            with ws_connect(client) as ws:
                frames = text_turn(ws, "hello there")
        done = frames[-1]
        self.assertEqual(done["type"], "done")

        # Prompt injection (plan 12 steps 15-16, identity order 6.3).
        system = llm.calls[0][1][0]["content"]
        self.assertIn("[CHARACTER STATE]", system)
        self.assertIn("[OWNER RELATIONSHIP - LIVED]", system)
        state_idx = system.index("[CHARACTER STATE]")
        profile_idx = system.index("[OWNER RELATIONSHIP")
        soul_idx = system.lower().index("# soul")
        self.assertLess(soul_idx, state_idx)
        self.assertLess(state_idx, profile_idx)
        # Zone lines never carry numbers.
        state_block = system[state_idx:system.index("[AGENCY THIS TURN]")]
        for line in state_block.splitlines():
            if ":" in line and not line.startswith("["):
                zone = line.split(": ", 1)[1]
                self.assertFalse(zone.strip().isdigit(), line)

        # First behavior turn materializes the stores.
        self.assertIn(needs_key("owner"), fake_redis.strings)
        self.assertIn(owner_profile_key("owner"), fake_redis.strings)
        self.assertIn(rhythm_key("owner"), fake_redis.strings)
        # Bids are inert until initiative registers them (0.7.0).
        self.assertNotIn(bids_key("owner"), fake_redis.strings)
        # History: user + assistant rows only.
        rows = fake_redis.store[companion_history_key("owner")]
        self.assertEqual(len(rows), 2)

    def test_profile_analysis_runs_when_llm_flag_enabled(self):
        config = self._config()
        config.OWNER_PROFILE_LLM_ENABLED = True
        llm = FakeLLM(replies=[
            "[EMOTION: neutral]\nHello.",
            json.dumps({"persona_summary": "Enjoys late-night chats.",
                        "appeal_delta": 1}),
        ])
        app, fake_redis, _ = build_app(config, llm)
        with TestClient(app) as client:
            with ws_connect(client) as ws:
                text_turn(ws, "hello there")
                deadline = time.monotonic() + 5.0
                while time.monotonic() < deadline:
                    profile = json.loads(
                        fake_redis.strings.get(owner_profile_key("owner"), "{}")
                    )
                    if profile.get("proposals_applied"):
                        break
                    time.sleep(0.01)
        profile = json.loads(fake_redis.strings[owner_profile_key("owner")])
        self.assertEqual(profile["proposals_applied"], 1)
        self.assertEqual(profile["persona_summary"], "Enjoys late-night chats.")
        self.assertEqual(profile["appeal"], 51)
        # Raw turn text never enters the store.
        self.assertNotIn("hello there", fake_redis.strings[owner_profile_key("owner")])

    def test_poll_endpoints_write_nothing_when_enabled_but_empty(self):
        app, fake_redis, _ = build_app(self._config())
        with TestClient(app) as client:
            state_body = client.get("/state").json()
            profile_body = client.get("/profiles/owner").json()
        self.assertIn("zones", state_body)
        self.assertFalse(profile_body["materialized"])
        self.assertEqual(set(fake_redis.strings.keys()), set())
        self.assertEqual(set(fake_redis.store.keys()), set())


class SoftBlockGateTest(unittest.TestCase):
    def _build(self, with_static_line: bool):
        static_file = None
        if with_static_line:
            tmp = tempfile.NamedTemporaryFile(
                "w", suffix=".json", delete=False
            )
            json.dump({
                "version": 1,
                "en": {"busy": "", "unavailable": "", "stt_empty": "",
                       "soft_block": "Please give me some space."},
                "es": {"busy": "", "unavailable": "", "stt_empty": "",
                       "soft_block": ""},
                "ja": {"busy": "", "unavailable": "", "stt_empty": "",
                       "soft_block": ""},
            }, tmp)
            tmp.close()
            static_file = tmp.name
        config = make_config(
            OWNER_PROFILE_ENABLED=True,
            OWNER_SOFT_BLOCK_ENABLED=True,
            STATIC_LINES_FILE=static_file or "",
        )
        app, fake_redis, llm = build_app(config)
        return app, fake_redis, llm

    def _block_owner(self, client):
        response = client.patch(
            "/profiles/owner",
            json={"soft_blocked": True},
            headers={"X-Confirm-Token": UPDATE_OWNER_PROFILE_TOKEN},
        )
        self.assertEqual(response.status_code, 200)

    def test_blocked_turn_makes_no_llm_or_history_calls(self):
        app, fake_redis, llm = self._build(with_static_line=False)
        with TestClient(app) as client:
            self._block_owner(client)
            with ws_connect(client) as ws:
                frames = text_turn(ws, "let me in")
        done = frames[-1]
        self.assertEqual(done["type"], "done")
        self.assertTrue(done["ignored"])
        self.assertEqual(done["reason"], "soft_blocked")
        # Protocol-only silence: no authored line -> no text/segments.
        self.assertNotIn("text", done)
        self.assertNotIn("segments", done)
        # No LLM call, no history writes, no status frames consumed above.
        self.assertEqual(llm.calls, [])
        self.assertNotIn(companion_history_key("owner"), fake_redis.store)

    def test_authored_line_speaks_once_per_cooldown(self):
        app, fake_redis, llm = self._build(with_static_line=True)
        with TestClient(app) as client:
            self._block_owner(client)
            with ws_connect(client) as ws:
                first = text_turn(ws, "let me in")[-1]
                second = text_turn(ws, "please")[-1]
        self.assertEqual(first.get("text"), "Please give me some space.")
        self.assertEqual(first["reason"], "soft_blocked")
        # Second turn inside the cooldown: protocol-only silence again.
        self.assertNotIn("text", second)
        self.assertEqual(second["reason"], "soft_blocked")
        self.assertEqual(llm.calls, [])
        self.assertNotIn(companion_history_key("owner"), fake_redis.store)

    def test_http_message_path_respects_soft_block(self):
        app, fake_redis, llm = self._build(with_static_line=False)
        with TestClient(app) as client:
            self._block_owner(client)
            response = client.post("/message", json={"text": "hi"})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["reason"], "soft_blocked")
        self.assertEqual(llm.calls, [])
        self.assertNotIn(companion_history_key("owner"), fake_redis.store)

    def test_lift_via_patch_restores_turns(self):
        app, fake_redis, llm = self._build(with_static_line=False)
        with TestClient(app) as client:
            self._block_owner(client)
            with ws_connect(client) as ws:
                blocked = text_turn(ws, "let me in")[-1]
                self.assertEqual(blocked["reason"], "soft_blocked")
                response = client.patch(
                "/profiles/owner",
                json={"soft_blocked": False},
                headers={"X-Confirm-Token": UPDATE_OWNER_PROFILE_TOKEN},
                )
                self.assertEqual(response.status_code, 200)
                restored = text_turn(ws, "hi again")[-1]
        self.assertEqual(restored["type"], "done")
        self.assertNotIn("reason", restored)
        self.assertEqual(len(llm.calls), 1)


class OwnerLanguagePinTest(unittest.TestCase):
    """Owner-profile preferred language joins the pin fallback (plan 7.4)."""

    def test_preferred_language_used_when_no_explicit_pin(self):
        config = make_config(OWNER_PROFILE_ENABLED=True)
        app, fake_redis, llm = build_app(config)
        with TestClient(app) as client:
            client.patch(
                "/profiles/owner",
                json={"preferred_language": "es"},
                headers={"X-Confirm-Token": UPDATE_OWNER_PROFILE_TOKEN},
            )
            with ws_connect(client) as ws:
                frames = text_turn(ws, "hello there")
        done = frames[-1]
        self.assertEqual(done["type"], "done")
        # The final-reminder language lock carries the preferred language.
        reminder = llm.calls[0][1][-2]["content"]
        self.assertIn("Reply in language: es", reminder)

    def test_explicit_pin_beats_preferred_language(self):
        config = make_config(OWNER_PROFILE_ENABLED=True)
        app, fake_redis, llm = build_app(config)
        with TestClient(app) as client:
            client.patch(
                "/profiles/owner",
                json={"preferred_language": "es"},
                headers={"X-Confirm-Token": UPDATE_OWNER_PROFILE_TOKEN},
            )
            with ws_connect(client) as ws:
                frames = text_turn(ws, "hello there", language="ja")
        reminder = llm.calls[0][1][-2]["content"]
        self.assertIn("Reply in language: ja", reminder)

    def test_invalid_preferred_language_rejected(self):
        config = make_config(OWNER_PROFILE_ENABLED=True)
        app, fake_redis, llm = build_app(config)
        with TestClient(app) as client:
            response = client.patch(
                "/profiles/owner",
                json={"preferred_language": "de"},
                headers={"X-Confirm-Token": UPDATE_OWNER_PROFILE_TOKEN},
            )
        self.assertEqual(response.status_code, 400)


class BoundaryPenaltyTurnTest(unittest.TestCase):
    def test_boundary_hit_blocks_after_major(self):
        config = make_config(
            OWNER_PROFILE_ENABLED=True,
            OWNER_BOUNDARY_PENALTIES_ENABLED=True,
            OWNER_SOFT_BLOCK_ENABLED=True,
        )
        app, fake_redis, llm = build_app(config)
        with TestClient(app) as client:
            with ws_connect(client) as ws:
                first = text_turn(ws, "I don't care about your boundaries")[-1]
                self.assertEqual(first["type"], "done")  # current turn still answers
                second = text_turn(ws, "hello?")[-1]
        self.assertEqual(second.get("reason"), "soft_blocked")
        self.assertEqual(len(llm.calls), 1)
        stored = fake_redis.strings[owner_profile_key("owner")]
        self.assertNotIn("I don't care", stored)


if __name__ == "__main__":
    unittest.main()
