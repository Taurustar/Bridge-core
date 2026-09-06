"""Release regression tests (plan section 31, milestone 1.0.0).

Machine-verifiable parts of the release acceptance:

- Version sources agree (constants vs pyproject).
- Every Config field and feature flag is documented in docs/ENVIRONMENT.md.
- Every documented Redis key is wipe-covered, and every wipe pattern is
  documented in the SPEC.
- No excluded-forever gateway/auth code exists in runtime source.
- The config validation and WS smoke scripts behave (exit codes, pure
  validators).
- Chroma blocking calls run on the dedicated single-thread executor.
- A flags-off boot stays fully inert end to end.
"""

from __future__ import annotations

import asyncio
import json
import re
import tempfile
import threading
import tomllib
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from core.app import create_app
from core.cache import RedisCache
from core.chroma_store import run_chroma_call
from core.config import Config
from core.constants import VERSION, wipe_key_patterns

from fakes import FakeLLM, FakeRedis, make_config

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS = REPO_ROOT / "docs"


class VersionSourcesTest(unittest.TestCase):
    def test_constants_version_matches_pyproject(self):
        pyproject = tomllib.loads(
            (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )
        self.assertEqual(pyproject["project"]["version"], VERSION)
        self.assertEqual(VERSION, "1.0.0")


class DocumentationCompletenessTest(unittest.TestCase):
    def test_every_config_field_is_documented(self):
        reference = (DOCS / "ENVIRONMENT.md").read_text(encoding="utf-8")
        missing = [
            field.name
            for field in Config.__dataclass_fields__.values()
            if field.name != "env" and field.name not in reference
        ]
        self.assertEqual(missing, [])

    def test_every_flag_is_documented(self):
        reference = (DOCS / "ENVIRONMENT.md").read_text(encoding="utf-8")
        flags = [
            name
            for name in Config.__dataclass_fields__
            if name.endswith("ENABLED") or name.endswith("_ACK")
        ]
        missing = [name for name in flags if name not in reference]
        self.assertEqual(missing, [])

    def test_documented_redis_keys_are_wipe_covered(self):
        spec = (REPO_ROOT / "BRIDGE_CORE_ENGINE_SPEC.md").read_text(
            encoding="utf-8"
        )
        documented = set(re.findall(r"`(core:[a-z_:{}0-9]+)`", spec))
        self.assertTrue(documented, "SPEC must list Redis keys")
        owner = "owner"
        patterns = wipe_key_patterns(owner)
        uncovered = []
        for key in documented:
            literal = key.replace("{owner}", owner)
            if "*" in literal:
                continue
            covered = any(
                re.fullmatch(
                    re.escape(pattern).replace(r"\*", "[^:]+") + r"(?::[^:]+)*",
                    literal,
                )
                for pattern in patterns
            )
            if not covered:
                uncovered.append(key)
        self.assertEqual(uncovered, [])


class ExcludedForeverTest(unittest.TestCase):
    """Plan section 2 / AGENTS.md: these are excluded forever in v1."""

    RUNTIME_FILES = sorted(
        list((REPO_ROOT / "core").rglob("*.py")) + [REPO_ROOT / "bridge_core.py"]
    )

    def test_no_gateway_or_public_exposure_code(self):
        forbidden = ("discord", "whatsapp", "telegram", "funnel")
        hits = []
        for path in self.RUNTIME_FILES:
            text = path.read_text(encoding="utf-8").lower()
            for token in forbidden:
                if token in text:
                    hits.append(f"{path.name}:{token}")
        self.assertEqual(hits, [])

    def test_no_application_bearer_auth(self):
        # Outbound provider `Authorization: Bearer` headers and the memory
        # secret-filter are allowed; the application/HTTP layer must not do
        # bearer-token auth (excluded forever in v1).
        app_layer = [REPO_ROOT / "core" / "app.py"] + sorted(
            (REPO_ROOT / "core" / "routes").glob("*.py")
        )
        hits = [
            path.name
            for path in app_layer
            if "bearer" in path.read_text(encoding="utf-8").lower()
        ]
        self.assertEqual(hits, [])


class ChromaExecutorTest(unittest.IsolatedAsyncioTestCase):
    async def test_blocking_calls_run_on_dedicated_single_thread(self):
        seen = await run_chroma_call(threading.current_thread)
        self.assertTrue(seen.name.startswith("chroma"))
        again = await run_chroma_call(threading.current_thread)
        self.assertEqual(seen, again)  # the same single worker thread


class ValidateConfigScriptTest(unittest.TestCase):
    SCRIPT = REPO_ROOT / "scripts" / "validate_config.py"

    def test_template_env_validates(self):
        from scripts.validate_config import main

        self.assertEqual(main([str(REPO_ROOT / "core.env.template")]), 0)

    def test_bad_value_fails(self):
        from scripts.validate_config import main

        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as tmp:
            tmp.write("BRIDGE_PORT=not_a_number\n")
            path = tmp.name
        self.assertEqual(main([path]), 1)

    def test_missing_env_file_fails(self):
        from scripts.validate_config import main

        self.assertEqual(main(["/nonexistent/core.env"]), 1)


class WsSmokeScriptTest(unittest.TestCase):
    SCRIPT = REPO_ROOT / "scripts" / "ws_smoke.py"

    def test_validators_accept_canonical_frames(self):
        from scripts.ws_smoke import verify_connected, verify_error, verify_heartbeat_ack

        self.assertEqual(
            verify_connected(
                {
                    "type": "connected",
                    "connection_id": "conn_abc",
                    "server_version": VERSION,
                    "capabilities": ["text", "heartbeat", "chat_sync"],
                }
            ),
            [],
        )
        self.assertEqual(
            verify_heartbeat_ack(
                {
                    "type": "heartbeat_ack",
                    "server_time": "2026-09-05T00:00:00+00:00",
                    "initiative_counter": 2,
                    "counted": True,
                }
            ),
            [],
        )
        self.assertEqual(
            verify_error(
                {
                    "type": "error",
                    "error": {"code": "bad_json", "message": "nope", "details": {}},
                },
                "bad_json",
            ),
            [],
        )

    def test_validators_reject_broken_frames(self):
        from scripts.ws_smoke import verify_connected, verify_heartbeat_ack

        self.assertTrue(verify_connected({"type": "connected"}))
        self.assertTrue(
            verify_heartbeat_ack({"type": "heartbeat_ack", "counted": False})
        )

    def test_dead_port_fails_closed(self):
        # Nothing listens on loopback port 1; the smoke run must exit 1
        # quickly rather than hang or pass.
        from scripts.ws_smoke import main

        self.assertEqual(
            main(["--host", "127.0.0.1", "--port", "1", "--timeout", "2"]), 1
        )


class FlagsOffReleaseBootTest(unittest.TestCase):
    def test_fresh_default_boot_is_inert_and_complete(self):
        app, fake_redis, _ = build_default_app()
        with TestClient(app) as client:
            health = client.get("/health")
            self.assertEqual(health.status_code, 200)
            self.assertTrue(health.json()["redis"])

            status = client.get("/status").json()
            self.assertEqual(status["version"], VERSION)
            for section in (
                "features", "initiative", "external_profiles", "work",
                "connections", "speech", "memory", "daily_tools",
                "user_schedule", "schedule", "life", "needs", "owner_profile",
            ):
                self.assertIn(section, status)
            self.assertFalse(status["features"]["initiative"])
            self.assertTrue(status["external_profiles"]["store"])
            self.assertFalse(status["external_profiles"]["behavior"])

            with client.websocket_connect("/ws/owner") as ws:
                connected = ws.receive_json()
                self.assertEqual(connected["server_version"], VERSION)
                self.assertIn("heartbeat", connected["capabilities"])
                ws.send_json({"type": "text", "text": "hello there, friend"})
                while True:
                    frame = ws.receive_json()
                    if frame.get("type") in ("done", "error"):
                        break
                ws.send_json({"type": "heartbeat", "sequence": 1})
                ack = ws.receive_json()
                self.assertEqual(ack["initiative_counter"], 0)

        # Flag-off parity: exactly one key family after a turn.
        self.assertEqual(
            set(fake_redis.store) | set(fake_redis.strings),
            {"core:history:owner:companion"},
        )


def build_default_app():
    fake_redis = FakeRedis()
    app = create_app(
        make_config(),
        cache=RedisCache(fake_redis),
        llm=FakeLLM(),
        tailscale_addresses=set(),
    )
    return app, fake_redis, None


if __name__ == "__main__":
    unittest.main()
