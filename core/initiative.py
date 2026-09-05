"""Heartbeat-driven initiative (plan section 23).

Intentionally small: heartbeats prove an owner device is connected and
active enough to receive an initiative. No screen content, application
names, images, or private telemetry exist anywhere in this engine.

Split of responsibilities:

- This module owns the owner-global state document
  (``core:initiative:{owner}``), the per-heartbeat counting (phase 1 of
  plan 23.3), the deterministic cadence roll (plan 23.4), and delivery
  accounting (the post-delivery counter updates of plan 23.3 step 16).
- ``core.bridge`` owns generation and delivery (plan 23.3 steps 9-15):
  the expensive gates, the deterministic reason selection, the proactive
  LLM call, and the section 12 pending/delivery protocol under the
  per-owner history lock.

Counting only runs when both ``HEARTBEAT_ENABLED`` and
``INITIATIVE_ENABLED`` are on — a flags-off deployment never creates the
key (plan section 28: no writes while feature flags are off). The plan's
trigger order counts before its disabled check; the flag-off parity rule
wins, so the disabled case simply never reaches the engine.

Atomicity (plan 23.3): bucket claim, window reset, daily reset, count
increment, and target selection all happen inside one read-modify-write of
the state document while the caller holds the per-owner initiative lock.
The engine is single-process per deployment, so that lock serializes every
mutator; no Lua/transaction is needed for correctness here.
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .cache import RedisCache
from .constants import initiative_key

log = logging.getLogger("bridge.initiative")

SEED_BYTES = 32

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SEED_PATH = REPO_ROOT / "data" / "initiative_seed"

# Bounded ``last_decision.reason`` values (plan 23.2: "bounded metadata").
REASON_BELOW_THRESHOLD = "below_threshold"
REASON_DAILY_MAX = "daily_max"
REASON_MIN_GAP = "min_gap"
REASON_CADENCE_ROLL = "cadence_roll"
REASON_ELIGIBLE = "eligible"
REASON_NO_TARGET = "no_target"
REASON_ACTIVE_TURN = "active_turn"
REASON_SCHEDULE = "schedule"
REASON_NEEDS = "needs"
REASON_SOFT_BLOCK = "soft_block"
REASON_OWNER_SCHEDULE = "owner_schedule"
REASON_NO_REASON = "no_reason"
REASON_SILENCE = "silence"
REASON_LLM_FAILED = "llm_failed"
REASON_UNDELIVERED = "undelivered"
REASON_DELIVERED = "delivered"

# Deterministic initiative reasons, in priority order (plan 23.3 step 13).
INITIATIVE_ACTIONS: tuple[str, ...] = ("life", "bond", "fun", "thread")

BID_KIND_BY_ACTION: dict[str, str] = {
    "life": "initiative_life",
    "bond": "initiative_bond",
    "fun": "initiative_fun",
    "thread": "initiative_thread",
}


class SeedUnavailable(RuntimeError):
    """The deployment seed file cannot be read or created.

    Initiative determinism depends on a stable seed, so a broken seed file
    disables initiative instead of degrading silently to a random roll.
    """


def fresh_state(day_key: str) -> dict:
    """Plan 23.2 schema."""
    return {
        "day_key": day_key,
        "heartbeat_count": 0,
        "window_started_ts": 0,
        "last_counted_bucket": 0,
        "counted_devices_in_bucket": [],
        "threshold_connection_id": "",
        "initiative_count_today": 0,
        "last_initiative_ts": 0,
        "last_decision": {"action": "no_action", "reason": "", "ts": 0},
    }


def owner_day_key(timezone_name: str, now_ts: float) -> str:
    """The owner's civil day (``OWNER_TIMEZONE`` owns daily reset)."""
    try:
        zone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        zone = ZoneInfo("UTC")
    return datetime.fromtimestamp(now_ts, tz=timezone.utc).astimezone(
        zone
    ).date().isoformat()


def cadence_roll(seed: str, day_key: str, counted_number: int) -> float:
    """Deterministic 0-1 roll (plan 23.4).

    SHA-256 of deployment seed + owner day key + counted heartbeat number,
    mapped to [0, 1). Stable under retries, never mechanically every Nth
    heartbeat.
    """
    digest = hashlib.sha256(
        f"{seed}:{day_key}:{int(counted_number)}".encode("utf-8")
    ).digest()
    return int.from_bytes(digest, "big") / float(2**256)


class InitiativeEngine:
    """Owner-global initiative counters under ``core:initiative:{owner}``."""

    def __init__(self, config, cache: RedisCache) -> None:
        self.config = config
        self.cache = cache
        self._seed: str | None = None
        self._seed_failed = False

    @property
    def available(self) -> bool:
        return bool(self.config.HEARTBEAT_ENABLED and self.config.INITIATIVE_ENABLED)

    def key(self, owner: str) -> str:
        return initiative_key(owner)

    # -- deployment seed (plan 23.4) ------------------------------------------

    def seed_path(self) -> Path:
        override = self.config.INITIATIVE_SEED_FILE.strip()
        return Path(override) if override else DEFAULT_SEED_PATH

    def ensure_seed(self) -> str:
        """Load or create the deployment seed exactly once; never logged."""
        if self._seed is not None:
            return self._seed
        if self._seed_failed:
            raise SeedUnavailable("initiative seed previously failed")
        path = self.seed_path()
        try:
            if path.exists():
                seed = path.read_text(encoding="utf-8").strip()
                if not seed:
                    raise ValueError("empty seed file")
            else:
                seed = secrets.token_hex(SEED_BYTES)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(seed, encoding="utf-8")
        except OSError as exc:
            self._seed_failed = True
            raise SeedUnavailable(f"seed file unusable: {type(exc).__name__}") from None
        self._seed = seed
        return seed

    # -- state access -----------------------------------------------------------

    async def load(self, owner: str) -> dict | None:
        raw = await self.cache.get_value(self.key(owner))
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("Corrupt initiative state ignored for owner")
            return None
        return data if isinstance(data, dict) else None

    async def save(self, owner: str, state: dict) -> None:
        await self.cache.set_value(self.key(owner), json.dumps(state))

    # -- phase 1: counting (plan 23.3 steps 2-8 + 23.4 roll) ---------------------

    async def count(self, owner: str, connection_id: str, now_ts: float | None = None) -> dict:
        """Account one valid heartbeat. Caller holds the initiative lock.

        Returns ``{"counted", "heartbeat_count", "candidate",
        "target_connection_id", "reason"}``. ``candidate`` means every cheap
        gate passed and the cadence roll succeeded — bridge may proceed to
        the expensive gates and delivery.
        """
        now_ts = time.time() if now_ts is None else float(now_ts)
        cfg = self.config
        state = await self.load(owner)
        if state is None:
            state = fresh_state(owner_day_key(cfg.OWNER_TIMEZONE, now_ts))
            state["window_started_ts"] = now_ts

        changed = False
        # Step 2: daily counters reset on owner civil day change.
        day_key = owner_day_key(cfg.OWNER_TIMEZONE, now_ts)
        if state.get("day_key") != day_key:
            state["day_key"] = day_key
            state["initiative_count_today"] = 0
            changed = True
        # Step 3: heartbeat window expiry resets the count.
        if (
            int(state.get("heartbeat_count", 0)) > 0
            and now_ts - float(state.get("window_started_ts", 0) or 0)
            > cfg.INITIATIVE_HEARTBEAT_WINDOW_SECONDS
        ):
            state["heartbeat_count"] = 0
            state["threshold_connection_id"] = ""
            changed = True

        # Step 4: owner-global server-time bucket; at most one count per
        # bucket no matter how many devices send. The first valid sender in
        # a newly claimed bucket becomes that bucket's candidate target.
        bucket = int(now_ts // cfg.INITIATIVE_HEARTBEAT_COUNT_INTERVAL_SECONDS)
        counted = bucket != int(state.get("last_counted_bucket", 0) or 0)
        if counted:
            state["last_counted_bucket"] = bucket
            state["heartbeat_count"] = int(state.get("heartbeat_count", 0)) + 1
            state["counted_devices_in_bucket"] = [connection_id]
            if (
                state["heartbeat_count"] == cfg.INITIATIVE_MIN_HEARTBEATS
                and not state.get("threshold_connection_id")
            ):
                state["threshold_connection_id"] = connection_id
            if state["heartbeat_count"] == 1:
                state["window_started_ts"] = now_ts
            changed = True

        # Steps 6-8: cheap caps, then the plan 23.4 cadence roll.
        reason = ""
        candidate = False
        if int(state.get("heartbeat_count", 0)) < cfg.INITIATIVE_MIN_HEARTBEATS:
            reason = REASON_BELOW_THRESHOLD
        elif int(state.get("initiative_count_today", 0)) >= cfg.INITIATIVE_DAILY_MAX:
            reason = REASON_DAILY_MAX
        elif (
            float(state.get("last_initiative_ts", 0) or 0)
            and now_ts - float(state["last_initiative_ts"])
            < cfg.INITIATIVE_MIN_GAP_SECONDS
        ):
            reason = REASON_MIN_GAP
        else:
            roll = cadence_roll(
                self.ensure_seed(), day_key, int(state["heartbeat_count"])
            )
            if roll >= cfg.INITIATIVE_ELIGIBILITY_CHANCE:
                reason = REASON_CADENCE_ROLL
            else:
                candidate = True
                reason = REASON_ELIGIBLE

        previous_reason = str((state.get("last_decision") or {}).get("reason", ""))
        state["last_decision"] = {
            "action": "no_action",
            "reason": reason,
            "ts": datetime.fromtimestamp(now_ts, tz=timezone.utc).isoformat(),
        }
        # Skip the write only when nothing at all moved (same bucket, same
        # counters, same decision) — repeated heartbeats stay read-mostly.
        if changed or reason != previous_reason:
            await self.save(owner, state)
        return {
            "counted": counted,
            "heartbeat_count": int(state.get("heartbeat_count", 0)),
            "candidate": candidate,
            "target_connection_id": str(state.get("threshold_connection_id", "")),
            "reason": reason,
        }

    # -- phase 2 accounting (plan 23.3 step 16) -----------------------------------

    async def record_delivery(
        self, owner: str, now_ts: float | None = None
    ) -> dict:
        """Post-delivery accounting. Caller holds the initiative lock.

        Runs only after source delivery AND delivered-history persistence
        both succeeded (plan 23.3 step 16): daily count, heartbeat-count
        reset, last-initiative stamp.
        """
        now_ts = time.time() if now_ts is None else float(now_ts)
        state = await self.load(owner)
        if state is None:
            state = fresh_state(owner_day_key(self.config.OWNER_TIMEZONE, now_ts))
        state["initiative_count_today"] = int(state.get("initiative_count_today", 0)) + 1
        state["heartbeat_count"] = 0
        state["threshold_connection_id"] = ""
        state["window_started_ts"] = 0
        state["last_counted_bucket"] = 0
        state["counted_devices_in_bucket"] = []
        state["last_initiative_ts"] = now_ts
        decision = state.get("last_decision") or {}
        decision["action"] = decision.get("action") or "no_action"
        decision["reason"] = REASON_DELIVERED
        decision["ts"] = datetime.fromtimestamp(now_ts, tz=timezone.utc).isoformat()
        state["last_decision"] = decision
        await self.save(owner, state)
        return state

    async def note_decision(
        self, owner: str, action: str, reason: str, now_ts: float | None = None
    ) -> None:
        """Bounded decision metadata for diagnostics (plan 23.2)."""
        now_ts = time.time() if now_ts is None else float(now_ts)
        state = await self.load(owner)
        if state is None:
            state = fresh_state(owner_day_key(self.config.OWNER_TIMEZONE, now_ts))
        state["last_decision"] = {
            "action": action,
            "reason": reason,
            "ts": datetime.fromtimestamp(now_ts, tz=timezone.utc).isoformat(),
        }
        await self.save(owner, state)
