"""Bids and bond tests (plan section 15.5)."""

from __future__ import annotations

import json
import time
import unittest

from core.bids import BidsEngine, make_bid
from core.constants import bids_key

from fakes import make_cache, make_config


class BidShapeTest(unittest.TestCase):
    def test_bid_stores_metadata_only(self):
        bid = make_bid("initiative_bond", now_ts=1000.0, lifetime_seconds=60.0)
        self.assertEqual(
            set(bid.keys()),
            {"id", "kind", "size", "sent_ts", "expire_ts", "answered_ts", "result"},
        )
        self.assertEqual(bid["result"], "open")
        self.assertEqual(bid["expire_ts"], 1060.0)

    def test_unknown_kind_rejected(self):
        with self.assertRaises(ValueError):
            make_bid("kink_bonus")

    def test_no_intimacy_kinds_exist(self):
        kinds_blob = "initiative_life initiative_bond initiative_fun initiative_thread"
        self.assertNotIn("intimacy", kinds_blob)
        self.assertNotIn("lust", kinds_blob)


class BidsEngineTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        cache, fake = make_cache()
        self.cache = cache
        self.fake = fake
        self.engine = BidsEngine(make_config(BIDS_ENABLED=True), cache)

    async def test_register_and_open(self):
        await self.engine.register_bid("owner", "initiative_fun", now_ts=time.time())
        opened = await self.engine.open_bids("owner")
        self.assertEqual(len(opened), 1)
        self.assertEqual(opened[0]["kind"], "initiative_fun")

    async def test_satisfy_requires_substantive_reply(self):
        await self.engine.register_bid("owner", "initiative_life", now_ts=time.time())
        answered = await self.engine.satisfy_open_bids("owner", "hi")
        self.assertEqual(answered, 0)
        answered = await self.engine.satisfy_open_bids(
            "owner", "a sufficiently long reply that means something"
        )
        self.assertEqual(answered, 1)
        record = json.loads(self.fake.strings[bids_key("owner")])
        self.assertEqual(record[0]["result"], "answered")

    async def test_expired_bids_are_swept(self):
        await self.engine.register_bid(
            "owner", "initiative_thread", now_ts=time.time() - 7200.0,
            lifetime_seconds=60.0,
        )
        swept = await self.engine.sweep_expired("owner")
        self.assertEqual(swept, 1)
        self.assertEqual(await self.engine.open_bids("owner"), [])
        record = json.loads(self.fake.strings[bids_key("owner")])
        self.assertEqual(record[0]["result"], "expired")

    async def test_record_is_bounded(self):
        for _ in range(100):
            await self.engine.register_bid("owner", "initiative_fun", now_ts=time.time())
        record = json.loads(self.fake.strings[bids_key("owner")])
        self.assertLessEqual(len(record), 64)

    async def test_flag_off_is_inert(self):
        engine = BidsEngine(make_config(BIDS_ENABLED=False), self.cache)
        self.assertFalse(engine.available)
        self.assertEqual(await self.engine.open_bids("owner"), [])
        self.assertNotIn(bids_key("owner"), self.fake.strings)


if __name__ == "__main__":
    unittest.main()
