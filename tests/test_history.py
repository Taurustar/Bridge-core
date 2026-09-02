"""History persistence tests (plan sections 12, 28)."""

from __future__ import annotations

import unittest

from core import history as hist
from core.constants import companion_history_key

from fakes import FakeRedis, make_cache

OWNER = "owner"


class HistoryTest(unittest.IsolatedAsyncioTestCase):
    async def test_rows_persisted_with_id_ts_delivery_state(self):
        cache, _ = make_cache()
        row = hist.make_row("user", "hello", hist.DELIVERED)
        await hist.append_row(cache, OWNER, row, 80)
        rows = await hist.load_rows(cache, OWNER)
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["id"].startswith("msg_"))
        self.assertTrue(rows[0]["ts"])
        self.assertEqual(rows[0]["delivery_state"], "delivered")
        self.assertEqual(rows[0]["role"], "user")

    async def test_cap_enforced(self):
        cache, _ = make_cache()
        for i in range(10):
            await hist.append_row(
                cache, OWNER, hist.make_row("user", f"m{i}", hist.DELIVERED), 4
            )
        rows = await hist.load_rows(cache, OWNER)
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[-1]["text"], "m9")
        self.assertEqual(rows[0]["text"], "m6")

    async def test_undelivered_rows_excluded_from_prompt_history(self):
        cache, _ = make_cache()
        await hist.append_row(
            cache, OWNER, hist.make_row("user", "hi", hist.DELIVERED), 80
        )
        bad = hist.make_row("assistant", "lost reply", hist.PENDING)
        await hist.append_row(cache, OWNER, bad, 80)
        await hist.mark_delivery_state(cache, OWNER, bad["id"], hist.UNDELIVERED)

        prompt_rows = await hist.load_prompt_history(cache, OWNER, 40)
        self.assertEqual([r["text"] for r in prompt_rows], ["hi"])

    async def test_delivery_unknown_rows_excluded_from_prompt_history(self):
        cache, _ = make_cache()
        row = hist.make_row("assistant", "maybe received", hist.DELIVERY_UNKNOWN)
        await hist.append_row(cache, OWNER, row, 80)
        prompt_rows = await hist.load_prompt_history(cache, OWNER, 40)
        self.assertEqual(prompt_rows, [])

    async def test_current_user_row_excluded_from_prompt(self):
        cache, _ = make_cache()
        row = hist.make_row("user", "current", hist.DELIVERED)
        await hist.append_row(cache, OWNER, row, 80)
        prompt_rows = await hist.load_prompt_history(
            cache, OWNER, 40, exclude_id=row["id"]
        )
        self.assertEqual(prompt_rows, [])

    async def test_budget_bounds_history(self):
        cache, _ = make_cache()
        for i in range(10):
            await hist.append_row(
                cache, OWNER, hist.make_row("user", f"m{i}", hist.DELIVERED), 80
            )
        prompt_rows = await hist.load_prompt_history(cache, OWNER, 3)
        self.assertEqual([r["text"] for r in prompt_rows], ["m7", "m8", "m9"])

    async def test_mark_delivery_state_updates_in_place(self):
        cache, fake = make_cache()
        row = hist.make_row("assistant", "reply", hist.PENDING)
        await hist.append_row(cache, OWNER, row, 80)
        ok = await hist.mark_delivery_state(cache, OWNER, row["id"], hist.DELIVERED)
        self.assertTrue(ok)
        rows = await hist.load_rows(cache, OWNER)
        self.assertEqual(rows[0]["delivery_state"], "delivered")
        # unknown id -> False, no crash
        self.assertFalse(
            await hist.mark_delivery_state(cache, OWNER, "msg_nope", hist.DELIVERED)
        )

    async def test_only_companion_history_key_created(self):
        cache, fake = make_cache()
        await hist.append_row(
            cache, OWNER, hist.make_row("user", "hi", hist.DELIVERED), 80
        )
        self.assertEqual(
            list(fake.store.keys()), [companion_history_key(OWNER)]
        )


if __name__ == "__main__":
    unittest.main()
