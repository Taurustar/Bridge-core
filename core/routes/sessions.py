"""Work/session HTTP APIs (plan sections 25.2, 29).

- ``GET /work``: non-secret work overview (enabled flags, sessions,
  armed devices).
- Sessions are listed, inspected, and archived. Archived sessions never
  auto-resume; a run/checkpoint diagnostics view is read-only.
- No destructive confirm tokens: archiving is reversible state, not a
  wipe.
"""

from __future__ import annotations

import json
import logging

from fastapi import Request
from fastapi.responses import JSONResponse

from ..app import error_body
from ..constants import agent_run_key

log = logging.getLogger("bridge.routes.sessions")


def register_session_routes(app, bridge) -> None:
    def _work_disabled():
        return JSONResponse(
            status_code=403,
            content=error_body(
                "feature_disabled",
                "Work mode is disabled on this deployment.",
            ),
        )

    @app.get("/work")
    async def work_overview():
        if not (bridge.config.WORK_ENABLED and bridge.config.SESSIONS_ENABLED):
            return _work_disabled()
        owner = bridge.config.OWNER_USER_ID
        sessions = await bridge.sessions.list_sessions(owner, limit=50)
        return {
            "enabled": True,
            "mcp": bridge.config.MCP_PROXY_ENABLED,
            "device": bridge.config.DEVICE_ENABLED,
            "armed_devices": len(bridge.device.armed_connections(owner, "read")),
            "active_sessions": len(
                [s for s in sessions if s.get("status") == "active"]
            ),
            "sessions": sessions,
        }

    @app.get("/sessions")
    async def list_sessions(request: Request):
        if not (bridge.config.WORK_ENABLED and bridge.config.SESSIONS_ENABLED):
            return _work_disabled()
        owner = bridge.config.OWNER_USER_ID
        status = request.query_params.get("status")
        if status is not None and status not in ("active", "archived"):
            return JSONResponse(
                status_code=400,
                content=error_body(
                    "invalid_request", "status must be active or archived."
                ),
            )
        limit = int(request.query_params.get("limit", "50"))
        limit = max(1, min(limit, 200))
        sessions = await bridge.sessions.list_sessions(
            owner, status=status, limit=limit
        )
        return {
            "items": sessions,
            "total": len(sessions),
            "limit": limit,
            "offset": 0,
        }

    @app.get("/sessions/{session_id}")
    async def get_session(session_id: str):
        if not (bridge.config.WORK_ENABLED and bridge.config.SESSIONS_ENABLED):
            return _work_disabled()
        owner = bridge.config.OWNER_USER_ID
        session = await bridge.sessions.get(owner, session_id)
        if session is None:
            return JSONResponse(
                status_code=404,
                content=error_body("not_found", "Unknown session."),
            )
        return {"session": session}

    @app.post("/sessions/{session_id}/archive")
    async def archive_session(session_id: str, request: Request):
        if not (bridge.config.WORK_ENABLED and bridge.config.SESSIONS_ENABLED):
            return _work_disabled()
        owner = bridge.config.OWNER_USER_ID
        session = await bridge.sessions.get(owner, session_id)
        if session is None:
            return JSONResponse(
                status_code=404,
                content=error_body("not_found", "Unknown session."),
            )
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        summary = ""
        facts: list[str] = []
        if isinstance(body, dict) and bridge.config.SESSION_SUMMARY_ENABLED:
            # One bounded session_summary LLM call (plan section 7.2).
            history_key = f"core:history:{owner}:session:{session_id}"
            from .. import history as hist

            rows = await hist.load_rows_from(bridge.cache, history_key)
            if rows:
                try:
                    from ..prompts import build_session_summary_prompt

                    messages = build_session_summary_prompt(
                        history=rows,
                        previous_summary=str(session.get("summary") or ""),
                    )
                    result = await bridge.llm.chat("session_summary", messages)
                    proposal = json.loads(result.text)
                    if isinstance(proposal, dict):
                        summary = str(proposal.get("summary") or "")[:1000]
                        raw_facts = proposal.get("project_facts")
                        if isinstance(raw_facts, list):
                            facts = [
                                str(fact)[:300] for fact in raw_facts[:8]
                            ]
                except (ValueError, json.JSONDecodeError) as exc:
                    log.info(
                        "Session summary skipped (%s)", type(exc).__name__
                    )
        archived = await bridge.sessions.archive(
            owner, session_id, summary=summary
        )
        if session.get("project_id") and facts:
            await bridge.sessions.append_project_facts(
                owner, session["project_id"], facts
            )
        return {"session": archived}

    @app.get("/sessions/{session_id}/runs")
    async def session_runs(session_id: str):
        """Read-only run/checkpoint diagnostics (plan section 29)."""
        if not (bridge.config.WORK_ENABLED and bridge.config.SESSIONS_ENABLED):
            return _work_disabled()
        owner = bridge.config.OWNER_USER_ID
        session = await bridge.sessions.get(owner, session_id)
        if session is None:
            return JSONResponse(
                status_code=404,
                content=error_body("not_found", "Unknown session."),
            )
        raw = await bridge.cache.get_value(agent_run_key(owner, session_id))
        if not raw:
            return {"runs": [], "total": 0}
        try:
            record = json.loads(raw)
        except json.JSONDecodeError:
            record = {}
        return {"runs": [record] if record else [], "total": 1}

