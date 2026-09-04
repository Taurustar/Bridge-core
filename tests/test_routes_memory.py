"""Memory, history, and admin-wipe HTTP route tests (plan sections 20.5, 20.6, 29)."""

from __future__ import annotations

import asyncio
import json
import unittest

from core.cache import RedisCache
from core.memory import MemoryBackend

from fakes import FakeChroma, FakeRedis, make_config


def make_app(**config_overrides):
    from core.app import create_app

    fake = FakeRedis()
    config = make_config(**config_overrides)
    app = create_app(config, cache=RedisCache(fake))
    return app, fake


class MemoryRouteTest(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient

        self.app, self.fake = make_app(MEMORY_CLEANUP_ENABLED=True)
        self.client = TestClient(self.app)
        self.backend = MemoryBackend(self.app.state.bridge.config, RedisCache(self.fake))

    def _seed(self, text: str, kind: str = "commitment", pinned: bool = False) -> dict:
        record = self.backend.make_record(
            kind=kind, text=text, source="test", source_mode="companion",
            importance=0.5, pinned=pinned,
        )
        asyncio.run(self.backend.add("owner", record))
        return record

    def test_create_list_get_patch_delete(self):
        created = self.client.post(
            "/memories",
            json={"kind": "commitment", "text": "Owner waters plants on Fridays."},
        )
        self.assertEqual(created.status_code, 201)
        record_id = created.json()["id"]

        listing = self.client.get("/memories").json()
        self.assertEqual(listing["total"], 1)
        self.assertEqual(listing["items"][0]["id"], record_id)

        got = self.client.get(f"/memories/{record_id}")
        self.assertEqual(got.status_code, 200)

        patched = self.client.patch(
            f"/memories/{record_id}", json={"pinned": True}
        )
        self.assertEqual(patched.status_code, 200)
        self.assertTrue(patched.json()["pinned"])

        # Pinned rows refuse deletion.
        refused = self.client.request(
            "DELETE", f"/memories/{record_id}", json={"confirm": "DELETE_MEMORY"}
        )
        self.assertEqual(refused.status_code, 409)
        self.assertEqual(refused.json()["error"]["code"], "pinned_memory")

        unpinned = self.client.patch(
            f"/memories/{record_id}", json={"pinned": False}
        )
        self.assertEqual(unpinned.status_code, 200)
        deleted = self.client.request(
            "DELETE", f"/memories/{record_id}", json={"confirm": "DELETE_MEMORY"}
        )
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(self.client.get(f"/memories/{record_id}").status_code, 404)

    def test_delete_requires_confirm_token(self):
        record = self._seed("delete me")
        missing_token = self.client.request("DELETE", f"/memories/{record['id']}")
        self.assertEqual(missing_token.status_code, 400)
        self.assertEqual(missing_token.json()["error"]["code"], "confirm_token_required")

    def test_patch_rejects_unknown_fields(self):
        record = self._seed("patch target")
        response = self.client.patch(
            f"/memories/{record['id']}", json={"banana": True}
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_memory")

    def test_patch_enforces_text_and_metadata_bounds(self):
        record = self._seed("patch target")
        too_long = self.client.patch(
            f"/memories/{record['id']}", json={"text": "x" * 1001}
        )
        self.assertEqual(too_long.status_code, 400)
        too_many_keys = self.client.patch(
            f"/memories/{record['id']}",
            json={"metadata": {f"key_{index}": index for index in range(33)}},
        )
        self.assertEqual(too_many_keys.status_code, 400)
        too_large = self.client.patch(
            f"/memories/{record['id']}", json={"metadata": {"note": "x" * 4096}}
        )
        self.assertEqual(too_large.status_code, 400)
        first = self.client.patch(
            f"/memories/{record['id']}", json={"metadata": {"first": "x" * 3000}}
        )
        self.assertEqual(first.status_code, 200)
        merged_too_large = self.client.patch(
            f"/memories/{record['id']}", json={"metadata": {"second": "x" * 1500}}
        )
        self.assertEqual(merged_too_large.status_code, 400)

    def test_create_rejects_unknown_kind(self):
        response = self.client.post("/memories", json={"kind": "gossip", "text": "x"})
        self.assertEqual(response.status_code, 400)

    def test_search_filter(self):
        self._seed("Owner speaks Japanese weekly")
        self._seed("Project alpha uses redis")
        filtered = self.client.get("/memories", params={"q": "japanese"}).json()
        self.assertEqual(filtered["total"], 1)
        self.assertIn("Japanese", filtered["items"][0]["text"])

    def test_cleanup_requires_flag(self):
        from fastapi.testclient import TestClient

        app, fake = make_app(MEMORY_CLEANUP_ENABLED=False)
        client = TestClient(app)
        response = client.post("/memories/cleanup", json={"dry_run": True})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "feature_disabled")

    def test_cleanup_dry_run_reports_without_deleting(self):
        record = self.backend.make_record(
            kind="conversation", text="old chat", source="t", source_mode="companion"
        )
        record["updated_ts"] = 1.0
        asyncio.run(self.backend.add("owner", record))
        # TTL default is 30 days; force expiry via the engine config.
        self.app.state.bridge.config.MEMORY_CONVERSATION_TTL_DAYS = 1
        dry = self.client.post("/memories/cleanup", json={"dry_run": True}).json()
        self.assertEqual(dry["count"], 1)
        self.assertEqual(self.client.get("/memories").json()["total"], 1)
        real = self.client.post("/memories/cleanup", json={"dry_run": False}).json()
        self.assertEqual(real["deleted"], [record["id"]])
        self.assertEqual(self.client.get("/memories").json()["total"], 0)

    def test_delete_returns_503_and_preserves_redis_when_chroma_delete_fails(self):
        bridge = self.app.state.bridge
        chroma = FakeChroma(fail=True)
        chroma.available = True
        bridge.longterm.chroma = chroma
        record = bridge.longterm.make_record(
            kind="commitment", text="private row", source="test", source_mode="admin"
        )
        asyncio.run(bridge.longterm.redis.add("owner", record))
        chroma.indexed[record["id"]] = {"owner": "owner", "text": record["text"]}

        response = self.client.request(
            "DELETE", f"/memories/{record['id']}", json={"confirm": "DELETE_MEMORY"}
        )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "memory_delete_failed")
        self.assertIsNotNone(asyncio.run(bridge.longterm.get("owner", record["id"])))
        self.assertIn(record["id"], chroma.indexed)

    def test_delete_returns_503_when_chroma_silently_retains_row(self):
        bridge = self.app.state.bridge
        chroma = FakeChroma(retain_deletes=True)
        chroma.start()
        bridge.longterm.chroma = chroma
        record = bridge.longterm.make_record(
            kind="commitment", text="retained row", source="test", source_mode="admin"
        )
        asyncio.run(bridge.longterm.add("owner", record))

        response = self.client.request(
            "DELETE", f"/memories/{record['id']}", json={"confirm": "DELETE_MEMORY"}
        )

        self.assertEqual(response.status_code, 503)
        self.assertIsNotNone(asyncio.run(bridge.longterm.get("owner", record["id"])))
        self.assertIn(record["id"], chroma.indexed)

    def test_cleanup_returns_503_and_preserves_redis_when_chroma_delete_fails(self):
        bridge = self.app.state.bridge
        chroma = FakeChroma(fail=True)
        chroma.available = True
        bridge.longterm.chroma = chroma
        record = bridge.longterm.make_record(
            kind="conversation", text="old private row", source="test",
            source_mode="companion",
        )
        record["updated_ts"] = 1.0
        asyncio.run(bridge.longterm.redis.add("owner", record))
        chroma.indexed[record["id"]] = {"owner": "owner", "text": record["text"]}
        bridge.config.MEMORY_CONVERSATION_TTL_DAYS = 1

        response = self.client.post("/memories/cleanup", json={"dry_run": False})

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "memory_cleanup_failed")
        self.assertIsNotNone(asyncio.run(bridge.longterm.get("owner", record["id"])))
        self.assertIn(record["id"], chroma.indexed)

    def test_cleanup_rejects_malformed_and_non_object_json(self):
        malformed = self.client.post(
            "/memories/cleanup",
            content="{",
            headers={"content-type": "application/json"},
        )
        non_object = self.client.post("/memories/cleanup", json=[])

        expected = {
            "error": {
                "code": "bad_json",
                "message": "Request body must be a JSON object.",
                "details": {},
            }
        }
        self.assertEqual(malformed.status_code, 400)
        self.assertEqual(malformed.json(), expected)
        self.assertEqual(non_object.status_code, 400)
        self.assertEqual(non_object.json(), expected)

    def test_memory_mutation_backend_failure_returns_standard_503(self):
        async def unavailable(*args, **kwargs):
            raise ConnectionError("redis unavailable")

        self.app.state.bridge.longterm.add = unavailable
        response = self.client.post(
            "/memories",
            json={"kind": "commitment", "text": "store this"},
        )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.json(),
            {"error": {
                "code": "redis_unavailable",
                "message": "The required data store is temporarily unavailable.",
                "details": {},
            }},
        )


class HistoryRouteTest(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient

        self.app, self.fake = make_app()
        self.client = TestClient(self.app)

    def _seed_history(self, count: int):
        from core.history import companion_history_key

        rows = [
            {"id": f"msg_{index:03d}", "role": "user" if index % 2 == 0 else "assistant",
             "text": f"hello {index}", "emotion": "neutral", "mode": "companion",
             "ts": f"2026-09-03T00:00:{index:02d}+00:00", "delivery_state": "delivered"}
            for index in range(count)
        ]
        self.fake.store[companion_history_key("owner")] = [
            json.dumps(row) for row in rows
        ]
        return rows

    def test_list_history_with_paging_and_order(self):
        rows = self._seed_history(5)
        page = self.client.get("/history", params={"limit": 2, "offset": 0}).json()
        self.assertEqual(page["total"], 5)
        self.assertEqual([row["id"] for row in page["items"]], ["msg_000", "msg_001"])
        descending = self.client.get(
            "/history", params={"order": "desc", "limit": 2}
        ).json()
        self.assertEqual([row["id"] for row in descending["items"]], ["msg_004", "msg_003"])
        after = self.client.get(
            "/history", params={"after_id": "msg_002", "limit": 10}
        ).json()
        self.assertEqual([row["id"] for row in after["items"]], ["msg_003", "msg_004"])

    def test_history_limit_is_capped(self):
        self._seed_history(10)
        page = self.client.get("/history", params={"limit": 500}).json()
        self.assertEqual(page["limit"], 200)

    def test_midterm_lists_chapters(self):
        from core.constants import midterm_key

        chapter = {
            "id": "mem_ch1", "kind": "conversation_chapter", "text": "chapter one",
            "source": "history_compaction", "source_mode": "companion",
            "importance": 0.5, "created_ts": 1.0, "updated_ts": 1.0,
            "pinned": False, "metadata": {},
        }
        self.fake.store[midterm_key("owner")] = [json.dumps(chapter)]
        listing = self.client.get("/history/midterm").json()
        self.assertEqual(listing["total"], 1)
        self.assertEqual(listing["items"][0]["id"], "mem_ch1")

    def test_midterm_total_covers_whole_ring_not_injection_limit(self):
        from core.constants import midterm_key

        chapters = [
            {
                "id": f"mem_ch{index}", "kind": "conversation_chapter",
                "text": f"chapter {index}", "created_ts": float(index),
            }
            for index in range(7)
        ]
        self.fake.store[midterm_key("owner")] = [json.dumps(row) for row in chapters]
        listing = self.client.get("/history/midterm", params={"limit": 2}).json()
        self.assertEqual(listing["total"], 7)
        self.assertEqual([row["id"] for row in listing["items"]], ["mem_ch6", "mem_ch5"])

    def test_history_read_backend_failure_returns_standard_503(self):
        async def unavailable(*args, **kwargs):
            raise ConnectionError("redis unavailable")

        self.app.state.bridge.cache.get_rows = unavailable
        response = self.client.get("/history")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "redis_unavailable")
        self.assertEqual(response.json()["error"]["details"], {})


class WipeRouteTest(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient

        self.app, self.fake = make_app()
        self.client = TestClient(self.app)

    def test_wrong_owner_forbidden(self):
        response = self.client.post(
            "/admin/wipe/someone-else", json={"confirm": "WIPE_USER"}
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "forbidden_user")

    def test_wrong_confirm_rejected(self):
        response = self.client.post("/admin/wipe/owner", json={"confirm": "NOPE"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "confirm_token_required")

    def test_wipe_clears_every_documented_key_family(self):
        from core.constants import wipe_key_patterns

        documented = [
            "core:history:owner:companion",
            "core:history:owner:session:ses_1",
            "core:midterm:owner:companion",
            "core:longterm:owner",
            "core:needs:owner",
            "core:bids:owner",
            "core:rhythm:owner",
            "core:owner_profile:owner",
            "core:external_profile:owner:discord:1234",
            "core:life:last_block:owner",
            "core:life:pending:owner",
            "core:initiative:owner",
            "core:deferred:owner",
            "core:busy_count:owner",
            "core:sessions:owner",
            "core:projects:owner",
            "core:pending_agent:owner:ses_1:run_1",
            "core:agent_run:owner:ses_1",
            "core:mcp_response:owner:req_1",
            "core:device_response:owner:req_1",
            "core:device:audit:owner",
            "core:daily:reminders:owner",
            "core:daily:idempotency:owner",
            "core:user_schedule:owner",
            "core:user_schedule:day:owner:2026-09-03",
        ]
        for key in documented:
            self.fake.store[key] = ["x"]
        self.fake.store["core:longterm:owner"] = [json.dumps({"id": "mem_1"})]
        # Every documented family must be covered by the wipe patterns.
        for key in documented:
            matched = any(
                self._pattern_matches(pattern, key)
                for pattern in wipe_key_patterns("owner")
            )
            self.assertTrue(matched, f"wipe misses documented key {key}")

        response = self.client.post(
            "/admin/wipe/owner", json={"confirm": "WIPE_USER"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["wiped"])
        self.assertEqual(response.json()["keys_deleted"], len(documented))
        self.assertEqual(response.json()["memory_rows_deleted"], 1)
        remaining = set(self.fake.store) | set(self.fake.strings)
        self.assertEqual([key for key in remaining if ":owner" in key], [])

    def test_wipe_holds_owner_turn_lock(self):
        bridge = self.app.state.bridge
        original_wipe = bridge.longterm.wipe

        async def checking_wipe(owner):
            self.assertTrue(bridge.connections.turn_lock(owner).locked())
            return await original_wipe(owner)

        bridge.longterm.wipe = checking_wipe
        response = self.client.post("/admin/wipe/owner", json={"confirm": "WIPE_USER"})
        self.assertEqual(response.status_code, 200)

    def test_chroma_wipe_failure_returns_error_and_preserves_redis(self):
        bridge = self.app.state.bridge
        bridge.longterm.chroma = FakeChroma(fail=True)
        bridge.longterm.chroma.available = True
        record = bridge.longterm.make_record(
            kind="commitment", text="keep on failed wipe", source="test",
            source_mode="admin",
        )
        asyncio.run(bridge.longterm.redis.add("owner", record))
        self.fake.store["core:history:owner:companion"] = ["history"]

        response = self.client.post("/admin/wipe/owner", json={"confirm": "WIPE_USER"})

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "wipe_failed")
        self.assertEqual(asyncio.run(bridge.longterm.count("owner")), 1)
        self.assertIn("core:history:owner:companion", self.fake.store)

    def test_wipe_deletes_longterm_key_recreated_after_chroma_wipe(self):
        bridge = self.app.state.bridge
        original_wipe = bridge.longterm.wipe

        async def recreating_wipe(owner):
            removed = await original_wipe(owner)
            self.fake.store["core:longterm:owner"] = [json.dumps({"id": "late"})]
            return removed

        bridge.longterm.wipe = recreating_wipe
        response = self.client.post("/admin/wipe/owner", json={"confirm": "WIPE_USER"})

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("core:longterm:owner", self.fake.store)

    def test_wipe_redis_failure_returns_standard_503(self):
        async def unavailable(*args, **kwargs):
            raise ConnectionError("redis unavailable")

        self.app.state.bridge.cache.keys = unavailable
        response = self.client.post(
            "/admin/wipe/owner", json={"confirm": "WIPE_USER"}
        )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "redis_unavailable")
        self.assertEqual(response.json()["error"]["details"], {})

    @staticmethod
    def _pattern_matches(pattern: str, key: str) -> bool:
        import fnmatch

        return fnmatch.fnmatch(key, pattern)


if __name__ == "__main__":
    unittest.main()
