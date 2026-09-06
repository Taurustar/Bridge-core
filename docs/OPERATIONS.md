# Operating Bridge Core Engine (day-2 runbook)

Deployment steps live in `docs/DEPLOYMENT.md`. This file is the runbook for
a running instance: health, logs, backups, wipes, and the failure modes you
are most likely to meet.

## Daily health

```bash
systemctl status bridge-core-engine
journalctl -u bridge-core-engine -n 100 --no-pager
curl http://127.0.0.1:8766/health    # from the VPS: {"status":"ok","redis":true}
curl http://127.0.0.1:8766/status    # full non-secret diagnostics
redis-cli ping                       # PONG
```

`/health` is the minimal check (process + Redis). `/status` shows version,
deployment mode, provider configuration (never keys), enabled features,
schedule/life/needs/work/memory/initiative/device state, and connection
count. Anything you would show in a status tile is already there.

Log discipline: at INFO the engine logs only metadata (connection ids,
provider names, message ids, feature flags, bounded decision reasons). It
never logs secrets, full prompts, audio, or private turn bodies. If you
raise `LOG_LEVEL=debug` for a session, remember to set it back.

## Startup banner and what it proves

Every start logs one line like:

```
Bridge Core Engine 1.0.0 starting: bind=100.x.y.z:8766 deployment=tailscale
features_enabled=[...] routes=[...]
```

- `bind`/`deployment`: with `TAILSCALE_REQUIRED=true` and a tailscale
  address, deployment is `tailscale`; loopback dev says `loopback-dev`.
- `features_enabled`: exactly the flags that are on. If a flag you expected
  is missing, check `scripts/validate_config.py core.env`.

## Backups

What matters, in order of irreplaceability:

1. **Redis data** — companion history, memory tiers, needs, owner profile,
   sessions, reminders, initiative counters. Use Redis persistence
   (`appendonly yes` is a reasonable default) and snapshot the dump:

   ```bash
   redis-cli BGSAVE
   # copy /var/lib/redis/dump.rdb (or your dir) somewhere safe
   ```

2. **Identity files** — `identity/SOUL.md`, `PROFILE.md`, `STATE.md`,
   `skills/WORK_SKILLS.md`, `schedule/` day files, `life_events/`. The
   engine never writes these; a file-level backup of the runtime directory
   covers them.

3. **`core.env`** — secrets. Back it up deliberately and protect the copy.

4. **`data/`** — optional Chroma index and the initiative seed
   (`data/initiative_seed`). The Chroma tree can be rebuilt from Redis; the
   seed can be regenerated (changing initiative roll parity) but should
   just stay untouched on the server.

Restore = fresh Redis + restored dump + runtime files + `core.env`, then
start and run `scripts/ws_smoke.py`.

## Admin actions (all are mistake guards, not auth)

| Action | How |
|---|---|
| Hot-reload config | `POST /admin/reload-config` |
| Reload schedule files | `POST /admin/reload-schedule` `{"confirm":"RELOAD_SCHEDULE"}` |
| Wipe one owner's every key + Chroma rows | `POST /admin/wipe/{owner}` `{"confirm":"WIPE_USER"}` |

The wipe clears every documented key family (plan section 28 inventory) and
the owner's Chroma rows; a failed Chroma deletion returns
`503 wipe_failed` and preserves Redis so a partial wipe is never reported
as complete. Destructive but reversible from backups — take one first.

## Common failure modes

**Engine won't start: config error.** Run
`python3 scripts/validate_config.py core.env`. The message names the field.

**Engine won't start: Redis.** `systemctl status redis-server`,
`redis-cli ping`. Redis is required by design; there is no RAM-only mode.

**Engine won't start: bind rejected.** With `TAILSCALE_REQUIRED=true` the
bind must be loopback, a real `tailscale0` address, or paired with
`TAILSCALE_FIREWALL_ACK=true` plus an actual firewall rule (plan 27.2).

**Client connects, immediately closes with 4003.** The `user_id` in the WS
path does not equal `OWNER_USER_ID`. This is routing, by design.

**LLM errors (`llm_unavailable`).** Check `/status` providers; a provider
with a blank key/model is skipped with one startup warning. The chain has
`LLM_CHAIN_DEADLINE_SECONDS` to answer before failing a turn.

**Chroma degraded (`chroma_degraded: true` in `/status`).** Chat, memory
writes, and cleanup keep working; semantic search falls back to
deterministic token ranking. Fix Chroma (`CHROMA_PATH` permissions,
installation) or set `CHROMA_REQUIRED=false` and leave it degraded — Redis
is the store of record either way.

**Message shows as missing after a crash/restart.** An assistant row that
was `pending` at SIGKILL becomes `delivery_unknown` at the next startup
(after 60s). Clients confirm receipt with `message_ack`; ops can inspect
`GET /history` which exposes delivery states.

**Initiative never fires.** Checklist: `INITIATIVE_ENABLED=true` in
`/status` → heartbeats arriving (client interval ≤ counting interval) →
`INITIATIVE_MIN_HEARTBEATS` reachable within the window → daily max / min
gap not exhausted → character schedule `free` (`INITIATIVE_REQUIRE_SCHEDULE_FREE`)
→ no critical needs / soft block → the deterministic roll (some counted
heartbeats simply lose it; that is the design, not a fault).

**Initiative fired too often.** Lower `INITIATIVE_ELIGIBILITY_CHANCE`,
raise `INITIATIVE_MIN_GAP_SECONDS`, or lower `INITIATIVE_DAILY_MAX`. All
are hot-reloadable.

**Soft block stuck on.** Inspect `GET /profiles/owner`; lift with
`PATCH /profiles/owner` (`X-Confirm-Token: UPDATE_OWNER_PROFILE`,
`{"soft_blocked": false}`) — or wait out the cooldown with trust above
`OWNER_SOFT_BLOCK_UNBLOCK_TRUST_FLOOR`.

## Upgrades and restarts

- Restarting mid-turn is safe by construction: user rows persist before
  provider calls; assistant rows reconcile via delivery states; deferred
  messages survive their 48h TTL; checkpoints expire after 24h.
- `systemctl restart bridge-core-engine` after file changes; hot reload
  (`POST /admin/reload-config`) covers flag/tuning changes without a
  restart, but a restart is always the safer path for runtime wiring
  (speech, Chroma, schedule dir changes).
- After any upgrade: `scripts/validate_config.py`, restart,
  `scripts/ws_smoke.py --probe-foreign-user`.
