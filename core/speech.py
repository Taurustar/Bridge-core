"""Speech providers: ElevenLabs TTS, Deepgram/AssemblyAI STT (plan section 14).

- One shared async httpx client per service; never ``requests`` or sync I/O.
- Audio bytes are never stored server-side and never logged; failures and
  completions log bounded metadata only (plan sections 6.6, 14).
- ``decode_audio`` enforces the shared validation contract of plan 14.2/14.3
  for both STT providers: allowed content types only, raw base64 or a
  matching data URI, decoded size against ``MAX_AUDIO_BYTES``, and container
  signature sniffing — declared MIME is never trusted alone.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import time
from pathlib import Path
from typing import Any

import httpx

from .config import Config
from .constants import FINAL_EMOTIONS

log = logging.getLogger("bridge.speech")

# Per-request ElevenLabs timeout. No environment knob exists for it in the
# plan inventory; documented in BRIDGE_CORE_ENGINE_SPEC.md.
TTS_REQUEST_TIMEOUT_SECONDS = 60.0

_AAI_PER_REQUEST_TIMEOUT_SECONDS = 30.0


class AudioValidationError(ValueError):
    """Inbound audio failed validation. ``code`` is a stable error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class SpeechProviderError(RuntimeError):
    """A speech provider call failed (transport, HTTP, or malformed response)."""


class TTSError(SpeechProviderError):
    """TTS synthesis failed for one chunk; the text reply stands."""


class VoiceProfileError(RuntimeError):
    """The configured TTS voice profile is missing or invalid."""


# ---------------------------------------------------------------------------
# Inbound audio validation (plan sections 14.2, 14.3)
# ---------------------------------------------------------------------------


def sniff_audio_type(data: bytes) -> str | None:
    """Best-effort container signature check. None when unrecognized."""
    if data.startswith(b"\x1a\x45\xdf\xa3"):  # EBML header (WebM)
        return "audio/webm"
    if data.startswith(b"OggS"):
        return "audio/ogg"
    if data.startswith(b"RIFF") and len(data) >= 12 and data[8:12] == b"WAVE":
        return "audio/wav"
    if data.startswith(b"ID3"):
        return "audio/mpeg"
    if len(data) >= 2 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0:
        return "audio/mpeg"  # MPEG frame sync (11 bits)
    return None


def decode_audio(
    audio_base64: str,
    declared_content_type: str,
    *,
    max_bytes: int,
    allowed_types: set[str],
) -> tuple[bytes, str]:
    """Validate and decode an inbound audio payload.

    Accepts raw base64 or a ``data:<mime>[;base64],<payload>`` URI whose mime
    must match the declared content type. Rejects disallowed types, invalid
    base64, empty payloads, oversized payloads, and containers whose magic
    bytes do not match the declared type. Returns ``(bytes, content_type)``.
    """
    text = audio_base64.strip()
    content_type = (declared_content_type or "").strip().lower()

    if text.startswith("data:"):
        header, _, payload = text.partition(",")
        if not payload:
            raise AudioValidationError("invalid_audio", "Audio data URI has no payload.")
        meta = header[5:]
        mime = meta.split(";")[0].strip().lower()
        if "base64" not in meta.lower():
            raise AudioValidationError("invalid_audio", "Audio data URI must be base64-encoded.")
        if mime:
            if content_type and mime != content_type:
                raise AudioValidationError(
                    "unsupported_audio_type",
                    "Declared audio content type does not match the data URI type.",
                )
            content_type = mime
        text = payload

    if not content_type:
        raise AudioValidationError("invalid_audio", "Missing audio content type.")
    if content_type not in allowed_types:
        raise AudioValidationError(
            "unsupported_audio_type", f"Audio content type is not allowed: {content_type}"
        )

    try:
        data = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError):
        raise AudioValidationError(
            "invalid_audio", "Audio payload is not valid base64."
        ) from None
    if not data:
        raise AudioValidationError("invalid_audio", "Audio payload is empty.")
    if len(data) > max_bytes:
        raise AudioValidationError(
            "audio_too_large", f"Audio payload exceeds the maximum of {max_bytes} bytes."
        )

    sniffed = sniff_audio_type(data)
    if sniffed is None or sniffed != content_type:
        raise AudioValidationError(
            "unsupported_audio_type",
            "Audio container signature does not match the declared content type.",
        )
    return data, content_type


# ---------------------------------------------------------------------------
# STT (plan sections 14.2, 14.3)
# ---------------------------------------------------------------------------


def _deepgram_transcript(data: dict) -> str:
    """Defensive walk of a Deepgram pre-recorded response; '' when absent."""
    results = data.get("results")
    if not isinstance(results, dict):
        return ""
    channels = results.get("channels")
    if not isinstance(channels, list) or not channels:
        return ""
    first = channels[0]
    if not isinstance(first, dict):
        return ""
    alternatives = first.get("alternatives")
    if not isinstance(alternatives, list) or not alternatives:
        return ""
    alternative = alternatives[0]
    if not isinstance(alternative, dict):
        return ""
    transcript = alternative.get("transcript")
    return transcript.strip() if isinstance(transcript, str) else ""


class STTService:
    """Speech-to-text routing for Deepgram and AssemblyAI."""

    def __init__(self, config: Config, client: httpx.AsyncClient | None = None) -> None:
        self._config = config
        self._client = client or httpx.AsyncClient()
        self._owns_client = client is None

    @property
    def provider_name(self) -> str:
        return self._config.STT_PROVIDER

    def available(self) -> bool:
        """Enabled and its provider credentials configured."""
        cfg = self._config
        if not cfg.STT_ENABLED:
            return False
        if cfg.STT_PROVIDER == "deepgram":
            return bool(cfg.DEEPGRAM_API_KEY.strip())
        if cfg.STT_PROVIDER == "assemblyai":
            return bool(cfg.ASSEMBLYAI_API_KEY.strip())
        return False

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def transcribe(self, audio: bytes, content_type: str, language: str) -> str:
        """Transcribe decoded audio bytes.

        Returns ``""`` when the provider succeeded with no transcript; raises
        ``SpeechProviderError`` on transport/HTTP/malformed-response failure.
        """
        if self._config.STT_PROVIDER == "assemblyai":
            return await self._transcribe_assemblyai(audio, language)
        return await self._transcribe_deepgram(audio, content_type, language)

    # -- Deepgram ------------------------------------------------------------

    async def _transcribe_deepgram(
        self, audio: bytes, content_type: str, language: str
    ) -> str:
        cfg = self._config
        params: dict[str, str] = {"model": cfg.DEEPGRAM_MODEL}
        if language.strip():
            params["language"] = language.strip()
        headers = {
            "Authorization": f"Bearer {cfg.DEEPGRAM_API_KEY.strip()}",
            "Content-Type": content_type,
        }
        try:
            response = await self._client.post(
                cfg.DEEPGRAM_URL,
                params=params,
                content=audio,
                headers=headers,
                timeout=cfg.DEEPGRAM_TIMEOUT,
            )
        except httpx.HTTPError as exc:
            raise SpeechProviderError(
                f"deepgram request failed: {type(exc).__name__}"
            ) from None
        data = _json_object(response, "deepgram")
        return _deepgram_transcript(data)

    # -- AssemblyAI ----------------------------------------------------------

    async def _transcribe_assemblyai(self, audio: bytes, language: str) -> str:
        cfg = self._config
        base = (cfg.ASSEMBLYAI_URL or "").rstrip("/")
        headers = {"authorization": cfg.ASSEMBLYAI_API_KEY.strip()}

        upload_url = await self._aai_upload(base, headers, audio)
        transcript_id = await self._aai_submit(base, headers, upload_url, language)
        return await self._aai_poll(base, headers, transcript_id)

    async def _aai_upload(
        self, base: str, headers: dict[str, str], audio: bytes
    ) -> str:
        try:
            response = await self._client.post(
                f"{base}/upload",
                content=audio,
                headers={**headers, "Content-Type": "application/octet-stream"},
                timeout=_AAI_PER_REQUEST_TIMEOUT_SECONDS,
            )
        except httpx.HTTPError as exc:
            raise SpeechProviderError(
                f"assemblyai upload failed: {type(exc).__name__}"
            ) from None
        data = _json_object(response, "assemblyai upload")
        upload_url = data.get("upload_url")
        if not isinstance(upload_url, str) or not upload_url.strip():
            raise SpeechProviderError("assemblyai upload returned no upload_url")
        return upload_url.strip()

    async def _aai_submit(
        self, base: str, headers: dict[str, str], upload_url: str, language: str
    ) -> str:
        cfg = self._config
        body: dict[str, Any] = {
            "audio_url": upload_url,
            "speech_model": cfg.ASSEMBLYAI_SPEECH_MODEL,
        }
        if language.strip():
            body["language_code"] = language.strip()
        try:
            response = await self._client.post(
                f"{base}/transcript",
                json=body,
                headers=headers,
                timeout=_AAI_PER_REQUEST_TIMEOUT_SECONDS,
            )
        except httpx.HTTPError as exc:
            raise SpeechProviderError(
                f"assemblyai submit failed: {type(exc).__name__}"
            ) from None
        data = _json_object(response, "assemblyai submit")
        transcript_id = data.get("id")
        if not isinstance(transcript_id, str) or not transcript_id.strip():
            raise SpeechProviderError("assemblyai submit returned no transcript id")
        return transcript_id.strip()

    async def _aai_poll(
        self, base: str, headers: dict[str, str], transcript_id: str
    ) -> str:
        cfg = self._config
        url = f"{base}/transcript/{transcript_id}"
        deadline = time.monotonic() + cfg.ASSEMBLYAI_TIMEOUT
        interval = max(cfg.ASSEMBLYAI_POLL_INTERVAL, 0.01)
        while True:
            try:
                response = await self._client.get(
                    url, headers=headers, timeout=_AAI_PER_REQUEST_TIMEOUT_SECONDS
                )
            except httpx.HTTPError as exc:
                raise SpeechProviderError(
                    f"assemblyai poll failed: {type(exc).__name__}"
                ) from None
            data = _json_object(response, "assemblyai poll")
            status = data.get("status")
            if status == "completed":
                text = data.get("text")
                return text.strip() if isinstance(text, str) else ""
            if status == "error":
                raise SpeechProviderError("assemblyai transcript job failed")
            if time.monotonic() >= deadline:
                raise SpeechProviderError("assemblyai transcript timed out")
            await asyncio.sleep(interval)


def _json_object(response: httpx.Response, provider: str) -> dict:
    """Shared provider-response validation (never assume shapes)."""
    if response.status_code != 200:
        raise SpeechProviderError(f"{provider} returned HTTP {response.status_code}")
    try:
        data = response.json()
    except json.JSONDecodeError:
        raise SpeechProviderError(f"{provider} returned malformed JSON") from None
    if not isinstance(data, dict):
        raise SpeechProviderError(f"{provider} returned a non-object response")
    return data


# ---------------------------------------------------------------------------
# TTS (plan section 14.1)
# ---------------------------------------------------------------------------

_PROFILE_FIELDS = frozenset(
    {"stability", "similarity", "similarity_boost", "style", "speed", "use_speaker_boost"}
)
_FINAL_EMOTION_SET = frozenset(FINAL_EMOTIONS)

# Built-in neutral defaults; the emotions-manifest tts_speed seeds ``speed``.
_DEFAULT_VOICE_SETTINGS: dict[str, Any] = {
    "stability": 0.5,
    "similarity_boost": 0.75,
    "style": 0.0,
}


def load_voice_profile(path: str) -> dict:
    """Load and validate the configured voice-profile JSON file."""
    profile_path = Path(path)
    try:
        data = json.loads(profile_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise VoiceProfileError(f"Voice profile file not found: {profile_path}") from None
    except json.JSONDecodeError as exc:
        raise VoiceProfileError(
            f"Voice profile is not valid JSON: {profile_path}: {exc}"
        ) from None
    validate_voice_profile(data, source=str(profile_path))
    return data


def validate_voice_profile(data: object, source: str = "<voice profile>") -> None:
    if not isinstance(data, dict) or not isinstance(data.get("version"), int):
        raise VoiceProfileError(f"{source}: requires an integer 'version'")
    default = data.get("default")
    if default is not None:
        _validate_profile_entry(default, "default", source)
    emotions = data.get("emotions")
    if emotions is not None:
        if not isinstance(emotions, dict):
            raise VoiceProfileError(f"{source}: 'emotions' must be an object")
        for name, entry in emotions.items():
            if name not in _FINAL_EMOTION_SET:
                raise VoiceProfileError(
                    f"{source}: unknown emotion {name!r} (must use the wire palette)"
                )
            _validate_profile_entry(entry, name, source)


def _validate_profile_entry(entry: object, label: str, source: str) -> None:
    if not isinstance(entry, dict):
        raise VoiceProfileError(f"{source}: emotion {label!r} must be an object")
    for key, value in entry.items():
        if key not in _PROFILE_FIELDS:
            raise VoiceProfileError(f"{source}: unknown voice-profile field {key!r}")
        if key == "use_speaker_boost":
            if not isinstance(value, bool):
                raise VoiceProfileError(
                    f"{source}: {label}.{key} must be a boolean"
                )
            continue
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise VoiceProfileError(f"{source}: {label}.{key} must be a number")
        if key == "speed":
            if not 0.25 <= float(value) <= 2.0:
                raise VoiceProfileError(
                    f"{source}: {label}.{key} must be within 0.25..2.0"
                )
        elif not 0.0 <= float(value) <= 1.0:
            raise VoiceProfileError(
                f"{source}: {label}.{key} must be within 0.0..1.0"
            )


class TTSService:
    """ElevenLabs text-to-speech with emotion-aware voice settings."""

    def __init__(self, config: Config, client: httpx.AsyncClient | None = None) -> None:
        self._config = config
        self._client = client or httpx.AsyncClient()
        self._owns_client = client is None
        self._voice_profile: dict = {}
        self._manifest_speeds: dict[str, float] = {}

    def available(self) -> bool:
        """Enabled with voice id and API key configured."""
        cfg = self._config
        return bool(
            cfg.TTS_ENABLED
            and cfg.ELEVENLABS_API_KEY.strip()
            and cfg.ELEVENLABS_VOICE_ID.strip()
        )

    @property
    def audio_format(self) -> str:
        """Wire audio format derived from ``TTS_OUTPUT_FORMAT`` (e.g. mp3)."""
        return (self._config.TTS_OUTPUT_FORMAT or "mp3").split("_")[0]

    @property
    def has_voice_profile(self) -> bool:
        return bool(self._voice_profile)

    def attach_manifest(self, manifest: dict) -> None:
        """Index tts_speed per emotion from the validated emotions manifest."""
        speeds: dict[str, float] = {}
        for entry in manifest.get("emotions", []) or []:
            if isinstance(entry, dict) and entry.get("name"):
                speed = entry.get("tts_speed", 1.0)
                if isinstance(speed, (int, float)) and not isinstance(speed, bool):
                    speeds[str(entry["name"])] = float(speed)
        self._manifest_speeds = speeds

    def set_voice_profile(self, profile: dict) -> None:
        self._voice_profile = profile

    def _voice_settings(self, emotion: str) -> dict[str, Any]:
        """Resolve voice settings: manifest tts_speed -> profile default ->
        profile neutral entry -> profile emotion entry (plan 14.1)."""
        settings: dict[str, Any] = dict(_DEFAULT_VOICE_SETTINGS)
        settings["speed"] = self._manifest_speeds.get(emotion, 1.0)

        profile = self._voice_profile if isinstance(self._voice_profile, dict) else {}
        default = profile.get("default")
        entries = profile.get("emotions")
        entries = entries if isinstance(entries, dict) else {}
        sources = [
            default if isinstance(default, dict) else None,
            entries.get("neutral") if isinstance(entries.get("neutral"), dict) else None,
            entries.get(emotion) if isinstance(entries.get(emotion), dict) else None,
        ]
        for source in sources:
            if source is None:
                continue
            for key in ("stability", "similarity_boost", "style", "speed"):
                value = source.get(key)
                if key == "similarity_boost" and value is None:
                    value = source.get("similarity")
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    settings[key] = value
            boost = source.get("use_speaker_boost")
            if isinstance(boost, bool):
                settings["use_speaker_boost"] = boost
        return settings

    async def synthesize(self, text: str, emotion: str) -> bytes:
        """Synthesize one chunk. Raises ``TTSError``; the text reply stands."""
        cfg = self._config
        url = (
            f"{cfg.ELEVENLABS_URL.rstrip('/')}/text-to-speech/{cfg.ELEVENLABS_VOICE_ID.strip()}"
        )
        body = {
            "text": text,
            "model_id": cfg.ELEVENLABS_MODEL,
            "voice_settings": self._voice_settings(emotion),
        }
        headers = {
            "xi-api-key": cfg.ELEVENLABS_API_KEY.strip(),
            "Content-Type": "application/json",
            "Accept": "audio/mpeg",
        }
        try:
            response = await self._client.post(
                url,
                params={"output_format": cfg.TTS_OUTPUT_FORMAT},
                json=body,
                headers=headers,
                timeout=TTS_REQUEST_TIMEOUT_SECONDS,
            )
        except httpx.HTTPError as exc:
            raise TTSError(f"elevenlabs request failed: {type(exc).__name__}") from None
        if response.status_code != 200:
            raise TTSError(f"elevenlabs returned HTTP {response.status_code}")
        audio = response.content
        if not audio:
            raise TTSError("elevenlabs returned empty audio")
        # Metadata only: byte count and emotion, never audio or text bodies.
        log.info("TTS chunk synthesized: bytes=%d emotion=%s", len(audio), emotion)
        return audio

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
