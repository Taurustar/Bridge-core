"""Load discord.env only. Never log the bot token."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values

_ROOT = Path(__file__).resolve().parent.parent
_PACKAGE = Path(__file__).resolve().parent


def _bool(raw: str) -> bool:
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _int(raw: str, default: int) -> int:
    try:
        return int(raw.strip())
    except (TypeError, ValueError):
        return default


@dataclass
class DiscordConfig:
    DISCORD_ENABLED: bool = False
    DISCORD_BOT_TOKEN: str = ""
    DISCORD_GUILD_IDS: str = ""
    DISCORD_CHANNEL_IDS: str = ""
    DISCORD_DM_OWNER_ENABLED: bool = False
    DISCORD_DM_STRANGERS_ENABLED: bool = False
    DISCORD_OWNER_USER_ID: str = ""
    DISCORD_STT_ENABLED: bool = False
    DISCORD_TTS_ENABLED: bool = False
    DISCORD_VISION_ENABLED: bool = False
    DISCORD_RATE_MAX: int = 8
    DISCORD_RATE_WINDOW_SECONDS: int = 60


def env_file_path() -> Path | None:
    override = os.environ.get("DISCORD_ENV_FILE", "").strip()
    candidates = []
    if override:
        candidates.append(Path(override))
    candidates.extend((_ROOT / "discord.env", _PACKAGE / "discord.env"))
    for path in candidates:
        if path.is_file():
            return path
    return None


def load(path: Path | None = None, environ: dict[str, str] | None = None) -> DiscordConfig | None:
    """Return None when no env file exists (adapter must not start)."""
    source = path if path is not None else env_file_path()
    if source is None or not source.is_file():
        return None
    values = {key: str(value) for key, value in dotenv_values(source).items() if value is not None}
    merged = dict(values)
    merged.update(os.environ if environ is None else environ)
    return DiscordConfig(
        DISCORD_ENABLED=_bool(merged.get("DISCORD_ENABLED", "false")),
        DISCORD_BOT_TOKEN=merged.get("DISCORD_BOT_TOKEN", "").strip(),
        DISCORD_GUILD_IDS=merged.get("DISCORD_GUILD_IDS", "").strip(),
        DISCORD_CHANNEL_IDS=merged.get("DISCORD_CHANNEL_IDS", "").strip(),
        DISCORD_DM_OWNER_ENABLED=_bool(merged.get("DISCORD_DM_OWNER_ENABLED", "false")),
        DISCORD_DM_STRANGERS_ENABLED=_bool(
            merged.get("DISCORD_DM_STRANGERS_ENABLED", "false")
        ),
        DISCORD_OWNER_USER_ID=merged.get("DISCORD_OWNER_USER_ID", "").strip(),
        DISCORD_STT_ENABLED=_bool(merged.get("DISCORD_STT_ENABLED", "false")),
        DISCORD_TTS_ENABLED=_bool(merged.get("DISCORD_TTS_ENABLED", "false")),
        DISCORD_VISION_ENABLED=_bool(merged.get("DISCORD_VISION_ENABLED", "false")),
        DISCORD_RATE_MAX=max(1, _int(merged.get("DISCORD_RATE_MAX", "8"), 8)),
        DISCORD_RATE_WINDOW_SECONDS=max(
            1, _int(merged.get("DISCORD_RATE_WINDOW_SECONDS", "60"), 60)
        ),
    )
