"""Bridge: wiring and companion turn lifecycle (plan sections 10-12, 14-18, 21-22).

Holds the config, cache, connection manager, LLM router, speech services, the
0.3.0 needs/interaction/owner-profile engines, and the 0.4.0 schedule, life,
awareness, catch-up, and contextual owner-schedule engines.

Turn lifecycle (work sessions, memory extraction, and initiative do not exist
yet and remain flag-off inert):

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

Every turn terminates with a ``done`` frame or a terminal error frame (plan
section 30.2). Empty/failed STT returns a localized static line (or
protocol-only metadata) with a terminal ``done`` and makes no LLM/history
call. Catch-up runs under its own per-owner lock and only when availability
is free/soft_busy; work/companion deferred entries stay separated by mode
(work itself ships in 0.5.0).
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import WebSocket, WebSocketDisconnect

from . import history as hist
from .bids import BidsEngine
from .cache import RedisCache
from .config import Config
from .connections import Connection, ConnectionManager
from .constants import (
    DEFAULT_EMOTION,
    HEARTBEAT_MAX_AGE_SECONDS,
    HEARTBEAT_MAX_FUTURE_SECONDS,
    INITIATIVE_COUNTER_STUB,
    STATUS_TO_EMOTION,
    SUPPORTED_LANGUAGES,
    VERSION,
)
from .context_feed import build_awareness_block, build_context_feed
from .emotions import load_emotions_manifest
from .interaction import DeferredQueue
from .life import LifeEngine
from .llm import LLMChainExhausted, LLMResult, LLMRouter
from .memory import LongTermMemory
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
    build_owner_profile_analysis_prompt,
)
from .rhythm import RhythmEngine
from .schedule import Schedule
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
from .text_utils import chunk_segments, join_segments, parse_emotion_segments
from .user_schedule import UserSchedule

log = logging.getLogger("bridge.turn")

REPO_ROOT = Path(__file__).resolve().parent.parent

_MAX_SEQUENCE = 2**64 - 1

# Strict-JSON proposal clamp for background analysis (plan section 18.6).
OWNER_PROPOSAL_MAX_DELTA = PROPOSAL_MAX_DELTA


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
        self.longterm = LongTermMemory(config, cache)
        self.schedule: Schedule | None = None
        self.life: LifeEngine | None = None
        self.emotions_manifest: dict = {}
        self.static_lines: dict = {}
        self.deployment_mode = "unknown"
        self.background_tasks: set[asyncio.Task] = set()
        self._identity_cache: dict[str, tuple[float, str]] = {}
        self._started_monotonic = time.monotonic()
        self._last_bid_sweep = 0.0
        self._life_task: asyncio.Task | None = None
        self._life_stop: asyncio.Event | None = None

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

    def capabilities(self) -> list[str]:
        """What this build actually supports right now (see SPEC)."""
        caps = ["text"]
        if self.tts.available():
            caps.append("audio")
        if self.stt.available():
            caps.append("voice_input")
        caps.extend(["heartbeat", "chat_sync"])
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
            "initiative": cfg.INITIATIVE_ENABLED,
            "device": cfg.DEVICE_ENABLED,
            "daily_tools": cfg.DAILY_TOOLS_ENABLED,
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
        if frame_type == "heartbeat":
            await self.handle_heartbeat(conn, frame)
        elif frame_type == "text":
            await self._handle_text_frame(conn, frame)
        elif frame_type == "audio":
            await self._handle_audio_frame(conn, frame)
        elif frame_type == "message_ack":
            # Delivery reconciliation for delivery_unknown rows arrives with
            # the history APIs milestone; acknowledgements are accepted and
            # idempotently ignored in 0.1.0 (documented in SPEC).
            log.debug("message_ack ignored (no pending reconciliation)")
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
        if mode == "work":
            # Work mode ships in milestone 0.5.0 (documented in SPEC).
            await conn.send_json(
                error_frame(
                    "work_unavailable",
                    "Work mode is not available in this build.",
                    terminal=True,
                )
            )
            return
        if mode != "companion":
            await conn.send_json(
                error_frame(
                    "unknown_mode",
                    f"Unknown mode: {mode!r}. Supported: companion.",
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
        # heartbeats and other frames while the turn holds the owner lock.
        task = asyncio.create_task(
            self.run_companion_turn(
                text=text if isinstance(text, str) else "",
                language=language,
                source_conn=conn,
                wants_audio=wants_audio,
            )
        )
        self.background_tasks.add(task)
        task.add_done_callback(self.background_tasks.discard)

    # -- audio (voice input) turns (plan sections 10.3, 14.2, 14.3) -----------

    def _allowed_audio_types(self) -> set[str]:
        return {
            part.strip().lower()
            for part in self.config.ALLOWED_AUDIO_CONTENT_TYPES.split(",")
            if part.strip()
        }

    async def _handle_audio_frame(self, conn: Connection, frame: dict) -> None:
        mode = frame.get("mode", "companion")
        if mode == "work":
            await conn.send_json(
                error_frame(
                    "work_unavailable",
                    "Work mode is not available in this build.",
                    terminal=True,
                )
            )
            return
        if mode != "companion":
            await conn.send_json(
                error_frame(
                    "unknown_mode",
                    f"Unknown mode: {mode!r}. Supported: companion.",
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
        # The initiative engine is milestone 0.7.0; the counter is a constant
        # stub until then (documented in SPEC). Replay flagging exists now so
        # clients see stable semantics.
        await conn.send_json(
            {
                "type": "heartbeat_ack",
                "server_time": hist.utc_now_iso(),
                "initiative_counter": INITIATIVE_COUNTER_STUB,
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

    async def _sweep_bids(self, owner_id: str) -> None:
        try:
            await self.bids.sweep_expired(owner_id)
        except Exception:  # noqa: BLE001 - maintenance never fails heartbeats
            log.debug("bid sweep failed", exc_info=True)

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
    ) -> dict:
        """Busy/unavailable ladder (plan section 16.3).

        The message is queued for one later catch-up answer; no LLM call, no
        history write, no bids/needs effects run on this path. The first
        message in a busy window may speak the authored static line;
        repeated messages defer protocol-only. Skipped hooks (do not run):
        bids, needs effects, boundary classification (classified at
        catch-up), history persistence, memory/analysis.
        """
        user_row = hist.make_row("user", text.strip(), hist.DELIVERED)
        origin = source_conn.connection_id if source_conn is not None else "http"
        async with self.connections.catchup_lock(owner):
            window_count = await self.deferred.busy_count(owner)
            await self.deferred.append(
                owner,
                message_id=user_row["id"],
                mode="companion",
                text=text.strip(),
                source_connection_id=origin,
            )
            await self.deferred.increment_busy(owner)
        done: dict[str, Any] = {
            "type": "done",
            "id": hist.new_message_id(),
            "mode": "companion",
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
            "Companion message deferred (availability=%s, window_count=%d)",
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
        if self.config.SCHEDULE_ENABLED or self.config.USER_SCHEDULE_ENABLED:
            awareness_block = await self._build_awareness_block(
                owner, source_conn, prompt_history
            )
        if self.life is not None and self.life.available:
            context_feed_text, pending_life_ids = await self._build_life_feed(owner)
        return {
            "state_block": state_block,
            "owner_block": owner_block,
            "awareness_block": awareness_block,
            "context_feed": context_feed_text,
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

    async def _build_life_feed(self, owner: str) -> tuple[str, list[str]]:
        """Bounded life context feed (plan sections 20.4, 21.2)."""
        events = await self.life.recent(owner, limit=8)
        pending_ids = await self.life.pending_ids(owner)
        feed, included = build_context_feed(
            life_events=events,
            pending_ids=pending_ids,
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
                owner, source_conn=source_conn, prompt_history=prompt_history
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
                context_feed=blocks["context_feed"],
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
                result = await self.llm.chat("companion", messages)
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
        # catch-up lock.
        if delivered and self.schedule is not None and self.schedule.available:
            catchup_task = asyncio.create_task(
                self._maybe_catchup(owner, trigger_conn=source_conn)
            )
            self.background_tasks.add(catchup_task)
            catchup_task.add_done_callback(self.background_tasks.discard)
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
    ) -> dict:
        return {
            "type": "chat_sync",
            "role": row["role"],
            "text": row["text"],
            "emotion": row.get("emotion", "neutral"),
            "mode": "companion",
            "initiated_by": initiated_by,
            "id": row["id"],
            "origin_connection_id": (
                origin
                or (source_conn.connection_id if source_conn is not None else "http")
            ),
            "ts": row["ts"],
        }

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


def _audio_complete(message_id: str, succeeded: int, failed: int) -> dict:
    return {
        "type": "audio_complete",
        "id": message_id,
        "succeeded_chunks": succeeded,
        "failed_chunks": failed,
    }
