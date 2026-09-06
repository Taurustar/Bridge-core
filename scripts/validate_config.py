#!/usr/bin/env python3
"""Config validation script (plan section 31, milestone 1.0.0).

Loads ``core.env`` (plus the real process environment, exactly like the
server would) through ``core.config.Config.from_env`` and reports:

- configuration errors with a clear message and exit code 1, or
- a bounded, non-secret summary and exit code 0.

Secrets (API keys) are never printed — only whether they are configured.
Run it before starting or restarting the service, and after any edit to
``core.env``:

    python3 scripts/validate_config.py [path/to/core.env] [--json]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core.config import Config, ConfigError  # noqa: E402
from core.constants import VERSION  # noqa: E402


def summarize(config: Config) -> dict:
    """Non-secret projection of the resolved configuration."""
    providers = {}
    for name in ("fireworks", "chutes", "ollama", "openai_compat"):
        configured = bool(
            getattr(config, f"{name.upper()}_API_KEY", "").strip()
            or (name == "ollama" and getattr(config, "OLLAMA_URL", "").strip())
        )
        providers[name] = {
            "configured": configured and bool(
                getattr(config, f"{name.upper()}_MODEL", "").strip()
            ),
            "model": getattr(config, f"{name.upper()}_MODEL", "") or None,
        }
    identity = {}
    for which in ("SOUL", "PROFILE", "STATE"):
        override = getattr(config, f"{which}_FILE", "").strip()
        path = (
            Path(override)
            if override
            else REPO_ROOT / "identity" / f"{which}.md"
        )
        identity[which.lower()] = {"path": str(path), "exists": path.exists()}
    return {
        "version": VERSION,
        "owner_user_id": config.OWNER_USER_ID,
        "bind": f"{config.BRIDGE_HOST}:{config.BRIDGE_PORT}",
        "tailscale_required": config.TAILSCALE_REQUIRED,
        "tailscale_firewall_ack": config.TAILSCALE_FIREWALL_ACK,
        "default_language": config.DEFAULT_LANGUAGE,
        "owner_timezone": config.OWNER_TIMEZONE,
        "character_timezone": config.CHARACTER_TIMEZONE,
        "redis": {"host": config.REDIS_HOST, "port": config.REDIS_PORT,
                  "db": config.REDIS_DB},
        "chroma": {"enabled": config.CHROMA_ENABLED,
                   "required": config.CHROMA_REQUIRED,
                   "path": config.CHROMA_PATH},
        "providers": providers,
        "speech": {
            "tts_enabled": config.TTS_ENABLED,
            "tts_configured": bool(config.ELEVENLABS_API_KEY.strip())
            and bool(config.ELEVENLABS_VOICE_ID.strip()),
            "stt_enabled": config.STT_ENABLED,
            "stt_provider": config.STT_PROVIDER,
        },
        "identity_files": identity,
        "flags": {
            name: getattr(config, name)
            for name in (
                "NEEDS_ENABLED", "BIDS_ENABLED", "RHYTHM_ENABLED",
                "STATE_EXPRESSION_ENABLED", "OWNER_PROFILE_ENABLED",
                "OWNER_SOFT_BLOCK_ENABLED", "OWNER_PROFILE_LLM_ENABLED",
                "SCHEDULE_ENABLED", "LIFE_ENABLED", "USER_SCHEDULE_ENABLED",
                "MEMORY_EXTRACTION_ENABLED", "MEMORY_CLEANUP_ENABLED",
                "CHROMA_ENABLED", "CONTEXT_FEED_ENABLED", "DAILY_TOOLS_ENABLED",
                "DAILY_WEB_ENABLED", "HEARTBEAT_ENABLED", "INITIATIVE_ENABLED",
                "INITIATIVE_RESPECT_OWNER_SCHEDULE",
                "EXTERNAL_USER_PROFILE_STORE_ENABLED",
                "EXTERNAL_USER_PROFILES_BEHAVIOR_ENABLED",
                "EXTERNAL_USER_PROFILE_LLM_ENABLED",
                "WORK_ENABLED", "SESSIONS_ENABLED", "WORK_SKILLS_ENABLED",
                "MCP_PROXY_ENABLED", "AGENT_CHECKPOINTS_ENABLED",
                "DEVICE_ENABLED",
            )
        },
        "initiative": {
            "min_heartbeats": config.INITIATIVE_MIN_HEARTBEATS,
            "window_seconds": config.INITIATIVE_HEARTBEAT_WINDOW_SECONDS,
            "count_interval_seconds": config.INITIATIVE_HEARTBEAT_COUNT_INTERVAL_SECONDS,
            "min_gap_seconds": config.INITIATIVE_MIN_GAP_SECONDS,
            "daily_max": config.INITIATIVE_DAILY_MAX,
            "eligibility_chance": config.INITIATIVE_ELIGIBILITY_CHANCE,
            "seed_file": config.INITIATIVE_SEED_FILE or "(default ./data/initiative_seed)",
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate a Bridge Core Engine core.env file."
    )
    parser.add_argument(
        "env_file",
        nargs="?",
        default="core.env",
        help="Path to the env file (default: ./core.env)",
    )
    parser.add_argument(
        "--json", action="store_true", help="Print the summary as JSON."
    )
    args = parser.parse_args(argv)

    if not Path(args.env_file).is_file():
        print(f"ERROR: env file not found: {args.env_file}", file=sys.stderr)
        return 1
    try:
        config = Config.from_env(env_file=args.env_file)
    except ConfigError as exc:
        print(f"ERROR: invalid configuration: {exc}", file=sys.stderr)
        return 1

    summary = summarize(config)
    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print("Configuration OK.")
        flags_on = sorted(name for name, on in summary["flags"].items() if on)
        print(f"  version: {summary['version']}")
        print(f"  bind: {summary['bind']} (owner={summary['owner_user_id']})")
        print(f"  redis: {summary['redis']['host']}:{summary['redis']['port']}"
              f"/{summary['redis']['db']}")
        print(f"  flags on: {', '.join(flags_on) if flags_on else '(none)'}")
        for name, entry in summary["providers"].items():
            state = "configured" if entry["configured"] else "not configured"
            print(f"  provider {name}: {state}"
                  + (f" model={entry['model']}" if entry["model"] else ""))
        for which, entry in summary["identity_files"].items():
            state = "found" if entry["exists"] else "missing"
            print(f"  identity {which}: {state} ({entry['path']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
