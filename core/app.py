"""FastAPI application and lifespan (plan sections 10, 27, 29, 30).

Startup: logging, Tailscale bind validation, Redis ping (required service),
emotions manifest validation, version/feature summary log. Shutdown cancels
and awaits background tasks.

HTTP error responses use the standard shape
``{"error": {"code", "message", "details": {}}}`` (plan section 29).
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, WebSocket
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .bridge import Bridge
from .cache import RedisCache
from .config import Config
from .connections import ConnectionManager
from .constants import SUPPORTED_LANGUAGES, VERSION
from .llm import LLMRouter
from .speech import STTService, TTSService
from .tailscale import validate_bind

log = logging.getLogger("bridge.app")


def error_body(code: str, message: str, details: dict | None = None) -> dict:
    return {"error": {"code": code, "message": message, "details": details or {}}}


def create_app(
    config: Config | None = None,
    *,
    cache: RedisCache | None = None,
    llm: LLMRouter | None = None,
    stt: STTService | None = None,
    tts: TTSService | None = None,
    tailscale_addresses: set[str] | None = None,
) -> FastAPI:
    config = config or Config.from_env()

    logging.basicConfig(
        level=getattr(logging, config.LOG_LEVEL.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    if cache is None:
        cache = RedisCache.connect(config.REDIS_HOST, config.REDIS_PORT, config.REDIS_DB)
    bridge = Bridge(
        config, cache, llm=llm, connections=ConnectionManager(), stt=stt, tts=tts
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Tailscale deployment requirement is validated before serving (27.2).
        bridge.deployment_mode = await validate_bind(config, tailscale_addresses)
        # Redis is a required service.
        await cache.require_ping()
        await bridge.startup()
        enabled = [name for name, on in bridge.feature_summary().items() if on]
        log.info(
            "Bridge Core Engine %s starting: bind=%s:%d deployment=%s "
            "features_enabled=%s routes=%s",
            VERSION,
            config.BRIDGE_HOST,
            config.BRIDGE_PORT,
            bridge.deployment_mode,
            enabled or ["none"],
            [f"{r.provider}/{r.model or '-'}" for r in bridge.llm.routes_for("companion")],
        )
        yield
        await bridge.shutdown()
        await cache.close()

    app = FastAPI(title="Bridge Core Engine", version=VERSION, lifespan=lifespan)
    app.state.bridge = bridge

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError):
        return JSONResponse(
            status_code=422,
            content=error_body("invalid_request", "Request validation failed.",
                               {"errors": exc.errors()}),
        )

    @app.get("/health")
    async def health():
        redis_ok = await cache.ping()
        body = {"status": "ok" if redis_ok else "degraded", "redis": redis_ok,
                "version": VERSION}
        return JSONResponse(status_code=200 if redis_ok else 503, content=body)

    @app.get("/status")
    async def status():
        cfg = bridge.config
        redis_ok = await cache.ping()
        providers = {
            "fireworks": {
                "configured": bool(cfg.FIREWORKS_API_KEY.strip() and cfg.FIREWORKS_MODEL.strip()),
                "url": cfg.FIREWORKS_URL,
                "model": cfg.FIREWORKS_MODEL or None,
            },
            "chutes": {
                "configured": bool(
                    cfg.CHUTES_API_KEY.strip() and cfg.CHUTES_URL.strip() and cfg.CHUTES_MODEL.strip()
                ),
                "url": cfg.CHUTES_URL or None,
                "model": cfg.CHUTES_MODEL or None,
            },
            "ollama": {
                "configured": bool(cfg.OLLAMA_URL.strip() and cfg.OLLAMA_MODEL.strip()),
                "url": cfg.OLLAMA_URL,
                "model": cfg.OLLAMA_MODEL or None,
            },
            "openai_compat": {
                "configured": bool(cfg.OPENAI_COMPAT_URL.strip() and cfg.OPENAI_COMPAT_MODEL.strip()),
                "url": cfg.OPENAI_COMPAT_URL or None,
                "model": cfg.OPENAI_COMPAT_MODEL or None,
            },
        }
        return {
            "version": VERSION,
            "redis": {"ok": redis_ok, "host": cfg.REDIS_HOST, "port": cfg.REDIS_PORT},
            "deployment_mode": bridge.deployment_mode,
            "tailscale_required": cfg.TAILSCALE_REQUIRED,
            "providers": providers,
            "companion_routes": [
                {"provider": r.provider, "model": r.model or None}
                for r in bridge.llm.routes_for("companion")
            ],
            "speech": {
                "tts": {
                    "enabled": cfg.TTS_ENABLED,
                    "provider": "elevenlabs",
                    "configured": bridge.tts.available(),
                    "voice_profile_loaded": bridge.tts.has_voice_profile,
                    "output_format": cfg.TTS_OUTPUT_FORMAT,
                },
                "stt": {
                    "enabled": cfg.STT_ENABLED,
                    "provider": cfg.STT_PROVIDER,
                    "configured": bridge.stt.available(),
                },
            },
            "features": bridge.feature_summary(),
            "identity_files": bridge.identity_info(),
            "emotions_manifest_version": bridge.emotions_manifest.get("version"),
            "needs": {
                "enabled": bridge.needs.available,
                "profile_loaded": bool(bridge.needs.spec),
                "state_expression": cfg.STATE_EXPRESSION_ENABLED,
            },
            "owner_profile": {
                "enabled": bridge.owner_profile.available,
                "inject": cfg.OWNER_PROFILE_INJECT,
                "boundary_penalties": cfg.OWNER_BOUNDARY_PENALTIES_ENABLED,
                "soft_block": cfg.OWNER_SOFT_BLOCK_ENABLED,
                "llm_analysis": cfg.OWNER_PROFILE_LLM_ENABLED,
                "agreements": cfg.OWNER_AGREEMENTS_ENABLED,
            },
            "schedule": {
                "enabled": bridge.schedule is not None and bridge.schedule.available,
                "timezone": cfg.CHARACTER_TIMEZONE,
                "dir": cfg.SCHEDULE_DIR or None,
                "now": (
                    bridge.schedule.peek()["now"]
                    if bridge.schedule is not None and bridge.schedule.available
                    else None
                ),
                "soft_busy_policy": cfg.SCHEDULE_SOFT_BUSY_POLICY,
            },
            "life": {
                "enabled": bridge.life is not None and bridge.life.available,
                "events_dir": cfg.LIFE_EVENTS_DIR or None,
                "templates_enabled": (
                    len(bridge.life.templates) if bridge.life is not None else 0
                ),
                "daily_min": cfg.LIFE_DAILY_MIN,
                "daily_max": cfg.LIFE_DAILY_MAX,
                "longterm_backend": "redis_fallback",
            },
            "user_schedule": {
                "enabled": bridge.user_schedule.available,
            },
            "connections": len(bridge.connections.connections_for(cfg.OWNER_USER_ID)),
        }

    @app.get("/emotions")
    async def emotions():
        return bridge.emotions_manifest

    from .routes.life import register_life_routes
    from .routes.profiles import register_profile_routes
    from .routes.schedule import register_schedule_routes
    from .routes.state import register_state_routes
    from .routes.user_schedule import register_user_schedule_routes

    register_profile_routes(app, bridge)
    register_state_routes(app, bridge)
    register_schedule_routes(app, bridge)
    register_life_routes(app, bridge)
    register_user_schedule_routes(app, bridge)

    @app.post("/message")
    async def message(request: Request):
        """Minimal HTTP webhook for text companion turns (plan section 10.10).

        Full webhook completion (message_ack reconciliation, tool-less work
        turns) is milestone 0.7.0 — documented in BRIDGE_CORE_ENGINE_SPEC.md.
        """
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse(
                status_code=400,
                content=error_body("bad_json", "Request body must be JSON."),
            )
        if not isinstance(body, dict):
            return JSONResponse(
                status_code=400,
                content=error_body("bad_request", "Request body must be a JSON object."),
            )
        user_id = body.get("user_id", bridge.config.OWNER_USER_ID)
        if user_id != bridge.config.OWNER_USER_ID:
            return JSONResponse(
                status_code=403,
                content=error_body(
                    "forbidden_user", "This deployment serves exactly one configured owner."
                ),
            )
        mode = body.get("mode", "companion")
        if mode == "work":
            return JSONResponse(
                status_code=400,
                content=error_body(
                    "work_unavailable", "Work mode is not available in this build."
                ),
            )
        if mode != "companion":
            return JSONResponse(
                status_code=400,
                content=error_body("unknown_mode", f"Unknown mode: {mode!r}."),
            )
        text = body.get("text")
        if not isinstance(text, str) or not text.strip():
            return JSONResponse(
                status_code=400,
                content=error_body("empty_input", "Message text must not be empty."),
            )
        language = body.get("language")
        if language is not None and language != "":
            if not isinstance(language, str) or language.strip().lower() not in SUPPORTED_LANGUAGES:
                return JSONResponse(
                    status_code=400,
                    content=error_body(
                        "unsupported_language",
                        f"Unsupported reply language: {language!r}. "
                        f"Supported: {', '.join(SUPPORTED_LANGUAGES)}.",
                    ),
                )
            language = language.strip().lower()
        else:
            preferred = await bridge._owner_preferred_language()
            language = preferred or bridge.config.DEFAULT_LANGUAGE
        done = await bridge.run_companion_turn(
            text=text, language=language, source_conn=None
        )
        if done.get("type") == "error":
            return JSONResponse(status_code=502, content=error_body(
                done["error"]["code"], done["error"]["message"], done["error"]["details"]
            ))
        return done

    @app.websocket("/ws/{user_id}")
    async def ws(websocket: WebSocket, user_id: str):
        await bridge.handle_websocket(
            websocket,
            user_id,
            client_type=websocket.query_params.get("client_type", "unknown"),
            device_id=websocket.query_params.get("device_id", ""),
            timezone_name=websocket.query_params.get("tz", ""),
        )

    return app


__all__ = ["create_app", "error_body"]
