"""Mid-term compaction, extraction, and session-close tests
(plan sections 20.2, 20.4.1, 20.6)."""

from __future__ import annotations

import asyncio
import json
import unittest

from core.cache import RedisCache
from core.bridge import Bridge
from core.constants import midterm_key
from core.history import companion_history_key, load_rows, make_row
from core.llm import LLMChainExhausted
from core.memory import MemoryBackend
from core.memory_tiers import MidTermMemory, fact_is_storable, parse_extraction

from fakes import FakeChroma, FakeLLM, FakeRedis, make_cache, make_config


def make_tiers(**overrides) -> tuple[MidTermMemory, MemoryBackend, FakeRedis, FakeLLM]:
    cache, fake = make_cache()
    settings = {
        "COMPANION_COMPACT_THRESHOLD": 6,
        "COMPANION_KEEP_RECENT": 2,
        "MEMORY_EXTRACTION_ENABLED": True,
    }
    settings.update(overrides)
    config = make_config(**settings)
    backend = MemoryBackend(config, cache)
    llm = FakeLLM()
    tiers = MidTermMemory(config, cache, backend, llm=llm)
    return tiers, backend, fake, llm


def seed_history(fake: FakeRedis, count: int, keep_prefix: str = "msg") -> list[dict]:
    rows = [
        make_row("user" if index % 2 == 0 else "assistant", f"{keep_prefix} {index}",
                 "delivered")
        for index in range(count)
    ]
    fake.store[companion_history_key("owner")] = [
        __import__("json").dumps(row) for row in rows
    ]
    return rows


class CompactionTest(unittest.TestCase):
    def test_threshold_gate(self):
        tiers, _, _, _ = make_tiers()
        self.assertTrue(tiers.compaction_needed(7))
        self.assertFalse(tiers.compaction_needed(6))
        tiers.config.COMPANION_COMPACT_THRESHOLD = 0
        self.assertFalse(tiers.compaction_needed(100))

    def test_threshold_counts_only_delivered_rows(self):
        tiers, _, _, _ = make_tiers()
        rows = [make_row("assistant", "pending", "pending") for _ in range(20)]
        rows.extend(make_row("user", "delivered", "delivered") for _ in range(6))
        self.assertFalse(tiers.compaction_needed(rows))

    def test_compaction_stores_chapter_and_replaces_history(self):
        tiers, backend, fake, llm = make_tiers()
        llm._replies.clear()
        llm._replies.append("The owner planned the week and tested the app.")
        rows = seed_history(fake, 8)

        async def run():
            return await tiers.compact("owner", rows, now_ts=1000.0)

        result = asyncio.run(run())
        self.assertTrue(result["compacted"])
        remaining = asyncio.run(load_rows(RedisCache(fake), "owner"))
        self.assertEqual(len(remaining), 2)  # COMPANION_KEEP_RECENT
        self.assertEqual(remaining[0]["text"], "msg 6")
        chapters = asyncio.run(tiers.recent_chapters("owner"))
        self.assertEqual(len(chapters), 1)
        self.assertEqual(len(chapters[0]["metadata"]["source_ids"]), 6)
        self.assertIn(
            midterm_key("owner"),
            fake.store,
            "chapters live in the dedicated midterm ring key",
        )

    def test_llm_failure_preserves_history(self):
        tiers, backend, fake, llm = make_tiers()
        llm._replies.clear()
        llm._replies.append(LLMChainExhausted("chain down"))
        rows = seed_history(fake, 8)

        async def run():
            return await tiers.compact("owner", rows, now_ts=1000.0)

        result = asyncio.run(run())
        self.assertFalse(result["compacted"])
        self.assertEqual(result["reason"], "chapter_failed")
        remaining = asyncio.run(load_rows(RedisCache(fake), "owner"))
        self.assertEqual(len(remaining), 8)  # history untouched

    def test_store_failure_preserves_history(self):
        tiers, backend, fake, llm = make_tiers()
        llm._replies.clear()
        llm._replies.append("a chapter")
        rows = seed_history(fake, 8)

        async def run():
            async def broken_store(owner, text, *, source_ids, now_ts):
                raise RuntimeError("store down")

            tiers.store_chapter = broken_store
            return await tiers.compact("owner", rows, now_ts=1000.0)

        result = asyncio.run(run())
        self.assertFalse(result["compacted"])
        remaining = asyncio.run(load_rows(RedisCache(fake), "owner"))
        self.assertEqual(len(remaining), 8)

    def test_compaction_excludes_and_preserves_non_delivered_rows(self):
        tiers, _, fake, llm = make_tiers(MEMORY_EXTRACTION_ENABLED=False)
        rows = seed_history(fake, 8)
        pending = make_row("assistant", "do not compact me", "pending")
        rows.insert(2, pending)
        fake.store[companion_history_key("owner")] = [json.dumps(row) for row in rows]
        llm._replies.clear()
        llm._replies.append("A delivered-only chapter.")

        result = asyncio.run(tiers.compact("owner", rows, now_ts=1000.0))

        self.assertTrue(result["compacted"])
        remaining = asyncio.run(load_rows(RedisCache(fake), "owner"))
        self.assertEqual([row["id"] for row in remaining], [pending["id"], rows[-2]["id"], rows[-1]["id"]])
        prompt = json.dumps(llm.calls[0][1])
        self.assertNotIn("do not compact me", prompt)

    def test_recent_chapters_newest_first(self):
        tiers, backend, fake, llm = make_tiers()

        async def run():
            for index in range(3):
                await tiers.store_chapter(
                    "owner",
                    f"chapter {index}",
                    source_ids=["msg_x"],
                    now_ts=float(index),
                )
            chapters = await tiers.recent_chapters("owner")
            self.assertEqual(
                [c["text"] for c in chapters], ["chapter 2", "chapter 1", "chapter 0"]
            )
            capped = await tiers.recent_chapters("owner", limit=1)
            self.assertEqual(len(capped), 1)

        asyncio.run(run())

    def test_midterm_eviction_removes_chapter_from_chroma(self):
        from unittest.mock import patch

        tiers, backend, _, _ = make_tiers(CHROMA_ENABLED=True)
        chroma = FakeChroma()
        chroma.start()
        backend.chroma = chroma

        async def run():
            with patch("core.memory_tiers.MIDTERM_MAX_CHAPTERS", 2):
                first = await tiers.store_chapter(
                    "owner", "chapter 1", source_ids=["m1"], now_ts=1.0
                )
                await tiers.store_chapter(
                    "owner", "chapter 2", source_ids=["m2"], now_ts=2.0
                )
                await tiers.store_chapter(
                    "owner", "chapter 3", source_ids=["m3"], now_ts=3.0
                )
            self.assertNotIn(first["id"], chroma.indexed)
            self.assertEqual(len(chroma.indexed), 2)

        asyncio.run(run())


class ExtractionParseTest(unittest.TestCase):
    def test_valid_extraction_parses(self):
        raw = (
            '{"items": ['
            '{"kind": "user_profile", "fact": "The owner prefers tea.", '
            '"importance": 0.7, "confidence": 0.9},'
            '{"kind": "commitment", "fact": "The owner will send the report Friday.", '
            '"importance": 1.4, "confidence": -1}'
            "]}"
        )
        items = parse_extraction(raw)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["importance"], 0.7)
        self.assertEqual(items[1]["importance"], 1.0)  # clamped
        self.assertEqual(items[1]["confidence"], 0.0)  # clamped

    def test_unknown_kinds_and_keys_discarded(self):
        raw = (
            '{"items": ['
            '{"kind": "gossip", "fact": "nope"},'
            '{"kind": "project", "fact": "ok", "importance": 0.5, "confidence": 0.5},'
            '{"kind": "project", "fact": "bad", "extra": 1}'
            "]}"
        )
        items = parse_extraction(raw)
        self.assertEqual([item["fact"] for item in items], ["ok"])

    def test_cap_of_eight_items(self):
        items_json = ", ".join(
            f'{{"kind": "conversation", "fact": "fact number {index}", '
            '"importance": 0.1, "confidence": 0.1}'
            for index in range(12)
        )
        items = parse_extraction('{"items": [' + items_json + "]}")
        self.assertEqual(len(items), 8)

    def test_non_json_returns_empty(self):
        self.assertEqual(parse_extraction("no json at all"), [])
        self.assertEqual(parse_extraction('{"wrong": []}'), [])

    def test_prose_and_fenced_json_are_rejected(self):
        payload = '{"items":[{"kind":"conversation","fact":"A fact."}]}'
        self.assertEqual(parse_extraction(f"Here is JSON: {payload}"), [])
        self.assertEqual(parse_extraction(f"```json\n{payload}\n```"), [])

    def test_storable_filter_rejects_secrets_code_and_prompts(self):
        self.assertFalse(fact_is_storable("The api_key is sk-abcdef123456"))
        self.assertFalse(fact_is_storable("```def main(): pass```"))
        self.assertFalse(fact_is_storable("The system prompt says to be polite"))
        self.assertFalse(fact_is_storable("My passphrase is correct horse battery staple"))
        self.assertFalse(fact_is_storable("postgres://owner:pw@db.example/app"))
        self.assertFalse(fact_is_storable("AWS access key AKIAIOSFODNN7EXAMPLE"))
        self.assertFalse(fact_is_storable("-----BEGIN PRIVATE KEY-----"))
        self.assertFalse(fact_is_storable("~~~javascript\nconst x = 1;\n~~~"))
        self.assertFalse(fact_is_storable(""))
        self.assertFalse(fact_is_storable("x" * 501))
        self.assertTrue(fact_is_storable("The owner studies Japanese on Tuesdays."))


class SessionCloseTest(unittest.TestCase):
    def test_close_stores_chapter_and_clears_history(self):
        tiers, backend, fake, llm = make_tiers()
        llm._replies.clear()
        llm._replies.append("Final chapter of the thread.")
        seed_history(fake, 5)

        async def run():
            rows = await load_rows(RedisCache(fake), "owner")
            return await tiers.close_session("owner", rows, now_ts=50.0)

        result = asyncio.run(run())
        self.assertTrue(result["closed"])
        remaining = asyncio.run(load_rows(RedisCache(fake), "owner"))
        self.assertEqual(remaining, [])
        chapters = asyncio.run(tiers.recent_chapters("owner"))
        self.assertEqual(len(chapters), 1)

    def test_failed_close_keeps_history(self):
        tiers, backend, fake, llm = make_tiers()
        llm._replies.clear()
        llm._replies.append(LLMChainExhausted("down"))
        seed_history(fake, 5)

        async def run():
            rows = await load_rows(RedisCache(fake), "owner")
            return await tiers.close_session("owner", rows, now_ts=50.0)

        result = asyncio.run(run())
        self.assertFalse(result["closed"])
        remaining = asyncio.run(load_rows(RedisCache(fake), "owner"))
        self.assertEqual(len(remaining), 5)

    def test_close_excludes_non_delivered_rows_from_llm_and_sources(self):
        tiers, _, fake, llm = make_tiers(MEMORY_EXTRACTION_ENABLED=False)
        rows = seed_history(fake, 2)
        rows.append(make_row("assistant", "private pending draft", "pending"))
        fake.store[companion_history_key("owner")] = [json.dumps(row) for row in rows]
        llm._replies.clear()
        llm._replies.append("Delivered discussion only.")

        result = asyncio.run(tiers.close_session("owner", rows, now_ts=50.0))

        self.assertTrue(result["closed"])
        self.assertNotIn("private pending draft", json.dumps(llm.calls[0][1]))
        chapter = asyncio.run(tiers.all_chapters("owner"))[0]
        self.assertEqual(chapter["metadata"]["source_ids"], [rows[0]["id"], rows[1]["id"]])

    def test_extraction_excludes_non_delivered_rows(self):
        tiers, backend, _, llm = make_tiers()
        rows = [
            make_row("user", "Owner likes tea.", "delivered"),
            make_row("assistant", "secret pending draft", "pending"),
        ]
        llm._replies.clear()
        llm._replies.append(
            '{"items":[{"kind":"user_profile","fact":"Owner likes tea."}]}'
        )

        count = asyncio.run(tiers.extract_from_rows("owner", rows, now_ts=10.0))

        self.assertEqual(count, 1)
        self.assertNotIn("secret pending draft", json.dumps(llm.calls[0][1]))
        self.assertEqual(asyncio.run(backend.count("owner")), 1)

    def test_empty_history_is_a_no(self):
        tiers, _, fake, _ = make_tiers()

        async def run():
            return await tiers.close_session("owner", [], now_ts=1.0)

        result = asyncio.run(run())
        self.assertFalse(result["closed"])
        self.assertEqual(result["reason"], "empty_history")


class BridgeMemoryWiringTest(unittest.TestCase):
    def test_midterm_uses_the_actual_router(self):
        cache, _ = make_cache()
        bridge = Bridge(make_config(), cache)
        self.assertIs(bridge.midterm.llm, bridge.llm)

    def test_admin_memory_is_retrieved_when_extraction_is_disabled(self):
        cache, _ = make_cache()
        bridge = Bridge(
            make_config(
                CONTEXT_FEED_ENABLED=True,
                MEMORY_EXTRACTION_ENABLED=False,
            ),
            cache,
            llm=FakeLLM(),
        )

        async def run():
            record = bridge.longterm.make_record(
                kind="user_profile",
                text="The owner grows orchids.",
                source="admin",
                source_mode="admin",
            )
            await bridge.longterm.add("owner", record)
            return await bridge._build_prompt_blocks(
                "owner", source_conn=None, prompt_history=[], current_text="orchids"
            )

        blocks = asyncio.run(run())
        self.assertIn("The owner grows orchids.", blocks["context_feed"])

    def test_disabled_context_feed_renders_direct_memory_life_and_chapters(self):
        cache, _ = make_cache()
        bridge = Bridge(
            make_config(
                CONTEXT_FEED_ENABLED=False,
                MEMORY_EXTRACTION_ENABLED=False,
            ),
            cache,
            llm=FakeLLM(),
        )

        class LifeRows:
            available = True

            async def recent(self, owner, limit):
                return [{
                    "id": "life_1",
                    "kind": "character_life_event",
                    "text": "Visited the library.",
                    "metadata": {"day": "2026-09-03", "place": "library"},
                }]

            async def pending_ids(self, owner):
                return []

        bridge.life = LifeRows()

        async def run():
            record = bridge.longterm.make_record(
                kind="commitment",
                text="Manual memory about library books.",
                source="admin",
                source_mode="admin",
            )
            await bridge.longterm.add("owner", record)
            await bridge.midterm.store_chapter(
                "owner", "A prior chapter.", source_ids=["msg_1"], now_ts=1.0
            )
            return await bridge._build_prompt_blocks(
                "owner", source_conn=None, prompt_history=[], current_text="library"
            )

        blocks = asyncio.run(run())
        direct = blocks["context_feed"]
        self.assertIn("[LIFE CONTEXT]", direct)
        self.assertIn("Visited the library.", direct)
        self.assertIn("Manual memory about library books.", direct)
        self.assertIn("A prior chapter.", direct)


if __name__ == "__main__":
    unittest.main()
