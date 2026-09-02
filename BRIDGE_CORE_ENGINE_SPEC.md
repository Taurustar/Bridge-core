# Bridge Core Engine — Implementation Spec

Living implementation contract. It refines unspecified details of
`BRIDGE_CORE_ENGINE_IMPLEMENTATION_PLAN.md`; it may not override locked
decisions (plan section 2). Milestones implemented: **0.1.0**, **0.2.0**.

## Deviations from the plan

- **Repository**: the plan locks a new repo named `bridge-core-engine`. The
  project owner explicitly directed that all code live in the existing
  `Bridge-core/` repository instead. Everything else follows the plan.
- **Module set**: only the modules each milestone needs exist (plan section
  35 — no placeholder scaffolding). Added relative to plan section 5's full
  layout: `core/tailscale.py` (bind validation), `core/emotions.py`
  (manifest loading/validation), `core/speech.py` (TTS/STT/audio validation),
  and `core/static_lines.py` (static-lines loading). Bundled data files ship
  inside the package so they travel with installs: `core/emotions.json`
  (neutral manifest) and `core/static_lines.json` (schema-complete, empty
  values). `EMOTIONS_FILE` / `STATIC_LINES_FILE` override them.
- **Voice profile "repeat variations"**: plan 14.1 lists "repeat variations"
  among voice-profile fields. The ElevenLabs chat-completions-era TTS API used
  here has no such parameter, so it is not implemented; the profile schema
  covers stability, similarity (or `similarity_boost`), style, speed, and
  `use_speaker_boost`.

## Protocol decisions (unspecified details, now decided)

### WebSocket

- `capabilities` in the `connected` frame is dynamic: base
  `["text", "heartbeat", "chat_sync"]`, plus `"audio"` when TTS is enabled
  and configured, plus `"voice_input"` when STT is enabled and configured.
  Work/mcp/device capabilities arrive with their milestones.
- Non-owner `user_id`: the server accepts the socket, sends a terminal
  `error` frame with code `forbidden_user`, then closes with code `4003`.
- Error frames carry the standard shape
  `{"type": "error", "error": {"code", "message", "details": {}}}`; frames
  that terminate a turn add `"terminal": true` so clients never wait for a
  `done` that will not come (plan section 30.2).
- Unknown `mode` values → terminal `unknown_mode` error frame, never silent
  companion fallback. `mode: "work"` → terminal `work_unavailable` error
  frame; work mode ships in milestone 0.5.0. This applies to both `text` and
  `audio` frames.
- Empty/whitespace `text` → terminal `empty_input` error frame; no LLM call
  and no history write (plan section 12 step 3).
- Explicit `language` outside {en, es, ja} → terminal `unsupported_language`
  error frame (text frames, audio frames, and `POST /message`). An absent or
  empty field is not an error — the pin resolves by fallback (below).
- **Status frames** (plan 10.4): a `status` frame with `status: "thinking"`
  is sent to the source connection when a companion turn begins, before the
  per-owner turn lock is acquired. Status frames go only to the source
  connection; status emotions never become final reply emotions. Status
  `message` values are bounded, display-safe engine/UI text ("Thinking"),
  never character voice.

### Heartbeats

- `initiative_counter` in `heartbeat_ack` is the constant `0`. The initiative
  engine ships in milestone 0.7.0; the field exists now for wire stability.
- `heartbeat_ack` carries an extra boolean `counted`: `false` for
  replayed/out-of-order sequences (acked but flagged, per plan 10.8). This is
  additive metadata, not a shape change.
- Heartbeats are handled inline on the reader loop and never touch the
  per-owner turn lock; turns run in background tasks precisely so acks are
  served while a turn is active.
- Heartbeat `timezone` updates connection-local context only.

### Language pin (plan 7.4, milestone 0.2.0 scope)

Resolution for the reply language, in order:

1. Explicit per-message `language` field (validated; invalid values are
   terminal errors, not silent fallbacks).
2. For audio turns, the frame's `stt_language` when it is one of en/es/ja
   (clear inbound-language detection; other codes pass through to the STT
   provider but do not pin the reply).
3. `DEFAULT_LANGUAGE` (config-validated to en/es/ja at startup).

The pinned language is enforced by a post-history system message with the
final-reply critical rules (plan 12 step 19), and localizes STT-empty static
lines. Owner-profile preferred language joins step 2 in milestone 0.3.0.

### Voice-input (audio) turns

- An `audio` frame is validated in this order: mode → STT availability →
  `language`/`stt_language` fields → payload. New terminal error codes:
  `stt_unavailable` (STT disabled or unconfigured), `invalid_audio` (missing
  payload, invalid base64, empty decode, non-base64 data URI),
  `unsupported_audio_type` (content type not in
  `ALLOWED_AUDIO_CONTENT_TYPES`, data-URI type mismatch, or container
  signature mismatch — including unrecognized signatures, since declared
  MIME is never trusted alone), `audio_too_large` (decoded size over
  `MAX_AUDIO_BYTES`). None of these make LLM or history calls.
- STT runs inline on the reader loop; the companion turn then runs as a
  background task. Frame order on the source connection: `stt` → `status`
  (thinking) → `done` → audio chunks.
- The `stt` frame carries the transcript, provider name, and the STT language
  actually used (per-message `stt_language` overrides `STT_LANGUAGE`).
- **Empty transcript** (provider succeeded, nothing spoken): the `stt` frame
  is sent with `"text": ""`, then a terminal `done` with `ignored: true`,
  `reason: "stt_empty"`. If the owner authored an `stt_empty` static line for
  the pinned language, it is carried as `text`/`segments`; otherwise the
  done is protocol-only (no `text`/`segments` keys) — the engine never
  invents static character voice. No LLM, history, or memory call.
- **Failed STT** (transport/HTTP/malformed response): no `stt` frame; a
  terminal `done` with `ignored: true`, `reason: "stt_failed"` and the same
  static-line rules. Provider failures log bounded metadata only (exception
  summary, never audio or response bodies).
- A non-empty transcript becomes a normal text companion turn; `wants_audio`
  passes through.

### Companion turn (0.1.0 simplification of plan section 12)

Skipped hooks (they do not exist yet; flags off, no keys, no tasks, no LLM
calls): needs/bids/rhythm, schedule/availability, owner profile, memory
retrieval/extraction, life mentions, catch-up.

- User rows persist with `delivery_state: "delivered"` (they were received
  from the owner device) **before** the provider call; `chat_sync` fanout
  failure never rolls back persistence.
- Assistant rows persist `pending`, become `delivered` after the source
  `done` send succeeds (followed by assistant `chat_sync` to other devices),
  or `undelivered` on source-send failure (excluded from prompt history, no
  fanout). `delivery_unknown` is reserved for the crash-recovery
  reconciliation that ships with the history APIs milestone; 0.1.0/0.2.0
  produce no `delivery_unknown` rows.
- `message_ack` frames are accepted and idempotently ignored until that
  milestone lands.
- Emotion-only/empty provider replies are retried exactly once; usage is
  additive across the retry. If still empty: terminal `empty_reply` error.
- History rows are capped at `MAX_HISTORY_TURNS` via transactional
  `RPUSH`+`LTRIM`; prompt history is the last
  `LLM_HISTORY_MESSAGE_BUDGET` delivered rows, excluding the current user row.
- For HTTP-originated turns (`POST /message`), returning the response body is
  the delivery path, so the assistant row is marked `delivered`;
  `chat_sync.origin_connection_id` is the string `"http"`.
- `done.tokens` is emitted only when provider usage exists.

### Reply parsing and segments (plan 13.2, 12.23)

- `parse_emotion_segments` splits a reply into `{"text", "emotion"}`
  segments; text before the first `[EMOTION:]` tag becomes a neutral
  segment; per-segment emotions normalize unknown names to `neutral`;
  display text joins segments with a blank line.
- Scrubbers applied to every segment body: reasoning blocks
  (`<think>`/`<thinking>`/`<reasoning>` paired, plus an unclosed block at the very
  start which swallows the rest — such replies parse empty and take the
  retry path), unsupported uppercase control tags (`[STATUS: ...]`,
  `[TOOL: ...]`; lowercase bracketed prose like `[Note: ...]` survives), and
  asterisk roleplay actions.
- An emotion-only reply yields no segments → the single retry.

### TTS chunk stream (plan 10.6, 13.4)

- Triggered only by `wants_audio` on a delivered source-connection turn.
  TTS runs **outside** the per-owner turn lock (it mutates no shared state
  and streams only to the requesting connection); the `done` frame always
  precedes the first `audio_chunk`.
- `chunk_segments`: replies at or under `TTS_CHUNK_THRESHOLD` characters are
  not split (each segment stays its own chunk, preserving per-segment
  emotions); longer replies pack consecutive same-emotion segments up to
  `TTS_CHUNK_SIZE` and split oversized segments at sentence boundaries
  (`.!?。！？…`); a single over-long sentence is kept whole. Empty chunks are
  never synthesized.
- Generation is sequential with **one-chunk lookahead** (at most two
  syntheses in flight), so chunk order on the wire is deterministic.
  `TTS_CHUNK_SPACING_MS` paces sends between successful chunks.
- `audio_chunk` frames carry the done id, chunk text, chunk emotion,
  `chunk_index`, `total_chunks`, `is_final` (last planned chunk), base64
  audio, and `audio_format` derived from `TTS_OUTPUT_FORMAT` (e.g.
  `mp3_44100_128` → `mp3`).
- A failed synthesis skips that chunk (the text reply always stands), then
  exactly one bounded `status` (`error`) frame precedes the terminal
  `audio_complete`, which is sent exactly once with `succeeded_chunks` and
  `failed_chunks`.
- `wants_audio` with TTS unavailable: one bounded `status` (`error`) frame,
  then `audio_complete` with 0/0 — clients must treat `audio_complete` as
  the stream terminator regardless of counts.
- A failed send (disconnect) cancels pending synthesis immediately; shutdown
  cancels in-flight synthesis via the turn task's cancellation path.

### Speech providers (plan section 14)

- **Audio validation** (shared by both STT providers): allowed content types
  only; raw base64 or a matching `data:` URI (non-base64 URIs rejected);
  decoded size against `MAX_AUDIO_BYTES`; container signature sniffing
  (EBAML/WebM, OggS, RIFF/WAVE, ID3 or MPEG frame sync). Unknown signatures
  are rejected — declared MIME alone is never sufficient.
- **Deepgram**: pre-recorded endpoint with `model`/`language` params, raw
  audio body, `DEEPGRAM_TIMEOUT`. The response is walked defensively
  (`results.channels[0].alternatives[0].transcript`); a missing transcript is
  `""`, while non-200/malformed/non-object responses raise provider errors.
- **AssemblyAI**: upload bytes → submit transcript job (with
  `speech_model` and `language_code`) → poll asynchronously at
  `ASSEMBLYAI_POLL_INTERVAL` until completed/error; total polling is bounded
  by `ASSEMBLYAI_TIMEOUT`. `completed` with null text is `""`.
- **ElevenLabs**: `POST {ELEVENLABS_URL}/text-to-speech/{voice_id}?output_format=...`
  with `xi-api-key`; one shared async client per service. Per-request
  timeout is the constant `TTS_REQUEST_TIMEOUT_SECONDS = 60.0` (no env knob
  exists in the plan inventory). Only byte-count/emotion metadata is logged.
- **Voice profile** (`TTS_VOICE_PROFILE_FILE`, optional): schema
  `{"version": 1, "default": {...}, "emotions": {"happy": {...}}}` with
  fields stability / similarity (or `similarity_boost`) / style (0..1),
  speed (0.25..2.0), and `use_speaker_boost` (bool). Resolution order for
  voice settings: built-in neutral defaults seeded with the emotions
  manifest `tts_speed`, then profile `default`, then the profile's `neutral`
  entry, then the profile's emotion entry. A configured-but-invalid file
  fails startup.

### Static lines (plan sections 7.1, 18.4)

- Bundled `core/static_lines.json` ships the required EN/ES/JA tables with
  blank values; `STATIC_LINES_FILE` overrides it (missing/invalid override
  fails startup, matching the emotions-manifest rule).
- Line keys: `busy`, `unavailable`, `soft_block`, `stt_empty` (unknown keys
  are validation errors; extra languages are allowed but unused).
- A blank value in the pinned language is deliberate silence — protocol-only
  metadata, **no cross-language fallback**.
- 0.2.0 uses only `stt_empty` (empty and failed STT). `busy`/`unavailable`
  arrive with schedule (0.4.0) and `soft_block` with the owner profile
  (0.3.0).

### LLM router

- Route order per mode: `{MODE}_PROVIDERS` → `COMPANION_PROVIDERS` →
  `LLM_CHAIN` (companion skips the middle step's inheritance of itself).
- Model resolution: `{MODE}_{PROVIDER}_MODEL` (read from the merged
  environment snapshot, e.g. `COMPANION_FIREWORKS_MODEL`) → `{MODE}_MODEL` →
  provider default (`FIREWORKS_MODEL`, etc.).
- Base URLs are normalized to end in `/v1` (`http://host:11434` and
  `http://host:11434/v1` both work). Bearer headers are sent only when a key
  is configured (local Ollama needs none).
- `OLLAMA_TIMEOUT=0` (and any provider timeout `<= 0`) means no per-request
  timeout; the chain deadline still bounds the whole chain.
- "Missing credentials" means: fireworks needs key+model; chutes needs
  key+URL+model; ollama needs URL+model; openai_compat needs URL+model.
  Skips log one warning per provider per chain run.
- Streaming (`LLM_STREAMING_ENABLED`), tool calling, and service tiers beyond
  Fireworks' pass-through are unused in 0.2.0 (no work callers exist).

### HTTP

- `POST /message` accepts `{"user_id"?, "text", "mode"?, "language"?}`;
  `user_id` defaults to `OWNER_USER_ID` when omitted and 403s otherwise.
  Returns the terminal done-shaped JSON on success; `llm_unavailable` maps to
  502, validation failures (including `unsupported_language`) to 400, all in
  the standard error shape. Full webhook completion (tool-less work turns,
  `message_ack` reconciliation, audio) is milestone 0.7.0. HTTP-originated
  turns never drive MCP or the device daemon — neither exists yet.
- `/health` returns 200 `{"status": "ok", ...}` or 503
  `{"status": "degraded", "redis": false, ...}`; Redis is a required service
  and startup pings it.
- `/status` exposes version, Redis health, provider configured booleans and
  URLs (never credentials), a `speech` section (TTS/STT enabled, provider,
  configured, voice-profile-loaded, output format), feature flags, identity
  file paths/mtimes (never contents), companion routes, deployment mode, and
  connection count.
- `/emotions` serves the validated manifest. An invalid/missing manifest
  fails startup; the manifest may not introduce names outside the
  `constants.py` palette.

### Tailscale validation

- Deployment modes reported at startup and in `/status`: `loopback-dev`,
  `tailscale`, `firewall-ack`, or `unvalidated` (only when
  `TAILSCALE_REQUIRED=false`, which logs a warning).
- Interface enumeration uses `ip -o addr show dev tailscale0` (Linux) or
  `ifconfig tailscale0` (macOS), injectable as `tailscale_addresses` for
  tests. Loopback binds short-circuit before enumeration so development needs
  no Tailscale.

## Testing contract

- `tests/fakes.py::FakeRedis` implements the exact async subset
  `core.cache.RedisCache` uses (`ping`, transactional `pipeline` with
  `rpush`/`ltrim`/`execute`, `lrange`, `lset`, `llen`, `delete`, `keys`,
  `aclose`) against an in-memory `dict[str, list[str]]`, preserving the store
  contract without a live server.
- `tests/fakes.py::FakeLLM` is a scriptable router substitute (queued replies,
  exceptions, or callables; optional blocking gate).
- `tests/fakes.py::FakeSTT` / `FakeTTS` are scriptable speech-service
  substitutes (queued transcripts, forced provider errors, per-chunk-text TTS
  failures, availability toggles) so WS integration tests need no network.
- Speech-service unit tests use `httpx.MockTransport` responders that record
  requests and replay scripted responses.
- HTTP+WS tests use FastAPI's `TestClient`; no network, no live Redis.

## Redis keys in 0.1.0–0.2.0

Exactly one key family may exist: `core:history:{owner}:companion` (list of
JSON rows: `id`, `role`, `text`, `emotion`, `ts`, `delivery_state`). The
flags-off test asserts no other keys are created by a turn. **Audio is never
stored server-side** — audio bytes exist only in flight, and the STT/TTS
paths write no keys. All other keys in plan section 28 belong to later
milestones.

## Version source

`core/constants.py::VERSION = "0.2.0"` is the single source; the entrypoint
docstring, README, `connected` frame, and `/status` derive from it.
