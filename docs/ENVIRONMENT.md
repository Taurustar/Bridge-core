# Environment reference (v1.0.0)

Every configuration variable Bridge Core Engine reads, with defaults. The
authoritative machine-readable source is `core/config.py`; this document is
the human reference and the release tests check that every variable is
listed here.

## How configuration loads

- Values come from two places: the real process environment always wins,
  then `core.env` (loaded with `dotenv`), then built-in defaults.
- Keep real secrets only in `core.env` (gitignored) or your process
  environment. Never commit them.
- Validation runs at startup: bad numbers/booleans, invalid timezones,
  unknown languages/providers, or out-of-range values fail with a clear
  message (run `scripts/validate_config.py` to check without starting).
- Hot reload: `POST /admin/reload-config` re-reads every non-structural
  field. Structural (restart-required): `BRIDGE_HOST`, `BRIDGE_PORT`,
  `REDIS_HOST`, `REDIS_PORT`, `REDIS_DB`, `CHROMA_PATH`, `CHROMA_REQUIRED`.
- Dynamic overrides: `{MODE}_{PROVIDER}_MODEL` (for example
  `COMPANION_FIREWORKS_MODEL=accounts/.../models/kimi-k2`) overrides that
  one route without touching the defaults.

## Server

| Variable | Default | Meaning |
|---|---|---|
| `BRIDGE_HOST` | `127.0.0.1` | Bind address. Production: the Tailscale address, or `0.0.0.0` only with the firewall acknowledgement below. |
| `BRIDGE_PORT` | `8766` | HTTP+WS port. |
| `TAILSCALE_REQUIRED` | `true` | Startup fails unless the bind is loopback, a `tailscale0` address, or the firewall acknowledgement is set. |
| `TAILSCALE_FIREWALL_ACK` | `false` | Set `true` only after applying and testing a real firewall rule (plan 27.2). |
| `LOG_LEVEL` | `info` | Python log level for journald output. |
| `OWNER_USER_ID` | `owner` | The single owner routing id. Not authentication. |
| `DEFAULT_LANGUAGE` | `en` | Reply language fallback; must be `en`, `es`, or `ja`. |
| `OWNER_TIMEZONE` | `UTC` | Durable owner civil-time authority (owner schedule, initiative daily reset). Valid IANA name, required. |
| `CHARACTER_TIMEZONE` | `UTC` | Character schedule timezone. Valid IANA name, required. |

## Redis and Chroma

| Variable | Default | Meaning |
|---|---|---|
| `REDIS_HOST` / `REDIS_PORT` / `REDIS_DB` | `127.0.0.1` / `6379` / `0` | Required store; the engine refuses to start if unreachable. |
| `CHROMA_ENABLED` | `false` | Optional semantic index over durable long-term rows. Redis stays the store of record. |
| `CHROMA_PATH` | `./data/chroma` | Chroma persistence directory (structural). |
| `CHROMA_REQUIRED` | `false` | When `true`, an unavailable Chroma fails startup instead of degrading (structural). |

## LLM routing

| Variable | Default | Meaning |
|---|---|---|
| `LLM_CHAIN` | `fireworks,chutes,ollama,openai_compat` | Provider failover order; unknown names fail startup. |
| `LLM_CHAIN_DEADLINE_SECONDS` | `75` | Hard budget for one full chain attempt set. |
| `LLM_HISTORY_MESSAGE_BUDGET` | `40` | Delivered history messages sent as prompt context. |
| `LLM_STREAMING_ENABLED` | `false` | Reserved; responses are currently non-streamed. |

Per-provider connection settings (leave a provider's key/model blank to
skip it with one startup warning):

| Variable | Default |
|---|---|
| `FIREWORKS_API_KEY` / `FIREWORKS_URL` / `FIREWORKS_MODEL` / `FIREWORKS_TIMEOUT` / `FIREWORKS_SERVICE_TIER` | `` / `https://api.fireworks.ai/inference/v1` / `` / `60` / `` |
| `CHUTES_API_KEY` / `CHUTES_URL` / `CHUTES_MODEL` / `CHUTES_TIMEOUT` | `` / `` / `` / `60` |
| `OLLAMA_URL` / `OLLAMA_MODEL` / `OLLAMA_TIMEOUT` | `http://127.0.0.1:11434` / `` / `0` (no timeout) |
| `OPENAI_COMPAT_API_KEY` / `OPENAI_COMPAT_URL` / `OPENAI_COMPAT_MODEL` / `OPENAI_COMPAT_TIMEOUT` | `` / `` / `` / `60` |

Per-mode routes: `{MODE}_PROVIDERS` (`COMPANION_PROVIDERS`,
`WORK_PROVIDERS`, `LIFE_PROVIDERS`, `PROACTIVE_PROVIDERS`,
`MEMORY_PROVIDERS`, `SESSION_SUMMARY_PROVIDERS`,
`OWNER_PROFILE_PROVIDERS`; a blank chain falls back to
`COMPANION_PROVIDERS`, then `LLM_CHAIN`) plus per-mode model
(`COMPANION_MODEL`, `WORK_MODEL`, `LIFE_MODEL`, `PROACTIVE_MODEL`,
`MEMORY_MODEL`, `SESSION_SUMMARY_MODEL`, `OWNER_PROFILE_MODEL`) and
temperature, and token limits:
`COMPANION_TEMPERATURE`/`COMPANION_MAX_TOKENS` (0.8/1200),
`WORK_TEMPERATURE`/`WORK_MAX_TOKENS` (0.3/4000),
`LIFE_TEMPERATURE`/`LIFE_MAX_TOKENS` (0.8/400),
`PROACTIVE_TEMPERATURE`/`PROACTIVE_MAX_TOKENS` (0.8/120, initiative
messages), `MEMORY_TEMPERATURE`/`MEMORY_MAX_TOKENS` (0.2/400),
`SESSION_SUMMARY_MAX_TOKENS` (500), `OWNER_PROFILE_MAX_TOKENS` (400).

## Speech

| Variable | Default | Meaning |
|---|---|---|
| `TTS_ENABLED` | `false` | Master TTS switch. |
| `ELEVENLABS_API_KEY` / `ELEVENLABS_URL` / `ELEVENLABS_VOICE_ID` / `ELEVENLABS_MODEL` | `` / `https://api.elevenlabs.io/v1` / `` / `eleven_flash_v2_5` | TTS provider; `capabilities` gains `audio` only when enabled and configured. |
| `TTS_OUTPUT_FORMAT` | `mp3_44100_128` | Requested audio format. |
| `TTS_CHUNK_THRESHOLD` / `TTS_CHUNK_SIZE` / `TTS_CHUNK_SPACING_MS` | `150` / `150` / `50` | Segment-to-chunk chunking and pacing. |
| `TTS_VOICE_PROFILE_FILE` | `` | Optional voice-profile JSON. Empty loads bundled `core/voice_profile.json` (v1.0.1). |
| `STT_ENABLED` | `false` | Master STT switch. |
| `STT_PROVIDER` | `deepgram` | `deepgram` or `assemblyai`. |
| `STT_LANGUAGE` | `en` | Default transcription language. |
| `DEEPGRAM_API_KEY` / `DEEPGRAM_URL` / `DEEPGRAM_MODEL` / `DEEPGRAM_TIMEOUT` | `` / `https://api.deepgram.com/v1/listen` / `nova-3` / `45` | |
| `ASSEMBLYAI_API_KEY` / `ASSEMBLYAI_URL` / `ASSEMBLYAI_SPEECH_MODEL` / `ASSEMBLYAI_TIMEOUT` / `ASSEMBLYAI_POLL_INTERVAL` | `` / `https://api.assemblyai.com/v2` / `best` / `90` / `1.0` | |

Audio input guards: `MAX_AUDIO_BYTES` (15728640) and
`ALLOWED_AUDIO_CONTENT_TYPES` (comma list; container signatures are also
verified). Audio is never stored server-side.

## History and memory tiers

| Variable | Default | Meaning |
|---|---|---|
| `MAX_HISTORY_TURNS` | `80` | Companion history ring bound (rows). |
| `COMPANION_SHORT_WINDOW_ENABLED` | `true` | Live-window feature switch. |
| `COMPANION_LIVE_WINDOW_MESSAGES` | `8` | Recent messages kept verbatim before compaction. |
| `COMPANION_COMPACT_THRESHOLD` | `60` | History length that triggers a chapter distill (0 disables). Must exceed keep-recent. |
| `COMPANION_KEEP_RECENT` | `16` | Rows kept after compaction. |
| `MIDTERM_INJECT_CHAPTERS` | `4` | Chapter notes rendered into the context feed. |
| `MEMORY_EXTRACTION_ENABLED` | `false` | Strict-JSON durable-fact extraction after delivered turns. |
| `MEMORY_CLEANUP_ENABLED` | `false` | TTL/policy cleanup (also gates `POST /memories/cleanup` real runs). |
| `MEMORY_CLEANUP_INTERVAL_HOURS` | `12` | Background cleanup cadence. |
| `MEMORY_MAX_PER_USER` | `1000` | Durable longterm row cap. |
| `MEMORY_TEMPERATURE` | `0.2` | Memory LLM temperature. |
| `MEMORY_CHAPTER_MAX_CHARS` | `800` | Chapter size cap. |
| `MEMORY_CONVERSATION_TTL_DAYS` / `MEMORY_LIFE_TTL_DAYS` | `30` / `365` | Cleanup TTLs (conversation-family vs life-family). |
| `MEMORY_PROTECTED_PROJECT_FLOOR` | `0.7` | Project rows at/above this importance never auto-delete. |
| `CONTEXT_FEED_ENABLED` | `true` | Hard-budget feed as sole renderer for memories/chapters/life. |
| `CONTEXT_FEED_MAX_TOKENS` | `700` | Feed budget (whole rendered block). |
| `MEMORY_PROVIDERS` / `MEMORY_MODEL` | `` / `` | Extraction route override. |
| `SESSION_SUMMARY_PROVIDERS` / `SESSION_SUMMARY_MODEL` | `` / `` | Work session summary route. |

## Needs, bids, rhythm, state expression (plan 15)

`NEEDS_ENABLED`, `BIDS_ENABLED`, `RHYTHM_ENABLED`, `STATE_EXPRESSION_ENABLED`
— all default `false`. `NEEDS_PROFILE_FILE` overrides the bundled
`schedule/needs.json`. Amounts, thresholds, and caps live in that file, not
in env. `NEEDS_MAX_ELAPSED_HOURS` (default `48`) bounds how much missed
time one projection may consume.

## Owner lived profile (plan 18)

| Variable | Default |
|---|---|
| `OWNER_PROFILE_ENABLED` | `false` |
| `OWNER_PROFILE_INJECT` | `true` |
| `OWNER_PROFILE_PROVIDERS` / `OWNER_PROFILE_MODEL` / `OWNER_PROFILE_MAX_TOKENS` / `OWNER_PROFILE_LLM_ENABLED` | `` / `` / `400` / `false` |
| `OWNER_STATUS_START` | `acquaintance` |
| `OWNER_TRUST_START` / `OWNER_CLOSENESS_START` / `OWNER_APPEAL_START` / `OWNER_DESIRABILITY_START` | `50` / `0` / `50` / `50` |
| `OWNER_BOUNDARY_PENALTIES_ENABLED` | `false` |
| `OWNER_BOUNDARY_PENALTY_MINOR` / `OWNER_BOUNDARY_PENALTY_MODERATE` / `OWNER_BOUNDARY_PENALTY_MAJOR` | `3.0` / `6.0` / `12.0` |
| `OWNER_SOFT_BLOCK_ENABLED` | `false` |
| `OWNER_SOFT_BLOCK_COOLDOWN_SECONDS` / `OWNER_SOFT_BLOCK_UNBLOCK_TRUST_FLOOR` | `3600` / `25` |
| `OWNER_STATUS_DRIFT_ENABLED` / `OWNER_AGREEMENTS_ENABLED` / `OWNER_AGREEMENT_AFTERMATH_ENABLED` | `false` / `false` / `false` |

## Dormant gateway profiles (plan 19)

| Variable | Default | Meaning |
|---|---|---|
| `EXTERNAL_USER_PROFILE_STORE_ENABLED` | `true` | Admin CRUD for dormant external profiles; `false` answers `409 feature_disabled` and creates no keys. |
| `EXTERNAL_USER_PROFILES_BEHAVIOR_ENABLED` | `false` | Reserved for future gateways; gates nothing today. |
| `EXTERNAL_USER_PROFILE_LLM_ENABLED` | `false` | Reserved for future gateways; gates nothing today. |

## Schedule and life (plans 16, 17)

| Variable | Default | Meaning |
|---|---|---|
| `SCHEDULE_ENABLED` | `false` | Character day schedule. |
| `SCHEDULE_DIR` | `` | Authored day files directory. |
| `SCHEDULE_SOFT_BUSY_POLICY` | `normal` | `normal` or `short` reply policy during soft-busy. |
| `LIFE_ENABLED` | `false` | Block-entry life events (requires `SCHEDULE_ENABLED`). |
| `LIFE_EVENTS_DIR` | `` | Authored life-event template directory. |
| `LIFE_DAILY_MIN` / `LIFE_DAILY_MAX` | `0` / `4` | Daily event bounds (min ≤ max). |
| `LIFE_EVENT_COOLDOWN_MINUTES` | `40` | Minimum spacing between events. |
| `LIFE_POLL_INTERVAL_SECONDS` | `60` | Block-entry poll cadence. |
| `LIFE_MISSED_BLOCK_POLICY` | `current_only` | Only the current block generates. |
| `LIFE_SKIP_ACTIVITIES` | `sleep` | Comma list of activities whose block entries never generate events. |
| `LIFE_PROVIDERS` / `LIFE_MODEL` | `` / `` | Life generation route. |

## Heartbeat initiative (plan 23)

| Variable | Default | Meaning |
|---|---|---|
| `HEARTBEAT_ENABLED` | `true` | Heartbeat frame support (acks are always protocol-safe). |
| `INITIATIVE_ENABLED` | `false` | Master initiative switch; off means no counting and no `core:initiative:{owner}` key. |
| `INITIATIVE_MIN_HEARTBEATS` | `3` | Counted heartbeats needed before eligibility (≥1). |
| `INITIATIVE_HEARTBEAT_WINDOW_SECONDS` | `900` | Counted streak lifetime; expiry resets the count. |
| `INITIATIVE_HEARTBEAT_COUNT_INTERVAL_SECONDS` | `60` | Owner-global bucket length; one count per bucket regardless of devices (≥1). |
| `INITIATIVE_MIN_GAP_SECONDS` | `3600` | Minimum time between delivered initiatives. |
| `INITIATIVE_DAILY_MAX` | `3` | Hard per-owner-day cap. |
| `INITIATIVE_REQUIRE_SCHEDULE_FREE` | `true` | When true, initiative fires only while availability is `free`; when false, `soft_busy` also qualifies. |
| `INITIATIVE_ELIGIBILITY_CHANCE` | `0.35` | Deterministic roll acceptance probability (0–1). |
| `INITIATIVE_RESPECT_OWNER_SCHEDULE` | `false` | Also suppress initiative during contextual owner-schedule `sleep`/`busy`. |
| `INITIATIVE_SEED_FILE` | `` | Deployment seed path (default `./data/initiative_seed`). Created once; never logged; not portable between machines. |
| `PROACTIVE_PROVIDERS` / `PROACTIVE_MODEL` | `` / `` | Initiative generation route. |

## Work mode, MCP, device daemon (plans 25, 26)

| Variable | Default | Meaning |
|---|---|---|
| `WORK_ENABLED` / `SESSIONS_ENABLED` | `true` / `true` | Work mode master switches. |
| `SESSION_HISTORY_TURNS` | `80` | Per-session history bound. |
| `SESSION_SUMMARY_ENABLED` | `true` | Strict-JSON session summaries. |
| `WORK_SKILLS_ENABLED` | `true` | Inject `WORK_SKILLS.md`. |
| `WORK_SKILLS_FILE` / `DAILY_SKILLS_FILE` | `` / `` | Path overrides (repo defaults under `skills/`). |
| `MCP_PROXY_ENABLED` | `true` | MCP execution proxy. |
| `MCP_TOOL_TIMEOUT` | `120` | Per-tool-call deadline. |
| `MCP_MAX_ITERATIONS` | `20` | Agent loop bound. |
| `MCP_VERIFICATION_ENABLED` / `MCP_VERIFICATION_RETRIES` | `true` / `2` | Write read-back enforcement. |
| `AGENT_CHECKPOINTS_ENABLED` | `true` | Durable pause/resume checkpoints (24h expiry). |
| `DEVICE_ENABLED` | `false` | Device daemon tools. |
| `DEVICE_TOOL_TIMEOUT` | `120` | Device call deadline. |
| `DEVICE_PER_TURN_CALL_CAP` | `20` | Device calls per turn. |
| `DEVICE_MAX_OUTPUT_CHARS` | `30000` | Output cap (also the `device_write` size cap). |
| `DEVICE_SHELL_TIMEOUT` / `DEVICE_SHELL_TIMEOUT_MAX` | `120` / `600` | Shell deadline bounds. |
| `DEVICE_WRITE_ROOTS` | `` | Comma list of allowed write roots. |

## Daily tools and web (plan 24)

| Variable | Default | Meaning |
|---|---|---|
| `DAILY_TOOLS_ENABLED` | `false` | Private server-side tools (clock, arithmetic, reminders, schedule reads). |
| `DAILY_WEB_ENABLED` | `false` | Web search/open (fail-closed without a key). |
| `TAVILY_API_KEY` | `` | Tavily key. |
| `DAILY_WEB_SEARCH_CAP` / `DAILY_WEB_OPEN_CAP` | `1` / `2` | Per-turn web call caps. |
| `DAILY_TOOL_MAX_CALLS` | `6` | Tool calls per turn (1–6). |
| `DAILY_WEB_MAX_BYTES` / `DAILY_WEB_MAX_TEXT_CHARS` / `DAILY_WEB_TIMEOUT` | `500000` / `4000` / `20` | Fetch size/text/time caps. |

## Contextual owner schedule (plan 22)

| Variable | Default | Meaning |
|---|---|---|
| `USER_SCHEDULE_ENABLED` | `false` | Owner expectation store (`GET/PATCH /user-schedule`). |
| `SCHEDULE_SOFT_BUSY_POLICY` | `normal` | (listed above; shared with the character schedule). |

## Identity and client manifests

| Variable | Default | Meaning |
|---|---|---|
| `SOUL_FILE` / `PROFILE_FILE` / `STATE_FILE` | `` | Identity file overrides (repo defaults under `identity/`, blank-heading templates). The engine never writes these. |
| `EMOTIONS_FILE` / `STATIC_LINES_FILE` | `` | Manifest overrides (bundled neutral defaults ship in `core/`). |

## Discord adapter (v1.1.0, not `core.env`)

These live in `discord.env` (see `discord_adapter/discord.env.template`). They are
not `Config` fields. Missing file or `DISCORD_ENABLED=false` leaves the
adapter idle. The bot token is never in `/status` or INFO logs.

| Variable | Default | Meaning |
|---|---|---|
| `DISCORD_ENABLED` | `false` | Master switch. |
| `DISCORD_BOT_TOKEN` | `` | Bot token. Secret. |
| `DISCORD_GUILD_IDS` / `DISCORD_CHANNEL_IDS` | `` | Allowlists. Empty = none. |
| `DISCORD_DM_OWNER_ENABLED` | `false` | Owner DMs join the companion thread. |
| `DISCORD_DM_STRANGERS_ENABLED` | `false` | Stranger DMs get a separate history list. |
| `DISCORD_OWNER_USER_ID` | `` | Owner Discord snowflake. |
| `DISCORD_STT_ENABLED` / `DISCORD_TTS_ENABLED` | `false` | Voice-message files, not live voice. |
| `DISCORD_VISION_ENABLED` | `false` | Attach images to the turn when the model accepts them. |
| `DISCORD_RATE_MAX` / `DISCORD_RATE_WINDOW_SECONDS` | `8` / `60` | Per-author rate limit. |
