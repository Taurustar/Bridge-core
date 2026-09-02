"""Owner lived-profile HTTP APIs (plan sections 18.7, 29).

- ``GET /profiles/owner`` never materializes a missing store (read-only poll,
  plan section 6.4): it returns a default projection instead.
- ``PATCH /profiles/owner`` requires the ``UPDATE_OWNER_PROFILE`` mistake-guard
  token; the token is not a secret and never authenticates anything.
"""

from __future__ import annotations

import time

from fastapi import Request
from fastapi.responses import JSONResponse

from ..app import error_body
from ..constants import UPDATE_OWNER_PROFILE_TOKEN
from ..owner_profile import default_profile, validate_profile_patch

TOKEN_HEADER = "X-Confirm-Token"


def register_profile_routes(app, bridge) -> None:
    @app.get("/profiles/owner")
    async def get_owner_profile():
        if not bridge.owner_profile.available:
            return JSONResponse(
                status_code=403,
                content=error_body(
                    "feature_disabled", "Owner profile is disabled on this deployment."
                ),
            )
        owner = bridge.config.OWNER_USER_ID
        profile = await bridge.owner_profile.get(owner)
        materialized = profile is not None
        if profile is None:
            profile = default_profile(bridge.config)
        return {
            "profile": profile,
            "materialized": materialized,
            "soft_block_status": await bridge.owner_profile.soft_block_status(owner),
        }

    @app.patch("/profiles/owner")
    async def patch_owner_profile(request: Request):
        if request.headers.get(TOKEN_HEADER, "") != UPDATE_OWNER_PROFILE_TOKEN:
            return JSONResponse(
                status_code=400,
                content=error_body(
                    "confirm_token_required",
                    f"PATCH requires the {TOKEN_HEADER}: {UPDATE_OWNER_PROFILE_TOKEN} "
                    f"mistake-guard header.",
                ),
            )
        if not bridge.owner_profile.available:
            return JSONResponse(
                status_code=403,
                content=error_body(
                    "feature_disabled", "Owner profile is disabled on this deployment."
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
        async with bridge.connections.profile_lock(owner):
            current = await bridge.owner_profile.get(owner)
            current = current if current is not None else default_profile(bridge.config)
            try:
                updates = validate_profile_patch(current, patch)
            except ValueError as exc:
                return JSONResponse(
                    status_code=400,
                    content=error_body("invalid_patch", str(exc)),
                )
            soft_flag = updates.pop("soft_blocked", None)
            soft_reason = updates.pop("soft_block_reason", None)
            current.update(updates)
            if soft_flag is True:
                current["soft_blocked"] = True
                current["soft_block_reason"] = soft_reason or "admin_patch"
                # An explicit admin block opens a fresh cooldown window;
                # lift then requires the duration to pass (plan 18.4).
                current["soft_blocked_until_ts"] = (
                    time.time() + bridge.config.OWNER_SOFT_BLOCK_COOLDOWN_SECONDS
                )
            elif soft_flag is False:
                current["soft_blocked"] = False
                current["soft_block_reason"] = ""
            saved = await bridge.owner_profile.upsert(
                owner, current, int(current.get("version", 1))
            )
        if saved is None:
            return JSONResponse(
                status_code=409,
                content=error_body(
                    "version_conflict",
                    "Profile changed concurrently; retry with a fresh GET.",
                ),
            )
        return {"profile": saved}
