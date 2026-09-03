"""MCP registry and proxy tests (plan sections 10.11, 25.3)."""

from __future__ import annotations

import asyncio
import unittest

from core.cache import RedisCache
from core.mcp import (
    MCPProxy,
    build_tool_schemas,
    known_tool_names,
    parse_servers,
    parse_tool_name,
)
from core.connections import ConnectionManager

from fakes import FakeRedis, make_config


class RegistryTest(unittest.TestCase):
    def test_parse_tool_name(self):
        self.assertEqual(parse_tool_name("mcp__fs__read_file"), ("fs", "read_file"))
        self.assertEqual(parse_tool_name("mcp__a__b__c"), ("a", "b__c"))
        self.assertIsNone(parse_tool_name("device_read"))
        self.assertIsNone(parse_tool_name("mcp__onlyserver"))

    def test_schema_rich_servers_get_exact_schemas(self):
        context = {
            "mcp_servers": [
                {
                    "name": "fs",
                    "tools": [
                        {
                            "name": "read_file",
                            "description": "Read a file.",
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
        schemas = build_tool_schemas(parse_servers(context))
        self.assertEqual(len(schemas), 1)
        self.assertEqual(schemas[0]["function"]["name"], "mcp__fs__read_file")
        self.assertEqual(
            schemas[0]["function"]["parameters"]["required"], ["path"]
        )

    def test_legacy_server_gets_generic_wrapper(self):
        context = {"mcp_servers": [{"name": "legacy"}]}
        schemas = build_tool_schemas(parse_servers(context))
        self.assertEqual(schemas[0]["function"]["name"], "mcp__legacy__call")
        self.assertIn(
            "mcp__legacy__call", known_tool_names(parse_servers(context))
        )

    def test_context_is_execution_authority(self):
        # Servers not in the current context never appear.
        self.assertEqual(parse_servers({"mcp_servers": []}), [])
        self.assertEqual(parse_servers(None), [])
        self.assertEqual(parse_servers({"mcp_servers": "junk"}), [])


class FakeWS:
    def __init__(self) -> None:
        self.frames: list[dict] = []
        self.sent_future: asyncio.Future | None = None

    async def send_json(self, frame: dict) -> None:
        self.frames.append(frame)
        if self.sent_future is not None:
            self.sent_future.set_result(None)


class ProxyTest(unittest.IsolatedAsyncioTestCase):
    def _proxy(self) -> tuple[MCPProxy, ConnectionManager, Connection]:
        connections = ConnectionManager()
        fake = FakeRedis()
        proxy = MCPProxy(make_config(MCP_TOOL_TIMEOUT=2), RedisCache(fake), connections)
        conn = connections.connect(FakeWS(), "owner")
        return proxy, connections, conn

    async def test_call_and_result_correlation(self):
        proxy, connections, conn = self._proxy()

        async def responder():
            await asyncio.sleep(0.01)
            request_frame = conn.websocket.frames[-1]
            self.assertEqual(request_frame["type"], "mcp_tool_request")
            self.assertTrue(request_frame["id"].startswith("mcp_"))
            self.assertEqual(request_frame["server"], "fs")
            result = proxy.handle_result(
                conn,
                {
                    "type": "mcp_result",
                    "id": request_frame["id"],
                    "run_id": request_frame["run_id"],
                    "ok": True,
                    "result": {"content": "file body"},
                    "truncated": False,
                },
            )
            self.assertTrue(result)

        task = asyncio.create_task(responder())
        tool_result = await proxy.call(
            "owner",
            run_id="run_1",
            source_conn=conn,
            server="fs",
            tool="read_file",
            arguments={"path": "README.md"},
            turn_calls=[],
        )
        await task
        self.assertTrue(tool_result["ok"])
        self.assertEqual(tool_result["result"]["content"], "file body")
        self.assertEqual(connections.pending_count(), 0)

    async def test_wrong_request_id_ignored(self):
        proxy, connections, conn = self._proxy()

        async def responder():
            await asyncio.sleep(0.02)
            frame = conn.websocket.frames[-1]
            # Wrong id: must be ignored and not resolve the future.
            handled = proxy.handle_result(
                conn,
                {"type": "mcp_result", "id": "mcp_wrong", "run_id": frame["run_id"],
                 "ok": True, "result": None, "truncated": False},
            )
            self.assertFalse(handled)
            # Right id, wrong run id: also ignored.
            handled = proxy.handle_result(
                conn,
                {"type": "mcp_result", "id": frame["id"], "run_id": "run_other",
                 "ok": True, "result": None, "truncated": False},
            )
            self.assertFalse(handled)

        task = asyncio.create_task(responder())
        tool_result = await proxy.call(
            "owner", run_id="run_1", source_conn=conn, server="fs",
            tool="read_file", arguments={}, turn_calls=[],
        )
        await task
        self.assertFalse(tool_result["ok"])
        self.assertEqual(tool_result["error"], "timeout")

    async def test_timeout_returns_structured_failure(self):
        proxy, _, conn = self._proxy()
        tool_result = await proxy.call(
            "owner", run_id="run_1", source_conn=conn, server="fs",
            tool="slow_tool", arguments={}, turn_calls=[],
        )
        self.assertFalse(tool_result["ok"])
        self.assertEqual(tool_result["error"], "timeout")

    async def test_http_origin_never_executes(self):
        proxy, _, _ = self._proxy()
        tool_result = await proxy.call(
            "owner", run_id="run_1", source_conn=None, server="fs",
            tool="read_file", arguments={}, turn_calls=[],
        )
        self.assertEqual(tool_result["error"], "tools_require_websocket")

    async def test_redis_backup_deleted_after_resolution(self):
        proxy, _, conn = self._proxy()
        cache = proxy.cache

        async def responder():
            await asyncio.sleep(0.01)
            frame = conn.websocket.frames[-1]
            proxy.handle_result(
                conn,
                {"type": "mcp_result", "id": frame["id"], "run_id": frame["run_id"],
                 "ok": True, "result": None, "truncated": False},
            )

        task = asyncio.create_task(responder())
        await proxy.call(
            "owner", run_id="run_1", source_conn=conn, server="fs",
            tool="read_file", arguments={}, turn_calls=[],
        )
        await task
        keys = await cache.keys("core:mcp_response:*")
        self.assertEqual(keys, [])


if __name__ == "__main__":
    unittest.main()
