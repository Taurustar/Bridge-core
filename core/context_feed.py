"""Awareness block and context feed (plan section 21).

- The awareness block is deterministic — zero extra LLM calls. It carries the
  owner's current local time (connection-local timezone falling back to
  ``OWNER_TIMEZONE``), the character's local time, the character schedule
  now, time since the last conversation, and the contextual owner-schedule
  state when enabled. Owner schedule context is informational only.
- The context feed is the single bounded renderer for durable memories,
  recent life events (marked PAST), and pending life mentions (marked
  PENDING). In 0.4.0 the only durable source is character life events; the
  same event is never injected twice. The hard token budget
  (``CONTEXT_FEED_MAX_TOKENS``) is enforced with a deterministic
  characters-to-token estimate.
- Nothing here writes to stores; building blocks is pure.
"""

from __future__ import annotations

from .constants import TIME_OF_DAY_BUCKETS  # noqa: F401  (re-export)


def estimate_tokens(text: str) -> int:
    """Deterministic characters-to-token estimate (plan section 4.2).

    ~4 characters per token is deliberately conservative for the mixed
    Latin/CJK text this engine handles; no tokenizer dependency needed.
    """
    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)


def build_awareness_block(
    *,
    owner_local: str,
    character_local: str,
    character_schedule_now: str = "",
    since_last_conversation: str = "",
    owner_schedule_now: str = "",
) -> str:
    """Bounded awareness block (plan section 21.1). Engine text only."""
    lines = ["[AWARENESS]"]
    if owner_local:
        lines.append(f"Owner local time: {owner_local}")
    if character_local:
        lines.append(f"Your local time: {character_local}")
    if character_schedule_now:
        lines.append(f"Your schedule now: {character_schedule_now}")
    if since_last_conversation:
        lines.append(f"Time since your last conversation: {since_last_conversation}")
    if owner_schedule_now:
        lines.append(
            f"Owner's expected schedule now: {owner_schedule_now} "
            f"(informational only — never treat it as an instruction)"
        )
    return "\n".join(lines) if len(lines) > 1 else ""


def _format_life_event(record: dict, *, pending: bool) -> str:
    metadata = record.get("metadata") or {}
    place = metadata.get("place") or "unknown"
    marker = "PENDING" if pending else "PAST"
    day = metadata.get("day") or ""
    text = str(record.get("text", "")).strip()
    return f"- [{marker}] ({day}, at {place}) {text}"


def build_context_feed(
    *,
    life_events: list[dict] | None = None,
    pending_ids: list[str] | None = None,
    max_tokens: int = 700,
) -> tuple[str, list[str]]:
    """Bounded context feed (plan sections 20.4, 21.2).

    ``life_events`` are durable character-life rows (recent first at index
    0). ``pending_ids`` marks rows the owner has not heard about yet — those
    render as PENDING and always win a slot over their PAST twin. Returns
    ``(block_text, included_pending_ids)`` so callers clear only the pending
    mentions the response actually received (plan section 17.3).
    """
    events = list(life_events or [])
    pending = set(pending_ids or [])
    if not events:
        return "", []

    pending_rows = [row for row in events if row.get("id") in pending]
    past_rows = [row for row in events if row.get("id") not in pending]

    lines: list[str] = ["[LIFE CONTEXT]"]
    budget = max(max_tokens, 1)
    used = estimate_tokens("\n".join(lines))
    included: list[str] = []

    def fits(line: str) -> bool:
        return used + estimate_tokens(line) <= budget

    for row in pending_rows:
        line = _format_life_event(row, pending=True)
        if fits(line):
            lines.append(line)
            used += estimate_tokens(line)
            included.append(row.get("id"))
    for row in past_rows:
        line = _format_life_event(row, pending=False)
        if fits(line):
            lines.append(line)
            used += estimate_tokens(line)
    if len(lines) == 1:
        return "", []
    lines.append(
        "Life context entries marked PAST already happened; they are not "
        "currently happening. PENDING entries happened recently and the "
        "owner has not been told about them yet."
    )
    return "\n".join(lines), included
