# Release audit — v1.0.0

Plan section 31 requires the 1.0.0 milestone to ship a resource cleanup and
deadline audit and to prove the excluded-forever list is still excluded.
This document records the audit performed for the 1.0.0 release, what was
found, and what changed. The release tests in `tests/test_release.py`
re-check the machine-verifiable parts on every run.

## 1. Resource cleanup audit (plan 6.1, 11, 30)

| Area | Result |
|---|---|
| Background tasks | All `asyncio.create_task` sites either run under `bridge.background_tasks` (cancelled and gathered on shutdown) or are awaited inline. TTS lookahead tasks are cancelled + gathered in `_stream_tts`'s `finally` (plan 10.6). |
| Lifespan shutdown | `Bridge.shutdown` cancels the life poll task, all tracked background tasks, and closes the LLM, STT, TTS, and web HTTP clients; the lifespan then closes the Redis client. |
| Pending MCP/device futures | Removed on success, timeout, and disconnect (`ConnectionManager.disconnect` fails a connection's futures); one-time, connection-bound, run-bound. |
| Work pause bookkeeping | `_pending_pauses` entries are popped on resume/clear and converted to `interrupted` on disconnect; durable checkpoints expire after 24h. |
| Blocking work off the loop | Chroma operations run on a **dedicated single-thread executor** (plan 6.1). Changed in this release: `asyncio.to_thread` (default multi-thread pool) → `chroma_store.run_chroma_call` on one `ThreadPoolExecutor(max_workers=1)` worker, serializing Chroma's SQLite-backed client. Identity file reads use mtime caches; the initiative seed is read once per process. |
| Bounded stores | History rows, sessions, chapters, longterm rows, reminders, idempotency keys, bids, and the audit ring all have caps; deferred entries expire in 48h. Lock dictionaries grow per owner/session only (bounded by the 200-session cap in practice). |
| Chroma client lifecycle | The `chromadb.PersistentClient` has no explicit close API; the process exits through uvicorn's normal shutdown and the executor worker is joined by `concurrent.futures`' atexit hook. Accepted: no leak of unbounded resources. |
| Graceful stop vs systemd | `TimeoutStopSec=30` may SIGKILL a turn still inside the 75s LLM deadline. By construction this is safe: user rows persist before provider calls; assistant rows left `pending` become `delivery_unknown` at the next startup and reconcile via `message_ack` (plan 12). |

## 2. Deadline audit

Every external wait is bounded: per-provider HTTP timeouts
(`*_TIMEOUT`), the chain deadline (`LLM_CHAIN_DEADLINE_SECONDS`), MCP and
device tool deadlines (`MCP_TOOL_TIMEOUT`, `DEVICE_TOOL_TIMEOUT`,
`DEVICE_SHELL_TIMEOUT` clamped to `DEVICE_SHELL_TIMEOUT_MAX`), web fetch
caps (`DAILY_WEB_TIMEOUT`, byte and text caps), the AssemblyAI poll loop
(bounded by `ASSEMBLYAI_TIMEOUT`), agent loop iterations
(`MCP_MAX_ITERATIONS`), and per-turn device call caps. No unbounded
`await` on an external system exists in `core/`.

## 3. Excluded-forever audit (plan section 2, AGENTS.md hard rules)

The acceptance bar: no public gateway, media generation, intimacy,
appraisal, reflection, SER, or time-loop code exists.

- Source scan of `core/` and `bridge_core.py` for gateway/SDK markers
  (`discord`, `whatsapp`, `telegram`), media generation (`image
  generation`, diffusion/`dall-e` style providers), `bearer` auth, and
  Funnel/public exposure: **no hits**. The only bearer of those words is
  prose that forbids them (docs, comments like "Intimacy kinds are excluded
  forever").
- Dormant external-user profiles are storage + admin CRUD only; no app
  prompt, turn update, or LLM analysis path reads them (plan 19.4), and
  `EXTERNAL_USER_PROFILES_BEHAVIOR_ENABLED` /
  `EXTERNAL_USER_PROFILE_LLM_ENABLED` gate nothing today.
- Time authority is wall-clock real time; there is no accelerated or looped
  world time anywhere. No appointment or joint-free booking APIs exist.
- No application auth exists to audit: the service is Tailscale-only, bind
  validation enforces it at startup, and confirm tokens are mistake guards,
  never authentication.
- `tests/test_release.py` asserts the scan stays clean and that every
  feature flag and Redis key family is documented (plan section 31
  acceptance).

## 4. Documentation completeness

- Fresh VPS deployment end to end: `docs/DEPLOYMENT.md` (packages, user,
  venv, `core.env`, Tailscale bind decision, firewall, ACL examples,
  systemd, health checks, upgrade procedure).
- Full environment reference: `docs/ENVIRONMENT.md` (every `Config` field;
  test-enforced).
- Runbook: `docs/OPERATIONS.md`.
- Redis keys and retention: plan section 28 inventory mirrored in
  `constants.wipe_key_patterns`, `BRIDGE_CORE_ENGINE_SPEC.md`, and the
  README; the wipe test exercises every pattern.
- Protocols: `BRIDGE_CORE_ENGINE_IMPLEMENTATION_PLAN.md` sections 10-26 plus
  `BRIDGE_CORE_ENGINE_SPEC.md` decisions; client-side mirrors in
  `unity-agent/`.
