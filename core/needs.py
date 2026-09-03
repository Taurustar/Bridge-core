"""Needs and interaction engine (plan sections 15.1-15.4, 15.7).

Stats are clamped 0-100; thresholds/rates/effects/caps live in the owner-tuned
``needs.json`` template (bundled neutral default), never in engine logic.

- State persists in Redis under ``core:needs:{owner}`` with ``last_eval_ts``.
- ``evaluate`` advances from UTC timestamps (DST never alters elapsed time),
  bounds large gaps at ``NEEDS_MAX_ELAPSED_HOURS``, and records the
  skipped-gap diagnostics count without replaying it later.
- ``peek`` projects a snapshot without writing anything (plan section 6.4).
- Unknown future schema versions fail startup rather than corrupt state.
- Everything is inert while ``NEEDS_ENABLED`` is false: no keys, no writes.

Shutdown (critical social-battery/energy unavailability) is advisory
metadata here; the schedule/availability gate consumes ``peek`` as of
milestone 0.4.0 (plan section 15.4) without persisting anything.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from .cache import RedisCache
from .config import Config
from .constants import needs_key

log = logging.getLogger("bridge.needs")

BUNDLED_NEEDS_FILE = Path(__file__).resolve().parent.parent / "schedule" / "needs.json"

STAT_NAMES: tuple[str, ...] = (
    "energy",
    "hunger",
    "stress",
    "social_battery",
    "fun",
    "bond",
    "hurt",
)

STAT_DIRECTIONS: tuple[str, ...] = ("higher_is_better", "lower_is_better")
TURN_EFFECT_KINDS: tuple[str, ...] = ("companion_brief", "companion_engaged", "work")

DEFAULTS: dict[str, Any] = {
    "activity_multipliers": {},
    "turn_effects": {},
    "shutdown": {"enabled": False, "energy_below": 10, "social_battery_below": 10},
    "bids": {
        "open_bid_lifetime_seconds": 1209600,
        "max_open_bids": 4,
        "min_gap_between_bids_seconds": 3600,
        "reply_satisfy_bonus": 2.0,
        "reply_quality_lengths": [8, 400],
    },
    "owner_profile": {
        "soft_block_trust_threshold": 20,
        "agreement_tension_trust_floor": 50,
        "agreement_tension_closeness_floor": 40,
        "agreement_max_active": 12,
        "max_boundary_events": 50,
    },
}


class NeedsProfileError(RuntimeError):
    """The needs profile file is missing, malformed, or invalid."""


# -- loading and validation ----------------------------------------------------


def migrate_needs(data: object, from_version: int, source: str = "<needs>") -> dict:
    """Explicit schema migrations, tested from every released version.

    Version 1 is the first released schema; there is nothing to transform.
    Unknown future versions must fail startup rather than corrupt state.
    """
    if from_version == 1:
        return data  # type: ignore[return-value]
    raise NeedsProfileError(
        f"{source}: unsupported needs schema version {from_version} "
        f"(this build understands version 1 and older; upgrade first)"
    )


def validate_needs(data: object, source: str = "<needs>") -> None:
    if not isinstance(data, dict):
        raise NeedsProfileError(f"{source}: needs profile must be a JSON object")
    version = data.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise NeedsProfileError(f"{source}: requires a positive integer 'version'")
    if version > 1:
        raise NeedsProfileError(
            f"{source}: unsupported needs schema version {version} "
            f"(this build understands version 1; upgrade first)"
        )

    stats = data.get("stats")
    if not isinstance(stats, dict) or not stats:
        raise NeedsProfileError(f"{source}: 'stats' must be a non-empty object")
    for name, entry in stats.items():
        if name not in STAT_NAMES:
            raise NeedsProfileError(f"{source}: unknown stat {name!r}")
        _validate_stat(name, entry, source)

    multipliers = data.get("activity_multipliers", DEFAULTS["activity_multipliers"])
    if not isinstance(multipliers, dict) or not all(
        isinstance(value, (int, float)) and not isinstance(value, bool)
        for value in multipliers.values()
    ):
        raise NeedsProfileError(
            f"{source}: 'activity_multipliers' must map names to numbers"
        )

    effects = data.get("turn_effects", DEFAULTS["turn_effects"])
    if not isinstance(effects, dict):
        raise NeedsProfileError(f"{source}: 'turn_effects' must be an object")
    for kind, effect in effects.items():
        if kind not in TURN_EFFECT_KINDS:
            raise NeedsProfileError(
                f"{source}: unknown turn effect kind {kind!r} "
                f"(allowed: {', '.join(TURN_EFFECT_KINDS)})"
            )
        if not isinstance(effect, dict) or not all(
            isinstance(value, (int, float)) and not isinstance(value, bool)
            for value in effect.values()
        ):
            raise NeedsProfileError(
                f"{source}: turn effect {kind!r} must map stat names to numbers"
            )
        for stat in effect:
            if stat not in STAT_NAMES:
                raise NeedsProfileError(f"{source}: unknown stat {stat!r} in {kind!r}")

    shutdown = data.get("shutdown", DEFAULTS["shutdown"])
    if not isinstance(shutdown, dict):
        raise NeedsProfileError(f"{source}: 'shutdown' must be an object")
    for key in ("enabled", "energy_below", "social_battery_below"):
        if key not in shutdown:
            raise NeedsProfileError(f"{source}: 'shutdown' requires {key!r}")

    for section in ("bids", "owner_profile"):
        value = data.get(section, DEFAULTS[section])
        if not isinstance(value, dict):
            raise NeedsProfileError(f"{source}: {section!r} must be an object")


def _validate_stat(name: str, entry: object, source: str) -> None:
    if not isinstance(entry, dict):
        raise NeedsProfileError(f"{source}: stat {name!r} must be an object")
    for key in ("start", "direction", "rate_per_hour"):
        if key not in entry:
            raise NeedsProfileError(f"{source}: stat {name!r} requires {key!r}")
    if entry["direction"] not in STAT_DIRECTIONS:
        raise NeedsProfileError(
            f"{source}: stat {name!r} direction must be one of {STAT_DIRECTIONS}"
        )
    for key in ("start", "rate_per_hour"):
        value = entry[key]
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise NeedsProfileError(f"{source}: stat {name!r} {key} must be a number")
    thresholds = ("low_below", "critical_below", "low_above", "critical_above",
                  "strained_below", "deprived_below")
    for key, value in entry.items():
        if key in thresholds and (
            not isinstance(value, (int, float)) or isinstance(value, bool)
        ):
            raise NeedsProfileError(
                f"{source}: stat {name!r} {key} must be a number"
            )


def load_needs(path: str | None = None, *, content: str | None = None) -> dict:
    """Load and validate the needs profile; returns the parsed dict."""
    if content is not None:
        try:
            data = json.loads(content)
        except json.JSONDecodeError as exc:
            raise NeedsProfileError(
                f"Needs profile is not valid JSON: {exc}"
            ) from None
        validate_needs(data, source="<needs content>")
        return data
    needs_path = Path(path) if path else BUNDLED_NEEDS_FILE
    try:
        raw = needs_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise NeedsProfileError(f"Needs profile file not found: {needs_path}") from None
    return load_needs(content=raw)


# -- pure evaluation ------------------------------------------------------------


def clamp(value: float) -> float:
    return max(0.0, min(100.0, float(value)))


def zone_for(stat: str, value: float, spec: dict) -> str:
    """Zone labels describe condition, not numeric magnitude (plan 15.3)."""
    direction = spec["direction"]
    if stat == "bond":
        if value < float(spec.get("deprived_below", 15)):
            return "deprived"
        if value < float(spec.get("strained_below", 35)):
            return "strained"
        return "secure"
    if direction == "higher_is_better":
        if value < float(spec.get("critical_below", 15)):
            return "critical"
        if value < float(spec.get("low_below", 35)):
            return "low"
        return "fine"
    if value > float(spec.get("critical_above", 80)):
        return "critical"
    if value > float(spec.get("low_above", 55)):
        return "low"
    return "fine"


def _bounded_elapsed_hours(
    last_eval_ts: float, now_ts: float, max_hours: float
) -> tuple[float, bool]:
    """Elapsed hours clamped to NEEDS_MAX_ELAPSED_HOURS plus a skip flag."""
    if last_eval_ts <= 0:
        return 0.0, False
    elapsed_hours = max(0.0, (now_ts - last_eval_ts) / 3600.0)
    if elapsed_hours > max_hours:
        return float(max_hours), True
    return elapsed_hours, False


def project_needs(
    state: dict,
    spec: dict,
    now_ts: float,
    activity: str = "default",
    max_elapsed_hours: float = 48.0,
) -> dict:
    """Pure projection used by both ``peek`` (no write) and ``evaluate``."""
    stats_spec = spec["stats"]
    if not state:
        values = {
            name: clamp(stats_spec[name].get("start", 50)) for name in stats_spec
        }
        return {
            "values": values,
            "last_eval_ts": now_ts,
            "skipped_gap_count": 0,
            "activity": activity,
        }
    last_eval = float(state.get("last_eval_ts", 0) or 0)
    elapsed_hours, skipped = _bounded_elapsed_hours(last_eval, now_ts, max_elapsed_hours)
    multipliers = spec.get("activity_multipliers", {})
    multiplier = float(multipliers.get(activity, multipliers.get("default", 1.0)))
    values = dict(state.get("values", {}))
    for name, entry in stats_spec.items():
        current = clamp(values.get(name, entry.get("start", 50)))
        delta = float(entry["rate_per_hour"]) * elapsed_hours * multiplier
        values[name] = clamp(current + delta)
    return {
        "values": values,
        "last_eval_ts": now_ts,
        "skipped_gap_count": int(state.get("skipped_gap_count", 0)) + (1 if skipped else 0),
        "activity": activity,
    }


def compute_shutdown(spec: dict, values: dict) -> bool:
    shutdown = spec.get("shutdown", DEFAULTS["shutdown"])
    if not shutdown.get("enabled"):
        return False
    energy = float(values.get("energy", 100))
    social = float(values.get("social_battery", 100))
    return (
        energy < float(shutdown.get("energy_below", 10))
        and social < float(shutdown.get("social_battery_below", 10))
    )


def zones_of(spec: dict, values: dict) -> dict[str, str]:
    return {
        name: zone_for(name, float(values.get(name, 0)), spec["stats"][name])
        for name in spec["stats"]
    }


def apply_effects(
    values: dict, effect: dict, spec: dict, bounds: dict | None = None
) -> dict:
    """Deterministic turn-effect application. Caller decides gating."""
    caps = bounds or {}
    for name, delta in effect.items():
        if name not in spec["stats"]:
            continue
        key = f"max_{name}"
        if key in caps and caps[key] > 0:
            delta = min(delta, float(caps[key]))
        values[name] = clamp(float(values.get(name, 50)) + float(delta))
    return values


def classify_turn_kind(text: str) -> str:
    """Deterministic brief/engaged classification; no LLM (plan 15.5)."""
    return "companion_engaged" if len(text) > 240 else "companion_brief"


class NeedsEngine:
    """Redis-backed needs state per owner (plan sections 15.2-15.4)."""

    def __init__(self, config: Config, cache: RedisCache) -> None:
        self.config = config
        self.cache = cache
        self.spec: dict = {}

    @property
    def available(self) -> bool:
        return bool(self.config.NEEDS_ENABLED and self.spec)

    def load_spec(self) -> None:
        self.spec = load_needs(self.config.NEEDS_PROFILE_FILE.strip() or None)
        migrate_needs(self.spec, self.spec.get("version", 1))

    def key(self, owner: str) -> str:
        return needs_key(owner)

    async def read_state(self, owner: str) -> dict:
        raw = await self.cache.get_value(self.key(owner))
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("Corrupt needs state ignored for owner")
            return {}
        return data if isinstance(data, dict) else {}

    async def write_state(self, owner: str, state: dict) -> None:
        await self.cache.set_value(self.key(owner), json.dumps(state))

    async def peek(self, owner: str, activity: str = "default") -> dict:
        """Read-only projection; never persists (plan sections 6.4, 15.2)."""
        state = await self.read_state(owner)
        projection = project_needs(
            state,
            self.spec,
            time.time(),
            activity=activity,
            max_elapsed_hours=self.config.NEEDS_MAX_ELAPSED_HOURS,
        )
        return {
            "values": projection["values"],
            "zones": zones_of(self.spec, projection["values"]),
            "shutdown": compute_shutdown(self.spec, projection["values"]),
            "skipped_gap_count": projection["skipped_gap_count"],
            "last_eval_ts": projection["last_eval_ts"],
        }

    async def evaluate(self, owner: str, activity: str = "default") -> dict:
        """Advance state and persist. Caller holds the per-owner turn lock."""
        state = await self.read_state(owner)
        projection = project_needs(
            state,
            self.spec,
            time.time(),
            activity=activity,
            max_elapsed_hours=self.config.NEEDS_MAX_ELAPSED_HOURS,
        )
        await self.write_state(owner, projection)
        return {
            "values": projection["values"],
            "zones": zones_of(self.spec, projection["values"]),
            "shutdown": compute_shutdown(self.spec, projection["values"]),
            "skipped_gap_count": projection["skipped_gap_count"],
            "last_eval_ts": projection["last_eval_ts"],
        }

    async def turn_effects(self, owner: str, kind: str) -> None:
        """Apply a delivered-turn effect (plan section 12 step 29)."""
        if not self.available or kind not in TURN_EFFECT_KINDS:
            return
        effect = self.spec.get("turn_effects", {}).get(kind)
        if not effect:
            return
        state = await self.read_state(owner)
        if not state:
            state = project_needs({}, self.spec, time.time())
        state["values"] = apply_effects(
            state.get("values", {}), effect, self.spec
        )
        state.setdefault("skipped_gap_count", 0)
        await self.write_state(owner, state)

    def bid_config(self) -> dict:
        return dict(self.spec.get("bids", DEFAULTS["bids"]))

    def owner_profile_config(self) -> dict:
        return dict(self.spec.get("owner_profile", DEFAULTS["owner_profile"]))
