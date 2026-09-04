"""Admin wipe (plan sections 28, 29).

``POST /admin/wipe/{owner}`` with the ``WIPE_USER`` body token clears every
documented key family for the owner (plan section 28 inventory, mirrored in
``constants.wipe_key_patterns``) plus the owner's Chroma rows. The path
owner must equal the configured ``OWNER_USER_ID``. This is a mistake guard,
not authentication — the tailnet is the security boundary.
"""

from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse

from ..app import error_body
from ..constants import WIPE_USER_TOKEN, longterm_key, wipe_key_patterns
from ..memory import ChromaWipeError


async def _parse_json(request: Request) -> object:
    try:
        return await request.json()
    except Exception:  # noqa: BLE001
        return None


def register_admin_routes(app, bridge) -> None:
    @app.post("/admin/wipe/{owner}")
    async def wipe_owner(owner: str, request: Request):
        if owner != bridge.config.OWNER_USER_ID:
            return JSONResponse(
                status_code=403,
                content=error_body(
                    "forbidden_user",
                    "This deployment serves exactly one configured owner.",
                ),
            )
        body = await _parse_json(request)
        if not isinstance(body, dict) or body.get("confirm") != WIPE_USER_TOKEN:
            return JSONResponse(
                status_code=400,
                content=error_body(
                    "confirm_token_required",
                    f"Wipe requires the body token {WIPE_USER_TOKEN!r}.",
                ),
            )
        lock = bridge.connections.turn_lock(owner)
        async with lock:
            patterns = wipe_key_patterns(owner)
            deleted_keys = 0
            seen: set[str] = set()
            memory_key = longterm_key(owner)
            if await bridge.cache.keys(memory_key):
                seen.add(memory_key)
                deleted_keys += 1
            try:
                memories_deleted = await bridge.longterm.wipe(owner)
            except ChromaWipeError:
                return JSONResponse(
                    status_code=503,
                    content=error_body(
                        "wipe_failed",
                        "Chroma owner deletion failed; Redis data was preserved.",
                    ),
                )
            # Sweep twice. The first pass catches a longterm key recreated
            # after the Chroma-first wipe; the second catches writers that
            # raced the first key scan while the owner turn lock was held.
            for _ in range(2):
                for pattern in patterns:
                    for key in await bridge.cache.keys(pattern):
                        await bridge.cache.delete(key)
                        if key not in seen:
                            seen.add(key)
                            deleted_keys += 1
        return {
            "wiped": True,
            "owner": owner,
            "keys_deleted": deleted_keys,
            "memory_rows_deleted": memories_deleted,
        }


__all__ = ["register_admin_routes"]
