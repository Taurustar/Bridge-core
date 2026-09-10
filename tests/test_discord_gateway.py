"""Discord adapter unit tests. The word is allowed here; core/ is still clean."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.bridge import Bridge
from core.constants import companion_history_key, external_thread_key
from discord_adapter.allowlist import (
    ROUTE_CHANNEL,
    ROUTE_OWNER_DM,
    ROUTE_STRANGER_DM,
    route_for,
)
from discord_adapter.config import DiscordConfig, load
from discord_adapter.media import synthesize, transcribe
from discord_adapter.sidecar import start as sidecar_start
from discord_adapter.turns import InboundEvent, RateLimiter, process

from fakes import FakeLLM, FakeSTT, FakeTTS, make_cache, make_config


def _cfg(**overrides) -> DiscordConfig:
    base = DiscordConfig(
        DISCORD_ENABLED=True,
        DISCORD_OWNER_USER_ID="99",
        DISCORD_DM_OWNER_ENABLED=True,
        DISCORD_DM_STRANGERS_ENABLED=True,
        DISCORD_GUILD_IDS="1",
        DISCORD_CHANNEL_IDS="2",
    )
    for key, value in overrides.items():
        setattr(base, key, value)
    return base


class ConfigTests(unittest.TestCase):
    def test_missing_file_returns_none(self):
        self.assertIsNone(load(Path("/nonexistent/discord.env"), environ={}))

    def test_disabled_file_loads(self):
        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as tmp:
            tmp.write("DISCORD_ENABLED=false\nDISCORD_BOT_TOKEN=secret-token\n")
            path = Path(tmp.name)
        cfg = load(path, environ={})
        self.assertIsNotNone(cfg)
        self.assertFalse(cfg.DISCORD_ENABLED)
        self.assertEqual(cfg.DISCORD_BOT_TOKEN, "secret-token")


class AllowlistTests(unittest.TestCase):
    def test_empty_lists_mean_none(self):
        cfg = _cfg(DISCORD_GUILD_IDS="", DISCORD_CHANNEL_IDS="")
        self.assertIsNone(
            route_for(
                cfg,
                is_dm=False,
                author_id="8",
                guild_id="1",
                channel_id="2",
                mentioned=True,
                replied_to_bot=False,
            )
        )

    def test_guild_requires_mention_or_reply(self):
        cfg = _cfg()
        self.assertIsNone(
            route_for(
                cfg,
                is_dm=False,
                author_id="8",
                guild_id="1",
                channel_id="2",
                mentioned=False,
                replied_to_bot=False,
            )
        )
        self.assertEqual(
            route_for(
                cfg,
                is_dm=False,
                author_id="8",
                guild_id="1",
                channel_id="2",
                mentioned=True,
                replied_to_bot=False,
            ),
            ROUTE_CHANNEL,
        )
        self.assertEqual(
            route_for(
                cfg,
                is_dm=False,
                author_id="8",
                guild_id="1",
                channel_id="2",
                mentioned=False,
                replied_to_bot=True,
            ),
            ROUTE_CHANNEL,
        )

    def test_owner_and_stranger_dms(self):
        cfg = _cfg()
        self.assertEqual(
            route_for(
                cfg,
                is_dm=True,
                author_id="99",
                guild_id="",
                channel_id="dm",
                mentioned=False,
                replied_to_bot=False,
            ),
            ROUTE_OWNER_DM,
        )
        self.assertEqual(
            route_for(
                cfg,
                is_dm=True,
                author_id="77",
                guild_id="",
                channel_id="dm",
                mentioned=False,
                replied_to_bot=False,
            ),
            ROUTE_STRANGER_DM,
        )

    def test_owner_dm_flag_off(self):
        cfg = _cfg(DISCORD_DM_OWNER_ENABLED=False)
        self.assertIsNone(
            route_for(
                cfg,
                is_dm=True,
                author_id="99",
                guild_id="",
                channel_id="dm",
                mentioned=False,
                replied_to_bot=False,
            )
        )


class MediaTests(unittest.IsolatedAsyncioTestCase):
    async def test_stt_off_makes_no_call(self):
        stt = FakeSTT(transcripts=["hello"])
        cache, _ = make_cache()
        bridge = Bridge(make_config(), cache, llm=FakeLLM(), stt=stt)
        text = await transcribe(bridge, _cfg(DISCORD_STT_ENABLED=False), b"ogg", "audio/ogg", "en")
        self.assertEqual(text, "")
        self.assertEqual(stt.calls, [])

    async def test_tts_off_makes_no_call(self):
        tts = FakeTTS()
        cache, _ = make_cache()
        bridge = Bridge(make_config(), cache, llm=FakeLLM(), tts=tts)
        audio = await synthesize(
            bridge,
            _cfg(DISCORD_TTS_ENABLED=False),
            [{"text": "hi", "emotion": "neutral"}],
        )
        self.assertIsNone(audio)
        self.assertEqual(tts.calls, [])


class TurnTests(unittest.IsolatedAsyncioTestCase):
    async def test_owner_dm_joins_companion_thread(self):
        cache, fake = make_cache()
        bridge = Bridge(make_config(), cache, llm=FakeLLM())
        reply = await process(
            bridge,
            _cfg(),
            InboundEvent(author_id="99", channel_id="dm", text="hello owner"),
            RateLimiter(8, 60),
        )
        self.assertIsNotNone(reply)
        self.assertIn(companion_history_key("owner"), fake.store)
        self.assertNotIn(external_thread_key("owner", "discord", "dm", "99"), fake.store)

    async def test_stranger_dm_separate_key(self):
        cache, fake = make_cache()
        bridge = Bridge(make_config(), cache, llm=FakeLLM())
        await process(
            bridge,
            _cfg(),
            InboundEvent(author_id="77", channel_id="dm", text="hello stranger"),
            RateLimiter(8, 60),
        )
        self.assertIn(external_thread_key("owner", "discord", "dm", "77"), fake.store)
        self.assertNotIn(companion_history_key("owner"), fake.store)

    async def test_channel_separate_key(self):
        cache, fake = make_cache()
        bridge = Bridge(make_config(), cache, llm=FakeLLM())
        await process(
            bridge,
            _cfg(),
            InboundEvent(
                author_id="77",
                channel_id="2",
                guild_id="1",
                text="hello",
                mentioned=True,
            ),
            RateLimiter(8, 60),
        )
        self.assertIn(external_thread_key("owner", "discord", "channel", "2"), fake.store)
        self.assertNotIn(companion_history_key("owner"), fake.store)

    async def test_unmentioned_guild_is_silence(self):
        cache, fake = make_cache()
        bridge = Bridge(make_config(), cache, llm=FakeLLM())
        reply = await process(
            bridge,
            _cfg(),
            InboundEvent(
                author_id="77", channel_id="2", guild_id="1", text="hello"
            ),
            RateLimiter(8, 60),
        )
        self.assertIsNone(reply)
        self.assertEqual(set(fake.store) | set(fake.strings), set())

    async def test_vision_off_drops_image(self):
        llm = FakeLLM()
        cache, _ = make_cache()
        bridge = Bridge(make_config(), cache, llm=llm)
        await process(
            bridge,
            _cfg(DISCORD_VISION_ENABLED=False),
            InboundEvent(
                author_id="77",
                channel_id="dm",
                text="what is this",
                image=b"\x89PNG",
                image_type="image/png",
            ),
            RateLimiter(8, 60),
        )
        content = llm.calls[0][1][-1]["content"]
        self.assertEqual(content, "what is this")

    async def test_stt_off_ignores_audio(self):
        stt = FakeSTT(transcripts=["transcribed"])
        cache, fake = make_cache()
        bridge = Bridge(make_config(), cache, llm=FakeLLM(), stt=stt)
        reply = await process(
            bridge,
            _cfg(DISCORD_STT_ENABLED=False),
            InboundEvent(
                author_id="77", channel_id="dm", text="", audio=b"ogg", audio_type="audio/ogg"
            ),
            RateLimiter(8, 60),
        )
        self.assertIsNone(reply)
        self.assertEqual(stt.calls, [])
        self.assertEqual(set(fake.store) | set(fake.strings), set())

    async def test_rate_limit(self):
        cache, fake = make_cache()
        bridge = Bridge(make_config(), cache, llm=FakeLLM())
        limiter = RateLimiter(1, 60)
        first = await process(
            bridge,
            _cfg(),
            InboundEvent(author_id="77", channel_id="dm", text="one"),
            limiter,
        )
        second = await process(
            bridge,
            _cfg(),
            InboundEvent(author_id="77", channel_id="dm", text="two"),
            limiter,
        )
        self.assertIsNotNone(first)
        self.assertIsNone(second)


class SidecarTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_env_does_not_start(self):
        class _App:
            class state:
                bridge = None

        from unittest.mock import patch

        with patch("discord_adapter.sidecar.load", return_value=None):
            await sidecar_start(_App())


if __name__ == "__main__":
    unittest.main()
