"""Connection and concurrency model (plan section 11).

- ``connection_id -> Connection`` and ``owner user_id -> connection ids``.
- Connecting a new device never closes an existing connection.
- Disconnect removes exactly one connection and resolves pending futures.
- Companion turns serialize on one per-owner lock shared across devices;
  work turns serialize on one per-session lock (plan section 11).
- Heartbeats are never blocked on the turn lock.
- Pending MCP/device requests are one-time, connection-bound, run-bound;
  disconnect fails all of a connection's pending futures.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("bridge.connections")


@dataclass
class PendingRequest:
    """One awaited MCP/device tool result (plan sections 10.11, 10.12)."""

    future: asyncio.Future
    run_id: str
    connection_id: str
    kind: str  # "mcp" | "device"


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
    last_activity_ts: float = field(default_factory=time.monotonic)
    # Device-daemon arming (plan section 26). Reconnect starts disarmed
    # because these are connection-local defaults.
    device_armed: bool = False
    device_level: str = ""  # "read" | "full"
    device_roots: list[str] = field(default_factory=list)
    device_protocol_version: int = 0
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def touch(self) -> None:
        self.last_activity_ts = time.monotonic()

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
        self._catchup_locks: dict[str, asyncio.Lock] = {}
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._initiative_locks: dict[str, asyncio.Lock] = {}
        self._pending: dict[str, PendingRequest] = {}

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
        for request_id, pending in list(self._pending.items()):
            if pending.connection_id != connection_id:
                continue
            if not pending.future.done():
                pending.future.cancel()
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

    def catchup_lock(self, user_id: str) -> asyncio.Lock:
        """Per-owner catch-up lock, separate from the turn lock, preventing
        duplicate catch-up sends (plan section 11)."""
        lock = self._catchup_locks.get(user_id)
        if lock is None:
            lock = asyncio.Lock()
            self._catchup_locks[user_id] = lock
        return lock

    def session_lock(self, session_id: str) -> asyncio.Lock:
        """Work turns serialize per session (plan sections 11, 25.2)."""
        lock = self._session_locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._session_locks[session_id] = lock
        return lock

    def initiative_lock(self, user_id: str) -> asyncio.Lock:
        """Per-owner initiative lock (plan section 23.3).

        Serializes heartbeat counting and delivery accounting so concurrent
        device heartbeats cannot double-count. Separate from the turn lock,
        the catch-up lock, and the profile lock (plan section 11).
        """
        lock = self._initiative_locks.get(user_id)
        if lock is None:
            lock = asyncio.Lock()
            self._initiative_locks[user_id] = lock
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

    # Pending MCP/device requests (plan sections 10.11, 10.12): one-time,
    # connection-bound, run-bound, removed on success/timeout/disconnect.
    def add_pending(
        self, request_id: str, future: asyncio.Future, meta: dict
    ) -> None:
        self._pending[request_id] = PendingRequest(
            future=future,
            run_id=str(meta.get("run_id") or ""),
            connection_id=str(meta.get("connection_id") or ""),
            kind=str(meta.get("kind") or ""),
        )

    def pending_meta(self, request_id: str) -> dict | None:
        pending = self._pending.get(request_id)
        if pending is None:
            return None
        return {
            "run_id": pending.run_id,
            "connection_id": pending.connection_id,
            "kind": pending.kind,
        }

    def resolve_pending(self, request_id: str, result: Any) -> bool:
        pending = self._pending.pop(request_id, None)
        if pending is None or pending.future.done():
            return False
        pending.future.set_result(result)
        return True

    def drop_pending(self, request_id: str) -> None:
        self._pending.pop(request_id, None)

    def pending_count(self) -> int:
        return len(self._pending)
