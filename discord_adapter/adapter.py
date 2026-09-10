"""Discord gateway + REST. Outbound only. Token never logged."""

from __future__ import annotations

import asyncio
import io
import logging
from typing import Any

from .config import DiscordConfig
from .media import MAX_MEDIA_BYTES
from .turns import InboundEvent, OutboundReply, RateLimiter, process

log = logging.getLogger("bridge.discord")


class Adapter:
    def __init__(self, config: DiscordConfig) -> None:
        self.config = config
        self.bridge: Any = None
        self.limiter = RateLimiter(
            config.DISCORD_RATE_MAX, float(config.DISCORD_RATE_WINDOW_SECONDS)
        )
        self._client: Any = None
        self._task: asyncio.Task | None = None

    async def start(self, bridge: Any) -> None:
        self.bridge = bridge
        token = self.config.DISCORD_BOT_TOKEN
        if not token:
            log.error("Discord adapter enabled but token missing")
            return
        try:
            import discord
        except ImportError:
            log.error("discord.py is not installed; adapter idle")
            return
        intents = discord.Intents.default()
        intents.message_content = True
        intents.messages = True
        intents.dm_messages = True
        client = discord.Client(intents=intents)
        adapter = self

        @client.event
        async def on_ready() -> None:
            user = client.user
            log.info("Discord adapter ready id=%s", getattr(user, "id", "?"))

        @client.event
        async def on_message(message: Any) -> None:
            await adapter._on_message(message)

        self._client = client
        self._task = asyncio.create_task(client.start(token))

    async def stop(self) -> None:
        client = self._client
        self._client = None
        if client is not None:
            try:
                await client.close()
            except Exception:
                log.warning("Discord adapter close failed", exc_info=True)
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    async def _on_message(self, message: Any) -> None:
        if self.bridge is None:
            return
        author = getattr(message, "author", None)
        if author is None:
            return
        if getattr(author, "bot", False):
            return
        bot_user = getattr(self._client, "user", None)
        bot_id = str(getattr(bot_user, "id", "") or "")
        author_id = str(getattr(author, "id", "") or "")
        if bot_id and author_id == bot_id:
            return
        guild = getattr(message, "guild", None)
        channel = getattr(message, "channel", None)
        mentions = list(getattr(message, "mentions", []) or [])
        mentioned = any(str(getattr(user, "id", "")) == bot_id for user in mentions)
        replied = _replied_to_bot(message, bot_id)
        audio, audio_type, image, image_type = await _read_attachments(message)
        event = InboundEvent(
            author_id=author_id,
            channel_id=str(getattr(channel, "id", "") or ""),
            guild_id=str(getattr(guild, "id", "") or ""),
            text=str(getattr(message, "content", "") or ""),
            mentioned=mentioned,
            replied_to_bot=replied,
            bot_user_id=bot_id,
            audio=audio,
            audio_type=audio_type,
            image=image,
            image_type=image_type,
        )
        try:
            reply = await process(self.bridge, self.config, event, self.limiter)
        except Exception:
            log.warning("Discord turn failed", exc_info=True)
            return
        if reply is None or reply.error:
            return
        await _send_reply(message, reply)


def _replied_to_bot(message: Any, bot_id: str) -> bool:
    if not bot_id:
        return False
    reference = getattr(message, "reference", None)
    if reference is None:
        return False
    resolved = getattr(reference, "resolved", None)
    resolved_author = getattr(resolved, "author", None) if resolved is not None else None
    if resolved_author is not None:
        return str(getattr(resolved_author, "id", "")) == bot_id
    return False


async def _read_attachments(message: Any) -> tuple[bytes, str, bytes, str]:
    audio = b""
    audio_type = ""
    image = b""
    image_type = ""
    attachments = list(getattr(message, "attachments", []) or [])
    for attachment in attachments:
        size = int(getattr(attachment, "size", 0) or 0)
        if size > MAX_MEDIA_BYTES:
            continue
        content_type = str(getattr(attachment, "content_type", "") or "")
        try:
            payload = await attachment.read()
        except Exception:
            log.warning("Discord attachment download failed", exc_info=True)
            continue
        if not payload or len(payload) > MAX_MEDIA_BYTES:
            continue
        if content_type.startswith("audio/") and not audio:
            audio, audio_type = payload, content_type
        elif content_type.startswith("image/") and not image:
            image, image_type = payload, content_type
    return audio, audio_type, image, image_type


async def _send_reply(message: Any, reply: OutboundReply) -> None:
    text = (reply.text or "").strip()
    if reply.ignored and not text:
        return
    if not text and reply.audio is None:
        return
    kwargs: dict[str, Any] = {}
    if text:
        kwargs["content"] = text[:2000]
    if reply.audio:
        try:
            import discord
        except ImportError:
            discord = None  # type: ignore[assignment]
        if discord is not None:
            kwargs["file"] = discord.File(io.BytesIO(reply.audio), filename="voice.mp3")
    channel = getattr(message, "channel", None)
    send = getattr(channel, "send", None)
    if send is None:
        return
    try:
        await send(**kwargs)
    except Exception:
        log.warning("Discord reply send failed", exc_info=True)
