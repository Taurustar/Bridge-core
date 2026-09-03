"""Work sessions and projects (plan sections 25.2, 25.8, 28).

Session resolution order: explicit ``session_id`` → latest active session
for the explicit/detected project → auto-create. Archived sessions never
auto-resume. Work history lives at
``core:history:{owner}:session:{session_id}`` and is never mixed with
companion history.

Both stores are single JSON documents with a hard session cap. Projects
are a minimal registry: id, goal, summary, and bounded facts appended at
archive time — high-level facts only, never code dumps or transcripts.
"""

from __future__ import annotations

import json
import logging
import time
import uuid

from .cache import RedisCache
from .config import Config
from .constants import (
    SESSION_MAX_COUNT,
    projects_key,
    sessions_key,
)

log = logging.getLogger("bridge.sessions")

MAX_TITLE_CHARS = 120
MAX_SUMMARY_CHARS = 1000
MAX_PROJECT_FACTS = 64
MAX_PROJECT_FACT_CHARS = 300
MAX_SESSION_FACTS_SNAPSHOT = 16


class SessionError(ValueError):
    """Invalid session reference or state."""


def new_session_id() -> str:
    return f"ses_{uuid.uuid4().hex}"


def new_project_id() -> str:
    return f"prj_{uuid.uuid4().hex}"


def default_session(session_id: str, project_id: str = "", title: str = "") -> dict:
    now = time.time()
    return {
        "id": session_id,
        "title": (title or "Work session")[:MAX_TITLE_CHARS],
        "project_id": project_id,
        "status": "active",
        "summary": "",
        "last_run_id": "",
        "message_count": 0,
        "created_ts": now,
        "updated_ts": now,
    }


class SessionStore:
    """Bounded session and project registry per owner."""

    def __init__(self, config: Config, cache: RedisCache) -> None:
        self.config = config
        self.cache = cache

    # -- sessions -----------------------------------------------------------

    async def read_all(self, owner: str) -> dict[str, dict]:
        raw = await self.cache.get_value(sessions_key(owner))
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("Corrupt session store ignored")
            return {}
        sessions = data.get("sessions") if isinstance(data, dict) else None
        return sessions if isinstance(sessions, dict) else {}

    async def write_all(self, owner: str, sessions: dict[str, dict]) -> None:
        await self.cache.set_value(
            sessions_key(owner), json.dumps({"sessions": sessions})
        )

    async def get(self, owner: str, session_id: str) -> dict | None:
        return (await self.read_all(owner)).get(session_id)

    async def list_sessions(
        self, owner: str, status: str | None = None, limit: int = 50
    ) -> list[dict]:
        sessions = await self.read_all(owner)
        rows = [
            s
            for s in sessions.values()
            if status is None or s.get("status") == status
        ]
        rows.sort(key=lambda s: float(s.get("updated_ts", 0) or 0), reverse=True)
        return rows[: max(1, min(limit, 200))]

    async def resolve(
        self,
        owner: str,
        *,
        session_id: str | None,
        project_id: str | None,
        create: bool = True,
    ) -> tuple[dict, bool]:
        """Plan 25.2 resolution order. Raises SessionError on an explicit
        archived or unknown id. Returns (session, created)."""
        sessions = await self.read_all(owner)
        if session_id:
            existing = sessions.get(session_id)
            if existing is None:
                raise SessionError(f"Unknown session: {session_id}")
            if existing.get("status") == "archived":
                raise SessionError(
                    f"Session {session_id} is archived and cannot resume"
                )
            return existing, False
        if project_id:
            candidates = [
                s
                for s in sessions.values()
                if s.get("status") == "active"
                and (s.get("project_id") or "") == project_id
            ]
            if candidates:
                latest = max(
                    candidates, key=lambda s: float(s.get("updated_ts", 0) or 0)
                )
                return latest, False
        if not create:
            raise SessionError("No session resolved and creation is disabled")
        if len(sessions) >= SESSION_MAX_COUNT:
            # Drop the oldest archived sessions first, then oldest overall.
            def oldest(rows: list[dict]) -> dict:
                return min(rows, key=lambda s: float(s.get("updated_ts", 0) or 0))

            archived = [s for s in sessions.values() if s.get("status") == "archived"]
            victim = oldest(archived) if archived else oldest(list(sessions.values()))
            sessions.pop(victim["id"], None)
            log.info("Session cap reached; dropped %s", victim["id"])
        session = default_session(new_session_id(), project_id=project_id or "")
        sessions[session["id"]] = session
        await self.write_all(owner, sessions)
        return session, True

    async def update(
        self, owner: str, session_id: str, **changes
    ) -> dict | None:
        sessions = await self.read_all(owner)
        session = sessions.get(session_id)
        if session is None:
            return None
        for key, value in changes.items():
            if key in ("title", "summary"):
                limit = MAX_TITLE_CHARS if key == "title" else MAX_SUMMARY_CHARS
                session[key] = str(value or "")[:limit]
            elif key in ("status", "last_run_id", "message_count", "project_id"):
                session[key] = value
        session["updated_ts"] = time.time()
        await self.write_all(owner, sessions)
        return session

    async def archive(
        self, owner: str, session_id: str, summary: str = ""
    ) -> dict | None:
        return await self.update(
            owner,
            session_id,
            status="archived",
            summary=summary or None,
        )

    # -- projects ---------------------------------------------------------------

    async def read_projects(self, owner: str) -> dict[str, dict]:
        raw = await self.cache.get_value(projects_key(owner))
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        projects = data.get("projects") if isinstance(data, dict) else None
        return projects if isinstance(projects, dict) else {}

    async def write_projects(self, owner: str, projects: dict[str, dict]) -> None:
        await self.cache.set_value(
            projects_key(owner), json.dumps({"projects": projects})
        )

    async def touch_project(self, owner: str, project_id: str) -> dict:
        projects = await self.read_projects(owner)
        project = projects.get(project_id)
        if project is None:
            project = {
                "id": project_id,
                "goal": "",
                "summary": "",
                "facts": [],
                "created_ts": time.time(),
                "updated_ts": time.time(),
            }
            projects[project_id] = project
            await self.write_projects(owner, projects)
        return project

    async def append_project_facts(
        self, owner: str, project_id: str, facts: list[str]
    ) -> dict | None:
        """Append bounded, high-level facts (plan section 25.8)."""
        projects = await self.read_projects(owner)
        project = projects.get(project_id)
        if project is None:
            return None
        for fact in facts[:8]:
            clean = str(fact or "").strip()[:MAX_PROJECT_FACT_CHARS]
            if clean and clean not in project["facts"]:
                project["facts"].append(clean)
        project["facts"] = project["facts"][-MAX_PROJECT_FACTS:]
        project["updated_ts"] = time.time()
        await self.write_projects(owner, projects)
        return project
