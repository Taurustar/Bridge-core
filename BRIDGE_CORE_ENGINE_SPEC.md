# Bridge Core Engine — Implementation Spec

Living implementation contract. It refines unspecified details of
`BRIDGE_CORE_ENGINE_IMPLEMENTATION_PLAN.md`; it may not override locked
decisions (plan section 2). Milestones implemented: **0.1.0**, **0.2.0**,
**0.3.0**, **0.4.0**.

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
  Milestone 0.3.0 adds `core/needs.py`, `core/bids.py`, `core/rhythm.py`,
  `core/state_expression.py`, `core/owner_profile.py`, and the
  `core/routes/` package (`profiles.py`, `state.py` only so far); the needs
  tuning template stays at the plan-layout path `schedule/needs.json`
  (`NEEDS_PROFILE_FILE` overrides).
  Milestone 0.4.0 adds `core/schedule.py`, `core/interaction.py` (deferred
  queue + busy counters), `core/life.py`, `core/memory.py` (minimal durable
  long-term fallback), `core/context_feed.py`, `core/user_schedule.py`, and
  the routes `schedule.py`, `life.py`, `user_schedule.py`. The bundled
  life-event example lives at the plan-layout path
  `life_events/schema_example.disabled.json` (`enabled: false` — it
  establishes no backstory); `LIFE_EVENTS_DIR` points at author-supplied
  template directories.
- **Implementation-specific env fields** (allowed by plan 8.3, documented
  here): `LIFE_SKIP_ACTIVITIES` (comma-separated block activities whose
  entries never generate life events; default `sleep`) and
  `SCHEDULE_SOFT_BUSY_POLICY` (`normal` (default) or `short` — plan 16.3
  leaves soft_busy reply length to "configured policy"). Owner and character
  timezones (`OWNER_TIMEZONE`, `CHARACTER_TIMEZONE`) must be valid IANA
  names and fail startup otherwise (plan 16.2).
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
final-reply critical rules (plan 12 step 19), and localizes STT-empty and
soft-block static lines.

Resolution for the reply language (plan 7.4 order, complete as of
milestone 0.3.0):

1. Explicit per-message `language` field (validated; invalid values are
   terminal errors, not silent fallbacks).
2. The owner lived profile's `preferred_language` (blank = no preference;
   set via `PATCH /profiles/owner`).
3. For audio turns, the frame's `stt_language` when it is one of en/es/ja
   (clear inbound-language detection; other codes pass through to the STT
   provider but do not pin the reply).
4. `DEFAULT_LANGUAGE` (config-validated to en/es/ja at startup).

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

### Companion turn (plan section 12, as of 0.4.0)

Implemented hooks, in lifecycle order: soft-block gate (step 5, companion
only) → status(thinking) → availability gate + defer policy (steps 6-7:
busy/unavailable queue the message and terminate protocol-only) → rhythm
stamp (step 9, flag-gated) → turn lock → bid satisfaction (step 8,
flag-gated) → user row persist + fanout (step 11) → needs evaluate +
state-expression block (step 15) → owner-profile block injection (step 16,
first turn materializes the record) → boundary classification (18.4,
flag-gated) → awareness + bounded context feed (step 14, identity layer 5)
→ prompt → LLM → segments → assistant row pending → done → delivered: mark
+ fanout + needs turn effects + status drift + pending-life clear
(steps 27-29, 34) → background strict-JSON profile analysis (step 33,
flag-gated, delivered exchanges only) → TTS stream (step 31, outside the
lock) → optional catch-up of held companion messages (plan 16.3, own
lock).

Skipped hooks (they do not exist yet; flags off, no keys, no tasks, no LLM
calls): work sessions/tooling (0.5.0), memory retrieval/extraction
(0.6.0), initiative (0.7.0).

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
- 0.2.0 uses `stt_empty` (empty and failed STT); 0.3.0 adds `soft_block`.
  `busy`/`unavailable` arrive with schedule (0.4.0), spoken at most once
  per busy window (see the 0.4.0 section).

## Milestone 0.3.0 — needs, interaction, and owner profile

### Redis keys (plan section 28, 0.3.0 scope)

Beyond companion history, the following single-document keys may exist, each
only while its feature flag is ON (flag-off creates none — covered by
integration tests):

- `core:needs:{owner}` — needs state (`values`, `last_eval_ts`,
  `skipped_gap_count`, `activity`).
- `core:bids:{owner}` — bounded bid record (≤ 64 entries, metadata only).
- `core:rhythm:{owner}` — hourly owner-contact histogram (≤ 48 buckets) +
  last-contact timestamps in owner civil time. Never message text.
- `core:owner_profile:{owner}` — the lived-profile JSON document (plan 18.2
  schema plus `preferred_language`, `proposals_applied`, `last_drift_ts`,
  `proposals_at_last_drift` refinements).

### Needs engine (plan section 15)

- Tuning file: `schedule/needs.json` (bundled conservative defaults,
  `NEEDS_PROFILE_FILE` override). Keys starting with `_` are ignored so the
  template can carry authoring comments. Unknown stats, unknown turn-effect
  kinds, and schema versions > 1 fail startup; `migrate_needs` is the
  explicit per-version migration seam.
- `evaluate` advances from UTC timestamps (DST cannot alter elapsed time),
  bounds gaps at `NEEDS_MAX_ELAPSED_HOURS` (skipped gaps increment
  `skipped_gap_count` and are never replayed), and persists. `peek` is the
  read-only projection used by `GET /state`.
- Zones: `fine|low|critical` (`higher_is_better` thresholds go below; 
  `lower_is_better` thresholds go above), bond `secure|strained|deprived`.
- `GET /state` requires `NEEDS_ENABLED` **and** `STATE_EXPRESSION_ENABLED`
  (zones exist only through the needs engine); otherwise 403
  `feature_disabled`. The route is a pure poll — it never writes.
- State expression renders only `(stat, zone)` pairs that have a matching
  `## stat:zone` section in the authored `STATE.md`; bond `fine` renders as
  `secure`. No numeric values and no dialogue are ever emitted.
- Needs turn effects apply only after the assistant row is delivered
  (plan 12 step 29), classified deterministically: ≤ 240 chars =
  `companion_brief`, else `companion_engaged`.

### Bids and rhythm (plan sections 15.5, 15.6)

- Bid registration happens only after confirmed initiative delivery, which
  is milestone 0.7.0 — in 0.3.0 nothing opens bids, so the store stays empty
  unless initiative lands. Store, deterministic satisfaction (replies of at
  least 8 characters answer every open bid; no LLM), bounded record, and the
  expiry sweep (at most once per minute, from heartbeat maintenance) exist
  now behind `BIDS_ENABLED`.
- Rhythm stamps one owner-contact histogram bucket per turn start using the
  source connection's timezone (falling back to `OWNER_TIMEZONE`) behind
  `RHYTHM_ENABLED`. It never reads telemetry and never stores text.

### Owner lived profile (plan section 18)

- **Store**: one JSON document at `core:owner_profile:{owner}`. GET returns
  `{"profile": <default projection>, "materialized": false}` when the store
  is missing — it never materializes (plan 6.4). The first behavior turn or
  an explicit PATCH creates the record. All read-modify-write paths
  (PATCH, background proposals) serialize under a per-owner **profile lock**
  (separate from the turn lock) and go through a version-checked upsert;
  a version mismatch returns HTTP 409 `version_conflict`.
- **PATCH /profiles/owner** requires the `X-Confirm-Token:
  UPDATE_OWNER_PROFILE` mistake-guard header (plan 18.7; the token is a
  human-factors guard, not a secret and not authentication). Valid fields:
  trust/closeness/appeal/desirability (clamped 0-100), `tone_with_owner`,
  `preferred_language` (blank or en/es/ja), `persona_summary` (≤ 400),
  `likes`/`prefs` (≤ 16 items × 120 chars), `status` (plan 18.2 list;
  change stamps `status_reason: "admin_patch"`), and `soft_blocked`
  (setting it true opens a fresh cooldown window; clearing it lifts
  immediately). Unknown/invalid fields → 400 `invalid_patch`. Feature-off →
  403 `feature_disabled`.
- **Boundary penalties** (`OWNER_BOUNDARY_PENALTIES_ENABLED`): deterministic
  EN/ES/JA pattern classifiers for the five plan-18.4 categories (marker-
  based language detection, unknown text falls back to the frame language).
  Severity: 1 pattern hit = moderate (hard-boundary disregard = major),
  2+ hits = major. Each hit stores `{category, severity, ts, penalty, mode}`
  — never message text — and applies its configured trust penalty
  (`OWNER_BOUNDARY_PENALTY_{MINOR,MODERATE,MAJOR}`). Events are capped at
  the `needs.json` `owner_profile.max_boundary_events` (default 50).
- **Soft block** (`OWNER_SOFT_BLOCK_ENABLED`): engages on any major hit or
  when trust drops under `needs.json`
  `owner_profile.soft_block_trust_threshold` (default 20). While blocked,
  companion turns return a terminal `done` with `ignored: true`,
  `reason: "soft_blocked"` **before** the turn lock: no LLM call, no bids,
  no history writes, no needs effects; history is never wiped. The authored
  `soft_block` static line is spoken at most once per
  `OWNER_SOFT_BLOCK_COOLDOWN_SECONDS`, otherwise the done is protocol-only.
  Auto-lift requires the cooldown to have passed **and** trust above
  `OWNER_SOFT_BLOCK_UNBLOCK_TRUST_FLOOR`; otherwise the window extends.
  `PATCH {"soft_blocked": false}` lifts immediately (admin action). Work
  mode bypasses the relationship soft block by design (plan 12 step 5);
  work itself ships in 0.5.0.
- **Agreements** (`OWNER_AGREEMENTS_ENABLED`): validated against the plan
  18.5 shape; ≤ 12 active (`agreement_max_active` in `needs.json`);
  `personality_tension` agreements require trust ≥ 50 and closeness ≥ 40
  (floors in `needs.json`); schedules are reminder windows only.
- **Strict-JSON proposals** (`OWNER_PROFILE_LLM_ENABLED`): after each
  **delivered** companion turn the bridge enqueues a background
  `owner_profile`-mode analysis (plan 12 step 33). The reply must parse as a
  single JSON object (markdown fences tolerated, prose rejected); everything
  is clamped: summary ≤ 400 chars, list items ≤ 120×16, appeal/desirability
  deltas ±3, status suggestions must be adjacent steps in the plan-18.2
  order (treated as an intimacy axis: partner→estranged), agreement adds
  pass the same cap/floor validation, and `personality_tension` floors are
  evaluated against the live record. Rejected proposals are logged as one
  bounded line; raw exchanges and raw proposals are never stored. The store
  only ever sees validated fields plus a `proposals_applied` counter.
- **Status drift** (`OWNER_STATUS_DRIFT_ENABLED`): evaluated on delivered
  turns; score = −(1 minor / 2 moderate / 4 major boundary events since the
  last drift) + (+1 per applied proposal since then). |score| ≥ 5 moves the
  status exactly one adjacent step (negative → toward `estranged`, positive
  → toward `partner`), stamps `status_reason: "status_drift"`, and resets
  the evidence windows. At the axis ends there is no further drift.
- **Agreement aftermath** (`OWNER_AGREEMENT_AFTERMATH_ENABLED`): when a soft
  block engages, every `active` agreement becomes `suspended_by_block` and
  `agreement_aftermath` records the count; when the block lifts (auto or
  admin), suspended agreements return to `active` and the aftermath records
  the restoration.
- **Preferred language**: the profile's `preferred_language` (plan 7.4
  step 2) joins the reply-language fallback between the explicit pin and
  inbound-language detection (see the language-pin section above).

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
  configured, voice-profile-loaded, output format), `needs` and
  `owner_profile` sections (enabled/inject/flags — never scores), feature
  flags, identity file paths/mtimes (never contents), companion routes,
  deployment mode, and connection count.
- `/emotions` serves the validated manifest. An invalid/missing manifest
  fails startup; the manifest may not introduce names outside the
  `constants.py` palette.
- `GET /state` (0.3.0): read-only needs peek — zones, values, shutdown flag,
  and the rendered `[CHARACTER STATE]` block. Requires
  `NEEDS_ENABLED` + `STATE_EXPRESSION_ENABLED`, else 403 `feature_disabled`.
  Never writes (plan 6.4).
- `GET /profiles/owner` (0.3.0): the stored profile or a default projection
  with `"materialized": false`; includes the effective soft-block status.
  Never materializes the store.
- `PATCH /profiles/owner` (0.3.0): mistake-guard token required (see the
  0.3.0 section). Validation failures → 400, feature-off → 403, concurrent
  version mismatch → 409 `version_conflict`.
- `POST /message` (0.3.0): the owner-profile preferred language joins the
  language fallback, and a soft-blocked turn returns 200 with the
  done-shaped body (`ignored: true`, `reason: "soft_blocked"`).

### Tailscale validation

- Deployment modes reported at startup and in `/status`: `loopback-dev`,
  `tailscale`, `firewall-ack`, or `unvalidated` (only when
  `TAILSCALE_REQUIRED=false`, which logs a warning).
- Interface enumeration uses `ip -o addr show dev tailscale0` (Linux) or
  `ifconfig tailscale0` (macOS), injectable as `tailscale_addresses` for
  tests. Loopback binds short-circuit before enumeration so development needs
  no Tailscale.

## Milestone 0.4.0 — schedule, life, awareness, catch-up

### Real-time schedule (plan section 16)

- Day resolution: `mon.json`..`sun.json` if present, else `weekday.json`
  (Mon–Fri) / `weekend.json` (Sat–Sun); missing files yield one all-day
  default block and a warning. `SCHEDULE_DIR` empty/missing ⇒ all-day
  defaults (schedule stays available, availability `free`).
- Blocks: inclusive start / exclusive end; `24:00` only as an end; single
  overnight blocks rejected; normalized overlaps rejected; availability in
  `free|soft_busy|busy|unavailable`. Invalid startup/reload keeps the last
  valid schedule.
- Block ids are `{ymd}:{index}:{start}-{end}`; gap blocks get `{ymd}:gap`
  and are synthetic: they never trigger life generation and never enter
  life bookkeeping.
- DST (plan 16.2): nonexistent local times advance to the next valid
  instant; repeated local times take the first occurrence for starts and
  the second for ends (both transitions covered by tests).
- Hot reload by mtime on turn/availability evaluation; explicit
  `POST /admin/reload-schedule` (body token `{"confirm": "RELOAD_SCHEDULE"}`)
  rejects an invalid day with 400 `invalid_schedule` and keeps the previous
  schedule. Hot reload never retro-generates life events for prior blocks;
  only a **new** current block id may generate one event.

### Availability ladder and defer (plan sections 12 steps 6-7, 15.4, 16.3)

- Effective availability = schedule block availability, downgraded to
  `unavailable` by critical needs shutdown (via needs `peek` — the gate
  never persists). Soft block is handled earlier (plan 12 step 5).
- `free` → normal reply; `soft_busy` → normal reply, or a brief
  `[AVAILABILITY]` system note when `SCHEDULE_SOFT_BUSY_POLICY=short`.
- `busy`/`unavailable` → the message is appended to the deferred queue and
  the turn terminates with `done` `ignored: true`,
  `reason: "busy"|"unavailable"`, `deferred: true` — no LLM call, no
  history write, no bids/needs effects, no boundary classification (held
  texts classify at catch-up). The first message in a busy window
  (`core:busy_count:{owner}` == 0) may speak the authored `busy` /
  `unavailable` static line; repeated messages are protocol-only. Skipped
  hooks are exactly the ones named here (plan 12 "early refusal" law).
- Queue (`core:deferred:{owner}` document): ≤ 5 entries and ≤ 4,000 UTF-8
  bytes total; oldest drops first with a warning; dedupe by original
  message id preserving arrival order; entries expire after 48h
  (`expires_ts`), expire without answer, and increment a bounded
  `expired_count` diagnostic; drops increment `dropped_count`.
- All queue mutations and claims run under a dedicated per-owner
  **catch-up lock** (plan section 11).

### Deferred catch-up (plan section 16.3)

- Triggers: after every delivered companion turn, and from heartbeat
  maintenance (throttled to at most once per minute), when
  `SCHEDULE_ENABLED`.
- Guards: availability must be `free|soft_busy`; the per-owner turn lock
  must be free (a later trigger retries); at least one live owner
  connection must exist (target = the triggering connection, else the most
  recent owner connection; without one, entries restore to `held`).
- Flow (under the catch-up lock, history under the per-owner turn lock):
  claim companion entries `held → delivering` (work entries are never
  claimed by the companion path) → persist each held text as a **deduped**
  delivered user row (`chat_sync` fanned out with the original deferring
  connection id) → one prompt (`build_catchup_prompt`, `[CATCH-UP]` note)
  containing the bounded batch → companion-mode LLM → usual emotion
  validation/retry → assistant row pending → done (extra metadata
  `catchup: true`, `initiated_by: "character"`) → delivered: mark +
  assistant fanout + needs turn effects + status drift + pending-life
  clear; only then are claimed entries removed and the busy window reset.
  Generation or delivery failure restores entries to `held` (expired ones
  drop), the assistant row (if any) is marked undelivered, and the already
  persisted user rows remain (retry dedupes by message id).
- Work catch-up (text-only, tool-less) ships with work mode in 0.5.0; the
  mode field and separate claim filters are in place now.

### Character life (plan section 17)

- Requires `SCHEDULE_ENABLED` (block-entry driven). `LIFE_ENABLED` without
  a schedule logs a warning and stays inert.
- Templates: `*.json` in `LIFE_EVENTS_DIR`; malformed files fail startup;
  only `enabled: true` templates participate. Matching is deterministic:
  activities / places / schedule_tags (intersect) / time_of_day buckets;
  weighted random choice among matches (no match ⇒ no generation).
- Generation runs only on **new authored block ids** claimed before the
  LLM call (single-writer life lock + one state document). One event max
  per block; daily max hard; daily min forces the next eligible block only
  as a chance-selection override (0.4.0 generation is deterministic, so
  the seat exists but is not normally reached); cooldown enforced for all
  poll-driven generation; admin force bypasses only the cooldown and the
  failed-block retry bar — never the daily max.
- The `life`-mode LLM reply is sanitized (control tags/asterisk actions
  stripped, clamped to 500 chars) and stored through the durable Redis
  long-term fallback (`core:longterm:{owner}`) as
  `kind=character_life_event` with block metadata and `past: true`. Failed
  generation retains the claimed block with `generation_failed: true` and
  is never retried by the poll loop; `POST /life/generate` (body token
  `GENERATE_LIFE`, optional `force`) is the explicit retry path.
- Pending mentions (`core:life:pending:{owner}`, bounded ring) clear only
  after a successful companion response that received the context (the
  feed reports which pending ids rendered; plan 12 step 34).
- `LIFE_MISSED_BLOCK_POLICY=current_only`: the poll evaluates only the
  current block; nothing is fabricated for offline periods.
- Reads: `GET /life/today`, `GET /life/recent?limit=` are pure polls over
  the durable store.

### Awareness and context feed (plan section 21)

- The awareness block renders when `SCHEDULE_ENABLED` or
  `USER_SCHEDULE_ENABLED` (flag-off parity: never with both off). It
  carries owner local time (connection timezone → `OWNER_TIMEZONE`
  fallback), character local time, character schedule now, time since the
  last conversation, and the owner-schedule state ("informational only"
  is part of the block text). Zero extra LLM calls.
- The bounded context feed (`[LIFE CONTEXT]`) renders recent life events
  marked PAST plus pending mentions marked PENDING; the same event is
  never injected twice; pending rows win slots and are the only ids that
  may be cleared later. The hard budget is `CONTEXT_FEED_MAX_TOKENS`
  enforced by the deterministic ~4-chars-per-token estimate (plan 4.2
  fallback). Mid-term chapters, durable memory rows beyond life, and
  project context join in later milestones — the renderer is already the
  single injection point (plan 20.4).

### Contextual owner schedule (plan section 22)

- Store: `core:user_schedule:{owner}` (baseline week + timezone) and
  `core:user_schedule:day:{owner}:{ymd}` (per-date overrides). States
  `busy|free|sleep|unknown`; unknown is never free; empty/missing store ⇒
  everything unknown; GET never materializes (plan 6.4).
- `PATCH /user-schedule` requires the `UPDATE_USER_SCHEDULE`
  mistake-guard header. Accepted fields: `timezone` (valid IANA; **this
  endpoint is the only path that changes the durable owner-schedule
  timezone**), `days` (day key → blocks, replaces those days), and
  `date` + `blocks` (one per-date override). Unknown fields → 400
  `invalid_patch`. Blocks reuse the schedule time rules (HH:MM, `24:00`
  end-only, no overlap) and DST semantics.
- Usage in 0.4.0: awareness context only. Initiative gating and daily-tool
  access arrive with their milestones.

## Testing contract

- `tests/fakes.py::FakeRedis` implements the exact async subset
  `core.cache.RedisCache` uses (`ping`, transactional `pipeline` with
  `rpush`/`ltrim`/`execute`, `lrange`, `lset`, `llen`, `set`/`get` for
  single documents, `delete`, `keys`, `aclose`) against in-memory list and
  string stores, preserving the store contract without a live server.
- `tests/fakes.py::FakeLLM` is a scriptable router substitute (queued replies,
  exceptions, or callables; optional blocking gate).
- `tests/fakes.py::FakeSTT` / `FakeTTS` are scriptable speech-service
  substitutes (queued transcripts, forced provider errors, per-chunk-text TTS
  failures, availability toggles) so WS integration tests need no network.
- `tests/fakes.py::FakeNeeds` / `FakeOwnerProfile` are scriptable engine
  substitutes for the bridge-facing 0.3.0 surfaces; the enabled-profile
  integration tests exercise the real engines over `FakeRedis`.
- `tests/fakes.py::FakeSchedule` is a scriptable schedule substitute
  (settable availability) so availability/catch-up integration needs no
  schedule files; the schedule/life engine tests exercise the real engines
  over `FakeRedis` with temp dirs and fixed clocks.
- Speech-service unit tests use `httpx.MockTransport` responders that record
  requests and replay scripted responses.
- HTTP+WS tests use FastAPI's `TestClient`; no network, no live Redis.

## Redis keys in 0.1.0–0.4.0

`core:history:{owner}:companion` (list of JSON rows: `id`, `role`, `text`,
`emotion`, `ts`, `delivery_state`) is the only key a flags-off deployment
ever creates — the flag-off parity tests assert no other keys after turns
and polls. With 0.3.0 flags enabled, the single-document keys
`core:needs:{owner}`, `core:bids:{owner}`, `core:rhythm:{owner}`, and
`core:owner_profile:{owner}` may appear. With 0.4.0 flags enabled:
`core:longterm:{owner}` (list of durable memory records),
`core:life:last_block:{owner}` and `core:life:pending:{owner}`
(LIFE_ENABLED), `core:deferred:{owner}` (document; absent when empty) and
`core:busy_count:{owner}` (SCHEDULE_ENABLED defers), and
`core:user_schedule:{owner}` / `core:user_schedule:day:{owner}:{ymd}`
(USER_SCHEDULE_ENABLED writes). **Audio is never stored server-side** —
audio bytes exist only in flight, and the STT/TTS paths write no keys. All
other keys in plan section 28 belong to later milestones.

## Version source

`core/constants.py::VERSION = "0.4.0"` is the single source; the entrypoint
docstring, README, `connected` frame, and `/status` derive from it.
