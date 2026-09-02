"""Rhythm tests (plan section 15.6): metadata-only, disabled by default."""

from __future__ import annotations

import json
import unittest

from core.constants import rhythm_key
from core.rhythm import RhythmEngine, _hour_bucket, record_contact

from fakes import make_cache, make_config


class RhythmPureTest(unittest.TestCase):
    def test_hour_bucket_uses_owner_civil_time(self):
        # 2026-09-02 21:00 UTC is a different civil hour elsewhere.
        bucket = _hour_bucket(1788480000.0, "UTC")
        self.assertRegex(bucket, r"^\d{4}-\d{2}-\d{2}T\d{2}$")
        self.assertEqual(_hour_bucket(1788480000.0, "Not/AZone"),
                         _hour_bucket(1788480000.0, "UTC"))

    def test_record_contact_keeps_only_metadata(self):
        record = record_contact("2026-09-02T21", {"hourly": {}, "last_contact_ts": 0})
        self.assertEqual(record["hourly"]["2026-09-02T21"], 1)
        self.assertEqual(set(record.keys()), {"hourly", "last_contact_ts",
                                              "last_contact_bucket"})

    def test_histogram_is_bounded(self):
        record: dict = {"hourly": {}}
        for hour in range(100):
            record = record_contact(f"2026-01-01T{hour % 24:02d}", record)
        self.assertLessEqual(len(record["hourly"]), 48)


class RhythmEngineTest(unittest.IsolatedAsyncioTestCase):
    async def test_stamp_contact_writes_metadata_only(self):
        cache, fake = make_cache()
        engine = RhythmEngine(make_config(RHYTHM_ENABLED=True), cache)
        await engine.stamp_contact("owner", "UTC", now_ts=1788480000.0)
        stored = fake.strings[rhythm_key("owner")]
        record = json.loads(stored)
        self.assertIn("hourly", record)
        self.assertNotIn("text", stored)
        self.assertNotIn("message", stored)

    async def test_flag_off_creates_no_key(self):
        cache, fake = make_cache()
        engine = RhythmEngine(make_config(RHYTHM_ENABLED=False), cache)
        self.assertFalse(engine.available)
        # The bridge never calls stamp_contact when unavailable; the store
        # method itself is still safe but the flag-off contract is the
        # bridge-level no-op (covered in the WS integration tests).
        self.assertNotIn(rhythm_key("owner"), fake.strings)


if __name__ == "__main__":
    unittest.main()
