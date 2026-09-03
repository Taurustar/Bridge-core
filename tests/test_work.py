"""Work mode tests: agent loop, verification, pause/resume, isolation,
and the relationship soft-block bypass (plan sections 25, 12 step 5)."""

from __future__ import annotations

import asyncio
import json
import unittest

from core.agent_runs import run_agent_loop
from core import history as hist
from core.bridge import Bridge
from core.constants import agent_run_key, companion_history_key
from core.history import session_history_key
from core.work_tools import WorkToolRegistry

from fakes import FakeLLM, FakeRedis, make_cache, make_config


def make_bridge(**config_overrides) -> tuple[Bridge, FakeRedis]:
    config = make_config(**config_overrides)
    cache, fake = make_cache()
    return Bridge(config, cache, llm=FakeLLM()), fake


def tool_call(call_id: str, name: str, arguments: dict) -> dict:
    return {
        "text": "",
        "tool_calls": [
            {"id": call_id, "name": name,
             "arguments": json.dumps(arguments)}
        ],
    }


class DeadConn:
    connection_id = "conn_dead"

    async def send_json(self, frame: dict) -> bool:
        return False


class AgentLoopTest(unittest.IsolatedAsyncioTestCase):
    def _registry(self, context=None) -> WorkToolRegistry:
        return WorkToolRegistry.build(
            context=context or {
                "mcp_servers": [
                    {"name": "fs", "tools": [
                        {"name": "read_file", "input_schema":
                         {"type": "object", "properties": {}}},
                        {"name": "write_file", "input_schema":
                         {"type": "object", "properties": {}}},
                    ]}
                ]
            },
            device_level="",
        )

    async def test_tool_loop_then_final_reply(self):
        _, fake = make_bridge()
        llm = FakeLLM([
            tool_call("c1", "mcp__fs__read_file", {"path": "a.py"}),
            {"text": "[EMOTION: confident]\nHere is what I found.", "tool_calls": None},
        ])
        registry = self._registry()
        executed: list[str] = []

        async def executor(name, arguments):
            executed.append(name)
            return {"ok": True, "result": {"content": "data"},
                    "truncated": False}

        loop = await run_agent_loop(
            llm, messages=[{"role": "user", "content": "read it"}],
            registry=registry, executor=executor,
            max_iterations=5, verification_enabled=False,
            verification_retries=0,
        )
        self.assertEqual(executed, ["mcp__fs__read_file"])
        self.assertEqual(loop.text, "[EMOTION: confident]\nHere is what I found.")
        self.assertEqual(loop.tool_calls_made, 1)
        self.assertEqual(loop.iterations, 2)
        # Tool role response entered the transcript.
        tool_messages = [m for m in loop.transcript if m.get("role") == "tool"]
        self.assertEqual(len(tool_messages), 1)
        self.assertEqual(tool_messages[0]["tool_call_id"], "c1")

    async def test_unknown_tool_never_executes(self):
        llm = FakeLLM([
            tool_call("c1", "mcp__other__tool", {}),
            "[EMOTION: neutral]\nThat tool is not available.",
        ])
        registry = self._registry()

        async def executor(name, arguments):
            raise AssertionError("executor must not be called")

        loop = await run_agent_loop(
            llm, messages=[{"role": "user", "content": "go"}],
            registry=registry, executor=executor,
            max_iterations=3, verification_enabled=False,
            verification_retries=0,
        )
        self.assertEqual(loop.evidence[0]["ok"], False)
        self.assertFalse(loop.evidence[0]["tool"].startswith("mcp__fs__"))

    async def test_iteration_limit_forces_no_tools_synthesis(self):
        llm = FakeLLM([
            tool_call("c1", "mcp__fs__read_file", {}),
            tool_call("c2", "mcp__fs__read_file", {}),
            "[EMOTION: neutral]\nFinal answer.",
        ])
        registry = self._registry()

        async def executor(name, arguments):
            return {"ok": True, "result": None, "truncated": False}

        loop = await run_agent_loop(
            llm, messages=[{"role": "user", "content": "go"}],
            registry=registry, executor=executor,
            max_iterations=2, verification_enabled=False,
            verification_retries=0,
        )
        self.assertTrue(loop.hit_iteration_limit)
        self.assertEqual(loop.text, "[EMOTION: neutral]\nFinal answer.")

    async def test_unverified_write_triggers_bounded_follow_up(self):
        llm = FakeLLM([
            tool_call("c1", "mcp__fs__write_file", {"path": "a.py"}),
            "[EMOTION: neutral]\nI did not verify it.",
            "[EMOTION: neutral]\nStill unverified; noted honestly.",
        ])
        registry = self._registry()

        async def executor(name, arguments):
            return {"ok": True, "result": None, "truncated": False}

        loop = await run_agent_loop(
            llm, messages=[{"role": "user", "content": "write it"}],
            registry=registry, executor=executor,
            max_iterations=3, verification_enabled=True,
            verification_retries=1,
        )
        # The forced follow-up system note was appended and the write
        # recorded as unverified (no read-back followed it).
        self.assertTrue(
            any("VERIFICATION REQUIRED" in str(m.get("content"))
                for m in loop.transcript if m.get("role") == "system")
        )
        self.assertEqual(len(loop.unverified_writes), 1)
        self.assertEqual(loop.unverified_writes[0]["tool"],
                         "mcp__fs__write_file")

    async def test_write_verified_by_later_read(self):
        llm = FakeLLM([
            tool_call("c1", "mcp__fs__write_file", {"path": "a.py"}),
            tool_call("c2", "mcp__fs__read_file", {"path": "a.py"}),
            "[EMOTION: confident]\nVerified.",
        ])
        registry = self._registry()

        async def executor(name, arguments):
            return {"ok": True, "result": None, "truncated": False}

        loop = await run_agent_loop(
            llm, messages=[{"role": "user", "content": "go"}],
            registry=registry, executor=executor,
            max_iterations=5, verification_enabled=True,
            verification_retries=2,
        )
        self.assertEqual(loop.unverified_writes, [])


class WorkTurnTest(unittest.IsolatedAsyncioTestCase):
    async def test_http_work_turn_creates_session_and_isolates_history(self):
        bridge, fake = make_bridge()
        llm = FakeLLM(["[EMOTION: confident]\nDone: analysis complete."])
        bridge.llm = llm
        done = await bridge.run_work_turn(
            text="analyze the flaky test", language="en", source_conn=None,
        )
        self.assertEqual(done["type"], "done")
        self.assertEqual(done["mode"], "work")
        self.assertTrue(done["session_id"].startswith("ses_"))
        session_key = session_history_key("owner", done["session_id"])
        rows = await hist.load_rows_from(bridge.cache, session_key)
        self.assertEqual([row["role"] for row in rows], ["user", "assistant"])
        self.assertEqual(rows[0]["mode"], "work")
        # Companion history untouched.
        self.assertNotIn(companion_history_key("owner"), fake.store)
        # Session registered and checkpoint completed.
        session = await bridge.sessions.get("owner", done["session_id"])
        self.assertEqual(session["last_run_id"], done["run_id"])
        record = json.loads(fake.strings[agent_run_key("owner", done["session_id"])])
        self.assertEqual(record["state"], "completed")

    async def test_work_stays_available_under_soft_block(self):
        import time as _time

        config = make_config(
            OWNER_PROFILE_ENABLED=True,
            OWNER_SOFT_BLOCK_ENABLED=True,
        )
        cache, fake = make_cache()
        bridge = Bridge(config, cache, llm=FakeLLM())
        from core.owner_profile import default_profile

        profile = default_profile(config)
        profile.update(
            {
                "soft_blocked": True,
                "soft_block_reason": "boundary_threshold",
                "soft_blocked_until_ts": _time.time() + 3600,
            }
        )
        await bridge.owner_profile.upsert("owner", profile, 1)
        # Companion turn is soft-blocked...
        companion_done = await bridge.run_companion_turn(
            text="let me in", language="en", source_conn=None,
        )
        self.assertEqual(companion_done.get("reason"), "soft_blocked")
        # ...but work proceeds (plan 12 step 5).
        llm = FakeLLM(["[EMOTION: serious]\nWork continues."])
        bridge.llm = llm
        done = await bridge.run_work_turn(
            text="ship the report", language="en", source_conn=None,
        )
        self.assertEqual(done["type"], "done")
        self.assertEqual(done["mode"], "work")
        self.assertEqual(len(llm.calls), 1)

    async def test_pause_then_resume_with_explicit_ids(self):
        bridge, fake = make_bridge()
        bridge.llm = FakeLLM(["[STATUS: question]\nWhich branch should I target?"])
        paused = await bridge.run_work_turn(
            text="merge the PR", language="en", source_conn=None,
        )
        self.assertEqual(paused["type"], "paused")
        self.assertEqual(paused["status"], "question")
        session_id = paused["session_id"]
        run_id = paused["run_id"]
        # Pending pause and paused checkpoint exist.
        self.assertIsNotNone(
            await bridge._read_pending_pause("owner", session_id, run_id)
        )
        record = json.loads(fake.strings[agent_run_key("owner", session_id)])
        self.assertEqual(record["state"], "paused")
        # Resume from the same session + run id with the owner's answer.
        bridge.llm = FakeLLM(["[EMOTION: confident]\nMerged to main."])
        done = await bridge.run_work_turn(
            text="main branch", language="en", source_conn=None,
            session_id=session_id, explicit_run_id=run_id,
        )
        self.assertEqual(done["type"], "done")
        record = json.loads(fake.strings[agent_run_key("owner", session_id)])
        self.assertEqual(record["state"], "completed")
        self.assertIsNone(
            await bridge._read_pending_pause("owner", session_id, run_id)
        )

    async def test_disconnect_marks_run_interrupted(self):
        bridge, fake = make_bridge()
        bridge.llm = FakeLLM(["[STATUS: request_permission]\nMay I push?"])
        paused = await bridge.run_work_turn(
            text="push", language="en", source_conn=None,
        )
        session_id = paused["session_id"]
        run_id = paused["run_id"]
        # Simulate the disconnect path (connection-bound pauses).
        bridge._pending_pauses[run_id] = {"session_id": session_id,
                                          "connection_id": "conn_x"}
        await bridge._mark_run_interrupted("owner", session_id, run_id)
        record = json.loads(fake.strings[agent_run_key("owner", session_id)])
        self.assertEqual(record["state"], "interrupted")
        # The durable checkpoint stays recoverable for explicit resume.
        self.assertIsNotNone(
            await bridge._read_pending_pause("owner", session_id, run_id)
        )


if __name__ == "__main__":
    unittest.main()
