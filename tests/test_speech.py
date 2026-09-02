"""Speech provider tests with mocked httpx transports (plan section 14).

No network and no live provider keys. Covers the shared audio validation
contract, Deepgram/AssemblyAI STT behavior, ElevenLabs TTS with voice-profile
overlay, and the static-lines loader.
"""

from __future__ import annotations

import base64
import json
import tempfile
import unittest
from pathlib import Path

import httpx

from core.config import Config
from core.speech import (
    AudioValidationError,
    SpeechProviderError,
    STTService,
    TTSError,
    TTSService,
    VoiceProfileError,
    decode_audio,
    load_voice_profile,
    sniff_audio_type,
)
from core.static_lines import (
    StaticLinesError,
    get_static_line,
    load_static_lines,
)

from fakes import make_config

ALLOWED = {"audio/webm", "audio/ogg", "audio/mpeg", "audio/wav"}

WEBM = b"\x1a\x45\xdf\xa3" + b"0" * 8
OGG = b"OggS" + b"0" * 8
WAV = b"RIFF\x00\x00\x00\x00WAVE" + b"0" * 8
MP3_ID3 = b"ID3\x04\x00" + b"0" * 8
MP3_SYNC = b"\xff\xfb\x90\x00" + b"0" * 8


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


class Responder:
    """Records requests and replays queued httpx responses in order."""

    def __init__(self, responses: list[httpx.Response]):
        self._responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if not self._responses:
            return httpx.Response(500, text="no scripted response")
        return self._responses.pop(0)


class SniffTest(unittest.TestCase):
    def test_known_signatures(self):
        self.assertEqual(sniff_audio_type(WEBM), "audio/webm")
        self.assertEqual(sniff_audio_type(OGG), "audio/ogg")
        self.assertEqual(sniff_audio_type(WAV), "audio/wav")
        self.assertEqual(sniff_audio_type(MP3_ID3), "audio/mpeg")
        self.assertEqual(sniff_audio_type(MP3_SYNC), "audio/mpeg")

    def test_unknown_signature(self):
        self.assertIsNone(sniff_audio_type(b"RANDOMBYTES!"))


class DecodeAudioTest(unittest.TestCase):
    def test_raw_base64(self):
        data, ctype = decode_audio(
            b64(WEBM), "audio/webm", max_bytes=100, allowed_types=ALLOWED
        )
        self.assertEqual(data, WEBM)
        self.assertEqual(ctype, "audio/webm")

    def test_matching_data_uri(self):
        payload = b64(OGG)
        uri = f"data:audio/ogg;base64,{payload}"
        data, ctype = decode_audio(uri, "audio/ogg", max_bytes=100, allowed_types=ALLOWED)
        self.assertEqual(data, OGG)
        self.assertEqual(ctype, "audio/ogg")

    def test_data_uri_without_declared_type(self):
        uri = f"data:audio/wav;base64,{b64(WAV)}"
        data, ctype = decode_audio(uri, "", max_bytes=100, allowed_types=ALLOWED)
        self.assertEqual(ctype, "audio/wav")

    def test_data_uri_type_mismatch_rejected(self):
        uri = f"data:audio/ogg;base64,{b64(OGG)}"
        with self.assertRaises(AudioValidationError) as ctx:
            decode_audio(uri, "audio/webm", max_bytes=100, allowed_types=ALLOWED)
        self.assertEqual(ctx.exception.code, "unsupported_audio_type")

    def test_non_base64_data_uri_rejected(self):
        with self.assertRaises(AudioValidationError) as ctx:
            decode_audio("data:audio/ogg,abcd", "audio/ogg", max_bytes=100, allowed_types=ALLOWED)
        self.assertEqual(ctx.exception.code, "invalid_audio")

    def test_disallowed_content_type_rejected(self):
        with self.assertRaises(AudioValidationError) as ctx:
            decode_audio(b64(b"0000"), "audio/flac", max_bytes=100, allowed_types=ALLOWED)
        self.assertEqual(ctx.exception.code, "unsupported_audio_type")

    def test_invalid_base64_rejected(self):
        with self.assertRaises(AudioValidationError) as ctx:
            decode_audio("!!!not-base64!!!", "audio/webm", max_bytes=100, allowed_types=ALLOWED)
        self.assertEqual(ctx.exception.code, "invalid_audio")

    def test_empty_payload_rejected(self):
        with self.assertRaises(AudioValidationError) as ctx:
            decode_audio(b64(b""), "audio/webm", max_bytes=100, allowed_types=ALLOWED)
        self.assertEqual(ctx.exception.code, "invalid_audio")

    def test_oversized_rejected(self):
        with self.assertRaises(AudioValidationError) as ctx:
            decode_audio(b64(WEBM), "audio/webm", max_bytes=4, allowed_types=ALLOWED)
        self.assertEqual(ctx.exception.code, "audio_too_large")

    def test_signature_mismatch_rejected(self):
        with self.assertRaises(AudioValidationError) as ctx:
            decode_audio(b64(OGG), "audio/webm", max_bytes=100, allowed_types=ALLOWED)
        self.assertEqual(ctx.exception.code, "unsupported_audio_type")

    def test_unknown_signature_rejected(self):
        with self.assertRaises(AudioValidationError) as ctx:
            decode_audio(b64(b"NO SIGNATURE"), "audio/webm", max_bytes=100, allowed_types=ALLOWED)
        self.assertEqual(ctx.exception.code, "unsupported_audio_type")


class DeepgramTest(unittest.IsolatedAsyncioTestCase):
    def _config(self) -> Config:
        return make_config(STT_ENABLED=True, STT_PROVIDER="deepgram",
                           DEEPGRAM_API_KEY="dg-key", DEEPGRAM_MODEL="nova-3")

    def _service(self, responses: list[httpx.Response]) -> tuple[STTService, Responder]:
        responder = Responder(responses)
        client = httpx.AsyncClient(transport=httpx.MockTransport(responder))
        return STTService(self._config(), client=client), responder

    async def test_transcribe_success(self):
        service, responder = self._service(
            [httpx.Response(200, json={"results": {"channels": [
                {"alternatives": [{"transcript": "hello there"}]}]}})]
        )
        try:
            transcript = await service.transcribe(WEBM, "audio/webm", "en")
        finally:
            await service.aclose()
        self.assertEqual(transcript, "hello there")
        request = responder.requests[0]
        self.assertIn("model=nova-3", str(request.url))
        self.assertIn("language=en", str(request.url))
        self.assertEqual(request.headers["Authorization"], "Bearer dg-key")
        self.assertEqual(request.headers["Content-Type"], "audio/webm")
        self.assertEqual(request.content, WEBM)

    async def test_per_message_language_override(self):
        service, responder = self._service(
            [httpx.Response(200, json={"results": {"channels": [
                {"alternatives": [{"transcript": "hola"}]}]}})]
        )
        try:
            await service.transcribe(OGG, "audio/ogg", "es")
        finally:
            await service.aclose()
        self.assertIn("language=es", str(responder.requests[0].url))

    async def test_no_transcript_returns_empty_string(self):
        service, _ = self._service([httpx.Response(200, json={"results": {}})])
        try:
            transcript = await service.transcribe(WEBM, "audio/webm", "en")
        finally:
            await service.aclose()
        self.assertEqual(transcript, "")

    async def test_http_error_raises(self):
        service, _ = self._service([httpx.Response(401, json={"error": "denied"})])
        with self.assertRaises(SpeechProviderError):
            await service.transcribe(WEBM, "audio/webm", "en")
        await service.aclose()

    async def test_malformed_json_raises(self):
        service, _ = self._service([httpx.Response(200, text="not json")])
        with self.assertRaises(SpeechProviderError):
            await service.transcribe(WEBM, "audio/webm", "en")
        await service.aclose()

    async def test_non_object_response_raises(self):
        service, _ = self._service([httpx.Response(200, json=["nope"])])
        with self.assertRaises(SpeechProviderError):
            await service.transcribe(WEBM, "audio/webm", "en")
        await service.aclose()

    def test_available_requires_key(self):
        self.assertTrue(STTService(self._config()).available())
        no_key = make_config(STT_ENABLED=True, STT_PROVIDER="deepgram",
                             DEEPGRAM_API_KEY="")
        self.assertFalse(STTService(no_key).available())
        disabled = make_config(STT_ENABLED=False, STT_PROVIDER="deepgram",
                               DEEPGRAM_API_KEY="dg-key")
        self.assertFalse(STTService(disabled).available())


class AssemblyAITest(unittest.IsolatedAsyncioTestCase):
    def _config(self, **extra) -> Config:
        base = {
            "STT_ENABLED": True,
            "STT_PROVIDER": "assemblyai",
            "ASSEMBLYAI_API_KEY": "aai-key",
            "ASSEMBLYAI_SPEECH_MODEL": "best",
            "ASSEMBLYAI_TIMEOUT": 1.0,
            "ASSEMBLYAI_POLL_INTERVAL": 0.01,
        }
        base.update(extra)
        return make_config(**base)

    def _service(self, responses, config=None):
        responder = Responder(responses)
        client = httpx.AsyncClient(transport=httpx.MockTransport(responder))
        return STTService(config or self._config(), client=client), responder

    async def test_upload_submit_poll_flow(self):
        service, responder = self._service(
            [
                httpx.Response(200, json={"upload_url": "https://cdn/x"}),
                httpx.Response(200, json={"id": "tid-1"}),
                httpx.Response(200, json={"status": "processing"}),
                httpx.Response(200, json={"status": "completed", "text": "hi from aai"}),
            ]
        )
        try:
            transcript = await service.transcribe(WAV, "audio/wav", "en")
        finally:
            await service.aclose()
        self.assertEqual(transcript, "hi from aai")
        urls = [str(request.url) for request in responder.requests]
        self.assertTrue(urls[0].endswith("/upload"))
        self.assertTrue(urls[1].endswith("/transcript"))
        self.assertTrue(urls[2].endswith("/transcript/tid-1"))
        submit_body = json.loads(responder.requests[1].content)
        self.assertEqual(submit_body["audio_url"], "https://cdn/x")
        self.assertEqual(submit_body["speech_model"], "best")
        self.assertEqual(submit_body["language_code"], "en")
        self.assertEqual(responder.requests[0].headers["authorization"], "aai-key")

    async def test_error_status_raises(self):
        service, _ = self._service(
            [
                httpx.Response(200, json={"upload_url": "https://cdn/x"}),
                httpx.Response(200, json={"id": "tid-2"}),
                httpx.Response(200, json={"status": "error", "error": "bad audio"}),
            ]
        )
        with self.assertRaises(SpeechProviderError):
            await service.transcribe(WAV, "audio/wav", "en")
        await service.aclose()

    async def test_poll_timeout_raises(self):
        service, _ = self._service(
            [
                httpx.Response(200, json={"upload_url": "https://cdn/x"}),
                httpx.Response(200, json={"id": "tid-3"}),
                httpx.Response(200, json={"status": "processing"}),
                httpx.Response(200, json={"status": "processing"}),
                httpx.Response(200, json={"status": "processing"}),
            ],
            config=self._config(ASSEMBLYAI_TIMEOUT=0.05),
        )
        with self.assertRaises(SpeechProviderError):
            await service.transcribe(WAV, "audio/wav", "en")
        await service.aclose()

    async def test_completed_with_null_text_returns_empty(self):
        service, _ = self._service(
            [
                httpx.Response(200, json={"upload_url": "https://cdn/x"}),
                httpx.Response(200, json={"id": "tid-4"}),
                httpx.Response(200, json={"status": "completed", "text": None}),
            ]
        )
        try:
            transcript = await service.transcribe(WAV, "audio/wav", "en")
        finally:
            await service.aclose()
        self.assertEqual(transcript, "")


class TTSServiceTest(unittest.IsolatedAsyncioTestCase):
    def _config(self, **extra) -> Config:
        base = {
            "TTS_ENABLED": True,
            "ELEVENLABS_API_KEY": "el-key",
            "ELEVENLABS_VOICE_ID": "voice-9",
            "ELEVENLABS_MODEL": "eleven_flash_v2_5",
            "TTS_OUTPUT_FORMAT": "mp3_44100_128",
        }
        base.update(extra)
        return make_config(**base)

    def _service(self, responses, config=None):
        responder = Responder(responses)
        client = httpx.AsyncClient(transport=httpx.MockTransport(responder))
        return TTSService(config or self._config(), client=client), responder

    async def test_synthesize_success(self):
        service, responder = self._service([httpx.Response(200, content=b"MP3DATA")])
        try:
            audio = await service.synthesize("Hello there.", "happy")
        finally:
            await service.aclose()
        self.assertEqual(audio, b"MP3DATA")
        request = responder.requests[0]
        self.assertIn("/text-to-speech/voice-9", str(request.url))
        self.assertIn("output_format=mp3_44100_128", str(request.url))
        self.assertEqual(request.headers["xi-api-key"], "el-key")
        body = json.loads(request.content)
        self.assertEqual(body["text"], "Hello there.")
        self.assertEqual(body["model_id"], "eleven_flash_v2_5")
        self.assertEqual(
            body["voice_settings"],
            {"stability": 0.5, "similarity_boost": 0.75, "style": 0.0, "speed": 1.0},
        )

    async def test_voice_settings_overlay_order(self):
        service, responder = self._service(
            [httpx.Response(200, content=b"X"), httpx.Response(200, content=b"X")]
        )
        service.attach_manifest(
            {"emotions": [{"name": "sad", "tts_speed": 0.9}, {"name": "happy", "tts_speed": 1.1}]}
        )
        service.set_voice_profile(
            {
                "version": 1,
                "default": {"stability": 0.7, "similarity": 0.6},
                "emotions": {"happy": {"speed": 1.2, "style": 0.3}},
            }
        )
        try:
            await service.synthesize("a", "happy")
            await service.synthesize("b", "sad")
        finally:
            await service.aclose()
        happy, sad = (json.loads(r.content)["voice_settings"] for r in responder.requests)
        # happy: manifest speed overridden by the emotion entry; other fields
        # overlay the profile default.
        self.assertEqual(
            happy, {"stability": 0.7, "similarity_boost": 0.6, "style": 0.3, "speed": 1.2}
        )
        # sad: no emotion entry -> profile default + manifest tts_speed.
        self.assertEqual(
            sad, {"stability": 0.7, "similarity_boost": 0.6, "style": 0.0, "speed": 0.9}
        )

    async def test_manifest_speed_used_without_profile(self):
        service, responder = self._service(
            [httpx.Response(200, content=b"X"), httpx.Response(200, content=b"X")]
        )
        service.attach_manifest({"emotions": [{"name": "sad", "tts_speed": 0.9}]})
        try:
            await service.synthesize("a", "sad")
            await service.synthesize("b", "happy")  # not in manifest -> 1.0
        finally:
            await service.aclose()
        speeds = [json.loads(r.content)["voice_settings"]["speed"] for r in responder.requests]
        self.assertEqual(speeds, [0.9, 1.0])

    async def test_http_error_raises_tts_error(self):
        service, _ = self._service([httpx.Response(401, json={"detail": "nope"})])
        with self.assertRaises(TTSError):
            await service.synthesize("x", "neutral")
        await service.aclose()

    async def test_empty_audio_raises_tts_error(self):
        service, _ = self._service([httpx.Response(200, content=b"")])
        with self.assertRaises(TTSError):
            await service.synthesize("x", "neutral")
        await service.aclose()

    def test_available_and_format(self):
        service = TTSService(self._config())
        self.assertTrue(service.available())
        self.assertEqual(service.audio_format, "mp3")
        missing_voice = TTSService(self._config(ELEVENLABS_VOICE_ID=""))
        self.assertFalse(missing_voice.available())
        disabled = TTSService(self._config(TTS_ENABLED=False))
        self.assertFalse(disabled.available())


class VoiceProfileTest(unittest.TestCase):
    def _write(self, data) -> str:
        tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        self.addCleanup(lambda: Path(tmp.name).unlink())
        tmp.write(json.dumps(data))
        tmp.close()
        return tmp.name

    def test_valid_profile_loads(self):
        path = self._write(
            {"version": 1, "default": {"stability": 0.4},
             "emotions": {"happy": {"style": 0.2, "speed": 1.1}}}
        )
        profile = load_voice_profile(path)
        self.assertEqual(profile["emotions"]["happy"]["speed"], 1.1)

    def test_missing_file_fails(self):
        with self.assertRaises(VoiceProfileError):
            load_voice_profile("/nonexistent/voice_profile.json")

    def test_invalid_version_fails(self):
        path = self._write({"emotions": {}})
        with self.assertRaises(VoiceProfileError):
            load_voice_profile(path)

    def test_unknown_emotion_fails(self):
        path = self._write({"version": 1, "emotions": {"jubilant": {}}})
        with self.assertRaises(VoiceProfileError):
            load_voice_profile(path)

    def test_unknown_field_fails(self):
        path = self._write({"version": 1, "emotions": {"happy": {"mystery": 1}}})
        with self.assertRaises(VoiceProfileError):
            load_voice_profile(path)

    def test_out_of_range_value_fails(self):
        path = self._write({"version": 1, "emotions": {"happy": {"speed": 5.0}}})
        with self.assertRaises(VoiceProfileError):
            load_voice_profile(path)


class StaticLinesTest(unittest.TestCase):
    def test_bundled_file_is_schema_complete_and_empty(self):
        lines = load_static_lines()
        for language in ("en", "es", "ja"):
            for key in ("busy", "unavailable", "soft_block", "stt_empty"):
                self.assertIsNone(get_static_line(lines, key, language))

    def test_override_file_loads(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "lines.json"
            path.write_text(json.dumps({
                "version": 1,
                "en": {"stt_empty": "I didn't catch that."},
                "es": {"stt_empty": "No te entendí."},
                "ja": {"stt_empty": ""},
            }))
            lines = load_static_lines(str(path))
            self.assertEqual(get_static_line(lines, "stt_empty", "en"),
                             "I didn't catch that.")
            self.assertEqual(get_static_line(lines, "stt_empty", "es"),
                             "No te entendí.")
            # Blank value = deliberate silence, no cross-language fallback.
            self.assertIsNone(get_static_line(lines, "stt_empty", "ja"))

    def test_invalid_file_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "lines.json"
            path.write_text(json.dumps({"version": 1, "en": {"stt_empty": "x"}}))
            with self.assertRaises(StaticLinesError):
                load_static_lines(str(path))

    def test_unknown_line_key_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "lines.json"
            path.write_text(json.dumps({
                "version": 1,
                "en": {"greeting": "hi"}, "es": {}, "ja": {},
            }))
            with self.assertRaises(StaticLinesError):
                load_static_lines(str(path))


if __name__ == "__main__":
    unittest.main()
