"""Minimal durable long-term memory backend tests (plan section 20.3)."""

from __future__ import annotations

import unittest

from core.cache import RedisCache
from core.memory import LongTermMemory
from core.constants import longterm_key

from fakes import FakeRedis, make_config


def make_backend(max_records: int = 1000) -> tuple[LongTermMemory, FakeRedis]:
    fake = FakeRedis()
    config = make_config(MEMORY_MAX_PER_USER=max_records)
    return LongTermMemory(config, RedisCache(fake)), fake


class LongTermMemoryTest(unittest.IsolatedAsyncioTestCase):
    async def test_add_and_read_records(self):
        backend, fake = make_backend()
        record = backend.make_record(
            kind="character_life_event",
            text="Visited the market.",
            source="life_engine",
            source_mode="life",
            importance=0.5,
            metadata={"day": "2026-09-02"},
        )
        await backend.add("owner", record)
        self.assertIn(longterm_key("owner"), fake.store)
        rows = await backend.records("owner")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["text"], "Visited the market.")
        self.assertEqual(await backend.count("owner"), 1)

    async def test_upsert_by_id(self):
        backend, _ = make_backend()
        record = backend.make_record(
            kind="character_life_event", text="v1", source="s", source_mode="life",
            record_id="mem_fixed",
        )
        await backend.add("owner", record)
        record["text"] = "v2"
        record["updated_ts"] = 2.0
        await backend.add("owner", record)
        rows = await backend.records("owner")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["text"], "v2")

    async def test_unknown_kind_rejected(self):
        backend, _ = make_backend()
        with self.assertRaises(ValueError):
            backend.make_record(kind="dreams", text="x", source="s", source_mode="life")

    async def test_bound_at_max_records(self):
        backend, _ = make_backend(max_records=3)
        for index in range(5):
            record = backend.make_record(
                kind="character_life_event", text=f"e{index}", source="s",
                source_mode="life", created_ts=index, record_id=f"mem_{index}",
            )
            record["updated_ts"] = float(index)
            await backend.add("owner", record)
        rows = await backend.records("owner")
        self.assertEqual(len(rows), 3)
        # Oldest rows dropped first.
        self.assertEqual([r["id"] for r in rows], ["mem_2", "mem_3", "mem_4"])

    async def test_kind_filter_and_limit(self):
        backend, _ = make_backend()
        for kind in ("character_life_event", "commitment"):
            await backend.add(
                "owner",
                backend.make_record(kind=kind, text="t", source="s",
                                    source_mode="life"),
            )
        rows = await backend.records("owner", kind="commitment")
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(await backend.records("owner", limit=1)), 1)

    async def test_get_by_id(self):
        backend, _ = make_backend()
        record = backend.make_record(
            kind="character_life_event", text="find me", source="s",
            source_mode="life", record_id="mem_target",
        )
        await backend.add("owner", record)
        self.assertEqual((await backend.get("owner", "mem_target"))["text"], "find me")
        self.assertIsNone(await backend.get("owner", "mem_missing"))


if __name__ == "__main__":
    unittest.main()
