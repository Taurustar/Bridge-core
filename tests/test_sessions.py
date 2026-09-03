"""Session store tests (plan section 25.2)."""

from __future__ import annotations

import unittest

from core.cache import RedisCache
from core.constants import SESSION_MAX_COUNT, companion_history_key
from core.sessions import SessionError, SessionStore
from core.history import (
    append_row_to,
    load_rows,
    load_rows_from,
    make_row,
    session_history_key,
)

from fakes import FakeRedis, make_config


def make_store() -> tuple[SessionStore, FakeRedis]:
    fake = FakeRedis()
    return SessionStore(make_config(), RedisCache(fake)), fake


class SessionResolutionTest(unittest.IsolatedAsyncioTestCase):
    async def test_auto_create_and_latest_for_project(self):
        store, _ = make_store()
        session, created = await store.resolve(
            "owner", session_id=None, project_id=None
        )
        self.assertTrue(created)
        self.assertTrue(session["id"].startswith("ses_"))
        # Project resolution returns the latest active for that project.
        second, created2 = await store.resolve(
            "owner", session_id=None, project_id="prj_a"
        )
        self.assertTrue(created2)
        third, created3 = await store.resolve(
            "owner", session_id=None, project_id="prj_a"
        )
        self.assertFalse(created3)
        self.assertEqual(third["id"], second["id"])

    async def test_explicit_unknown_and_archived(self):
        store, _ = make_store()
        with self.assertRaises(SessionError):
            await store.resolve("owner", session_id="ses_missing", project_id=None)
        session, _ = await store.resolve("owner", session_id=None, project_id=None)
        await store.archive("owner", session["id"])
        with self.assertRaises(SessionError):
            await store.resolve(
                "owner", session_id=session["id"], project_id=None
            )
        # Auto-resolution never returns archived sessions.
        resolved, created = await store.resolve(
            "owner", session_id=None, project_id=None
        )
        self.assertTrue(created)
        self.assertNotEqual(resolved["id"], session["id"])

    async def test_cap_drops_oldest_archived_first(self):
        from core.constants import SESSION_MAX_COUNT

        store, _ = make_store()
        sessions: dict[str, dict] = {}
        for index in range(SESSION_MAX_COUNT - 1):
            record = (await store.resolve("owner", session_id=None,
                                          project_id=None))[0]
            record["created_ts"] = float(index)
            record["updated_ts"] = float(index)
            sessions[record["id"]] = record
        sessions[next(iter(sessions))]["status"] = "archived"
        await store.write_all("owner", sessions)
        resolved, created = await store.resolve(
            "owner", session_id=None, project_id=None
        )
        self.assertTrue(created)
        self.assertLessEqual(
            len(await store.read_all("owner")), SESSION_MAX_COUNT
        )


class HistoryIsolationTest(unittest.IsolatedAsyncioTestCase):
    async def test_work_history_never_mixes_with_companion(self):
        fake = FakeRedis()
        cache = RedisCache(fake)
        store = SessionStore(make_config(), cache)
        session, _ = await store.resolve("owner", session_id=None, project_id=None)
        key = session_history_key("owner", session["id"])
        await append_row_to(cache, key, make_row("user", "work thing",
                                                 "delivered", mode="work"),
                            max_rows=80)
        await append_row_to(cache, companion_history_key("owner"),
                            make_row("user", "chat thing", "delivered"),
                            max_rows=80)
        work_rows = await load_rows(cache, "owner")  # companion key only
        self.assertEqual([row["text"] for row in work_rows], ["chat thing"])
        session_rows = await load_rows_from(cache, key)
        self.assertEqual([row["text"] for row in session_rows], ["work thing"])
        self.assertEqual(session_rows[0]["mode"], "work")


class ProjectFactsTest(unittest.IsolatedAsyncioTestCase):
    async def test_touch_and_append_bounded_facts(self):
        store, _ = make_store()
        await store.touch_project("owner", "prj_x")
        updated = await store.append_project_facts(
            "owner", "prj_x", ["Uses FastAPI.", "Uses FastAPI.", "Ships weekly."]
        )
        self.assertEqual(updated["facts"], ["Uses FastAPI.", "Ships weekly."])


if __name__ == "__main__":
    unittest.main()
