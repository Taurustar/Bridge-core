"""Connection and concurrency model (plan section 11).

- ``connection_id -> Connection`` and ``owner user_id -> connection ids``.
- Connecting a new device never closes an existing connection.
- Disconnect removes exactly one connection and resolves pending futures.
- Companion turns serialize on one per-owner lock shared across devices.
- Heartbeats are never blocked on the turn lock.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("bridge.connections")


@dataclass
class Connection:
    """One owner device connection. State here is connection-local."""

    connection_id: str
    user_id: str
    websocket: Any
    client_type: str = "unknown"
    device_id: str = ""
    timezone: str = ""
    last_heartbeat_sequence: int = -1
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def send_json(self, frame: dict) -> bool:
        """Send a frame; returns a delivery boolean (plan section 12 step 26)."""
        try:
            async with self.send_lock:
                await self.websocket.send_json(frame)
            return True
        except Exception as exc:  # noqa: BLE001 - caller decides policy
            log.info(
                "Send to %s failed (%s); treating as undelivered",
                self.connection_id,
                type(exc).__name__,
            )
            return False


class ConnectionManager:
    def __init__(self) -> None:
        self._connections: dict[str, Connection] = {}
        self._by_user: dict[str, set[str]] = {}
        self._turn_locks: dict[str, asyncio.Lock] = {}
        self._profile_locks: dict[str, asyncio.Lock] = {}
        self._pending: dict[str, asyncio.Future] = {}

    def connect(
        self,
        websocket: Any,
        user_id: str,
        client_type: str = "unknown",
        device_id: str = "",
        timezone: str = "",
    ) -> Connection:
        connection_id = f"conn_{uuid.uuid4().hex}"
        conn = Connection(
            connection_id=connection_id,
            user_id=user_id,
            websocket=websocket,
            client_type=client_type,
            device_id=device_id,
            timezone=timezone,
        )
        self._connections[connection_id] = conn
        self._by_user.setdefault(user_id, set()).add(connection_id)
        log.info("Connected %s (user=%s, devices=%d)", connection_id, user_id,
                 len(self._by_user[user_id]))
        return conn

    def disconnect(self, connection_id: str) -> None:
        """Remove exactly one connection and fail its pending futures."""
        conn = self._connections.pop(connection_id, None)
        if conn is None:
            return
        ids = self._by_user.get(conn.user_id)
        if ids is not None:
            ids.discard(connection_id)
            if not ids:
                del self._by_user[conn.user_id]
        for request_id, future in list(self._pending.items()):
            if not future.done():
                future.cancel()
            del self._pending[request_id]
        log.info("Disconnected %s (user=%s)", connection_id, conn.user_id)

    def get(self, connection_id: str) -> Connection | None:
        return self._connections.get(connection_id)

    def connections_for(self, user_id: str) -> list[Connection]:
        return [
            self._connections[cid]
            for cid in self._by_user.get(user_id, set())
            if cid in self._connections
        ]

    def turn_lock(self, user_id: str) -> asyncio.Lock:
        """The single per-owner companion turn lock shared across devices."""
        lock = self._turn_locks.get(user_id)
        if lock is None:
            lock = asyncio.Lock()
            self._turn_locks[user_id] = lock
        return lock

    def profile_lock(self, user_id: str) -> asyncio.Lock:
        """Per-owner profile lock for read-modify-write serialization
        (plan section 18.7); separate from the companion turn lock."""
        lock = self._profile_locks.get(user_id)
        if lock is None:
            lock = asyncio.Lock()
            self._profile_locks[user_id] = lock
        return lock

    async def fan_out(
        self, user_id: str, frame: dict, exclude_connection_id: str | None = None
    ) -> int:
        """Send a frame to every other connection of the user.

        Fanout failure never rolls back persistence (plan section 12 step 11).
        Returns the number of connections that acknowledged the send.
        """
        delivered = 0
        for conn in self.connections_for(user_id):
            if conn.connection_id == exclude_connection_id:
                continue
            if await conn.send_json(frame):
                delivered += 1
        return delivered

    # Pending in-process futures (MCP/device routing arrives in later
    # milestones; the lifecycle contract exists now per section 11).
    def add_pending(self, request_id: str, future: asyncio.Future) -> None:
        self._pending[request_id] = future

    def resolve_pending(self, request_id: str, result: Any) -> bool:
        future = self._pending.pop(request_id, None)
        if future is None or future.done():
            return False
        future.set_result(result)
        return True

    def pending_count(self) -> int:
        return len(self._pending)
