"""Call Bridge turns. No MCP or device daemon."""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field

from .allowlist import (
    ROUTE_CHANNEL,
    ROUTE_OWNER_DM,
    ROUTE_STRANGER_DM,
    route_for,
)
from .config import DiscordConfig
from .media import synthesize, transcribe

log = logging.getLogger("bridge.discord.turns")

PLATFORM = "discord"


@dataclass
class InboundEvent:
    author_id: str
    channel_id: str
    guild_id: str = ""
    text: str = ""
    mentioned: bool = False
    replied_to_bot: bool = False
    bot_user_id: str = ""
    audio: bytes = b""
    audio_type: str = ""
    image: bytes = b""
    image_type: str = ""


@dataclass
class OutboundReply:
    text: str = ""
    segments: list[dict] = field(default_factory=list)
    audio: bytes | None = None
    ignored: bool = False
    reason: str = ""
    error: str = ""


class RateLimiter:
    def __init__(self, max_events: int, window_seconds: float) -> None:
        self.max_events = max_events
        self.window_seconds = window_seconds
        self._hits: dict[str, list[float]] = {}

    def allow(self, key: str, now: float | None = None) -> bool:
        stamp = time.monotonic() if now is None else now
        window_start = stamp - self.window_seconds
        hits = [item for item in self._hits.get(key, []) if item > window_start]
        if len(hits) >= self.max_events:
            self._hits[key] = hits
            return False
        hits.append(stamp)
        self._hits[key] = hits
        return True


def _strip_bot_mention(text: str, bot_user_id: str) -> str:
    if not bot_user_id:
        return text.strip()
    cleaned = re.sub(rf"<@!?{re.escape(bot_user_id)}>", "", text)
    return cleaned.strip()


async def process(
    bridge,
    config: DiscordConfig,
    event: InboundEvent,
    limiter: RateLimiter,
    *,
    language: str = "",
) -> OutboundReply | None:
    route = route_for(
        config,
        is_dm=not event.guild_id,
        author_id=event.author_id,
        guild_id=event.guild_id,
        channel_id=event.channel_id,
        mentioned=event.mentioned,
        replied_to_bot=event.replied_to_bot,
    )
    if route is None:
        return None
    if not limiter.allow(event.author_id):
        log.info("Discord turn dropped: rate limited")
        return None

    text = _strip_bot_mention(event.text, event.bot_user_id)
    lang = language or getattr(bridge.config, "DEFAULT_LANGUAGE", "en")
    if event.audio:
        transcript = await transcribe(
            bridge, config, event.audio, event.audio_type, lang
        )
        if not text:
            text = transcript
    image_bytes = event.image if config.DISCORD_VISION_ENABLED else b""
    image_mime = event.image_type if image_bytes else ""
    if not text:
        return None

    join_owner = route == ROUTE_OWNER_DM
    if route == ROUTE_CHANNEL:
        thread_kind = "channel"
        thread_id = event.channel_id
    else:
        thread_kind = "dm"
        thread_id = event.author_id

    done = await bridge.run_external_turn(
        platform=PLATFORM,
        external_id=event.author_id,
        text=text,
        language=lang,
        thread_kind=thread_kind,
        thread_id=thread_id,
        join_owner_thread=join_owner,
        image_bytes=image_bytes or None,
        image_mime=image_mime,
    )
    if done.get("type") == "error":
        code = (done.get("error") or {}).get("code", "error")
        log.warning("Discord turn error code=%s", code)
        return OutboundReply(error=str(code), ignored=True)

    segments = list(done.get("segments") or [])
    reply_text = str(done.get("text") or "")
    audio = None
    if reply_text and not done.get("ignored"):
        audio = await synthesize(bridge, config, segments)
    return OutboundReply(
        text=reply_text,
        segments=segments,
        audio=audio,
        ignored=bool(done.get("ignored")),
        reason=str(done.get("reason") or ""),
    )
