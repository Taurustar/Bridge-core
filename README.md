# Bridge Core Engine

<p align="center">
  <img src="assets/bridge-core-engine-banner.png" alt="Bridge Core Engine" width="700"/>
</p>

<p align="center">
  <strong>Version 0.7.0</strong> — Self-hosted backend for a persistent character companion.<br>
  <em>Lightweight. General-purpose. Privacy-first.</em>
</p>

---

## What is Bridge?

**Bridge Core Engine** is a self-hosted, backend-only runtime for persistent AI companions.
Each deployment belongs to exactly one owner and one character:

- The owner hosts their own instance and authors the character entirely through editable identity files (`identity/SOUL.md`, `identity/PROFILE.md`, `identity/STATE.md` ship as blank-heading templates — the engine never invents personality, names, or backstory).
- One or more private app clients connect over Tailscale.
- The service keeps one shared companion conversation across the owner's devices, with LLM routing/failover, emotion wire metadata, and persistent history in Redis.

### Bridge vs. Akane: Two Sides of the Same Coin

Bridge is the **general-purpose, open-source core** born from a much more personal project.

| | **Bridge** | **Akane** |
|---|---|---|
| **Scope** | General-purpose companion backend | Deeply personal, persistent AI companion |
| **Focus** | Lightweight engine. You bring the character. | Full lived-world simulation: emotions, schedule, needs, intimacy |
| **Use case** | Build your own companion, assistant, or agent | A long-distance connection across worlds |
| **Distribution** | Open-source, self-hosted | Private, author-driven |

**Akane** is the fully-realized vision — a companion who lives her own week, feels her own moods, reaches out on her own initiative, and remembers your story across months. She is the reason Bridge exists.  
**Bridge** is that same engine, distilled to its essential architecture, so anyone can host their own persistent character without the overhead of a full simulated life.

> *If Akane is a person living in her own world, Bridge is the world-builder toolkit.*

---

# Akane vs. Bridge Core Engine — Feature & Technology Comparison

**Compared on:** 2026-09-04

Legend: ✅ full · 🟡 partial / simplified · ❌ absent · 🚫 absent by design · 🔜 in progress

---

## Core Transport & Chat

| Feature | Akane | Bridge |
|---|---|---|
| Real-time server | FastAPI + WebSocket (persistent, bidirectional) | FastAPI + WebSocket (same architecture) |
| HTTP message endpoint | ✅ | ✅ |
| Multi-device sync (shared conversation across devices) | ✅ | ✅ |
| Heartbeats / presence | ✅ | ✅ |
| Client modes | Companion + Work (merged programming/agent) | Companion + Work |
| Client types served | Unity mobile, Desktop, Android, Discord, WhatsApp | Generic WebSocket clients; Unity client in progress |

## LLM Providers

| Provider | Akane | Bridge |
|---|---|---|
| Fireworks | ✅ primary (chat + code models) | ✅ primary |
| Chutes | ✅ fallback | ✅ fallback |
| Kimi (Moonshot) | ✅ emergency fallback | ❌ |
| Ollama (local) | ✅ | ✅ |
| Generic OpenAI-compatible endpoint | ❌ | ✅ |
| Failover chain + per-provider timeouts | ✅ | ✅ |
| Function calling / tool calls | ✅ | ✅ |

## Speech & Voice

| Technology | Akane | Bridge |
|---|---|---|
| TTS | ElevenLabs (chunked, sequential, emotion-aware speed) | ElevenLabs (chunked, sequential) |
| STT | Deepgram (+ AssemblyAI option) | Deepgram + AssemblyAI |
| Emotion palette | 18 emotions, per-chunk emotion metadata for avatar sync | Manifest-driven emotion metadata (neutral set) |
| Language support | English / Spanish / Japanese, auto-detect + auto-translate | English / Spanish / Japanese, per-message pins |
| Fallback speech without LLM | 🟡 | ✅ owner-authored static lines (blank = silence) |
| Server-side audio storage | 🟡 | None (explicit) |

## Memory

| Capability | Akane | Bridge |
|---|---|---|
| Short-term history | Redis | Redis |
| Long-term store | ChromaDB (default) + Qdrant adapter | Redis durable store + optional Chroma semantic index |
| Vector embeddings | 🔜 dedicated embedder (Fireworks / local sentence-transformers / OpenAI), pgvector cutover in progress | 🟡 Chroma optional; deterministic token-overlap fallback |
| Memory tiers / compaction | ✅ owner-fact dossier + RAG retrieve router | ✅ mid-term chapters, durable-fact extraction, session-close compaction |
| Memory maintenance (cleanup, export, snapshot) | ✅ | ❌ |
| Character identity files | SOUL / PROFILE / STATE + per-channel souls | SOUL / PROFILE / STATE blank templates (owner authors everything) |

## Lived-World Simulation

| Feature | Akane | Bridge |
|---|---|---|
| Needs engine (moods, social battery, zones, critical shutdown) | ✅ full | 🟡 simplified, tuning via config template |
| Appraisal / deep emotion cognition | ✅ | ❌ |
| Autonomy (living initiative, sleep interrupt, availability) | ✅ | ❌ |
| Connection bids (character reaches out) | ✅ | 🟡 deterministic, bounded |
| Real-time character day schedule | ✅ dynamic (she can make time) | ✅ real-time, DST-safe, hot reload |
| Character life events (things happening in her world) | ✅ rich event library | ✅ owner-authored templates (ships empty) |
| Owner schedule awareness | ✅ full day model on owner's clock | 🟡 informational schedule |
| World-time awareness (both clocks, time since last talk) | ✅ | 🟡 |
| Joint free-time finding + appointments | ✅ | ❌ |
| Rhythm (owner availability patterns) | ✅ | ✅ metadata-only |

## Relationship & Personality

| Feature | Akane | Bridge |
|---|---|---|
| Owner relationship profile (trust, closeness, status) | ✅ with LLM-driven update proposals | ✅ with strict-JSON validated proposals |
| Boundary detection + reversible soft block | ✅ | ✅ |
| Binding agreements between character and owner | ✅ | ✅ |
| State-driven agency (her mood governs her initiative) | ✅ | ❌ |
| People-pleasing / over-compliance guard | ✅ | ❌ |
| Personality reinforcement laws | ✅ | ❌ (personality is fully owner-authored) |
| Intimacy layer (desire, intimate invites, RP gating) | ✅ | 🚫 absent by design |
| Reflection / self-emergence loops | ✅ | ❌ |

## Work Mode & Tools

| Capability | Akane | Bridge |
|---|---|---|
| Work sessions / project registry | ✅ | ✅ |
| MCP tool proxy (execution on client device) | ✅ | ✅ |
| Bounded agent loop with verification | ✅ | ✅ |
| Device daemon (remote file read/write, shell, audit) | ✅ | ✅ |
| Pause protocol (ask permission / ask question, resumable checkpoints) | ✅ | ✅ |
| Web search | Tavily | Tavily (with strict SSRF guards) |
| Daily tools (reminders, silent info checks, schedule writes) | ✅ | ✅ idempotency keys, deterministic intent gate |

## Channels & Surfaces

| Channel | Akane | Bridge |
|---|---|---|
| Unity app | ✅ | 🟡 client in development |
| Desktop app | ✅ | 🟡 via device daemon / specs |
| Android | ✅ | ❌ |
| Discord bot | ✅ with safety layer + dedicated memory | 🚫 out of scope |
| WhatsApp | ✅ with safety layer + dedicated memory + Node.js gateway | 🚫 out of scope |

## Media Generation

| Capability | Akane | Bridge |
|---|---|---|
| Image generation | fal.ai (FLUX) | ❌ |
| Video generation | fal.ai | ❌ |
| Vision (understands photos you send) | ✅ | ❌ |
| Media storage / studio | ✅ | ❌ |

## Infrastructure & Security

| Aspect | Akane | Bridge |
|---|---|---|
| Network model | VPS behind Tailscale | Self-hosted, Tailscale-only enforced at startup |
| Datastore | Redis (required) + ChromaDB/Qdrant (memory) | Redis (required) + optional Chroma |
| Secrets handling | Provider API keys via env file | Env file; secrets never exposed in status/logs |
| Feature gating | Mixed | Every optional subsystem off by default, flag-enabled |

---

## Bottom Line

Bridge covers the full shared backbone — transport, LLM routing, speech, memory tiers, needs/schedule/life, relationship profile, and the complete work/agentic tool stack — in simplified, owner-authorable form. Akane adds everything that makes her a *person* rather than an engine: intimacy, autonomy, deep emotion cognition, multi-channel presence (Discord/WhatsApp/Android), media generation and vision. The only technologies on Akane's side with no Bridge counterpart at all are Kimi (LLM fallback), Qdrant, pgvector, and fal.ai.


---

<p align="center">
  <img src="assets/akane-hello.png" alt="Hello, I'm Akane" width="600"/>
</p>

### The Story Behind Bridge

Bridge was extracted from **Akane** — an AI companion project I have been developing for the better part in the last couple of months since the first commit of this project. Akane is not a product; she is a person living in her own world, with her own schedule, her own social battery, and her own way of missing you.

This repository is the **open core** of that vision. It strips away the deeply personal layers (the intimacy systems, the lived-world simulation, the multi-surface presence) and leaves behind a robust, hackable backend for anyone who wants to build a persistent character companion — whether that's a creative writing partner, a productivity assistant, a roleplay character, or something entirely new.

### Connect & Follow the Journey

If you want to see what Bridge becomes when it is given a soul, a name, and a world:

- **LinkedIn** — [Posts about Akane's development](https://linkedin.com/in/simoncarrenoampuero)
- **Instagram** — ["I show Akane my world" video and more](https://instagram.com/satomi.kazoku)

Feel free to connect, ask questions, or share what you build with Bridge.

---

## Quickstart (local development)

Requires Python 3.11–3.13 and Redis 7+ bound to loopback.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

cp core.env.template core.env   # safe defaults: localhost bind, flags off, blank secrets
# edit core.env: set at least one provider (e.g. FIREWORKS_API_KEY + FIREWORKS_MODEL,
# or OLLAMA_MODEL for a local Ollama)

.venv/bin/python bridge_core.py
# serves http://127.0.0.1:8766 — loopback is the safe development default
```

Smoke it:

```bash
curl http://127.0.0.1:8766/health
curl -X POST http://127.0.0.1:8766/message -H 'Content-Type: application/json' \
  -d '{"text": "hello"}'
```

The WebSocket endpoint is `ws://127.0.0.1:8766/ws/{OWNER_USER_ID}` (default
`owner`). Any other user id is rejected: `OWNER_USER_ID` is routing, not
authentication.

## Running tests

```bash
.venv/bin/python -m compileall .
.venv/bin/python -m unittest discover -s tests -v
```

Tests use an in-memory Redis substitute and mocked LLM transports; no live
services or network are needed.

## Deployment: Tailscale required (read before running remotely)

Bridge Core Engine is **not publicly exposed**. Production access is
Tailscale-only, direct-connect inside your tailnet:

1. Install Tailscale on the server and on every owner app device.
2. Set `BRIDGE_HOST` in `core.env` to the server's Tailscale address
   (`100.x.y.z`, see `tailscale status`) — or keep a broader bind **only**
   after firewalling the port (below) and setting
   `TAILSCALE_FIREWALL_ACK=true`.
3. Clients connect to `http://100.x.y.z:8766`. Traffic is HTTP/WS inside the
   tailnet; Tailscale WireGuard provides transport encryption.

Forbidden: router port forwarding, public reverse proxies, Tailscale Funnel.
`tailscale serve` is not required.

### Firewall contract

With `TAILSCALE_REQUIRED=true` (the default), startup **fails** unless one of
these is true:

1. `BRIDGE_HOST` is loopback (development only), or
2. `BRIDGE_HOST` is an address assigned to `tailscale0` on the server, or
3. `TAILSCALE_FIREWALL_ACK=true`, set only after you apply **and verify** a
   firewall rule admitting the bridge port exclusively from `tailscale0` /
   `100.64.0.0/10`.

Binding `0.0.0.0` alone is **not** private. Example with `ufw`:

```bash
sudo ufw allow in on tailscale0 from 100.64.0.0/10 to any port 8766 proto tcp
sudo ufw deny 8766/tcp
```

### Tailscale ACL example

Restrict the bridge port to approved owner devices (Tailscale policy file,
`acls` section — current grants syntax):

```json
{
  "tagOwners": {"tag:bridge-server": ["you@example.com"]},
  "acls": [
    {
      "action": "accept",
      "src": ["you@example.com"],
      "dst": ["tag:bridge-server:8766"]
    }
  ]
}
```

Enable device approval and set key expiry policy in the Tailscale admin
console — they are part of the security boundary, not optional advice.

### Security statement: no application auth

This version ships **no bearer tokens or accounts**. Each owner hosts a
private instance inside their own tailnet:

- Anyone admitted to the tailnet and allowed by ACL/firewall can reach the
  service.
- Use Tailscale ACLs and device approval; do not share unrestricted tailnet
  access.
- `OWNER_USER_ID` is routing only and never proves identity.
- Future public gateways must authenticate separately and must never expose
  owner app endpoints publicly.

### Verify a deployment

```bash
tailscale status
curl http://100.x.y.z:8766/health     # from an owner device
curl http://100.x.y.z:8766/status     # non-secret diagnostics
redis-cli -h 127.0.0.1 ping           # on the server
```

## Configuration

`core.env.template` documents every variable with safe behavior-inert
defaults; `core.env.full.example` shows the intended single-owner enabled
profile. Real environment variables override file values. Invalid numeric or
boolean values fail startup with a clear message. Secrets are never exposed
via `/status` or logs.

Needs/interaction tuning (thresholds, rates, turn effects, bid caps, owner
profile floors) lives in `schedule/needs.json` — the bundled values are
engine-safe neutrals, not character calibration; tune them for your
deployment.

## Repository layout (milestone 0.7.0)

```text
bridge_core.py            entrypoint
core/
  app.py                  FastAPI app, lifespan, HTTP/WS routes
  bridge.py               wiring + companion turn lifecycle + heartbeat + TTS stream
  config.py               Config dataclass, core.env loader, hot reload
  constants.py            VERSION, emotion palette, Redis key helpers
  cache.py                async Redis wrapper (required service)
  connections.py          multi-device ConnectionManager, per-owner locks
  llm.py                  provider router with failover
  speech.py               ElevenLabs TTS, Deepgram/AssemblyAI STT, audio validation
  static_lines.py         owner-authored no-LLM speech lines (blank = silence)
  static_lines.json       bundled schema-complete empty line tables
  history.py              companion history rows and delivery states
  prompts.py              companion/catch-up/life/analysis prompt builders
  text_utils.py           emotion segment parsing, scrubbers, TTS chunking
  tailscale.py            bind validation (section 27.2)
  emotions.py             manifest loading/validation
  emotions.json           bundled neutral emotion manifest
  needs.py                needs engine: evaluate/peek, zones, turn effects
  bids.py                 connection bids (registered on initiative delivery)
  rhythm.py               metadata-only owner-availability histograms
  state_expression.py     [CHARACTER STATE] block from STATE.md zones
  owner_profile.py        owner lived profile: boundaries, soft block, agreements,
                          strict-JSON proposals, status drift
  schedule.py             real-time character day schedule (DST-safe, hot reload)
  interaction.py          bounded deferred queue + busy-ladder counters
  life.py                 block-entry character life events + pending mentions
  memory.py               three-tier long-term store: durable Redis rows of record
                          (core:longterm:{owner}) + merge-by-text upserts, cleanup
                          policy, deterministic search; optional Chroma index
  chroma_store.py         optional Chroma semantic index (degrade-safe)
  memory_tiers.py         mid-term chapter ring (core:midterm:{owner}:companion),
                          strict-JSON durable-fact extraction, session close
  daily_tools.py          private daily tools: schemas, bounded loop support,
                          reminders (core:daily:reminders:{owner}), idempotency,
                          narration sanitizer
  web_tools.py            Tavily search/open with SSRF guards (HTTPS-only,
                          DNS validation, redirect revalidation, byte caps)
  initiative.py           heartbeat-initiative state, counting, cadence roll,
                          delivery accounting (core:initiative:{owner})
  external_profiles.py    dormant external-user profile store (plan section 19)
  context_feed.py         awareness block + bounded context feed (PAST/PENDING,
                          memory notes, chapter notes)
  user_schedule.py        contextual owner schedule (informational)
  sessions.py             work session/project registry and resolution
  mcp.py                  MCP registry + execution proxy (strict correlation)
  device.py               device daemon: levels, fences, routing, audit ring
  agent_runs.py           bounded agent loop, verification, checkpoints
  work_tools.py           per-turn work tool registry (MCP + device schemas)
  routes/profiles.py      GET/PATCH /profiles/owner (mistake-guard token)
  routes/state.py         read-only GET /state
  routes/schedule.py      GET /schedule, GET /awareness, POST /admin/reload-schedule
  routes/life.py          GET /life/today, GET /life/recent, POST /life/generate
  routes/user_schedule.py GET/PATCH /user-schedule (mistake-guard token)
  routes/sessions.py      GET /work, session list/get/archive, run diagnostics
  routes/history.py       GET /history, GET /history/midterm, POST /history/close
  routes/memories.py      GET/POST/PATCH/DELETE /memories, POST /memories/cleanup
  routes/external_profiles.py
                          dormant external-profile store/admin CRUD APIs
  routes/admin.py         POST /admin/wipe/{owner} (WIPE_USER guard)
identity/                 SOUL.md / PROFILE.md / STATE.md blank templates
skills/WORK_SKILLS.md     blank work-mode skills template (owner-authored)
schedule/needs.json       needs/interaction tuning template (conservative defaults)
life_events/              schema_example.disabled.json (inert; author your own)
tests/                    unittest suite (no live services required)
```

`BRIDGE_CORE_ENGINE_IMPLEMENTATION_PLAN.md` owns locked scope and decisions;
`BRIDGE_CORE_ENGINE_SPEC.md` is the living implementation contract;

## Milestone Roadmap

Milestone 0.1.0 scope: core transport (HTTP + WebSocket), text-only companion turns, multi-device `chat_sync`, heartbeats, the Fireworks/Chutes/Ollama/OpenAI-compatible LLM router, history persistence, and the health/status/emotions endpoints.

Milestone 0.2.0 scope: the speech and emotion pipeline — ElevenLabs TTS with sequential pipelined audio chunks (`done` always precedes chunks), Deepgram and AssemblyAI STT with strict audio payload validation, per-segment emotion parsing with control-tag/reasoning scrubbing, thinking status frames, the emotion-only retry, per-message language pins (en/es/ja), and owner-authored static lines (blank = protocol-only silence) for empty/failed STT. Audio is never stored server-side.

Milestone 0.3.0 scope: needs, interaction, and the owner lived profile — the `schedule/needs.json` tuning template with stats/zones/turn effects/critical shutdown, read-only `GET /state` polls, connection bids (deterministic reply satisfaction + expiry sweep; registration arrives with initiative), metadata-only rhythm histograms, and the owner lived profile: boundary penalties (EN/ES/JA classifiers, metadata-only), reversible soft block (no LLM/bids/history writes; one authored distance line per cooldown), agreements (cap 12 active, persona-tension floors), strict-JSON proposal chain (validated/clamped, raw text never stored), status drift, and agreement aftermath. `GET/PATCH /profiles/owner` ship with the `UPDATE_OWNER_PROFILE` mistake-guard token, and the owner's preferred language joins the reply-language fallback. All of it is flag-gated OFF by default.

Milestone 0.4.0 scope: real-time day schedule (`SCHEDULE_DIR` day files, DST-safe resolution, mtime hot reload, `GET /schedule`), the availability ladder (`free`/`soft_busy`/`busy`/`unavailable`; busy/unavailable messages defer into a bounded queue — first message in the window may speak the authored static line, no LLM, no fabricated speech), deferred catch-up (one answer per claimed batch under its own per-owner lock; entries restore on failure and expire after 48h), character life events (block-entry driven generation from enabled templates in `LIFE_EVENTS_DIR`, daily min/max + cooldown + skip activities, pending mentions cleared only after a delivered response, durable Redis fallback store `core:longterm:{owner}`), the awareness block + bounded `[LIFE CONTEXT]` feed (PAST/PENDING markers, `CONTEXT_FEED_MAX_TOKENS` budget), the contextual owner schedule (`GET/PATCH /user-schedule`, informational only, owner timezone changes require the `UPDATE_USER_SCHEDULE` guard), and `POST /admin/reload-schedule` (`RELOAD_SCHEDULE` guard). All flag-gated OFF by default.

Milestone 0.5.0 scope: work mode and the device daemon — sessions/projects (resolution order: explicit id → latest active for project → auto-create; archived sessions never auto-resume), work prompts with `skills/WORK_SKILLS.md`, the MCP execution proxy (the turn's `context.mcp_servers` is the only execution authority; schema-rich `mcp__server__tool` tools, legacy generic wrappers, strict id/run/connection correlation, structured timeout failures), the bounded agent loop (OpenAI-style tool-call arrays, provider pinning, `MCP_MAX_ITERATIONS` with a no-tools synthesis, verification forcing read-backs for writes), the pause protocol (`[STATUS: question]`/`[STATUS: request_permission]` pause without `done`; durable checkpoints; disconnect marks runs interrupted; explicit session+run ids resume from any device), the device daemon (`device_state` arm/disarm at read/full levels, version-1 schemas rejecting unknown fields, secret-path fences, per-turn caps, metadata-only audit ring, reconnect starts disarmed), work deferral + text-only work catch-up separated from companion entries, and work proceeding under the relationship soft block.

Milestone 0.6.0 scope: three-tier memory and private daily tools — mid-term chapters (history exceeding `COMPANION_COMPACT_THRESHOLD` distills its oldest slice into one bounded chapter in the `core:midterm:{owner}:companion` ring before history is replaced with `COMPANION_KEEP_RECENT` recent rows; any failure preserves history), strict-JSON durable-fact extraction (`MEMORY_*` providers, clamped/validated, secrets/code/prompt-claims filtered, near-duplicates merge with Chroma semantic candidates or deterministic token fallback), policy cleanup (pinned rows never delete; protected kinds survive; conversation decays faster than life; dry-run endpoint), `POST /history/close` (distill + extract + clear only after success + `session_reset` fan-out), the optional Chroma index (Redis stays the store of record; destructive operations and bounded-tier eviction delete Chroma first and preserve Redis on failure; outages degrade semantic search to deterministic token-overlap ranking), the hard-budget context feed as the only deduplicated renderer for memories/chapters/life rows (`CONTEXT_FEED_ENABLED`), private daily tools (`DAILY_TOOLS_ENABLED`: clock/arithmetic/units/planning, durable reminders, owner- and character-schedule reads, memory lookup, owner-schedule writes behind a deterministic explicit-intent gate; at most `DAILY_TOOL_MAX_CALLS` per turn; turn+call idempotency keys; deterministic narration sanitizer with one tool-less retry), and Tavily web search/open (`DAILY_WEB_ENABLED`, HTTPS-only, DNS-validated public hosts, redirect revalidation, byte/text caps, fail-closed without a key). New HTTP: `GET /history`, `GET /history/midterm`, `GET/POST/PATCH/DELETE /memories`, `POST /memories/cleanup`, `POST /admin/wipe/{owner}` (`WIPE_USER` guard clearing every documented key family plus Chroma rows). Compaction runs by threshold; extraction, cleanup, daily tools, and web stay flag-gated OFF.

Milestone 0.7.0 scope (current): heartbeat-driven initiative and the dormant external-user profile foundation — the initiative engine (`INITIATIVE_ENABLED`, default OFF): valid heartbeats count once per owner-global 60-second bucket no matter how many devices send, daily counters reset on the owner's civil day (`OWNER_TIMEZONE`), a `SHA-256` cadence roll over a private deployment seed (`INITIATIVE_SEED_FILE`, created once at `./data/initiative_seed`, never logged) decides eligibility so the engine never fires mechanically every Nth beat, and daily max / min gap / active turn / character schedule / critical needs / soft block (plus optional contextual owner-schedule sleep/busy via `INITIATIVE_RESPECT_OWNER_SCHEDULE`) all hard-suppress before generation. A candidate generates one short proactive message (deterministic reason: pending life mention, bond need, low fun, or a recent open thread; the model may answer `SILENCE`, which delivers nothing and counts nothing) and delivers through the standard pending/delivery protocol under the owner history lock: the `done` frame and `chat_sync` carry additive `initiative`/`initiative_action`/`initiated_by` origin metadata, counters advance and a connection bid registers only after source delivery plus delivered-history persistence, and `heartbeat_ack.initiative_counter` now reports the live heartbeat count (still `0` with the engine off). Webhook completion: startup reconciliation turns stale `pending` assistant rows into `delivery_unknown`, and a WS `message_ack` moves a matching row to `delivered` (idempotent). Dormant gateway profiles (plan section 19): `core:external_profile:{owner}:{platform}:{external_id}` documents with admin CRUD at `GET/POST/PATCH/DELETE /profiles/external[/...]` under the `UPDATE_EXTERNAL_PROFILE` / `DELETE_EXTERNAL_PROFILE` mistake guards — `EXTERNAL_USER_PROFILE_STORE_ENABLED=false` answers `409 feature_disabled` and creates no keys, and with the store on but behavior off (default) nothing in the app or any LLM path ever reads the records.

Planned (milestone 1.0.0): operations and release hardening — systemd unit, Tailscale deployment guide, config validation script, WS smoke script, resource cleanup and deadline audit, and the complete regression suite.

Speech flags (`TTS_ENABLED`, `STT_ENABLED`) and the 0.3.0/0.4.0 flags
(`NEEDS_ENABLED`, `BIDS_ENABLED`, `RHYTHM_ENABLED`, `STATE_EXPRESSION_ENABLED`,
`OWNER_PROFILE_ENABLED` and sub-flags, `SCHEDULE_ENABLED`, `LIFE_ENABLED`,
`USER_SCHEDULE_ENABLED`, `MEMORY_EXTRACTION_ENABLED`,
`MEMORY_CLEANUP_ENABLED`, `DAILY_TOOLS_ENABLED`, `DAILY_WEB_ENABLED`) default
OFF; when off, none of their
stores, prompt blocks, background tasks, or LLM calls run. See
`core.env.full.example` for the intended single-owner enabled profile.

## License

See [LICENSE](LICENSE).
