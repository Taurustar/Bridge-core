"""Device daemon tests (plan section 26)."""

from __future__ import annotations

import unittest

from core.cache import RedisCache
from core.constants import device_audit_key
from core.device import (
    DeviceManager,
    contains_secret_pattern,
    validate_device_arguments,
)
from core.connections import Connection, ConnectionManager

from fakes import FakeRedis, make_config

import json


def make_manager(**config_overrides) -> tuple[DeviceManager, ConnectionManager, FakeRedis]:
    connections = ConnectionManager()
    fake = FakeRedis()
    config = make_config(
        DEVICE_ENABLED=True, DEVICE_TOOL_TIMEOUT=1, **config_overrides
    )
    return DeviceManager(config, RedisCache(fake), connections), connections, fake


def armed_conn(connections: ConnectionManager, level: str = "read") -> Connection:
    conn = connections.connect(FakeWS(), "owner")
    conn.device_armed = True
    conn.device_level = level
    conn.device_roots = ["/Users/owner/Projects"]
    conn.device_protocol_version = 1
    return conn


class FakeWS:
    async def send_json(self, frame: dict) -> None:
        pass


class ApplyStateTest(unittest.IsolatedAsyncioTestCase):
    async def test_arm_disarm_and_validation(self):
        manager, connections, _ = make_manager()
        conn = connections.connect(FakeWS(), "owner")
        self.assertTrue(
            manager.apply_state(
                conn, {"armed": True, "level": "full", "roots": ["/tmp"],
                       "protocol_version": 1}
            ) is None
        )
        self.assertTrue(conn.device_armed)
        self.assertEqual(conn.device_level, "full")
        # Reconnect starts disarmed: a fresh connection has defaults.
        fresh = connections.connect(FakeWS(), "owner")
        self.assertFalse(fresh.device_armed)
        # Invalid states rejected.
        self.assertEqual(
            manager.apply_state(conn, {"armed": "yes"}), "invalid_device_state"
        )
        self.assertEqual(
            manager.apply_state(conn, {"armed": True, "level": "admin"}),
            "invalid_device_state",
        )
        self.assertEqual(
            manager.apply_state(conn, {"armed": True, "protocol_version": 2}),
            "invalid_device_state",
        )
        # Disarm clears state.
        manager.apply_state(conn, {"armed": False})
        self.assertFalse(conn.device_armed)
        self.assertEqual(conn.device_roots, [])


class FenceTest(unittest.TestCase):
    def test_unknown_fields_rejected(self):
        clean, error = validate_device_arguments(
            "device_read", {"path": "/x", "evil": 1}, "read",
            ["/Users/owner/Projects"], 30000,
        )
        self.assertIsNone(clean)
        self.assertEqual(error, "unknown_arguments")

    def test_level_gating(self):
        clean, error = validate_device_arguments(
            "device_shell", {"cwd": "/Users/owner/Projects", "argv": ["ls"]},
            "read", ["/Users/owner/Projects"], 30000,
        )
        self.assertEqual(error, "device_level_required")
        clean, error = validate_device_arguments(
            "device_write",
            {"path": "/Users/owner/Projects/x.txt", "content": "hi",
             "encoding": "utf8"},
            "read", ["/Users/owner/Projects"], 30000,
        )
        self.assertEqual(error, "device_level_required")

    def test_secret_paths_rejected(self):
        for path in (
            "/Users/owner/Projects/.env",
            "/Users/owner/.ssh/id_rsa",
            "/Users/owner/Projects/credentials.json",
        ):
            clean, error = validate_device_arguments(
                "device_read", {"path": path}, "read",
                ["/Users/owner/Projects"], 30000,
            )
            self.assertEqual(error, "secret_path_rejected", path)

    def test_roots_containment(self):
        clean, error = validate_device_arguments(
            "device_read", {"path": "/etc/passwd"}, "read",
            ["/Users/owner/Projects"], 30000,
        )
        self.assertEqual(error, "path_outside_roots")
        clean, error = validate_device_arguments(
            "device_read", {"path": "/Users/owner/Projects/src/app.py"},
            "read", ["/Users/owner/Projects"], 30000,
        )
        self.assertIsNone(error)

    def test_shell_rules(self):
        base = {"cwd": "/Users/owner/Projects"}
        clean, error = validate_device_arguments(
            "device_shell", {**base, "argv": ["ls"], "shell_command": "ls"},
            "full", ["/Users/owner/Projects"], 30000,
        )
        self.assertEqual(error, "invalid_arguments")
        clean, error = validate_device_arguments(
            "device_shell", {**base}, "full", ["/Users/owner/Projects"], 30000,
        )
        self.assertEqual(error, "invalid_arguments")
        clean, error = validate_device_arguments(
            "device_shell", {**base, "argv": ["cat", "/x/.env"]},
            "full", ["/Users/owner/Projects"], 30000,
        )
        self.assertEqual(error, "secret_path_rejected")
        clean, error = validate_device_arguments(
            "device_shell",
            {**base, "shell_command": "echo hi", "timeout_seconds": 99999},
            "full", ["/Users/owner/Projects"], 30000,
        )
        self.assertEqual(error, "invalid_arguments")
        # Environment filtered to the allowlist.
        clean, error = validate_device_arguments(
            "device_shell",
            {**base, "argv": ["ls"],
             "environment": {"PATH": "/usr/bin", "EVIL": "x"}},
            "full", ["/Users/owner/Projects"], 30000,
        )
        self.assertIsNone(error)
        self.assertEqual(clean["environment"], {"PATH": "/usr/bin"})

    def test_write_rules(self):
        clean, error = validate_device_arguments(
            "device_write",
            {"path": "/Users/owner/Projects/x.txt", "content": "hi"},
            "full", ["/Users/owner/Projects"], 30000,
        )
        self.assertEqual(error, "invalid_arguments")  # encoding required
        clean, error = validate_device_arguments(
            "device_write",
            {"path": "/Users/owner/Projects/x.txt", "content": "x" * 40000,
             "encoding": "utf8"},
            "full", ["/Users/owner/Projects"], 30000,
        )
        self.assertEqual(error, "invalid_arguments")  # over cap

    def test_contains_secret_pattern(self):
        self.assertTrue(contains_secret_pattern("/a/.SSH/config"))
        self.assertFalse(contains_secret_pattern("/a/src/main.py"))


class RoutingTest(unittest.IsolatedAsyncioTestCase):
    async def test_disarmed_means_unavailable(self):
        manager, connections, _ = make_manager()
        connections.connect(FakeWS(), "owner")  # present but not armed
        result = await manager.call(
            "owner", run_id="run_1", tool="device_read",
            arguments={"path": "/Users/owner/Projects/x"}, turn_calls=[],
        )
        self.assertEqual(result["error"], "device_unavailable")

    async def test_result_correlation_and_audit(self):
        manager, connections, fake = make_manager()
        conn = armed_conn(connections, "read")

        class RecordingWS:
            def __init__(self):
                self.frames = []

            async def send_json(self, frame):
                self.frames.append(frame)

        recording = RecordingWS()
        conn.websocket = recording

        import asyncio

        async def responder():
            await asyncio.sleep(0.01)
            frame = recording.frames[-1]
            self.assertEqual(frame["type"], "device_tool_request")
            handled = manager.handle_result(
                conn,
                {
                    "type": "device_tool_result",
                    "id": frame["id"],
                    "run_id": frame["run_id"],
                    "ok": True,
                    "result": {"content": "data"},
                    "truncated": False,
                    "duration_ms": 5,
                },
            )
            self.assertTrue(handled)

        task = asyncio.create_task(responder())
        result = await manager.call(
            "owner", run_id="run_1", tool="device_read",
            arguments={"path": "/Users/owner/Projects/x.txt"}, turn_calls=[],
        )
        await task
        self.assertTrue(result["ok"])
        entries = [json.loads(row) for row in fake.store[device_audit_key("owner")]]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["tool"], "device_read")
        self.assertIn("x.txt", entries[0]["preview"])
        # Audit stores metadata only — never file content.
        self.assertNotIn("data", entries[0]["preview"])

    async def test_per_turn_cap(self):
        manager, connections, _ = make_manager(DEVICE_PER_TURN_CALL_CAP=1)
        armed_conn(connections, "read")
        first = await manager.call(
            "owner", run_id="run_1", tool="device_stat",
            arguments={"path": "/Users/owner/Projects/x"}, turn_calls=[],
        )
        # The fake client never answers; both paths end unavailable/timeout —
        # the cap is what we assert via turn_calls saturation.
        turn_calls = ["device_stat"] * 20
        second = await manager.call(
            "owner", run_id="run_2", tool="device_stat",
            arguments={"path": "/Users/owner/Projects/x"}, turn_calls=turn_calls,
        )
        self.assertTrue(
            first["error"] in ("timeout", "device_unavailable")
            or first["ok"]
        )
        self.assertEqual(second["error"], "device_call_cap")


if __name__ == "__main__":
    unittest.main()
