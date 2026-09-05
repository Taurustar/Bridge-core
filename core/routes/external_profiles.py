"""Dormant external-user profile admin APIs (plan sections 19, 29).

Store-only CRUD for future gateway adapters. No gateway ships in v1 and the
app companion path never reads these records
(``EXTERNAL_USER_PROFILES_BEHAVIOR_ENABLED`` stays false by default).

- ``EXTERNAL_USER_PROFILE_STORE_ENABLED=false``: every route answers
  ``409 feature_disabled`` and no key is ever created (milestone
  acceptance).
- ``PATCH`` requires the ``UPDATE_EXTERNAL_PROFILE`` ``X-Confirm-Token``
  mistake guard (plan 19.5 requires a confirm token but names no constant;
  recorded in BRIDGE_CORE_ENGINE_SPEC.md).
- ``DELETE`` requires the ``DELETE_EXTERNAL_PROFILE`` body token (plan 29).
- Listing sorts by ``updated_ts desc, subject_id asc`` with the standard
  ``limit``/``offset`` envelope (plan 29).
"""

from __future__ import annotations

import time

from fastapi import Request
from fastapi.responses import JSONResponse

from ..app import error_body
from ..constants import (
    DELETE_EXTERNAL_PROFILE_TOKEN,
    UPDATE_EXTERNAL_PROFILE_TOKEN,
)
from ..external_profiles import (
    ExternalProfileError,
    default_profile,
    validate_external_id,
    validate_patch,
    validate_platform,
)

TOKEN_HEADER = "X-Confirm-Token"
DEFAULT_LIMIT = 50
MAX_LIMIT = 200


def _list_envelope(items: list[dict], total: int, limit: int, offset: int) -> dict:
    return {"items": items, "total": total, "limit": limit, "offset": offset}


async def _parse_json(request: Request) -> object:
    try:
        return await request.json()
    except Exception:  # noqa: BLE001
        return None


def _identity_error(exc: ExternalProfileError) -> JSONResponse:
    return JSONResponse(
        status_code=400,
        content=error_body("invalid_external_profile", str(exc)),
    )


def register_external_profile_routes(app, bridge) -> None:
    store = bridge.external_profiles

    def _disabled() -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content=error_body(
                "feature_disabled",
                "The external-user profile store is disabled on this deployment.",
            ),
        )

    @app.get("/profiles/external")
    async def list_external_profiles(limit: int = DEFAULT_LIMIT, offset: int = 0):
        if not store.available:
            return _disabled()
        limit = max(0, min(limit, MAX_LIMIT))
        offset = max(0, offset)
        owner = bridge.config.OWNER_USER_ID
        rows = await store.list_profiles(owner)
        rows.sort(
            key=lambda row: (
                -float(row.get("updated_ts", 0) or 0),
                str(row.get("subject_id", "")),
            )
        )
        total = len(rows)
        return _list_envelope(rows[offset : offset + limit], total, limit, offset)

    @app.get("/profiles/external/{platform}/{external_id}")
    async def get_external_profile(platform: str, external_id: str):
        if not store.available:
            return _disabled()
        owner = bridge.config.OWNER_USER_ID
        try:
            platform = validate_platform(platform)
            external_id = validate_external_id(external_id)
        except ExternalProfileError as exc:
            return _identity_error(exc)
        profile = await store.get(owner, platform, external_id)
        if profile is None:
            return JSONResponse(
                status_code=404,
                content=error_body("not_found", "No such external profile."),
            )
        return profile

    @app.patch("/profiles/external/{platform}/{external_id}")
    async def patch_external_profile(platform: str, external_id: str, request: Request):
        if request.headers.get(TOKEN_HEADER, "") != UPDATE_EXTERNAL_PROFILE_TOKEN:
            return JSONResponse(
                status_code=400,
                content=error_body(
                    "confirm_token_required",
                    f"PATCH requires the {TOKEN_HEADER}: "
                    f"{UPDATE_EXTERNAL_PROFILE_TOKEN} mistake-guard header.",
                ),
            )
        if not store.available:
            return _disabled()
        owner = bridge.config.OWNER_USER_ID
        try:
            platform = validate_platform(platform)
            external_id = validate_external_id(external_id)
        except ExternalProfileError as exc:
            return _identity_error(exc)
        body = await _parse_json(request)
        try:
            updates = validate_patch(body)
        except ExternalProfileError as exc:
            return JSONResponse(
                status_code=400,
                content=error_body("invalid_patch", str(exc)),
            )
        current = await store.get(owner, platform, external_id)
        if current is None:
            return JSONResponse(
                status_code=404,
                content=error_body("not_found", "No such external profile."),
            )
        supplied_version = updates.pop("version", None)
        if supplied_version is not None and int(supplied_version) != int(
            current.get("version", 1)
        ):
            return JSONResponse(
                status_code=409,
                content=error_body(
                    "version_conflict",
                    "Profile changed concurrently; retry with a fresh GET.",
                ),
            )
        current.update(updates)
        current["updated_ts"] = time.time()
        current["version"] = int(current.get("version", 1)) + 1
        saved = await store.put(owner, platform, external_id, current)
        return {"profile": saved}

    @app.post("/profiles/external/{platform}/{external_id}")
    async def create_external_profile(platform: str, external_id: str, request: Request):
        """Explicit create so adapters can pre-register a subject_id.

        Creating an empty record touches no behavior path; it exists so the
        store can hold a profile before any gateway conversation. Same
        mistake guard as PATCH (plan 19.4: store and admin APIs only).
        """
        if request.headers.get(TOKEN_HEADER, "") != UPDATE_EXTERNAL_PROFILE_TOKEN:
            return JSONResponse(
                status_code=400,
                content=error_body(
                    "confirm_token_required",
                    f"POST requires the {TOKEN_HEADER}: "
                    f"{UPDATE_EXTERNAL_PROFILE_TOKEN} mistake-guard header.",
                ),
            )
        if not store.available:
            return _disabled()
        owner = bridge.config.OWNER_USER_ID
        try:
            platform = validate_platform(platform)
            external_id = validate_external_id(external_id)
        except ExternalProfileError as exc:
            return _identity_error(exc)
        existing = await store.get(owner, platform, external_id)
        if existing is not None:
            return JSONResponse(
                status_code=409,
                content=error_body(
                    "already_exists", "An external profile already exists."
                ),
            )
        saved = await store.put(
            owner, platform, external_id, default_profile(platform, external_id)
        )
        return {"profile": saved}

    @app.delete("/profiles/external/{platform}/{external_id}")
    async def delete_external_profile(platform: str, external_id: str, request: Request):
        body = await _parse_json(request)
        if not isinstance(body, dict) or body.get("confirm") != DELETE_EXTERNAL_PROFILE_TOKEN:
            return JSONResponse(
                status_code=400,
                content=error_body(
                    "confirm_token_required",
                    f"DELETE requires the body token {DELETE_EXTERNAL_PROFILE_TOKEN!r}.",
                ),
            )
        if not store.available:
            return _disabled()
        owner = bridge.config.OWNER_USER_ID
        try:
            platform = validate_platform(platform)
            external_id = validate_external_id(external_id)
        except ExternalProfileError as exc:
            return _identity_error(exc)
        existed = await store.delete(owner, platform, external_id)
        if not existed:
            return JSONResponse(
                status_code=404,
                content=error_body("not_found", "No such external profile."),
            )
        return {"deleted": True, "subject_id": f"{platform}:{external_id}"}


__all__ = ["register_external_profile_routes"]
