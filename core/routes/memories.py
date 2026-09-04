"""Memory admin APIs (plan sections 20.5, 29).

- ``GET /memories``: filtered, deterministic listing with bounded search.
- ``GET/POST/PATCH/DELETE /memories/{id}``: admin CRUD. PATCH validates
  unknown fields as errors; DELETE requires the ``DELETE_MEMORY``
  mistake-guard token and refuses pinned rows (plan section 20.5).
- ``POST /memories/cleanup``: policy cleanup with dry-run diagnostics;
  ``409 feature_disabled`` unless ``MEMORY_CLEANUP_ENABLED``.
"""

from __future__ import annotations

import json

from fastapi import Request
from fastapi.responses import JSONResponse

from ..app import error_body
from ..constants import DELETE_MEMORY_TOKEN
from ..memory import ChromaDeleteError, MEMORY_KINDS, PinnedMemoryError

DEFAULT_LIMIT = 50
MAX_LIMIT = 200
MAX_MANUAL_TEXT_CHARS = 1000
MAX_METADATA_KEYS = 32
MAX_METADATA_KEY_CHARS = 64
MAX_METADATA_JSON_CHARS = 4096

PATCH_FIELDS = {"text", "importance", "pinned", "metadata"}


def _list_envelope(items: list[dict], total: int, limit: int, offset: int) -> dict:
    return {"items": items, "total": total, "limit": limit, "offset": offset}


async def _parse_json(request: Request) -> object:
    try:
        return await request.json()
    except Exception:  # noqa: BLE001
        return None


def _valid_metadata(value: object) -> bool:
    if not isinstance(value, dict) or len(value) > MAX_METADATA_KEYS:
        return False
    if any(
        not isinstance(key, str) or not key or len(key) > MAX_METADATA_KEY_CHARS
        for key in value
    ):
        return False
    try:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return False
    return len(encoded) <= MAX_METADATA_JSON_CHARS


def register_memory_routes(app, bridge) -> None:
    backend = bridge.longterm

    @app.get("/memories")
    async def list_memories(
        limit: int = DEFAULT_LIMIT,
        offset: int = 0,
        kind: str = "",
        source_mode: str = "",
        pinned: str = "",
        q: str = "",
    ):
        limit = max(0, min(limit, MAX_LIMIT))
        offset = max(0, offset)
        owner = bridge.config.OWNER_USER_ID
        if kind and kind not in MEMORY_KINDS:
            return JSONResponse(
                status_code=400,
                content=error_body("invalid_memory", f"Unknown memory kind: {kind!r}."),
            )
        pinned_filter: bool | None = None
        if pinned.strip():
            if pinned.lower() not in ("true", "false"):
                return JSONResponse(
                    status_code=400,
                    content=error_body("invalid_memory", "pinned must be true or false."),
                )
            pinned_filter = pinned.lower() == "true"
        query = q.strip()[:400]
        if query:
            ranked = await backend.search(
                owner,
                query,
                kinds=(kind,) if kind else None,
                limit=MAX_LIMIT + offset,
            )
            rows = [
                row
                for row in ranked
                if (source_mode == "" or row.get("source_mode") == source_mode)
                and (pinned_filter is None or bool(row.get("pinned")) is pinned_filter)
            ]
            total = len(rows)
            return _list_envelope(rows[offset : offset + limit], total, limit, offset)
        rows = await backend.records(
            owner,
            kind=kind or None,
            source_mode=source_mode or None,
            pinned=pinned_filter,
        )
        rows.sort(
            key=lambda row: (
                -float(row.get("updated_ts", 0) or 0),
                str(row.get("id", "")),
            )
        )
        total = len(rows)
        return _list_envelope(rows[offset : offset + limit], total, limit, offset)

    @app.get("/memories/{record_id}")
    async def get_memory(record_id: str):
        owner = bridge.config.OWNER_USER_ID
        row = await backend.get(owner, record_id)
        if row is None:
            return JSONResponse(
                status_code=404,
                content=error_body("not_found", "No such memory row."),
            )
        return row

    @app.post("/memories")
    async def create_memory(request: Request):
        body = await _parse_json(request)
        if not isinstance(body, dict):
            return JSONResponse(
                status_code=400,
                content=error_body("bad_json", "Request body must be a JSON object."),
            )
        kind = body.get("kind")
        text = body.get("text")
        if kind not in MEMORY_KINDS:
            return JSONResponse(
                status_code=400,
                content=error_body("invalid_memory", f"Unknown memory kind: {kind!r}."),
            )
        if not isinstance(text, str) or not text.strip():
            return JSONResponse(
                status_code=400,
                content=error_body("invalid_memory", "text must be a non-empty string."),
            )
        if len(text) > MAX_MANUAL_TEXT_CHARS:
            return JSONResponse(
                status_code=400,
                content=error_body(
                    "invalid_memory",
                    f"text is limited to {MAX_MANUAL_TEXT_CHARS} characters.",
                ),
            )
        importance = body.get("importance", 0.0)
        if not isinstance(importance, (int, float)) or isinstance(importance, bool):
            return JSONResponse(
                status_code=400,
                content=error_body("invalid_memory", "importance must be a number."),
            )
        pinned = body.get("pinned", False)
        if not isinstance(pinned, bool):
            return JSONResponse(
                status_code=400,
                content=error_body("invalid_memory", "pinned must be a boolean."),
            )
        metadata = body.get("metadata", {})
        if not _valid_metadata(metadata):
            return JSONResponse(
                status_code=400,
                content=error_body(
                    "invalid_memory",
                    "metadata must be a JSON object with at most 32 keys and 4096 characters.",
                ),
            )
        owner = bridge.config.OWNER_USER_ID
        record = backend.make_record(
            kind=kind,
            text=text.strip(),
            source=str(body.get("source", "admin")),
            source_mode=str(body.get("source_mode", "admin")),
            importance=float(importance),
            pinned=pinned,
            metadata=metadata,
        )
        saved = await backend.add(owner, record)
        return JSONResponse(status_code=201, content=saved)

    @app.patch("/memories/{record_id}")
    async def patch_memory(record_id: str, request: Request):
        body = await _parse_json(request)
        if not isinstance(body, dict):
            return JSONResponse(
                status_code=400,
                content=error_body("bad_json", "Request body must be a JSON object."),
            )
        unknown = set(body.keys()) - PATCH_FIELDS
        if unknown:
            return JSONResponse(
                status_code=400,
                content=error_body(
                    "invalid_memory",
                    f"Unknown PATCH fields: {', '.join(sorted(unknown))}.",
                ),
            )
        text = body.get("text")
        if text is not None and (not isinstance(text, str) or not text.strip()):
            return JSONResponse(
                status_code=400,
                content=error_body("invalid_memory", "text must be a non-empty string."),
            )
        if text is not None and len(text) > MAX_MANUAL_TEXT_CHARS:
            return JSONResponse(
                status_code=400,
                content=error_body(
                    "invalid_memory",
                    f"text is limited to {MAX_MANUAL_TEXT_CHARS} characters.",
                ),
            )
        importance = body.get("importance")
        if importance is not None and (
            not isinstance(importance, (int, float)) or isinstance(importance, bool)
        ):
            return JSONResponse(
                status_code=400,
                content=error_body("invalid_memory", "importance must be a number."),
            )
        pinned = body.get("pinned")
        if pinned is not None and not isinstance(pinned, bool):
            return JSONResponse(
                status_code=400,
                content=error_body("invalid_memory", "pinned must be a boolean."),
            )
        metadata = body.get("metadata")
        if metadata is not None and not _valid_metadata(metadata):
            return JSONResponse(
                status_code=400,
                content=error_body(
                    "invalid_memory",
                    "metadata must be a JSON object with at most 32 keys and 4096 characters.",
                ),
            )
        owner = bridge.config.OWNER_USER_ID
        if metadata is not None:
            existing = await backend.get(owner, record_id)
            if existing is not None:
                merged_metadata = dict(existing.get("metadata") or {})
                merged_metadata.update(metadata)
                if not _valid_metadata(merged_metadata):
                    return JSONResponse(
                        status_code=400,
                        content=error_body(
                            "invalid_memory",
                            "merged metadata exceeds the 32-key or 4096-character limit.",
                        ),
                    )
        updated = await backend.patch(
            owner,
            record_id,
            text=text.strip() if text is not None else None,
            importance=float(importance) if importance is not None else None,
            pinned=pinned,
            metadata=metadata,
        )
        if updated is None:
            return JSONResponse(
                status_code=404,
                content=error_body("not_found", "No such memory row."),
            )
        return updated

    @app.delete("/memories/{record_id}")
    async def delete_memory(record_id: str, request: Request):
        body = await _parse_json(request)
        if not isinstance(body, dict) or body.get("confirm") != DELETE_MEMORY_TOKEN:
            return JSONResponse(
                status_code=400,
                content=error_body(
                    "confirm_token_required",
                    f"DELETE requires the body token {DELETE_MEMORY_TOKEN!r}.",
                ),
            )
        owner = bridge.config.OWNER_USER_ID
        try:
            removed = await backend.delete(owner, record_id)
        except PinnedMemoryError:
            return JSONResponse(
                status_code=409,
                content=error_body(
                    "pinned_memory",
                    "Pinned rows never delete; unpin the row first.",
                ),
            )
        except ChromaDeleteError:
            return JSONResponse(
                status_code=503,
                content=error_body(
                    "memory_delete_failed",
                    "Chroma deletion could not be confirmed; Redis data was preserved.",
                ),
            )
        if removed is None:
            return JSONResponse(
                status_code=404,
                content=error_body("not_found", "No such memory row."),
            )
        return {"deleted": record_id}

    @app.post("/memories/cleanup")
    async def cleanup_memories(request: Request):
        if not bridge.config.MEMORY_CLEANUP_ENABLED:
            return JSONResponse(
                status_code=409,
                content=error_body(
                    "feature_disabled",
                    "Memory cleanup is disabled on this deployment.",
                ),
            )
        body = await _parse_json(request)
        if not isinstance(body, dict):
            return JSONResponse(
                status_code=400,
                content=error_body("bad_json", "Request body must be a JSON object."),
            )
        dry_run = body.get("dry_run") is True
        owner = bridge.config.OWNER_USER_ID
        try:
            result = await backend.cleanup(owner, dry_run=dry_run)
        except ChromaDeleteError:
            return JSONResponse(
                status_code=503,
                content=error_body(
                    "memory_cleanup_failed",
                    "Chroma deletion could not be confirmed; Redis data was preserved.",
                ),
            )
        return result


__all__ = ["register_memory_routes"]
