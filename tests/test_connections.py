"""ConnectionManager unit tests (plan section 11)."""

from __future__ import annotations

import asyncio
import unittest

from core.connections import ConnectionManager


class FakeWebSocket:
    def __init__(self, fail: bool = False) -> None:
        self.sent: list[dict] = []
        self.fail = fail

    async def send_json(self, frame: dict) -> None:
        if self.fail:
            raise RuntimeError("socket closed")
        self.sent.append(frame)


class ConnectionManagerTest(unittest.IsolatedAsyncioTestCase):
    async def test_multi_device_and_disconnect_removes_exactly_one(self):
        manager = ConnectionManager()
        a = manager.connect(FakeWebSocket(), "owner")
        b = manager.connect(FakeWebSocket(), "owner")
        self.assertEqual(len(manager.connections_for("owner")), 2)

        manager.disconnect(a.connection_id)
        remaining = manager.connections_for("owner")
        self.assertEqual([c.connection_id for c in remaining], [b.connection_id])
        # disconnect is idempotent for unknown ids
        manager.disconnect("conn_gone")
        self.assertEqual(len(manager.connections_for("owner")), 1)

    async def test_fan_out_excludes_source_and_counts_delivery(self):
        manager = ConnectionManager()
        a = manager.connect(FakeWebSocket(), "owner")
        b = manager.connect(FakeWebSocket(), "owner")
        c = manager.connect(FakeWebSocket(fail=True), "owner")
        delivered = await manager.fan_out(
            "owner", {"type": "chat_sync"}, exclude_connection_id=a.connection_id
        )
        self.assertEqual(delivered, 1)
        self.assertEqual(a.websocket.sent, [])
        self.assertEqual(len(b.websocket.sent), 1)

    async def test_turn_lock_shared_per_owner(self):
        manager = ConnectionManager()
        self.assertIs(manager.turn_lock("owner"), manager.turn_lock("owner"))
        self.assertIsNot(manager.turn_lock("owner"), manager.turn_lock("other"))

    async def test_disconnect_cleans_pending_futures(self):
        manager = ConnectionManager()
        conn = manager.connect(FakeWebSocket(), "owner")
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        manager.add_pending("mcp_1", future, {"run_id": "run_1", "connection_id": conn.connection_id, "kind": "mcp"})
        self.assertEqual(manager.pending_count(), 1)
        manager.disconnect(conn.connection_id)
        self.assertEqual(manager.pending_count(), 0)
        self.assertTrue(future.cancelled())

    async def test_resolve_pending(self):
        manager = ConnectionManager()
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        manager.add_pending("mcp_1", future, {"run_id": "run_1", "connection_id": "conn_x", "kind": "mcp"})
        self.assertTrue(manager.resolve_pending("mcp_1", {"ok": True}))
        self.assertEqual(await future, {"ok": True})
        # duplicate results are ignored
        self.assertFalse(manager.resolve_pending("mcp_1", {"ok": True}))


if __name__ == "__main__":
    unittest.main()
