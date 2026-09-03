"""Device daemon: levels, fences, routing, audit (plan section 26).

The desktop client hosts the executor; the server routes requests over
the existing WebSocket and independently enforces its own fence layer:

- ``read`` level allows device_read/list/stat/find; ``full`` adds
  device_write/device_shell.
- Version 1 argument schemas reject unknown fields outright.
- Secret paths (.env, SSH keys, cloud credentials, browser profiles,
  keychains, token stores) are rejected before a request is sent.
- Per-turn call caps and output caps bound every request.
- Results are one-time, connection-bound, run-bound, and deleted after
  consumption. Device reconnect starts disarmed and invalidates pending
  requests for the old connection.
- The audit ring stores metadata only: tool, bounded path/command
  preview, ok flag, duration, timestamp — never output text or file
  content.

The client must still enforce its own fences (path roots, command
blocklist, timeouts, user permission); the server checks are the second,
independent layer.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from pathlib import PurePosixPath, PureWindowsPath

from .cache import RedisCache
from .config import Config
from .constants import (
    AUDIT_RING_MAX,
    DEVICE_MAX_ARGV,
    DEVICE_MAX_DEPTH,
    DEVICE_MAX_ENV_ENTRIES,
    DEVICE_MAX_FIND_RESULTS,
    DEVICE_MAX_LIST_ENTRIES,
    DEVICE_MAX_PATH_CHARS,
    DEVICE_MAX_PATTERN_CHARS,
    DEVICE_MAX_SHELL_COMMAND_CHARS,
    device_audit_key,
    device_response_key,
)
from .connections import Connection, ConnectionManager

log = logging.getLogger("bridge.device")

DEVICE_TOOLS_READ: tuple[str, ...] = (
    "device_read",
    "device_list",
    "device_stat",
    "device_find",
)
DEVICE_TOOLS_FULL: tuple[str, ...] = ("device_write", "device_shell")

# Secret-pattern fence (plan section 26.4). Applied to every path-like
# argument; the client must apply the same blocklist independently.
SECRET_PATTERNS: tuple[str, ...] = (
    ".env",
    "id_rsa",
    "id_ed25519",
    "id_ecdsa",
    ".ssh/",
    ".aws/",
    ".gcloud/",
    ".azure/",
    ".gnupg/",
    ".kube/",
    ".docker/config",
    "credentials",
    "keychain",
    ".pem",
    ".p12",
    ".pfx",
    ".kdbx",
    "cookies",
    "login data",
    "tokens.json",
    ".netrc",
    ".git-credentials",
    ".wallet",
)

_PATH_ARG_FIELDS: dict[str, tuple[str, ...]] = {
    "device_read": ("path",),
    "device_list": ("path",),
    "device_stat": ("path",),
    "device_find": ("root",),
    "device_write": ("path",),
    "device_shell": ("cwd",),
}

_OUTPUT_FIELDS: dict[str, str] = {
    "device_read": "max_bytes",
    "device_list": "max_entries",
    "device_find": "max_results",
}

_WRITE_SCHEMA = {
    "path": str,
    "content": str,
    "encoding": str,
    "overwrite": bool,
    "create_parents": bool,
}

SHELL_ENV_KEY_PATTERN_ALLOW: tuple[str, ...] = (
    "LANG", "LC_ALL", "TZ", "PATH", "HOME", "TERM", "TMPDIR",
)


def device_tool_schemas(
    level: str, max_chars: int, shell_timeout_max: int = 600
) -> list[dict]:
    """OpenAI function schemas for device tools at the given level."""
    cap = max_chars
    schemas: list[dict] = [
        {
            "type": "function",
            "function": {
                "name": "device_read",
                "description": (
                    "Read bytes from a file on the owner's device. "
                    "Returns utf8 text or base64."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "offset_bytes": {"type": "integer", "minimum": 0},
                        "max_bytes": {"type": "integer",
                                      "minimum": 1, "maximum": cap},
                        "encoding": {"type": "string",
                                     "enum": ["utf8", "base64"]},
                    },
                    "required": ["path"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "device_list",
                "description": "List a directory on the owner's device.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "max_entries": {"type": "integer",
                                        "minimum": 1,
                                        "maximum": DEVICE_MAX_LIST_ENTRIES},
                        "include_hidden": {"type": "boolean"},
                    },
                    "required": ["path"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "device_stat",
                "description": "Stat one path on the owner's device.",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "device_find",
                "description": "Find files by glob pattern under a root.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "root": {"type": "string"},
                        "pattern": {"type": "string"},
                        "max_results": {"type": "integer",
                                        "minimum": 1,
                                        "maximum": DEVICE_MAX_FIND_RESULTS},
                        "max_depth": {"type": "integer",
                                      "minimum": 0,
                                      "maximum": DEVICE_MAX_DEPTH},
                    },
                    "required": ["root", "pattern"],
                },
            },
        },
    ]
    if level == "full":
        schemas.append(
            {
                "type": "function",
                "function": {
                    "name": "device_write",
                    "description": (
                        "Write a file on the owner's device (full level). "
                        "Text utf8 or base64 payload."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "content": {"type": "string"},
                            "encoding": {"type": "string",
                                         "enum": ["utf8", "base64"]},
                            "overwrite": {"type": "boolean"},
                            "create_parents": {"type": "boolean"},
                        },
                        "required": ["path", "content", "encoding"],
                    },
                },
            }
        )
        schemas.append(
            {
                "type": "function",
                "function": {
                    "name": "device_shell",
                    "description": (
                        "Run a command on the owner's device (full level). "
                        "Provide argv OR shell_command, never both."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "cwd": {"type": "string"},
                            "argv": {"type": "array",
                                     "items": {"type": "string"}},
                            "shell_command": {"type": "string"},
                            "timeout_seconds": {"type": "integer",
                                                "minimum": 1,
                                                "maximum":
                                                shell_timeout_max},
                            "environment": {"type": "object"},
                        },
                        "required": ["cwd"],
                    },
                },
            }
        )
    return schemas


def contains_secret_pattern(text: str) -> bool:
    lowered = (text or "").lower()
    return any(pattern in lowered for pattern in SECRET_PATTERNS)


def _path_looks_outside_roots(path: str, roots: list[str]) -> bool:
    """Advisory containment check (the client re-checks with realpath)."""
    if not roots:
        return False  # nothing to compare against; client enforces
    for candidate in (PurePosixPath(path), PureWindowsPath(path)):
        for root in roots:
            try:
                candidate.relative_to(PurePosixPath(root))
                return False
            except ValueError:
                continue
            try:
                candidate.relative_to(PureWindowsPath(root))
                return False
            except ValueError:
                continue
    return True


def validate_device_arguments(
    tool: str,
    arguments: object,
    level: str,
    roots: list[str],
    max_output_chars: int = 30000,
    shell_timeout_max: int = 600,
) -> tuple[dict | None, str | None]:
    """Server-side fence: schema, level, caps, secret patterns.

    Returns (clean_arguments, error_code). Unknown fields are rejected.
    """
    if tool not in DEVICE_TOOLS_READ + DEVICE_TOOLS_FULL:
        return None, "unknown_device_tool"
    needs_full = tool in DEVICE_TOOLS_FULL
    if needs_full and level != "full":
        return None, "device_level_required"
    if not isinstance(arguments, dict):
        return None, "invalid_arguments"

    schema = {
        "device_read": {"path", "offset_bytes", "max_bytes", "encoding"},
        "device_list": {"path", "max_entries", "include_hidden"},
        "device_stat": {"path"},
        "device_find": {"root", "pattern", "max_results", "max_depth"},
        "device_write": set(_WRITE_SCHEMA),
        "device_shell": {"cwd", "argv", "shell_command", "timeout_seconds",
                         "environment"},
    }[tool]
    unknown = set(arguments) - schema
    if unknown:
        return None, "unknown_arguments"

    clean = dict(arguments)

    for field in _PATH_ARG_FIELDS.get(tool, ()):
        value = clean.get(field)
        if not isinstance(value, str) or not value.strip():
            return None, "invalid_arguments"
        if len(value) > DEVICE_MAX_PATH_CHARS:
            return None, "invalid_arguments"
        if contains_secret_pattern(value):
            return None, "secret_path_rejected"
        if _path_looks_outside_roots(value, roots):
            return None, "path_outside_roots"

    if tool == "device_find":
        pattern = clean.get("pattern")
        if not isinstance(pattern, str) or not pattern.strip():
            return None, "invalid_arguments"
        if len(pattern) > DEVICE_MAX_PATTERN_CHARS:
            return None, "invalid_arguments"

    output_field = _OUTPUT_FIELDS.get(tool)
    if output_field:
        value = clean.get(output_field)
        if value is not None and (
            not isinstance(value, int)
            or isinstance(value, bool)
            or value < 1
            or value > max_output_chars
        ):
            return None, "invalid_arguments"

    if tool == "device_write":
        encoding = clean.get("encoding")
        if encoding not in ("utf8", "base64"):
            return None, "invalid_arguments"
        content = clean.get("content")
        if not isinstance(content, str):
            return None, "invalid_arguments"
        if len(content) > max_output_chars:
            return None, "invalid_arguments"

    if tool == "device_shell":
        argv = clean.get("argv")
        shell_command = clean.get("shell_command")
        if argv is not None and shell_command is not None:
            return None, "invalid_arguments"
        if argv is None and shell_command is None:
            return None, "invalid_arguments"
        if argv is not None:
            if (
                not isinstance(argv, list)
                or not argv
                or len(argv) > DEVICE_MAX_ARGV
                or not all(isinstance(a, str) and a for a in argv)
            ):
                return None, "invalid_arguments"
            if any(contains_secret_pattern(argument) for argument in argv):
                return None, "secret_path_rejected"
        if shell_command is not None:
            if (
                not isinstance(shell_command, str)
                or len(shell_command) > DEVICE_MAX_SHELL_COMMAND_CHARS
            ):
                return None, "invalid_arguments"
            if contains_secret_pattern(shell_command):
                return None, "secret_path_rejected"
        timeout = clean.get("timeout_seconds")
        if timeout is not None and (
            not isinstance(timeout, int)
            or isinstance(timeout, bool)
            or not (1 <= timeout <= shell_timeout_max)
        ):
            return None, "invalid_arguments"
        environment = clean.get("environment")
        if environment is not None:
            if not isinstance(environment, dict):
                return None, "invalid_arguments"
            if len(environment) > DEVICE_MAX_ENV_ENTRIES:
                return None, "invalid_arguments"
            filtered = {}
            for key, value in environment.items():
                if not isinstance(key, str) or not isinstance(value, str):
                    return None, "invalid_arguments"
                if key.upper() in SHELL_ENV_KEY_PATTERN_ALLOW:
                    filtered[key] = value
            clean["environment"] = filtered
        cwd = clean["cwd"]
        if _path_looks_outside_roots(cwd, roots):
            return None, "path_outside_roots"

    return clean, None


class DeviceManager:
    """Routes device tool requests to armed owner connections."""

    def __init__(
        self, config: Config, cache: RedisCache, connections: ConnectionManager
    ) -> None:
        self.config = config
        self.cache = cache
        self.connections = connections

    # -- arming ---------------------------------------------------------------

    def apply_state(self, conn: Connection, frame: dict) -> str | None:
        """Apply a ``device_state`` frame. Returns an error code or None."""
        if not self.config.DEVICE_ENABLED:
            return "device_disabled"
        armed = frame.get("armed")
        level = frame.get("level", "read")
        roots = frame.get("roots", [])
        version = frame.get("protocol_version", 1)
        if not isinstance(armed, bool):
            return "invalid_device_state"
        if armed and level not in ("read", "full"):
            return "invalid_device_state"
        if not isinstance(roots, list) or not all(
            isinstance(root, str) and root.strip() for root in roots
        ):
            return "invalid_device_state"
        if version != 1:
            return "invalid_device_state"
        conn.device_armed = armed
        conn.device_level = level if armed else ""
        conn.device_roots = [root.strip() for root in roots] if armed else []
        conn.device_protocol_version = version
        log.info(
            "Device state on %s: armed=%s level=%s roots=%d",
            conn.connection_id, armed, conn.device_level, len(conn.device_roots),
        )
        return None

    def armed_connections(self, owner: str, level_needed: str) -> list[Connection]:
        """Armed connections supporting the level, most recently active
        first (plan sections 11, 26.6)."""
        result: list[Connection] = []
        for conn in self.connections.connections_for(owner):
            if not conn.device_armed:
                continue
            if level_needed == "full" and conn.device_level != "full":
                continue
            result.append(conn)
        result.sort(key=lambda c: c.last_activity_ts, reverse=True)
        return result

    # -- execution ---------------------------------------------------------------

    async def call(
        self,
        owner: str,
        *,
        run_id: str,
        tool: str,
        arguments: object,
        turn_calls: list[str],
    ) -> dict:
        """Execute one device tool via an armed connection.

        Always returns a structured result; routing failures and timeouts
        become failed tool results (plan section 26).
        """
        if len(turn_calls) >= self.config.DEVICE_PER_TURN_CALL_CAP:
            return {
                "ok": False, "error": "device_call_cap", "result": None,
                "truncated": False,
            }
        level_needed = "full" if tool in DEVICE_TOOLS_FULL else "read"
        candidates = self.armed_connections(owner, level_needed)
        if not candidates:
            return {
                "ok": False, "error": "device_unavailable", "result": None,
                "truncated": False,
            }
        conn = candidates[0]
        clean, error = validate_device_arguments(
            tool,
            arguments,
            conn.device_level,
            conn.device_roots,
            max_output_chars=self.config.DEVICE_MAX_OUTPUT_CHARS,
            shell_timeout_max=self.config.DEVICE_SHELL_TIMEOUT_MAX,
        )
        if error is not None:
            return {
                "ok": False, "error": error, "result": None,
                "truncated": False,
            }

        request_id = f"dev_{uuid.uuid4().hex}"
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self.connections.add_pending(
            request_id,
            future,
            {"run_id": run_id, "connection_id": conn.connection_id,
             "kind": "device"},
        )
        await self.cache.set_value(
            device_response_key(owner, request_id),
            json.dumps({
                "request_id": request_id,
                "run_id": run_id,
                "tool": tool,
                "connection_id": conn.connection_id,
            }),
        )
        turn_calls.append(tool)
        started = asyncio.get_running_loop().time()
        timeout = min(
            self.config.DEVICE_TOOL_TIMEOUT, self.config.DEVICE_SHELL_TIMEOUT_MAX
        )
        preview = self._preview(tool, clean or {})
        try:
            await conn.send_json(
                {
                    "type": "device_tool_request",
                    "id": request_id,
                    "run_id": run_id,
                    "tool": tool,
                    "arguments": clean,
                    "timeout_seconds": timeout,
                    "max_output_chars": self.config.DEVICE_MAX_OUTPUT_CHARS,
                }
            )
            result = await asyncio.wait_for(future, timeout=max(timeout, 1))
        except asyncio.TimeoutError:
            result = {
                "ok": False, "error": "timeout", "result": None,
                "truncated": False,
            }
        except Exception as exc:  # noqa: BLE001 - structured failure only
            log.info("Device request failed to send (%s): %s", tool,
                     type(exc).__name__)
            result = {
                "ok": False, "error": "connection_lost", "result": None,
                "truncated": False,
            }
        finally:
            self.connections.drop_pending(request_id)
            await self.cache.delete(device_response_key(owner, request_id))
        duration_ms = int(
            (asyncio.get_running_loop().time() - started) * 1000
        )
        await self.audit(owner, tool, preview, bool(result.get("ok")), duration_ms)
        return result if isinstance(result, dict) else {
            "ok": False, "error": "malformed_result", "result": None,
            "truncated": False,
        }

    def handle_result(self, conn: Connection, frame: dict) -> bool:
        """Resolve a pending future from a ``device_tool_result`` frame."""
        request_id = frame.get("id")
        if not isinstance(request_id, str):
            return False
        pending = self.connections.pending_meta(request_id)
        if pending is None:
            log.debug("Ignoring stale device result %s", request_id[:16])
            return False
        if (
            pending.get("kind") != "device"
            or pending.get("connection_id") != conn.connection_id
            or frame.get("run_id") != pending.get("run_id")
        ):
            log.info(
                "Ignoring device result with wrong correlation (%s)",
                request_id[:16],
            )
            return False
        result = {
            "ok": bool(frame.get("ok")),
            "error": frame.get("error"),
            "result": frame.get("result"),
            "truncated": bool(frame.get("truncated")),
            "duration_ms": frame.get("duration_ms"),
        }
        return self.connections.resolve_pending(request_id, result)

    @staticmethod
    def _preview(tool: str, arguments: dict) -> str:
        for field in _PATH_ARG_FIELDS.get(tool, ()):
            if field in arguments:
                return f"{tool} {arguments[field]}"[:120]
        if "pattern" in arguments:
            return f"{tool} {arguments.get('root', '')} {arguments['pattern']}"[:120]
        if "argv" in arguments:
            return f"{tool} {' '.join(arguments['argv'][:4])}"[:120]
        if "shell_command" in arguments:
            return f"{tool} {arguments['shell_command']}"[:120]
        return tool

    async def audit(
        self, owner: str, tool: str, preview: str, ok: bool, duration_ms: int
    ) -> None:
        """Metadata-only audit ring (plan section 26.5)."""
        entry = json.dumps(
            {
                "tool": tool,
                "preview": preview[:120],
                "ok": ok,
                "duration_ms": duration_ms,
                "ts": time.time(),
            }
        )
        pipe = self.cache.client.pipeline(transaction=True)
        pipe.rpush(device_audit_key(owner), entry)
        pipe.ltrim(device_audit_key(owner), -AUDIT_RING_MAX, -1)
        await pipe.execute()
