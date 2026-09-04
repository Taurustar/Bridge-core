"""Private daily tools (plan section 24).

Daily tools are server-owned and invisible: the character silently checks
information and speaks only the answer. This module owns the OpenAI function
schemas, the per-turn execution bound, the idempotency keys for mutations,
the durable reminder store, and the deterministic narration sanitizer.

Mutation law (plan section 24.2): reminder and owner-schedule writes require
explicit intent in the current owner message (deterministic keyword gate);
missing time/timezone material returns a structured clarification error
instead of a guessed write; every mutation carries an idempotency key
derived from turn id + tool call id, so a provider replay cannot
double-write.
"""

from __future__ import annotations

import ast
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field

from .cache import RedisCache
from .config import Config
from .constants import (
    DAILY_IDEMPOTENCY_MAX_KEYS,
    DAILY_TOOL_RESULT_MAX_CHARS,
    REMINDERS_MAX,
    REMINDER_TEXT_MAX_CHARS,
    daily_idempotency_key,
    reminders_key,
    user_schedule_key,
)
from .user_schedule import default_store

log = logging.getLogger("bridge.daily_tools")

# Explicit-intent gates for mutations (deterministic conservative check).
REMINDER_INTENT_WORDS: tuple[str, ...] = (
    "remind", "reminder", "remember",
)
SCHEDULE_INTENT_WORDS: tuple[str, ...] = (
    "schedule", "calendar", "availability", "available", "busy", "free",
)

# Deterministic narration sanitizer phrases (plan section 24.2). Matched
# case-insensitively as whole words; matches are removed before the reply
# ships. If nothing speakable survives, the bridge retries synthesis once
# without tools.
_SANITIZE_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\btavily\b",
        r"\bapi\b(?:\s+\w+){0,2}",
        r"\bschema\b",
        r"\btool(?:s|\s+call|\s+calls)?\b",
        r"\bfunction call(?:s)?\b",
        r"\bexecution(?:\s+result(?:s)?)?\b",
        r"\bhidden result(?:s)?\b",
        r"\binternal result(?:s)?\b",
        r"\bsearch result(?:s)?\b",
        r"\bweb (?:search|query|request)\b",
    )
)


def sanitize_daily_reply(text: str) -> str:
    """Remove tool/API/execution narration from a final reply."""
    cleaned = text or ""
    for pattern in _SANITIZE_PATTERNS:
        cleaned = pattern.sub(" ", cleaned)
    return re.sub(r"[ \t]{2,}", " ", cleaned)


def mutation_intent_present(user_text: str, words: tuple[str, ...]) -> bool:
    lowered = re.sub(r"\s+", " ", (user_text or "").lower()).strip()
    if not lowered:
        return False
    domain = "|".join(re.escape(word) for word in words)
    mutation = r"\b(?:add|create|set|make|change|edit|update|move|delete|remove|cancel|clear|remind|remember|schedule)\b"
    for clause in re.split(r"\s*(?:[;.!?]+|\bbut\b)\s*", lowered):
        # "Don't forget to ..." reinforces the following command rather than
        # negating it. Other negation remains a conservative refusal.
        clause = re.sub(r"\b(?:do not|don't|dont|never) forget to\b", "", clause)
        if not re.search(rf"\b(?:{domain})\b", clause):
            continue
        if not re.search(mutation, clause):
            continue
        if re.search(r"\b(?:do not|don't|dont|never|not|stop)\b", clause):
            continue
        if re.match(r"^(?:am|is|are)\s+i\b", clause):
            continue
        if re.match(r"^(?:what|when|where|why|how)\b", clause):
            continue
        if re.search(r"\b(?:explain|describe|show|list|tell me about)\b", clause):
            continue
        return True
    return False


def safe_arithmetic(expression: str) -> float:
    """Evaluate a pure arithmetic expression (plan section 24.2)."""
    tree = ast.parse(expression, mode="eval")

    def _eval(node: ast.AST) -> float:
        if isinstance(node, ast.Expression):
            return _eval(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return float(node.value)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            value = _eval(node.operand)
            return value if isinstance(node.op, ast.UAdd) else -value
        if isinstance(node, ast.BinOp):
            left = _eval(node.left)
            right = _eval(node.right)
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            if isinstance(node.op, ast.Mult):
                return left * right
            if isinstance(node.op, ast.Div):
                if right == 0:
                    raise ValueError("division by zero")
                return left / right
            if isinstance(node.op, ast.Mod):
                if right == 0:
                    raise ValueError("modulo by zero")
                return left % right
            if isinstance(node.op, ast.Pow):
                if abs(right) > 64:
                    raise ValueError("exponent too large")
                return left**right
        raise ValueError("unsupported expression")

    return _eval(tree)


_UNIT_TABLE: dict[str, dict[str, float]] = {
    "length": {"m": 1.0, "km": 1000.0, "mi": 1609.344, "ft": 0.3048, "cm": 0.01},
    "mass": {"kg": 1.0, "g": 0.001, "lb": 0.45359237, "oz": 0.028349523},
}

_TEMP_UNITS = {"c", "f", "k"}


def convert_units(value: float, from_unit: str, to_unit: str) -> dict:
    """Deterministic unit conversion for length, mass, and temperature."""
    src = from_unit.strip().lower()
    dst = to_unit.strip().lower()
    if src in _TEMP_UNITS and dst in _TEMP_UNITS:
        celsius = {"c": value, "f": (value - 32.0) * 5.0 / 9.0, "k": value - 273.15}[src]
        result = {"c": celsius, "f": celsius * 9.0 / 5.0 + 32.0, "k": celsius + 273.15}[dst]
        return {"ok": True, "value": round(result, 4), "unit": dst}
    for table in _UNIT_TABLE.values():
        if src in table and dst in table:
            result = value * table[src] / table[dst]
            return {"ok": True, "value": round(result, 6), "unit": dst}
    return {"ok": False, "error": "unsupported_units"}


# -- stores -----------------------------------------------------------------


class ReminderStore:
    """Durable owner reminders (plan section 24.3): notes, not alarms."""

    def __init__(self, cache: RedisCache) -> None:
        self.cache = cache

    def _key(self, owner: str) -> str:
        return reminders_key(owner)

    async def list(self, owner: str) -> list[dict]:
        rows: list[dict] = []
        for raw in await self.cache.get_rows(self._key(owner)):
            try:
                row = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
        return rows

    async def create(self, owner: str, *, text: str, due_ts: float, timezone_name: str) -> dict:
        reminder = {
            "id": f"rem_{int(time.time() * 1000):x}",
            "text": text[:REMINDER_TEXT_MAX_CHARS],
            "due_ts": float(due_ts),
            "timezone": timezone_name or "",
            "created_ts": time.time(),
        }
        rows = await self.list(owner)
        rows.append(reminder)
        rows = rows[-REMINDERS_MAX:]
        await self._write(owner, rows)
        return reminder

    async def create_once(
        self, owner: str, mutation_key: str, *, text: str, due_ts: float, timezone_name: str
    ) -> tuple[bool, dict | None]:
        reminder = {
            "id": f"rem_{uuid.uuid4().hex}",
            "text": text[:REMINDER_TEXT_MAX_CHARS],
            "due_ts": float(due_ts),
            "timezone": timezone_name,
            "created_ts": time.time(),
        }

        def update(raw_rows: list[str]) -> tuple[list[str], dict]:
            return (raw_rows + [json.dumps(reminder)])[-REMINDERS_MAX:], reminder

        _, duplicate, result = await self.cache.atomic_replace_list_once(
            self._key(owner), daily_idempotency_key(owner), mutation_key,
            DAILY_IDEMPOTENCY_MAX_KEYS, update,
        )
        return duplicate, result

    async def update_once(
        self, owner: str, mutation_key: str, reminder_id: str, changes: dict
    ) -> tuple[bool, dict | None]:
        def update(raw_rows: list[str]) -> tuple[list[str] | None, dict | None]:
            replacement: list[str] = []
            changed = None
            for raw in raw_rows:
                try:
                    row = json.loads(raw)
                except json.JSONDecodeError:
                    replacement.append(raw)
                    continue
                if isinstance(row, dict) and row.get("id") == reminder_id:
                    row.update(changes)
                    changed = row
                    raw = json.dumps(row)
                replacement.append(raw)
            return (replacement, changed) if changed is not None else (None, None)

        _, duplicate, result = await self.cache.atomic_replace_list_once(
            self._key(owner), daily_idempotency_key(owner), mutation_key,
            DAILY_IDEMPOTENCY_MAX_KEYS, update,
        )
        return duplicate, result

    async def delete_once(
        self, owner: str, mutation_key: str, reminder_id: str
    ) -> tuple[bool, bool]:
        def update(raw_rows: list[str]) -> tuple[list[str] | None, bool]:
            remaining = []
            found = False
            for raw in raw_rows:
                try:
                    row = json.loads(raw)
                except json.JSONDecodeError:
                    row = None
                if isinstance(row, dict) and row.get("id") == reminder_id:
                    found = True
                else:
                    remaining.append(raw)
            return (remaining, True) if found else (None, False)

        _, duplicate, result = await self.cache.atomic_replace_list_once(
            self._key(owner), daily_idempotency_key(owner), mutation_key,
            DAILY_IDEMPOTENCY_MAX_KEYS, update,
        )
        return duplicate, bool(result)

    async def delete(self, owner: str, reminder_id: str) -> bool:
        rows = await self.list(owner)
        remaining = [row for row in rows if row.get("id") != reminder_id]
        if len(remaining) == len(rows):
            return False
        await self._write(owner, remaining)
        return True

    async def _write(self, owner: str, rows: list[dict]) -> None:
        pipe = self.cache.client.pipeline(transaction=True)
        pipe.delete(self._key(owner))
        for row in rows:
            pipe.rpush(self._key(owner), json.dumps(row))
        await pipe.execute()


class IdempotencyStore:
    """Executed-mutation keys, bounded (plan section 24.2)."""

    def __init__(self, cache: RedisCache) -> None:
        self.cache = cache

    def _key(self, owner: str) -> str:
        return daily_idempotency_key(owner)

    async def seen(self, owner: str, key: str) -> bool:
        return key in await self.cache.get_rows(self._key(owner))

    async def record(self, owner: str, key: str) -> None:
        pipe = self.cache.client.pipeline(transaction=True)
        pipe.rpush(self._key(owner), key)
        pipe.ltrim(self._key(owner), -DAILY_IDEMPOTENCY_MAX_KEYS, -1)
        await pipe.execute()

# -- schemas -----------------------------------------------------------------


def daily_tool_schemas(*, web_enabled: bool, schedule_available: bool, user_schedule_available: bool) -> list[dict]:
    """Schemas for the tools available this deployment (plan section 24.2)."""
    schemas: list[dict] = [
        {
            "type": "function",
            "function": {
                "name": "get_now",
                "description": "Current server time in a timezone (private helper).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "timezone": {"type": "string", "description": "IANA timezone, default server character timezone"},
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "calculate",
                "description": "Evaluate a pure arithmetic expression, e.g. 2*(3+4)/5.",
                "parameters": {
                    "type": "object",
                    "properties": {"expression": {"type": "string"}},
                    "required": ["expression"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "convert_units",
                "description": "Convert length (m/km/mi/ft/cm), mass (kg/g/lb/oz), or temperature (c/f/k).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "value": {"type": "number"},
                        "from_unit": {"type": "string"},
                        "to_unit": {"type": "string"},
                    },
                    "required": ["value", "from_unit", "to_unit"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "plan_span",
                "description": "End time of a span: start HH:MM plus duration in minutes.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "start": {"type": "string", "description": "HH:MM"},
                        "duration_minutes": {"type": "integer"},
                    },
                    "required": ["start", "duration_minutes"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "reminder_create",
                "description": "Store a durable reminder note for the owner (not an alarm).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string"},
                        "due_ts": {"type": "number", "description": "Unix seconds"},
                        "timezone": {"type": "string"},
                    },
                    "required": ["text", "due_ts", "timezone"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "reminder_update",
                "description": "Update an existing stored reminder by id.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "text": {"type": "string"},
                        "due_ts": {"type": "number", "description": "Unix seconds"},
                        "timezone": {"type": "string"},
                    },
                    "required": ["id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "reminder_list",
                "description": "List the owner's stored reminders.",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "reminder_delete",
                "description": "Delete one stored reminder by id.",
                "parameters": {
                    "type": "object",
                    "properties": {"id": {"type": "string"}},
                    "required": ["id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "memory_search",
                "description": "Look up durable long-term memories by meaning.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "kinds": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Optional kind filter",
                        },
                    },
                    "required": ["query"],
                },
            },
        },
    ]
    if schedule_available:
        schemas.append({
            "type": "function",
            "function": {
                "name": "character_schedule_read",
                "description": "Read the character's current and today's schedule blocks.",
                "parameters": {"type": "object", "properties": {}},
            },
        })
    if user_schedule_available:
        schemas.extend([
            {
                "type": "function",
                "function": {
                    "name": "owner_schedule_read",
                    "description": "Read the owner's contextual schedule state for now.",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "owner_schedule_update",
                    "description": (
                        "Replace one weekday of the owner's schedule. Only when "
                        "the owner explicitly asked in this message."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "day": {"type": "string", "description": "mon..sun"},
                            "blocks": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "start": {"type": "string"},
                                        "end": {"type": "string"},
                                        "state": {"type": "string", "description": "busy|free|sleep|unknown"},
                                    },
                                    "required": ["start", "end", "state"],
                                },
                            },
                        },
                        "required": ["day", "blocks"],
                    },
                },
            },
        ])
    if web_enabled:
        schemas.extend([
            {
                "type": "function",
                "function": {
                    "name": "web_search",
                    "description": "Search the public web; returns titled result snippets.",
                    "parameters": {
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                        "required": ["query"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "web_open",
                    "description": "Open a public HTTPS page and return its extracted text.",
                    "parameters": {
                        "type": "object",
                        "properties": {"url": {"type": "string"}},
                        "required": ["url"],
                    },
                },
            },
        ])
    return schemas


# -- executor -----------------------------------------------------------------


@dataclass
class ToolContext:
    """Everything a daily-tool call may touch for one turn."""

    owner: str
    user_text: str
    turn_id: str
    tool_call_id: str
    reminders: ReminderStore
    idempotency: IdempotencyStore
    web: object | None = None
    longterm: object | None = None
    schedule: object | None = None
    user_schedule: object | None = None
    character_timezone: str = "UTC"
    calls_used: dict = field(default_factory=dict)


class DailyToolExecutor:
    """Structured dispatch; tool errors are values, never exceptions."""

    def __init__(self, config: Config) -> None:
        self.config = config

    async def execute(self, name: str, arguments: dict, ctx: ToolContext) -> dict:
        try:
            result = await self._execute(name, arguments or {}, ctx)
        except Exception as exc:  # noqa: BLE001 - tool boundary (plan 24.2)
            log.warning("Daily tool %s failed: %s", name, type(exc).__name__)
            result = {"ok": False, "error": "tool_failed"}
        return _cap_result(result)

    async def _execute(self, name: str, args: dict, ctx: ToolContext) -> dict:
        if name == "get_now":
            return self._get_now(args, ctx)
        if name == "calculate":
            return self._calculate(args)
        if name == "convert_units":
            return self._convert(args)
        if name == "plan_span":
            return self._plan_span(args)
        if name == "reminder_create":
            return await self._reminder_create(args, ctx)
        if name == "reminder_update":
            return await self._reminder_update(args, ctx)
        if name == "reminder_list":
            return {"ok": True, "reminders": await ctx.reminders.list(ctx.owner)}
        if name == "reminder_delete":
            return await self._reminder_delete(args, ctx)
        if name == "memory_search":
            return await self._memory_search(args, ctx)
        if name == "character_schedule_read":
            return await self._character_schedule_read(ctx)
        if name == "owner_schedule_read":
            return await self._owner_schedule_read(ctx)
        if name == "owner_schedule_update":
            return await self._owner_schedule_update(args, ctx)
        if name == "web_search":
            return await self._web("search", ctx, query=str(args.get("query", "")))
        if name == "web_open":
            return await self._web("open", ctx, url=str(args.get("url", "")))
        return {"ok": False, "error": "unknown_tool"}

    # -- deterministic helpers ------------------------------------------------

    def _get_now(self, args: dict, ctx: ToolContext) -> dict:
        from datetime import datetime
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        tz_name = str(args.get("timezone") or "").strip() or ctx.character_timezone
        try:
            zone = ZoneInfo(tz_name)
        except (ZoneInfoNotFoundError, ValueError, KeyError):
            return {"ok": False, "error": "unknown_timezone", "needs_clarification": True}
        now = datetime.now(zone)
        return {
            "ok": True,
            "iso": now.isoformat(),
            "timezone": tz_name,
            "weekday": now.strftime("%A"),
        }

    def _calculate(self, args: dict) -> dict:
        expression = str(args.get("expression", "")).strip()
        if not expression or len(expression) > 200:
            return {"ok": False, "error": "invalid_expression"}
        try:
            value = safe_arithmetic(expression)
        except (ValueError, SyntaxError):
            return {"ok": False, "error": "invalid_expression"}
        return {"ok": True, "value": value}

    def _convert(self, args: dict) -> dict:
        try:
            value = float(args.get("value"))
        except (TypeError, ValueError):
            return {"ok": False, "error": "invalid_value"}
        return convert_units(value, str(args.get("from_unit", "")), str(args.get("to_unit", "")))

    def _plan_span(self, args: dict) -> dict:
        match = re.fullmatch(r"(\d{1,2}):(\d{2})", str(args.get("start", "")).strip())
        duration = args.get("duration_minutes")
        if not match or not isinstance(duration, (int, float)) or duration < 0:
            return {"ok": False, "error": "invalid_input", "needs_clarification": True}
        start_minutes = int(match.group(1)) * 60 + int(match.group(2))
        if int(match.group(1)) > 23 or int(match.group(2)) > 59:
            return {"ok": False, "error": "invalid_input", "needs_clarification": True}
        end_minutes = start_minutes + int(duration)
        wrapped = end_minutes >= 1440
        end_minutes = end_minutes % 1440
        return {
            "ok": True,
            "start": f"{int(match.group(1)):02d}:{match.group(2)}",
            "end": f"{end_minutes // 60:02d}:{end_minutes % 60:02d}",
            "wraps_past_midnight": wrapped,
        }

    # -- mutations (idempotent, intent-gated) ------------------------------------

    def _idempotency_key(self, ctx: ToolContext, name: str) -> str:
        return f"{ctx.turn_id}:{ctx.tool_call_id}:{name}"

    def _check_intent(self, ctx: ToolContext, words: tuple[str, ...]) -> dict | None:
        if not mutation_intent_present(ctx.user_text, words):
            return {
                "ok": False,
                "error": "explicit_intent_required",
                "detail": "ask the owner to confirm this change in their own words",
            }
        return None

    async def _reminder_create(self, args: dict, ctx: ToolContext) -> dict:
        refusal = self._check_intent(ctx, REMINDER_INTENT_WORDS)
        if refusal is not None:
            return refusal
        text = str(args.get("text", "")).strip()
        due = args.get("due_ts")
        tz_name = str(args.get("timezone", "")).strip()
        if not text or not isinstance(due, (int, float)) or not tz_name:
            return {"ok": False, "error": "missing_material", "needs_clarification": True}
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(tz_name)
        except (ZoneInfoNotFoundError, ValueError, KeyError):
            return {"ok": False, "error": "unknown_timezone", "needs_clarification": True}
        duplicate, reminder = await ctx.reminders.create_once(
            ctx.owner, self._idempotency_key(ctx, "reminder_create"),
            text=text, due_ts=float(due), timezone_name=tz_name,
        )
        if duplicate:
            return {"ok": True, "duplicate": True}
        return {"ok": True, "reminder": reminder}

    async def _reminder_update(self, args: dict, ctx: ToolContext) -> dict:
        refusal = self._check_intent(ctx, REMINDER_INTENT_WORDS)
        if refusal is not None:
            return refusal
        reminder_id = str(args.get("id", "")).strip()
        supplied = {key for key in ("text", "due_ts", "timezone") if key in args}
        if not reminder_id or not supplied:
            return {"ok": False, "error": "missing_material", "needs_clarification": True}
        changes: dict = {}
        if "text" in supplied:
            text = str(args.get("text", "")).strip()
            if not text:
                return {"ok": False, "error": "missing_material", "needs_clarification": True}
            changes["text"] = text[:REMINDER_TEXT_MAX_CHARS]
        if "due_ts" in supplied:
            due = args.get("due_ts")
            if not isinstance(due, (int, float)):
                return {"ok": False, "error": "missing_material", "needs_clarification": True}
            changes["due_ts"] = float(due)
        if "timezone" in supplied:
            tz_name = str(args.get("timezone", "")).strip()
            if not tz_name:
                return {"ok": False, "error": "missing_material", "needs_clarification": True}
            from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

            try:
                ZoneInfo(tz_name)
            except (ZoneInfoNotFoundError, ValueError, KeyError):
                return {"ok": False, "error": "unknown_timezone", "needs_clarification": True}
            changes["timezone"] = tz_name
        duplicate, reminder = await ctx.reminders.update_once(
            ctx.owner, self._idempotency_key(ctx, "reminder_update"), reminder_id, changes
        )
        if duplicate:
            return {"ok": True, "duplicate": True}
        if reminder is None:
            return {"ok": False, "error": "not_found"}
        return {"ok": True, "reminder": reminder}

    async def _reminder_delete(self, args: dict, ctx: ToolContext) -> dict:
        refusal = self._check_intent(ctx, REMINDER_INTENT_WORDS)
        if refusal is not None:
            return refusal
        reminder_id = str(args.get("id", "")).strip()
        if not reminder_id:
            return {"ok": False, "error": "missing_material", "needs_clarification": True}
        duplicate, deleted = await ctx.reminders.delete_once(
            ctx.owner, self._idempotency_key(ctx, "reminder_delete"), reminder_id
        )
        if duplicate:
            return {"ok": True, "duplicate": True}
        return {"ok": deleted, "error": None if deleted else "not_found"}

    async def _owner_schedule_update(self, args: dict, ctx: ToolContext) -> dict:
        refusal = self._check_intent(ctx, SCHEDULE_INTENT_WORDS)
        if refusal is not None:
            return refusal
        if ctx.user_schedule is None:
            return {"ok": False, "error": "unavailable"}
        day = str(args.get("day", "")).strip().lower()
        blocks = args.get("blocks")
        if day not in ("mon", "tue", "wed", "thu", "fri", "sat", "sun") or not isinstance(blocks, list):
            return {"ok": False, "error": "invalid_input", "needs_clarification": True}
        clean_blocks = []
        for block in blocks[:24]:
            if not isinstance(block, dict):
                continue
            clean_blocks.append({
                "start": str(block.get("start", "")),
                "end": str(block.get("end", "")),
                "state": str(block.get("state", "unknown")),
            })
        try:
            updates = ctx.user_schedule.validate_patch({"days": {day: clean_blocks}})
        except Exception as exc:  # noqa: BLE001 - validation errors are values
            return {"ok": False, "error": "invalid_schedule", "detail": str(exc)[:200]}
        def update(raw: str | None) -> tuple[str, dict]:
            try:
                store = json.loads(raw) if raw else None
            except json.JSONDecodeError:
                store = None
            if not isinstance(store, dict):
                store = default_store(ctx.user_schedule.config.OWNER_TIMEZONE)
            store.setdefault("days", {}).update(updates["days"])
            return json.dumps(store), store

        _, duplicate, _ = await ctx.idempotency.cache.atomic_transform_value_once(
            user_schedule_key(ctx.owner), daily_idempotency_key(ctx.owner),
            self._idempotency_key(ctx, "owner_schedule_update"),
            DAILY_IDEMPOTENCY_MAX_KEYS, update,
        )
        if duplicate:
            return {"ok": True, "duplicate": True}
        return {"ok": True, "day": day, "blocks": clean_blocks}

    # -- reads ---------------------------------------------------------------------

    async def _memory_search(self, args: dict, ctx: ToolContext) -> dict:
        query = str(args.get("query", "")).strip()
        if not query:
            return {"ok": False, "error": "invalid_query"}
        kinds = args.get("kinds")
        if not isinstance(kinds, list):
            kinds = None
        if ctx.longterm is None:
            return {"ok": True, "memories": []}
        rows = await ctx.longterm.search(
            ctx.owner, query, kinds=kinds or None, limit=6
        )
        return {
            "ok": True,
            "memories": [
                {
                    "kind": row.get("kind"),
                    "text": str(row.get("text", ""))[:400],
                    "updated_ts": row.get("updated_ts"),
                }
                for row in rows
            ],
        }

    async def _character_schedule_read(self, ctx: ToolContext) -> dict:
        if ctx.schedule is None:
            return {"ok": False, "error": "unavailable"}
        peek = ctx.schedule.peek()
        return {"ok": True, "schedule": peek}

    async def _owner_schedule_read(self, ctx: ToolContext) -> dict:
        if ctx.user_schedule is None:
            return {"ok": False, "error": "unavailable"}
        from datetime import datetime, timezone

        block = await ctx.user_schedule.current_block(
            ctx.owner, datetime.now(timezone.utc)
        )
        return {"ok": True, "now": block}

    async def _web(self, action: str, ctx: ToolContext, **kwargs) -> dict:
        if ctx.web is None:
            return {"ok": False, "error": "web_disabled"}
        if action == "search":
            return await ctx.web.search(kwargs.get("query", ""))
        return await ctx.web.open(kwargs.get("url", ""))


def _cap_result(result: dict) -> dict:
    """Bounded structured tool result (plan section 24.2)."""
    try:
        encoded = json.dumps(result, ensure_ascii=False)
    except (TypeError, ValueError):
        return {"ok": False, "error": "unserializable_result"}
    if len(encoded) <= DAILY_TOOL_RESULT_MAX_CHARS:
        return result
    return {
        "ok": result.get("ok", True),
        "truncated": True,
        "summary": encoded[:DAILY_TOOL_RESULT_MAX_CHARS],
    }


__all__ = [
    "DailyToolExecutor",
    "IdempotencyStore",
    "ReminderStore",
    "ToolContext",
    "convert_units",
    "daily_tool_schemas",
    "mutation_intent_present",
    "safe_arithmetic",
    "sanitize_daily_reply",
]
