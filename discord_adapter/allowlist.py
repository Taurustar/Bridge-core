"""Guild/channel allowlist and DM routing. Empty lists mean none, never all."""

from __future__ import annotations

from .config import DiscordConfig

ROUTE_OWNER_DM = "owner_dm"
ROUTE_STRANGER_DM = "stranger_dm"
ROUTE_CHANNEL = "channel"


def parse_id_set(raw: str) -> frozenset[str]:
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


def guild_channel_allowed(config: DiscordConfig, guild_id: str, channel_id: str) -> bool:
    guilds = parse_id_set(config.DISCORD_GUILD_IDS)
    channels = parse_id_set(config.DISCORD_CHANNEL_IDS)
    if not guilds or not channels:
        return False
    return guild_id in guilds and channel_id in channels


def route_for(
    config: DiscordConfig,
    *,
    is_dm: bool,
    author_id: str,
    guild_id: str,
    channel_id: str,
    mentioned: bool,
    replied_to_bot: bool,
) -> str | None:
    """Which surface this message belongs to, or None for silence."""
    owner_id = config.DISCORD_OWNER_USER_ID.strip()
    if is_dm:
        if owner_id and author_id == owner_id:
            return ROUTE_OWNER_DM if config.DISCORD_DM_OWNER_ENABLED else None
        if config.DISCORD_DM_STRANGERS_ENABLED:
            return ROUTE_STRANGER_DM
        return None
    if not guild_channel_allowed(config, guild_id, channel_id):
        return None
    if not (mentioned or replied_to_bot):
        return None
    return ROUTE_CHANNEL
