# Deploying Bridge Core Engine (fresh VPS, end to end)

This guide takes one fresh Ubuntu/Debian VPS from nothing to a running,
Tailscale-only Bridge Core Engine 1.0.0. It implements the plan section 27
deployment requirements. Read it once fully before running anything.

Time needed: roughly 20-30 minutes.

## 0. What you need

- A VPS with root access (Debian 12/Ubuntu 22.04+ assumed), 1 vCPU/1 GB is
  enough.
- A Tailscale account. The VPS and **every** device you will chat from must
  join your tailnet.
- Your character's identity files (`SOUL.md`, `PROFILE.md`, `STATE.md`) if
  you have authored them.

Security model, in one paragraph: there is **no application login**. The
whole security boundary is "who can reach the port". Only your tailnet can
reach it, and only devices your ACLs allow. Anyone admitted to the tailnet
and permitted by ACL/firewall can talk to the character as the owner.
`OWNER_USER_ID` is routing, not identity. Use Tailscale device approval and
key expiry; do not share unrestricted tailnet access.

## 1. System packages

As root on the VPS:

```bash
apt update
apt install -y python3 python3-venv python3-pip redis-server git curl
systemctl enable --now redis-server
```

Redis is a **required** service — the engine refuses to start without it.

## 2. Runtime user and directory

```bash
useradd --system --create-home --shell /usr/sbin/nologin bridgecore
mkdir -p /opt/bridge-core-engine
```

Copy the runtime files into `/opt/bridge-core-engine` (rsync/scp from your
machine, or `git clone` and copy). Runtime means: `bridge_core.py`,
`pyproject.toml`, `requirements.txt`, `core/`, `identity/`, `schedule/`,
`skills/`, `life_events/`, and your `core.env`. You do not need `tests/`,
`assets/`, or `.git/` on the server.

```bash
chown -R bridgecore:bridgecore /opt/bridge-core-engine
```

## 3. Python environment

```bash
sudo -u bridgecore bash -lc 'cd /opt/bridge-core-engine &&
  python3 -m venv .venv &&
  .venv/bin/pip install -r requirements.txt'
```

Optional semantic memory: `.venv/bin/pip install 'bridge-core-engine[chroma]'`
or `.venv/bin/pip install chromadb`. Without it, long-term memory keeps
working over durable Redis with deterministic ranking (Redis is the store of
record; Chroma is only a search index).

## 4. Configure core.env

Copy `core.env.template` to `core.env` in the runtime directory and edit it.
Minimum production changes:

```bash
OWNER_USER_ID=<your owner id>          # routing id your app connects as
BRIDGE_HOST=<the VPS Tailscale IPv4>   # see step 5; e.g. 100.101.102.103
OWNER_TIMEZONE=<your IANA timezone>    # e.g. America/Santiago
CHARACTER_TIMEZONE=<her IANA timezone>
```

Leave `TAILSCALE_REQUIRED=true` (the safe default). Behavior flags default
OFF; turn them on deliberately, one group at a time. The full flag and
variable reference lives in `docs/ENVIRONMENT.md`; day-2 operations
(backup, wipe, logs, incidents) live in `docs/OPERATIONS.md`.

Validate before every start or restart:

```bash
sudo -u bridgecore bash -lc 'cd /opt/bridge-core-engine &&
  .venv/bin/python scripts/validate_config.py core.env'
```

It exits nonzero with a clear message on any bad value and never prints
secrets. **The init seed for initiative** (`./data/initiative_seed`) is
created automatically on first use when `INITIATIVE_ENABLED=true`; keep the
directory writable by `bridgecore` and never copy the seed between machines.

## 5. Bind to Tailscale (the privacy decision)

Install Tailscale on the VPS: `curl -fsSL https://tailscale.com/install.sh | sh`,
then `tailscale up` and approve the device. Note its address:

```bash
tailscale ip -4     # e.g. 100.101.102.103
```

Pick **one** of the two allowed postures (plan 27.2):

**Option A — bind the Tailscale address (recommended, simplest).**
Set `BRIDGE_HOST=100.101.102.103` in `core.env`. Startup fails if that
address is not actually assigned to `tailscale0` — that check is the point.

**Option B — bind all interfaces + firewall.** Set
`BRIDGE_HOST=0.0.0.0`, keep only tightly-restricted ingress (below), and
set `TAILSCALE_FIREWALL_ACK=true` to acknowledge you did it. Startup fails
without that acknowledgement on purpose: binding wide is only private if
the firewall makes it so. Never claim `0.0.0.0` alone is private.

In both options: no router port forwarding, no public reverse proxy, no
Tailscale Funnel. `tailscale serve` is not needed.

### Firewall (required for option B, good hygiene for A)

Allow SSH, allow the bridge port only from the tailnet interface, default
deny everything else:

```bash
ufw default deny incoming
ufw allow OpenSSH
ufw allow in on tailscale0 to any port 8766 proto tcp
ufw enable
```

(If your cloud provider uses an external security group, mirror the same
rule there: allow TCP 8766 only from `100.64.0.0/10`.)

## 6. Tailscale ACL example

Permit the bridge port only from approved owner devices (tag `tag:owner-devices`)
to the server (tag `tag:bridge`). Review the current syntax against
Tailscale's documentation at install time — both accepted formats are shown.

Modern `grants` format (`tailscale policy` / admin console):

```jsonc
{
  "tagOwners": {
    "tag:bridge": ["autogroup:admin"],
    "tag:owner-devices": ["autogroup:admin"]
  },
  "grants": [
    {
      "src": ["tag:owner-devices"],
      "dst": ["tag:bridge"],
      "ip": ["tcp:8766"]
    }
  ]
}
```

Legacy `acls` format (still valid on many tailnets):

```jsonc
{
  "tagOwners": {
    "tag:bridge": ["autogroup:admin"],
    "tag:owner-devices": ["autogroup:admin"]
  },
  "acls": [
    {
      "action": "accept",
      "src": ["tag:owner-devices"],
      "dst": ["tag:bridge:8766"]
    }
  ]
}
```

Test the policy, do not assume it:

```bash
# From an approved owner device (should succeed):
tailscale ping 100.101.102.103
curl http://100.101.102.103:8766/health

# From a tailnet device NOT in tag:owner-devices (should time out / refuse):
tailscale ping 100.101.102.103
```

Also enable device approval (`tailscale set --approve-connections` behavior
via the admin console) and set key expiry on owner devices deliberately.

## 7. systemd service

The unit ships in `deploy/bridge-core-engine.service` (dedicated user,
`EnvironmentFile=`, restart on failure, SIGTERM, journald, Redis and
tailscaled ordering).

```bash
cp deploy/bridge-core-engine.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now bridge-core-engine
```

`Restart=on-failure` covers crashes; `TimeoutStopSec=30` gives in-flight
turns a bounded grace period. A turn still running at SIGKILL leaves its
assistant row `pending`; the next startup reconciles it to
`delivery_unknown`, and your client can confirm it with `message_ack`.
Nothing is lost or duplicated.

## 8. Verify the deployment

```bash
systemctl status bridge-core-engine
journalctl -u bridge-core-engine -f        # startup banner: version, bind,
                                           # deployment mode, enabled features
tailscale status
redis-cli ping                             # PONG
curl http://100.101.102.103:8766/health    # {"status":"ok","redis":true,...}
curl http://100.101.102.103:8766/status    # full non-secret diagnostics
```

Then run the smoke script from any machine (or the VPS itself):

```bash
python3 scripts/ws_smoke.py --host 100.101.102.103 --port 8766 \
  --user <your owner id> --probe-foreign-user
```

It checks `/health`, the `connected` frame (version + capabilities), a
heartbeat ack, the structured error path, and (with the flag) that a
non-owner id is rejected. Exit code 0 means the deployment answers the
protocol correctly end to end.

Finally connect your app client over Tailscale and send one message.

## 9. Upgrading

1. Read the release notes; note any new env fields
   (`scripts/validate_config.py` catches mistakes).
2. Stop or let the socket drain: `systemctl stop bridge-core-engine`.
3. Replace the runtime files (keep `core.env`, `data/`, and Redis).
4. `python3 scripts/validate_config.py core.env`
5. `systemctl start bridge-core-engine` and re-run the smoke script.

`scp`-style copies overwrite but never delete: if a release retires a file,
remove it on the server yourself. Never copy `data/initiative_seed` between
machines unless you intend identical initiative rolls.

## 10. What must never exist here

For v1 there is deliberately no code or config for: public gateways
(Discord/WhatsApp/Telegram), image/video generation, intimacy content,
appraisal/reflection/SER loops, accelerated or looped world time,
appointment booking, public internet exposure, and bearer-token auth. If a
checkout claims to be Bridge Core Engine and contains any of those, it is
not this project. See `docs/RELEASE_AUDIT.md` for the 1.0.0 verification.
