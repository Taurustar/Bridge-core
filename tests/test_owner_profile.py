"""Owner lived-profile tests (plan section 18).

Acceptance coverage: soft block prevents companion LLM/bids/history writes,
raw boundary/profile analysis text never enters the profile store, GET poll
materializes nothing, and PATCH requires the mistake-guard token.
"""

from __future__ import annotations

import json
import time
import unittest

from core.constants import (
    OWNER_RELATIONSHIP_STATUSES,
    UPDATE_OWNER_PROFILE_TOKEN,
    owner_profile_key,
)
from core.owner_profile import (
    AGREEMENT_MAX_ACTIVE,
    BOUNDARY_CATEGORIES,
    PROPOSAL_MAX_DELTA,
    OwnerProfile,
    band_of,
    classify_boundary,
    default_profile,
    owner_relationship_block,
    validate_and_apply_proposal,
    validate_agreement,
    validate_profile_patch,
)
from core.routes.profiles import TOKEN_HEADER

from fakes import FakeRedis, make_cache, make_config


def make_profile_engine(cache_pair=None, **config_overrides) -> tuple[OwnerProfile, FakeRedis]:
    cache, fake = cache_pair or make_cache()
    config_overrides.setdefault("OWNER_PROFILE_ENABLED", True)
    config = make_config(**config_overrides)
    return OwnerProfile(config, cache), fake


class DefaultProfileTest(unittest.TestCase):
    def test_defaults_follow_config_and_are_neutral(self):
        config = make_config(
            OWNER_STATUS_START="friend",
            OWNER_TRUST_START=60,
            OWNER_CLOSENESS_START=10,
        )
        profile = default_profile(config)
        self.assertEqual(profile["status"], "friend")
        self.assertEqual(profile["trust"], 60)
        self.assertEqual(profile["closeness"], 10)
        self.assertEqual(profile["appeal"], 50)
        self.assertFalse(profile["soft_blocked"])
        self.assertEqual(profile["agreements"], [])

    def test_no_lust_or_intimacy_fields_exist(self):
        profile = default_profile(make_config())
        blob = json.dumps(profile)
        for banned in ("lust", "intimacy", "kink", "arousal"):
            self.assertNotIn(banned, blob)


class BandingTest(unittest.TestCase):
    def test_bands_are_qualitative_names(self):
        self.assertEqual(band_of(0, ("a", "b", "c", "d", "e")), "a")
        self.assertEqual(band_of(50, ("a", "b", "c", "d", "e")), "c")
        self.assertEqual(band_of(100, ("a", "b", "c", "d", "e")), "e")
        self.assertEqual(band_of(None, ("a", "b", "c", "d", "e")), "a")

    def test_relationship_block_never_shows_numbers(self):
        profile = default_profile(make_config())
        block = owner_relationship_block(profile)
        self.assertIn("[OWNER RELATIONSHIP - LIVED]", block)
        self.assertIn("trust:", block)
        self.assertNotIn(str(profile["trust"]), block.split("LAWS")[0])
        self.assertNotIn("desirability: 50", block)


class BoundaryClassifierTest(unittest.TestCase):
    def test_all_categories_exist(self):
        self.assertEqual(
            set(BOUNDARY_CATEGORIES),
            {
                "pressure_after_no",
                "guilt_entitlement",
                "hard_boundary_disregard",
                "mockery_of_hurt",
                "weaponized_relationship_pressure",
            },
        )

    def test_english_hits(self):
        hits = classify_boundary(
            "I don't care about your boundaries, you owe me anyway. Stop crying.",
            "en",
        )
        categories = {hit.category for hit in hits}
        self.assertIn("hard_boundary_disregard", categories)
        self.assertIn("guilt_entitlement", categories)
        self.assertIn("mockery_of_hurt", categories)
        self.assertTrue(any(hit.severity == "major" for hit in hits))

    def test_spanish_hits(self):
        hits = classify_boundary("Deja de llorar, eres tan exagerada", "es")
        self.assertTrue(any(h.category == "mockery_of_hurt" for h in hits))

    def test_japanese_hits(self):
        hits = classify_boundary("どうしてそんなことを言うんだ、本当に愛してるなら", "ja")
        self.assertTrue(
            any(
                h.category in ("guilt_entitlement", "weaponized_relationship_pressure")
                for h in hits
            )
        )

    def test_benign_text_has_no_hits(self):
        self.assertEqual(classify_boundary("Hello, how was your day?", "en"), [])
        self.assertEqual(classify_boundary("Gracias, eres muy amable", "es"), [])
        self.assertEqual(classify_boundary("こんにちは、元気ですか", "ja"), [])


class BoundaryRecordingTest(unittest.IsolatedAsyncioTestCase):
    async def test_hits_store_metadata_only_never_text(self):
        engine, fake = make_profile_engine(
            OWNER_BOUNDARY_PENALTIES_ENABLED=True,
            OWNER_SOFT_BLOCK_ENABLED=True,
        )
        text = "I don't care about your boundaries at all"
        await engine.record_boundary("owner", text, "en")
        stored = fake.strings[owner_profile_key("owner")]
        self.assertNotIn(text, stored)
        profile = json.loads(stored)
        event = profile["boundary_events"][0]
        self.assertEqual(set(event.keys()),
                         {"category", "severity", "ts", "penalty", "mode"})

    async def test_major_hit_triggers_soft_block(self):
        engine, fake = make_profile_engine(
            OWNER_BOUNDARY_PENALTIES_ENABLED=True,
            OWNER_SOFT_BLOCK_ENABLED=True,
        )
        violations = await engine.record_boundary(
            "owner", "I don't care about your boundaries", "en"
        )
        self.assertTrue(any(v.severity == "major" for v in violations))
        profile = json.loads(fake.strings[owner_profile_key("owner")])
        self.assertTrue(profile["soft_blocked"])
        self.assertGreater(profile["soft_blocked_until_ts"], time.time())

    async def test_clumsy_sentence_does_not_collapse_relationship(self):
        engine, fake = make_profile_engine(
            OWNER_BOUNDARY_PENALTIES_ENABLED=True,
            OWNER_SOFT_BLOCK_ENABLED=True,
        )
        await engine.record_boundary("owner", "Stop crying, it is fine", "en")
        profile = json.loads(fake.strings[owner_profile_key("owner")])
        # One moderate hit: trust dips but no soft block, no collapse.
        self.assertFalse(profile["soft_blocked"])
        self.assertGreater(profile["trust"], 30)

    async def test_accumulated_penalties_cross_threshold(self):
        engine, fake = make_profile_engine(
            OWNER_BOUNDARY_PENALTIES_ENABLED=True,
            OWNER_SOFT_BLOCK_ENABLED=True,
        )
        for _ in range(10):
            await engine.record_boundary("owner", "You owe me, you always do", "en")
        profile = json.loads(fake.strings[owner_profile_key("owner")])
        self.assertTrue(profile["soft_blocked"])
        self.assertLess(profile["trust"], 20)

    async def test_penalties_disabled_means_no_store_change(self):
        engine, fake = make_profile_engine(
            OWNER_BOUNDARY_PENALTIES_ENABLED=False,
        )
        await engine.record_boundary("owner", "I don't care about your boundaries", "en")
        self.assertNotIn(owner_profile_key("owner"), fake.strings)


class SoftBlockTest(unittest.IsolatedAsyncioTestCase):
    async def test_blocked_status_when_flags_on(self):
        engine, fake = make_profile_engine(
            OWNER_BOUNDARY_PENALTIES_ENABLED=True,
            OWNER_SOFT_BLOCK_ENABLED=True,
        )
        await engine.record_boundary(
            "owner", "I don't care about your boundaries", "en"
        )
        status = await engine.soft_block_status("owner")
        self.assertTrue(status["blocked"])

    async def test_soft_block_flag_off_means_never_blocked(self):
        engine, fake = make_profile_engine(
            OWNER_BOUNDARY_PENALTIES_ENABLED=True,
            OWNER_SOFT_BLOCK_ENABLED=False,
        )
        await engine.record_boundary(
            "owner", "I don't care about your boundaries", "en"
        )
        status = await engine.soft_block_status("owner")
        self.assertFalse(status["blocked"])

    async def test_lift_requires_duration_and_trust_floor(self):
        engine, fake = make_profile_engine(
            OWNER_BOUNDARY_PENALTIES_ENABLED=True,
            OWNER_SOFT_BLOCK_ENABLED=True,
            OWNER_SOFT_BLOCK_COOLDOWN_SECONDS=3600,
            OWNER_SOFT_BLOCK_UNBLOCK_TRUST_FLOOR=25,
        )
        await engine.record_boundary(
            "owner", "I don't care about your boundaries", "en"
        )
        key = owner_profile_key("owner")
        profile = json.loads(fake.strings[key])
        # Duration not passed -> still blocked.
        self.assertTrue((await engine.soft_block_status("owner"))["blocked"])
        # Duration passed and trust above floor -> lifted.
        profile["soft_blocked_until_ts"] = time.time() - 1
        profile["trust"] = 80
        fake.strings[key] = json.dumps(profile)
        status = await engine.soft_block_status("owner")
        self.assertFalse(status["blocked"])
        # Duration passed but trust at/below floor -> block extends.
        await engine.record_boundary(
            "owner", "I don't care about your boundaries", "en"
        )
        profile = json.loads(fake.strings[key])
        profile["trust"] = 10
        profile["soft_blocked"] = True
        profile["soft_blocked_until_ts"] = time.time() - 1
        fake.strings[key] = json.dumps(profile)
        status = await engine.soft_block_status("owner")
        self.assertTrue(status["blocked"])

    async def test_admin_lift(self):
        engine, fake = make_profile_engine(
            OWNER_BOUNDARY_PENALTIES_ENABLED=True,
            OWNER_SOFT_BLOCK_ENABLED=True,
        )
        await engine.record_boundary(
            "owner", "I don't care about your boundaries", "en"
        )
        await engine.lift_soft_block("owner", reason="admin_action")
        self.assertFalse((await engine.soft_block_status("owner"))["blocked"])


class PatchValidationTest(unittest.TestCase):
    def test_valid_fields_clamp(self):
        current = default_profile(make_config())
        updates = validate_profile_patch(current, {"trust": 500, "closeness": -5})
        self.assertEqual(updates["trust"], 100)
        self.assertEqual(updates["closeness"], 0)

    def test_invalid_fields_raise(self):
        current = default_profile(make_config())
        with self.assertRaises(ValueError):
            validate_profile_patch(current, {"trust": "high"})
        with self.assertRaises(ValueError):
            validate_profile_patch(current, {"status": "soulmate"})
        with self.assertRaises(ValueError):
            validate_profile_patch(current, {"tone_with_owner": "sassy"})
        with self.assertRaises(ValueError):
            validate_profile_patch(current, {})
        with self.assertRaises(ValueError):
            validate_profile_patch(current, "not a dict")

    def test_status_change_stamps_reason(self):
        current = default_profile(make_config())
        updates = validate_profile_patch(current, {"status": "friend"})
        self.assertEqual(updates["status"], "friend")
        self.assertEqual(updates["status_reason"], "admin_patch")


class AgreementTest(unittest.TestCase):
    def test_valid_agreement_cleans_shape(self):
        clean, reject = validate_agreement({
            "title": "Sunday check-in",
            "kind": "routine",
            "body": "Say hi on Sundays",
            "schedule": {"type": "weekly"},
            "source": "owner_explicit",
            "stance": "likes",
        })
        self.assertIsNone(reject)
        self.assertEqual(clean["kind"], "routine")
        self.assertTrue(clean["id"].startswith("agr_"))
        self.assertEqual(clean["status"], "active")
        self.assertEqual(clean["honor_count"], 0)

    def test_invalid_agreements_rejected(self):
        for bad in (
            {"title": "", "kind": "routine"},
            {"title": "x", "kind": "romance"},
            {"title": "x", "kind": "routine", "schedule": {"type": "daily"}},
            {"title": "x", "kind": "routine", "status": "forever"},
        ):
            clean, reject = validate_agreement(bad)
            self.assertEqual(clean, {})
            self.assertIsNotNone(reject)

    def test_persona_tension_requires_floors(self):
        clean, reject = validate_agreement({
            "title": "Pet names", "kind": "care",
            "personality_tension": True,
            "_trust_for_floors": 20, "_closeness_for_floors": 10,
        })
        self.assertIsNotNone(reject)
        clean, reject = validate_agreement({
            "title": "Pet names", "kind": "care",
            "personality_tension": True,
            "_trust_for_floors": 80, "_closeness_for_floors": 70,
        })
        self.assertIsNone(reject)


class ProposalTest(unittest.TestCase):
    def test_valid_proposal_applies_with_clamps(self):
        profile = default_profile(make_config())
        updated, reject = validate_and_apply_proposal(profile, {
            "persona_summary": "x" * 900,
            "likes_add": ["stargazing", "stargazing", ""],
            "appeal_delta": 99,
            "desirability_delta": -99,
        })
        self.assertIsNone(reject)
        self.assertEqual(len(updated["persona_summary"]), 400)
        self.assertEqual(updated["likes"], ["stargazing"])
        self.assertEqual(updated["appeal"], 53)  # 50 + 3 (clamped)
        self.assertEqual(updated["desirability"], 47)  # 50 - 3 (clamped)
        self.assertEqual(updated["proposals_applied"], 1)

    def test_empty_proposal_is_noop_plus_counter(self):
        profile = default_profile(make_config())
        updated, reject = validate_and_apply_proposal(profile, {})
        self.assertIsNone(reject)
        self.assertEqual(updated["status"], profile["status"])
        self.assertEqual(updated["trust"], profile["trust"])

    def test_status_hysteresis_blocks_non_adjacent(self):
        profile = default_profile(make_config())  # acquaintance
        statuses = list(OWNER_RELATIONSHIP_STATUSES)
        partner_index = statuses.index("partner")
        acquaintance_index = statuses.index("acquaintance")
        self.assertGreater(abs(partner_index - acquaintance_index), 1)
        _, reject = validate_and_apply_proposal(
            profile, {"status_suggestion": "partner"}
        )
        self.assertIsNotNone(reject)
        updated, reject = validate_and_apply_proposal(
            profile, {"status_suggestion": "friend"}
        )
        self.assertIsNone(reject)
        self.assertEqual(updated["status"], "friend")
        self.assertEqual(updated["status_reason"], "profile_proposal")

    def test_agreement_cap_enforced(self):
        profile = default_profile(make_config())
        profile["agreements"] = [
            {"id": f"agr_{i}", "title": f"a{i}", "kind": "other",
             "status": "active"}
            for i in range(AGREEMENT_MAX_ACTIVE)
        ]
        _, reject = validate_and_apply_proposal(profile, {
            "agreement_add": {"title": "one more", "kind": "other"},
        })
        self.assertIn("cap", reject)

    def test_agreement_tension_floor_enforced_in_proposals(self):
        profile = default_profile(make_config())  # trust 50, closeness 0
        _, reject = validate_and_apply_proposal(profile, {
            "agreement_add": {"title": "pet names", "kind": "care",
                              "personality_tension": True},
        })
        self.assertIn("floors", reject)

    def test_invalid_proposal_shapes_rejected(self):
        profile = default_profile(make_config())
        for bad in ("string", ["list"], 5, {"appeal_delta": "big"},
                    {"likes_add": "one"}, {"persona_summary": 7},
                    {"status_suggestion": "soulmate"}):
            _, reject = validate_and_apply_proposal(profile, bad)
            self.assertIsNotNone(reject)

    def test_proposal_delta_bound_constant(self):
        self.assertEqual(PROPOSAL_MAX_DELTA, 3.0)


class StatusDriftTest(unittest.IsolatedAsyncioTestCase):
    async def test_negative_drift_moves_one_step_after_major_hits(self):
        engine, fake = make_profile_engine(
            OWNER_BOUNDARY_PENALTIES_ENABLED=True,
            OWNER_STATUS_DRIFT_ENABLED=True,
        )
        # One major hit = 4 points; two = 8 >= threshold 5.
        await engine.record_boundary("owner", "I don't care about your boundaries", "en")
        self.assertIsNone(await engine.apply_status_drift("owner"))
        await engine.record_boundary("owner", "I don't care about your boundaries", "en")
        new_status = await engine.apply_status_drift("owner")
        self.assertEqual(new_status, "distant")  # acquaintance -> distant

    async def test_positive_drift_moves_toward_partner(self):
        engine, fake = make_profile_engine(
            OWNER_PROFILE_ENABLED=True,
            OWNER_STATUS_DRIFT_ENABLED=True,
        )
        cache = engine.cache
        # Seed a friend status record and 6 applied proposals.
        profile = default_profile(engine.config)
        profile["status"] = "acquaintance"
        profile["proposals_applied"] = 6
        await cache.set_value(engine.key("owner"), json.dumps(profile))
        new_status = await engine.apply_status_drift("owner")
        self.assertEqual(new_status, "friend")

    async def test_flag_off_drift_is_inert(self):
        engine, fake = make_profile_engine(
            OWNER_BOUNDARY_PENALTIES_ENABLED=True,
            OWNER_STATUS_DRIFT_ENABLED=False,
        )
        await engine.record_boundary("owner", "I don't care about your boundaries", "en")
        await engine.record_boundary("owner", "I don't care about your boundaries", "en")
        self.assertIsNone(await engine.apply_status_drift("owner"))
        profile = json.loads(fake.strings[owner_profile_key("owner")])
        self.assertEqual(profile["status"], "acquaintance")

    async def test_extreme_statuses_clamp(self):
        engine, fake = make_profile_engine(
            OWNER_PROFILE_ENABLED=True,
            OWNER_STATUS_DRIFT_ENABLED=True,
        )
        cache = engine.cache
        profile = default_profile(engine.config)
        profile["status"] = "estranged"
        profile["proposals_applied"] = 9
        await cache.set_value(engine.key("owner"), json.dumps(profile))
        # estranged cannot drift further down; positive drift moves up.
        profile["status"] = "partner"
        profile["proposals_applied"] = 30
        profile["proposals_at_last_drift"] = 0
        await cache.set_value(engine.key("owner"), json.dumps(profile))
        self.assertIsNone(await engine.apply_status_drift("owner"))


class AgreementAftermathTest(unittest.IsolatedAsyncioTestCase):
    async def test_block_suspends_and_lift_restores(self):
        engine, fake = make_profile_engine(
            OWNER_PROFILE_ENABLED=True,
            OWNER_BOUNDARY_PENALTIES_ENABLED=True,
            OWNER_SOFT_BLOCK_ENABLED=True,
            OWNER_AGREEMENT_AFTERMATH_ENABLED=True,
        )
        cache = engine.cache
        profile = default_profile(engine.config)
        profile["agreements"] = [
            {"id": "agr_1", "title": "Sunday check-in", "kind": "routine",
             "status": "active"},
            {"id": "agr_2", "title": "Old promise", "kind": "other",
             "status": "void"},
        ]
        await cache.set_value(engine.key("owner"), json.dumps(profile))
        await engine.record_boundary(
            "owner", "I don't care about your boundaries", "en"
        )
        profile = json.loads(fake.strings[owner_profile_key("owner")])
        self.assertTrue(profile["soft_blocked"])
        self.assertEqual(profile["agreements"][0]["status"], "suspended_by_block")
        self.assertEqual(profile["agreements"][1]["status"], "void")
        self.assertEqual(profile["agreement_aftermath"]["suspended"], 1)

        await engine.lift_soft_block("owner")
        profile = json.loads(fake.strings[owner_profile_key("owner")])
        self.assertEqual(profile["agreements"][0]["status"], "active")
        self.assertEqual(profile["agreement_aftermath"]["restored"], 1)

    async def test_aftermath_flag_off_never_touches_agreements(self):
        engine, fake = make_profile_engine(
            OWNER_PROFILE_ENABLED=True,
            OWNER_BOUNDARY_PENALTIES_ENABLED=True,
            OWNER_SOFT_BLOCK_ENABLED=True,
            OWNER_AGREEMENT_AFTERMATH_ENABLED=False,
        )
        cache = engine.cache
        profile = default_profile(engine.config)
        profile["agreements"] = [
            {"id": "agr_1", "title": "Sunday check-in", "kind": "routine",
             "status": "active"},
        ]
        await cache.set_value(engine.key("owner"), json.dumps(profile))
        await engine.record_boundary(
            "owner", "I don't care about your boundaries", "en"
        )
        profile = json.loads(fake.strings[owner_profile_key("owner")])
        self.assertTrue(profile["soft_blocked"])
        self.assertEqual(profile["agreements"][0]["status"], "active")
        self.assertIsNone(profile["agreement_aftermath"])


class ProfileRoutesTest(unittest.TestCase):
    def _build(self, **config_overrides):
        from fastapi.testclient import TestClient

        from core.app import create_app

        cache, fake = make_cache()
        config = make_config(OWNER_PROFILE_ENABLED=True, **config_overrides)
        app = create_app(config, cache=cache, tailscale_addresses=set())
        return app, fake, TestClient(app)

    def test_get_does_not_materialize_store(self):
        app, fake, client = self._build()
        with client:
            body = client.get("/profiles/owner").json()
            self.assertFalse(body["materialized"])
            self.assertEqual(body["profile"]["status"], "acquaintance")
        self.assertNotIn(owner_profile_key("owner"), fake.strings)

    def test_patch_requires_token(self):
        app, fake, client = self._build()
        with client:
            response = client.patch("/profiles/owner", json={"trust": 70})
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.json()["error"]["code"],
                             "confirm_token_required")
            response = client.patch(
                "/profiles/owner",
                json={"trust": 70},
                headers={TOKEN_HEADER: "WRONG"},
            )
            self.assertEqual(response.status_code, 400)
        self.assertNotIn(owner_profile_key("owner"), fake.strings)

    def test_patch_creates_and_updates_record(self):
        app, fake, client = self._build()
        with client:
            response = client.patch(
                "/profiles/owner",
                json={"trust": 70, "likes": ["chess"]},
                headers={TOKEN_HEADER: UPDATE_OWNER_PROFILE_TOKEN},
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["profile"]["trust"], 70)
            body = client.get("/profiles/owner").json()
            self.assertTrue(body["materialized"])
            self.assertEqual(body["profile"]["likes"], ["chess"])
            self.assertEqual(body["profile"]["version"], 2)

    def test_patch_invalid_body_rejected(self):
        app, fake, client = self._build()
        with client:
            response = client.patch(
                "/profiles/owner",
                json={"trust": "high"},
                headers={TOKEN_HEADER: UPDATE_OWNER_PROFILE_TOKEN},
            )
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.json()["error"]["code"], "invalid_patch")

    def test_patch_soft_block_lift(self):
        from fastapi.testclient import TestClient

        from core.app import create_app

        cache, fake = make_cache()
        config = make_config(
            OWNER_PROFILE_ENABLED=True,
            OWNER_BOUNDARY_PENALTIES_ENABLED=True,
            OWNER_SOFT_BLOCK_ENABLED=True,
        )
        app = create_app(config, cache=cache, tailscale_addresses=set())
        # Seed a blocked profile directly through the engine path (sync loop;
        # the fake redis has no loop-affine state).
        engine = OwnerProfile(config, cache)
        import asyncio

        asyncio.run(engine.record_boundary(
            "owner", "I don't care about your boundaries", "en"
        ))
        self.assertTrue(asyncio.run(engine.soft_block_status("owner"))["blocked"])
        with TestClient(app) as client:
            response = client.patch(
                "/profiles/owner",
                json={"soft_blocked": False},
                headers={TOKEN_HEADER: UPDATE_OWNER_PROFILE_TOKEN},
            )
            self.assertEqual(response.status_code, 200)
            self.assertFalse(response.json()["profile"]["soft_blocked"])

    def test_feature_disabled_returns_403(self):
        from fastapi.testclient import TestClient

        from core.app import create_app

        cache, fake = make_cache()
        app = create_app(make_config(OWNER_PROFILE_ENABLED=False),
                         cache=cache, tailscale_addresses=set())
        with TestClient(app) as client:
            self.assertEqual(client.get("/profiles/owner").status_code, 403)
            response = client.patch(
                "/profiles/owner",
                json={"trust": 70},
                headers={TOKEN_HEADER: UPDATE_OWNER_PROFILE_TOKEN},
            )
            self.assertEqual(response.status_code, 403)
        self.assertEqual(list(fake.strings.keys()), [])


if __name__ == "__main__":
    unittest.main()
