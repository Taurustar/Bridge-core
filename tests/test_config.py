"""Config parsing tests (plan sections 8.1, 8.3)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.config import Config, ConfigError

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = REPO_ROOT / "core.env.template"
FULL_EXAMPLE = REPO_ROOT / "core.env.full.example"


class ConfigDefaultsTest(unittest.TestCase):
    def test_template_defaults_are_safe_and_inert(self):
        config = Config.from_env(env_file=str(TEMPLATE), environ={})
        self.assertEqual(config.BRIDGE_HOST, "127.0.0.1")
        self.assertEqual(config.BRIDGE_PORT, 8766)
        self.assertTrue(config.TAILSCALE_REQUIRED)
        self.assertFalse(config.TAILSCALE_FIREWALL_ACK)
        self.assertEqual(config.OWNER_USER_ID, "owner")
        # Behavior flags default OFF.
        for flag in (
            "NEEDS_ENABLED", "BIDS_ENABLED", "RHYTHM_ENABLED",
            "STATE_EXPRESSION_ENABLED", "OWNER_PROFILE_ENABLED",
            "SCHEDULE_ENABLED", "LIFE_ENABLED", "MEMORY_EXTRACTION_ENABLED",
            "MEMORY_CLEANUP_ENABLED", "INITIATIVE_ENABLED", "DEVICE_ENABLED",
            "DAILY_TOOLS_ENABLED", "DAILY_WEB_ENABLED", "USER_SCHEDULE_ENABLED",
            "TTS_ENABLED", "STT_ENABLED", "CHROMA_ENABLED",
        ):
            self.assertFalse(getattr(config, flag), flag)
        # Secrets blank.
        self.assertEqual(config.FIREWORKS_API_KEY, "")
        self.assertEqual(config.CHUTES_API_KEY, "")
        self.assertEqual(config.OPENAI_COMPAT_API_KEY, "")
        self.assertEqual(config.ELEVENLABS_API_KEY, "")
        self.assertEqual(config.DEEPGRAM_API_KEY, "")
        self.assertEqual(config.ASSEMBLYAI_API_KEY, "")
        self.assertEqual(config.TAVILY_API_KEY, "")

    def test_template_has_no_auth_variables(self):
        text = TEMPLATE.read_text()
        for forbidden in ("BEARER", "AUTH_TOKEN", "PASSWORD", "OAUTH"):
            self.assertNotIn(forbidden, text)

    def test_real_env_wins_over_core_env_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / "core.env"
            env_file.write_text("BRIDGE_PORT=9999\nMAX_HISTORY_TURNS=5\n")
            config = Config.from_env(
                env_file=str(env_file), environ={"BRIDGE_PORT": "1234"}
            )
            self.assertEqual(config.BRIDGE_PORT, 1234)
            self.assertEqual(config.MAX_HISTORY_TURNS, 5)

    def test_file_values_apply_when_env_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / "core.env"
            env_file.write_text("BRIDGE_PORT=9999\nOWNER_USER_ID=me\n")
            config = Config.from_env(env_file=str(env_file), environ={})
            self.assertEqual(config.BRIDGE_PORT, 9999)
            self.assertEqual(config.OWNER_USER_ID, "me")

    def test_invalid_numeric_fails_clearly(self):
        with self.assertRaises(ConfigError) as ctx:
            Config.from_env(env_file=None, environ={"BRIDGE_PORT": "abc"})
        self.assertIn("BRIDGE_PORT", str(ctx.exception))

    def test_invalid_float_fails_clearly(self):
        with self.assertRaises(ConfigError) as ctx:
            Config.from_env(env_file=None, environ={"COMPANION_TEMPERATURE": "hot"})
        self.assertIn("COMPANION_TEMPERATURE", str(ctx.exception))

    def test_invalid_bool_fails_clearly(self):
        with self.assertRaises(ConfigError) as ctx:
            Config.from_env(env_file=None, environ={"NEEDS_ENABLED": "maybe"})
        self.assertIn("NEEDS_ENABLED", str(ctx.exception))

    def test_unknown_chain_provider_fails(self):
        with self.assertRaises(ConfigError) as ctx:
            Config.from_env(env_file=None, environ={"LLM_CHAIN": "fireworks,mystery"})
        self.assertIn("mystery", str(ctx.exception))

    def test_daily_tool_max_calls_enforces_protocol_range(self):
        for value in (0, 7):
            with self.subTest(value=value), self.assertRaises(ConfigError):
                Config.from_env(
                    env_file=None,
                    environ={"DAILY_TOOL_MAX_CALLS": str(value)},
                )
        self.assertEqual(
            Config.from_env(
                env_file=None, environ={"DAILY_TOOL_MAX_CALLS": "6"}
            ).DAILY_TOOL_MAX_CALLS,
            6,
        )

    def test_full_example_parses_and_enables_owner_features(self):
        """Plan section 8.4: the full single-owner profile must parse."""
        config = Config.from_env(env_file=str(FULL_EXAMPLE), environ={})
        self.assertTrue(config.NEEDS_ENABLED)
        self.assertTrue(config.OWNER_PROFILE_ENABLED)
        self.assertTrue(config.OWNER_PROFILE_INJECT)
        self.assertTrue(config.OWNER_BOUNDARY_PENALTIES_ENABLED)
        self.assertTrue(config.OWNER_SOFT_BLOCK_ENABLED)
        self.assertTrue(config.OWNER_STATUS_DRIFT_ENABLED)
        self.assertTrue(config.OWNER_AGREEMENTS_ENABLED)
        self.assertTrue(config.OWNER_AGREEMENT_AFTERMATH_ENABLED)
        # External-profile behavior stays off even in the full profile.
        self.assertFalse(config.EXTERNAL_USER_PROFILES_BEHAVIOR_ENABLED)
        self.assertFalse(config.EXTERNAL_USER_PROFILE_LLM_ENABLED)

    def test_apply_hot_reloads_non_structural_only(self):
        live = Config.from_env(env_file=None, environ={})
        candidate = Config.from_env(
            env_file=None,
            environ={
                "BRIDGE_PORT": "9999",       # structural: must NOT apply
                "REDIS_HOST": "10.0.0.5",    # structural: must NOT apply
                "COMPANION_TEMPERATURE": "0.1",
                "NEEDS_ENABLED": "true",
            },
        )
        applied = live.apply(candidate)
        self.assertEqual(live.BRIDGE_PORT, 8766)
        self.assertEqual(live.REDIS_HOST, "127.0.0.1")
        self.assertEqual(live.COMPANION_TEMPERATURE, 0.1)
        self.assertTrue(live.NEEDS_ENABLED)
        self.assertIn("COMPANION_TEMPERATURE", applied)
        self.assertNotIn("BRIDGE_PORT", applied)


if __name__ == "__main__":
    unittest.main()
