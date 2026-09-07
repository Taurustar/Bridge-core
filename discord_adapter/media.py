"""Voice-message STT/TTS and optional image pass-through. No live voice channels."""

from __future__ import annotations

import logging
from typing import Any

from .config import DiscordConfig

log = logging.getLogger("bridge.discord.media")

MAX_MEDIA_BYTES = 8 * 1024 * 1024


async def transcribe(
    bridge: Any, config: DiscordConfig, audio: bytes, content_type: str, language: str
) -> str:
    if not config.DISCORD_STT_ENABLED or not audio:
        return ""
    stt = getattr(bridge, "stt", None)
    if stt is None or not stt.available():
        return ""
    mime = content_type.strip() or "audio/ogg"
    try:
        return (await stt.transcribe(audio, mime, language)).strip()
    except Exception:
        log.warning("Discord STT failed", exc_info=True)
        return ""


async def synthesize(bridge: Any, config: DiscordConfig, segments: list[dict]) -> bytes | None:
    if not config.DISCORD_TTS_ENABLED or not segments:
        return None
    tts = getattr(bridge, "tts", None)
    if tts is None or not tts.available():
        return None
    chunks: list[bytes] = []
    same_run = 0
    last_emotion = ""
    for segment in segments:
        text = str(segment.get("text") or "").strip()
        emotion = str(segment.get("emotion") or "neutral")
        if not text:
            continue
        if emotion == last_emotion:
            same_run += 1
        else:
            same_run = 0
            last_emotion = emotion
        try:
            chunks.append(await tts.synthesize(text, emotion, same_run))
        except Exception:
            log.warning("Discord TTS chunk failed", exc_info=True)
    if not chunks:
        return None
    return b"".join(chunks)
