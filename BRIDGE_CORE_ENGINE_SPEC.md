# Bridge Core Engine — Implementation Spec

Living implementation contract. It refines unspecified details of
`BRIDGE_CORE_ENGINE_IMPLEMENTATION_PLAN.md`; it may not override locked
decisions (plan section 2). Milestone implemented: **0.1.0**.

## Deviations from the plan

- **Repository**: the plan locks a new repo named `bridge-core-engine`. The
  project owner explicitly directed that all code live in the existing
  `Bridge-core/` repository instead. Everything else follows the plan.
- **Module set**: only the modules 0.1.0 needs exist (plan section 35 — no
  placeholder scaffolding). Added relative to plan section 5's full layout:
  `core/tailscale.py` (bind validation) and `core/emotions.py` (manifest
  loading/validation). The bundled neutral emotion manifest ships as
  `core/emotions.json` so it travels with the package; `EMOTIONS_FILE`
  overrides it.

## Protocol decisions (unspecified details, now decided)

### WebSocket

- `capabilities` in the `connected` frame lists what exists in 0.1.0:
  `["text", "heartbeat", "chat_sync"]`. Audio/work/mcp/device capabilities
  are added with their milestones.
- Non-owner `user_id`: the server accepts the socket, sends a terminal
  `error` frame with code `forbidden_user`, then closes with code `4003`.
- Error frames carry the standard shape
  `{"type": "error", "error": {"code", "message", "details": {}}}`; frames
  that terminate a turn add `"terminal": true` so clients never wait for a
  `done` that will not come (plan section 30.2).
- Unknown `mode` values → terminal `unknown_mode` error frame, never silent
  companion fallback. `mode: "work"` → terminal `work_unavailable` error
  frame; work mode ships in milestone 0.5.0.
- Empty/whitespace `text` → terminal `empty_input` error frame; no LLM call
  and no history write (plan section 12 step 3).

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

### Companion turn (0.1.0 simplification of plan section 12)

Skipped hooks (they do not exist yet; flags off, no keys, no tasks, no LLM
calls): needs/bids/rhythm, schedule/availability, owner profile, memory
retrieval/extraction, TTS, life mentions, catch-up.

- User rows persist with `delivery_state: "delivered"` (they were received
  from the owner device) **before** the provider call; `chat_sync` fanout
  failure never rolls back persistence.
- Assistant rows persist `pending`, become `delivered` after the source
  `done` send succeeds (followed by assistant `chat_sync` to other devices),
  or `undelivered` on source-send failure (excluded from prompt history, no
  fanout). `delivery_unknown` is reserved for the crash-recovery
  reconciliation that ships with the history APIs milestone; 0.1.0 produces
  no `delivery_unknown` rows.
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
  Fireworks' pass-through are unused in 0.1.0 (no TTS/work callers exist).

### HTTP

- `POST /message` accepts `{"user_id"?, "text", "mode"?, "language"?}`;
  `user_id` defaults to `OWNER_USER_ID` when omitted and 403s otherwise.
  Returns the terminal done-shaped JSON on success; `llm_unavailable` maps to
  502, validation failures to 400, all in the standard error shape. Full
  webhook completion (tool-less work turns, `message_ack` reconciliation) is
  milestone 0.7.0. HTTP-originated turns never drive MCP or the device
  daemon — neither exists yet.
- `/health` returns 200 `{"status": "ok", ...}` or 503
  `{"status": "degraded", "redis": false, ...}`; Redis is a required service
  and startup pings it.
- `/status` exposes version, Redis health, provider configured booleans and
  URLs (never credentials), feature flags, identity file paths/mtimes (never
  contents), companion routes, deployment mode, and connection count.
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
- HTTP+WS tests use FastAPI's `TestClient`; no network, no live Redis.

## Redis keys in 0.1.0

Exactly one key family may exist: `core:history:{owner}:companion` (list of
JSON rows: `id`, `role`, `text`, `emotion`, `ts`, `delivery_state`). The
flags-off test asserts no other keys are created by a turn. All other keys in
plan section 28 belong to later milestones.

## Version source

`core/constants.py::VERSION = "0.1.0"` is the single source; the entrypoint
docstring, README, `connected` frame, and `/status` derive from it.
