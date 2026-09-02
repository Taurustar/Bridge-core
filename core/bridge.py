"""Bridge: wiring and companion turn lifecycle (plan sections 10-12).

Holds the config, cache, connection manager, and LLM router, and implements
the milestone 0.1.0 text-only companion turn plus heartbeat handling.

Simplified turn lifecycle for 0.1.0 (needs/bids/schedule/profile/memory/TTS
hooks do not exist yet and are flag-off inert):

validate owner -> non-empty text -> per-owner turn lock -> persist user row
(delivered) BEFORE provider call -> fan out user chat_sync -> load bounded
delivered history -> build prompt -> LLM router -> validate/parse reply
(emotion-only retried once) -> append assistant row (pending) -> send done to
source -> delivered: mark delivered + fan out assistant chat_sync; failed:
mark undelivered, excluded from future prompts, no fanout.

Every turn terminates with a ``done`` frame or a terminal error frame
(plan section 30.2).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

from . import history as hist
from .cache import RedisCache
from .config import Config
from .connections import Connection, ConnectionManager
from .constants import (
    HEARTBEAT_MAX_AGE_SECONDS,
    HEARTBEAT_MAX_FUTURE_SECONDS,
    INITIATIVE_COUNTER_STUB,
    VERSION,
)
from .emotions import load_emotions_manifest
from .llm import LLMChainExhausted, LLMResult, LLMRouter
from .prompts import build_companion_prompt
from .text_utils import parse_emotion_reply

log = logging.getLogger("bridge.turn")

REPO_ROOT = Path(__file__).resolve().parent.parent

# Capabilities that actually exist in milestone 0.1.0 (see SPEC).
CAPABILITIES = ["text", "heartbeat", "chat_sync"]

_MAX_SEQUENCE = 2**64 - 1


def error_frame(code: str, message: str, details: dict | None = None, terminal: bool = False) -> dict:
    frame: dict[str, Any] = {
        "type": "error",
        "error": {"code": code, "message": message, "details": details or {}},
    }
    if terminal:
        frame["terminal"] = True
    return frame


class Bridge:
    def __init__(
        self,
        config: Config,
        cache: RedisCache,
        llm: LLMRouter | None = None,
        connections: ConnectionManager | None = None,
    ) -> None:
        self.config = config
        self.cache = cache
        self.llm = llm or LLMRouter(config)
        self.connections = connections or ConnectionManager()
        self.emotions_manifest: dict = {}
        self.deployment_mode = "unknown"
        self.background_tasks: set[asyncio.Task] = set()
        self._identity_cache: dict[str, tuple[float, str]] = {}
        self._started_monotonic = time.monotonic()

    # -- startup -------------------------------------------------------------

    async def startup(self) -> None:
        self.emotions_manifest = load_emotions_manifest(
            self.config.EMOTIONS_FILE or None
        )

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
                "capabilities": list(CAPABILITIES),
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
        elif frame_type == "message_ack":
            # Delivery reconciliation for delivery_unknown rows arrives with
            # the history APIs milestone; acknowledgements are accepted and
            # idempotently ignored in 0.1.0 (documented in SPEC).
            log.debug("message_ack ignored (no pending reconciliation)")
        else:
            await conn.send_json(
                error_frame("unknown_frame_type", f"Unknown frame type: {frame_type!r}")
            )

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
        text = frame.get("text")
        language = frame.get("language") or self.config.DEFAULT_LANGUAGE
        # Run the turn in a background task so the reader loop keeps serving
        # heartbeats and other frames while the turn holds the owner lock.
        task = asyncio.create_task(
            self.run_companion_turn(
                text=text if isinstance(text, str) else "",
                language=language,
                source_conn=conn,
            )
        )
        self.background_tasks.add(task)
        task.add_done_callback(self.background_tasks.discard)

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

    # -- companion turn -----------------------------------------------------------

    async def run_companion_turn(
        self,
        *,
        text: str,
        language: str,
        source_conn: Connection | None,
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

        lock = self.connections.turn_lock(owner)
        async with lock:
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
            messages = build_companion_prompt(
                soul_text=self._read_identity("soul"),
                profile_text=self._read_identity("profile"),
                history=prompt_history,
                current_text=text.strip(),
                language=language,
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

            reply_text, emotion = parse_emotion_reply(result.text)
            if not reply_text:
                # Emotion-only/empty reply is invalid; retry once (7.3, 12.21).
                log.warning("Empty/emotion-only reply; retrying once")
                try:
                    retry = await self.llm.chat("companion", messages)
                    result = self._merge_usage(result, retry)
                    reply_text, emotion = parse_emotion_reply(result.text)
                except LLMChainExhausted:
                    pass
            if not reply_text:
                log.error("Companion turn failed: empty reply after retry")
                frame = error_frame(
                    "empty_reply",
                    "The provider returned an empty reply.",
                    terminal=True,
                )
                if source_conn is not None:
                    await source_conn.send_json(frame)
                return frame

            assistant_row = hist.make_row(
                "assistant", reply_text, hist.PENDING, emotion=emotion
            )
            await hist.append_row(
                self.cache, owner, assistant_row, self.config.MAX_HISTORY_TURNS
            )

            done: dict[str, Any] = {
                "type": "done",
                "id": assistant_row["id"],
                "text": reply_text,
                "emotion": emotion,
                "segments": [{"text": reply_text, "emotion": emotion}],
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
            return done

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
        self, row: dict, initiated_by: str, source_conn: Connection | None
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
                source_conn.connection_id if source_conn is not None else "http"
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
        for task in list(self.background_tasks):
            task.cancel()
        if self.background_tasks:
            await asyncio.gather(*self.background_tasks, return_exceptions=True)
        self.background_tasks.clear()
        await self.llm.aclose()
