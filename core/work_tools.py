"""Work tool registry (plan sections 25.3, 25.4, 26.6).

Combines the current turn's MCP tool schemas with the device-daemon
schemas exposed by armed connections, and classifies tools for the
verification pass. The registry is built per turn: the frame's MCP
context is the only MCP execution authority, and device tools are offered
only when an armed connection exists (offline state is stated honestly;
nothing queues).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from .device import DEVICE_TOOLS_FULL, DEVICE_TOOLS_READ, device_tool_schemas
from .mcp import build_tool_schemas, known_tool_names, parse_servers

WRITE_HINTS: tuple[str, ...] = (
    "write", "create", "edit", "update", "delete", "remove", "move",
    "rename", "mkdir", "append",
)
CHECK_HINTS: tuple[str, ...] = (
    "check", "test", "verify", "lint", "compile", "build", "run_tests",
)
READ_HINTS: tuple[str, ...] = (
    "read", "list", "stat", "find", "search", "get", "inspect",
)


def classify_tool(name: str) -> str:
    """Best-effort write/check classification for verification (25.5)."""
    lowered = (name or "").lower()
    for hint in WRITE_HINTS:
        if hint in lowered:
            return "write"
    for hint in CHECK_HINTS:
        if hint in lowered:
            return "check"
    for hint in READ_HINTS:
        if hint in lowered:
            return "read"
    return "other"


@dataclass
class WorkToolRegistry:
    """Everything the agent loop may call this turn."""

    mcp_servers: list[dict] = field(default_factory=list)
    device_level: str = ""  # "", "read", or "full"
    schemas: list[dict] = field(default_factory=list)
    known: set[str] = field(default_factory=set)

    @classmethod
    def build(
        cls,
        *,
        context: dict | None,
        device_level: str,
        max_chars: int = 30000,
        shell_timeout_max: int = 600,
    ) -> "WorkToolRegistry":
        servers = parse_servers(context)
        schemas = build_tool_schemas(servers)
        if device_level:
            schemas.extend(
                device_tool_schemas(device_level, max_chars, shell_timeout_max)
            )
        known = known_tool_names(servers)
        known.update(
            tool
            for level_tools in (DEVICE_TOOLS_READ, DEVICE_TOOLS_FULL)
            for tool in level_tools
        )
        return cls(
            mcp_servers=servers,
            device_level=device_level,
            schemas=schemas,
            known=known,
        )

    @property
    def has_tools(self) -> bool:
        return bool(self.schemas)

    def device_tools_available(self) -> list[str]:
        if not self.device_level:
            return []
        return list(DEVICE_TOOLS_READ) + (
            list(DEVICE_TOOLS_FULL) if self.device_level == "full" else []
        )

    def availability_note(self) -> str:
        """Honest prompt text about tool availability (plan 26.6)."""
        if not self.mcp_servers and not self.device_level:
            return (
                "No MCP servers and no armed device are available for this "
                "turn. Answer from what you know; never claim tool or file "
                "results."
            )
        parts = []
        if self.mcp_servers:
            names = ", ".join(server["name"] for server in self.mcp_servers)
            parts.append(f"MCP servers available: {names}.")
        else:
            parts.append("No MCP servers are connected for this turn.")
        if self.device_level:
            parts.append(
                f"The owner's device is armed at {self.device_level} level: "
                f"{', '.join(self.device_tools_available())}."
            )
        else:
            parts.append("No device is armed; device tools are unavailable.")
        return " ".join(parts)


def bound_tool_result(result: dict, max_chars: int = 30000) -> str:
    """One-line bounded JSON for a tool role message."""
    try:
        text = json.dumps(result, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = json.dumps({"ok": False, "error": "unserializable_result"})
    if len(text) > max_chars:
        text = text[:max_chars]
    return text
