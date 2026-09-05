"""Dormant external-user profile store/API tests (plan sections 19, 29).

Acceptance coverage (plan section 31, milestone 0.7.0):
- With ``EXTERNAL_USER_PROFILE_STORE_ENABLED=false``, CRUD returns
  ``feature_disabled`` and creates no keys.
- With the store enabled but behavior disabled (default), admin CRUD works
  while app prompts, turn updates, and LLM analysis stay inert.
- Identity canonicalization, patch validation, confirm guards, list
  envelope/sort, version conflict, and wipe coverage of the key family.
"""

from __future__ import annotations

import json
import unittest
import urllib.parse

from fastapi.testclient import TestClient

from core.app import create_app
from core.cache import RedisCache
from core.constants import UPDATE_EXTERNAL_PROFILE_TOKEN, external_profile_key

from fakes import FakeLLM, FakeRedis, make_config

TOKEN_HEADER = "X-Confirm-Token"


def build_app(config=None):
    fake_redis = FakeRedis()
    config = config or make_config()
    app = create_app(
        config, cache=RedisCache(fake_redis), llm=FakeLLM(), tailscale_addresses=set()
    )
    return app, fake_redis


class StoreDisabledTest(unittest.TestCase):
    def setUp(self):
        self.app, self.fake = build_app(make_config(
            EXTERNAL_USER_PROFILE_STORE_ENABLED=False
        ))
        self.client = TestClient(self.app)

    def test_all_routes_feature_disabled_without_keys(self):
        response = self.client.get("/profiles/external")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "feature_disabled")

        got = self.client.get("/profiles/external/discord/123")
        self.assertEqual(got.status_code, 409)

        patched = self.client.patch(
            "/profiles/external/discord/123",
            headers={TOKEN_HEADER: UPDATE_EXTERNAL_PROFILE_TOKEN},
            json={"display_name": "A"},
        )
        self.assertEqual(patched.status_code, 409)

        created = self.client.post(
            "/profiles/external/discord/123",
            headers={TOKEN_HEADER: UPDATE_EXTERNAL_PROFILE_TOKEN},
        )
        self.assertEqual(created.status_code, 409)

        deleted = self.client.request(
            "DELETE",
            "/profiles/external/discord/123",
            json={"confirm": "DELETE_EXTERNAL_PROFILE"},
        )
        self.assertEqual(deleted.status_code, 409)

        self.assertEqual(self.fake.store, {})
        self.assertEqual(self.fake.strings, {})


class StoreCrudTest(unittest.TestCase):
    def setUp(self):
        # Default flags: store enabled, behavior disabled (dormant).
        self.app, self.fake = build_app(make_config())
        self.client = TestClient(self.app)
        created = self.client.post(
            "/profiles/external/discord/123456789",
            headers={TOKEN_HEADER: UPDATE_EXTERNAL_PROFILE_TOKEN},
        )
        self.assertEqual(created.status_code, 200)
        self.profile = created.json()["profile"]

    def test_default_schema_and_key(self):
        expected_key = external_profile_key("owner", "discord", "123456789")
        raw = self.fake.strings.get(expected_key)
        self.assertIsNotNone(raw)
        stored = json.loads(raw)
        self.assertEqual(stored["subject_id"], "discord:123456789")
        self.assertEqual(stored["platform"], "discord")
        self.assertEqual(stored["display_name"], "")
        self.assertEqual(stored["tone"], "neutral")
        self.assertEqual(stored["trust"], 50)
        self.assertEqual(stored["version"], 1)
        for field in (
            "aliases", "likes", "topics", "boundaries_seen", "observations"
        ):
            self.assertEqual(stored[field], [])

    def test_get_one_and_missing(self):
        got = self.client.get("/profiles/external/discord/123456789")
        self.assertEqual(got.status_code, 200)
        self.assertEqual(got.json()["subject_id"], "discord:123456789")
        missing = self.client.get("/profiles/external/discord/000")
        self.assertEqual(missing.status_code, 404)

    def test_list_envelope_and_sort(self):
        self.client.post(
            "/profiles/external/telegram/987654321",
            headers={TOKEN_HEADER: UPDATE_EXTERNAL_PROFILE_TOKEN},
        )
        # Bump the telegram profile so it sorts first by updated_ts desc.
        self.client.patch(
            "/profiles/external/telegram/987654321",
            headers={TOKEN_HEADER: UPDATE_EXTERNAL_PROFILE_TOKEN},
            json={"display_name": "Newest"},
        )
        listing = self.client.get("/profiles/external")
        self.assertEqual(listing.status_code, 200)
        body = listing.json()
        self.assertEqual(body["total"], 2)
        self.assertEqual(body["limit"], 50)
        self.assertEqual(body["offset"], 0)
        self.assertEqual(
            [item["subject_id"] for item in body["items"]],
            ["telegram:987654321", "discord:123456789"],
        )
        limited = self.client.get("/profiles/external?limit=1&offset=1")
        self.assertEqual(limited.json()["total"], 2)
        self.assertEqual(len(limited.json()["items"]), 1)
        self.assertEqual(
            limited.json()["items"][0]["subject_id"], "discord:123456789"
        )

    def test_patch_validates_and_normalizes(self):
        patched = self.client.patch(
            "/profiles/external/discord/123456789",
            headers={TOKEN_HEADER: UPDATE_EXTERNAL_PROFILE_TOKEN},
            json={
                "display_name": "Raven",
                "aliases": ["Rav"],
                "preferred_language": "ES",
                "tone": "warm",
                "familiarity": 40,
                "trust": 65,
                "likes": ["old maps"],
                "topics": ["cartography"],
                "observations": ["prefers short replies"],
                "nonsense_field": True,
            },
        )
        self.assertEqual(patched.status_code, 400)
        self.assertEqual(patched.json()["error"]["code"], "invalid_patch")

        patched = self.client.patch(
            "/profiles/external/discord/123456789",
            headers={TOKEN_HEADER: UPDATE_EXTERNAL_PROFILE_TOKEN},
            json={
                "display_name": "Raven",
                "aliases": ["Rav"],
                "preferred_language": "es",
                "tone": "warm",
                "familiarity": 40,
                "trust": 65,
                "likes": ["old maps"],
                "topics": ["cartography"],
                "observations": ["prefers short replies"],
            },
        )
        self.assertEqual(patched.status_code, 200)
        profile = patched.json()["profile"]
        self.assertEqual(profile["display_name"], "Raven")
        self.assertEqual(profile["preferred_language"], "es")
        self.assertEqual(profile["version"], 2)
        self.assertGreater(profile["updated_ts"], profile["created_ts"])

    def test_patch_bounds(self):
        too_long = self.client.patch(
            "/profiles/external/discord/123456789",
            headers={TOKEN_HEADER: UPDATE_EXTERNAL_PROFILE_TOKEN},
            json={"display_name": "x" * 121},
        )
        self.assertEqual(too_long.status_code, 400)
        too_many = self.client.patch(
            "/profiles/external/discord/123456789",
            headers={TOKEN_HEADER: UPDATE_EXTERNAL_PROFILE_TOKEN},
            json={"aliases": ["a"] * 17},
        )
        self.assertEqual(too_many.status_code, 400)
        bad_score = self.client.patch(
            "/profiles/external/discord/123456789",
            headers={TOKEN_HEADER: UPDATE_EXTERNAL_PROFILE_TOKEN},
            json={"trust": 101},
        )
        self.assertEqual(bad_score.status_code, 400)
        bad_language = self.client.patch(
            "/profiles/external/discord/123456789",
            headers={TOKEN_HEADER: UPDATE_EXTERNAL_PROFILE_TOKEN},
            json={"preferred_language": "fr"},
        )
        self.assertEqual(bad_language.status_code, 400)

    def test_patch_requires_confirm_token(self):
        unguarded = self.client.patch(
            "/profiles/external/discord/123456789",
            json={"display_name": "Nope"},
        )
        self.assertEqual(unguarded.status_code, 400)
        self.assertEqual(
            unguarded.json()["error"]["code"], "confirm_token_required"
        )

    def test_patch_version_conflict(self):
        conflict = self.client.patch(
            "/profiles/external/discord/123456789",
            headers={TOKEN_HEADER: UPDATE_EXTERNAL_PROFILE_TOKEN},
            json={"display_name": "Stale", "version": 7},
        )
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.json()["error"]["code"], "version_conflict")
        fresh = self.client.patch(
            "/profiles/external/discord/123456789",
            headers={TOKEN_HEADER: UPDATE_EXTERNAL_PROFILE_TOKEN},
            json={"display_name": "Current", "version": 1},
        )
        self.assertEqual(fresh.status_code, 200)

    def test_identity_canonicalization(self):
        upper = self.client.post(
            "/profiles/external/DISCORD/1",
            headers={TOKEN_HEADER: UPDATE_EXTERNAL_PROFILE_TOKEN},
        )
        self.assertEqual(upper.status_code, 200)
        self.assertEqual(upper.json()["profile"]["platform"], "discord")
        bad_platform = self.client.post(
            "/profiles/external/bad platform!/1",
            headers={TOKEN_HEADER: UPDATE_EXTERNAL_PROFILE_TOKEN},
        )
        self.assertEqual(bad_platform.status_code, 400)
        self.assertEqual(
            bad_platform.json()["error"]["code"], "invalid_external_profile"
        )
        empty_id = self.client.post(
            "/profiles/external/discord/",
            headers={TOKEN_HEADER: UPDATE_EXTERNAL_PROFILE_TOKEN},
        )
        # An empty path segment does not match the two-segment route.
        self.assertEqual(empty_id.status_code, 404)
        self.client.request(
            "DELETE",
            "/profiles/external/discord/1",
            json={"confirm": "DELETE_EXTERNAL_PROFILE"},
        )

    def test_url_encoded_external_id_round_trips(self):
        encoded = urllib.parse.quote("user@example.com", safe="")
        created = self.client.post(
            f"/profiles/external/whatsapp/{encoded}",
            headers={TOKEN_HEADER: UPDATE_EXTERNAL_PROFILE_TOKEN},
        )
        self.assertEqual(created.status_code, 200)
        self.assertEqual(
            created.json()["profile"]["subject_id"],
            f"whatsapp:user@example.com",
        )
        got = self.client.get(f"/profiles/external/whatsapp/{encoded}")
        self.assertEqual(got.status_code, 200)
        deleted = self.client.request(
            "DELETE",
            f"/profiles/external/whatsapp/{encoded}",
            json={"confirm": "DELETE_EXTERNAL_PROFILE"},
        )
        self.assertEqual(deleted.status_code, 200)

    def test_delete_guard_and_missing(self):
        unguarded = self.client.request(
            "DELETE", "/profiles/external/discord/123456789"
        )
        self.assertEqual(unguarded.status_code, 400)
        wrong = self.client.request(
            "DELETE",
            "/profiles/external/discord/123456789",
            json={"confirm": "WIPE_USER"},
        )
        self.assertEqual(wrong.status_code, 400)
        missing = self.client.request(
            "DELETE",
            "/profiles/external/discord/000",
            json={"confirm": "DELETE_EXTERNAL_PROFILE"},
        )
        self.assertEqual(missing.status_code, 404)
        deleted = self.client.request(
            "DELETE",
            "/profiles/external/discord/123456789",
            json={"confirm": "DELETE_EXTERNAL_PROFILE"},
        )
        self.assertEqual(deleted.status_code, 200)
        self.assertTrue(deleted.json()["deleted"])
        self.assertNotIn(
            external_profile_key("owner", "discord", "123456789"),
            self.fake.strings,
        )

    def test_duplicate_create_conflicts(self):
        duplicate = self.client.post(
            "/profiles/external/discord/123456789",
            headers={TOKEN_HEADER: UPDATE_EXTERNAL_PROFILE_TOKEN},
        )
        self.assertEqual(duplicate.status_code, 409)
        self.assertEqual(duplicate.json()["error"]["code"], "already_exists")

    def test_wipe_covers_external_profile_keys(self):
        from core.constants import WIPE_USER_TOKEN

        wiped = self.client.post("/admin/wipe/owner", json={"confirm": WIPE_USER_TOKEN})
        self.assertEqual(wiped.status_code, 200)
        self.assertNotIn(
            external_profile_key("owner", "discord", "123456789"),
            self.fake.strings,
        )


class BehaviorInertTest(unittest.TestCase):
    def test_behavior_flag_gates_nothing_because_no_gateway_exists(self):
        """CRUD works with behavior disabled; no app path reads the store.

        The companion turn path creates only history keys even when dormant
        profiles exist (plan 19.4: app companion path never reads or
        injects these profiles).
        """
        app, fake = build_app(make_config())
        with TestClient(app) as client:
            with client.websocket_connect("/ws/owner") as ws:
                connected = ws.receive_json()
                self.assertEqual(connected["type"], "connected")
                ws.send_json({"type": "text", "text": "hi there, friend"})
                while True:
                    frame = ws.receive_json()
                    if frame.get("type") in ("done", "error"):
                        break
        keys = set(fake.store) | set(fake.strings)
        self.assertNotIn("core:external_profile:owner:discord:1", keys)
        # No LLM analysis call consumed the store either.
        self.assertEqual(keys, {"core:history:owner:companion"})


if __name__ == "__main__":
    unittest.main()
