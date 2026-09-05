"""Bridge: wiring and companion turn lifecycle (plan sections 10-12, 14-18, 20-23).

Holds the config, cache, connection manager, LLM router, speech services, the
0.3.0 needs/interaction/owner-profile engines, the 0.4.0 schedule, life,
awareness, catch-up, and contextual owner-schedule engines, the 0.6.0
three-tier memory backend (mid-term compaction, extraction, session close),
private daily tools, the Tavily web tool, and the 0.7.0 heartbeat-initiative
engine (counting, cadence roll, delivery accounting) plus delivery
reconciliation (startup pending -> delivery_unknown, message_ack -> delivered).

validate owner -> non-empty text -> soft-block gate (plan 12 step 5; while
blocked: one authored distance line per cooldown, no LLM/bids/history writes)
-> status(thinking) to source -> schedule availability gate (plan 12 steps
6-7: busy/unavailable defers into the bounded queue without fabricated
speech; first message in the window may speak the authored static line) ->
rhythm stamp -> per-owner turn lock -> bid satisfaction -> persist user row
(delivered) BEFORE provider call -> fan out user chat_sync -> needs evaluate
+ [CHARACTER STATE] + owner lived profile + awareness/context-feed blocks ->
build prompt -> LLM router -> validate/parse reply segments (emotion-only
retried once) -> append assistant row (pending) -> send done to source ->
delivered: mark delivered + fan out assistant chat_sync + needs turn effects
+ clear delivered pending life mentions; failed: mark undelivered, excluded
from future prompts -> release lock -> optional catch-up of held companion
messages -> strict-JSON owner-profile analysis (flag-gated, background) ->
pipelined sequential TTS chunks to the source connection only.

Heartbeats are counted inline per valid sequence (owner-global buckets under
the initiative lock, plan 23.3) and never touch the turn lock; a candidate
initiative generates and delivers in a background task under the turn lock,
and counters advance only after source delivery plus delivered-history
persistence.

Every turn terminates with a ``done`` frame or a terminal error frame (plan
section 30.2). Empty/failed STT returns a localized static line (or
protocol-only metadata) with a terminal ``done`` and makes no LLM/history
call. Catch-up runs under its own per-owner lock and only when availability
is free/soft_busy; work/companion deferred entries stay separated by mode.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import WebSocket, WebSocketDisconnect

from . import history as hist
from .agent_runs import checkpoint_from_result, run_agent_loop
from .bids import BidsEngine
from .cache import RedisCache
from .config import Config
from .connections import Connection, ConnectionManager
from .constants import (
    DEFAULT_EMOTION,
    DAILY_TOOL_RESULT_MAX_CHARS,
    HEARTBEAT_MAX_AGE_SECONDS,
    HEARTBEAT_MAX_FUTURE_SECONDS,
    INITIATIVE_COUNTER_STUB,
    INITIATIVE_THREAD_WINDOW_SECONDS,
    PAUSE_STATUSES,
    PENDING_UNKNOWN_THRESHOLD_SECONDS,
    RUN_STATES,
    STATUS_TO_EMOTION,
    SUPPORTED_LANGUAGES,
    VERSION,
    agent_run_key,
    pending_agent_key,
)
from .context_feed import (
    build_awareness_block,
    build_context_feed,
    build_direct_context_blocks,
)
from .daily_tools import (
    DailyToolExecutor,
    IdempotencyStore,
    ReminderStore,
    ToolContext,
    daily_tool_schemas,
    sanitize_daily_reply,
)
from .device import DeviceManager
from .emotions import load_emotions_manifest
from .external_profiles import ExternalProfileStore
from .interaction import DeferredQueue
from .initiative import (
    BID_KIND_BY_ACTION,
    InitiativeEngine,
    REASON_ACTIVE_TURN,
    REASON_DELIVERED,
    REASON_LLM_FAILED,
    REASON_NO_REASON,
    REASON_NO_TARGET,
    REASON_OWNER_SCHEDULE,
    REASON_NEEDS,
    REASON_SCHEDULE,
    REASON_SILENCE,
    REASON_SOFT_BLOCK,
    REASON_UNDELIVERED,
    SeedUnavailable,
)
from .life import LifeEngine
from .llm import LLMChainExhausted, LLMResult, LLMRouter, ProviderRoute
from .mcp import MCPProxy, parse_tool_name
from .memory import LongTermMemory, MemoryBackend
from .memory_tiers import MidTermMemory
from .needs import NeedsEngine, classify_turn_kind
from .owner_profile import (
    PROPOSAL_MAX_DELTA,
    OwnerProfile,
    default_profile,
    owner_relationship_block,
    validate_and_apply_proposal,
)
from .prompts import (
    build_catchup_prompt,
    build_companion_prompt,
    build_initiative_prompt,
    build_owner_profile_analysis_prompt,
    build_session_summary_prompt,
    build_work_catchup_prompt,
    build_work_prompt,
)
from .rhythm import RhythmEngine
from .schedule import Schedule
from .sessions import SessionError, SessionStore
from .speech import (
    AudioValidationError,
    SpeechProviderError,
    STTService,
    TTSError,
    TTSService,
    decode_audio,
    load_voice_profile,
)
from .state_expression import build_state_block
from .static_lines import get_static_line, load_static_lines
from .text_utils import (
    chunk_segments,
    join_segments,
    parse_emotion_segments,
    parse_pause_status,
    strip_pause_tags,
)
from .user_schedule import UserSchedule
from .web_tools import WebTools
from .work_tools import WorkToolRegistry

log = logging.getLogger("bridge.turn")

_DAILY_SANITIZER_FALLBACK = "[EMOTION: neutral]\nI cannot answer that safely right now."

REPO_ROOT = Path(__file__).resolve().parent.parent

_MAX_SEQUENCE = 2**64 - 1

# Strict-JSON proposal clamp for background analysis (plan section 18.6).
OWNER_PROPOSAL_MAX_DELTA = PROPOSAL_MAX_DELTA


def _parse_tool_arguments(raw: str) -> dict:
    """Parse a provider tool-call argument string; malformed JSON is an
    empty argument set — the executor answers with a structured error
    instead of raising (plan section 24.2)."""
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _parse_strict_json_object(raw: str) -> dict | None:
    """Parse an LLM proposal that must be a strict JSON object.

    Tolerates markdown fences (a common provider habit) but accepts no prose:
    anything that is not exactly one JSON object parses as None.
    """
    text = (raw or "").strip()
    if text.startswith("```"):
        first_newline = text.find("\n")
        if first_newline != -1:
            text = text[first_newline + 1 :]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
        text = text.strip()
    if not text.startswith("{") or not text.endswith("}"):
        return None
    try:
        proposal = json.loads(text)
    except json.JSONDecodeError:
        return None
    return proposal if isinstance(proposal, dict) else None


def error_frame(code: str, message: str, details: dict | None = None, terminal: bool = False) -> dict:
    frame: dict[str, Any] = {
        "type": "error",
        "error": {"code": code, "message": message, "details": details or {}},
    }
    if terminal:
        frame["terminal"] = True
    return frame


def status_frame(status: str, message: str | None = None) -> dict:
    """Status frame shape of plan section 10.4.

    ``message`` is bounded, display-safe engine/UI text — never character
    voice. Status emotions never become final reply emotions.
    """
    return {
        "type": "status",
        "status": status,
        "message": message or status.replace("_", " ").capitalize(),
        "emotion": STATUS_TO_EMOTION.get(status, DEFAULT_EMOTION),
        "timestamp": hist.utc_now_iso(),
    }


class Bridge:
    def __init__(
        self,
        config: Config,
        cache: RedisCache,
        llm: LLMRouter | None = None,
        connections: ConnectionManager | None = None,
        stt: STTService | None = None,
        tts: TTSService | None = None,
    ) -> None:
        self.config = config
        self.cache = cache
        self.llm = llm or LLMRouter(config)
        self.connections = connections or ConnectionManager()
        self.stt = stt or STTService(config)
        self.tts = tts or TTSService(config)
        self.needs = NeedsEngine(config, cache)
        self.bids = BidsEngine(config, cache)
        self.rhythm = RhythmEngine(config, cache)
        self.owner_profile = OwnerProfile(config, cache)
        self.deferred = DeferredQueue(config, cache)
        self.user_schedule = UserSchedule(config, cache)
        self.longterm = MemoryBackend(config, cache)
        self.midterm = MidTermMemory(config, cache, self.longterm, llm=self.llm)
        self.reminders = ReminderStore(cache)
        self.daily_idempotency = IdempotencyStore(cache)
        self.daily_exec = DailyToolExecutor(config)
        self.web = WebTools(config)
        self.sessions = SessionStore(config, cache)
        self.mcp = MCPProxy(config, cache, self.connections)
        self.device = DeviceManager(config, cache, self.connections)
        self.initiative = InitiativeEngine(config, cache)
        self.external_profiles = ExternalProfileStore(config, cache)
        self.schedule: Schedule | None = None
        self.life: LifeEngine | None = None
        self.emotions_manifest: dict = {}
        self.static_lines: dict = {}
        self.deployment_mode = "unknown"
        self.background_tasks: set[asyncio.Task] = set()
        self._identity_cache: dict[str, tuple[float, str]] = {}
        self._started_monotonic = time.monotonic()
        self._last_bid_sweep = 0.0
        self._last_memory_cleanup = 0.0
        self._life_task: asyncio.Task | None = None
        self._life_stop: asyncio.Event | None = None
        # In-process index of pending work pauses (run_id -> connection),
        # so disconnect can convert connection-only pauses to interrupted
        # while the durable checkpoint stays recoverable (plan 25.6).
        self._pending_pauses: dict[str, dict] = {}

    # -- startup -------------------------------------------------------------

    async def startup(self) -> None:
        self.emotions_manifest = load_emotions_manifest(
            self.config.EMOTIONS_FILE or None
        )
        self.tts.attach_manifest(self.emotions_manifest)
        if self.config.TTS_VOICE_PROFILE_FILE.strip():
            self.tts.set_voice_profile(
                load_voice_profile(self.config.TTS_VOICE_PROFILE_FILE.strip())
            )
        self.static_lines = load_static_lines(self.config.STATIC_LINES_FILE or None)
        # Three-tier memory (plan section 20): the durable Redis store of
        # record plus the optional Chroma index (degraded-safe).
        self.longterm.start()
        await self.longterm.reconcile_owner(
            self.config.OWNER_USER_ID,
            extra_rows=await self.midterm.all_chapters(self.config.OWNER_USER_ID),
        )
        # Needs/interaction engines (plan section 15) and the owner lived
        # profile (plan section 18). Flags default OFF; load/validate only
        # when enabled so an inert deployment never touches stores.
        if self.config.NEEDS_ENABLED or self.config.STATE_EXPRESSION_ENABLED:
            self.needs.load_spec()
            self.owner_profile.tuning = self.needs.owner_profile_config()
        if self.config.OWNER_PROFILE_ENABLED:
            self.owner_profile.tuning = self.needs.owner_profile_config()
        # Schedule, life, awareness, catch-up, and contextual owner schedule
        # (plan sections 16, 17, 21, 22). All flag-gated.
        if self.config.SCHEDULE_ENABLED:
            self.schedule = Schedule(self.config)
            self.schedule.load()
            if self.config.LIFE_ENABLED:
                self.life = LifeEngine(
                    self.config, self.cache, self.schedule, self.longterm, self.llm
                )
                self.life.load_templates()
                self._life_stop = asyncio.Event()
                self._life_task = asyncio.create_task(
                    self.life.poll_loop(self.config.OWNER_USER_ID, self._life_stop)
                )
        elif self.config.LIFE_ENABLED:
            log.warning(
                "LIFE_ENABLED requires SCHEDULE_ENABLED (life generates at "
                "block entry); life stays inert"
            )
        # Plan 12 crash recovery: assistant rows left pending beyond a short
        # threshold become delivery_unknown, never silently delivered or
        # undelivered. A matching message_ack moves them back to delivered.
        await self._reconcile_startup_pending(self.config.OWNER_USER_ID)

    async def _reconcile_startup_pending(self, owner: str) -> None:
        try:
            async with self.connections.turn_lock(owner):
                rows = await hist.load_rows(self.cache, owner)
                now_ts = time.time()
                for index, row in enumerate(rows):
                    if row.get("role") != "assistant":
                        continue
                    if row.get("delivery_state") != hist.PENDING:
                        continue
                    try:
                        row_ts = datetime.fromisoformat(str(row.get("ts", "")))
                    except (ValueError, TypeError):
                        continue
                    if (now_ts - row_ts.timestamp()) < PENDING_UNKNOWN_THRESHOLD_SECONDS:
                        continue
                    row["delivery_state"] = hist.DELIVERY_UNKNOWN
                    await self.cache.set_row(
                        hist.companion_history_key(owner), index, json.dumps(row)
                    )
        except Exception:  # noqa: BLE001 - reconciliation never blocks startup
            log.warning("Startup pending-row reconciliation failed", exc_info=True)

    def capabilities(self) -> list[str]:
        """What this build actually supports right now (see SPEC)."""
        caps = ["text"]
        if self.tts.available():
            caps.append("audio")
        if self.stt.available():
            caps.append("voice_input")
        caps.extend(["heartbeat", "chat_sync"])
        if self.config.WORK_ENABLED and self.config.SESSIONS_ENABLED:
            caps.append("work")
            if self.config.MCP_PROXY_ENABLED:
                caps.append("mcp")
            if self.config.DEVICE_ENABLED:
                caps.append("device")
        return caps

    def feature_summary(self) -> dict[str, bool]:
        cfg = self.config
        return {
            "tts": cfg.TTS_ENABLED,
            "stt": cfg.STT_ENABLED,
            "needs": cfg.NEEDS_ENABLED,
            "owner_profile": cfg.OWNER_PROFILE_ENABLED,
            "schedule": cfg.SCHEDULE_ENABLED,
            "life": cfg.LIFE_ENABLED,
            "memory_extraction": cfg.MEMORY_EXTRACTION_ENABLED,
            "memory_cleanup": cfg.MEMORY_CLEANUP_ENABLED,
            "chroma": cfg.CHROMA_ENABLED,
            "daily_tools": cfg.DAILY_TOOLS_ENABLED,
            "daily_web": self.web.available(),
            "initiative": self.initiative.available,
            "external_profile_store": self.external_profiles.available,
            "device": cfg.DEVICE_ENABLED,
            "user_schedule": cfg.USER_SCHEDULE_ENABLED,
        }

    # -- identity files --------------------------------------------------------

    def identity_path(self, which: str) -> Path:
        """Resolve an identity file path (env override or repo default)."""
        override = getattr(self.config, f"{which.upper()}_FILE", "") or ""
        if override.strip():
            return Path(override.strip())
        return REPO_ROOT / "identity" / f"{which.upper()}.md"

    def identity_info(self) -> dict[str, dict]:
        """Resolved paths and mtimes for /status — never contents."""
        info: dict[str, dict] = {}
        for which in ("soul", "profile", "state"):
            path = self.identity_path(which)
            entry: dict[str, Any] = {"path": str(path), "exists": path.exists()}
            if path.exists():
                entry["mtime"] = path.stat().st_mtime
            info[which] = entry
        return info

    def _read_identity(self, which: str) -> str:
        """Read an identity file with an mtime cache (plan section 6.1)."""
        path = self.identity_path(which)
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return ""
        cached = self._identity_cache.get(str(path))
        if cached and cached[0] == mtime:
            return cached[1]
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return ""
        self._identity_cache[str(path)] = (mtime, text)
        return text

    # -- websocket ------------------------------------------------------------

    async def handle_websocket(
        self,
        websocket: WebSocket,
        user_id: str,
        client_type: str = "unknown",
        device_id: str = "",
        timezone_name: str = "",
    ) -> None:
        if user_id != self.config.OWNER_USER_ID:
            await websocket.accept()
            await websocket.send_json(
                error_frame(
                    "forbidden_user",
                    "This deployment serves exactly one configured owner.",
                    terminal=True,
                )
            )
            await websocket.close(code=4003)
            log.info("Rejected non-owner connection attempt")
            return

        await websocket.accept()
        conn = self.connections.connect(
            websocket,
            user_id,
            client_type=client_type,
            device_id=device_id,
            timezone=timezone_name,
        )
        await conn.send_json(
            {
                "type": "connected",
                "connection_id": conn.connection_id,
                "server_version": VERSION,
                "capabilities": self.capabilities(),
                "server_time": hist.utc_now_iso(),
            }
        )
        try:
            while True:
                raw = await websocket.receive_text()
                await self._handle_inbound(conn, raw)
        except WebSocketDisconnect:
            pass
        finally:
            self.connections.disconnect(conn.connection_id)
            # Plan 25.6: a connection-only pending pause becomes
            # interrupted, but the durable checkpoint stays recoverable
            # via explicit session/run ids.
            for run_id, meta in list(self._pending_pauses.items()):
                if meta.get("connection_id") != conn.connection_id:
                    continue
                interrupt_task = asyncio.create_task(
                    self._mark_run_interrupted(conn.user_id, meta["session_id"], run_id)
                )
                self.background_tasks.add(interrupt_task)
                interrupt_task.add_done_callback(self.background_tasks.discard)

    async def _handle_inbound(self, conn: Connection, raw: str) -> None:
        try:
            frame = json.loads(raw)
        except json.JSONDecodeError:
            await conn.send_json(error_frame("bad_json", "Frame is not valid JSON."))
            return
        if not isinstance(frame, dict):
            await conn.send_json(error_frame("bad_frame", "Frame must be a JSON object."))
            return

        frame_type = frame.get("type")
        conn.touch()
        if frame_type == "heartbeat":
            await self.handle_heartbeat(conn, frame)
        elif frame_type == "text":
            await self._handle_text_frame(conn, frame)
        elif frame_type == "audio":
            await self._handle_audio_frame(conn, frame)
        elif frame_type == "message_ack":
            # Delivery reconciliation (plan sections 10.10, 12): a matching
            # pending/delivery_unknown assistant row becomes delivered.
            # Unknown ids and duplicates are accepted and idempotent.
            message_id = frame.get("id")
            if isinstance(message_id, str) and message_id.strip():
                ack_task = asyncio.create_task(
                    self._reconcile_message_ack(conn.user_id, message_id.strip())
                )
                self.background_tasks.add(ack_task)
                ack_task.add_done_callback(self.background_tasks.discard)
        elif frame_type == "mcp_result":
            self.mcp.handle_result(conn, frame)
        elif frame_type == "device_tool_result":
            self.device.handle_result(conn, frame)
        elif frame_type == "device_state":
            error_code = self.device.apply_state(conn, frame)
            if error_code is not None:
                await conn.send_json(
                    error_frame(error_code, "Invalid device_state frame.")
                )
        else:
            await conn.send_json(
                error_frame("unknown_frame_type", f"Unknown frame type: {frame_type!r}")
            )

    def _resolve_reply_language(self, frame: dict) -> str | None:
        """Per-message language pin (plan section 7.4).

        Returns the pinned language, or None when the caller should fall back
        (absent field -> owner profile -> STT language detection ->
        DEFAULT_LANGUAGE). Raises ValueError with a message for explicit
        invalid values.
        """
        language = frame.get("language")
        if language is None or language == "":
            return None
        if not isinstance(language, str) or language.strip().lower() not in SUPPORTED_LANGUAGES:
            raise ValueError(f"Unsupported reply language: {language!r}")
        return language.strip().lower()

    async def _owner_preferred_language(self) -> str | None:
        """Owner lived-profile preferred language (plan 7.4 step 2).

        Joins the fallback between the explicit pin and inbound-language
        detection; blank/absent means no preference.
        """
        if not self.owner_profile.available:
            return None
        profile = await self.owner_profile.get(self.config.OWNER_USER_ID)
        language = (profile or {}).get("preferred_language", "")
        return language if language in SUPPORTED_LANGUAGES else None

    async def _handle_text_frame(self, conn: Connection, frame: dict) -> None:
        mode = frame.get("mode", "companion")
        if mode not in ("companion", "work"):
            await conn.send_json(
                error_frame(
                    "unknown_mode",
                    f"Unknown mode: {mode!r}. Supported: companion, work.",
                    terminal=True,
                )
            )
            return
        if mode == "work" and not (
            self.config.WORK_ENABLED and self.config.SESSIONS_ENABLED
        ):
            await conn.send_json(
                error_frame(
                    "work_unavailable",
                    "Work mode is not available on this server.",
                    terminal=True,
                )
            )
            return
        try:
            language = self._resolve_reply_language(frame)
        except ValueError as exc:
            await conn.send_json(
                error_frame(
                    "unsupported_language",
                    f"{exc}. Supported: {', '.join(SUPPORTED_LANGUAGES)}.",
                    terminal=True,
                )
            )
            return
        if language is None:
            language = (
                await self._owner_preferred_language() or self.config.DEFAULT_LANGUAGE
            )
        text = frame.get("text")
        wants_audio = bool(frame.get("wants_audio"))
        # Run the turn in a background task so the reader loop keeps serving
        # heartbeats, tool results, and other frames while the turn runs.
        if mode == "work":
            session_id = frame.get("session_id")
            project_id = frame.get("project_id")
            run_task = asyncio.create_task(
                self.run_work_turn(
                    text=text if isinstance(text, str) else "",
                    language=language,
                    source_conn=conn,
                    wants_audio=wants_audio,
                    session_id=session_id if isinstance(session_id, str) else None,
                    project_id=project_id if isinstance(project_id, str) else None,
                    context=frame.get("context")
                    if isinstance(frame.get("context"), dict)
                    else None,
                    explicit_run_id=frame.get("run_id")
                    if isinstance(frame.get("run_id"), str)
                    else None,
                )
            )
        else:
            run_task = asyncio.create_task(
                self.run_companion_turn(
                    text=text if isinstance(text, str) else "",
                    language=language,
                    source_conn=conn,
                    wants_audio=wants_audio,
                )
            )
        self.background_tasks.add(run_task)
        run_task.add_done_callback(self.background_tasks.discard)

    # -- audio (voice input) turns (plan sections 10.3, 14.2, 14.3) -----------

    def _allowed_audio_types(self) -> set[str]:
        return {
            part.strip().lower()
            for part in self.config.ALLOWED_AUDIO_CONTENT_TYPES.split(",")
            if part.strip()
        }

    async def _handle_audio_frame(self, conn: Connection, frame: dict) -> None:
        mode = frame.get("mode", "companion")
        if mode not in ("companion", "work"):
            await conn.send_json(
                error_frame(
                    "unknown_mode",
                    f"Unknown mode: {mode!r}. Supported: companion, work.",
                    terminal=True,
                )
            )
            return
        if mode == "work" and not (
            self.config.WORK_ENABLED and self.config.SESSIONS_ENABLED
        ):
            await conn.send_json(
                error_frame(
                    "work_unavailable",
                    "Work mode is not available on this server.",
                    terminal=True,
                )
            )
            return
        if not self.stt.available():
            await conn.send_json(
                error_frame(
                    "stt_unavailable",
                    "Voice input is not available on this server.",
                    terminal=True,
                )
            )
            return

        try:
            language = self._resolve_reply_language(frame)
        except ValueError as exc:
            await conn.send_json(
                error_frame(
                    "unsupported_language",
                    f"{exc}. Supported: {', '.join(SUPPORTED_LANGUAGES)}.",
                    terminal=True,
                )
            )
            return

        stt_language = frame.get("stt_language")
        if stt_language is not None and stt_language != "":
            if not isinstance(stt_language, str) or not stt_language.strip():
                await conn.send_json(
                    error_frame(
                        "unsupported_language",
                        "'stt_language' must be a non-empty language code.",
                        terminal=True,
                    )
                )
                return
            stt_language = stt_language.strip()
        else:
            stt_language = self.config.STT_LANGUAGE
        # Clear inbound-language detection (plan 7.4 steps 2-4): owner-profile
        # preferred language, then the spoken language, then the default.
        if language is None:
            language = await self._owner_preferred_language()
        if language is None:
            language = (
                stt_language.lower()
                if stt_language.lower() in SUPPORTED_LANGUAGES
                else self.config.DEFAULT_LANGUAGE
            )

        audio_base64 = frame.get("audio_base64")
        if not isinstance(audio_base64, str) or not audio_base64.strip():
            await conn.send_json(
                error_frame("invalid_audio", "Missing audio payload.", terminal=True)
            )
            return
        declared = frame.get("audio_content_type")
        if declared is not None and not isinstance(declared, str):
            await conn.send_json(
                error_frame(
                    "invalid_audio", "'audio_content_type' must be a string.", terminal=True
                )
            )
            return
        try:
            audio, content_type = decode_audio(
                audio_base64,
                declared or "",
                max_bytes=self.config.MAX_AUDIO_BYTES,
                allowed_types=self._allowed_audio_types(),
            )
        except AudioValidationError as exc:
            await conn.send_json(
                error_frame(exc.code, str(exc), terminal=True)
            )
            return

        try:
            transcript = await self.stt.transcribe(audio, content_type, stt_language)
        except SpeechProviderError as exc:
            # No provider bodies in logs (plan section 14.3).
            log.warning("STT failed (%s)", exc)
            await self._stt_terminal(conn, reason="stt_failed", language=language)
            return

        await conn.send_json(
            {
                "type": "stt",
                "text": transcript,
                "provider": self.stt.provider_name,
                "language": stt_language,
            }
        )
        if not transcript.strip():
            await self._stt_terminal(conn, reason="stt_empty", language=language)
            return

        wants_audio = bool(frame.get("wants_audio"))
        if mode == "work":
            work_task = asyncio.create_task(
                self.run_work_turn(
                    text=transcript,
                    language=language,
                    source_conn=conn,
                    wants_audio=wants_audio,
                    session_id=frame.get("session_id")
                    if isinstance(frame.get("session_id"), str)
                    else None,
                    project_id=frame.get("project_id")
                    if isinstance(frame.get("project_id"), str)
                    else None,
                )
            )
            self.background_tasks.add(work_task)
            work_task.add_done_callback(self.background_tasks.discard)
            return
        task = asyncio.create_task(
            self.run_companion_turn(
                text=transcript,
                language=language,
                source_conn=conn,
                wants_audio=wants_audio,
            )
        )
        self.background_tasks.add(task)
        task.add_done_callback(self.background_tasks.discard)

    async def _stt_terminal(self, conn: Connection, *, reason: str, language: str) -> None:
        """Localized static line + terminal done; no LLM/history call.

        A blank authored line is deliberate silence: protocol-only metadata
        with no fabricated character voice (plan sections 7.1, 10.3).
        """
        done: dict[str, Any] = {
            "type": "done",
            "id": hist.new_message_id(),
            "mode": "companion",
            "emotion": DEFAULT_EMOTION,
            "ignored": True,
            "reason": reason,
            "initiated_by": "user",
        }
        line = get_static_line(self.static_lines, "stt_empty", language)
        if line:
            done["text"] = line
            done["segments"] = [{"text": line, "emotion": DEFAULT_EMOTION}]
        await conn.send_json(done)

    # -- heartbeat --------------------------------------------------------------

    async def handle_heartbeat(self, conn: Connection, frame: dict) -> None:
        sequence = frame.get("sequence")
        if (
            not isinstance(sequence, int)
            or isinstance(sequence, bool)
            or sequence < 0
            or sequence > _MAX_SEQUENCE
        ):
            await conn.send_json(
                error_frame(
                    "invalid_heartbeat",
                    "heartbeat requires a non-negative 64-bit integer 'sequence'.",
                )
            )
            return

        last_input_at = frame.get("last_input_at")
        if last_input_at is not None:
            if not isinstance(last_input_at, (int, float)) or isinstance(
                last_input_at, bool
            ):
                await conn.send_json(
                    error_frame(
                        "invalid_heartbeat",
                        "'last_input_at' must be a unix timestamp number.",
                    )
                )
                return
            now = time.time()
            if (
                last_input_at > now + HEARTBEAT_MAX_FUTURE_SECONDS
                or last_input_at < now - HEARTBEAT_MAX_AGE_SECONDS
            ):
                await conn.send_json(
                    error_frame(
                        "invalid_heartbeat",
                        "'last_input_at' is outside the allowed freshness window.",
                    )
                )
                return

        tz = frame.get("timezone")
        if isinstance(tz, str) and tz.strip():
            conn.timezone = tz.strip()

        counted = sequence > conn.last_heartbeat_sequence
        if counted:
            conn.last_heartbeat_sequence = sequence
        else:
            log.debug(
                "Replayed/out-of-order heartbeat sequence on %s", conn.connection_id
            )

        # Initiative counting (plan section 23). Counting never takes the
        # per-owner turn lock, so heartbeats stay acknowledged while another
        # turn is active (plan section 11). Replayed/out-of-order sequences
        # are acked but never count. When the engine is disabled no key is
        # created and the ack keeps the constant stub counter.
        counter = INITIATIVE_COUNTER_STUB
        candidate: dict | None = None
        if self.initiative.available and counted:
            try:
                async with self.connections.initiative_lock(conn.user_id):
                    outcome = await self.initiative.count(
                        conn.user_id, conn.connection_id
                    )
                counter = outcome["heartbeat_count"]
                if outcome["candidate"]:
                    candidate = outcome
            except SeedUnavailable:
                log.warning("Initiative seed unusable; engine stays idle")
            except Exception:  # noqa: BLE001 - counting never fails acks
                log.debug("initiative counting failed", exc_info=True)

        await conn.send_json(
            {
                "type": "heartbeat_ack",
                "server_time": hist.utc_now_iso(),
                "initiative_counter": counter,
                "counted": counted,
            }
        )
        # Bids expire deterministically; sweep during heartbeat maintenance
        # at most once per minute (plan section 15.5). The same maintenance
        # tick checks for due companion catch-ups (plan section 16.3).
        now = time.monotonic()
        if now - self._last_bid_sweep >= 60.0:
            self._last_bid_sweep = now
            if self.bids.available:
                sweep_task = asyncio.create_task(self._sweep_bids(owner_id=conn.user_id))
                self.background_tasks.add(sweep_task)
                sweep_task.add_done_callback(self.background_tasks.discard)
            if self.schedule is not None and self.schedule.available:
                catchup_task = asyncio.create_task(
                    self._maybe_catchup(conn.user_id, trigger_conn=conn)
                )
                self.background_tasks.add(catchup_task)
                catchup_task.add_done_callback(self.background_tasks.discard)
                work_catchup_task = asyncio.create_task(
                    self._maybe_work_catchup(conn.user_id, trigger_conn=conn)
                )
                self.background_tasks.add(work_catchup_task)
                work_catchup_task.add_done_callback(
                    self.background_tasks.discard
                )
        # Delivery (plan 23.3 steps 9-16) runs as a background task so the
        # reader loop never waits on LLM generation.
        if candidate is not None:
            initiative_task = asyncio.create_task(
                self._deliver_initiative(conn.user_id, candidate)
            )
            self.background_tasks.add(initiative_task)
            initiative_task.add_done_callback(self.background_tasks.discard)

    async def _sweep_bids(self, owner_id: str) -> None:
        try:
            await self.bids.sweep_expired(owner_id)
        except Exception:  # noqa: BLE001 - maintenance never fails heartbeats
            log.debug("bid sweep failed", exc_info=True)

    # -- heartbeat initiative delivery (plan section 23) ---------------------------

    async def _deliver_initiative(self, owner: str, candidate: dict) -> None:
        """Generate and deliver one initiative (plan 23.3 steps 9-16).

        Never raises. The expensive gates re-run here because connection and
        turn state may have moved since counting. Generation and the
        section 12 pending/delivery protocol run under the per-owner turn
        lock; counter accounting runs only after source delivery AND
        delivered-history persistence both succeed.
        """
        try:
            stop_action, stop_reason = await self._initiative_gates(owner)
            if stop_reason:
                await self.initiative.note_decision(owner, stop_action, stop_reason)
                return

            target = self._initiative_target(owner, candidate)
            if target is None:
                await self.initiative.note_decision(owner, "no_action", REASON_NO_TARGET)
                return

            action = await self._select_initiative_action(owner)
            if action is None:
                await self.initiative.note_decision(owner, "no_action", REASON_NO_REASON)
                return

            language = (
                await self._owner_preferred_language() or self.config.DEFAULT_LANGUAGE
            )
            delivered = False
            async with self.connections.turn_lock(owner):
                prompt_history = await hist.load_prompt_history(
                    self.cache, owner, self.config.LLM_HISTORY_MESSAGE_BUDGET
                )
                blocks = await self._build_prompt_blocks(
                    owner, source_conn=target, prompt_history=prompt_history
                )
                messages = build_initiative_prompt(
                    soul_text=self._read_identity("soul"),
                    profile_text=self._read_identity("profile"),
                    history=prompt_history,
                    action=action,
                    language=language,
                    state_block=blocks["state_block"],
                    owner_block=blocks["owner_block"],
                    awareness_block=blocks["awareness_block"],
                    context_feed=blocks["context_feed"] or blocks["chapter_block"],
                )
                result = await self.llm.chat("proactive", messages)
                segments = parse_emotion_segments(result.text)
                if _is_silence(segments):
                    await self.initiative.note_decision(
                        owner, "no_action", REASON_SILENCE
                    )
                    log.info("Initiative resolved to silence")
                    return
                reply_text = join_segments(segments)
                emotion = segments[0]["emotion"]
                assistant_row = hist.make_row(
                    "assistant", reply_text, hist.PENDING, emotion=emotion
                )
                assistant_row["initiated_by"] = "character"
                assistant_row["initiative"] = True
                assistant_row["initiative_action"] = action
                await hist.append_row(
                    self.cache, owner, assistant_row, self.config.MAX_HISTORY_TURNS
                )
                done: dict[str, Any] = {
                    "type": "done",
                    "id": assistant_row["id"],
                    "text": reply_text,
                    "emotion": emotion,
                    "segments": [dict(segment) for segment in segments],
                    "mode": "companion",
                    "provider": result.provider,
                    "model": result.model,
                    "initiative": True,
                    "initiative_action": action,
                    "initiated_by": "character",
                }
                if result.usage:
                    done["tokens"] = {
                        "prompt": result.usage.get("prompt_tokens", 0),
                        "completion": result.usage.get("completion_tokens", 0),
                        "total": result.usage.get("total_tokens", 0),
                    }
                delivered = await target.send_json(done)
                if delivered:
                    await hist.mark_delivery_state(
                        self.cache, owner, assistant_row["id"], hist.DELIVERED
                    )
                    await self._fan_out(
                        owner,
                        self._chat_sync(assistant_row, "character", target),
                        exclude=target,
                    )
                else:
                    await hist.mark_delivery_state(
                        self.cache, owner, assistant_row["id"], hist.UNDELIVERED
                    )

            if delivered:
                # Plan 23.3 step 16: accounting and the connection bid are
                # registered only after confirmed delivery + persistence.
                async with self.connections.initiative_lock(owner):
                    await self.initiative.record_delivery(owner)
                await self.initiative.note_decision(owner, action, REASON_DELIVERED)
                if self.bids.available:
                    try:
                        lifetime = float(
                            self.needs.bid_config().get(
                                "open_bid_lifetime_seconds", 1209600.0
                            )
                        )
                        await self.bids.register_bid(
                            owner,
                            BID_KIND_BY_ACTION[action],
                            lifetime_seconds=lifetime,
                        )
                    except Exception:  # noqa: BLE001
                        log.debug("initiative bid registration failed", exc_info=True)
                log.info("Initiative delivered (action=%s)", action)
            else:
                await self.initiative.note_decision(owner, action, REASON_UNDELIVERED)
        except LLMChainExhausted:
            await self.initiative.note_decision(owner, "no_action", REASON_LLM_FAILED)
            log.warning("Initiative generation failed; no delivery, no accounting")
        except Exception:  # noqa: BLE001 - initiative never crashes the bridge
            log.debug("initiative delivery failed", exc_info=True)

    async def _initiative_gates(self, owner: str) -> tuple[str, str]:
        """Plan 23.3 steps 9-12. Returns (action, stop_reason); an empty
        reason means every gate passed."""
        # Step 9: any owner connection with an active turn suppresses.
        if self.connections.turn_lock(owner).locked():
            return "no_action", REASON_ACTIVE_TURN
        # Step 10: character schedule availability per config.
        availability = await self._effective_availability(owner)
        allowed = ("free",) if self.config.INITIATIVE_REQUIRE_SCHEDULE_FREE else (
            "free",
            "soft_busy",
        )
        if availability not in allowed:
            return "no_action", REASON_SCHEDULE
        # Step 11: critical needs or owner-profile soft block suppress.
        if self.needs.available:
            try:
                snapshot = await self.needs.peek(owner)
                if snapshot.get("shutdown") or any(
                    zone in ("critical", "deprived")
                    for zone in snapshot.get("zones", {}).values()
                ):
                    return "no_action", REASON_NEEDS
            except Exception:  # noqa: BLE001 - advisory gate
                log.debug("needs peek for initiative failed", exc_info=True)
        if self.owner_profile.available and self.config.OWNER_SOFT_BLOCK_ENABLED:
            status = await self.owner_profile.soft_block_status(owner)
            if status.get("blocked"):
                return "no_action", REASON_SOFT_BLOCK
        # Step 12: optional contextual owner-schedule suppression.
        if self.config.INITIATIVE_RESPECT_OWNER_SCHEDULE and self.user_schedule.available:
            block = await self.user_schedule.current_block(owner)
            if block is not None and block.get("state") in ("sleep", "busy"):
                return "no_action", REASON_OWNER_SCHEDULE
        return "", ""

    def _initiative_target(self, owner: str, candidate: dict) -> Connection | None:
        """Plan 23.3 target rule: the first valid sender of the
        threshold-crossing bucket if still connected and turn-free,
        otherwise the most recently active valid owner connection."""
        conns = self.connections.connections_for(owner)
        if not conns:
            return None
        threshold_id = str(candidate.get("target_connection_id", "") or "")
        if threshold_id and not self.connections.turn_lock(owner).locked():
            for conn in conns:
                if conn.connection_id == threshold_id:
                    return conn
        return max(conns, key=lambda c: c.last_activity_ts)

    async def _select_initiative_action(self, owner: str) -> str | None:
        """Deterministic reason selection (plan 23.3 step 13).

        Priority: pending life mention, bond need, low fun, recent open
        thread. ``None`` when nothing justifies speaking.
        """
        if self.life is not None and self.life.available:
            try:
                if await self.life.pending_ids(owner):
                    return "life"
            except Exception:  # noqa: BLE001
                log.debug("life pending check failed", exc_info=True)
        if self.needs.available:
            try:
                snapshot = await self.needs.peek(owner)
                zones = snapshot.get("zones", {})
                if zones.get("bond") in ("strained", "deprived"):
                    return "bond"
                if zones.get("fun") in ("low", "critical"):
                    return "fun"
            except Exception:  # noqa: BLE001
                log.debug("needs peek for reason failed", exc_info=True)
        try:
            rows = await hist.load_rows(self.cache, owner)
            for row in reversed(rows):
                if row.get("delivery_state") != hist.DELIVERED:
                    continue
                try:
                    row_ts = datetime.fromisoformat(str(row.get("ts", "")))
                except (ValueError, TypeError):
                    continue
                if time.time() - row_ts.timestamp() <= INITIATIVE_THREAD_WINDOW_SECONDS:
                    return "thread"
                break
        except Exception:  # noqa: BLE001
            log.debug("thread reason check failed", exc_info=True)
        return None

    # -- delivery reconciliation (plan sections 10.10, 12) --------------------------

    async def _reconcile_message_ack(self, owner: str, message_id: str) -> None:
        """A client acknowledgement moves a ``pending``/``delivery_unknown``
        assistant row to delivered (plan section 12 crash recovery).
        Idempotent: unknown ids and already-delivered rows change nothing."""
        try:
            async with self.connections.turn_lock(owner):
                rows = await hist.load_rows(self.cache, owner)
                for index, row in enumerate(rows):
                    if row.get("id") != message_id:
                        continue
                    if row.get("delivery_state") not in (
                        hist.PENDING,
                        hist.DELIVERY_UNKNOWN,
                    ):
                        return
                    row["delivery_state"] = hist.DELIVERED
                    await self.cache.set_row(
                        hist.companion_history_key(owner), index, json.dumps(row)
                    )
                    log.info("message_ack marked %s delivered", message_id)
                    return
        except Exception:  # noqa: BLE001 - reconciliation never fails the reader
            log.debug("message_ack reconciliation failed", exc_info=True)

    # -- companion turn -----------------------------------------------------------

    async def _maybe_soft_block(
        self, owner: str, language: str, source_conn: Connection | None
    ) -> dict | None:
        """Owner-profile soft block gate (plan sections 12 step 5, 18.4).

        While blocked: one owner-authored localized distance line per
        cooldown, otherwise protocol-only silence. No companion LLM call,
        no bids, no history writes. History is never wiped. Work mode
        bypasses the relationship soft block (prepared for milestone 0.5.0).
        """
        if not (self.owner_profile.available and self.config.OWNER_SOFT_BLOCK_ENABLED):
            return None
        status = await self.owner_profile.soft_block_status(owner)
        if not status.get("blocked"):
            return None
        line = self.owner_profile.soft_block_line(self.static_lines, language)
        can_speak = bool(line)
        profile = await self.owner_profile.get(owner)
        if profile is not None:
            last_notice = float(profile.get("soft_block_last_notice_ts", 0) or 0)
            if time.time() - last_notice < self.config.OWNER_SOFT_BLOCK_COOLDOWN_SECONDS:
                can_speak = False
        done: dict[str, Any] = {
            "type": "done",
            "id": hist.new_message_id(),
            "mode": "companion",
            "emotion": DEFAULT_EMOTION,
            "ignored": True,
            "reason": "soft_blocked",
            "initiated_by": "user",
        }
        if can_speak and line:
            done["text"] = line
            done["segments"] = [{"text": line, "emotion": DEFAULT_EMOTION}]
            await self.owner_profile.mark_soft_block_notice(owner)
        if source_conn is not None:
            await source_conn.send_json(done)
        log.info("Companion turn suppressed by soft block")
        return done

    # -- schedule availability, defer, and catch-up (plan sections 12, 16.3) -----

    def _resolve_zone(self, tz_name: str) -> ZoneInfo:
        try:
            return ZoneInfo(tz_name)
        except (ZoneInfoNotFoundError, ValueError, KeyError):
            return ZoneInfo("UTC")

    async def _effective_availability(self, owner: str) -> str:
        """Raw schedule availability combined with critical needs shutdown
        (plan section 15.4). Soft block is handled earlier (plan 12 step 5).
        Uses needs ``peek`` — the gate never persists lazy state (plan 6.4)."""
        availability = "free"
        if self.schedule is not None and self.schedule.available:
            self.schedule.maybe_reload()
            availability = str(self.schedule.current_block()["availability"])
        if self.needs.available:
            try:
                snapshot = await self.needs.peek(owner)
                if snapshot.get("shutdown"):
                    availability = "unavailable"
            except Exception:  # noqa: BLE001 - advisory gate never fails a turn
                log.debug("needs peek for availability failed", exc_info=True)
        return availability

    async def _defer_turn(
        self,
        owner: str,
        *,
        text: str,
        language: str,
        source_conn: Connection | None,
        availability: str,
        mode: str = "companion",
        session_id: str | None = None,
    ) -> dict:
        """Busy/unavailable ladder (plan section 16.3).

        The message is queued for one later catch-up answer; no LLM call,
        no history write, no bids/needs effects run on this path. The first
        message in a busy window may speak the authored static line;
        repeated messages defer protocol-only. Skipped hooks (do not run):
        bids, needs effects, boundary classification (classified at
        catch-up), history persistence, memory/analysis. Work entries keep
        their session metadata so work catch-up stays separated (plan
        16.3).
        """
        user_row = hist.make_row("user", text.strip(), hist.DELIVERED, mode=mode)
        origin = source_conn.connection_id if source_conn is not None else "http"
        async with self.connections.catchup_lock(owner):
            window_count = await self.deferred.busy_count(owner)
            await self.deferred.append(
                owner,
                message_id=user_row["id"],
                mode=mode,
                text=text.strip(),
                source_connection_id=origin,
                session_id=session_id,
            )
            await self.deferred.increment_busy(owner)
        done: dict[str, Any] = {
            "type": "done",
            "id": hist.new_message_id(),
            "mode": mode,
            "emotion": DEFAULT_EMOTION,
            "ignored": True,
            "reason": availability,
            "deferred": True,
            "initiated_by": "user",
        }
        if window_count == 0:
            line = get_static_line(self.static_lines, availability, language)
            if line:
                done["text"] = line
                done["segments"] = [{"text": line, "emotion": DEFAULT_EMOTION}]
        if source_conn is not None:
            await source_conn.send_json(done)
        log.info(
            "%s message deferred (availability=%s, window_count=%d)",
            mode.capitalize(),
            availability,
            window_count,
        )
        return done

    async def _build_prompt_blocks(
        self,
        owner: str,
        *,
        source_conn: Connection | None,
        prompt_history: list[dict],
        current_text: str = "",
    ) -> dict:
        """Shared per-turn block building (plan 12 steps 14-16, 21).

        Returns state/owner/awareness/context-feed blocks plus the prompt
        profile and the pending life-mention ids included in the feed.
        """
        state_block = ""
        owner_block = ""
        prompt_profile: dict | None = None
        if self.needs.available:
            snapshot = await self.needs.evaluate(owner)
            if self.config.STATE_EXPRESSION_ENABLED:
                state_block = build_state_block(
                    snapshot["zones"], self._read_identity("state")
                )
        if self.owner_profile.available:
            prompt_profile = await self.owner_profile.get(owner)
            if prompt_profile is None:
                prompt_profile = await self.owner_profile.upsert(
                    owner,
                    default_profile(self.config),
                    expected_version=1,
                ) or default_profile(self.config)
            if self.config.OWNER_PROFILE_INJECT:
                blocked_now = bool(
                    self.config.OWNER_SOFT_BLOCK_ENABLED
                    and prompt_profile.get("soft_blocked")
                )
                owner_block = owner_relationship_block(
                    prompt_profile, soft_blocked=blocked_now
                )
        awareness_block = ""
        context_feed_text = ""
        pending_life_ids: list[str] = []
        chapter_block = ""
        if self.config.SCHEDULE_ENABLED or self.config.USER_SCHEDULE_ENABLED:
            awareness_block = await self._build_awareness_block(
                owner, source_conn, prompt_history
            )
        if self.config.CONTEXT_FEED_ENABLED:
            # The feed is the only renderer for life rows, durable memories,
            # and mid-term chapters (plan section 20.4).
            context_feed_text, pending_life_ids = await self._build_context_feed(
                owner, current_text=current_text
            )
        else:
            context_feed_text, pending_life_ids = await self._build_context_feed(
                owner, current_text=current_text, direct=True
            )
        return {
            "state_block": state_block,
            "owner_block": owner_block,
            "awareness_block": awareness_block,
            "context_feed": context_feed_text,
            "chapter_block": chapter_block,
            "pending_life_ids": pending_life_ids,
            "prompt_profile": prompt_profile,
        }

    async def _build_awareness_block(
        self,
        owner: str,
        source_conn: Connection | None,
        prompt_history: list[dict],
    ) -> str:
        """Bounded awareness block (plan section 21.1); deterministic."""
        owner_tz = (
            (source_conn.timezone if source_conn else "") or self.config.OWNER_TIMEZONE
        )
        now = datetime.now(timezone.utc)
        owner_local = now.astimezone(self._resolve_zone(owner_tz))
        character_local = now.astimezone(self._resolve_zone(self.config.CHARACTER_TIMEZONE))
        schedule_now = ""
        if self.schedule is not None and self.schedule.available:
            block = self.schedule.current_block(now)
            schedule_now = (
                f"{block.get('activity')} at {block.get('place')} "
                f"({block.get('availability')})"
            )
        since = ""
        if prompt_history:
            try:
                last_ts = datetime.fromisoformat(prompt_history[-1]["ts"])
                delta = max(0, int((now - last_ts).total_seconds()))
                if delta >= 60:
                    hours, minutes = divmod(delta // 60, 60)
                    since = f"{hours}h {minutes}m" if hours else f"{minutes}m"
            except (ValueError, TypeError, KeyError):
                since = ""
        owner_schedule_now = ""
        if self.user_schedule.available:
            owner_block_now = await self.user_schedule.current_block(owner, now)
            if owner_block_now is not None:
                state = owner_block_now.get("state", "unknown")
                span = (
                    f" ({owner_block_now.get('start')}-{owner_block_now.get('end')})"
                    if owner_block_now.get("start")
                    else ""
                )
                owner_schedule_now = f"{state}{span}"
        return build_awareness_block(
            owner_local=owner_local.strftime("%A %H:%M (%Z)"),
            character_local=character_local.strftime("%A %H:%M (%Z)"),
            character_schedule_now=schedule_now,
            since_last_conversation=since,
            owner_schedule_now=owner_schedule_now,
        )

    async def _build_context_feed(
        self, owner: str, *, current_text: str = "", direct: bool = False
    ) -> tuple[str, list[str]]:
        """Single bounded feed for life rows, memories, and chapters
        (plan sections 20.4, 21.2). At most one semantic memory query per
        turn; without a query it falls back to the most recent protected
        facts deterministically."""
        life_events: list[dict] = []
        pending_ids: list[str] = []
        if self.life is not None and self.life.available:
            life_events = await self.life.recent(owner, limit=8)
            pending_ids = await self.life.pending_ids(owner)

        if current_text.strip():
            memories = await self.longterm.search(owner, current_text, limit=6)
        else:
            memories = list(
                reversed(
                    await self.longterm.records(
                        owner,
                        limit=6,
                    )
                )
            )

        chapters = await self.midterm.recent_chapters(owner)
        renderer = build_direct_context_blocks if direct else build_context_feed
        feed, included = renderer(
            life_events=life_events,
            pending_ids=pending_ids,
            memories=memories,
            chapters=chapters,
            max_tokens=self.config.CONTEXT_FEED_MAX_TOKENS,
        )
        return feed, [rid for rid in included if rid]

    async def _maybe_catchup(
        self, owner: str, trigger_conn: Connection | None = None
    ) -> bool:
        """Guarded catch-up entry point; never raises."""
        if not (
            self.config.SCHEDULE_ENABLED
            and self.schedule is not None
            and self.schedule.available
        ):
            return False
        try:
            return await self.run_catchup(owner, trigger_conn=trigger_conn)
        except Exception:  # noqa: BLE001 - maintenance never fails callers
            log.debug("catch-up attempt failed", exc_info=True)
            return False

    async def run_catchup(
        self, owner: str, *, trigger_conn: Connection | None = None
    ) -> bool:
        """Answer all held companion messages with one response (plan 16.3).

        Claims entries atomically under the catch-up lock, then mutates
        history under the per-owner turn lock. Entries restore to ``held``
        on generation or delivery failure (unless expired). Success means
        source delivery plus delivered-history persistence; only then are
        claimed entries removed and the busy window reset.
        """
        async with self.connections.catchup_lock(owner):
            if self.connections.turn_lock(owner).locked():
                return False  # a turn is active; a later trigger retries
            availability = await self._effective_availability(owner)
            if availability not in ("free", "soft_busy"):
                return False
            now_ts = time.time()
            entries = await self.deferred.claim(owner, "companion", now_ts)
            if not entries:
                if await self.deferred.busy_count(owner):
                    await self.deferred.reset_busy(owner)
                return False
            conns = self.connections.connections_for(owner)
            target = None
            if trigger_conn is not None:
                target = next(
                    (c for c in conns if c.connection_id == trigger_conn.connection_id),
                    None,
                )
            if target is None:
                target = conns[0] if conns else None
            if target is None:
                await self.deferred.restore(owner, entries, now_ts)
                return False

            await target.send_json(status_frame("thinking"))
            language = (
                await self._owner_preferred_language() or self.config.DEFAULT_LANGUAGE
            )
            delivered = False
            assistant_row: dict = {}
            segments: list[dict] = []
            try:
                async with self.connections.turn_lock(owner):
                    # Persist the held user rows (deduplicated) so the thread
                    # reflects the owner's messages, fanning each out.
                    existing_ids = {
                        row.get("id")
                        for row in await hist.load_rows(self.cache, owner)
                    }
                    batch_ids: set[str] = set()
                    for entry in entries:
                        batch_ids.add(entry["message_id"])
                        if entry["message_id"] in existing_ids:
                            continue
                        row = {
                            "id": entry["message_id"],
                            "role": "user",
                            "text": entry["text"],
                            "emotion": DEFAULT_EMOTION,
                            "ts": datetime.fromtimestamp(
                                float(entry.get("created_ts", now_ts)),
                                tz=timezone.utc,
                            ).isoformat(),
                            "delivery_state": hist.DELIVERED,
                        }
                        await hist.append_row(
                            self.cache, owner, row, self.config.MAX_HISTORY_TURNS
                        )
                        await self._fan_out(
                            owner,
                            self._chat_sync(
                                row, "user", None, origin=entry.get(
                                    "source_connection_id"
                                )
                            ),
                        )
                    prompt_history = await hist.load_prompt_history(
                        self.cache,
                        owner,
                        self.config.LLM_HISTORY_MESSAGE_BUDGET,
                        exclude_ids=batch_ids,
                    )
                    blocks = await self._build_prompt_blocks(
                        owner, source_conn=target, prompt_history=prompt_history
                    )
                    # Boundary classification for held texts runs now, inside
                    # the history lock (deferred paths never classify).
                    if self.owner_profile.available and (
                        self.config.OWNER_BOUNDARY_PENALTIES_ENABLED
                    ):
                        for entry in entries:
                            try:
                                await self.owner_profile.record_boundary(
                                    owner, entry["text"], language
                                )
                            except Exception:  # noqa: BLE001
                                log.debug("boundary classification failed",
                                          exc_info=True)
                    if self.bids.available:
                        try:
                            await self.bids.satisfy_open_bids(
                                owner, " ".join(e["text"] for e in entries)
                            )
                        except Exception:  # noqa: BLE001
                            log.debug("bid satisfaction failed", exc_info=True)

                    messages = build_catchup_prompt(
                        soul_text=self._read_identity("soul"),
                        profile_text=self._read_identity("profile"),
                        history=prompt_history,
                        held_messages=[entry["text"] for entry in entries],
                        language=language,
                        state_block=blocks["state_block"],
                        owner_block=blocks["owner_block"],
                        awareness_block=blocks["awareness_block"],
                        context_feed=blocks["context_feed"],
                    )
                    result = await self.llm.chat("companion", messages)
                    segments = parse_emotion_segments(result.text)
                    if not _has_spoken_text(segments):
                        try:
                            retry = await self.llm.chat("companion", messages)
                            result = self._merge_usage(result, retry)
                            segments = parse_emotion_segments(retry.text)
                        except LLMChainExhausted:
                            pass
                    if not _has_spoken_text(segments):
                        raise LLMChainExhausted("empty catch-up reply")

                    reply_text = join_segments(segments)
                    assistant_row = hist.make_row(
                        "assistant", reply_text, hist.PENDING,
                        emotion=segments[0]["emotion"],
                    )
                    await hist.append_row(
                        self.cache, owner, assistant_row, self.config.MAX_HISTORY_TURNS
                    )
                    done = {
                        "type": "done",
                        "id": assistant_row["id"],
                        "text": reply_text,
                        "emotion": assistant_row["emotion"],
                        "segments": [dict(s) for s in segments],
                        "mode": "companion",
                        "provider": result.provider,
                        "model": result.model,
                        "initiated_by": "character",
                        "catchup": True,
                    }
                    if result.usage:
                        done["tokens"] = {
                            "prompt": result.usage.get("prompt_tokens", 0),
                            "completion": result.usage.get("completion_tokens", 0),
                            "total": result.usage.get("total_tokens", 0),
                        }
                    delivered = await target.send_json(done)
                    if delivered:
                        await hist.mark_delivery_state(
                            self.cache, owner, assistant_row["id"], hist.DELIVERED
                        )
                        await self._fan_out(
                            owner,
                            self._chat_sync(assistant_row, "character", target),
                            exclude=target,
                        )
                        if self.needs.available:
                            try:
                                await self.needs.turn_effects(
                                    owner, classify_turn_kind(reply_text)
                                )
                            except Exception:  # noqa: BLE001
                                log.debug("needs turn effects failed", exc_info=True)
                        if self.owner_profile.available:
                            try:
                                await self.owner_profile.apply_status_drift(owner)
                            except Exception:  # noqa: BLE001
                                log.debug("status drift failed", exc_info=True)
                        delivered_pending = [
                            rid for rid in blocks["pending_life_ids"] if rid
                        ]
                        if delivered_pending:
                            try:
                                await self.life.clear_pending(owner, delivered_pending)
                            except Exception:  # noqa: BLE001
                                log.debug("pending life clear failed", exc_info=True)
                    else:
                        await hist.mark_delivery_state(
                            self.cache, owner, assistant_row["id"], hist.UNDELIVERED
                        )
            except LLMChainExhausted:
                log.warning("Catch-up generation failed; entries restored to held")
                await self.deferred.restore(owner, entries, time.time())
                return False
            if delivered:
                await self.deferred.remove(owner, [entry["id"] for entry in entries])
                await self.deferred.reset_busy(owner)
                log.info(
                    "Catch-up delivered one answer for %d held message(s)",
                    len(entries),
                )
            else:
                await self.deferred.restore(owner, entries, time.time())
            return delivered

    # -- work mode (plan sections 25, 26, 11) --------------------------------------

    def _work_skills_path(self) -> Path:
        override = self.config.WORK_SKILLS_FILE.strip()
        if override:
            return Path(override)
        return REPO_ROOT / "skills" / "WORK_SKILLS.md"

    def _read_file_cached(self, path: Path) -> str:
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return ""
        cached = self._identity_cache.get(f"file:{path}")
        if cached and cached[0] == mtime:
            return cached[1]
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return ""
        self._identity_cache[f"file:{path}"] = (mtime, text)
        return text

    def _work_skills_text(self) -> str:
        if not self.config.WORK_SKILLS_ENABLED:
            return ""
        return self._read_file_cached(self._work_skills_path())

    def _session_context(self, owner: str, session: dict | None) -> str:
        if session is None:
            return ""
        lines = [
            f"[WORK SESSION] id: {session.get('id')}",
            f"Session status: {session.get('status')}",
        ]
        if session.get("summary"):
            lines.append(f"Previous summary: {str(session['summary'])[:500]}")
        project_id = session.get("project_id") or ""
        if project_id:
            lines.append(f"Project: {project_id}")
            return "\n".join(lines)
        return "\n".join(lines)

    def _device_level_for(self, owner: str) -> str:
        """Device tools are offered only when an armed connection exists
        (plan section 26.6); the prompt states availability honestly."""
        if not self.config.DEVICE_ENABLED:
            return ""
        if self.device.armed_connections(owner, "full"):
            return "full"
        if self.device.armed_connections(owner, "read"):
            return "read"
        return ""

    async def _write_checkpoint(
        self,
        owner: str,
        session_id: str,
        *,
        run_id: str,
        state: str,
        loop=None,
        last_error: str = "",
        started_ts: float = 0.0,
    ) -> None:
        """Bounded run/checkpoint record; stale run ids never overwrite
        newer runs (plan section 25.7)."""
        if not self.config.AGENT_CHECKPOINTS_ENABLED:
            return
        record = checkpoint_from_result(
            run_id=run_id,
            session_id=session_id,
            state=state if state in RUN_STATES else "failed",
            loop=loop,
            last_error=last_error,
        )
        record["started_ts"] = started_ts or record["updated_ts"]
        key = agent_run_key(owner, session_id)
        existing_raw = await self.cache.get_value(key)
        if existing_raw:
            try:
                existing = json.loads(existing_raw)
                if (
                    existing.get("run_id") != run_id
                    and float(existing.get("started_ts", 0) or 0)
                    > record["started_ts"]
                ):
                    return  # stale run id
            except (json.JSONDecodeError, TypeError, ValueError):
                pass
        await self.cache.set_value(key, json.dumps(record))

    async def _store_pending_pause(
        self,
        owner: str,
        session_id: str,
        run_id: str,
        status_tag: str,
        source_conn: Connection | None,
        transcript: list[dict],
    ) -> None:
        payload = json.dumps(
            transcript[-12:], ensure_ascii=False, default=str
        )[:8000]
        record = {
            "run_id": run_id,
            "session_id": session_id,
            "status": status_tag,
            "connection_id": source_conn.connection_id if source_conn else "",
            "created_ts": time.time(),
            "expires_ts": time.time() + 24 * 3600,
            "transcript": payload,
        }
        await self.cache.set_value(
            pending_agent_key(owner, session_id, run_id), json.dumps(record)
        )
        if source_conn is not None:
            self._pending_pauses[run_id] = {
                "session_id": session_id,
                "connection_id": source_conn.connection_id,
            }

    async def _read_pending_pause(
        self, owner: str, session_id: str, run_id: str
    ) -> dict | None:
        raw = await self.cache.get_value(
            pending_agent_key(owner, session_id, run_id)
        )
        if not raw:
            return None
        try:
            record = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if not isinstance(record, dict):
            return None
        if float(record.get("expires_ts", 0) or 0) < time.time():
            await self.cache.delete(pending_agent_key(owner, session_id, run_id))
            return None
        return record

    async def _clear_pending_pause(
        self, owner: str, session_id: str, run_id: str
    ) -> None:
        await self.cache.delete(pending_agent_key(owner, session_id, run_id))
        self._pending_pauses.pop(run_id, None)

    async def _mark_run_interrupted(
        self, owner: str, session_id: str, run_id: str
    ) -> None:
        """Disconnect converts a connection-only pending pause into
        interrupted; the durable checkpoint remains recoverable (25.6)."""
        self._pending_pauses.pop(run_id, None)
        key = agent_run_key(owner, session_id)
        raw = await self.cache.get_value(key)
        if not raw:
            return
        try:
            record = json.loads(raw)
        except json.JSONDecodeError:
            return
        if record.get("run_id") == run_id and record.get("state") == "paused":
            record["state"] = "interrupted"
            record["updated_ts"] = time.time()
            await self.cache.set_value(key, json.dumps(record))
            log.info("Run %s marked interrupted after disconnect", run_id[:16])

    async def run_work_turn(
        self,
        *,
        text: str,
        language: str,
        source_conn: Connection | None,
        wants_audio: bool = False,
        session_id: str | None = None,
        project_id: str | None = None,
        context: dict | None = None,
        explicit_run_id: str | None = None,
    ) -> dict:
        """One work-mode turn (plan sections 25.1, 25.4, 25.6).

        Serializes per session (plan section 11). Work bypasses the
        relationship soft block by design (plan 12 step 5); mood blocks are
        excluded so companion state cannot degrade work quality. Tool
        execution routes only to the originating WS connection (MCP) or an
        armed owner connection (device); HTTP-originated turns get no
        tools.
        """
        owner = self.config.OWNER_USER_ID
        if not (self.config.WORK_ENABLED and self.config.SESSIONS_ENABLED):
            frame = error_frame(
                "work_unavailable",
                "Work mode is not available on this server.",
                terminal=True,
            )
            if source_conn is not None:
                await source_conn.send_json(frame)
            return frame
        if not text.strip():
            frame = error_frame(
                "empty_input", "Message text must not be empty.", terminal=True
            )
            if source_conn is not None:
                await source_conn.send_json(frame)
            return frame

        # Plan 25.1 work availability policy (soft block never applies).
        availability = await self._effective_availability(owner)
        if availability in ("busy", "unavailable"):
            return await self._defer_turn(
                owner,
                text=text,
                language=language,
                source_conn=source_conn,
                availability=availability,
                mode="work",
                session_id=session_id,
            )

        try:
            session, _created = await self.sessions.resolve(
                owner, session_id=session_id, project_id=project_id
            )
        except SessionError as exc:
            frame = error_frame("session_error", str(exc), terminal=True)
            if source_conn is not None:
                await source_conn.send_json(frame)
            return frame

        session_id_resolved = session["id"]
        history_key = hist.session_history_key(owner, session_id_resolved)
        run_id = explicit_run_id or f"run_{uuid.uuid4().hex}"
        started_ts = time.time()
        resumed_record = None
        if explicit_run_id:
            resumed_record = await self._read_pending_pause(
                owner, session_id_resolved, run_id
            )

        lock = self.connections.session_lock(session_id_resolved)
        done: dict[str, Any]
        async with lock:
            if source_conn is not None:
                await source_conn.send_json(status_frame("working"))
            # Persist the user row before any provider/tool activity.
            user_row = hist.make_row(
                "user", text.strip(), hist.DELIVERED, mode="work"
            )
            await hist.append_row_to(
                self.cache, history_key, user_row, self.config.SESSION_HISTORY_TURNS
            )
            await self._fan_out(
                owner,
                self._chat_sync(user_row, "user", source_conn, mode="work"),
                exclude=source_conn,
            )
            prompt_history = await hist.load_prompt_history(
                self.cache,
                owner,
                self.config.SESSION_HISTORY_TURNS,
                key=history_key,
            )

            websocket_authorized = source_conn is not None
            device_level = (
                self._device_level_for(owner) if websocket_authorized else ""
            )
            web_permitted = source_conn is not None and self.web.available()
            web_schemas = (
                daily_tool_schemas(
                    web_enabled=True,
                    schedule_available=False,
                    user_schedule_available=False,
                )[-2:]
                if web_permitted
                else None
            )
            registry = WorkToolRegistry.build(
                context=context if websocket_authorized else None,
                device_level=device_level,
                max_chars=self.config.DEVICE_MAX_OUTPUT_CHARS,
                shell_timeout_max=self.config.DEVICE_SHELL_TIMEOUT_MAX,
                web_schemas=web_schemas,
            )
            turn_web = self.web.for_turn() if web_permitted else None
            turn_calls: list[str] = []

            async def executor(name: str, arguments: dict) -> dict:
                if name in ("web_search", "web_open"):
                    if turn_web is None:
                        return {
                            "ok": False,
                            "error": (
                                "tools_require_websocket"
                                if source_conn is None
                                else "web_disabled"
                            ),
                            "result": None,
                            "truncated": False,
                        }
                    if name == "web_search":
                        outcome = await turn_web.search(str(arguments.get("query", "")))
                    else:
                        outcome = await turn_web.open(str(arguments.get("url", "")))
                    return {
                        "ok": bool(outcome.get("ok")),
                        "result": outcome,
                        "error": outcome.get("error"),
                        "truncated": bool(outcome.get("truncated")),
                    }
                if not registry.has_tools or name not in registry.known:
                    return {
                        "ok": False, "error": "unknown_tool", "result": None,
                        "truncated": False,
                    }
                if name.startswith("mcp__"):
                    if source_conn is None:
                        return {
                            "ok": False, "error": "tools_require_websocket",
                            "result": None, "truncated": False,
                        }
                    parsed = parse_tool_name(name)
                    if parsed is None:
                        return {
                            "ok": False, "error": "invalid_tool_name",
                            "result": None, "truncated": False,
                        }
                    server, tool = parsed
                    return await self.mcp.call(
                        owner,
                        run_id=run_id,
                        source_conn=source_conn,
                        server=server,
                        tool=tool,
                        arguments=arguments,
                        turn_calls=turn_calls,
                    )
                return await self.device.call(
                    owner,
                    run_id=run_id,
                    tool=name,
                    arguments=arguments,
                    turn_calls=turn_calls,
                )

            if resumed_record is not None:
                try:
                    messages = json.loads(
                        resumed_record.get("transcript") or "[]"
                    )
                except json.JSONDecodeError:
                    messages = []
                if not isinstance(messages, list) or not messages:
                    messages = build_work_prompt(
                        soul_text=self._read_identity("soul"),
                        profile_text=self._read_identity("profile"),
                        skills_text=self._work_skills_text(),
                        history=prompt_history,
                        current_text=text.strip(),
                        language=language,
                        session_context=self._session_context(owner, session),
                        tools_note=registry.availability_note(),
                    )
                else:
                    messages.append({"role": "user", "content": text.strip()})
                log.info("Resuming paused run %s", run_id[:16])
            else:
                messages = build_work_prompt(
                    soul_text=self._read_identity("soul"),
                    profile_text=self._read_identity("profile"),
                    skills_text=self._work_skills_text(),
                    history=prompt_history,
                    current_text=text.strip(),
                    language=language,
                    session_context=self._session_context(owner, session),
                    tools_note=registry.availability_note(),
                )

            await self._write_checkpoint(
                owner,
                session_id_resolved,
                run_id=run_id,
                state="running",
                loop=None,
                started_ts=started_ts,
            )

            try:
                loop = await run_agent_loop(
                    self.llm,
                    messages=messages,
                    registry=registry,
                    executor=executor,
                    max_iterations=(
                        self.config.MCP_MAX_ITERATIONS
                        if registry.has_tools
                        else 1
                    ),
                    verification_enabled=(
                        self.config.MCP_VERIFICATION_ENABLED
                        and registry.has_tools
                    ),
                    verification_retries=self.config.MCP_VERIFICATION_RETRIES,
                    reject_tool_calls=not websocket_authorized,
                )
            except LLMChainExhausted:
                log.error("Work turn failed: LLM chain exhausted")
                await self._write_checkpoint(
                    owner,
                    session_id_resolved,
                    run_id=run_id,
                    state="failed",
                    loop=None,
                    last_error="llm_chain_exhausted",
                    started_ts=started_ts,
                )
                frame = error_frame(
                    "llm_unavailable",
                    "No LLM provider could produce a reply.",
                    terminal=True,
                )
                if source_conn is not None:
                    await source_conn.send_json(frame)
                return frame

            if loop.rejected_tool_calls:
                await self._write_checkpoint(
                    owner,
                    session_id_resolved,
                    run_id=run_id,
                    state="failed",
                    loop=loop,
                    last_error="tools_require_websocket",
                    started_ts=started_ts,
                )
                return error_frame(
                    "tools_require_websocket",
                    "Work tools require an originating WebSocket connection.",
                    terminal=True,
                )

            pause_tag = _parse_pause_status(loop.text)
            if pause_tag:
                paused_text = _strip_pause_tags(loop.text).strip()
                await self._store_pending_pause(
                    owner,
                    session_id_resolved,
                    run_id,
                    pause_tag,
                    source_conn,
                    loop.transcript,
                )
                await self._write_checkpoint(
                    owner,
                    session_id_resolved,
                    run_id=run_id,
                    state="paused",
                    loop=loop,
                    started_ts=started_ts,
                )
                await self.sessions.update(
                    owner, session_id_resolved, last_run_id=run_id
                )
                if source_conn is not None:
                    # No done frame on pause (plan 30.2); the status frame
                    # carries the bounded question text.
                    await source_conn.send_json(
                        status_frame(pause_tag, message=paused_text[:200])
                    )
                return {
                    "type": "paused",
                    "session_id": session_id_resolved,
                    "run_id": run_id,
                    "status": pause_tag,
                    "message": paused_text[:200],
                }

            segments = parse_emotion_segments(loop.text)
            if not _has_spoken_text(segments):
                try:
                    retry_messages = [
                        dict(message) for message in loop.transcript
                    ]
                    retry = await self.llm.chat(
                        "work", retry_messages, pinned=None
                    )
                    loop.text = retry.text
                    loop.attempts += retry.attempts
                    if retry.usage:
                        for key, value in retry.usage.items():
                            loop.usage[key] = loop.usage.get(key, 0) + value
                    segments = parse_emotion_segments(retry.text)
                except LLMChainExhausted:
                    pass
            if not _has_spoken_text(segments):
                log.error("Work turn failed: empty reply after retry")
                await self._write_checkpoint(
                    owner,
                    session_id_resolved,
                    run_id=run_id,
                    state="failed",
                    loop=loop,
                    last_error="empty_reply",
                    started_ts=started_ts,
                )
                frame = error_frame(
                    "empty_reply",
                    "The provider returned an empty reply.",
                    terminal=True,
                )
                if source_conn is not None:
                    await source_conn.send_json(frame)
                return frame

            reply_text = join_segments(segments)
            emotion = segments[0]["emotion"]
            assistant_row = hist.make_row(
                "assistant", reply_text, hist.PENDING,
                emotion=emotion, mode="work",
            )
            await hist.append_row_to(
                self.cache, history_key, assistant_row,
                self.config.SESSION_HISTORY_TURNS,
            )
            done = {
                "type": "done",
                "id": assistant_row["id"],
                "text": reply_text,
                "emotion": emotion,
                "segments": [dict(segment) for segment in segments],
                "mode": "work",
                "provider": loop.provider,
                "model": loop.model,
                "session_id": session_id_resolved,
                "run_id": run_id,
                "initiated_by": "user",
            }
            if loop.usage:
                done["tokens"] = {
                    "prompt": loop.usage.get("prompt_tokens", 0),
                    "completion": loop.usage.get("completion_tokens", 0),
                    "total": loop.usage.get("total_tokens", 0),
                }
            if source_conn is not None:
                delivered = await source_conn.send_json(done)
            else:
                delivered = True
            if delivered:
                await hist.mark_delivery_state_key(
                    self.cache, history_key, assistant_row["id"], hist.DELIVERED
                )
                await self._fan_out(
                    owner,
                    self._chat_sync(assistant_row, "character", source_conn,
                                    mode="work"),
                )
                await self._write_checkpoint(
                    owner,
                    session_id_resolved,
                    run_id=run_id,
                    state="completed",
                    loop=loop,
                    started_ts=started_ts,
                )
                await self._clear_pending_pause(
                    owner, session_id_resolved, run_id
                )
                await self.sessions.update(
                    owner, session_id_resolved, last_run_id=run_id
                )
            else:
                await hist.mark_delivery_state_key(
                    self.cache, history_key, assistant_row["id"], hist.UNDELIVERED
                )
                await self._write_checkpoint(
                    owner,
                    session_id_resolved,
                    run_id=run_id,
                    state="failed",
                    loop=loop,
                    last_error="delivery_failed",
                    started_ts=started_ts,
                )
            log.info(
                "Work turn completed: session=%s run=%s iterations=%d "
                "tool_calls=%d delivered=%s",
                session_id_resolved,
                run_id[:16],
                loop.iterations,
                loop.tool_calls_made,
                delivered,
            )

        if wants_audio and delivered and source_conn is not None:
            await self._stream_tts(source_conn, done["id"], segments)
        if delivered:
            work_catchup = asyncio.create_task(
                self._maybe_work_catchup(owner, trigger_conn=source_conn)
            )
            self.background_tasks.add(work_catchup)
            work_catchup.add_done_callback(self.background_tasks.discard)
        return done

    async def _maybe_work_catchup(
        self, owner: str, trigger_conn: Connection | None = None
    ) -> bool:
        if not (
            self.config.SCHEDULE_ENABLED
            and self.schedule is not None
            and self.schedule.available
            and self.config.WORK_ENABLED
            and self.config.SESSIONS_ENABLED
        ):
            return False
        try:
            return await self.run_work_catchup(owner, trigger_conn=trigger_conn)
        except Exception:  # noqa: BLE001 - maintenance never fails callers
            log.debug("work catch-up attempt failed", exc_info=True)
            return False

    async def run_work_catchup(
        self, owner: str, *, trigger_conn: Connection | None = None
    ) -> bool:
        """Text-only, tool-less work catch-up (plan section 16.3).

        Claims work entries separately from companion entries, groups them
        by original session, and answers each group once in work voice
        tied to the original session/project metadata. Failures restore
        that group's entries to held.
        """
        async with self.connections.catchup_lock(owner):
            availability = await self._effective_availability(owner)
            if availability not in ("free", "soft_busy"):
                return False
            now_ts = time.time()
            entries = await self.deferred.claim(owner, "work", now_ts)
            if not entries:
                return False
            session_id = entries[0].get("session_id") or ""
            group = [
                entry
                for entry in entries
                if (entry.get("session_id") or "") == session_id
            ]
            others = [entry for entry in entries if entry not in group]
            if others:
                # Deliver one session group per trigger; the rest stay
                # claimed for the next pass only if delivered here —
                # otherwise restore everything together.
                pass
            conns = self.connections.connections_for(owner)
            target = None
            if trigger_conn is not None:
                target = next(
                    (
                        c
                        for c in conns
                        if c.connection_id == trigger_conn.connection_id
                    ),
                    None,
                )
            if target is None:
                target = conns[0] if conns else None
            if target is None:
                await self.deferred.restore(owner, entries, now_ts)
                return False

            session = (
                await self.sessions.get(owner, session_id) if session_id else None
            )
            history_key = (
                hist.session_history_key(owner, session["id"])
                if session
                else None
            )
            delivered = False
            try:
                if session is not None:
                    async with self.connections.session_lock(session["id"]):
                        delivered = await self._deliver_work_catchup(
                            owner,
                            entries=group,
                            session=session,
                            history_key=history_key,
                            target=target,
                            language=(
                                await self._owner_preferred_language()
                                or self.config.DEFAULT_LANGUAGE
                            ),
                        )
                else:
                    delivered = await self._deliver_work_catchup(
                        owner,
                        entries=group,
                        session=None,
                        history_key=None,
                        target=target,
                        language=(
                            await self._owner_preferred_language()
                            or self.config.DEFAULT_LANGUAGE
                        ),
                    )
            except LLMChainExhausted:
                log.warning("Work catch-up generation failed; entries restored")
                delivered = False
            if delivered:
                await self.deferred.remove(owner, [e["id"] for e in group])
                if others:
                    await self.deferred.restore(owner, others, time.time())
                log.info(
                    "Work catch-up delivered for %d held message(s)", len(group)
                )
            else:
                await self.deferred.restore(owner, entries, time.time())
            return delivered

    async def _deliver_work_catchup(
        self,
        owner: str,
        *,
        entries: list[dict],
        session: dict | None,
        history_key: str | None,
        target: Connection,
        language: str,
    ) -> bool:
        messages = build_work_catchup_prompt(
            soul_text=self._read_identity("soul"),
            profile_text=self._read_identity("profile"),
            skills_text=self._work_skills_text(),
            session_context=self._session_context(owner, session),
            held_messages=[entry["text"] for entry in entries],
            language=language,
        )
        result = await self.llm.chat("work", messages)
        segments = parse_emotion_segments(result.text)
        if not _has_spoken_text(segments):
            retry = await self.llm.chat("work", messages)
            result = self._merge_usage(result, retry)
            segments = parse_emotion_segments(retry.text)
        if not _has_spoken_text(segments):
            raise LLMChainExhausted("empty work catch-up reply")
        reply_text = join_segments(segments)
        assistant_row = hist.make_row(
            "assistant", reply_text, hist.PENDING,
            emotion=segments[0]["emotion"], mode="work",
        )
        max_rows = self.config.SESSION_HISTORY_TURNS
        if history_key is not None:
            existing_ids = {
                row.get("id")
                for row in await hist.load_rows_from(self.cache, history_key)
            }
            for entry in entries:
                if entry["message_id"] in existing_ids:
                    continue
                user_row = {
                    "id": entry["message_id"],
                    "role": "user",
                    "text": entry["text"],
                    "emotion": DEFAULT_EMOTION,
                    "mode": "work",
                    "ts": datetime.fromtimestamp(
                        float(entry.get("created_ts", time.time())),
                        tz=timezone.utc,
                    ).isoformat(),
                    "delivery_state": hist.DELIVERED,
                }
                await hist.append_row_to(
                    self.cache, history_key, user_row, max_rows
                )
                await self._fan_out(
                    owner,
                    self._chat_sync(
                        user_row,
                        "user",
                        None,
                        origin=entry.get("source_connection_id"),
                        mode="work",
                    ),
                )
            await hist.append_row_to(
                self.cache, history_key, assistant_row, max_rows
            )
        done = {
            "type": "done",
            "id": assistant_row["id"],
            "text": reply_text,
            "emotion": assistant_row["emotion"],
            "segments": [dict(segment) for segment in segments],
            "mode": "work",
            "provider": result.provider,
            "model": result.model,
            "initiated_by": "character",
            "catchup": True,
        }
        if session is not None:
            done["session_id"] = session["id"]
        delivered = await target.send_json(done)
        if delivered and history_key is not None:
            await hist.mark_delivery_state_key(
                self.cache, history_key, assistant_row["id"], hist.DELIVERED
            )
            await self._fan_out(
                owner,
                self._chat_sync(assistant_row, "character", target, mode="work"),
                exclude=target,
            )
        elif not delivered and history_key is not None:
            await hist.mark_delivery_state_key(
                self.cache, history_key, assistant_row["id"], hist.UNDELIVERED
            )
        return delivered

    async def _companion_tool_loop(
        self, messages: list[dict], user_text: str
    ) -> LLMResult:
        """Companion provider call with the bounded private daily-tool loop
        (plan section 24.2). Without ``DAILY_TOOLS_ENABLED`` this is one
        plain routed call. At most ``DAILY_TOOL_MAX_CALLS`` tool executions
        per turn; the first successful provider stays pinned; final speech
        passes the deterministic narration sanitizer, with one tool-less
        synthesis retry when sanitization leaves nothing speakable."""
        cfg = self.config
        schemas: list[dict] | None = None
        web_available = self.web.available()
        if cfg.DAILY_TOOLS_ENABLED:
            schemas = daily_tool_schemas(
                web_enabled=web_available,
                schedule_available=self.schedule is not None and self.schedule.available,
                user_schedule_available=self.user_schedule.available,
            )
        turn_web = self.web.for_turn() if web_available else None
        result = await self.llm.chat("companion", messages, tools=schemas)
        if not schemas:
            return result

        pinned = ProviderRoute(provider=result.provider, model=result.model)
        transcript = list(messages)
        turn_id = f"turn_{uuid.uuid4().hex}"
        calls_used = 0
        while result.tool_calls and calls_used < cfg.DAILY_TOOL_MAX_CALLS:
            transcript.append({
                "role": "assistant",
                "content": result.text or "",
                "tool_calls": [
                    {
                        "id": call["id"],
                        "type": "function",
                        "function": {
                            "name": call["name"],
                            "arguments": call["arguments"],
                        },
                    }
                    for call in result.tool_calls
                ],
            })
            for call in result.tool_calls:
                if calls_used >= cfg.DAILY_TOOL_MAX_CALLS:
                    tool_result = {"ok": False, "error": "tool_call_cap_reached"}
                else:
                    calls_used += 1
                    arguments = _parse_tool_arguments(call.get("arguments", ""))
                    context = ToolContext(
                        owner=cfg.OWNER_USER_ID,
                        user_text=user_text,
                        turn_id=turn_id,
                        tool_call_id=call["id"],
                        reminders=self.reminders,
                        idempotency=self.daily_idempotency,
                        web=turn_web,
                        longterm=self.longterm,
                        schedule=self.schedule,
                        user_schedule=self.user_schedule
                        if self.user_schedule.available
                        else None,
                        character_timezone=cfg.CHARACTER_TIMEZONE,
                        calls_used={"n": calls_used},
                    )
                    tool_result = await self.daily_exec.execute(
                        call["name"], arguments, context
                    )
                try:
                    encoded = json.dumps(tool_result, ensure_ascii=False)
                except (TypeError, ValueError):
                    encoded = '{"ok": false, "error": "unserializable_result"}'
                transcript.append({
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": encoded[:DAILY_TOOL_RESULT_MAX_CHARS],
                })
            result = await self.llm.chat(
                "companion", transcript, tools=schemas, pinned=pinned
            )

        final_text = sanitize_daily_reply(result.text)
        if not final_text.strip() and result.text.strip():
            # Sanitization removed all speech: retry synthesis once without
            # tools (plan section 24.2).
            try:
                retry = await self.llm.chat("companion", messages, pinned=pinned)
                final_text = sanitize_daily_reply(retry.text)
                if not final_text.strip():
                    final_text = _DAILY_SANITIZER_FALLBACK
                result.attempts += retry.attempts
                if retry.usage:
                    usage = dict(result.usage or {})
                    for key, value in retry.usage.items():
                        usage[key] = usage.get(key, 0) + int(value)
                    result.usage = usage
            except LLMChainExhausted:
                final_text = _DAILY_SANITIZER_FALLBACK
        result.text = final_text
        result.tool_calls = None
        return result

    async def _maybe_compact(self, owner: str) -> None:
        """Background compaction + policy cleanup (plan sections 20.2, 20.5).

        Runs under the per-owner turn lock; any failure preserves history
        (plan acceptance) and only logs.
        """
        lock = self.connections.turn_lock(owner)
        async with lock:
            rows = await hist.load_rows(self.cache, owner)
            if self.midterm.compaction_needed(rows):
                try:
                    await self.midterm.compact(owner, rows, now_ts=time.time())
                except Exception:  # noqa: BLE001 - background task owns errors
                    log.warning("Compaction failed; history preserved", exc_info=True)
            if self.config.MEMORY_CLEANUP_ENABLED:
                interval = max(self.config.MEMORY_CLEANUP_INTERVAL_HOURS, 1) * 3600
                if time.time() - self._last_memory_cleanup >= interval:
                    self._last_memory_cleanup = time.time()
                    try:
                        await self.longterm.cleanup(owner, dry_run=False)
                    except Exception:  # noqa: BLE001
                        log.warning("Memory cleanup failed", exc_info=True)

    async def run_companion_turn(
        self,
        *,
        text: str,
        language: str,
        source_conn: Connection | None,
        wants_audio: bool = False,
    ) -> dict:
        """Run one text companion turn. Returns the terminal frame.

        ``source_conn`` is None for HTTP-originated turns, where returning the
        response body is the delivery path.
        """
        owner = self.config.OWNER_USER_ID

        if not text.strip():
            frame = error_frame(
                "empty_input", "Message text must not be empty.", terminal=True
            )
            if source_conn is not None:
                await source_conn.send_json(frame)
            return frame

        # Plan 12 step 5: owner-profile soft block, companion mode only.
        soft_block = await self._maybe_soft_block(owner, language, source_conn)
        if soft_block is not None:
            return soft_block

        # Plan 12 steps 6-7: character schedule and effective availability.
        # Busy/unavailable messages defer into the bounded queue without an
        # LLM call or history write; the first message in a busy window may
        # speak the authored static line (plan section 16.3).
        availability = await self._effective_availability(owner)
        if availability in ("busy", "unavailable"):
            return await self._defer_turn(
                owner,
                text=text,
                language=language,
                source_conn=source_conn,
                availability=availability,
            )

        # Status frames go to the source connection only; status emotions
        # never become final reply emotions (plan section 10.4).
        if source_conn is not None:
            await source_conn.send_json(status_frame("thinking"))

        # Plan 12 step 9: stamp owner activity (rhythm; metadata only).
        if self.rhythm.available:
            tz_name = (
                source_conn.timezone if source_conn else ""
            ) or self.config.OWNER_TIMEZONE
            try:
                await self.rhythm.stamp_contact(owner, tz_name)
            except Exception:  # noqa: BLE001 - advisory hook never fails a turn
                log.debug("rhythm stamp failed", exc_info=True)

        lock = self.connections.turn_lock(owner)
        delivered = False
        segments: list[dict] = []
        assistant_row: dict = {}
        done: dict[str, Any]
        async with lock:
            # Plan 12 step 8: bids owner-message hook (deterministic).
            if self.bids.available:
                try:
                    await self.bids.satisfy_open_bids(owner, text)
                except Exception:  # noqa: BLE001
                    log.debug("bid satisfaction failed", exc_info=True)

            # Persist the user row BEFORE the provider call (plan 12.11).
            user_row = hist.make_row("user", text.strip(), hist.DELIVERED)
            await hist.append_row(
                self.cache, owner, user_row, self.config.MAX_HISTORY_TURNS
            )
            await self._fan_out(
                owner, self._chat_sync(user_row, "user", source_conn), exclude=source_conn
            )

            prompt_history = await hist.load_prompt_history(
                self.cache,
                owner,
                self.config.LLM_HISTORY_MESSAGE_BUDGET,
                exclude_id=user_row["id"],
            )

            # Plan 12 step 16: deterministic boundary classification for the
            # live owner message (deferred texts classify at catch-up).
            if self.owner_profile.available and (
                self.config.OWNER_BOUNDARY_PENALTIES_ENABLED
            ):
                try:
                    await self.owner_profile.record_boundary(owner, text, language)
                except Exception:  # noqa: BLE001
                    log.debug("boundary classification failed", exc_info=True)

            # Plan 12 steps 14-16 + identity layer 5: shared block building
            # (needs, owner profile, awareness, bounded context feed).
            blocks = await self._build_prompt_blocks(
                owner,
                source_conn=source_conn,
                prompt_history=prompt_history,
                current_text=text.strip(),
            )
            prompt_profile = blocks["prompt_profile"]

            soft_busy_note = (
                availability == "soft_busy"
                and self.config.SCHEDULE_SOFT_BUSY_POLICY == "short"
            )
            messages = build_companion_prompt(
                soul_text=self._read_identity("soul"),
                profile_text=self._read_identity("profile"),
                history=prompt_history,
                current_text=text.strip(),
                language=language,
                state_block=blocks["state_block"],
                owner_block=blocks["owner_block"],
                awareness_block=blocks["awareness_block"],
                context_feed=blocks["context_feed"] or blocks["chapter_block"],
                soft_busy_note=soft_busy_note,
            )

            wants_analysis = bool(
                self.owner_profile.available and self.config.OWNER_PROFILE_LLM_ENABLED
            )
            exchange = (
                {
                    "user_text": text.strip(),
                    "assistant_text": "",
                    "language": language,
                }
                if wants_analysis
                else None
            )

            try:
                result = await self._companion_tool_loop(messages, text.strip())
            except LLMChainExhausted as exc:
                log.error("Companion turn failed: LLM chain exhausted")
                frame = error_frame(
                    "llm_unavailable",
                    "No LLM provider could produce a reply.",
                    terminal=True,
                )
                if source_conn is not None:
                    await source_conn.send_json(frame)
                return frame

            segments = parse_emotion_segments(result.text)
            if not _has_spoken_text(segments):
                # Emotion-only/empty reply is invalid; retry once (7.3, 12.21).
                log.warning("Empty/emotion-only reply; retrying once")
                try:
                    retry = await self.llm.chat("companion", messages)
                    result = self._merge_usage(result, retry)
                    segments = parse_emotion_segments(retry.text)
                except LLMChainExhausted:
                    pass
            if not _has_spoken_text(segments):
                log.error("Companion turn failed: empty reply after retry")
                frame = error_frame(
                    "empty_reply",
                    "The provider returned an empty reply.",
                    terminal=True,
                )
                if source_conn is not None:
                    await source_conn.send_json(frame)
                return frame

            reply_text = join_segments(segments)
            emotion = segments[0]["emotion"]
            assistant_row = hist.make_row(
                "assistant", reply_text, hist.PENDING, emotion=emotion
            )
            await hist.append_row(
                self.cache, owner, assistant_row, self.config.MAX_HISTORY_TURNS
            )

            done = {
                "type": "done",
                "id": assistant_row["id"],
                "text": reply_text,
                "emotion": emotion,
                "segments": [dict(segment) for segment in segments],
                "mode": "companion",
                "provider": result.provider,
                "model": result.model,
                "initiated_by": "user",
            }
            if result.usage:
                done["tokens"] = {
                    "prompt": result.usage.get("prompt_tokens", 0),
                    "completion": result.usage.get("completion_tokens", 0),
                    "total": result.usage.get("total_tokens", 0),
                }

            if source_conn is not None:
                delivered = await source_conn.send_json(done)
            else:
                delivered = True  # HTTP response return is the delivery path

            if delivered:
                await hist.mark_delivery_state(
                    self.cache, owner, assistant_row["id"], hist.DELIVERED
                )
                await self._fan_out(
                    owner,
                    self._chat_sync(assistant_row, "character", source_conn),
                    exclude=source_conn,
                )
                # Plan 12 step 29: needs/bid effects only after delivered
                # assistant state.
                if self.needs.available:
                    try:
                        await self.needs.turn_effects(
                            owner, classify_turn_kind(text)
                        )
                    except Exception:  # noqa: BLE001
                        log.debug("needs turn effects failed", exc_info=True)
                # Status drift runs on delivered turns (flag-gated, 18.2).
                if self.owner_profile.available:
                    try:
                        await self.owner_profile.apply_status_drift(owner)
                    except Exception:  # noqa: BLE001
                        log.debug("status drift failed", exc_info=True)
                # Plan 12 step 34: clear delivered pending life mentions only
                # after a successful response that received the context.
                delivered_pending = [
                    rid for rid in blocks["pending_life_ids"] if rid
                ]
                if delivered_pending:
                    try:
                        await self.life.clear_pending(owner, delivered_pending)
                    except Exception:  # noqa: BLE001
                        log.debug("pending life clear failed", exc_info=True)
            else:
                # Undelivered: excluded from future prompts, no fanout (12.28).
                await hist.mark_delivery_state(
                    self.cache, owner, assistant_row["id"], hist.UNDELIVERED
                )
            log.info(
                "Companion turn completed: provider=%s attempts=%d delivered=%s",
                result.provider,
                result.attempts,
                delivered,
            )

        # Plan 12 step 33: enqueue optional strict-JSON owner-profile
        # analysis, only from delivered exchanges (flag-gated, background).
        if delivered and exchange is not None and assistant_row:
            exchange["assistant_text"] = reply_text
            self._start_profile_analysis(owner, exchange, prompt_profile)

        # TTS runs outside the per-owner turn lock: it mutates no shared state
        # and streams only to the requesting connection (plan section 12
        # step 31). The done frame always precedes audio chunks (10.6).
        if wants_audio and delivered and source_conn is not None:
            await self._stream_tts(source_conn, assistant_row["id"], segments)

        # Plan 16.3: availability may have just freed; answer any held
        # companion messages once, outside the turn lock, under the
        # catch-up lock. Held work entries get their own text-only pass.
        if delivered and self.schedule is not None and self.schedule.available:
            catchup_task = asyncio.create_task(
                self._maybe_catchup(owner, trigger_conn=source_conn)
            )
            self.background_tasks.add(catchup_task)
            catchup_task.add_done_callback(self.background_tasks.discard)
            work_catchup_task = asyncio.create_task(
                self._maybe_work_catchup(owner, trigger_conn=source_conn)
            )
            self.background_tasks.add(work_catchup_task)
            work_catchup_task.add_done_callback(self.background_tasks.discard)

        # Plan 20.2/20.5: mid-term compaction + policy cleanup, background,
        # under the turn lock; failure preserves history.
        if delivered:
            compact_task = asyncio.create_task(self._maybe_compact(owner))
            self.background_tasks.add(compact_task)
            compact_task.add_done_callback(self.background_tasks.discard)
        return done

    def _start_profile_analysis(
        self, owner: str, exchange: dict, prompt_profile: dict | None
    ) -> None:
        async def _analyze() -> None:
            try:
                profile = prompt_profile
                if profile is None:
                    profile = await self.owner_profile.get(owner)
                if profile is None:
                    return
                open_agreements = [
                    agreement
                    for agreement in profile.get("agreements", [])
                    if agreement.get("status") == "active"
                ] if self.config.OWNER_AGREEMENTS_ENABLED else []
                messages = build_owner_profile_analysis_prompt(
                    current_profile=profile,
                    exchange=exchange,
                    open_agreements=open_agreements,
                )
                result = await self.llm.chat("owner_profile", messages)
                proposal = _parse_strict_json_object(result.text)
                if proposal is None:
                    log.info("Owner-profile analysis skipped: non-JSON proposal")
                    return
                async with self.connections.profile_lock(owner):
                    current = await self.owner_profile.get(owner)
                    if current is None:
                        return
                    updated, reject = validate_and_apply_proposal(
                        current,
                        proposal,
                        max_delta=OWNER_PROPOSAL_MAX_DELTA,
                    )
                    if reject is not None:
                        log.info("Owner-profile proposal rejected: %s", reject)
                        return
                    saved = await self.owner_profile.upsert(
                        owner, updated, int(current.get("version", 1))
                    )
                if saved is not None:
                    log.info(
                        "Owner-profile proposal applied (%s)",
                        saved.get("status", "unknown"),
                    )
            except Exception:  # noqa: BLE001 - analysis must never break turns
                log.warning("Owner-profile analysis failed", exc_info=True)

        task = asyncio.create_task(_analyze())
        self.background_tasks.add(task)
        task.add_done_callback(self.background_tasks.discard)

    # -- TTS chunk stream (plan sections 10.6, 13.4) -----------------------------

    async def _stream_tts(
        self, conn: Connection, message_id: str, segments: list[dict]
    ) -> None:
        """Sequential pipelined chunk stream with one-chunk lookahead.

        - ``done`` was already sent; chunks follow in deterministic order.
        - A failed synthesis skips that chunk's audio but never removes the
          text reply; exactly one bounded audio-error status precedes the
          terminal ``audio_complete`` when any chunk failed.
        - A failed send (disconnect) cancels pending synthesis immediately.
        """
        if not self.tts.available():
            await conn.send_json(
                status_frame("error", "Audio output is unavailable on this server.")
            )
            await conn.send_json(_audio_complete(message_id, 0, 0))
            return

        chunks = chunk_segments(
            segments, self.config.TTS_CHUNK_THRESHOLD, self.config.TTS_CHUNK_SIZE
        )
        total = len(chunks)
        if total == 0:
            await conn.send_json(_audio_complete(message_id, 0, 0))
            return

        spacing = max(self.config.TTS_CHUNK_SPACING_MS, 0) / 1000.0
        on_deck: list[asyncio.Task] = []

        def start_next(index: int) -> None:
            on_deck.append(
                asyncio.create_task(
                    self.tts.synthesize(chunks[index]["text"], chunks[index]["emotion"])
                )
            )

        start_next(0)
        if total > 1:
            start_next(1)

        current: asyncio.Task | None = None
        succeeded = 0
        failed = 0
        try:
            for index in range(total):
                current = on_deck.pop(0)
                audio: bytes | None
                try:
                    audio = await current
                except TTSError as exc:
                    failed += 1
                    log.warning("TTS chunk %d failed (%s); text reply stands",
                                index, exc)
                    audio = None
                if index + 2 < total:
                    start_next(index + 2)
                if audio is None:
                    continue
                delivered = await conn.send_json(
                    {
                        "type": "audio_chunk",
                        "id": message_id,
                        "text": chunks[index]["text"],
                        "emotion": chunks[index]["emotion"],
                        "chunk_index": index,
                        "total_chunks": total,
                        "is_final": index == total - 1,
                        "audio": base64.b64encode(audio).decode("ascii"),
                        "audio_format": self.tts.audio_format,
                    }
                )
                if not delivered:
                    # Disconnect: cancel pending synthesis (plan section 10.6).
                    return
                succeeded += 1
                if index + 1 < total and spacing > 0:
                    await asyncio.sleep(spacing)
            if failed:
                await conn.send_json(
                    status_frame("error", "Some audio chunks could not be synthesized.")
                )
            await conn.send_json(_audio_complete(message_id, succeeded, failed))
        finally:
            pending = [task for task in [current, *on_deck] if task is not None]
            for task in pending:
                if not task.done():
                    task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

    # -- helpers ------------------------------------------------------------------

    @staticmethod
    def _merge_usage(first: LLMResult, second: LLMResult) -> LLMResult:
        """Usage is additive across retries (plan section 9.3)."""
        if first.usage or second.usage:
            merged: dict[str, int] = {}
            for usage in (first.usage, second.usage):
                for key, value in (usage or {}).items():
                    merged[key] = merged.get(key, 0) + value
            second.usage = merged
        second.attempts += first.attempts
        return second

    def _chat_sync(
        self,
        row: dict,
        initiated_by: str,
        source_conn: Connection | None,
        origin: str | None = None,
        mode: str = "companion",
    ) -> dict:
        frame: dict[str, Any] = {
            "type": "chat_sync",
            "role": row["role"],
            "text": row["text"],
            "emotion": row.get("emotion", "neutral"),
            "mode": row.get("mode", mode),
            "initiated_by": initiated_by,
            "id": row["id"],
            "origin_connection_id": (
                origin
                or (source_conn.connection_id if source_conn is not None else "http")
            ),
            "ts": row["ts"],
        }
        # Initiative origin metadata rides along additively (plan section
        # 23.5); user replies never carry it because their rows never do.
        if row.get("initiative"):
            frame["initiative"] = True
            frame["initiative_action"] = row.get("initiative_action", "")
        return frame

    async def _fan_out(self, owner: str, frame: dict, exclude: Connection | None = None) -> None:
        try:
            await self.connections.fan_out(
                owner,
                frame,
                exclude_connection_id=exclude.connection_id if exclude else None,
            )
        except Exception:  # noqa: BLE001 - fanout failure never rolls back
            log.warning("chat_sync fanout failed; persistence stands")

    async def shutdown(self) -> None:
        """Cancel and await background tasks (plan section 6.1)."""
        if self._life_task is not None:
            if self._life_stop is not None:
                self._life_stop.set()
            self._life_task.cancel()
            try:
                await self._life_task
            except asyncio.CancelledError:
                pass
            self._life_task = None
        await self.web.aclose()
        for task in list(self.background_tasks):
            task.cancel()
        if self.background_tasks:
            await asyncio.gather(*self.background_tasks, return_exceptions=True)
        self.background_tasks.clear()
        await self.llm.aclose()
        await self.stt.aclose()
        await self.tts.aclose()


def _has_spoken_text(segments: list[dict]) -> bool:
    return any(str(segment.get("text", "")).strip() for segment in segments)


def _is_silence(segments: list[dict]) -> bool:
    """Proactive-mode silence (plan 23.3 step 14): no spoken text at all,
    or the literal token SILENCE as the only spoken content."""
    spoken = " ".join(
        str(segment.get("text", "")).strip() for segment in segments
    ).strip()
    if not spoken:
        return True
    return spoken.strip("[].!? \n").upper() == "SILENCE"


def _parse_pause_status(text: str) -> str | None:
    """Work pause tags parse before final emotion validation (25.6)."""
    tag = parse_pause_status(text)
    return tag if tag in PAUSE_STATUSES else None


def _strip_pause_tags(text: str) -> str:
    return strip_pause_tags(text)


def _audio_complete(message_id: str, succeeded: int, failed: int) -> dict:
    return {
        "type": "audio_complete",
        "id": message_id,
        "succeeded_chunks": succeeded,
        "failed_chunks": failed,
    }
