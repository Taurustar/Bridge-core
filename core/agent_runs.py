"""Work agent loop: tool iterations, pinning, verification (plan section 25).

OpenAI-style loop: assistant tool-call arrays, one tool response per
call, bounded iterations (``MCP_MAX_ITERATIONS``). The first successful
provider is pinned for subsequent iterations and unpinned on failure
(plan section 9.3). If the iteration limit is reached, one no-tools
synthesis runs. With verification enabled, successful writes must be
followed by a read-back or check; unverified writes trigger a bounded
forced follow-up. Failed tools are evidence, never silent successes.

The loop never raises for tool failures: every tool call resolves to a
structured result. Only LLM-chain exhaustion propagates.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from .llm import LLMChainExhausted, LLMResult, LLMRouter, ProviderRoute
from .work_tools import WorkToolRegistry, bound_tool_result, classify_tool

log = logging.getLogger("bridge.agent")

Executor = Callable[[str, dict], Awaitable[dict]]

MAX_ITERATION_NOTE = (
    "[AGENT LIMIT] Maximum tool iterations reached. Produce your final "
    "reply now without calling any tools. State honestly what was "
    "completed and what was not."
)
VERIFICATION_NOTE = (
    "[VERIFICATION REQUIRED] The following writes were not followed by a "
    "read-back or check: {writes}. Use available read/check tools to "
    "verify them now, or state clearly in your reply what remains "
    "unverified. Never claim completion without evidence."
)


@dataclass
class AgentLoopResult:
    text: str = ""
    provider: str = ""
    model: str = ""
    attempts: int = 0
    usage: dict[str, int] = field(default_factory=dict)
    iterations: int = 0
    tool_calls_made: int = 0
    evidence: list[dict] = field(default_factory=list)  # {tool, ok, truncated}
    unverified_writes: list[dict] = field(default_factory=list)
    transcript: list[dict] = field(default_factory=list)
    hit_iteration_limit: bool = False
    rejected_tool_calls: bool = False


def _merge_usage(total: dict[str, int], result: LLMResult) -> dict[str, int]:
    if result.usage:
        for key, value in result.usage.items():
            total[key] = total.get(key, 0) + int(value)
    return total


def _unverified_writes(writes: list[dict], evidence: list[dict]) -> list[dict]:
    """Writes with no later read/check touching the same tool family."""
    verified_after: list[dict] = []
    for write in writes:
        verified = False
        for event in evidence:
            if event["index"] <= write["index"]:
                continue
            if event["kind"] in ("check", "read") and (
                event["tool"] == write["tool"]
                or event.get("path") == write.get("path")
            ):
                verified = True
                break
        if not verified:
            verified_after.append(write)
    return verified_after


def _write_summary(calls: list[dict]) -> str:
    parts = []
    for call in calls[:6]:
        path = call.get("path") or ""
        name = call.get("tool") or call.get("name") or ""
        parts.append(f"{name}({path})" if path else name)
    return ", ".join(parts)


async def run_agent_loop(
    llm: LLMRouter,
    *,
    messages: list[dict],
    registry: WorkToolRegistry,
    executor: Executor,
    max_iterations: int,
    verification_enabled: bool,
    verification_retries: int,
    reject_tool_calls: bool = False,
) -> AgentLoopResult:
    """Execute one bounded tool loop. Only LLMChainExhausted propagates."""
    transcript: list[dict] = [dict(message) for message in messages]
    result = AgentLoopResult(transcript=transcript)
    pinned: ProviderRoute | None = None
    evidence: list[dict] = []
    writes: list[dict] = []
    tool_calls_made = 0
    hit_limit = False

    tools = registry.schemas if registry.has_tools else None

    async def execute_and_record(name: str, arguments: dict) -> dict:
        nonlocal tool_calls_made
        tool_calls_made += 1
        if name not in registry.known:
            # The current turn's schemas are the execution authority
            # (plan section 25.3): anything else is a structured failure.
            tool_result = {
                "ok": False, "error": "unknown_tool", "result": None,
                "truncated": False,
            }
        else:
            tool_result = await executor(name, arguments)
        ok = bool(tool_result.get("ok"))
        kind = classify_tool(name)
        entry = {
            "tool": name,
            "kind": kind,
            "ok": ok,
            "truncated": bool(tool_result.get("truncated")),
            "index": len(transcript),
            "path": (arguments or {}).get("path")
            or (arguments or {}).get("root")
            or "",
        }
        evidence.append(entry)
        if kind == "write" and ok:
            writes.append(entry)
        return tool_result

    try:
        for _iteration in range(max(1, max_iterations)):
            result.iterations += 1
            reply = await llm.chat("work", transcript, tools=tools, pinned=pinned)
            pinned = ProviderRoute(provider=reply.provider, model=reply.model)
            _merge_usage(result.usage, reply)
            result.attempts += reply.attempts
            result.provider = reply.provider
            result.model = reply.model

            if not reply.tool_calls:
                result.text = reply.text
                break

            if reject_tool_calls:
                result.tool_calls_made = len(reply.tool_calls)
                result.rejected_tool_calls = True
                break

            transcript.append(
                {
                    "role": "assistant",
                    "content": reply.text or "",
                    "tool_calls": [
                        {
                            "id": call["id"],
                            "type": "function",
                            "function": {
                                "name": call["name"],
                                "arguments": call["arguments"],
                            },
                        }
                        for call in reply.tool_calls
                    ],
                }
            )
            for call in reply.tool_calls:
                try:
                    arguments = json.loads(call["arguments"] or "{}")
                    if not isinstance(arguments, dict):
                        arguments = {}
                except json.JSONDecodeError:
                    arguments = {}
                    tool_result = {
                        "ok": False, "error": "invalid_arguments_json",
                        "result": None, "truncated": False,
                    }
                    evidence.append({
                        "tool": call["name"], "kind": "other", "ok": False,
                        "truncated": False, "index": len(transcript),
                        "path": "",
                    })
                else:
                    tool_result = await execute_and_record(call["name"], arguments)
                transcript.append(
                    {
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "content": bound_tool_result(tool_result),
                    }
                )
        else:
            hit_limit = True
            log.info("Agent loop hit the iteration limit; forcing synthesis")
            transcript.append(
                {"role": "system", "content": MAX_ITERATION_NOTE}
            )
            reply = await llm.chat("work", transcript, tools=None, pinned=pinned)
            _merge_usage(result.usage, reply)
            result.attempts += reply.attempts
            result.text = reply.text
    except LLMChainExhausted:
        # A pinned route failed mid-loop: unpin once and retry the chain.
        if pinned is not None and result.iterations > 1:
            log.info("Pinned provider failed; retrying final step unpinned")
            reply = await llm.chat("work", transcript, tools=None, pinned=None)
            _merge_usage(result.usage, reply)
            result.attempts += reply.attempts
            result.text = reply.text
        else:
            raise

    result.hit_iteration_limit = hit_limit
    result.evidence = [
        {key: event[key] for key in ("tool", "kind", "ok", "truncated")}
        for event in evidence
    ]

    # Verification pass (plan section 25.5): bounded forced follow-up.
    if verification_enabled and registry.has_tools and writes:
        unverified = _unverified_writes(writes, evidence)
        attempts = 0
        while unverified and attempts < max(0, verification_retries):
            attempts += 1
            result.iterations += 1
            transcript.append(
                {
                    "role": "system",
                    "content": VERIFICATION_NOTE.format(
                        writes=_write_summary(unverified)
                    ),
                }
            )
            reply = await llm.chat(
                "work", transcript, tools=tools, pinned=pinned
            )
            _merge_usage(result.usage, reply)
            result.attempts += reply.attempts
            if not reply.tool_calls:
                result.text = reply.text
                break
            transcript.append(
                {
                    "role": "assistant",
                    "content": reply.text or "",
                    "tool_calls": [
                        {
                            "id": call["id"],
                            "type": "function",
                            "function": {
                                "name": call["name"],
                                "arguments": call["arguments"],
                            },
                        }
                        for call in reply.tool_calls
                    ],
                }
            )
            for call in reply.tool_calls:
                try:
                    arguments = json.loads(call["arguments"] or "{}")
                except json.JSONDecodeError:
                    arguments = {}
                if not isinstance(arguments, dict):
                    arguments = {}
                tool_result = await execute_and_record(call["name"], arguments)
                transcript.append(
                    {
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "content": bound_tool_result(tool_result),
                    }
                )
            unverified = _unverified_writes(writes, evidence)
        result.unverified_writes = [
            {"tool": write["tool"], "path": write["path"]}
            for write in unverified
        ]
    result.tool_calls_made = tool_calls_made
    return result


def checkpoint_from_result(
    *,
    run_id: str,
    session_id: str,
    state: str,
    loop: AgentLoopResult | None,
    last_error: str = "",
) -> dict:
    """Bounded run/checkpoint record (plan section 25.7)."""
    from .constants import TRANSCRIPT_TAIL_CHARS

    record: dict[str, Any] = {
        "run_id": run_id,
        "session_id": session_id,
        "state": state,
        "iteration": loop.iterations if loop else 0,
        "tool_calls": loop.tool_calls_made if loop else 0,
        "evidence": (loop.evidence if loop else [])[-16:],
        "unverified_writes": (loop.unverified_writes if loop else []),
        "last_error": last_error[:300],
        "provider": loop.provider if loop else "",
        "model": loop.model if loop else "",
        "started_ts": 0.0,
        "updated_ts": time.time(),
    }
    if loop is not None and loop.transcript:
        tail = json.dumps(loop.transcript[-6:], ensure_ascii=False, default=str)
        record["transcript_tail"] = tail[-TRANSCRIPT_TAIL_CHARS:]
    else:
        record["transcript_tail"] = ""
    return record
