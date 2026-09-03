"""Character life HTTP APIs (plan sections 17.4, 29).

- ``GET /life/today`` and ``GET /life/recent`` are read-only views over the
  durable life records; they never materialize or generate anything.
- ``POST /life/generate`` requires the ``GENERATE_LIFE`` mistake-guard token.
  ``force`` bypasses the cooldown and is the explicit retry path for a block
  whose generation previously failed; the daily maximum always applies.
"""

from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse

from ..app import error_body
from ..constants import GENERATE_LIFE_TOKEN


def register_life_routes(app, bridge) -> None:
    def _life_disabled():
        return JSONResponse(
            status_code=403,
            content=error_body(
                "feature_disabled",
                "Character life is disabled on this deployment.",
            ),
        )

    @app.get("/life/today")
    async def life_today():
        if bridge.life is None or not bridge.life.available:
            return _life_disabled()
        owner = bridge.config.OWNER_USER_ID
        events = await bridge.life.today(owner)
        return {"items": events, "total": len(events), "limit": len(events), "offset": 0}

    @app.get("/life/recent")
    async def life_recent(request: Request):
        if bridge.life is None or not bridge.life.available:
            return _life_disabled()
        limit = int(request.query_params.get("limit", "10"))
        limit = max(1, min(limit, 200))
        owner = bridge.config.OWNER_USER_ID
        events = await bridge.life.recent(owner, limit=limit)
        return {
            "items": events,
            "total": await bridge.longterm.count(owner),
            "limit": limit,
            "offset": 0,
        }

    @app.post("/life/generate")
    async def life_generate(request: Request):
        body = await request.json()
        if not isinstance(body, dict) or body.get("confirm") != GENERATE_LIFE_TOKEN:
            return JSONResponse(
                status_code=400,
                content=error_body(
                    "confirm_token_required",
                    "Generation requires the body token "
                    f'{{"confirm": "{GENERATE_LIFE_TOKEN}"}}.',
                ),
            )
        if bridge.life is None or not bridge.life.available:
            return _life_disabled()
        force = bool(body.get("force"))
        owner = bridge.config.OWNER_USER_ID
        bridge.schedule.maybe_reload()
        block = bridge.schedule.current_block()
        result = await bridge.life.generate_for_block(owner, block, force=force)
        return result
