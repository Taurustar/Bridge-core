"""History and mid-term HTTP APIs (plan sections 20.2, 20.6, 29).

- ``GET /history``: bounded companion-history reads with ``order``,
  ``after_id`` stable pagination, and list conventions (limit/offset).
- ``GET /history/midterm``: the mid-term chapter ring, newest first.
- ``POST /history/close``: session close (plan section 20.6) — distill,
  extract, clear short-term only after successful storage, then fan a
  ``session_reset`` frame to every connected owner device.
"""

from __future__ import annotations

import time

from fastapi import Request
from fastapi.responses import JSONResponse

from ..app import error_body
from ..constants import SESSION_RESET_FRAME
from ..history import load_rows

DEFAULT_LIMIT = 50
MAX_LIMIT = 200


def _list_envelope(items: list[dict], total: int, limit: int, offset: int) -> dict:
    return {"items": items, "total": total, "limit": limit, "offset": offset}


def register_history_routes(app, bridge) -> None:
    @app.get("/history")
    async def get_history(
        limit: int = DEFAULT_LIMIT,
        offset: int = 0,
        order: str = "asc",
        after_id: str = "",
    ):
        limit = max(0, min(limit, MAX_LIMIT))
        offset = max(0, offset)
        owner = bridge.config.OWNER_USER_ID
        rows = await load_rows(bridge.cache, owner)
        if after_id:
            for index, row in enumerate(rows):
                if row.get("id") == after_id:
                    rows = rows[index + 1 :]
                    break
            else:
                rows = []
        ascending = order != "desc"
        if not ascending:
            rows = list(reversed(rows))
        total = len(rows)
        return _list_envelope(rows[offset : offset + limit], total, limit, offset)

    @app.get("/history/midterm")
    async def get_history_midterm(limit: int = DEFAULT_LIMIT, offset: int = 0):
        limit = max(0, min(limit, MAX_LIMIT))
        offset = max(0, offset)
        owner = bridge.config.OWNER_USER_ID
        chapters = await bridge.midterm.all_chapters(owner)
        total = len(chapters)
        return _list_envelope(chapters[offset : offset + limit], total, limit, offset)

    @app.post("/history/close")
    async def post_history_close():
        owner = bridge.config.OWNER_USER_ID
        lock = bridge.connections.turn_lock(owner)
        async with lock:
            rows = await load_rows(bridge.cache, owner)
            result = await bridge.midterm.close_session(
                owner, rows, now_ts=time.time()
            )
        if not result.get("closed"):
            reason = result.get("reason", "close_failed")
            return JSONResponse(
                status_code=502,
                content=error_body(
                    "close_failed",
                    "Session close failed; the conversation is preserved.",
                    {"reason": reason},
                ),
            )
        await bridge._fan_out(owner, dict(SESSION_RESET_FRAME))
        return {
            "closed": True,
            "chapter_id": result.get("chapter_id"),
            "extracted": result.get("extracted", 0),
        }


__all__ = ["register_history_routes"]
