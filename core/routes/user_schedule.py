"""Contextual owner-schedule HTTP APIs (plan sections 22, 29).

- ``GET /user-schedule`` is a read-only poll; a missing store returns the
  default projection with ``materialized: false`` and never writes.
- ``PATCH /user-schedule`` requires the ``UPDATE_USER_SCHEDULE``
  mistake-guard token. This endpoint is the only path that changes the
  durable owner-schedule timezone. Unknown/invalid fields return 400.
"""

from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse

from ..app import error_body
from ..constants import UPDATE_USER_SCHEDULE_TOKEN
from ..user_schedule import UserScheduleError

TOKEN_HEADER = "X-Confirm-Token"


def register_user_schedule_routes(app, bridge) -> None:
    @app.get("/user-schedule")
    async def get_user_schedule():
        if not bridge.user_schedule.available:
            return JSONResponse(
                status_code=403,
                content=error_body(
                    "feature_disabled",
                    "Contextual owner schedule is disabled on this deployment.",
                ),
            )
        owner = bridge.config.OWNER_USER_ID
        return await bridge.user_schedule.view(owner)

    @app.patch("/user-schedule")
    async def patch_user_schedule(request: Request):
        if (
            request.headers.get(TOKEN_HEADER, "")
            != UPDATE_USER_SCHEDULE_TOKEN
        ):
            return JSONResponse(
                status_code=400,
                content=error_body(
                    "confirm_token_required",
                    f"PATCH requires the {TOKEN_HEADER}: "
                    f"{UPDATE_USER_SCHEDULE_TOKEN} mistake-guard header.",
                ),
            )
        if not bridge.user_schedule.available:
            return JSONResponse(
                status_code=403,
                content=error_body(
                    "feature_disabled",
                    "Contextual owner schedule is disabled on this deployment.",
                ),
            )
        try:
            patch = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse(
                status_code=400,
                content=error_body("bad_json", "Request body must be JSON."),
            )
        owner = bridge.config.OWNER_USER_ID
        try:
            updates = bridge.user_schedule.validate_patch(patch)
        except UserScheduleError as exc:
            return JSONResponse(
                status_code=400,
                content=error_body("invalid_patch", str(exc)),
            )
        store = await bridge.user_schedule.apply_patch(owner, updates)
        return {"schedule": {
            "timezone": store.get("timezone"),
            "days": store.get("days", {}),
        }}
