"""Schedule HTTP APIs and admin reload (plan sections 16.2, 16.4, 29).

- ``GET /schedule`` is a read-only poll (plan 6.4): schedule ``peek`` never
  writes and never advances simulation state.
- ``GET /awareness`` returns the deterministic awareness fields and rendered
  block; it never writes.
- ``POST /admin/reload-schedule`` requires the ``RELOAD_SCHEDULE``
  mistake-guard token. An invalid day keeps the last valid schedule.
"""

from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse

from ..app import error_body
from ..constants import RELOAD_SCHEDULE_TOKEN
from ..schedule import ScheduleError


def register_schedule_routes(app, bridge) -> None:
    @app.get("/schedule")
    async def get_schedule():
        if bridge.schedule is None or not bridge.schedule.available:
            return JSONResponse(
                status_code=403,
                content=error_body(
                    "feature_disabled",
                    "Schedule is disabled on this deployment.",
                ),
            )
        bridge.schedule.maybe_reload()
        return bridge.schedule.peek()

    @app.get("/awareness")
    async def get_awareness():
        enabled = (
            bridge.config.SCHEDULE_ENABLED
            or bridge.config.USER_SCHEDULE_ENABLED
        )
        if not enabled:
            return JSONResponse(
                status_code=403,
                content=error_body(
                    "feature_disabled",
                    "Awareness requires the schedule or owner-schedule feature.",
                ),
            )
        owner = bridge.config.OWNER_USER_ID
        awareness = await bridge._build_awareness_block(owner, None, [])
        owner_state = None
        if bridge.user_schedule.available:
            owner_state = await bridge.user_schedule.current_block(owner)
        return {
            "awareness_block": awareness,
            "owner_schedule_now": owner_state,
        }

    @app.post("/admin/reload-schedule")
    async def reload_schedule(request: Request):
        body = await request.json()
        if not isinstance(body, dict) or body.get("confirm") != RELOAD_SCHEDULE_TOKEN:
            return JSONResponse(
                status_code=400,
                content=error_body(
                    "confirm_token_required",
                    "Reload requires the body token "
                    f'{{"confirm": "{RELOAD_SCHEDULE_TOKEN}"}}.',
                ),
            )
        if bridge.schedule is None or not bridge.schedule.available:
            return JSONResponse(
                status_code=403,
                content=error_body(
                    "feature_disabled",
                    "Schedule is disabled on this deployment.",
                ),
            )
        try:
            result = bridge.schedule.reload()
        except ScheduleError as exc:
            return JSONResponse(
                status_code=400,
                content=error_body(
                    "invalid_schedule",
                    "Reload rejected; the last valid schedule is still active.",
                    {"reason": str(exc)[:500]},
                ),
            )
        return result
