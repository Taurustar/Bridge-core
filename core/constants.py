"""Bridge Core Engine constants — single source for version and wire contracts."""

from __future__ import annotations

VERSION = "0.4.0"

# ---------------------------------------------------------------------------
# Emotion palette (plan section 13.1). The v1 palette is fixed for wire
# compatibility; adding/removing/renaming a name requires a manifest
# protocol-version change.
# ---------------------------------------------------------------------------

FINAL_EMOTIONS: tuple[str, ...] = (
    "neutral",
    "happy",
    "sad",
    "angry",
    "annoyed",
    "embarrassed",
    "surprised",
    "confused",
    "worried",
    "scared",
    "tired",
    "sleepy",
    "excited",
    "playful",
    "affectionate",
    "confident",
    "serious",
    "shy",
)

STATUS_EMOTIONS: tuple[str, ...] = (
    "thinking",
    "working",
    "question",
    "request_permission",
)

# Status name -> emotion mapping (plan section 13.3). Single source.
STATUS_TO_EMOTION: dict[str, str] = {
    "thinking": "thinking",
    "planning": "thinking",
    "working": "working",
    "question": "question",
    "request_permission": "request_permission",
    "completed": "confident",
    "unavailable": "neutral",
    "error": "serious",
}

DEFAULT_EMOTION = "neutral"

# Supported reply languages (plan section 7.4).
SUPPORTED_LANGUAGES: tuple[str, ...] = ("en", "es", "ja")

# Owner lived-profile relationship statuses (plan section 18.2). Romance-specific
# labels may remain unused; code never promotes status without configured
# evidence.
OWNER_RELATIONSHIP_STATUSES: tuple[str, ...] = (
    "partner",
    "dating",
    "close_friend",
    "friend",
    "acquaintance",
    "distant",
    "estranged",
)

# Agreement kinds (plan section 18.5). Intimacy kinds are excluded forever.
AGREEMENT_KINDS: tuple[str, ...] = (
    "routine",
    "care",
    "boundary",
    "work_support",
    "other",
)

# Agreement schedules are reminder/evaluation windows only (plan 18.5).
AGREEMENT_SCHEDULE_TYPES: tuple[str, ...] = ("standing", "weekly", "once")

# A persona-tension agreement requires these minimum floors (plan 18.5).
AGREEMENT_TENSION_TRUST_FLOOR = 50
AGREEMENT_TENSION_CLOSENESS_FLOOR = 40

# Maximum number of active agreements (plan section 18.5).
AGREEMENT_MAX_ACTIVE = 12

# Bid kinds (plan section 15.5). Registration arrives with initiative (0.7.0).
BID_KINDS: tuple[str, ...] = (
    "initiative_life",
    "initiative_bond",
    "initiative_fun",
    "initiative_thread",
)

# LLM modes known to the router (plan section 8.2).
LLM_MODES: tuple[str, ...] = (
    "companion",
    "work",
    "life",
    "proactive",
    "owner_profile",
    "memory",
    "session_summary",
)

# Supported LLM providers (plan section 9.1).
LLM_PROVIDERS: tuple[str, ...] = ("fireworks", "chutes", "ollama", "openai_compat")

# Heartbeat timestamp sanity bounds (plan section 10.8).
HEARTBEAT_MAX_FUTURE_SECONDS = 60
HEARTBEAT_MAX_AGE_SECONDS = 600

# The initiative engine ships in milestone 0.7.0; the heartbeat ack carries a
# constant counter until then (documented in BRIDGE_CORE_ENGINE_SPEC.md).
INITIATIVE_COUNTER_STUB = 0

# ---------------------------------------------------------------------------
# Redis keys (plan section 28). Milestones 0.1.0-0.2.0 may create exactly one
# key family: companion history. Speech is never persisted server-side; audio
# bytes exist only in flight. Everything else belongs to later milestones and
# must not be written while their feature flags are off.
# ---------------------------------------------------------------------------


def companion_history_key(owner_user_id: str) -> str:
    """The only Redis key milestones 0.1.0-0.2.0 create."""
    return f"core:history:{owner_user_id}:companion"


def needs_key(owner_user_id: str) -> str:
    return f"core:needs:{owner_user_id}"


def bids_key(owner_user_id: str) -> str:
    return f"core:bids:{owner_user_id}"


def rhythm_key(owner_user_id: str) -> str:
    return f"core:rhythm:{owner_user_id}"


def owner_profile_key(owner_user_id: str) -> str:
    return f"core:owner_profile:{owner_user_id}"


# Owner-profile admin mistakes are guarded by this literal token (plan 18.7).
# It is an obvious-to-a-human string, not a secret; it is never logged at INFO.
UPDATE_OWNER_PROFILE_TOKEN = "UPDATE_OWNER_PROFILE"

# Confirm tokens (plan section 29). Mistake guards, not authentication.
GENERATE_LIFE_TOKEN = "GENERATE_LIFE"
RELOAD_SCHEDULE_TOKEN = "RELOAD_SCHEDULE"
UPDATE_USER_SCHEDULE_TOKEN = "UPDATE_USER_SCHEDULE"

# ---------------------------------------------------------------------------
# Schedule availability ladder (plan sections 16.1, 16.3).
# ---------------------------------------------------------------------------

AVAILABILITIES: tuple[str, ...] = ("free", "soft_busy", "busy", "unavailable")

# Safe default block for schedule gaps (plan section 16.2). Synthetic: life
# events never generate on it and it carries no authored block id.
GAP_BLOCK: dict[str, object] = {
    "place": "unknown",
    "activity": "unplanned",
    "availability": "free",
    "tags": [],
}

# Contextual owner-schedule states (plan section 22.3). Unknown is not free.
USER_SCHEDULE_STATES: tuple[str, ...] = ("busy", "free", "sleep", "unknown")

# Deferred queue bounds (plan section 16.3).
DEFERRED_MAX_ENTRIES = 5
DEFERRED_MAX_TOTAL_CHARS = 4000
DEFERRED_TTL_SECONDS = 48 * 3600

# Time-of-day buckets used by life-template matching (plan section 17.1).
TIME_OF_DAY_BUCKETS: tuple[str, ...] = ("morning", "afternoon", "evening", "night")


def deferred_key(owner_user_id: str) -> str:
    return f"core:deferred:{owner_user_id}"


def busy_count_key(owner_user_id: str) -> str:
    return f"core:busy_count:{owner_user_id}"


def longterm_key(owner_user_id: str) -> str:
    return f"core:longterm:{owner_user_id}"


def life_last_block_key(owner_user_id: str) -> str:
    return f"core:life:last_block:{owner_user_id}"


def life_pending_key(owner_user_id: str) -> str:
    return f"core:life:pending:{owner_user_id}"


def user_schedule_key(owner_user_id: str) -> str:
    return f"core:user_schedule:{owner_user_id}"


def user_schedule_day_key(owner_user_id: str, ymd: str) -> str:
    return f"core:user_schedule:day:{owner_user_id}:{ymd}"
