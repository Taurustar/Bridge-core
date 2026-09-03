"""MCP registry and execution proxy (plan sections 10.11, 25.3).

The current turn's ``context.mcp_servers`` is the execution authority:
only tools listed there for this turn may be requested, and only from the
originating WebSocket connection.

- Schema-rich servers list their tools with an input schema; each tool is
  exposed to the model as ``mcp__<server>__<tool>`` with the exact input
  schema. Legacy servers (no tool list) expose one generic wrapper
  ``mcp__<server>__call`` taking ``{tool, arguments}``.
- Results correlate strictly by request ``id``, verified against
  ``run_id`` and the originating connection. One result resolves one
  future and deletes the Redis backup. Duplicate, stale, or
  wrong-connection results are ignored with bounded metadata logs.
- A timeout creates a structured failed tool result so the agent loop can
  continue or report failure; it never raises into the turn.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid

from .cache import RedisCache
from .config import Config
from .constants import mcp_response_key
from .connections import Connection, ConnectionManager

log = logging.getLogger("bridge.mcp")

TOOL_NAME_SEPARATOR = "__"
MAX_RESULT_CHARS = 200_000  # server-side backstop; clients truncate first
MAX_SERVERS = 16
MAX_TOOLS_PER_SERVER = 64


class MCPError(RuntimeError):
    pass


def parse_tool_name(name: str) -> tuple[str, str] | None:
    """Split ``mcp__<server>__<tool>`` into (server, tool)."""
    parts = (name or "").split(TOOL_NAME_SEPARATOR)
    if len(parts) < 3 or parts[0] != "mcp":
        return None
    server = parts[1]
    tool = TOOL_NAME_SEPARATOR.join(parts[2:])
    if not server or not tool:
        return None
    return server, tool


def parse_servers(context: dict | None) -> list[dict]:
    """Extract and bound the MCP server declarations from turn context."""
    if not isinstance(context, dict):
        return []
    raw_servers = context.get("mcp_servers")
    if not isinstance(raw_servers, list):
        return []
    servers: list[dict] = []
    for raw in raw_servers[:MAX_SERVERS]:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or "").strip()
        if not name or TOOL_NAME_SEPARATOR in name:
            continue
        tools: list[dict] = []
        raw_tools = raw.get("tools")
        if isinstance(raw_tools, list):
            for tool in raw_tools[:MAX_TOOLS_PER_SERVER]:
                if not isinstance(tool, dict):
                    continue
                tool_name = str(tool.get("name") or "").strip()
                if not tool_name or TOOL_NAME_SEPARATOR in tool_name:
                    continue
                tools.append(
                    {
                        "name": tool_name,
                        "description": str(tool.get("description") or "")[:500],
                        "input_schema": tool.get("input_schema")
                        if isinstance(tool.get("input_schema"), dict)
                        else {"type": "object", "properties": {}},
                    }
                )
        servers.append({"name": name, "tools": tools})
    return servers


def build_tool_schemas(servers: list[dict]) -> list[dict]:
    """OpenAI function schemas for the current turn's MCP tools."""
    schemas: list[dict] = []
    for server in servers:
        if server["tools"]:
            for tool in server["tools"]:
                schemas.append(
                    {
                        "type": "function",
                        "function": {
                            "name": f"mcp__{server['name']}__{tool['name']}",
                            "description": tool["description"]
                            or f"{server['name']} tool {tool['name']}",
                            "parameters": tool["input_schema"],
                        },
                    }
                )
        else:
            # Legacy server: one generic wrapper.
            schemas.append(
                {
                    "type": "function",
                    "function": {
                        "name": f"mcp__{server['name']}__call",
                        "description": (
                            f"Generic call wrapper for legacy MCP server "
                            f"{server['name']}. Provide the server's tool "
                            f"name and a JSON arguments object."
                        ),
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "tool": {"type": "string"},
                                "arguments": {"type": "object"},
                            },
                            "required": ["tool", "arguments"],
                        },
                    },
                }
            )
    return schemas


def known_tool_names(servers: list[dict]) -> set[str]:
    names: set[str] = set()
    for server in servers:
        if server["tools"]:
            for tool in server["tools"]:
                names.add(f"mcp__{server['name']}__{tool['name']}")
        else:
            names.add(f"mcp__{server['name']}__call")
    return names


class MCPProxy:
    """Routes tool requests to the originating connection (plan 10.11)."""

    def __init__(
        self, config: Config, cache: RedisCache, connections: ConnectionManager
    ) -> None:
        self.config = config
        self.cache = cache
        self.connections = connections

    async def call(
        self,
        owner: str,
        *,
        run_id: str,
        source_conn: Connection | None,
        server: str,
        tool: str,
        arguments: dict,
        turn_calls: list[str],
    ) -> dict:
        """Execute one MCP tool. Always returns a structured result."""
        if source_conn is None:
            return {
                "ok": False,
                "error": "tools_require_websocket",
                "result": None,
                "truncated": False,
            }
        request_id = f"mcp_{uuid.uuid4().hex}"
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self.connections.add_pending(
            request_id,
            future,
            {"run_id": run_id, "connection_id": source_conn.connection_id,
             "kind": "mcp"},
        )
        backup = {
            "request_id": request_id,
            "run_id": run_id,
            "server": server,
            "tool": tool,
            "connection_id": source_conn.connection_id,
        }
        await self.cache.set_value(
            mcp_response_key(owner, request_id), json.dumps(backup)
        )
        turn_calls.append(f"{server}/{tool}")
        try:
            await source_conn.send_json(
                {
                    "type": "mcp_tool_request",
                    "id": request_id,
                    "run_id": run_id,
                    "server": server,
                    "tool": tool,
                    "arguments": arguments,
                    "timeout_seconds": self.config.MCP_TOOL_TIMEOUT,
                }
            )
            result = await asyncio.wait_for(
                future, timeout=max(self.config.MCP_TOOL_TIMEOUT, 1)
            )
        except asyncio.TimeoutError:
            log.info("MCP request timed out (%s/%s)", server, tool)
            return {
                "ok": False,
                "error": "timeout",
                "result": None,
                "truncated": False,
            }
        except Exception as exc:  # noqa: BLE001 - structured failure only
            log.info("MCP request failed to send (%s): %s", server,
                     type(exc).__name__)
            return {
                "ok": False,
                "error": "connection_lost",
                "result": None,
                "truncated": False,
            }
        finally:
            self.connections.drop_pending(request_id)
            await self.cache.delete(mcp_response_key(owner, request_id))
        return result if isinstance(result, dict) else {
            "ok": False, "error": "malformed_result", "result": None,
            "truncated": False,
        }

    def handle_result(self, conn: Connection, frame: dict) -> bool:
        """Resolve a pending future from an ``mcp_result`` frame.

        Returns True when this frame consumed a pending request.
        """
        request_id = frame.get("id")
        if not isinstance(request_id, str):
            return False
        pending = self.connections.pending_meta(request_id)
        if pending is None:
            log.debug("Ignoring stale MCP result %s", request_id[:16])
            return False
        if (
            pending.get("kind") != "mcp"
            or pending.get("connection_id") != conn.connection_id
            or frame.get("run_id") != pending.get("run_id")
        ):
            log.info(
                "Ignoring MCP result with wrong correlation (%s)",
                request_id[:16],
            )
            return False
        result = {
            "ok": bool(frame.get("ok")),
            "error": frame.get("error"),
            "result": frame.get("result"),
            "truncated": bool(frame.get("truncated")),
        }
        encoded = json.dumps(result)[:MAX_RESULT_CHARS]
        return self.connections.resolve_pending(request_id, json.loads(encoded))
