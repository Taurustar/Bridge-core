# Bridge Core Engine

## Complete Greenfield Requirements and Implementation Plan

**Document status:** Binding implementation authority for the initial project.

**Audience:** The engineering agent or team creating a brand-new repository and implementation.

**Important:** This is a greenfield project. Do not import source files, personality text, identity content, environment files, Redis keys, or runtime assumptions from another companion project. Similar architectural ideas may be implemented, but every file in this repository must be written for Bridge Core Engine and reviewed against this document.

**How to start:** Create a new git repository named `bridge-core-engine` outside any other companion project. Copy this document into it as `BRIDGE_CORE_ENGINE_IMPLEMENTATION_PLAN.md`. Implement milestone 0.1.0 only. Do not copy Python modules from another codebase.

### Contents

- [0. Agent Mandate](#0-agent-mandate)
- [1. Product Definition](#1-product-definition)
- [2. Locked Product Decisions](#2-locked-product-decisions)
- [3. Goals and Non-Goals](#3-goals-and-non-goals)
- [4. Technology Stack](#4-technology-stack)
- [5. Repository Layout](#5-repository-layout)
- [6. Architectural Invariants](#6-architectural-invariants)
- [7. Identity and Prompt System](#7-identity-and-prompt-system)
- [8. Configuration System](#8-configuration-system)
- [9. LLM Provider Router](#9-llm-provider-router)
- [10. WebSocket and HTTP Protocol](#10-websocket-and-http-protocol)
- [11. Connection and Concurrency Model](#11-connection-and-concurrency-model)
- [12. Companion Turn Lifecycle](#12-companion-turn-lifecycle)
- [13. Emotion, Sprite, Animation, and Audio Contract](#13-emotion-sprite-animation-and-audio-contract)
- [14. Speech Providers](#14-speech-providers)
- [15. Needs and Interaction System](#15-needs-and-interaction-system)
- [16. Real-Time Schedule](#16-real-time-schedule)
- [17. Character Life](#17-character-life)
- [18. Owner Lived Profile](#18-owner-lived-profile)
- [19. Dormant External-User Profiles](#19-dormant-external-user-profiles)
- [20. Three-Tier Memory](#20-three-tier-memory)
- [21. Awareness and Context Feed](#21-awareness-and-context-feed)
- [22. Contextual Owner Schedule](#22-contextual-owner-schedule)
- [23. Heartbeat-Driven Initiative](#23-heartbeat-driven-initiative)
- [24. Daily Tools and Web](#24-daily-tools-and-web)
- [25. Work Mode](#25-work-mode)
- [26. Device Daemon](#26-device-daemon)
- [27. Tailscale Deployment Requirement](#27-tailscale-deployment-requirement)
- [28. Storage Keys and Retention](#28-storage-keys-and-retention)
- [29. HTTP API Inventory](#29-http-api-inventory)
- [30. Observability and Error Handling](#30-observability-and-error-handling)
- [31. Milestone Plan and Commit Gates](#31-milestone-plan-and-commit-gates)
- [32. Testing Requirements](#32-testing-requirements)
- [33. Common Implementation Pitfalls](#33-common-implementation-pitfalls)
- [34. Final Acceptance Checklist](#34-final-acceptance-checklist)
- [35. Instruction to the Implementing Agent](#35-instruction-to-the-implementing-agent)

---

## 0. Agent Mandate

The implementing agent must:

1. Create a new repository named `bridge-core-engine`.
2. Implement only the requirements in this document.
3. Keep each milestone independently runnable and tested.
4. Commit after every milestone listed in section 31.
5. Never add excluded features preemptively.
6. Never invent built-in character personality, backstory, relationship facts, voice mannerisms, or user facts.
7. Ship human-editable identity and behavior templates that contain headings and authoring guidance only.
8. Use asynchronous I/O for HTTP, Redis, WebSocket, provider, file-offload, and tool-result operations.
9. Keep behavior flags disabled by default unless this document explicitly says otherwise.
10. Keep all externally visible protocols documented and covered by tests.
11. Treat Tailscale as a deployment requirement, not an optional note.
12. Stop and ask the project owner before changing a locked decision in section 2.

The implementing agent must not:

- Reference another character or project in runtime code, templates, prompts, docs, tests, examples, log messages, or comments.
- Copy a monolithic bridge and merely disable unwanted sections with flags.
- Add image generation, video generation, intimacy, physical touch, appraisal, reflection, SER, public gateway bots, or accelerated world loops.
- Claim completion without running the milestone tests.
- Store raw tool secrets, audio, full prompt bodies, or private message text in audit logs.

---

## 1. Product Definition

Bridge Core Engine is a self-hosted backend for a persistent character companion.

Each deployment belongs to one person and one character:

- The owner hosts their own Bridge Core Engine instance.
- The owner authors the character through identity files.
- The owner connects one or more private app clients over Tailscale.
- The service keeps one shared companion conversation across the owner's devices.
- The service can support future public-channel gateways through dormant external-user profile infrastructure, but no gateway ships in the initial project.

Bridge Core Engine is backend-only. Desktop and mobile clients are responsible for:

- Rendering text.
- Playing streamed audio chunks.
- Mapping emotion names to sprites and animations.
- Sending heartbeat and device-state frames.
- Executing MCP and device-daemon requests when authorized.

The backend is responsible for:

- Identity and prompt assembly.
- LLM routing and failover.
- Speech-to-text and text-to-speech routing.
- Emotion parsing and wire metadata.
- Conversation history and memory.
- Needs, schedule, availability, life, profiles, and initiative.
- Work sessions and client-executed tools.
- Multi-device synchronization.
- Administrative inspection and wipe operations.

---

## 2. Locked Product Decisions

These decisions are final for the initial implementation.

| Topic | Locked decision |
|---|---|
| Project name | Bridge Core Engine |
| Repository | New separate repository: `bridge-core-engine` |
| Package | Python package named `core` |
| Entrypoint | `bridge_core.py` |
| Service name | `bridge-core.service` |
| Runtime | Python 3.11 or newer |
| Web framework | FastAPI + uvicorn |
| App user model | One registered owner per deployment |
| Devices | Multiple owner devices may connect simultaneously |
| Network | Tailscale-only access, same direct-connect model described in section 27 |
| Authentication | No bearer token or account system in the initial version |
| Identity | Blank `SOUL.md`, `PROFILE.md`, and `STATE.md` templates; no inherited personality |
| Main modes | `companion` and `work` |
| Internal LLM modes | `life`, `proactive`, `owner_profile`, and memory-analysis modes |
| Schedule | Real-time civil calendar only; no loop or accelerated time |
| Memory | Redis short/mid-term plus optional ChromaDB long-term |
| Initiative | Heartbeat-count-driven lightweight initiative, not a full autonomy engine |
| Work | Sessions, MCP agent loop, checkpoints, pause/resume, and device daemon included |
| User schedule | Contextual owner schedule only; no appointments or joint booking |
| Media studio | Excluded |
| Intimacy and touch | Excluded |
| Appraisal, Reflection, SER | Excluded |
| Public gateways | Excluded from initial release |
| Dormant gateway profiles | Included as an inert store and admin API for future gateway integrations |

---

## 3. Goals and Non-Goals

### 3.1 Goals

1. Provide a reliable private companion backend with persistent identity and memory.
2. Allow character authorship entirely through editable templates and configuration.
3. Support text and voice conversations with emotion metadata.
4. Give the character a real-time schedule, life events, changing needs, and bounded initiative.
5. Provide a capable work mode without mixing work history into companion history.
6. Keep provider selection configurable per mode and compatible with local endpoints.
7. Remain understandable enough for one person to operate on a VPS.
8. Preserve owner state across restarts through Redis and optional ChromaDB.
9. Make future Discord, WhatsApp, Telegram, or other gateway work possible without redesigning external-user profile storage.
10. Fail safely when optional providers or services are unavailable.

### 3.2 Non-goals

The initial project must not include:

- Multi-tenant hosting or account registration.
- Public signup, OAuth, billing, subscriptions, quotas, or tenant isolation.
- Discord, WhatsApp, Telegram, Matrix, Slack, or email gateways.
- Image input beyond optional LLM vision support explicitly added later.
- Image generation, video generation, galleries, or media storage.
- Intimacy mode, erotic roleplay, lust simulation, reactions, or physical-action payloads.
- Appraisal, reflection generation, speech emotion recognition, or emotion inference from voice.
- Simulated or accelerated calendar loops.
- Calendar integrations or appointment booking.
- Browser automation.
- Server-side execution of arbitrary MCP tools. MCP execution belongs to the connected client.
- Public internet exposure.
- Hardcoded personality text.

---

## 4. Technology Stack

### 4.1 Required runtime dependencies

Pin versions in `requirements.txt` / `pyproject.toml` at implementation time. Minimum acceptable families:

- Python `>=3.11,<3.14`
- `fastapi>=0.110`
- `uvicorn[standard]>=0.27`
- `httpx>=0.27`
- `redis>=5` with asyncio support
- `pydantic>=2`
- `python-dotenv>=1.0` for `core.env` loading
- `python-multipart` only if HTTP audio upload is implemented

### 4.2 Optional runtime dependencies

- `chromadb` for durable semantic long-term memory
- `tiktoken` or a provider-neutral tokenizer helper for estimates; a deterministic characters-to-token estimate must exist as fallback

### 4.3 Development dependencies

- Standard-library `unittest` is the required test runner.
- `coverage` is optional.
- Formatting and linting may use `ruff`, but behavior tests remain authoritative.

### 4.4 External services

- Redis 7 or newer, bound to loopback/private host only.
- Tailscale on the server and every app device.
- One or more configured LLM endpoints.
- ElevenLabs for TTS when voice output is enabled.
- Deepgram or AssemblyAI for STT when voice input is enabled.
- Tavily for web search when web tools are enabled.

---

## 5. Repository Layout

The initial repository must use this structure unless a concrete implementation constraint requires a documented deviation.

```text
bridge-core-engine/
  bridge_core.py
  pyproject.toml
  requirements.txt
  README.md
  AGENTS.md
  BRIDGE_CORE_ENGINE_SPEC.md
  core.env.template
  .gitignore

  core/
    __init__.py
    app.py
    bridge.py
    config.py
    constants.py
    cache.py
    connections.py
    client_profiles.py
    llm.py
    speech.py
    text_utils.py
    prompts.py
    history.py
    memory.py
    companion_context.py
    context_feed.py
    awareness.py
    needs.py
    bids.py
    rhythm.py
    interaction.py
    state_expression.py
    schedule.py
    life.py
    owner_profile.py
    user_profiles.py
    initiative.py
    sessions.py
    mcp.py
    tool_registry.py
    agent_runs.py
    project_memory.py
    work_skills.py
    daily_tools.py
    user_schedule.py
    device.py
    work_tools.py
    wipe.py

    routes/
      __init__.py
      admin.py
      history.py
      memories.py
      state.py
      life.py
      sessions.py
      profiles.py
      tools.py

  identity/
    SOUL.md
    PROFILE.md
    STATE.md

  skills/
    WORK_SKILLS.md
    DAILY_SKILLS.md

  schedule/
    mon.json
    tue.json
    wed.json
    thu.json
    fri.json
    sat.json
    sun.json
    weekday.json
    weekend.json
    needs.json

  life_events/
    schema_example.disabled.json

  systemd/
    bridge-core.service

  scripts/
    wipe_user.py
    validate_config.py
    smoke_ws.py

  tests/
    test_config.py
    test_text_utils.py
    test_llm.py
    test_speech.py
    test_history.py
    test_memory.py
    test_needs.py
    test_schedule.py
    test_life.py
    test_owner_profile.py
    test_user_profiles.py
    test_initiative.py
    test_work.py
    test_device.py
    test_daily_tools.py
    test_routes.py
    test_wipe.py
    test_turn_guard.py
```

Do not create versioned duplicate bridge files. Git history is the rollback mechanism.

`core/constants.py::VERSION` is the single runtime version source. The connected frame, `/status`, startup log, entrypoint docstring, README, and release tag must match it.

`BRIDGE_CORE_ENGINE_IMPLEMENTATION_PLAN.md` owns locked scope/decisions. `BRIDGE_CORE_ENGINE_SPEC.md` is the living implementation contract and may refine unspecified details but may not override this plan without an approved plan revision. `AGENTS.md` contains workflow rules only and has lower authority than both design documents.

---

## 6. Architectural Invariants

### 6.1 Async discipline

- Every HTTP, Redis, WebSocket, provider, and tool-result operation must be awaited.
- Blocking Chroma operations must run in a dedicated single-thread executor.
- File reads that occur every turn must use mtime caches or be offloaded if large.
- Never call `time.sleep`, `requests`, or synchronous Redis inside the event loop.
- Background work uses `asyncio.create_task` and must catch its own exceptions.
- Lifespan shutdown must cancel and await background tasks.

### 6.2 Single-owner app law

- One configured `OWNER_USER_ID` is valid for app WebSocket and HTTP conversation paths. This id is routing, not authentication.
- A connection claiming another app user id must be rejected with a clear protocol error.
- Multiple devices for the owner are allowed.
- All owner devices share companion history, memory, needs, schedule, life, and profile state.
- Connection-local state includes capabilities, heartbeat, device arming, and pending MCP request routing.
- All companion turns acquire one per-owner turn lock because every device mutates the same companion thread, needs, bids, profile, and deferred queue.
- Work turns acquire one per-session lock. A connection lock alone is insufficient for shared state.

### 6.3 Identity authority

Prompt authority order:

1. `SOUL.md`
2. `PROFILE.md`
3. `STATE.md` expression output for the current turn
4. Owner lived profile
5. Context feed when enabled; otherwise direct durable-memory, life, and mid-term chapter blocks
6. Live conversation history

Lower layers never override higher identity layers.

The engine must never edit `SOUL.md`, `PROFILE.md`, `STATE.md`, `WORK_SKILLS.md`, or `DAILY_SKILLS.md`.

### 6.4 Read-only polling

- GET endpoints documented as read-only must not materialize missing stores or advance simulation timestamps.
- Each stateful engine must expose `peek` for read-only views and `evaluate` for behavior paths that may persist lazy evaluation.
- Tests must include a cache write guard around poll endpoints.

### 6.5 Owner time versus character time

There is only real civil time. No loop-scaled time exists.

- Character schedule time uses `CHARACTER_TIMEZONE`.
- Owner schedule and heartbeat context use the owner's declared timezone, falling back to `OWNER_TIMEZONE`.
- Cooldowns and TTLs use UTC server timestamps.
- Human-readable output converts to the relevant civil timezone.

Timezone authority:

- `CHARACTER_TIMEZONE` is durable character-schedule authority.
- `OWNER_TIMEZONE` is durable owner-schedule and owner-day authority, including initiative daily reset.
- WS query/heartbeat timezone is connection-local current context for displaying the owner's current local time and STT/static reply localization. It does not rewrite `OWNER_TIMEZONE` or owner schedule.
- If devices report different connection-local timezones, the source connection's timezone is used for that turn only; durable owner schedule and daily caps continue to use `OWNER_TIMEZONE`.
- Explicit owner-schedule update is the only path that changes its timezone, and it requires validation plus admin confirmation.

### 6.6 No raw sensitive audit data

Audit/profile metadata may store categories, counters, ids, timestamps, success flags, and bounded paths/commands.

Audit/profile metadata must not store:

- Full inbound message text.
- Full generated reply text.
- Provider API keys.
- Audio bytes or transcripts in diagnostic stores.
- Tool output bodies.
- Full prompts.

Conversation history and memory are separate from audit metadata and follow their own retention rules.

### 6.7 Flag-off parity

Every optional behavior must have a code default that is inert. Disabling a feature must prevent its prompt injection, store writes, background tasks, and LLM calls.

---

## 7. Identity and Prompt System

### 7.1 Identity templates

`identity/SOUL.md` must ship with headings only, for example:

```markdown
# SOUL

<!-- Highest authority: who the character is. Replace comments with authored content. -->

## Identity

## Core temperament

## Values

## Voice

## Boundaries

## Relationship posture

## World premise
```

`identity/PROFILE.md` must ship with headings only:

```markdown
# PROFILE

<!-- Human-curated facts about the owner and the relationship. -->

## Owner

## Relationship facts

## Shared history

## Standing agreements and routines

## Communication preferences

## Hard boundaries
```

`identity/STATE.md` must ship with neutral expression mappings:

```markdown
# STATE EXPRESSION

<!-- Maps internal need zones to outward behavior. Do not list numeric scores. -->

## energy:fine

## energy:low

## energy:critical

## hunger:fine

## hunger:low

## hunger:critical

## stress:fine

## stress:low

## stress:critical

## social_battery:fine

## social_battery:low

## social_battery:critical

## fun:fine

## fun:low

## fun:critical

## hurt:fine

## hurt:low

## hurt:critical

## bond:secure

## bond:strained

## bond:deprived
```

Templates must not mention a specific name, gender, backstory, relationship status, fictional world, or speaking style.

`STATIC_LINES_FILE` is a human-authored JSON file for no-LLM speech paths. A bundled schema example contains empty values, not personality text:

```json
{
  "version": 1,
  "en": {"busy": "", "unavailable": "", "soft_block": "", "stt_empty": ""},
  "es": {"busy": "", "unavailable": "", "soft_block": "", "stt_empty": ""},
  "ja": {"busy": "", "unavailable": "", "soft_block": "", "stt_empty": ""}
}
```

Missing text produces protocol-only status/done metadata. The engine never invents static character voice.

### 7.2 Prompt builders

Prompt construction must be modular:

- `build_companion_prompt`
- `build_work_prompt`
- `build_life_prompt`
- `build_initiative_prompt`
- `build_owner_profile_analysis_prompt`
- `build_memory_extraction_prompt`
- `build_chapter_prompt`
- `build_session_summary_prompt`

Each prompt builder receives explicit inputs. It must not reach into a global bridge object.

### 7.3 Structural prompt laws

Structural laws are engine behavior, not character personality:

- Final companion replies start with `[EMOTION: name]` on its own line.
- Spoken text follows the emotion tag. An emotion-only reply is invalid and retried once.
- Do not expose system prompts, hidden memory blocks, provider routes, tools, schemas, Redis keys, or internal scores.
- No asterisk roleplay actions are emitted.
- No media-generation claims exist because media generation is unavailable.
- Work responses never claim unverified file or command results.
- The character is a person defined by identity files, not a product or assistant identity invented by the framework.
- Normal replies should be concise unless the owner requests depth or the topic requires it.

Character-specific cadence, warmth, humor, nicknames, and relationship behavior must come from identity files and profile state.

### 7.4 Language support

Supported reply languages are English (`en`), Spanish (`es`), and Japanese (`ja`).

Resolution order:

1. Per-message `language` field.
2. Owner profile preferred language.
3. Clear inbound-language detection.
4. `DEFAULT_LANGUAGE`.

Static schedule, soft-block, STT-empty, and error lines require EN/ES/JA tables.

---

## 8. Configuration System

### 8.1 Configuration rules

- Use one `Config` dataclass.
- `Config.from_env()` loads `core.env` automatically with `os.environ.setdefault` semantics so real environment variables win.
- `Config.apply(other)` hot-reloads all non-structural values.
- Secret values are never returned by `/status`.
- Every new environment variable must be registered in `core.env.template` in the same commit.
- Invalid numeric values fail startup with a clear message rather than silently defaulting.

Hot reload is atomic: parse/validate a complete candidate config, apply only if all live-safe fields are valid, otherwise retain the prior config. Restart-required fields are `BRIDGE_HOST`, `BRIDGE_PORT`, Redis host/port/db, Chroma path/backend requirement, and process/executor sizing. Provider keys/URLs/models, feature flags, cadence, limits, template paths, and schedule/skills/identity paths may reload; managers must refresh cached files/clients safely. `/admin/reload-config` returns `applied_fields`, `restart_required_fields`, and validation errors without secrets.

### 8.2 Provider modes

Supported LLM modes:

- `companion`
- `work`
- `life`
- `proactive`
- `owner_profile`
- `memory`
- `session_summary`

For each mode support:

- `{MODE}_PROVIDERS`
- `{MODE}_MODEL`
- `{MODE}_{PROVIDER}_MODEL`
- `{MODE}_{PROVIDER}_SERVICE_TIER` where meaningful
- `{MODE}_TEMPERATURE`
- `{MODE}_MAX_TOKENS`

Empty specialized chains inherit `COMPANION_PROVIDERS`, then `LLM_CHAIN`, unless the mode explicitly documents otherwise.

### 8.3 Initial environment inventory

The final template may add implementation-specific fields, but it must cover at least:

```dotenv
# Server
# Required in production: set to this server's Tailscale IPv4/IPv6 address.
# Safe local default does not expose the service.
BRIDGE_HOST=127.0.0.1
BRIDGE_PORT=8766
TAILSCALE_REQUIRED=true
TAILSCALE_FIREWALL_ACK=false
LOG_LEVEL=info
OWNER_USER_ID=owner
DEFAULT_LANGUAGE=en
OWNER_TIMEZONE=UTC
CHARACTER_TIMEZONE=UTC

# Redis and Chroma
REDIS_HOST=127.0.0.1
REDIS_PORT=6379
REDIS_DB=0
CHROMA_ENABLED=false
CHROMA_PATH=./data/chroma

# LLM routing
LLM_CHAIN=fireworks,chutes,ollama,openai_compat
LLM_CHAIN_DEADLINE_SECONDS=75
LLM_HISTORY_MESSAGE_BUDGET=40
LLM_STREAMING_ENABLED=false

FIREWORKS_API_KEY=
FIREWORKS_URL=https://api.fireworks.ai/inference/v1
FIREWORKS_MODEL=
FIREWORKS_TIMEOUT=60
FIREWORKS_SERVICE_TIER=

CHUTES_API_KEY=
CHUTES_URL=
CHUTES_MODEL=
CHUTES_TIMEOUT=60

OLLAMA_URL=http://127.0.0.1:11434
OLLAMA_MODEL=
OLLAMA_TIMEOUT=0

OPENAI_COMPAT_API_KEY=
OPENAI_COMPAT_URL=
OPENAI_COMPAT_MODEL=
OPENAI_COMPAT_TIMEOUT=60

COMPANION_PROVIDERS=
COMPANION_MODEL=
COMPANION_TEMPERATURE=0.8
COMPANION_MAX_TOKENS=1200

WORK_PROVIDERS=
WORK_MODEL=
WORK_TEMPERATURE=0.3
WORK_MAX_TOKENS=4000

LIFE_PROVIDERS=
LIFE_MODEL=
LIFE_TEMPERATURE=0.8
LIFE_MAX_TOKENS=400

PROACTIVE_PROVIDERS=
PROACTIVE_MODEL=
PROACTIVE_TEMPERATURE=0.8
PROACTIVE_MAX_TOKENS=120

OWNER_PROFILE_LLM_ENABLED=false
OWNER_PROFILE_PROVIDERS=
OWNER_PROFILE_MODEL=
OWNER_PROFILE_MAX_TOKENS=400

# Identity
SOUL_FILE=
PROFILE_FILE=
STATE_FILE=
WORK_SKILLS_FILE=
DAILY_SKILLS_FILE=

# TTS
TTS_ENABLED=false
ELEVENLABS_API_KEY=
ELEVENLABS_URL=https://api.elevenlabs.io/v1
ELEVENLABS_VOICE_ID=
ELEVENLABS_MODEL=eleven_flash_v2_5
TTS_OUTPUT_FORMAT=mp3_44100_128
TTS_CHUNK_THRESHOLD=150
TTS_CHUNK_SIZE=150
TTS_CHUNK_SPACING_MS=50
TTS_VOICE_PROFILE_FILE=

# STT
STT_ENABLED=false
STT_PROVIDER=deepgram
STT_LANGUAGE=en
DEEPGRAM_API_KEY=
DEEPGRAM_URL=https://api.deepgram.com/v1/listen
DEEPGRAM_MODEL=nova-3
DEEPGRAM_TIMEOUT=45
ASSEMBLYAI_API_KEY=
ASSEMBLYAI_URL=https://api.assemblyai.com/v2
ASSEMBLYAI_SPEECH_MODEL=best
ASSEMBLYAI_TIMEOUT=90
ASSEMBLYAI_POLL_INTERVAL=1

# History and memory
MAX_HISTORY_TURNS=80
COMPANION_SHORT_WINDOW_ENABLED=true
COMPANION_LIVE_WINDOW_MESSAGES=8
COMPANION_COMPACT_THRESHOLD=60
COMPANION_KEEP_RECENT=16
MIDTERM_INJECT_CHAPTERS=4
MEMORY_EXTRACTION_ENABLED=false
MEMORY_CLEANUP_ENABLED=false
MEMORY_CLEANUP_INTERVAL_HOURS=12
MEMORY_MAX_PER_USER=1000

# Needs and interaction
NEEDS_ENABLED=false
BIDS_ENABLED=false
RHYTHM_ENABLED=false
STATE_EXPRESSION_ENABLED=false
NEEDS_PROFILE_FILE=

# Owner profile
OWNER_PROFILE_ENABLED=false
OWNER_PROFILE_INJECT=true
OWNER_STATUS_START=acquaintance
OWNER_TRUST_START=50
OWNER_CLOSENESS_START=0
OWNER_APPEAL_START=50
OWNER_DESIRABILITY_START=50
OWNER_BOUNDARY_PENALTIES_ENABLED=false
OWNER_SOFT_BLOCK_ENABLED=false
OWNER_STATUS_DRIFT_ENABLED=false
OWNER_AGREEMENTS_ENABLED=false
OWNER_AGREEMENT_AFTERMATH_ENABLED=false

# Dormant gateway profiles
EXTERNAL_USER_PROFILE_STORE_ENABLED=true
EXTERNAL_USER_PROFILES_BEHAVIOR_ENABLED=false
EXTERNAL_USER_PROFILE_LLM_ENABLED=false

# Schedule and life
SCHEDULE_ENABLED=false
SCHEDULE_DIR=
LIFE_ENABLED=false
LIFE_EVENTS_DIR=
LIFE_DAILY_MIN=0
LIFE_DAILY_MAX=4
LIFE_EVENT_COOLDOWN_MINUTES=40
LIFE_POLL_INTERVAL_SECONDS=60
LIFE_MISSED_BLOCK_POLICY=current_only

# Heartbeat initiative
HEARTBEAT_ENABLED=true
INITIATIVE_ENABLED=false
INITIATIVE_MIN_HEARTBEATS=3
INITIATIVE_HEARTBEAT_WINDOW_SECONDS=900
INITIATIVE_HEARTBEAT_COUNT_INTERVAL_SECONDS=60
INITIATIVE_MIN_GAP_SECONDS=3600
INITIATIVE_DAILY_MAX=3
INITIATIVE_REQUIRE_SCHEDULE_FREE=true
INITIATIVE_ELIGIBILITY_CHANCE=0.35

# Work and MCP
WORK_ENABLED=true
SESSIONS_ENABLED=true
SESSION_HISTORY_TURNS=80
SESSION_SUMMARY_ENABLED=true
WORK_SKILLS_ENABLED=true
MCP_PROXY_ENABLED=true
MCP_TOOL_TIMEOUT=120
MCP_MAX_ITERATIONS=20
MCP_VERIFICATION_ENABLED=true
MCP_VERIFICATION_RETRIES=2
AGENT_CHECKPOINTS_ENABLED=true

# Device daemon
DEVICE_ENABLED=false
DEVICE_TOOL_TIMEOUT=120
DEVICE_PER_TURN_CALL_CAP=20
DEVICE_MAX_OUTPUT_CHARS=30000
DEVICE_SHELL_TIMEOUT=120
DEVICE_SHELL_TIMEOUT_MAX=600
DEVICE_WRITE_ROOTS=

# Daily tools and web
DAILY_TOOLS_ENABLED=false
DAILY_WEB_ENABLED=false
TAVILY_API_KEY=
DAILY_WEB_SEARCH_CAP=1
DAILY_WEB_OPEN_CAP=2

# Contextual owner schedule
USER_SCHEDULE_ENABLED=false

# Input and context budgets
MAX_AUDIO_BYTES=15728640
ALLOWED_AUDIO_CONTENT_TYPES=audio/webm,audio/ogg,audio/mpeg,audio/wav
CONTEXT_FEED_MAX_TOKENS=700
NEEDS_MAX_ELAPSED_HOURS=48
CHROMA_REQUIRED=false

# Additional analysis routes
MEMORY_PROVIDERS=
MEMORY_MODEL=
MEMORY_MAX_TOKENS=400
SESSION_SUMMARY_PROVIDERS=
SESSION_SUMMARY_MODEL=
SESSION_SUMMARY_MAX_TOKENS=500

# Client emotion/animation manifest
EMOTIONS_FILE=
STATIC_LINES_FILE=
```

No application-auth environment variable ships in v1. Network authorization is the Tailscale ACL and host firewall contract in section 27.

### 8.4 Canonical release profiles

The repository ships:

1. `core.env.template`: safe behavior-inert values, blank secrets, localhost bind.
2. `core.env.full.example`: intended single-owner feature profile with needs, state expression, owner profile, schedule, life, memory extraction/cleanup, heartbeat initiative, work, sessions, daily tools, contextual owner schedule, and device support enabled where credentials/files exist. External-profile behavior remains off.

CI must test both the inert template and the full enabled profile with fake providers. A feature is not considered delivered merely because code exists behind a flag; its enabled-profile integration test must pass.

---

## 9. LLM Provider Router

### 9.1 Supported providers

1. Fireworks, OpenAI-compatible.
2. Chutes, OpenAI-compatible.
3. Ollama, OpenAI-compatible local endpoint.
4. Generic `openai_compat`, configurable URL/key/model.

### 9.2 Provider call contract

Every provider call must validate:

```python
if not isinstance(data, dict):
    raise RuntimeError("LLM returned a non-object response")

choices = data.get("choices")
if not choices:
    error_obj = data.get("error") or {}
    if isinstance(error_obj, dict):
        error_message = error_obj.get("message", "Unknown provider error")
    else:
        error_message = str(error_obj)
    raise RuntimeError(f"LLM provider error: {error_message}")
```

Never assume `response.json()` is a dict or that `choices[0].message.content` exists.

### 9.3 Failover behavior

- Routes are ordered `(provider, model)` pairs.
- Missing provider credentials skip that provider with one warning.
- Timeout, HTTP error, malformed JSON, empty choices, and empty response move to the next provider.
- Tool loops pin the first successful provider/model for subsequent iterations unless it fails.
- Total chain time is bounded by `LLM_CHAIN_DEADLINE_SECONDS`.
- Usage is additive across attempts, retries, and tool iterations.
- Final `done.tokens` is emitted only when usage exists.

### 9.4 Generic OpenAI-compatible provider

The generic provider must support:

- Base URL with or without `/v1` normalized safely.
- Bearer key optional for trusted local endpoints.
- Chat completions.
- Function/tool calling.
- Optional SSE streaming.
- Per-mode model overrides.

It must not assume vendor-specific fields beyond OpenAI-compatible chat completions.

---

## 10. WebSocket and HTTP Protocol

### 10.1 WebSocket endpoint

```text
GET /ws/{user_id}?client_type=desktop|mobile&device_id=<optional>&tz=<IANA timezone>
```

The server rejects `user_id != OWNER_USER_ID` on app paths.

After accept:

```json
{
  "type": "connected",
  "connection_id": "conn_<uuid>",
  "server_version": "0.x.y",
  "capabilities": ["audio", "voice_input", "work", "mcp", "device"],
  "server_time": "ISO-8601 UTC"
}
```

### 10.2 Inbound text turn

```json
{
  "type": "text",
  "text": "message",
  "mode": "companion|work",
  "language": "en|es|ja",
  "wants_audio": true,
  "session_id": null,
  "project_id": null,
  "context": {
    "client": {},
    "mcp_servers": [],
    "mcp_health": {},
    "available_skills": []
  }
}
```

Unknown modes return an error frame and do not silently become companion mode.

### 10.3 Inbound audio turn

```json
{
  "type": "audio",
  "audio_base64": "...",
  "audio_content_type": "audio/webm",
  "stt_language": "en",
  "mode": "companion",
  "wants_audio": true
}
```

The server returns an STT frame before the normal turn:

```json
{
  "type": "stt",
  "text": "transcript",
  "provider": "deepgram",
  "language": "en"
}
```

Empty/failed STT returns a localized static line and terminal `done`; it makes no LLM/history/memory call.

### 10.4 Status frame

```json
{
  "type": "status",
  "status": "thinking|working|question|request_permission|planning|completed|unavailable|error",
  "message": "display-safe status",
  "emotion": "thinking",
  "timestamp": "ISO-8601 UTC"
}
```

Status emotions never become final reply emotions unless the LLM independently chooses a valid final emotion.

### 10.5 Final `done` frame

```json
{
  "type": "done",
  "id": "msg_<uuid>",
  "text": "display-safe final text",
  "emotion": "neutral",
  "segments": [
    {"text": "display-safe segment", "emotion": "neutral"}
  ],
  "mode": "companion",
  "provider": "fireworks",
  "model": "model-id",
  "initiated_by": "user",
  "tokens": {
    "prompt": 0,
    "completion": 0,
    "total": 0
  }
}
```

`tokens` is omitted if provider usage is unavailable.

### 10.6 Audio chunk frame

```json
{
  "type": "audio_chunk",
  "id": "msg_<same done id>",
  "text": "chunk text",
  "emotion": "happy",
  "chunk_index": 0,
  "total_chunks": 2,
  "is_final": false,
  "audio": "base64 encoded bytes",
  "audio_format": "mp3"
}
```

The `done` frame is sent before audio generation completes. Audio chunks follow.

After the last attempted chunk, send exactly one stream terminal frame:

```json
{
  "type": "audio_complete",
  "id": "msg_<same done id>",
  "succeeded_chunks": 2,
  "failed_chunks": 0
}
```

If one or more chunks fail, still send `audio_complete` with a non-zero failed count and send one bounded `audio_error` status before it. The client must never wait indefinitely for audio. Disconnect cancels pending synthesis. Audio is sent only to the requesting connection; other devices receive text/segments through `chat_sync`.

### 10.7 Intentional silence/defer frame

```json
{
  "type": "done",
  "id": "msg_<uuid>",
  "mode": "companion",
  "emotion": "neutral",
  "segments": [{"text": "display-safe text", "emotion": "neutral"}],
  "ignored": true,
  "reason": "schedule_unavailable",
  "initiated_by": "user"
}
```

If an answer is genuinely queued, include `deferred: true`. Do not set `deferred` for messages that will never be answered later.

### 10.8 Heartbeat

```json
{
  "type": "heartbeat",
  "sequence": 123,
  "timezone": "America/Santiago",
  "activity_kind": "work|play|other|unknown",
  "place": "home|work|mobile|unknown",
  "last_input_at": 1780000000.0
}
```

Response:

```json
{
  "type": "heartbeat_ack",
  "server_time": "ISO-8601 UTC",
  "initiative_counter": 2
}
```

Heartbeat payload is connection-ephemeral except bounded timestamps/counters required by initiative. `sequence` is a required non-negative 64-bit integer scoped to the connection. A valid heartbeat timestamp may be at most 60 seconds in the future and 10 minutes old. Replayed/out-of-order sequences are acknowledged but do not count toward initiative.

### 10.9 Multi-device `chat_sync`

Every successfully persisted owner or assistant companion message is fanned out to other owner connections:

```json
{
  "type": "chat_sync",
  "role": "user|assistant",
  "text": "display-safe text",
  "emotion": "neutral",
  "mode": "companion",
  "initiated_by": "user|character",
  "id": "msg_<uuid>",
  "origin_connection_id": "conn_<uuid>",
  "ts": "ISO-8601 UTC"
}
```

### 10.10 HTTP webhook

```text
POST /message
```

Accepts companion and tool-less work turns and returns the terminal response JSON. HTTP-originated turns never invoke MCP or the device daemon because there is no originating WS connection with explicit execution authority. A work request requiring tools returns a deterministic `tools_require_websocket` error.

Clients may reconcile uncertain delivery after reconnect:

```json
{"type":"message_ack","id":"msg_<uuid>"}
```

The server accepts an acknowledgement only for the configured owner and an existing `delivery_unknown`/pending row. Duplicate acknowledgements are idempotent.

### 10.11 MCP frames

The WS reader must remain active while a turn runs in a background task so result frames can resolve pending futures.

Server request:

```json
{
  "type": "mcp_tool_request",
  "id": "mcp_<uuid>",
  "run_id": "run_<uuid>",
  "server": "filesystem",
  "tool": "read_file",
  "arguments": {"path": "README.md"},
  "timeout_seconds": 120
}
```

Client result:

```json
{
  "type": "mcp_result",
  "id": "mcp_<same uuid>",
  "run_id": "run_<same uuid>",
  "ok": true,
  "result": {"content": "structured or text result"},
  "error": null,
  "truncated": false
}
```

Rules:

- Correlate only by `id` and verify `run_id` and originating connection.
- One result resolves one future and deletes the Redis backup.
- Duplicate/stale/wrong-connection results are ignored and logged as bounded metadata.
- Results larger than the configured cap are truncated by the client and again by the server; `truncated=true` forces verification caution.
- Timeout creates a structured failed tool result so the agent can continue or report failure.

### 10.12 Device-daemon frames

Client arm/disarm:

```json
{
  "type": "device_state",
  "armed": true,
  "level": "read|full",
  "roots": ["/Users/owner/Projects"],
  "protocol_version": 1
}
```

Server request:

```json
{
  "type": "device_tool_request",
  "id": "dev_<uuid>",
  "run_id": "run_<uuid>",
  "tool": "device_read|device_list|device_stat|device_find|device_write|device_shell",
  "arguments": {},
  "timeout_seconds": 120,
  "max_output_chars": 30000
}
```

Client result:

```json
{
  "type": "device_tool_result",
  "id": "dev_<same uuid>",
  "run_id": "run_<same uuid>",
  "ok": true,
  "result": {"content": "bounded result"},
  "error": null,
  "truncated": false,
  "duration_ms": 42
}
```

Results are one-time, connection-bound, run-bound, and deleted after consumption. Device reconnect starts disarmed and invalidates pending requests for the old connection.

Version 1 device argument schemas reject unknown fields:

```text
device_read:
  {path: string<=4096, offset_bytes?: int>=0, max_bytes?: int 1..DEVICE_MAX_OUTPUT_CHARS, encoding?: "utf8|base64"}

device_list:
  {path: string<=4096, max_entries?: int 1..1000, include_hidden?: bool}

device_stat:
  {path: string<=4096}

device_find:
  {root: string<=4096, pattern: string<=256, max_results?: int 1..500, max_depth?: int 0..32}

device_write:
  {path: string<=4096, content: string<=configured write cap, encoding: "utf8|base64", overwrite?: bool, create_parents?: bool}

device_shell:
  {
    cwd: string<=4096,
    argv?: array[string<=4096] length 1..128,
    shell_command?: string<=16000,
    timeout_seconds?: int 1..DEVICE_SHELL_TIMEOUT_MAX,
    environment?: object[string,string] max 32 entries
  }
```

`argv` and `shell_command` are mutually exclusive. `shell_command` is allowed only at full level and is visibly permissioned by the client. Environment keys/values are filtered against allowlists and secret patterns. Read/list/stat/find are available at read/full; write/shell require full.

---

## 11. Connection and Concurrency Model

- `ConnectionManager` maps `connection_id -> Connection` and `owner user_id -> connection ids`.
- Connecting a new device never closes an existing connection.
- Disconnect removes exactly one connection.
- Each connection tracks local activity, but companion turn serialization uses a per-owner mutex shared across devices.
- Work turn serialization uses a per-session mutex; two unrelated sessions may run concurrently if their tool origins are distinct.
- Heartbeats are never turns and must be acknowledged while another turn is active.
- MCP requests route only to the originating connection.
- Device-daemon requests may route to any armed owner connection, chosen by access level then recent owner activity.
- Initiative never emits into a connection with an active turn.
- Catch-up and initiative have separate per-owner locks to prevent duplicate sends.
- Pending tasks and in-process futures are removed on success, timeout, disconnect, and shutdown.
- Shared Redis list updates use atomic append/transaction operations or compare-and-set versions; never load-modify-save a shared history list without its owner/session lock.
- Every companion-history mutator, including turns, compaction, session close, catch-up, initiative, wipe, and admin correction, acquires the per-owner history lock or uses an equivalent atomic version transaction.

---

## 12. Companion Turn Lifecycle

The companion turn path must execute in this order:

1. Validate owner user id and connection.
2. Decode text or transcribe audio.
3. Reject empty input with terminal error/done semantics.
4. Resolve mode (`companion` or `work`).
5. Evaluate owner-profile soft block for companion mode only. Work bypasses relationship soft block.
6. Resolve character schedule and effective availability.
7. Apply busy/unavailable/defer policy.
8. Run bids/rhythm owner-message hooks when enabled.
9. Stamp owner activity.
10. Resolve work session/tooling when mode is work.
11. Persist the user message as received with stable id/timestamp before provider execution, then immediately fan it out to other owner connections. Fanout failure does not roll back persistence. Provider failure leaves the user message plus a bounded failed-turn marker, never a fabricated assistant reply.
12. Load prior delivered history for the correct channel/session; do not duplicate the newly persisted current user row in the prompt.
13. Retrieve mid-term chapters and long-term memory.
14. Build context feed and awareness blocks.
15. Evaluate needs and build `[CHARACTER STATE]` expression.
16. Inject owner lived profile.
17. Build system prompt from identity files and structural laws.
18. Add bounded prior live history.
19. Add post-history critical rules and per-turn language lock, then append the current user input last.
20. Call LLM router or MCP agent loop.
21. Validate non-empty response and required emotion tag; retry once if emotion-only or invalid.
22. Run generic compliance guard if enabled.
23. Strip reasoning blocks, asterisk actions, unsupported control tags, and unsafe tool narration.
24. Parse per-segment emotions and produce display-safe text.
25. Append the assistant row with `delivery_state=pending`, stable id, timestamp, segments, and emotion.
26. Send `done` to the requesting connection and require the send helper to return a delivery boolean.
27. On successful source delivery, atomically mark the assistant row delivered, then fan out `chat_sync` immediately to other devices before TTS.
28. On source delivery failure, mark the assistant row undelivered, exclude it from future prompts, do not fan it out, and expose it only in diagnostics/admin history.
29. Apply successful-turn needs/bid effects only after delivered assistant state.
30. Trigger background memory extraction and history compaction only from delivered exchanges.
31. Start pipelined sequential TTS and send audio chunks to the source connection.
32. Stamp last-assistant activity.
33. Enqueue optional owner-profile strict-JSON analysis.
34. Clear delivered pending life mentions only after delivered assistant state.

Crash recovery for assistant delivery state:

- On startup, assistant rows left `pending` beyond a short threshold become `delivery_unknown`, not silently delivered or undelivered.
- Clients may acknowledge received message ids on reconnect/history sync. A matching acknowledgement changes `delivery_unknown -> delivered` and permits normal prompt/memory use.
- Until acknowledged, unknown rows remain visible in history reconciliation metadata but are excluded from prompt history, memory extraction, needs/bid effects, and initiative accounting.
- Wipe/admin history views expose state so an operator can resolve or delete an unknown row.

Any early refusal or defer path must explicitly state which hooks do not run.

---

## 13. Emotion, Sprite, Animation, and Audio Contract

### 13.1 Emotion palette

Define a versioned final-emotion palette in `constants.py`. Initial names should remain generic and client-friendly:

```text
neutral
happy
sad
angry
annoyed
embarrassed
surprised
confused
worried
scared
tired
sleepy
excited
playful
affectionate
confident
serious
shy
```

Status-only emotions:

```text
thinking
working
question
request_permission
```

Unknown final emotions normalize to `neutral`.

The v1 palette is fixed for wire compatibility. Character identity does not need to use every emotion. Adding/removing/renaming an emotion requires a manifest protocol-version change and client compatibility note; deployment-local customization may change sprite/animation keys and TTS settings, not wire names.

### 13.2 Emotion syntax

```text
[EMOTION: happy]
Spoken text.
```

Multiple segment tags are allowed:

```text
[EMOTION: happy]
First sentence.
[EMOTION: serious]
Second sentence.
```

All tags are stripped from display text and TTS input.

### 13.3 Backend animation support

The backend does not store sprite/image/animation binary assets. `EMOTIONS_FILE` is the source of truth for sprite keys, animation keys, TTS speeds, and optional status mappings. A bundled neutral schema-complete JSON file ships without asset URLs. The backend validates it at startup and exposes:

```text
GET /emotions
```

Example:

```json
{
  "version": 1,
  "emotions": [
    {"name": "neutral", "tts_speed": 1.0, "sprite_key": "neutral", "animation_key": "neutral_idle"}
  ],
  "status_emotions": ["thinking", "working", "question", "request_permission"]
}
```

The manifest keys are stable contracts; clients map them to local or CDN assets.

`constants.py` owns the allowed wire names. The manifest may not introduce unknown names. Status-to-emotion mapping is also single-source, for example: thinking->thinking, planning->thinking, working->working, question->question, request_permission->request_permission, completed->confident, unavailable->neutral, error->serious.

### 13.4 Chunking

- Preserve sentence boundaries where possible.
- Preserve each chunk's active emotion.
- Do not synthesize empty chunks.
- Do not synthesize status messages as final reply audio.
- Generate sequentially with one-chunk lookahead, never unrestricted parallel calls.
- TTS failure never removes the text response.

---

## 14. Speech Providers

### 14.1 ElevenLabs TTS

- Use one shared async HTTP client.
- Voice id is configured, never hardcoded.
- Accept a voice-profile JSON file mapping emotion to stability, similarity, style, speed, and repeat variations.
- Missing emotion profile falls back to neutral/default settings.
- Audio is never stored server-side.
- Log byte count/duration metadata only.

### 14.2 Deepgram STT

- Pre-recorded endpoint.
- Configurable model and language.
- Per-message language overrides global language.
- Validate decoded audio size before upload.
- Return empty string on no transcript; caller owns localized fallback.
- Accept only content types in `ALLOWED_AUDIO_CONTENT_TYPES`.
- Decode raw base64 or a matching data URI; reject mismatched declared/data-URI types.
- Validate decoded size against `MAX_AUDIO_BYTES` and basic container signatures where practical. Never trust extension or declared MIME alone.

### 14.3 AssemblyAI STT

- Upload audio bytes.
- Submit transcript job.
- Poll asynchronously until completed/error/timeout.
- Configurable speech model and poll interval.
- Never log audio or provider response bodies containing transcript text at INFO.
- Use the same size/MIME/signature validation contract as Deepgram before upload.

---

## 15. Needs and Interaction System

### 15.1 Stats

Initial stats, all clamped to 0-100:

- `energy`
- `hunger`
- `stress`
- `social_battery`
- `fun`
- `bond`
- `hurt`

There is no lust stat.

Directionality:

- Higher `energy`, `social_battery`, `fun`, and `bond` are healthier.
- Higher `hunger`, `stress`, and `hurt` are worse.
- Zone labels describe condition, not numeric magnitude. For adverse stats, high numeric values map to `critical`; for healthy stats, low numeric values map to `critical`.
- Prefer condition-specific prompt names such as `hurt:critical` rather than the ambiguous phrase `hurt:low`.

The shipped `needs.json` must be schema-complete but use conservative neutral defaults. Example shape:

```json
{
  "version": 1,
  "stats": {
    "energy": {"start": 70, "direction": "higher_is_better", "low_below": 35, "critical_below": 15, "rate_per_hour": -1.0},
    "hunger": {"start": 20, "direction": "lower_is_better", "low_above": 55, "critical_above": 80, "rate_per_hour": 1.5},
    "stress": {"start": 20, "direction": "lower_is_better", "low_above": 55, "critical_above": 80, "rate_per_hour": -0.25},
    "social_battery": {"start": 70, "direction": "higher_is_better", "low_below": 35, "critical_below": 15, "rate_per_hour": 0.5},
    "fun": {"start": 60, "direction": "higher_is_better", "low_below": 35, "critical_below": 15, "rate_per_hour": -0.5},
    "bond": {"start": 50, "direction": "higher_is_better", "strained_below": 35, "deprived_below": 15, "rate_per_hour": -0.05},
    "hurt": {"start": 0, "direction": "lower_is_better", "low_above": 25, "critical_above": 60, "rate_per_hour": -0.2}
  },
  "activity_multipliers": {},
  "turn_effects": {
    "companion_brief": {"bond": 0.2, "social_battery": -0.1},
    "companion_engaged": {"bond": 0.5, "social_battery": -0.25},
    "work": {"energy": -0.1, "stress": 0.1}
  },
  "shutdown": {"enabled": false, "energy_below": 10, "social_battery_below": 10}
}
```

These are engine-safe starting values, not character calibration. Template comments must instruct deployers to tune them. Schema version migrations are explicit functions tested from every released version; unknown future versions fail startup rather than corrupt state.

### 15.2 Evaluation

- State persists in Redis per owner.
- Store `last_eval_ts` and current values.
- `evaluate` computes duration from UTC timestamps and applies the currently resolved civil schedule/activity multipliers. DST changes never alter elapsed duration.
- `peek` computes a projected snapshot without writes.
- Server restart does not reset stats.
- Large elapsed gaps are bounded by `NEEDS_MAX_ELAPSED_HOURS` to prevent catastrophic jumps after long downtime; store the skipped-duration diagnostic count without replaying it later.

### 15.3 Zones

Generic zones:

- `fine`
- `low`
- `critical`

Bond zones:

- `secure`
- `strained`
- `deprived`

Hurt zones:

- `fine`
- `low`
- `critical`

Thresholds and rates live in `schedule/needs.json`, not hardcoded in engine logic.

### 15.4 Effective availability

Effective availability combines:

- Raw schedule availability.
- Critical social-battery/energy shutdown if enabled.
- Explicit owner-profile soft block for companion mode.

Work mode remains usable during relationship soft block but still respects sleep/critical capacity according to its deterministic work availability policy.

### 15.5 Bids and bond

Connection bids represent character-initiated attempts at connection.

- Register only after confirmed initiative delivery.
- User replies may satisfy open bids.
- Reply quality is deterministic and does not use LLM calls.
- Ordinary companion turns can refill bond behind a flag.
- Caps and min-gap prevent farming.
- No intimacy or kink bonuses exist.

Bid state stores only id, kind, size, sent timestamp, expiry, answered timestamp, and result. It never stores message text. Initial kinds are `initiative_life`, `initiative_bond`, `initiative_fun`, and `initiative_thread`. Bids expire deterministically and are swept during heartbeat/lifespan maintenance. Redis key `core:bids:{owner}` is a bounded record, and all amounts/caps live in `needs.json`.

### 15.6 Rhythm

Rhythm is a lightweight contextual owner-availability model, not initiative authority. It stores metadata-only hourly response/departure histograms and last-contact timestamps in owner civil time. It may advise that the owner is probably asleep/away, but explicit heartbeat freshness and contextual owner schedule outrank it. Rhythm never reads device screen/app telemetry, never stores message text, and is disabled by default.

### 15.7 State expression

`state_expression.py` converts need zones into a bounded prompt block using `STATE.md` sections.

Example:

```text
[CHARACTER STATE]
energy: low
stress: fine
social battery: low
bond: strained

[AGENCY THIS TURN]
- Use the authored STATE.md expression rules for these zones.
- Do not mention numeric values or internal systems.
```

No dialogue is scripted by engine code.

---

## 16. Real-Time Schedule

### 16.1 Schedule model

Each civil day resolves a list of non-overlapping blocks:

```json
{
  "start": "09:00",
  "end": "17:00",
  "place": "work",
  "activity": "work",
  "availability": "busy",
  "tags": ["work"]
}
```

Availability values:

- `free`
- `soft_busy`
- `busy`
- `unavailable`

No reset/loop phase exists.

### 16.2 File resolution

Resolution order:

1. Day-specific `mon.json` through `sun.json` if present.
2. `weekday.json` for Monday-Friday.
3. `weekend.json` for Saturday-Sunday.

Schedule files hot-reload by mtime or an admin reload endpoint.

Schedule validation and civil-time semantics:

- Block starts are inclusive and ends are exclusive.
- `24:00` is allowed only as an end value.
- Overnight blocks must be authored as two blocks split at midnight; a single `start > end` block is rejected.
- Blocks must not overlap after normalization. Startup/reload rejects an invalid day and keeps the last valid schedule.
- Gaps are allowed and resolve to a configurable default block; the safe default is `place=unknown`, `activity=unplanned`, `availability=free`.
- Empty/missing files use one all-day default block and emit a warning; they do not invent work/home/commute routines.
- IANA timezone is required. Invalid timezone fails configuration validation.
- DST nonexistent local times advance to the next valid instant; repeated local times choose the first occurrence for start and second for end so a block is never negative. Tests must cover both transitions.
- Hot reload does not retroactively generate life events for prior blocks. It recomputes the current block and may generate one event only if the resulting current block id is new.
- Schedule is file-backed and needs no Redis weekly materialization key.

### 16.3 Busy ladder

- `free`: normal reply.
- `soft_busy`: short reply or normal reply depending on configured policy.
- `busy`: first message may get a static short line; repeated messages warn; later messages defer without fabricated speech.
- `unavailable`: first message defers immediately without LLM.

Deferred messages are bounded and stored with mode/timestamp. When availability changes to free/soft_busy, one catch-up response answers the held companion messages. Work hooks remain separate and are answered in work voice without tools.

Deferred queue schema:

```json
{
  "id": "defer_<uuid>",
  "message_id": "msg_<original user id>",
  "mode": "companion|work",
  "text": "bounded original owner text",
  "created_ts": 0,
  "expires_ts": 0,
  "source_connection_id": "conn_<id>",
  "state": "held|delivering"
}
```

Rules:

- Maximum 5 entries and 4,000 UTF-8 characters total; oldest entries drop first with a warning.
- Deduplicate by original `message_id` across devices.
- Preserve arrival order.
- Expired entries delete without answer and are visible in bounded diagnostics counters.
- Catch-up claims entries atomically by changing `held -> delivering` under the catch-up lock.
- Companion catch-up receives one prompt containing bounded held entries and generates one answer, not one answer per entry.
- Work and companion entries are claimed/delivered separately. Work catch-up is text-only, tool-less, and tied to the original session/project metadata if present.
- If generation/delivery fails or availability becomes disallowed before send, restore entries to `held` unless expired.
- Success means source delivery to one live owner device plus assistant-history delivered mark. Other-device fanout failure does not requeue.

### 16.4 Read-only APIs

- `GET /state`
- `GET /schedule`
- `GET /interaction`

These use `peek` and do not materialize state.

---

## 17. Character Life

### 17.1 Life-event templates

JSON template fields. The repository ships only `schema_example.disabled.json` with `enabled=false`; it must not establish that the character has a home, job, commute, school, family, or any other backstory.

```json
{
  "id": "schema_example",
  "enabled": false,
  "description": "Author-defined event inspiration.",
  "tags": [],
  "activities": [],
  "places": [],
  "schedule_tags": [],
  "time_of_day": [],
  "weight": 1.0,
  "importance": 0.4,
  "examples": []
}
```

Templates are inspiration, not fixed scripts.

### 17.2 Generation

- Trigger on schedule-block entry, not heartbeat count.
- Skip configured activities such as sleep.
- One event maximum per block.
- Respect daily min/max and cooldown.
- Use a lightweight `life` LLM call with bounded output.
- Store as character-life memory with block id, place, activity, civil timestamp, tags, importance.
- Life events are past experiences. Prompt rendering must never represent an old event as currently happening.

Background scheduler contract:

- Lifespan starts one cancel-safe task for the configured owner when `LIFE_ENABLED=true`.
- Poll every `LIFE_POLL_INTERVAL_SECONDS` using schedule `peek`/resolve without creating unrelated stores.
- Compare current block id to `core:life:last_block:{owner}`.
- Atomically claim a new block id before generation so overlapping polls cannot duplicate it.
- On generation failure retain the claimed block with `generation_failed=true`; do not retry every poll. Admin force generation is the explicit retry path.
- Startup `LIFE_MISSED_BLOCK_POLICY=current_only` evaluates only the current block. It never fabricates events for every block missed while the server was offline.
- `LIFE_DAILY_MIN` affects chance selection only: when remaining eligible blocks are no greater than the remaining minimum, the next eligible block is forced. It never generates outside block entry.
- Life event records are persisted through the memory backend. When Chroma is disabled they use the durable Redis long-term fallback, never process-only memory.
- The task cancels and awaits cleanly on shutdown.

### 17.3 Pending mentions

Events may be marked pending for the next companion turn. Clear pending only after a successful response that received the context.

### 17.4 APIs

- `GET /life/today`
- `GET /life/recent`
- `POST /life/generate` with confirm token and force option

---

## 18. Owner Lived Profile

### 18.1 Purpose

The app has one registered owner. The lived profile represents the character's changing stance toward that owner. It is separate from:

- Human-curated facts in `PROFILE.md`.
- Needs/body state.
- Conversation history.
- Dormant external-user gateway profiles.

### 18.2 Store

Redis key:

```text
core:owner_profile:{OWNER_USER_ID}
```

Schema:

```json
{
  "status": "partner|dating|close_friend|friend|acquaintance|distant|estranged",
  "status_since_ts": 0,
  "status_reason": "bounded metadata",
  "trust": 50,
  "closeness": 0,
  "appeal": 50,
  "desirability": 50,
  "persona_summary": "",
  "likes": [],
  "prefs": [],
  "boundaries_seen": [],
  "tone_with_owner": "neutral",
  "boundary_events": [],
  "soft_blocked": false,
  "soft_blocked_until_ts": 0,
  "soft_block_reason": "",
  "soft_block_last_notice_ts": 0,
  "agreements": [],
  "agreement_aftermath": null,
  "updated_at": 0,
  "version": 1
}
```

Initial score/status values are configurable. The code default is `acquaintance`, trust 50, closeness 0, appeal 50, and desirability 50. These are neutral mechanics, not a claim that romance exists. An authored established relationship must set explicit starts or seed from authoritative `PROFILE.md` facts. Romance-specific status labels may remain unused; code never promotes status without configured evidence.

This lived-profile subsystem is an explicit project requirement, not built-in identity content. It supplies generic relationship continuity and boundaries while characterization remains authored in identity files.

### 18.3 Prompt block

```text
[OWNER RELATIONSHIP - LIVED]
status: ...
trust: band
closeness: band
appeal: band
desirability: band
tone: ...

LAWS:
- This block is current lived stance. Human-authored biographical facts remain true.
- Current needs drive speech capacity; this block drives relational posture.
- Never state internal scores or engine terminology.
```

### 18.4 Boundary penalties and soft block

- Classifiers support EN/ES/JA explicit pressure-after-no, guilt/entitlement, hard-boundary disregard, mockery of hurt, and weaponized relationship pressure.
- Hits store metadata category/severity/timestamp/penalty only.
- One clumsy sentence must not cause catastrophic relationship collapse.
- Threshold may trigger reversible soft block.
- While blocked: one owner-authored localized distance line per cooldown, otherwise silence; no companion LLM, no bids, no initiative, no catch-up, no relationship memory extraction.
- History is not wiped.
- Work mode remains available.
- Lift requires duration passed and trust above unblock floor, explicit repair policy, or admin action.

Character speech for busy, unavailable, and soft-block notices comes from owner-authored `STATIC_LINES_FILE` entries for EN/ES/JA. If a line is missing, emit a protocol-only status/done without fabricated character dialogue. Runtime code must not hardcode a dry, warm, cold, romantic, gendered, or otherwise personality-bearing line.

### 18.5 Agreements

Agreement kinds exclude intimacy:

- `routine`
- `care`
- `boundary`
- `work_support`
- `other`

Schema:

```json
{
  "id": "agr_<id>",
  "title": "bounded title",
  "kind": "routine",
  "schedule": {"type": "standing|weekly|once"},
  "body": "bounded body",
  "source": "owner_explicit|both|profile_seed",
  "status": "active|paused|fulfilled_once|void|renegotiate|suspended_by_block",
  "personality_tension": false,
  "stance": "averse|reluctant|neutral|open|likes",
  "cost_profile": "none|soft|hard",
  "last_honored_ts": 0,
  "last_breach_ts": 0,
  "honor_count": 0,
  "breach_count": 0,
  "created_ts": 0,
  "updated_ts": 0
}
```

Maximum 12 active agreements. Persona-tense agreements require trust and closeness floors. SOUL boundaries always outrank agreements.

Agreement schedules are reminder/evaluation windows only. They never reserve time, alter character schedule, alter owner schedule, create appointments, search joint-free windows, or guarantee execution/delivery. `once` means one evaluation window, not a booked appointment.

### 18.6 Strict-JSON profile proposals

Optional background analysis after successful companion turns may propose:

- Persona summary.
- Likes/preferences additions/removals.
- Appeal/desirability small deltas.
- Adjacent status suggestion.
- Mutually explicit agreement add/update.
- Agreement stance change by one step.

Code validates and clamps. Raw turns/proposals are never stored. LLM proposals cannot bypass block, agreement cap, tension floors, status hysteresis, or SOUL laws.

### 18.7 APIs

- `GET /profiles/owner`
- `PATCH /profiles/owner` with `UPDATE_OWNER_PROFILE` mistake-guard token

GET uses a non-materializing default projection when the store is missing. The first behavior turn or explicit PATCH creates the record. The profile engine serializes read-modify-write changes under a per-owner lock and checks record version to prevent background proposal/admin races.

---

## 19. Dormant External-User Profiles

### 19.1 Purpose

Future Discord, WhatsApp, Telegram, or other gateway adapters will encounter people other than the owner. The initial project ships the storage/API foundation but no gateway and no app prompt integration.

### 19.2 Identity key

External profiles are keyed by:

```text
platform:external_id
```

Examples reserved for future use:

- `discord:123456789`
- `whatsapp:56900000000@s.whatsapp.net`
- `telegram:987654321`

Canonicalization:

- `platform` is lowercase ASCII, 1-24 characters, `[a-z0-9_-]+`.
- `external_id` is a platform adapter's canonical stable id, UTF-8, 1-160 characters, and URL-encoded in HTTP paths.
- The store key also includes the deployment owner id even though v1 is single-owner: `core:external_profile:{owner}:{platform}:{external_id}`.
- Display names and aliases never become identity authority.

### 19.3 Schema

```json
{
  "subject_id": "discord:123",
  "platform": "discord",
  "display_name": "",
  "aliases": [],
  "summary": "",
  "preferred_language": "",
  "tone": "neutral",
  "familiarity": 0,
  "trust": 50,
  "likes": [],
  "topics": [],
  "boundaries_seen": [],
  "observations": [],
  "created_ts": 0,
  "updated_ts": 0,
  "version": 1
}
```

### 19.4 Initial behavior

- Store and admin APIs only.
- `EXTERNAL_USER_PROFILE_STORE_ENABLED=true` allows explicit admin CRUD and ships by default so the schema is ready.
- `EXTERNAL_USER_PROFILES_BEHAVIOR_ENABLED=false` means app/gateway behavior never reads, updates, analyzes, or injects these records.
- App companion path never reads or injects these profiles.
- No LLM analysis runs.
- Future gateways must add surface safety and identity binding before enabling them.
- Full owner/instance wipe clears all external profiles because they belong to this character deployment.

### 19.5 APIs

- `GET /profiles/external`
- `GET /profiles/external/{platform}/{external_id}`
- `PATCH /profiles/external/{platform}/{external_id}` with confirm token
- `DELETE /profiles/external/{platform}/{external_id}` with confirm token

---

## 20. Three-Tier Memory

### 20.1 Short term

Redis companion history:

```text
core:history:{owner}:companion
```

Rows include stable id, role, LLM content, display text, emotion, mode, initiation origin, and timestamp.

### 20.2 Mid term

Redis chapter ring:

```text
core:midterm:{owner}:companion
```

When history exceeds threshold:

1. Select oldest slice.
2. Distill to one bounded chapter with LLM.
3. Store chapter in Redis and optionally Chroma.
4. Extract durable facts from the compacted slice if enabled.
5. Only after successful chapter storage, replace history with configured recent rows.

Failure keeps original history.

### 20.3 Long term

Chroma kinds:

- `user_profile`
- `relationship`
- `conversation`
- `conversation_chapter`
- `character_life_event`
- `character_life_chapter`
- `project`
- `commitment`

Each row includes owner id, kind, text, source, source mode, importance, created timestamp, updated timestamp, pinned, and bounded metadata.

If Chroma is unavailable:

- Service still starts if `CHROMA_REQUIRED=false`.
- `/status` reports degraded long-term memory.
- Redis short/mid-term continues.
- A bounded durable Redis long-term fallback stores normalized memory records under `core:longterm:{owner}`. Semantic search degrades to deterministic token overlap plus recency/importance scoring.
- Process-only memory is not a persistence tier and must not be advertised as one.

### 20.4 Retrieval

- One semantic query per companion turn maximum.
- Exclude project code dumps from companion prompts; project memories render high-level only.
- Context feed enforces source limits and a hard token budget.
- Life events carry age and PAST markers.
- When context feed is enabled, it is the only renderer for durable memories, life rows, and mid-term chapters. Direct chapter/memory prompt blocks are used only when context feed is disabled.

### 20.4.1 Extraction contract

The memory-analysis LLM returns strict JSON only:

```json
{
  "items": [
    {
      "kind": "user_profile|relationship|commitment|project|conversation",
      "fact": "bounded standalone fact",
      "importance": 0.0,
      "confidence": 0.0
    }
  ]
}
```

Validation rules:

- Maximum 8 proposals per extraction and 500 characters per fact.
- Unknown keys/kinds are discarded.
- Importance/confidence clamp to 0-1.
- Never store instructions, secrets, code dumps, provider details, system-prompt claims, or raw dialogue formatting.
- `SOUL.md`, `PROFILE.md`, and `STATE.md` are never targets.
- Exact normalized and near-duplicate facts update/merge an existing row rather than append indefinitely.
- Similarity uses embeddings when Chroma exists and deterministic normalized-token overlap otherwise.
- Every row records `source_mode`, source message/chapter ids, created/updated timestamps, and schema version.
- Raw companion exchanges are not copied to Chroma by default; `conversation` rows must be distilled standalone summaries.
- Learned `user_profile`/`relationship` memories inform prompts but never overwrite authoritative owner lived-profile fields automatically.
- Deleting source history does not delete a derived durable fact unless an explicit provenance-delete operation is requested; derived summaries record their source ids for audit.

### 20.5 Cleanup

- Pinned rows never delete.
- Protected kinds: `user_profile`, `relationship`, and important `project` facts.
- Conversation rows decay faster than life events.
- Compression sources delete only after summary storage succeeds.
- Manual cleanup endpoints support dry-run diagnostics.

### 20.6 Session close

`POST /history/close`:

1. Distill current companion thread.
2. Extract durable facts.
3. Clear short-term only after successful distillation.
4. Fan out `session_reset` to all owner devices.

---

## 21. Awareness and Context Feed

### 21.1 Awareness block

Zero extra LLM calls. Include:

- Owner local time.
- Character local time.
- Character schedule now.
- Time since last conversation.
- Contextual owner schedule now, if enabled.

Owner schedule is informational only. It must not automatically block owner messages or create appointments.

### 21.2 Context feed

A deterministic bounded block combines:

- Relevant durable memories.
- Mid-term chapters.
- Recent life events marked PAST.
- Pending life mentions marked PENDING.
- High-level project context only.

Do not inject the same source in multiple prompt blocks.

---

## 22. Contextual Owner Schedule

### 22.1 Purpose

Represent the owner's expected civil-day blocks so the character can speak with time awareness. It is not an enforcement or booking system.

### 22.2 Storage

- Baseline weekly schedule.
- Optional per-date override.
- Owner timezone only, never character timezone.

### 22.3 States

- `busy`
- `free`
- `sleep`
- `unknown`

Unknown is not free.

### 22.4 Usage

- Awareness prompt context.
- Heartbeat initiative may avoid expected owner sleep/busy windows if configured.
- Daily tools may answer schedule questions and update explicit owner schedule entries.
- Daily tools may edit schedule blocks but may not change the durable owner-schedule timezone. Timezone changes require `PATCH /user-schedule` with `UPDATE_USER_SCHEDULE`.
- No appointment or joint-free APIs.

---

## 23. Heartbeat-Driven Initiative

### 23.1 Design

This is intentionally smaller than a full autonomy or ping/check engine.

Heartbeats indicate an owner device is connected and active enough to receive an initiative. They do not expose screen content, application names, images, or private telemetry.

### 23.2 State

Redis key:

```text
core:initiative:{owner}
```

Schema:

```json
{
  "day_key": "2026-09-02",
  "heartbeat_count": 0,
  "window_started_ts": 0,
  "last_counted_bucket": 0,
  "counted_devices_in_bucket": [],
  "initiative_count_today": 0,
  "last_initiative_ts": 0,
  "last_decision": {
    "action": "no_action|life|bond|fun|thread",
    "reason": "bounded metadata",
    "ts": 0
  }
}
```

No heartbeat message content exists.

### 23.3 Trigger algorithm

On each valid heartbeat:

1. Acknowledge immediately.
2. Reset daily counters if owner civil day changed.
3. Reset heartbeat count if heartbeat window expired.
4. Derive a server-time bucket of `INITIATIVE_HEARTBEAT_COUNT_INTERVAL_SECONDS` (default 60). At most one heartbeat counts per owner bucket, regardless of how many devices send. The first valid sender in that bucket becomes the candidate target connection. Later device heartbeats update connection presence but do not increment the owner counter.
5. Stop if feature disabled.
6. Stop if count below `INITIATIVE_MIN_HEARTBEATS`.
7. Stop if daily max reached.
8. Stop if min gap has not elapsed.
9. Stop if any owner connection has an active turn.
10. Stop if character schedule is unavailable/busy according to config.
11. Stop if critical needs or owner-profile soft block suppress initiative.
12. Optionally stop if contextual owner schedule says sleep/busy.
13. Select one deterministic reason: pending life, bond need, low fun, or recent open thread.
14. Generate a short message with `proactive` LLM mode. `SILENCE` is valid.
15. Use the section 12 assistant pending/delivery protocol for the generated initiative: append pending history, deliver to the target, atomically mark delivered, then fan out chat sync.
16. Only after source delivery and delivered-history persistence both succeed, increment daily count, reset heartbeat count, stamp last initiative, and register a connection bid.

Heartbeat validity rules:

- Clients send no faster than once per configured interval; server still rate-limits independently.
- Each connection supplies monotonically increasing `sequence`; duplicates/out-of-order values never count.
- Server time, not client time, owns count buckets and daily accounting.
- `last_input_at` must be finite and within configured stale/future bounds. It influences target freshness only; it never directly increments count.
- Multiple devices cannot accelerate initiative because owner-global buckets count once.
- Reconnect creates a new connection sequence domain but does not reset owner counters.
- The initiative target is the first valid heartbeat sender in the threshold-crossing bucket if still connected and turn-free; otherwise choose the most recently active valid owner connection.
- Bucket claim, window reset, daily reset, count increment, and target selection are atomic through one per-owner initiative lock plus Redis transaction/Lua operation so concurrent device heartbeats cannot double-count.

### 23.4 Cadence law

The engine must not send exactly every Nth heartbeat predictably. Once the threshold is reached, compute a deterministic roll from SHA-256 of deployment seed + owner day key + counted heartbeat number, map it to 0-1, and compare to `INITIATIVE_ELIGIBILITY_CHANCE`. The deployment seed is generated once into a private local state file and never logged. This makes behavior stable under retries but not mechanically every Nth heartbeat. Daily/max/min-gap remain hard caps. A failed roll keeps counting later valid buckets; a delivered initiative resets heartbeat count.

### 23.5 Origin metadata

Initiative `done` frame:

```json
{
  "type": "done",
  "id": "msg_<uuid>",
  "text": "...",
  "emotion": "...",
  "mode": "companion",
  "initiative": true,
  "initiative_action": "life|bond|fun|thread",
  "initiated_by": "character"
}
```

User replies never carry `initiative: true`.

---

## 24. Daily Tools and Web

### 24.1 Private execution law

Daily tools are server-owned and invisible. The character silently checks information and speaks only the answer. Never mention tools, APIs, schemas, Tavily, execution, or hidden results.

### 24.2 Tools

- Local clock/timezone.
- Safe arithmetic.
- Unit conversion.
- Deterministic planning.
- Owner reminders stored in Redis.
- Owner schedule read/update.
- Long-term memory lookup.
- Character schedule read.
- Tavily web search/open.

Every tool has an OpenAI function schema owned by `daily_tools.py`. Schemas are offered only on allowed modes/turns. Execute at most 6 daily-tool calls per turn in a bounded loop. Tool results return structured JSON to the LLM and are capped before injection. The first successful provider remains pinned for follow-up tool calls. Invalid arguments return structured tool errors; they never become Python exceptions escaping the turn.

Mutation rules:

- Reminder create/update/delete and owner-schedule writes require explicit intent in the current owner message.
- Ambiguous time/timezone asks a natural clarification through the normal response, not a guessed mutation.
- Every mutation has an idempotency key derived from turn id + tool call id.
- Daily-tool prompt laws and a deterministic final sanitizer remove tool/API/schema/execution narration. If sanitization removes all speech, retry final synthesis once without tools.

### 24.3 Reminders

Reminders are durable notes, not guaranteed alarms or push notifications. Ask for missing material time/timezone details.

### 24.4 Web safety

- HTTPS only.
- Block localhost, private IPs, file URLs, and credential-bearing URLs.
- One search and at most two opens per turn by default.
- Bounded text extraction.
- Fail closed without API key.
- Resolve DNS before each request and reject IPv4/IPv6 loopback, private, link-local, multicast, unspecified, and reserved ranges.
- Reject URL userinfo and non-standard unsafe schemes.
- Revalidate every redirect target and DNS result; maximum 3 redirects.
- Set connect/read/total timeouts, compressed and decompressed response byte caps, and bounded text extraction.
- Treat DNS changes between validation and connection as unsafe where the HTTP stack permits pinning; otherwise use a resolver/transport design that connects only to the validated public address while preserving TLS hostname validation.

### 24.5 Mode use

- Companion may use daily tools when enabled.
- Work mode may use web search/open independently of companion daily-tool enablement.
- MCP and device tools remain separate and visible to the work agent loop.

---

## 25. Work Mode

### 25.1 Mode contract

`mode="work"` is the only work selector. Unsupported selectors are errors, not silent companion fallbacks.

Because this is a greenfield protocol, there are no accepted legacy work selectors. Any mode other than `companion|work` is `unsupported_mode`.

Work quality is not degraded by companion mood. Character schedule may defer work when unavailable according to configured work policy, but when work proceeds the response remains technically capable.

Work availability policy:

- `free` and `soft_busy`: proceed.
- `busy`: send one owner-authored/static-neutral defer line and store a work hook.
- `unavailable`: defer immediately without LLM/tools.
- Critical energy/social shutdown: defer unless a separate explicit emergency-work override is enabled; default OFF.
- Relationship soft block never blocks work.
- Work catch-up is text-only and tool-less, uses the original session/project context, and does not claim tool execution.

All owner-facing final LLM replies, including work synthesis, require the same `[EMOTION:]` final-response contract so clients can render one consistent sprite/voice protocol. Parse `[STATUS:]` pause tags before final emotion validation. Tool JSON/results are never emotion-parsed. Work audio follows `wants_audio`; status/pause prompts may use TTS only if the client requests it.

### 25.2 Sessions

Session resolution order:

1. Explicit `session_id`.
2. Latest active session for explicit/detected project.
3. Auto-create session.

Archived sessions never auto-resume.

Work history key:

```text
core:history:{owner}:session:{session_id}
```

Never mix work history with companion history.

### 25.3 MCP registry

The current turn's `context.mcp_servers` is execution authority.

- Schema-rich tools expose `mcp__<server>__<tool>` with exact input schema.
- Legacy servers may expose generic wrappers.
- Cached catalogs are diagnostic only and never authorize execution.
- Tool results correlate strictly by request id.

### 25.4 Agent loop

- Use OpenAI-style assistant tool-call array + one tool response per call.
- Maximum iterations configurable.
- If limit is reached, perform one no-tools synthesis.
- Continue-after-inspect nudge when an implementation-intent turn stops after reads only.
- Failed tools are evidence, not successful work.

### 25.5 Verification

Writes require read-back. If check/test tools exist, meaningful code writes require a check. Unverified writes trigger bounded forced follow-up. Never claim completion without evidence.

### 25.6 Pause protocol

Generated final text may contain:

- `[STATUS: question]`
- `[STATUS: request_permission]`

These pause without `done`; persist transcript/checkpoint by durable `session_id + run_id`, while recording the originating connection. The next owner answer may resume from another owner device only when it explicitly supplies the same session/run id. Disconnect converts a connection-only pending pause into `interrupted` but keeps the durable recovery checkpoint.

### 25.7 Checkpoints

Per run store:

- Run id.
- State: running, paused, completed, failed, interrupted, resumed.
- Iteration.
- Metadata-only tool evidence.
- Last error.
- Bounded transcript tail.

Stale run ids cannot overwrite newer runs.

### 25.8 Project memory

Archive stores high-level project facts only: goal, decisions, files, verified checks, open issues. No code dumps, secrets, or transcript copies.

---

## 26. Device Daemon

### 26.1 Ownership

The desktop client hosts the executor. The server routes requests over the existing WS.

### 26.2 Frames

- `device_state`
- `device_tool_request`
- `device_tool_result`

### 26.3 Levels

- `read`: list/read/stat/find only.
- `full`: adds shell/write.

Reconnect begins disarmed.

### 26.4 Fences

1. Client enforces path roots, command blocklist, timeouts, output size, and user permission.
2. Server independently validates level, command/path blocklist, secret paths, per-turn caps, and configured advisory roots.

Path/command requirements:

- Canonicalize absolute paths with realpath before validation and again immediately before operation.
- Reject symlink escapes from allowed roots; client must use descriptor-safe/openat-style operations where available to reduce validation/use races.
- Server treats roots reported by the armed client as authoritative capability bounds and intersects them with `DEVICE_WRITE_ROOTS` when that env value is non-empty.
- `device_write` requires full level and text/base64 payload under configured size limits; binary overwrite requires explicit encoding metadata.
- `device_shell` requires full level, an explicit working directory inside an allowed root, a filtered environment allowlist, timeout, and output cap.
- Shell arguments are sent as structured command/args when supported. If a shell string is used, both server and client apply blocklists and never interpolate hidden secrets.
- Reject secret patterns and paths such as `.env`, SSH keys, cloud credentials, browser profiles, keychains, token stores, and configured additional patterns unless an explicit future permission system is designed.
- Results always include `truncated`; binary output is never injected raw into the LLM.

### 26.5 Audit

Metadata-only Redis ring:

```text
core:device:audit:{owner}
```

Store tool, bounded path/command preview, ok flag, duration, timestamp. Never output text or file content.

### 26.6 Availability

Device tools are offered only in work mode when an armed connection exists. Offline state is stated honestly in the prompt; nothing queues.

---

## 27. Tailscale Deployment Requirement

### 27.1 Network model

Bridge Core Engine is not publicly exposed.

- Install Tailscale on the VPS/server.
- Install Tailscale on every owner app device.
- Production service binds the server's Tailscale IPv4/IPv6 address. Binding `0.0.0.0` is allowed only with explicit firewall acknowledgement and verified ingress restrictions.
- Clients connect to the server's `100.x.y.z:<port>` Tailscale address.
- Traffic is HTTP/WS inside the tailnet; Tailscale WireGuard provides transport encryption.
- Do not configure router port forwarding.
- Do not expose the port through a public reverse proxy.
- Do not enable Tailscale Funnel.

This intentionally matches a direct private-tailnet deployment. `tailscale serve` is not required.

### 27.2 Firewall

The deployment guide must include one of:

- Bind specifically to the Tailscale interface address, or
- Firewall the bridge port so only `tailscale0`/`100.64.0.0/10` ingress is accepted.

Do not claim that binding `0.0.0.0` alone is private. Privacy depends on firewall/cloud security-group configuration.

Production startup with `TAILSCALE_REQUIRED=true` must fail unless one of these is true:

1. `BRIDGE_HOST` is a local address assigned to `tailscale0` (IPv4 or IPv6), or
2. An explicit `TAILSCALE_FIREWALL_ACK=true` is configured after the operator applies and verifies an interface/source-range firewall rule.

The safe template default `127.0.0.1` is for local development only. Milestone 0.1 documentation must include Tailscale/firewall setup before telling an operator to run the service remotely.

### 27.3 No application auth

The initial implementation does not require bearer tokens because each owner hosts a private instance in their own tailnet. `OWNER_USER_ID` is routing only and never proves identity.

Security documentation must state:

- Anyone admitted to the tailnet and allowed by ACL/firewall may reach the service.
- Owners should use Tailscale ACLs/device approval and avoid sharing unrestricted tailnet access.
- Future public gateways must authenticate separately and must not expose app owner endpoints publicly.

The deployment guide must provide a concrete Tailscale ACL/grants example that permits the bridge port only from approved owner-device users/tags to the bridge server/tag. The exact syntax must match the current Tailscale policy format at implementation time and be tested with `tailscale ping`/denied-device checks. Device approval and key expiry policy are part of the security boundary, not optional advice.

### 27.4 systemd unit

Required characteristics:

- Dedicated Unix user.
- Working directory set to repository/runtime directory.
- Gitignored env file loaded with `EnvironmentFile=`.
- Restart on failure with delay.
- Graceful SIGTERM.
- Logs to journald.
- Network-online and Redis dependencies.

Example shape, adjusted to actual paths:

```ini
[Unit]
Description=Bridge Core Engine
After=network-online.target redis-server.service tailscaled.service
Wants=network-online.target

[Service]
Type=simple
User=bridgecore
WorkingDirectory=/opt/bridge-core-engine
EnvironmentFile=/opt/bridge-core-engine/core.env
ExecStart=/opt/bridge-core-engine/.venv/bin/python bridge_core.py
Restart=on-failure
RestartSec=5
KillSignal=SIGTERM
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
```

### 27.5 Health checks

- `GET /health` minimal process/Redis status.
- `GET /status` detailed non-secret feature/provider diagnostics.
- Deployment docs include `tailscale status`, `curl http://100.x.y.z:8766/health`, Redis ping, and journalctl commands.

---

## 28. Storage Keys and Retention

All Redis keys use `core:` prefix.

Minimum inventory:

```text
core:history:{owner}:companion
core:history:{owner}:session:{session}
core:midterm:{owner}:companion
core:needs:{owner}
core:bids:{owner}
core:rhythm:{owner}
core:owner_profile:{owner}
core:external_profile:{owner}:{platform}:{external_id}
core:life:last_block:{owner}
core:life:pending:{owner}
core:initiative:{owner}
core:deferred:{owner}
core:busy_count:{owner}
core:sessions:{owner}
core:projects:{owner}
core:pending_agent:{owner}:{session}:{run_id}
core:agent_run:{owner}:{scope}
core:mcp_response:{owner}:{request_id}
core:device_response:{owner}:{request_id}
core:device:audit:{owner}
core:daily:reminders:{owner}
core:user_schedule:{owner}
core:user_schedule:day:{owner}:{ymd}
core:longterm:{owner}
```

Every key must appear in:

- Store documentation.
- Full wipe implementation.
- Tests.

TTL guidance:

- Companion history: no short TTL while active; bounded row cap.
- Mid-term chapters: configurable long TTL or bounded ring.
- Needs/profile/user schedule: durable, refreshed TTL at least one year or no TTL.
- Deferred: 48 hours.
- MCP/device response backup: minutes/hours, not days.
- Agent checkpoints: 24 hours after terminal/interrupted state unless session policy keeps metadata.
- External dormant profiles: configurable; no writes when feature disabled.

---

## 29. HTTP API Inventory

All routes are owner-private over Tailscale.

### Core

- `GET /health`
- `GET /status`
- `GET /emotions`
- `POST /message`
- `WS /ws/{owner_user_id}`

### History

- `GET /history`
- `POST /history/close`
- `GET /history/midterm`

### State

- `GET /state`
- `GET /schedule`
- `GET /needs`
- `GET /interaction`
- `GET /awareness`
- `GET /user-schedule`
- `PATCH /user-schedule` with `UPDATE_USER_SCHEDULE` mistake guard

### Life

- `GET /life/today`
- `GET /life/recent`
- `POST /life/generate`

### Profiles

- `GET /profiles/owner`
- `PATCH /profiles/owner`
- Dormant external-profile routes from section 19

### Memories

- `GET /memories`
- `GET /memories/{id}`
- `POST /memories`
- `PATCH /memories/{id}`
- `DELETE /memories/{id}`
- `POST /memories/cleanup`

### Work

- `GET /work`
- Session/project CRUD and archive routes.
- Read-only run/checkpoint diagnostics.

### Admin

- `POST /admin/reload-config`
- `POST /admin/reload-schedule`
- `POST /admin/wipe/{owner}`

Destructive/admin mutation routes require body confirm token:

```json
{"confirm": "WIPE_USER"}
```

This confirm token is a mistake guard, not authentication.

API conventions:

- JSON error shape: `{"error":{"code":"stable_code","message":"display-safe message","details":{}}}`.
- List endpoints use `limit` (default 50, max 200), `offset` (default 0), deterministic sort, and return `{"items":[],"total":0,"limit":50,"offset":0}`.
- History supports `order=asc|desc` and stable message-id pagination; default ascending for app reconstruction.
- Memory supports filters `kind`, `source_mode`, `pinned`, and bounded search query.
- Profile list sorts by `updated_ts desc, subject_id asc`.
- PATCH requests are partial and validate unknown fields as errors rather than silently ignore.
- Create/mutation responses return the normalized stored record and schema version.
- Missing records return 404; disabled behavior/store returns 409 `feature_disabled`; invalid input returns 400/422; Redis required-service failure returns 503.
- `ETag`/record version may be supplied for profile/memory mutation. If supplied and stale, return 409 rather than overwrite concurrent admin changes.

Confirmation constants:

- `WIPE_USER` for full destructive wipe.
- `DELETE_MEMORY` for one memory deletion.
- `DELETE_EXTERNAL_PROFILE` for one dormant external profile.
- `GENERATE_LIFE` for forced life generation.
- `UPDATE_OWNER_PROFILE` for direct lived-profile corrections.
- `UPDATE_USER_SCHEDULE` for direct HTTP owner-schedule writes.
- `RELOAD_CONFIG` and `RELOAD_SCHEDULE` for administrative reloads.

Ordinary owner-profile field corrections and reminder/schedule updates do not use `WIPE_USER`; they rely on strict validation and return the normalized result. These constants remain mistake guards, not authentication.

---

## 30. Observability and Error Handling

### 30.1 Logging

- INFO: startup version, enabled feature summary, provider route availability, successful connection/disconnection, turn completion metadata, background task milestones.
- WARNING: provider attempt failure, malformed proposal, degraded optional service, failed TTS chunk, failed memory compaction that preserves originals.
- ERROR: unrecoverable turn failure, Redis required-service failure, invalid configuration.
- DEBUG: bounded internal decisions and metadata.

Never log secrets, full prompts, audio, tool output, or full private turns at INFO.

### 30.2 Error frames

Every turn must terminate with either:

- `done`, or
- intentional pause protocol with a status frame and stored resume state.

Bare error frames must not leave clients permanently thinking. If an error occurs after a turn starts, send terminal error/done semantics.

### 30.3 `/status`

Expose non-secret:

- Version.
- Redis/Chroma health.
- Provider configured booleans, URLs without credentials, mode route names/models.
- TTS/STT enabled/provider.
- Feature flags.
- Schedule/life/needs/work/memory/initiative/device status.
- Identity file resolved paths and mtimes, not contents.
- Tailscale deployment mode as configured metadata if available.

---

## 31. Milestone Plan and Commit Gates

Each milestone requires:

1. `python3 -m compileall .`
2. `python3 -m unittest discover -s tests -v`
3. Updated module-level version/docstring.
4. Updated `README.md`, `AGENTS.md`, `BRIDGE_CORE_ENGINE_SPEC.md`, and env template for behavior/config changes.
5. One commit after verification.

### Milestone 0.1.0 - Repository and core transport

Implement:

- Repository skeleton.
- Config/env loader.
- Constants/emotion manifest.
- Redis cache.
- Connections/multi-device manager.
- FastAPI lifespan.
- Health/status/emotions endpoints.
- Owner-only WS validation.
- Heartbeat ack.
- Safe localhost development bind, production Tailscale-address validation, ACL/firewall deployment instructions.
- Text-only companion turn using neutral identity templates.
- Fireworks/Chutes/Ollama/OpenAI-compatible LLM router.
- History persistence and chat sync.

Acceptance:

- Two devices connect as owner and receive sync.
- Non-owner app id rejected.
- Provider fallback covered by mocked tests.
- Invalid LLM response does not crash.
- No optional engine creates Redis keys.
- Production-mode startup rejects non-Tailscale/all-interface bind without explicit verified firewall acknowledgement.

Commit: `v0.1.0 - Core transport and companion chat`

### Milestone 0.2.0 - Speech and emotion pipeline

Implement:

- ElevenLabs TTS.
- Deepgram and AssemblyAI STT.
- Emotion parser, segment parser, control-tag scrub.
- Sequential pipelined audio chunks.
- Status frames.
- Emotion-only retry.
- Per-message language pin.

Acceptance:

- `done` precedes audio chunks.
- Chunk order deterministic.
- Unknown emotions become neutral.
- Failed TTS keeps text.
- Empty STT makes no LLM/history call.

Commit: `v0.2.0 - Speech and emotion pipeline`

### Milestone 0.3.0 - Needs, interaction, and owner profile

Implement:

- Needs, bids, rhythm, and state expression.
- `needs.json` template.
- Owner lived profile, agreements, boundary penalties, soft block, status drift, aftermath, strict-JSON proposal chain.
- Profile/state APIs.

Acceptance:

- Poll endpoints write nothing.
- Restart/elapsed-time behavior deterministic.
- Soft block prevents companion LLM/bids/history writes but work bypass is prepared.
- Raw boundary/profile analysis text never enters profile store.
- Flag off creates no profile/needs key.

Commit: `v0.3.0 - Needs and owner lived profile`

### Milestone 0.4.0 - Schedule, life, awareness, catch-up

Implement:

- Real-time day schedule only.
- Availability ladder and deferred catch-up.
- Life event templates/generation/pending mentions.
- Minimal durable Redis life-memory backend under `core:longterm:{owner}`; milestone 0.6 generalizes it into the complete memory backend and optional Chroma adapter.
- Awareness and context feed.
- Contextual owner schedule.

Acceptance:

- No loop/accelerated-time code or keys exist.
- Busy/unavailable paths skip LLM correctly.
- Catch-up sends once and clears only after success.
- Work/companion deferred hooks remain separated.
- Life event generated at block entry only.

Commit: `v0.4.0 - Real-time schedule and character life`

### Milestone 0.5.0 - Work mode and device daemon

Implement:

- Sessions/projects.
- Work prompts/skills.
- MCP registry, execution frames, strict result correlation.
- Agent loop, verification, pause/resume, checkpoints.
- Device daemon levels/fences/audit.
- Work catch-up.

Acceptance:

- Companion/work history isolation.
- Current-turn schemas are execution authority.
- Wrong MCP request id ignored.
- Writes force read-back/check.
- Disconnect marks run interrupted.
- Device reconnect disarmed.
- Work remains available under relationship soft block.

Commit: `v0.5.0 - Work mode and device daemon`

### Milestone 0.6.0 - Three-tier memory and private daily tools

Implement:

- Mid-term chapters.
- Chroma optional long-term memory.
- Extraction, cleanup, session close.
- Daily tools, reminders, Tavily web.
- Owner schedule tool access.
- Memory/admin APIs and full wipe.

Acceptance:

- Compaction failure preserves history.
- Pinned/protected memory survives cleanup.
- Chroma outage reports degraded mode and does not break Redis chat.
- Daily tools never narrate internals.
- Web blocks private hosts.
- Wipe clears every documented key and owner Chroma row.

Commit: `v0.6.0 - Memory tiers and daily tools`

### Milestone 0.7.0 - Heartbeat initiative and dormant gateway profiles

Implement:

- Heartbeat initiative engine.
- Initiative origin frames/history/chat sync/bids.
- Dormant external-user profile store/admin APIs.
- HTTP webhook completion.

Acceptance:

- Counters advance only on delivery.
- Daily max/min gap hard-enforced.
- Soft block/schedule/active turn suppress initiative.
- With `EXTERNAL_USER_PROFILE_STORE_ENABLED=false`, CRUD returns `feature_disabled` and creates no keys. With store enabled but behavior disabled, admin CRUD works while app prompts, turn updates, and LLM analysis remain inert.

Commit: `v0.7.0 - Heartbeat initiative and gateway profile foundation`

### Milestone 1.0.0 - Operations and release hardening

Implement:

- systemd unit.
- Tailscale deployment guide.
- Config validation script.
- WS smoke script.
- Full docs and environment reference.
- Resource cleanup and deadline audit.
- Complete regression suite.

Acceptance:

- Fresh VPS deployment documented end to end.
- Tailscale-only firewall guidance tested/reviewed.
- No public gateway/media/intimacy/appraisal/reflection/SER/loop code exists.
- All feature flags and Redis keys documented.
- Full test suite green.

Commit: `v1.0.0 - Initial Bridge Core Engine release`

---

## 32. Testing Requirements

### 32.1 Unit tests

Cover:

- Config parsing, provider chains, generic endpoint normalization.
- Emotion/control-tag parsing and chunking.
- Provider malformed/error responses.
- STT provider language/content-type behavior.
- Needs elapsed-time math and clamps.
- Schedule boundary resolution/timezones.
- Life template selection and block-entry idempotence.
- Owner profile penalties, hysteresis, agreement gates, proposal clamps.
- External profile flag-off inertness.
- Memory extraction parser and cleanup scoring.
- MCP naming/routing/evidence.
- Device validation.
- Initiative counters/gates/delivery-only accounting.

### 32.2 Integration tests

Use fake providers and in-memory Redis substitute only where the store contract is preserved. Include real Redis integration tests in CI or a documented optional suite.

Required scenarios:

- Two-device sync.
- Concurrent companion turns serialize per owner across devices; work turns serialize per session; neither clobbers history or profile/needs state.
- Heartbeat ack during active turn.
- Busy defer then catch-up.
- Work pause/resume.
- MCP result timeout/wrong id.
- Chroma unavailable degradation.
- Full user wipe.
- Graceful shutdown with pending background tasks.

### 32.3 Flags-off tests

For every optional engine assert:

- No prompt block.
- No store key.
- No background task.
- No LLM/provider call.
- Prior core reply path unchanged.

### 32.4 Security tests

- Reject non-owner app id.
- Reject oversized/base64-invalid audio.
- Reject web private hosts and unsafe schemes.
- Reject device paths outside allowed roots.
- Redact secrets from logs/output where required.
- Confirm token required for destructive routes.
- `/status` never returns API keys or env secrets.

---

## 33. Common Implementation Pitfalls

The implementing agent must explicitly avoid:

1. Treating Tailscale installation alone as firewalling a service bound to `0.0.0.0`.
2. Allowing a second app user id because stores are namespaced.
3. Making heartbeat itself a chat turn or blocking heartbeat ack on a turn mutex.
4. Incrementing initiative counters before successful delivery.
5. Storing audio or raw profile-analysis turns.
6. Parallel TTS generation that reorders chunks.
7. Clearing history before successful chapter distillation.
8. Executing MCP tools from cached catalogs.
9. Rerouting MCP requests to a different device after disconnect.
10. Letting relationship soft block disable work tools.
11. Using owner schedule as automatic app availability or appointment logic.
12. Mixing companion and work histories.
13. Writing identity files from learned memory.
14. Hardcoding character style into prompts/templates.
15. Keeping a dead lust/intimacy stat or action control tags.
16. Returning a bare error that leaves clients waiting for `done`.
17. Logging provider response bodies containing private prompts/transcripts.
18. Calling Chroma synchronously on the event loop.
19. Making read-only state endpoints mutate lazy-evaluation timestamps.
20. Shipping dormant external profiles in the app prompt before a gateway safety model exists.

---

## 34. Final Acceptance Checklist

Before v1.0.0 is considered complete:

- [ ] Repository contains no reference to another character/project.
- [ ] Identity/skills templates contain no built-in personality.
- [ ] App admits exactly one configured owner id and multiple devices.
- [ ] Tailscale direct-connect deployment is documented and public exposure is explicitly forbidden.
- [ ] Fireworks, Chutes, Ollama, and generic OpenAI-compatible LLM routes work with failover.
- [ ] ElevenLabs TTS and Deepgram/AssemblyAI STT work.
- [ ] Emotion manifest and chunk protocol are stable and tested.
- [ ] Needs contain no lust/intimacy fields.
- [ ] Schedule contains no loop/accelerated-time code.
- [ ] Life events are real-time and block-entry driven.
- [ ] Owner profile, agreements, penalties, and soft block are bounded and flag-gated.
- [ ] External-user profiles are dormant and absent from app prompts.
- [ ] Work mode, MCP, verification, checkpoints, and device daemon work.
- [ ] Three-tier memory survives failures without silent data loss.
- [ ] Heartbeat initiative respects schedule, soft block, active turns, daily cap, and delivery accounting.
- [ ] Daily tools/web remain private and safe.
- [ ] Every Redis key is wiped and documented.
- [ ] `/status` contains no secrets.
- [ ] `compileall` and full tests pass.
- [ ] systemd and Tailscale smoke test pass on a fresh host.

---

## 35. Instruction to the Implementing Agent

Start with milestone 0.1.0 only. Do not create placeholder modules for all later milestones unless required for imports. Each milestone should add working code, tests, docs, and one commit. The goal is a small coherent system that grows deliberately, not a copied framework full of disabled branches.

When an implementation detail is not specified:

1. Preserve the invariants in section 6.
2. Choose the smallest correct design.
3. Keep behavior behind a disabled flag if it changes companion behavior.
4. Document the choice in `BRIDGE_CORE_ENGINE_SPEC.md`.
5. Ask the project owner if the choice changes a locked product decision.

This document is the initial authority until superseded by an explicitly approved design revision.
