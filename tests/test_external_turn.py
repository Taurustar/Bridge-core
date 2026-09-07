"""Generic outbound-adapter turn hook (no platform names in core)."""

from __future__ import annotations

import unittest

from core.bridge import Bridge
from core.constants import companion_history_key, external_thread_key
from core.external_profiles import default_profile
from core.llm import LLMChainExhausted

from fakes import FakeLLM, FakeOwnerProfile, FakeSchedule, make_cache, make_config


def _bridge(**overrides):
    cache, fake = make_cache()
    llm = overrides.pop("llm", None) or FakeLLM()
    config = make_config(**overrides.pop("config_overrides", {}))
    bridge = Bridge(config, cache, llm=llm, tts=overrides.pop("tts", None))
    return bridge, fake, llm


class ExternalTurnTests(unittest.IsolatedAsyncioTestCase):
    async def test_owner_join_writes_companion_history(self):
        bridge, fake, _ = _bridge()
        done = await bridge.run_external_turn(
            platform="chat",
            external_id="99",
            text="hello there",
            language="en",
            thread_kind="dm",
            thread_id="99",
            join_owner_thread=True,
        )
        self.assertEqual(done["type"], "done")
        self.assertIn(companion_history_key("owner"), fake.store)
        extra = external_thread_key("owner", "chat", "dm", "99")
        self.assertNotIn(extra, fake.store)

    async def test_non_owner_thread_uses_separate_key(self):
        bridge, fake, _ = _bridge()
        done = await bridge.run_external_turn(
            platform="chat",
            external_id="77",
            text="hello from a guest",
            language="en",
            thread_kind="dm",
            thread_id="77",
        )
        self.assertEqual(done["type"], "done")
        key = external_thread_key("owner", "chat", "dm", "77")
        self.assertIn(key, fake.store)
        self.assertNotIn(companion_history_key("owner"), fake.store)

    async def test_channel_thread_key(self):
        bridge, fake, _ = _bridge()
        await bridge.run_external_turn(
            platform="chat",
            external_id="77",
            text="hello channel",
            language="en",
            thread_kind="channel",
            thread_id="555",
        )
        self.assertIn(external_thread_key("owner", "chat", "channel", "555"), fake.store)
        self.assertNotIn(companion_history_key("owner"), fake.store)

    async def test_busy_skips_history(self):
        bridge, fake, _ = _bridge(config_overrides={"SCHEDULE_ENABLED": True})
        bridge.schedule = FakeSchedule(bridge.config, availability="busy")
        done = await bridge.run_external_turn(
            platform="chat",
            external_id="77",
            text="are you there",
            language="en",
            thread_kind="dm",
            thread_id="77",
        )
        self.assertTrue(done.get("ignored"))
        self.assertEqual(done.get("reason"), "busy")
        self.assertEqual(set(fake.store) | set(fake.strings), set())

    async def test_skips_owner_soft_block(self):
        bridge, fake, _ = _bridge(
            config_overrides={
                "OWNER_PROFILE_ENABLED": True,
                "OWNER_SOFT_BLOCK_ENABLED": True,
            }
        )
        bridge.owner_profile = FakeOwnerProfile(
            bridge.config, bridge.cache, available=True, blocked=True
        )
        done = await bridge.run_external_turn(
            platform="chat",
            external_id="77",
            text="hello guest",
            language="en",
            thread_kind="dm",
            thread_id="77",
        )
        self.assertEqual(done["type"], "done")
        self.assertFalse(done.get("ignored"))
        self.assertIn(external_thread_key("owner", "chat", "dm", "77"), fake.store)

    async def test_guest_block_when_behavior_on(self):
        llm = FakeLLM()
        bridge, _, _ = _bridge(
            llm=llm,
            config_overrides={
                "EXTERNAL_USER_PROFILE_STORE_ENABLED": True,
                "EXTERNAL_USER_PROFILES_BEHAVIOR_ENABLED": True,
            },
        )
        profile = default_profile("chat", "77")
        profile["display_name"] = "Sam"
        profile["summary"] = "Likes tea."
        await bridge.external_profiles.put("owner", "chat", "77", profile)
        await bridge.run_external_turn(
            platform="chat",
            external_id="77",
            text="hi",
            language="en",
            thread_kind="dm",
            thread_id="77",
        )
        system = llm.calls[0][1][0]["content"]
        self.assertIn("[GUEST]", system)
        self.assertIn("Sam", system)

    async def test_guest_block_off_by_default(self):
        llm = FakeLLM()
        bridge, _, _ = _bridge(llm=llm)
        profile = default_profile("chat", "77")
        profile["display_name"] = "Sam"
        await bridge.external_profiles.put("owner", "chat", "77", profile)
        await bridge.run_external_turn(
            platform="chat",
            external_id="77",
            text="hi",
            language="en",
            thread_kind="dm",
            thread_id="77",
        )
        system = llm.calls[0][1][0]["content"]
        self.assertNotIn("[GUEST]", system)

    async def test_vision_retry_text_only(self):
        llm = FakeLLM(
            [LLMChainExhausted("no vision"), "[EMOTION: neutral]\nI cannot see that."]
        )
        bridge, _, _ = _bridge(llm=llm)
        done = await bridge.run_external_turn(
            platform="chat",
            external_id="77",
            text="what is this",
            language="en",
            thread_kind="dm",
            thread_id="77",
            image_bytes=b"\x89PNG",
            image_mime="image/png",
        )
        self.assertEqual(done["type"], "done")
        first_content = llm.calls[0][1][-1]["content"]
        self.assertIsInstance(first_content, list)
        second_content = llm.calls[1][1][-1]["content"]
        self.assertEqual(second_content, "what is this")

    async def test_bad_thread_kind(self):
        bridge, fake, _ = _bridge()
        done = await bridge.run_external_turn(
            platform="chat",
            external_id="77",
            text="hi",
            language="en",
            thread_kind="group",
            thread_id="77",
        )
        self.assertEqual(done["type"], "error")
        self.assertEqual(set(fake.store) | set(fake.strings), set())


if __name__ == "__main__":
    unittest.main()
