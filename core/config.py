"""Configuration system (plan section 8).

One ``Config`` dataclass. ``Config.from_env()`` auto-loads ``core.env`` with
``os.environ.setdefault`` semantics: real environment variables always win
over file values. Invalid numeric/boolean values fail startup with a clear
message instead of silently defaulting.

``Config.apply(other)`` hot-reloads every non-structural field. Structural
(restart-required) fields are: BRIDGE_HOST, BRIDGE_PORT, REDIS_HOST,
REDIS_PORT, REDIS_DB, CHROMA_PATH, CHROMA_REQUIRED.

Secrets are never exposed through /status (see ``core.app``).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import dotenv_values

from .constants import OWNER_RELATIONSHIP_STATUSES, SUPPORTED_LANGUAGES

STRUCTURAL_FIELDS: frozenset[str] = frozenset(
    {
        "BRIDGE_HOST",
        "BRIDGE_PORT",
        "REDIS_HOST",
        "REDIS_PORT",
        "REDIS_DB",
        "CHROMA_PATH",
        "CHROMA_REQUIRED",
    }
)

_TRUE = {"true", "1", "yes", "on"}
_FALSE = {"false", "0", "no", "off"}


class ConfigError(ValueError):
    """Raised when configuration cannot be parsed or validated."""


def _str_allow_empty(env: dict[str, str], name: str, default: str) -> str:
    value = env.get(name)
    if value is None:
        return default
    return value


def _int(env: dict[str, str], name: str, default: int) -> int:
    value = env.get(name)
    if value is None or value == "":
        return default
    try:
        return int(value)
    except ValueError:
        raise ConfigError(
            f"Invalid numeric value for {name}: {value!r} (expected an integer)"
        ) from None


def _float(env: dict[str, str], name: str, default: float) -> float:
    value = env.get(name)
    if value is None or value == "":
        return default
    try:
        return float(value)
    except ValueError:
        raise ConfigError(
            f"Invalid numeric value for {name}: {value!r} (expected a number)"
        ) from None


def _bool(env: dict[str, str], name: str, default: bool) -> bool:
    value = env.get(name)
    if value is None or value == "":
        return default
    lowered = value.strip().lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    raise ConfigError(
        f"Invalid boolean value for {name}: {value!r} (expected true/false)"
    )


def parse_chain(raw: str) -> list[str]:
    """Parse a comma-separated provider chain, dropping blanks."""
    return [part.strip() for part in raw.split(",") if part.strip()]


@dataclass
class Config:
    # Server
    BRIDGE_HOST: str = "127.0.0.1"
    BRIDGE_PORT: int = 8766
    TAILSCALE_REQUIRED: bool = True
    TAILSCALE_FIREWALL_ACK: bool = False
    LOG_LEVEL: str = "info"
    OWNER_USER_ID: str = "owner"
    DEFAULT_LANGUAGE: str = "en"
    OWNER_TIMEZONE: str = "UTC"
    CHARACTER_TIMEZONE: str = "UTC"

    # Redis and Chroma
    REDIS_HOST: str = "127.0.0.1"
    REDIS_PORT: int = 6379
    REDIS_DB: int = 0
    CHROMA_ENABLED: bool = False
    CHROMA_PATH: str = "./data/chroma"
    CHROMA_REQUIRED: bool = False

    # LLM routing
    LLM_CHAIN: str = "fireworks,chutes,ollama,openai_compat"
    LLM_CHAIN_DEADLINE_SECONDS: float = 75.0
    LLM_HISTORY_MESSAGE_BUDGET: int = 40
    LLM_STREAMING_ENABLED: bool = False

    FIREWORKS_API_KEY: str = ""
    FIREWORKS_URL: str = "https://api.fireworks.ai/inference/v1"
    FIREWORKS_MODEL: str = ""
    FIREWORKS_TIMEOUT: float = 60.0
    FIREWORKS_SERVICE_TIER: str = ""

    CHUTES_API_KEY: str = ""
    CHUTES_URL: str = ""
    CHUTES_MODEL: str = ""
    CHUTES_TIMEOUT: float = 60.0

    OLLAMA_URL: str = "http://127.0.0.1:11434"
    OLLAMA_MODEL: str = ""
    OLLAMA_TIMEOUT: float = 0.0

    OPENAI_COMPAT_API_KEY: str = ""
    OPENAI_COMPAT_URL: str = ""
    OPENAI_COMPAT_MODEL: str = ""
    OPENAI_COMPAT_TIMEOUT: float = 60.0

    COMPANION_PROVIDERS: str = ""
    COMPANION_MODEL: str = ""
    COMPANION_TEMPERATURE: float = 0.8
    COMPANION_MAX_TOKENS: int = 1200

    WORK_PROVIDERS: str = ""
    WORK_MODEL: str = ""
    WORK_TEMPERATURE: float = 0.3
    WORK_MAX_TOKENS: int = 4000

    LIFE_PROVIDERS: str = ""
    LIFE_MODEL: str = ""
    LIFE_TEMPERATURE: float = 0.8
    LIFE_MAX_TOKENS: int = 400

    PROACTIVE_PROVIDERS: str = ""
    PROACTIVE_MODEL: str = ""
    PROACTIVE_TEMPERATURE: float = 0.8
    PROACTIVE_MAX_TOKENS: int = 120

    OWNER_PROFILE_LLM_ENABLED: bool = False
    OWNER_PROFILE_PROVIDERS: str = ""
    OWNER_PROFILE_MODEL: str = ""
    OWNER_PROFILE_MAX_TOKENS: int = 400

    # Identity
    SOUL_FILE: str = ""
    PROFILE_FILE: str = ""
    STATE_FILE: str = ""
    WORK_SKILLS_FILE: str = ""
    DAILY_SKILLS_FILE: str = ""

    # TTS
    TTS_ENABLED: bool = False
    ELEVENLABS_API_KEY: str = ""
    ELEVENLABS_URL: str = "https://api.elevenlabs.io/v1"
    ELEVENLABS_VOICE_ID: str = ""
    ELEVENLABS_MODEL: str = "eleven_flash_v2_5"
    TTS_OUTPUT_FORMAT: str = "mp3_44100_128"
    TTS_CHUNK_THRESHOLD: int = 150
    TTS_CHUNK_SIZE: int = 150
    TTS_CHUNK_SPACING_MS: int = 50
    TTS_VOICE_PROFILE_FILE: str = ""

    # STT
    STT_ENABLED: bool = False
    STT_PROVIDER: str = "deepgram"
    STT_LANGUAGE: str = "en"
    DEEPGRAM_API_KEY: str = ""
    DEEPGRAM_URL: str = "https://api.deepgram.com/v1/listen"
    DEEPGRAM_MODEL: str = "nova-3"
    DEEPGRAM_TIMEOUT: float = 45.0
    ASSEMBLYAI_API_KEY: str = ""
    ASSEMBLYAI_URL: str = "https://api.assemblyai.com/v2"
    ASSEMBLYAI_SPEECH_MODEL: str = "best"
    ASSEMBLYAI_TIMEOUT: float = 90.0
    ASSEMBLYAI_POLL_INTERVAL: float = 1.0

    # History and memory
    MAX_HISTORY_TURNS: int = 80
    COMPANION_SHORT_WINDOW_ENABLED: bool = True
    COMPANION_LIVE_WINDOW_MESSAGES: int = 8
    COMPANION_COMPACT_THRESHOLD: int = 60
    COMPANION_KEEP_RECENT: int = 16
    MIDTERM_INJECT_CHAPTERS: int = 4
    MEMORY_EXTRACTION_ENABLED: bool = False
    MEMORY_CLEANUP_ENABLED: bool = False
    MEMORY_CLEANUP_INTERVAL_HOURS: int = 12
    MEMORY_MAX_PER_USER: int = 1000

    # Needs and interaction
    NEEDS_ENABLED: bool = False
    BIDS_ENABLED: bool = False
    RHYTHM_ENABLED: bool = False
    STATE_EXPRESSION_ENABLED: bool = False
    NEEDS_PROFILE_FILE: str = ""

    # Owner profile
    OWNER_PROFILE_ENABLED: bool = False
    OWNER_PROFILE_INJECT: bool = True
    OWNER_STATUS_START: str = "acquaintance"
    OWNER_TRUST_START: int = 50
    OWNER_CLOSENESS_START: int = 0
    OWNER_APPEAL_START: int = 50
    OWNER_DESIRABILITY_START: int = 50
    OWNER_BOUNDARY_PENALTIES_ENABLED: bool = False
    OWNER_SOFT_BLOCK_ENABLED: bool = False
    OWNER_SOFT_BLOCK_COOLDOWN_SECONDS: int = 3600
    OWNER_SOFT_BLOCK_UNBLOCK_TRUST_FLOOR: int = 25
    OWNER_BOUNDARY_PENALTY_MINOR: float = 3.0
    OWNER_BOUNDARY_PENALTY_MODERATE: float = 6.0
    OWNER_BOUNDARY_PENALTY_MAJOR: float = 12.0
    OWNER_STATUS_DRIFT_ENABLED: bool = False
    OWNER_AGREEMENTS_ENABLED: bool = False
    OWNER_AGREEMENT_AFTERMATH_ENABLED: bool = False

    # Dormant gateway profiles
    EXTERNAL_USER_PROFILE_STORE_ENABLED: bool = True
    EXTERNAL_USER_PROFILES_BEHAVIOR_ENABLED: bool = False
    EXTERNAL_USER_PROFILE_LLM_ENABLED: bool = False

    # Schedule and life
    SCHEDULE_ENABLED: bool = False
    SCHEDULE_DIR: str = ""
    LIFE_ENABLED: bool = False
    LIFE_EVENTS_DIR: str = ""
    LIFE_DAILY_MIN: int = 0
    LIFE_DAILY_MAX: int = 4
    LIFE_EVENT_COOLDOWN_MINUTES: int = 40
    LIFE_POLL_INTERVAL_SECONDS: int = 60
    LIFE_MISSED_BLOCK_POLICY: str = "current_only"
    LIFE_SKIP_ACTIVITIES: str = "sleep"

    # Heartbeat initiative
    HEARTBEAT_ENABLED: bool = True
    INITIATIVE_ENABLED: bool = False
    INITIATIVE_MIN_HEARTBEATS: int = 3
    INITIATIVE_HEARTBEAT_WINDOW_SECONDS: int = 900
    INITIATIVE_HEARTBEAT_COUNT_INTERVAL_SECONDS: int = 60
    INITIATIVE_MIN_GAP_SECONDS: int = 3600
    INITIATIVE_DAILY_MAX: int = 3
    INITIATIVE_REQUIRE_SCHEDULE_FREE: bool = True
    INITIATIVE_ELIGIBILITY_CHANCE: float = 0.35

    # Work and MCP
    WORK_ENABLED: bool = True
    SESSIONS_ENABLED: bool = True
    SESSION_HISTORY_TURNS: int = 80
    SESSION_SUMMARY_ENABLED: bool = True
    WORK_SKILLS_ENABLED: bool = True
    MCP_PROXY_ENABLED: bool = True
    MCP_TOOL_TIMEOUT: int = 120
    MCP_MAX_ITERATIONS: int = 20
    MCP_VERIFICATION_ENABLED: bool = True
    MCP_VERIFICATION_RETRIES: int = 2
    AGENT_CHECKPOINTS_ENABLED: bool = True

    # Device daemon
    DEVICE_ENABLED: bool = False
    DEVICE_TOOL_TIMEOUT: int = 120
    DEVICE_PER_TURN_CALL_CAP: int = 20
    DEVICE_MAX_OUTPUT_CHARS: int = 30000
    DEVICE_SHELL_TIMEOUT: int = 120
    DEVICE_SHELL_TIMEOUT_MAX: int = 600
    DEVICE_WRITE_ROOTS: str = ""

    # Daily tools and web
    DAILY_TOOLS_ENABLED: bool = False
    DAILY_WEB_ENABLED: bool = False
    TAVILY_API_KEY: str = ""
    DAILY_WEB_SEARCH_CAP: int = 1
    DAILY_WEB_OPEN_CAP: int = 2

    # Contextual owner schedule
    USER_SCHEDULE_ENABLED: bool = False
    SCHEDULE_SOFT_BUSY_POLICY: str = "normal"

    # Input and context budgets
    MAX_AUDIO_BYTES: int = 15728640
    ALLOWED_AUDIO_CONTENT_TYPES: str = "audio/webm,audio/ogg,audio/mpeg,audio/wav"
    CONTEXT_FEED_MAX_TOKENS: int = 700
    NEEDS_MAX_ELAPSED_HOURS: int = 48

    # Additional analysis routes
    MEMORY_PROVIDERS: str = ""
    MEMORY_MODEL: str = ""
    MEMORY_MAX_TOKENS: int = 400
    SESSION_SUMMARY_PROVIDERS: str = ""
    SESSION_SUMMARY_MODEL: str = ""
    SESSION_SUMMARY_MAX_TOKENS: int = 500

    # Client emotion/animation manifest
    EMOTIONS_FILE: str = ""
    STATIC_LINES_FILE: str = ""

    # Raw merged environment snapshot (file values overridden by real env).
    # Used for dynamic per-mode/per-provider overrides such as
    # COMPANION_FIREWORKS_MODEL (plan section 8.2) that are not listed
    # individually in the section 8.3 inventory.
    env: dict[str, str] = field(default_factory=dict, repr=False)

    @classmethod
    def from_env(
        cls,
        env_file: str | None = "core.env",
        environ: dict[str, str] | None = None,
    ) -> "Config":
        """Build config from the process environment plus ``core.env``.

        File values are applied with ``os.environ.setdefault`` semantics:
        variables already present in the real environment always win.
        """
        source = os.environ if environ is None else environ
        merged: dict[str, str] = {}
        if env_file:
            for key, value in dotenv_values(env_file).items():
                if value is not None:
                    merged.setdefault(key, value)
        for key, value in source.items():
            if value is not None:
                merged[key] = value  # real environment wins

        kwargs: dict[str, object] = {}
        for f in fields(cls):
            if f.name == "env":
                continue
            default = f.default
            if isinstance(default, bool):
                kwargs[f.name] = _bool(merged, f.name, default)
            elif isinstance(default, int):
                kwargs[f.name] = _int(merged, f.name, default)
            elif isinstance(default, float):
                kwargs[f.name] = _float(merged, f.name, default)
            else:
                kwargs[f.name] = _str_allow_empty(merged, f.name, default)
        kwargs["env"] = merged
        config = cls(**kwargs)  # type: ignore[arg-type]
        config.validate()
        return config

    def validate(self) -> None:
        """Cross-field validation with clear startup errors."""
        if not self.OWNER_USER_ID.strip():
            raise ConfigError("OWNER_USER_ID must not be empty")
        for tz_field in ("OWNER_TIMEZONE", "CHARACTER_TIMEZONE"):
            tz_name = getattr(self, tz_field)
            try:
                ZoneInfo(tz_name)
            except (ZoneInfoNotFoundError, ValueError, KeyError):
                raise ConfigError(
                    f"{tz_field} must be a valid IANA timezone (got {tz_name!r})"
                ) from None
        if not (1 <= self.BRIDGE_PORT <= 65535):
            raise ConfigError(
                f"BRIDGE_PORT out of range: {self.BRIDGE_PORT} (expected 1..65535)"
            )
        if self.LLM_CHAIN_DEADLINE_SECONDS <= 0:
            raise ConfigError("LLM_CHAIN_DEADLINE_SECONDS must be positive")
        if self.MAX_HISTORY_TURNS < 1:
            raise ConfigError("MAX_HISTORY_TURNS must be at least 1")
        self.DEFAULT_LANGUAGE = self.DEFAULT_LANGUAGE.strip().lower()
        if self.DEFAULT_LANGUAGE not in SUPPORTED_LANGUAGES:
            raise ConfigError(
                f"DEFAULT_LANGUAGE must be one of {', '.join(SUPPORTED_LANGUAGES)} "
                f"(got {self.DEFAULT_LANGUAGE!r})"
            )
        self.STT_PROVIDER = self.STT_PROVIDER.strip().lower()
        if self.STT_PROVIDER not in ("deepgram", "assemblyai"):
            raise ConfigError(
                f"STT_PROVIDER must be deepgram or assemblyai "
                f"(got {self.STT_PROVIDER!r})"
            )
        unknown = [
            name for name in parse_chain(self.LLM_CHAIN) if name not in _KNOWN_PROVIDERS
        ]
        if unknown:
            raise ConfigError(
                f"LLM_CHAIN contains unknown providers: {', '.join(unknown)}"
            )
        self.OWNER_STATUS_START = self.OWNER_STATUS_START.strip().lower()
        if self.OWNER_STATUS_START not in OWNER_RELATIONSHIP_STATUSES:
            raise ConfigError(
                f"OWNER_STATUS_START must be one of "
                f"{', '.join(OWNER_RELATIONSHIP_STATUSES)} "
                f"(got {self.OWNER_STATUS_START!r})"
            )
        for score_name in ("OWNER_TRUST_START", "OWNER_CLOSENESS_START",
                           "OWNER_APPEAL_START", "OWNER_DESIRABILITY_START"):
            if not (0 <= getattr(self, score_name) <= 100):
                raise ConfigError(f"{score_name} must be between 0 and 100")
        if self.OWNER_SOFT_BLOCK_COOLDOWN_SECONDS < 0:
            raise ConfigError("OWNER_SOFT_BLOCK_COOLDOWN_SECONDS must be >= 0")
        for penalty_name in ("OWNER_BOUNDARY_PENALTY_MINOR",
                             "OWNER_BOUNDARY_PENALTY_MODERATE",
                             "OWNER_BOUNDARY_PENALTY_MAJOR"):
            if getattr(self, penalty_name) <= 0:
                raise ConfigError(f"{penalty_name} must be positive")
        if self.OWNER_SOFT_BLOCK_UNBLOCK_TRUST_FLOOR < 0:
            raise ConfigError("OWNER_SOFT_BLOCK_UNBLOCK_TRUST_FLOOR must be >= 0")
        self.LIFE_MISSED_BLOCK_POLICY = self.LIFE_MISSED_BLOCK_POLICY.strip().lower()
        if self.LIFE_MISSED_BLOCK_POLICY not in ("current_only",):
            raise ConfigError(
                "LIFE_MISSED_BLOCK_POLICY must be current_only "
                f"(got {self.LIFE_MISSED_BLOCK_POLICY!r})"
            )
        if self.LIFE_DAILY_MIN < 0 or self.LIFE_DAILY_MAX < 0:
            raise ConfigError("LIFE_DAILY_MIN and LIFE_DAILY_MAX must be >= 0")
        if self.LIFE_DAILY_MIN > self.LIFE_DAILY_MAX:
            raise ConfigError("LIFE_DAILY_MIN must not exceed LIFE_DAILY_MAX")
        if self.LIFE_POLL_INTERVAL_SECONDS < 1:
            raise ConfigError("LIFE_POLL_INTERVAL_SECONDS must be at least 1")
        if self.LIFE_EVENT_COOLDOWN_MINUTES < 0:
            raise ConfigError("LIFE_EVENT_COOLDOWN_MINUTES must be >= 0")
        self.SCHEDULE_SOFT_BUSY_POLICY = self.SCHEDULE_SOFT_BUSY_POLICY.strip().lower()
        if self.SCHEDULE_SOFT_BUSY_POLICY not in ("normal", "short"):
            raise ConfigError(
                "SCHEDULE_SOFT_BUSY_POLICY must be normal or short "
                f"(got {self.SCHEDULE_SOFT_BUSY_POLICY!r})"
            )
        if unknown:
            raise ConfigError(
                f"LLM_CHAIN contains unknown providers: {', '.join(unknown)}"
            )

    def apply(self, other: "Config") -> list[str]:
        """Hot-reload all non-structural fields from ``other``.

        Returns the list of applied field names. Structural fields keep their
        current values and are reported separately by callers when needed.
        """
        applied: list[str] = []
        for f in fields(self):
            if f.name == "env" or f.name in STRUCTURAL_FIELDS:
                continue
            new_value = getattr(other, f.name)
            if getattr(self, f.name) != new_value:
                setattr(self, f.name, new_value)
                applied.append(f.name)
        self.env = dict(other.env)
        return applied

    def env_override(self, name: str) -> str:
        """Look up a dynamic override variable from the merged environment."""
        return self.env.get(name, "").strip()


_KNOWN_PROVIDERS = frozenset({"fireworks", "chutes", "ollama", "openai_compat"})
