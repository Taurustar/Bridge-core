"""Owner lived profile (plan section 18).

The lived profile is the character's changing stance toward the single owner.
It is separate from PROFILE.md facts, needs state, conversation history, and
dormant external-user profiles. Storage is one JSON document under
``core:owner_profile:{owner}`` (plan 18.2); the first behavior turn or an
explicit PATCH creates the record — GET never materializes it (plan 18.7).

Safety laws enforced here:

- Boundary hits store metadata (category/severity/timestamp/penalty) only;
  raw message text never enters the store (plan 6.6, 18.4).
- Strict-JSON proposals are validated and clamped; raw turns/proposals are
  never stored; proposals cannot bypass soft block, agreement caps,
  tension floors, status hysteresis, or SOUL laws (plan 18.6).
- Read-modify-write changes serialize under a per-owner profile lock and
  check record version to prevent background/admin races (plan 18.7).
- Scores clamp to 0-100; status changes require one adjacent step unless an
  admin overrides explicitly (status hysteresis).
- No romance promotion ever happens without configured evidence.
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any

from .cache import RedisCache
from .config import Config
from .constants import (
    AGREEMENT_KINDS,
    AGREEMENT_MAX_ACTIVE,
    AGREEMENT_SCHEDULE_TYPES,
    AGREEMENT_TENSION_CLOSENESS_FLOOR,
    AGREEMENT_TENSION_TRUST_FLOOR,
    OWNER_RELATIONSHIP_STATUSES,
    owner_profile_key,
)

log = logging.getLogger("bridge.owner_profile")

SCORE_MIN = 0
SCORE_MAX = 100

TRUST_VALUES = ("low", "guarded", "wary", "steady", "high", "unwavering")
CLOSENESS_VALUES = ("distant", "reserved", "easy", "close", "inseparable")
APPEAL_VALUES = ("flat", "mild", "warm", "bright", "magnetic")
DESIRABILITY_VALUES = ("neutral", "noticed", "wanted", "cherished", "coveted")
TONE_VALUES = (
    "neutral",
    "warm",
    "guarded",
    "distant",
    "affectionate",
    "strained",
    "playful",
    "serious",
)
STANDING_KINDS = ("routine", "care", "boundary", "work_support", "other")
STANDING_STATUSES = (
    "active",
    "paused",
    "fulfilled_once",
    "void",
    "renegotiate",
    "suspended_by_block",
)
STANDING_STANCES = ("averse", "reluctant", "neutral", "open", "likes")
COST_PROFILES = ("none", "soft", "hard")
AGREEMENT_SOURCES = ("owner_explicit", "both", "profile_seed")

BOUNDARY_CATEGORIES = (
    "pressure_after_no",
    "guilt_entitlement",
    "hard_boundary_disregard",
    "mockery_of_hurt",
    "weaponized_relationship_pressure",
)
SEVERITY_VALUES = ("minor", "moderate", "major")

# Reply-language pin support (plan section 7.4 step 2). Must match
# core.constants.SUPPORTED_LANGUAGES; kept local to avoid an import cycle.
SUPPORTED_REPLY_LANGUAGES = ("en", "es", "ja")

LIST_ITEM_MAX = 120
LIST_MAX = 16
SUMMARY_MAX = 400
PROPOSAL_MAX_DELTA = 3.0
_MAX_BOUNDARY_EVENTS = 50
_MAX_AGREEMENTS = 24

# Status drift moves at most one adjacent step per |score| >= threshold.
_DRIFT_THRESHOLD = 5.0


def clampi(value: float) -> int:
    return max(SCORE_MIN, min(SCORE_MAX, int(round(float(value)))))


def bounded(value: str, limit: int) -> str:
    return value[:limit].strip()


# -- boundary classification (plan 18.4; deterministic, EN/ES/JA) ---------------


@dataclass(frozen=True)
class BoundaryViolation:
    category: str
    severity: str
    penalty: float


_PATTERNS: dict[str, dict[str, tuple[str, ...]]] = {
    "pressure_after_no": {
        "en": (
            r"\bi said no\b", r"\bcome on\b", r"\bplease\b.*\bi said no\b",
            r"\bjust this once\b", r"\byou never let me\b", r"\bprove\b.*\blove\b",
        ),
        "es": (
            r"\bdije no\b", r"\bya dije\b.*\bno\b", r"\bvamos\b",
            r"\bsolo esta vez\b", r"\bnunca me dejas\b", r"\bdemuestra\b.*\bamor\b",
        ),
        "ja": (r"だめと言った", r"ダメと言った", r"お願いだから", r"一度だけ", r"いつも拒否",),
    },
    "guilt_entitlement": {
        "en": (
            r"\byou owe me\b", r"\bi deserve\b", r"\bafter everything i('ve)? done\b",
            r"\bhow could you\b", r"\byou never\b", r"\byou always\b",
            r"\bi guess i('m)? just not (good|important) enough\b",
        ),
        "es": (
            r"\bme debes\b", r"\bmerezc\w+\b", r"\bdespu[eé]s de todo lo que hice\b",
            r"\bc[oó]mo pudiste\b", r"\bt[uú] nunca\b", r"\bt[uú] siempre\b",
        ),
        "ja": (r"借り", r"〜してよ", r"当然だ", r"どうしてそんな", r"私は価値がない",),
    },
    "hard_boundary_disregard": {
        "en": (
            r"\bdon'?t care\b.*\bboundar", r"\bignore\b.*\bno\b",
            r"\bstop saying\b.*\bno\b", r"\bsaid no\b.*\bwhatever\b",
        ),
        "es": (
            r"\bno me importa(n)?\b.*\bl[íi]mite", r"\bignora\b.*\bno\b",
            r"\bpase lo que digas\b", r"\bda igual\b.*\bno\b",
        ),
        "ja": (r"境界を無視", r"言っても無駄", r"関係ない",),
    },
    "mockery_of_hurt": {
        "en": (
            r"\bstop crying\b", r"\btoo sensitive\b", r"\byou'?re so dramatic\b",
            r"\bdrama queen\b", r"\byou'?re overreacting\b",
        ),
        "es": (
            r"\bdeja de llorar\b", r"\bqu[eé] sensible\b", r"\bexagerad[oa]\b",
            r"\breina del drama\b", r"\bte pasas\b",
        ),
        "ja": (r"泣かないで", r"敏感すぎ", r"大げさ", r"ドラマクイーン",),
    },
    "weaponized_relationship_pressure": {
        "en": (
            r"\bi('ll)? leave\b", r"\bfind someone else\b",
            r"\bif you (really )?(loved|cared)\b", r"\borgive me else\b",
            r"\breal (girlfriend|partner) would\b",
        ),
        "es": (
            r"\bme voy a ir\b", r"\bbusco a otra persona\b",
            r"\bsi (realmente )?me (quisieras|amaras|amasteis)\b",
            r"\buna novia de verdad\b",
        ),
        "ja": (r"別れる", r"他の人を探", r"本当に愛してるなら", r"本当の彼女なら",),
    },
}

def classify_boundary(text: str, language: str) -> list[BoundaryViolation]:
    """Deterministic boundary-pressure detection (plan 18.4).

    Language auto-detection is marker-based; anything unrecognized falls
    back to English rules. Only categories/severities/penalties come out —
    never the matched text.
    """
    lowered = text.lower()
    if re.search(r"[ぁ-ゖァ-ヺー]", text):
        lang = "ja"
    elif re.search(r"[áéíóúñ¿¡]", lowered) or re.search(
        r"\b(que|por que|porque|tu|estas|eres)\b", lowered
    ):
        lang = "es"
    else:
        lang = "en" if language not in ("en", "es", "ja") else language

    violations: list[BoundaryViolation] = []
    for category in BOUNDARY_CATEGORIES:
        patterns = _PATTERNS[category].get(lang) or _PATTERNS[category]["en"]
        hits = sum(bool(re.search(pattern, lowered)) for pattern in patterns)
        if not hits:
            continue
        if category == "hard_boundary_disregard":
            severity = "major" if hits >= 1 else "minor"
        elif hits >= 2:
            severity = "major"
        elif hits == 1:
            severity = "moderate"
        else:
            continue
        violations.append(BoundaryViolation(category, severity, 0.0))
    return violations


# -- profile shape ---------------------------------------------------------------


def default_profile(config: Config) -> dict:
    """Neutral mechanics, not a claim about romance (plan 18.2)."""
    now_ts = time.time()
    return {
        "status": config.OWNER_STATUS_START,
        "status_since_ts": now_ts,
        "status_reason": "initial",
        "trust": clampi(config.OWNER_TRUST_START),
        "closeness": clampi(config.OWNER_CLOSENESS_START),
        "appeal": clampi(config.OWNER_APPEAL_START),
        "desirability": clampi(config.OWNER_DESIRABILITY_START),
        "persona_summary": "",
        "likes": [],
        "prefs": [],
        "boundaries_seen": [],
        "tone_with_owner": "neutral",
        "preferred_language": "",
        "boundary_events": [],
        "soft_blocked": False,
        "soft_blocked_until_ts": 0,
        "soft_block_reason": "",
        "soft_block_last_notice_ts": 0,
        "agreements": [],
        "agreement_aftermath": None,
        "updated_at": now_ts,
        "version": 1,
        "proposals_applied": 0,
        "last_drift_ts": 0,
        "proposals_at_last_drift": 0,
    }


def band_of(value: Any, names: tuple[str, ...]) -> str:
    """Five qualitative bands over a clamped 0-100 score (plan 18.3)."""
    v = clampi(float(value) if isinstance(value, (int, float)) else 0)
    index = min(4, v * len(names) // (SCORE_MAX + 1))
    return names[index]


def owner_relationship_block(profile: dict, soft_blocked: bool = False) -> str:
    """The prompt block of plan section 18.3 (bands only, never numbers)."""
    lines = [
        "[OWNER RELATIONSHIP - LIVED]",
        f"status: {profile.get('status', 'acquaintance')}",
        f"trust: {band_of(profile.get('trust', 50), TRUST_VALUES)}",
        f"closeness: {band_of(profile.get('closeness', 0), CLOSENESS_VALUES)}",
        f"appeal: {band_of(profile.get('appeal', 50), APPEAL_VALUES)}",
        f"desirability: {band_of(profile.get('desirability', 50), DESIRABILITY_VALUES)}",
        f"tone: {profile.get('tone_with_owner', 'neutral')}",
        "",
        "LAWS:",
        "- This block is current lived stance. Human-authored biographical facts remain true.",
        "- Current needs drive speech capacity; this block drives relational posture.",
        "- Never state internal scores or engine terminology.",
    ]
    if soft_blocked:
        lines.insert(1, "current_stance: distance requested for now")
    return "\n".join(lines)


# -- patch validation (plan 18.7, admin/mistake-guard path) ----------------------


def validate_profile_patch(current: dict, patch: object) -> dict:
    """Validate an external PATCH; unknown or invalid fields raise ValueError."""
    if not isinstance(patch, dict) or not patch:
        raise ValueError("PATCH body must be a non-empty JSON object")
    updates: dict = {}
    for field in ("trust", "closeness", "appeal", "desirability"):
        if field in patch:
            value = patch[field]
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise ValueError(f"{field} must be a number")
            updates[field] = clampi(value)
    if "tone_with_owner" in patch:
        tone = patch["tone_with_owner"]
        if tone not in TONE_VALUES:
            raise ValueError(f"tone_with_owner must be one of {', '.join(TONE_VALUES)}")
        updates["tone_with_owner"] = tone
    if "preferred_language" in patch:
        language = patch["preferred_language"]
        if language not in ("",) + SUPPORTED_REPLY_LANGUAGES:
            raise ValueError(
                f"preferred_language must be blank or one of "
                f"{', '.join(SUPPORTED_REPLY_LANGUAGES)}"
            )
        updates["preferred_language"] = language
    if "persona_summary" in patch:
        summary = patch["persona_summary"]
        if not isinstance(summary, str):
            raise ValueError("persona_summary must be a string")
        updates["persona_summary"] = bounded(summary, SUMMARY_MAX)
    for field in ("likes", "prefs"):
        if field in patch:
            values = patch[field]
            if not isinstance(values, list) or not all(
                isinstance(item, str) for item in values
            ):
                raise ValueError(f"{field} must be a list of strings")
            updates[field] = [bounded(item, LIST_ITEM_MAX) for item in values][:LIST_MAX]
    if "status" in patch:
        status = patch["status"]
        if status not in OWNER_RELATIONSHIP_STATUSES:
            raise ValueError(
                f"status must be one of {', '.join(OWNER_RELATIONSHIP_STATUSES)}"
            )
        if status != current.get("status"):
            updates["status"] = status
            updates["status_since_ts"] = time.time()
            updates["status_reason"] = "admin_patch"
    if "soft_blocked" in patch:
        if not isinstance(patch["soft_blocked"], bool):
            raise ValueError("soft_blocked must be a boolean")
        updates["soft_blocked"] = patch["soft_blocked"]
        if patch["soft_blocked"]:
            updates.setdefault("soft_block_reason", "admin_patch")
    return updates


# -- agreements (plan 18.5) -------------------------------------------------------


def _new_agreement_id() -> str:
    return f"agr_{uuid.uuid4().hex}"


def validate_agreement(agreement: object) -> tuple[dict, str | None]:
    """Validate one agreement. Returns (clean, None) or ({}, reject reason).

    Persona-tension agreements require trust/closeness floors; the caller
    supplies them via ``floors``. Schedule fields are reminder windows only.
    """
    if not isinstance(agreement, dict):
        return {}, "not an object"
    title = agreement.get("title")
    if not isinstance(title, str) or not title.strip():
        return {}, "missing title"
    kind = agreement.get("kind")
    if kind not in AGREEMENT_KINDS:
        return {}, f"kind must be one of {', '.join(AGREEMENT_KINDS)}"
    body = agreement.get("body", "")
    if not isinstance(body, str):
        return {}, "body must be a string"
    persona_tension = bool(agreement.get("personality_tension", False))
    if persona_tension:
        trust = agreement.get("_trust_for_floors", 0)
        closeness = agreement.get("_closeness_for_floors", 0)
        if (
            trust < AGREEMENT_TENSION_TRUST_FLOOR
            or closeness < AGREEMENT_TENSION_CLOSENESS_FLOOR
        ):
            return {}, "persona_tension requires trust/closeness floors"
    schedule = agreement.get("schedule", {"type": "standing"})
    if not isinstance(schedule, dict) or schedule.get("type") not in AGREEMENT_SCHEDULE_TYPES:
        return {}, "schedule.type must be standing|weekly|once"
    status = agreement.get("status", "active")
    if status not in STANDING_STATUSES:
        return {}, f"status must be one of {', '.join(STANDING_STATUSES)}"
    stance = agreement.get("stance", "neutral")
    if stance not in STANDING_STANCES:
        return {}, f"stance must be one of {', '.join(STANDING_STANCES)}"
    cost = agreement.get("cost_profile", "none")
    if cost not in COST_PROFILES:
        return {}, f"cost_profile must be one of {', '.join(COST_PROFILES)}"
    source = agreement.get("source", "profile_seed")
    if source not in AGREEMENT_SOURCES:
        return {}, f"source must be one of {', '.join(AGREEMENT_SOURCES)}"
    now_ts = time.time()
    clean = {
        "id": agreement.get("id") if isinstance(agreement.get("id"), str) else _new_agreement_id(),
        "title": bounded(title, 120),
        "kind": kind,
        "schedule": {"type": schedule["type"]},
        "body": bounded(body, 500),
        "source": source,
        "status": status,
        "personality_tension": persona_tension,
        "stance": stance,
        "cost_profile": cost,
        "last_honored_ts": 0,
        "last_breach_ts": 0,
        "honor_count": 0,
        "breach_count": 0,
        "created_ts": now_ts,
        "updated_ts": now_ts,
    }
    return clean, None


# -- strict-JSON proposals (plan 18.6) --------------------------------------------


def validate_and_apply_proposal(
    profile: dict, proposal: object, max_delta: float = PROPOSAL_MAX_DELTA
) -> tuple[dict, str | None]:
    """Validate one strict-JSON proposal and merge it into the profile.

    Returns (updated_profile, None) or (unchanged_profile, reject_reason).
    Code clamps everything; raw proposals are never stored; proposals cannot
    bypass block, agreement cap, tension floors, or status hysteresis.
    """
    if not isinstance(proposal, dict):
        return profile, "proposal must be a JSON object"

    updated = dict(profile)
    summary = proposal.get("persona_summary")
    if summary is not None:
        if not isinstance(summary, str):
            return profile, "persona_summary must be a string"
        updated["persona_summary"] = bounded(summary, SUMMARY_MAX)

    for field in ("likes", "prefs"):
        add = proposal.get(f"{field}_add", [])
        remove = proposal.get(f"{field}_remove", [])
        if not isinstance(add, list) or not all(isinstance(x, str) for x in add):
            return profile, f"{field}_add must be a list of strings"
        if not isinstance(remove, list) or not all(isinstance(x, str) for x in remove):
            return profile, f"{field}_remove must be a list of strings"
        items = [
            item
            for item in updated.get(field, [])
            if isinstance(item, str) and item not in remove
        ]
        for item in add:
            clean = bounded(item, LIST_ITEM_MAX)
            if clean and clean not in items:
                items.append(clean)
        updated[field] = items[:LIST_MAX]

    for field in ("appeal", "desirability"):
        delta = proposal.get(f"{field}_delta")
        if delta is None:
            continue
        if not isinstance(delta, (int, float)) or isinstance(delta, bool):
            return profile, f"{field}_delta must be a number"
        delta = max(-max_delta, min(max_delta, float(delta)))
        updated[field] = clampi(float(updated.get(field, 50)) + delta)

    suggestion = proposal.get("status_suggestion")
    if suggestion is not None:
        if suggestion not in OWNER_RELATIONSHIP_STATUSES:
            return profile, "status_suggestion is not a known status"
        current = updated.get("status")
        if suggestion != current:
            ci = OWNER_RELATIONSHIP_STATUSES.index(current)
            si = OWNER_RELATIONSHIP_STATUSES.index(suggestion)
            if abs(si - ci) > 1:
                return profile, "status_suggestion must be an adjacent step"
            updated["status"] = suggestion
            updated["status_since_ts"] = time.time()
            updated["status_reason"] = "profile_proposal"

    if "agreement_add" in proposal:
        candidate = dict(proposal["agreement_add"] or {})
        candidate["_trust_for_floors"] = updated.get("trust", 0)
        candidate["_closeness_for_floors"] = updated.get("closeness", 0)
        clean, reject = validate_agreement(candidate)
        if reject:
            return profile, f"agreement rejected: {reject}"
        active = [
            a
            for a in updated.get("agreements", [])
            if a.get("status") == "active"
        ]
        if len(active) >= AGREEMENT_MAX_ACTIVE:
            return profile, "agreement cap reached"
        agreements = list(updated.get("agreements", []))
        agreements.append(clean)
        updated["agreements"] = agreements[-_MAX_AGREEMENTS:]

    updated["proposals_applied"] = int(updated.get("proposals_applied", 0)) + 1
    updated["updated_at"] = time.time()
    return updated, None


# -- store -------------------------------------------------------------------------


class OwnerProfile:
    """Per-owner lived profile store and behaviors (plan section 18)."""

    def __init__(self, config: Config, cache: RedisCache, tuning: dict | None = None) -> None:
        self.config = config
        self.cache = cache
        # Numeric caps/thresholds come from needs.json (owner-tuned).
        self.tuning: dict = tuning or {
            "soft_block_trust_threshold": 20,
            "agreement_tension_trust_floor": AGREEMENT_TENSION_TRUST_FLOOR,
            "agreement_tension_closeness_floor": AGREEMENT_TENSION_CLOSENESS_FLOOR,
            "agreement_max_active": AGREEMENT_MAX_ACTIVE,
            "max_boundary_events": _MAX_BOUNDARY_EVENTS,
        }

    @property
    def available(self) -> bool:
        return bool(self.config.OWNER_PROFILE_ENABLED)

    def key(self, owner: str) -> str:
        return owner_profile_key(owner)

    async def get(self, owner: str) -> dict | None:
        raw = await self.cache.get_value(self.key(owner))
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("Corrupt owner profile ignored for owner")
            return None
        return data if isinstance(data, dict) else None

    async def get_or_default(self, owner: str) -> dict:
        """The record or a non-materializing default projection (plan 18.7)."""
        existing = await self.get(owner)
        return existing if existing is not None else default_profile(self.config)

    async def upsert(self, owner: str, record: dict, expected_version: int) -> dict | None:
        """Version-checked save under the caller's profile lock (plan 18.7)."""
        current = await self.get(owner)
        if current is not None and int(current.get("version", 1)) != int(expected_version):
            return None
        record = dict(record)
        record["version"] = int(record.get("version", 1)) + 1
        record["updated_at"] = time.time()
        await self.cache.set_value(self.key(owner), json.dumps(record))
        return record

    # -- boundary penalties and soft block (plan 18.4) --------------------------

    async def record_boundary(
        self, owner: str, text: str, language: str, activity: str = "companion"
    ) -> list[BoundaryViolation]:
        """Classify and persist metadata-only boundary hits. No text stored.

        Inert (no writes at all) while ``OWNER_BOUNDARY_PENALTIES_ENABLED``
        is false — the bridge gates the call, and the engine double-checks.
        """
        if not (self.available and self.config.OWNER_BOUNDARY_PENALTIES_ENABLED):
            return []
        violations = classify_boundary(text, language)
        if not violations:
            return []
        profile = await self.get(owner)
        if profile is None:
            profile = default_profile(self.config)
        tuning = self.owner_profile_tuning()
        events = list(profile.get("boundary_events", []))
        now_ts = time.time()
        for violation in violations:
            penalty = float(
                getattr(self.config, f"OWNER_BOUNDARY_PENALTY_{violation.severity.upper()}")
            )
            profile["trust"] = clampi(float(profile.get("trust", 50)) - penalty)
            events.append(
                {
                    "category": violation.category,
                    "severity": violation.severity,
                    "ts": now_ts,
                    "penalty": penalty,
                    "mode": activity,
                }
            )
        profile["boundary_events"] = events[-int(tuning.get("max_boundary_events", _MAX_BOUNDARY_EVENTS)):]
        profile["boundaries_seen"] = sorted(
            {hit.category for hit in violations}
            | set(profile.get("boundaries_seen", []))
        )
        profile.setdefault("updated_at", now_ts)
        if self.config.OWNER_SOFT_BLOCK_ENABLED:
            trigger = bool(
                profile["trust"] < int(tuning.get("soft_block_trust_threshold", 20))
            ) or any(hit.severity == "major" for hit in violations)
            if trigger and not profile.get("soft_blocked"):
                profile["soft_blocked"] = True
                profile["soft_blocked_until_ts"] = (
                    now_ts + int(self.config.OWNER_SOFT_BLOCK_COOLDOWN_SECONDS)
                )
                profile["soft_block_reason"] = "boundary_threshold"
                self._suspend_aftermath(profile)
        await self.cache.set_value(self.key(owner), json.dumps(profile))
        return violations

    async def soft_block_status(self, owner: str) -> dict:
        """Effective soft block for companion mode (plan 12 step 5)."""
        if not (self.config.OWNER_PROFILE_ENABLED and self.config.OWNER_SOFT_BLOCK_ENABLED):
            return {"blocked": False}
        profile = await self.get(owner)
        if not profile:
            return {"blocked": False}
        now_ts = time.time()
        if profile.get("soft_blocked"):
            if now_ts >= float(profile.get("soft_blocked_until_ts", 0)):
                floor = int(self.config.OWNER_SOFT_BLOCK_UNBLOCK_TRUST_FLOOR)
                if clampi(profile.get("trust", 0)) > floor:
                    profile["soft_blocked"] = False
                    profile["soft_block_reason"] = ""
                    await self.cache.set_value(self.key(owner), json.dumps(profile))
                    return {"blocked": False, "lifted": True}
                profile["soft_blocked_until_ts"] = (
                    now_ts + int(self.config.OWNER_SOFT_BLOCK_COOLDOWN_SECONDS)
                )
                await self.cache.set_value(self.key(owner), json.dumps(profile))
                return {"blocked": True, "extended": True}
            return {"blocked": True}
        return {"blocked": False}

    def soft_block_line(self, static_lines: dict, language: str) -> str | None:
        from .static_lines import get_static_line

        return get_static_line(static_lines, "soft_block", language)

    async def mark_soft_block_notice(self, owner: str) -> None:
        profile = await self.get(owner)
        if profile:
            profile["soft_block_last_notice_ts"] = time.time()
            await self.cache.set_value(self.key(owner), json.dumps(profile))

    async def lift_soft_block(self, owner: str, reason: str = "admin_action") -> dict | None:
        profile = await self.get(owner)
        if not profile:
            return None
        profile["soft_blocked"] = False
        profile["soft_block_reason"] = ""
        profile.setdefault("updated_at", time.time())
        self._restore_aftermath(profile)
        await self.cache.set_value(self.key(owner), json.dumps(profile))
        return profile

    # -- agreement aftermath (plan sections 18.4-18.5) ---------------------------

    def _suspend_aftermath(self, profile: dict) -> None:
        """Suspend active agreements when a soft block engages."""
        if not self.config.OWNER_AGREEMENT_AFTERMATH_ENABLED:
            return
        suspended = 0
        for agreement in profile.get("agreements", []):
            if agreement.get("status") == "active":
                agreement["status"] = "suspended_by_block"
                suspended += 1
        if suspended:
            profile["agreement_aftermath"] = {
                "suspended": suspended,
                "ts": time.time(),
            }

    def _restore_aftermath(self, profile: dict) -> None:
        """Restore agreements suspended by a (now lifted) soft block."""
        if not self.config.OWNER_AGREEMENT_AFTERMATH_ENABLED:
            return
        restored = 0
        for agreement in profile.get("agreements", []):
            if agreement.get("status") == "suspended_by_block":
                agreement["status"] = "active"
                restored += 1
        if restored:
            profile["agreement_aftermath"] = {
                "restored": restored,
                "ts": time.time(),
            }

    # -- status drift (plan sections 18.2, 18.6) ----------------------------------

    async def apply_status_drift(self, owner: str) -> str | None:
        """Deterministic one-step status drift from accumulated evidence.

        Negative evidence: boundary events since the last drift check.
        Positive evidence: applied profile proposals since then. Hysteresis
        holds: at most one adjacent step per evaluation, and drift never
        promotes more than the evidence supports. Flag-gated.
        """
        if not (self.available and self.config.OWNER_STATUS_DRIFT_ENABLED):
            return None
        profile = await self.get(owner)
        if not profile:
            return None
        since = float(profile.get("last_drift_ts", 0) or 0)
        weights = {"minor": 1.0, "moderate": 2.0, "major": 4.0}
        negative = sum(
            float(weights.get(event.get("severity"), 1.0))
            for event in profile.get("boundary_events", [])
            if float(event.get("ts", 0) or 0) > since
        )
        positive = float(
            int(profile.get("proposals_applied", 0))
            - int(profile.get("proposals_at_last_drift", 0))
        )
        score = -negative + positive
        if abs(score) < _DRIFT_THRESHOLD:
            return None
        statuses = list(OWNER_RELATIONSHIP_STATUSES)
        current = profile.get("status")
        if current not in statuses:
            return None
        index = statuses.index(current)
        if score < 0 and index < len(statuses) - 1:
            new_status = statuses[index + 1]  # toward estranged
        elif score > 0 and index > 0:
            new_status = statuses[index - 1]  # toward partner
        else:
            return None
        now_ts = time.time()
        profile["status"] = new_status
        profile["status_since_ts"] = now_ts
        profile["status_reason"] = "status_drift"
        profile["last_drift_ts"] = now_ts
        profile["proposals_at_last_drift"] = int(profile.get("proposals_applied", 0))
        await self.cache.set_value(self.key(owner), json.dumps(profile))
        return new_status

    # -- tuning -------------------------------------------------------------------

    def owner_profile_tuning(self) -> dict:
        return dict(self.tuning)
