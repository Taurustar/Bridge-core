"""Three-tier memory store tests (plan sections 20.3, 20.4.1, 20.5)."""

from __future__ import annotations

import asyncio
import unittest

from core.cache import RedisCache
from core.memory import (
    ChromaDeleteError,
    MemoryBackend,
    PinnedMemoryError,
    normalize_text,
    token_overlap,
)

from fakes import FakeChroma, FakeRedis, make_cache, make_config


def make_backend(**overrides) -> tuple[MemoryBackend, FakeRedis]:
    cache, fake = make_cache()
    config = make_config(**overrides)
    return MemoryBackend(config, cache), fake


def life_record(backend: MemoryBackend, text: str, **kwargs) -> dict:
    fields = {
        "kind": "character_life_event",
        "text": text,
        "source": "life_engine",
        "source_mode": "life",
        "importance": 0.4,
        "metadata": {"day": "2026-09-03"},
    }
    fields.update(kwargs)
    return backend.make_record(**fields)


class TokenOverlapTest(unittest.TestCase):
    def test_identical_text_is_one(self):
        self.assertEqual(token_overlap("Fixed the lamp", "fixed the lamp"), 1.0)

    def test_disjoint_text_is_zero(self):
        self.assertEqual(token_overlap("cats sleep a lot", "rust compiles"), 0.0)

    def test_partial_overlap_between(self):
        score = token_overlap("owner likes green tea", "owner drinks green tea daily")
        self.assertGreater(score, 0.0)
        self.assertLess(score, 1.0)

    def test_normalize_collapses_whitespace(self):
        self.assertEqual(normalize_text("  a\n\tb  "), "a b")


class StoreBasicsTest(unittest.TestCase):
    def test_add_and_read_roundtrip(self):
        backend, _ = make_backend()

        async def run():
            record = life_record(backend, "Fixed the balcony lamp.")
            await backend.add("owner", record)
            rows = await backend.records("owner")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["text"], "Fixed the balcony lamp.")
            self.assertEqual(await backend.count("owner"), 1)
            self.assertIsNotNone(await backend.get("owner", record["id"]))
            self.assertIsNone(await backend.get("owner", "mem_missing"))

        asyncio.run(run())

    def test_kind_filter_and_limit(self):
        backend, _ = make_backend()

        async def run():
            await backend.add("owner", life_record(backend, "one"))
            await backend.add(
                "owner",
                backend.make_record(
                    kind="commitment", text="two", source="admin", source_mode="admin"
                ),
            )
            self.assertEqual(len(await backend.records("owner", kind="commitment")), 1)
            self.assertEqual(len(await backend.records("owner", limit=1)), 1)

        asyncio.run(run())

    def test_unknown_kind_rejected(self):
        backend, _ = make_backend()
        with self.assertRaises(ValueError):
            backend.make_record(kind="gossip", text="x", source="s", source_mode="m")

    def test_make_record_defaults_to_current_timestamp(self):
        backend, _ = make_backend()
        record = life_record(backend, "timestamped")
        self.assertGreater(record["created_ts"], 0.0)
        self.assertEqual(record["updated_ts"], record["created_ts"])


class MergeTest(unittest.TestCase):
    def test_exact_text_merges_into_existing_row(self):
        backend, _ = make_backend()

        async def run():
            first = life_record(backend, "Owner prefers tea.", created_ts=100.0)
            await backend.add("owner", first)
            proposal = life_record(
                backend,
                "owner prefers tea",
                importance=0.9,
                source_ids=["msg_1"],
            )
            stored, merged = await backend.upsert_merged(
                "owner", proposal, now_ts=200.0
            )
            self.assertTrue(merged)
            self.assertEqual(stored["id"], first["id"])
            self.assertEqual(stored["created_ts"], 100.0)
            self.assertEqual(stored["updated_ts"], 200.0)
            self.assertEqual(stored["importance"], 0.9)
            self.assertIn("msg_1", stored["metadata"]["source_ids"])
            self.assertEqual(await backend.count("owner"), 1)

        asyncio.run(run())

    def test_near_duplicate_merges(self):
        backend, _ = make_backend()

        async def run():
            await backend.add(
                "owner", life_record(backend, "The owner studies rust on weekends")
            )
            proposal = life_record(
                backend, "The owner studies rust on weekends often"
            )
            _, merged = await backend.upsert_merged("owner", proposal, now_ts=5.0)
            self.assertTrue(merged)
            self.assertEqual(await backend.count("owner"), 1)

        asyncio.run(run())

    def test_distinct_fact_appends(self):
        backend, _ = make_backend()

        async def run():
            await backend.add("owner", life_record(backend, "Owner likes tea."))
            _, merged = await backend.upsert_merged(
                "owner", life_record(backend, "Owner lives in Santiago."), now_ts=1.0
            )
            self.assertFalse(merged)
            self.assertEqual(await backend.count("owner"), 2)

        asyncio.run(run())

    def test_chroma_semantic_candidate_merges_despite_low_token_overlap(self):
        backend, _ = make_backend(CHROMA_ENABLED=True)
        chroma = FakeChroma()
        chroma.start()
        backend.chroma = chroma

        async def run():
            first = life_record(backend, "The owner enjoys tending houseplants")
            await backend.add("owner", first)
            chroma.semantic_candidates = [(first["id"], 0.91)]
            stored, merged = await backend.upsert_merged(
                "owner", life_record(backend, "Indoor gardening is a favorite hobby")
            )
            self.assertTrue(merged)
            self.assertEqual(stored["id"], first["id"])
            self.assertEqual(await backend.count("owner"), 1)

        asyncio.run(run())

    def test_chroma_candidate_below_threshold_does_not_merge(self):
        backend, _ = make_backend(CHROMA_ENABLED=True)
        chroma = FakeChroma()
        chroma.start()
        backend.chroma = chroma

        async def run():
            first = life_record(backend, "The owner studies rust on weekends")
            await backend.add("owner", first)
            chroma.semantic_candidates = [(first["id"], 0.4)]
            _, merged = await backend.upsert_merged(
                "owner", life_record(backend, "The owner studies rust on weekends often")
            )
            self.assertFalse(merged)
            self.assertEqual(await backend.count("owner"), 2)

        asyncio.run(run())


class BoundsAndPinsTest(unittest.TestCase):
    def test_overflow_drops_oldest_unpinned(self):
        backend, _ = make_backend(MEMORY_MAX_PER_USER=3)

        async def run():
            for index, text in enumerate(["a", "b", "c"]):
                record = life_record(backend, text, created_ts=float(index))
                record["updated_ts"] = float(index)
                await backend.add("owner", record)
            pinned = life_record(backend, "pinned-fact", created_ts=0.0)
            pinned["pinned"] = True
            pinned["updated_ts"] = 0.0
            await backend.add("owner", pinned)
            await backend.add(
                "owner", life_record(backend, "d", created_ts=99.0, )
            )
            texts = sorted(row["text"] for row in await backend.records("owner"))
            self.assertEqual(texts, ["c", "d", "pinned-fact"])

        asyncio.run(run())

    def test_overflow_removes_evicted_row_from_chroma(self):
        backend, _ = make_backend(CHROMA_ENABLED=True, MEMORY_MAX_PER_USER=2)
        chroma = FakeChroma()
        chroma.start()
        backend.chroma = chroma

        async def run():
            rows = []
            for index in range(3):
                row = life_record(backend, f"row {index}", created_ts=float(index))
                row["updated_ts"] = float(index)
                rows.append(row)
                await backend.add("owner", row)
            self.assertNotIn(rows[0]["id"], chroma.indexed)
            self.assertEqual(set(chroma.indexed), {rows[1]["id"], rows[2]["id"]})

        asyncio.run(run())

    def test_overflow_chroma_failure_preserves_redis_rows(self):
        backend, _ = make_backend(CHROMA_ENABLED=True, MEMORY_MAX_PER_USER=1)
        chroma = FakeChroma()
        chroma.start()
        backend.chroma = chroma

        async def run():
            first = life_record(backend, "first", created_ts=1.0)
            await backend.add("owner", first)
            chroma.fail = True
            with self.assertRaises(ChromaDeleteError):
                await backend.add("owner", life_record(backend, "second", created_ts=2.0))
            self.assertEqual(
                [row["id"] for row in await backend.records("owner")],
                [first["id"]],
            )

        asyncio.run(run())

    def test_delete_refuses_pinned(self):
        backend, _ = make_backend()

        async def run():
            record = life_record(backend, "keep me")
            record["pinned"] = True
            await backend.add("owner", record)
            with self.assertRaises(PinnedMemoryError):
                await backend.delete("owner", record["id"])
            self.assertIsNone(await backend.delete("owner", "mem_missing"))
            await backend.patch("owner", record["id"], pinned=False)
            removed = await backend.delete("owner", record["id"])
            self.assertEqual(removed["id"], record["id"])

        asyncio.run(run())

    def test_patch_updates_row(self):
        backend, _ = make_backend()

        async def run():
            record = life_record(backend, "original")
            await backend.add("owner", record)
            updated = await backend.patch(
                "owner", record["id"], text="edited", importance=0.8
            )
            self.assertEqual(updated["text"], "edited")
            self.assertEqual(updated["importance"], 0.8)
            row = await backend.get("owner", record["id"])
            self.assertEqual(row["text"], "edited")

        asyncio.run(run())


class CleanupTest(unittest.TestCase):
    def test_expired_conversation_rows_are_candidates(self):
        backend, _ = make_backend(MEMORY_CONVERSATION_TTL_DAYS=30)
        now = 1_000_000.0
        rows = [
            backend.make_record(
                kind="conversation", text="old chat", source="s",
                source_mode="companion", created_ts=1.0,
            ),
        ]
        rows[0]["updated_ts"] = now - 40 * 86400.0
        candidates = backend.redis.cleanup_candidates(rows, now)
        self.assertEqual(len(candidates), 1)

    def test_pinned_and_protected_survive_cleanup(self):
        backend, _ = make_backend(MEMORY_CONVERSATION_TTL_DAYS=30)
        now = 1_000_000.0

        async def run():
            pinned = backend.make_record(
                kind="conversation", text="pinned", source="s", source_mode="companion"
            )
            pinned["pinned"] = True
            pinned["updated_ts"] = now - 400 * 86400.0
            profile = backend.make_record(
                kind="user_profile", text="pref", source="s", source_mode="companion"
            )
            profile["updated_ts"] = now - 400 * 86400.0
            await backend.add("owner", pinned)
            await backend.add("owner", profile)
            result = await backend.cleanup("owner", dry_run=False)
            self.assertEqual(result["deleted"], [])
            self.assertEqual(await backend.count("owner"), 2)

        asyncio.run(run())

    def test_life_rows_decay_slower_than_conversation(self):
        backend, _ = make_backend(
            MEMORY_CONVERSATION_TTL_DAYS=30, MEMORY_LIFE_TTL_DAYS=365
        )
        now = 1_000_000.0
        conv = backend.make_record(
            kind="conversation", text="c", source="s", source_mode="companion"
        )
        conv["updated_ts"] = now - 60 * 86400.0
        life = backend.make_record(
            kind="character_life_event", text="l", source="s", source_mode="life"
        )
        life["updated_ts"] = now - 60 * 86400.0
        candidates = backend.redis.cleanup_candidates([conv, life], now)
        self.assertEqual([row["text"] for row in candidates], ["c"])

    def test_dry_run_does_not_delete(self):
        backend, _ = make_backend(MEMORY_CONVERSATION_TTL_DAYS=30)
        now = 1_000_000.0

        async def run():
            row = backend.make_record(
                kind="conversation", text="old", source="s", source_mode="companion"
            )
            row["updated_ts"] = now - 100 * 86400.0
            await backend.add("owner", row)
            result = await backend.cleanup("owner", dry_run=True)
            self.assertEqual(result["count"], 1)
            self.assertEqual(await backend.count("owner"), 1)

        asyncio.run(run())

    def test_cleanup_removes_rows_from_chroma_index(self):
        backend, _ = make_backend(MEMORY_CONVERSATION_TTL_DAYS=30)
        chroma = FakeChroma()
        chroma.start()
        backend.chroma = chroma
        now = 1_000_000.0

        async def run():
            row = backend.make_record(
                kind="conversation", text="old", source="s", source_mode="companion"
            )
            row["updated_ts"] = now - 100 * 86400.0
            await backend.add("owner", row)
            self.assertIn(row["id"], chroma.indexed)
            result = await backend.cleanup("owner", dry_run=False)
            self.assertEqual(result["deleted"], [row["id"]])
            self.assertNotIn(row["id"], chroma.indexed)

        asyncio.run(run())


class SearchTest(unittest.TestCase):
    def test_ranked_search(self):
        backend, _ = make_backend()

        async def run():
            await backend.add("owner", life_record(backend, "Owner drinks green tea daily"))
            await backend.add("owner", life_record(backend, "The cat knocked over a lamp"))
            rows = await backend.search("owner", "green tea", limit=2)
            self.assertEqual(rows[0]["text"], "Owner drinks green tea daily")

        asyncio.run(run())

    def test_empty_query_returns_nothing(self):
        backend, _ = make_backend()

        async def run():
            self.assertEqual(await backend.search("owner", "   "), [])

        asyncio.run(run())


class DegradedChromaTest(unittest.TestCase):
    def test_required_chroma_fails_startup_when_unavailable(self):
        backend, _ = make_backend(CHROMA_REQUIRED=True)
        backend.chroma = FakeChroma(fail=True)
        with self.assertRaisesRegex(RuntimeError, "required but unavailable"):
            backend.start()

    def test_reconciles_redis_rows_before_semantic_search(self):
        cache, _ = make_cache()
        config = make_config(CHROMA_ENABLED=True)
        backend = MemoryBackend(config, cache)
        chroma = FakeChroma()
        backend.chroma = chroma

        async def run():
            row = life_record(backend, "row written while index was unavailable")
            await backend.redis.add("owner", row)
            chroma.start()
            await backend.search("owner", "row written", limit=2)
            self.assertIn(row["id"], chroma.indexed)

        asyncio.run(run())

    def test_chroma_failure_degrades_without_breaking_redis(self):
        cache, fake = make_cache()
        config = make_config(CHROMA_ENABLED=True)
        backend = MemoryBackend(config, cache)
        backend.chroma = FakeChroma(fail=True)
        backend.chroma.start()
        self.assertFalse(backend.chroma.available)
        self.assertTrue(backend.degraded)
        self.assertEqual(backend.backend_name, "chroma_degraded_redis")

        async def run():
            record = life_record(backend, "durable row")
            saved = await backend.add("owner", record)
            self.assertEqual(saved["id"], record["id"])
            rows = await backend.records("owner")
            self.assertEqual(len(rows), 1)
            ranked = await backend.search("owner", "durable row")
            self.assertEqual(ranked[0]["id"], record["id"])

        asyncio.run(run())

    def test_healthy_chroma_index_stays_consistent(self):
        cache, fake = make_cache()
        config = make_config(CHROMA_ENABLED=True)
        backend = MemoryBackend(config, cache)
        backend.chroma = FakeChroma()
        backend.chroma.start()
        self.assertTrue(backend.chroma.available)

        async def run():
            record = life_record(backend, "indexed row")
            await backend.add("owner", record)
            self.assertEqual(backend.chroma.indexed[record["id"]]["owner"], "owner")
            await backend.delete("owner", record["id"])
            self.assertNotIn(record["id"], backend.chroma.indexed)

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
