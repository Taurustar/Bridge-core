"""Milestone 0.5.0 integration tests: work mode over WebSocket.

Acceptance coverage (plan section 31, milestone 0.5.0):
- Companion/work history isolation.
- Current-turn schemas are execution authority.
- Wrong MCP request id ignored.
- Device reconnect disarmed.
- Work remains available under relationship soft block.
- Work/companion deferred hooks remain separated.
"""

from __future__ import annotations

import json
import unittest

from fastapi.testclient import TestClient

from core.app import create_app
from core.cache import RedisCache
from core.config import Config
from core.constants import companion_history_key, deferred_key

from fakes import FakeLLM, FakeRedis, make_config


def build_app(config: Config | None = None, llm: FakeLLM | None = None):
    fake_redis = FakeRedis()
    config = config or make_config()
    llm = llm or FakeLLM()
    app = create_app(config, cache=RedisCache(fake_redis), llm=llm,
                     tailscale_addresses=set())
    return app, fake_redis, llm


def tool_call_script(call_id: str, name: str, arguments: dict,
                     arguments_raw: str | None = None) -> dict:
    return {
        "text": "",
        "tool_calls": [
            {
                "id": call_id,
                "name": name,
                "arguments": arguments_raw
                if arguments_raw is not None
                else json.dumps(arguments),
            }
        ],
    }


def receive_until(ws, frame_type: str) -> dict:
    while True:
        frame = ws.receive_json()
        if frame.get("type") == frame_type:
            return frame


class WorkOverWSTest(unittest.TestCase):
    def _config(self) -> Config:
        return make_config()

    def test_work_turn_with_mcp_tool_round_trip(self):
        context = {
            "mcp_servers": [
                {
                    "name": "fs",
                    "tools": [
                        {
                            "name": "read_file",
                            "description": "Read a file",
                            "input_schema": {
                                "type": "object",
                                "properties": {"path": {"type": "string"}},
                                "required": ["path"],
                            },
                        }
                    ],
                }
            ]
        }
        llm = FakeLLM([
            tool_call_script("c1", "mcp__fs__read_file", {"path": "README.md"}),
            "[EMOTION: confident]\nThe README says hello.",
        ])
        config = self._config()
        app, fake_redis, _ = build_app(config, llm)
        with TestClient(app) as client:
            with client.websocket_connect("/ws/owner") as ws:
                ws.receive_json()  # connected
                ws.send_json({
                    "type": "text", "text": "read the README",
                    "mode": "work", "context": context,
                })
                status = ws.receive_json()
                self.assertEqual(status["type"], "status")
                self.assertEqual(status["status"], "working")
                request = ws.receive_json()
                self.assertEqual(request["type"], "mcp_tool_request")
                self.assertEqual(request["server"], "fs")
                self.assertEqual(request["tool"], "read_file")
                self.assertTrue(request["id"].startswith("mcp_"))
                # Wrong id is ignored; the future stays pending.
                ws.send_json({
                    "type": "mcp_result", "id": "mcp_wrong",
                    "run_id": request["run_id"], "ok": True,
                    "result": None, "truncated": False,
                })
                ws.send_json({
                    "type": "mcp_result", "id": request["id"],
                    "run_id": request["run_id"], "ok": True,
                    "result": {"content": "hello from README"},
                    "truncated": False,
                })
                done = receive_until(ws, "done")
        self.assertEqual(done["mode"], "work")
        self.assertIn("README", done["text"])
        # The tool response reached the model transcript.
        tool_messages = [
            m for m in llm.calls[1][1] if m.get("role") == "tool"
        ]
        self.assertEqual(len(tool_messages), 1)
        self.assertIn("hello from README", tool_messages[0]["content"])

    def test_execution_authority_rejects_uncatalogued_tool(self):
        llm = FakeLLM([
            tool_call_script("c1", "mcp__hidden__secret_tool", {}),
            "[EMOTION: neutral]\nI could not run that.",
        ])
        context = {"mcp_servers": [{"name": "visible", "tools": []}]}
        app, _, _ = build_app(self._config(), llm)
        with TestClient(app) as client:
            with client.websocket_connect("/ws/owner") as ws:
                ws.receive_json()
                ws.send_json({
                    "type": "text", "text": "run the secret tool",
                    "mode": "work", "context": context,
                })
                done = receive_until(ws, "done")
        self.assertEqual(done["type"], "done")
        tool_messages = [
            m for m in llm.calls[1][1] if m.get("role") == "tool"
        ]
        self.assertIn("unknown_tool", tool_messages[0]["content"])

    def test_work_defer_and_catchup_separate_from_companion(self):
        from fakes import FakeSchedule

        config = make_config(
            SCHEDULE_ENABLED=True,
            STATIC_LINES_FILE=_static_lines_file(),
        )
        llm = FakeLLM([
            "[EMOTION: serious]\nWork backlog handled.",
            "[EMOTION: happy]\nHi, I am back!",
        ])
        app, fake_redis, _ = build_app(config, llm)
        with TestClient(app) as client:
            bridge = app.state.bridge
            fake_schedule = FakeSchedule(config, availability="busy")
            bridge.schedule = fake_schedule
            with client.websocket_connect("/ws/owner") as ws:
                ws.receive_json()
                # Companion defer (protocol-only; first-window line exists).
                ws.send_json({"type": "text", "text": "chat later?",
                              "mode": "companion"})
                companion_done = ws.receive_json()
                self.assertEqual(companion_done["reason"], "busy")
                self.assertTrue(companion_done["deferred"])
                # Work defer: same ladder, mode work.
                ws.send_json({"type": "text", "text": "run the build",
                              "mode": "work"})
                work_done = ws.receive_json()
                self.assertEqual(work_done["reason"], "busy")
                self.assertEqual(work_done["mode"], "work")
                self.assertEqual(len(llm.calls), 0)
                # Free the schedule; a heartbeat triggers both catch-ups.
                fake_schedule.availability = "free"
                ws.send_json({"type": "heartbeat", "sequence": 1})
                seen: list[dict] = []
                while True:
                    frame = ws.receive_json()
                    seen.append(frame)
                    dones = [f for f in seen if f.get("type") == "done"
                             and not f.get("ignored")]
                    work_dones = [d for d in dones if d.get("mode") == "work"]
                    companion_dones = [d for d in dones
                                       if d.get("mode") == "companion"]
                    if work_dones and companion_dones:
                        break
                self.assertTrue(work_dones[0]["catchup"])
                self.assertTrue(companion_dones[0]["catchup"])
                self.assertEqual(work_dones[0]["initiated_by"], "character")
        # Both queue entries consumed.
        self.assertNotIn(deferred_key("owner"), fake_redis.strings)
        # Companion catch-up wrote its own channel only...
        companion_rows = fake_redis.store.get(companion_history_key("owner"), [])
        self.assertTrue(
            all(json.loads(row).get("mode", "companion") == "companion"
                for row in companion_rows)
        )
        # ...and the session-less work catch-up (deferred without session
        # metadata) is delivery-only: no session channel was created.
        session_keys = [
            key for key in fake_redis.store
            if key.startswith("core:history:owner:session:")
        ]
        self.assertEqual(len(session_keys), 0)

    def test_device_arm_and_tool_round_trip(self):
        llm = FakeLLM([
            tool_call_script("c1", "device_read",
                             {"path": "/Users/owner/Projects/a.txt"}),
            "[EMOTION: confident]\nRead 3 bytes.",
        ])
        app, _, _ = build_app(make_config(DEVICE_ENABLED=True), llm)
        with TestClient(app) as client:
            with client.websocket_connect("/ws/owner") as ws:
                ws.receive_json()
                ws.send_json({
                    "type": "device_state", "armed": True, "level": "read",
                    "roots": ["/Users/owner/Projects"],
                    "protocol_version": 1,
                })
                ws.send_json({
                    "type": "text", "text": "read a.txt", "mode": "work",
                })
                status = ws.receive_json()
                self.assertEqual(status["status"], "working")
                request = receive_until(ws, "device_tool_request")
                self.assertEqual(request["tool"], "device_read")
                ws.send_json({
                    "type": "device_tool_result", "id": request["id"],
                    "run_id": request["run_id"], "ok": True,
                    "result": {"content": "abc"}, "truncated": False,
                    "duration_ms": 3,
                })
                done = receive_until(ws, "done")
        self.assertEqual(done["mode"], "work")
        tool_messages = [
            m for m in llm.calls[1][1] if m.get("role") == "tool"
        ]
        self.assertIn("abc", tool_messages[0]["content"])

    def test_device_reconnect_starts_disarmed(self):
        llm = FakeLLM(["[EMOTION: neutral]\nNo device available."])
        app, _, _ = build_app(make_config(DEVICE_ENABLED=True), llm)
        with TestClient(app) as client:
            with client.websocket_connect("/ws/owner") as ws:
                ws.receive_json()
                ws.send_json({
                    "type": "device_state", "armed": True, "level": "full",
                    "roots": ["/Users/owner/Projects"],
                    "protocol_version": 1,
                })
            # Reconnect: a brand-new connection; device tools unavailable.
            with client.websocket_connect("/ws/owner") as ws:
                ws.receive_json()
                ws.send_json({"type": "text", "text": "read files",
                              "mode": "work"})
                done = receive_until(ws, "done")
        system = llm.calls[0][1][0]["content"]
        self.assertIn("no armed device", system)
        self.assertEqual(done["type"], "done")

    def test_work_under_soft_block_over_ws(self):
        from core.constants import UPDATE_OWNER_PROFILE_TOKEN

        config = make_config(
            OWNER_PROFILE_ENABLED=True,
            OWNER_SOFT_BLOCK_ENABLED=True,
        )
        llm = FakeLLM(["[EMOTION: serious]\nWork proceeds."])
        app, _, _ = build_app(config, llm)
        with TestClient(app) as client:
            client.patch(
                "/profiles/owner",
                json={"soft_blocked": True},
                headers={"X-Confirm-Token": UPDATE_OWNER_PROFILE_TOKEN},
            )
            with client.websocket_connect("/ws/owner") as ws:
                ws.receive_json()
                ws.send_json({"type": "text", "text": "fix the bug",
                              "mode": "work"})
                done = receive_until(ws, "done")
        self.assertEqual(done["mode"], "work")
        self.assertEqual(done["type"], "done")


def _static_lines_file() -> str:
    import tempfile

    tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    json.dump(
        {
            "version": 1,
            "en": {"busy": "One moment.", "unavailable": "",
                   "soft_block": "", "stt_empty": ""},
            "es": {"busy": "", "unavailable": "", "soft_block": "",
                   "stt_empty": ""},
            "ja": {"busy": "", "unavailable": "", "soft_block": "",
                   "stt_empty": ""},
        },
        tmp,
    )
    tmp.close()
    return tmp.name


if __name__ == "__main__":
    unittest.main()
