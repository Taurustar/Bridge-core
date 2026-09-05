"""Dormant external-user profiles (plan section 19).

Store and admin APIs only — no gateway ships in v1 and the app companion
path never reads, updates, analyzes, or injects these records.

- Identity key: ``platform:external_id`` where ``platform`` is lowercase
  ASCII ``[a-z0-9_-]{1,24}`` and ``external_id`` is the adapter's canonical
  stable id (1-160 characters, UTF-8). Display names and aliases never
  become identity authority.
- Redis key: ``core:external_profile:{owner}:{platform}:{external_id}``
  (owner-scoped even though v1 is single-owner, per plan 19.2).
- ``EXTERNAL_USER_PROFILE_STORE_ENABLED=false`` disables the store entirely:
  admin CRUD answers ``409 feature_disabled`` and no key is ever created.
- ``EXTERNAL_USER_PROFILES_BEHAVIOR_ENABLED=false`` (default) keeps app
  behavior inert; nothing outside this module and its admin routes touches
  the records. ``EXTERNAL_USER_PROFILE_LLM_ENABLED`` gates future gateway
  analysis and gates nothing today because no gateway exists.
"""

from __future__ import annotations

import json
import logging
import re
import time

from .cache import RedisCache
from .constants import SUPPORTED_LANGUAGES, external_profile_key

log = logging.getLogger("bridge.external_profiles")

STORE_VERSION = 1

PLATFORM_PATTERN = re.compile(r"^[a-z0-9_-]{1,24}$")
EXTERNAL_ID_MAX_CHARS = 160

# Bounded-field limits. Plan 19.3 fixes types but no sizes; these bounds keep
# admin mistakes cheap and are recorded in BRIDGE_CORE_ENGINE_SPEC.md.
DISPLAY_NAME_MAX_CHARS = 120
SUMMARY_MAX_CHARS = 1000
ALIAS_MAX_ITEMS = 16
ALIAS_ITEM_MAX_CHARS = 120
LIST_FIELD_MAX_ITEMS = 16
LIST_ITEM_MAX_CHARS = 200
TONE_MAX_CHARS = 32

PATCH_STRING_FIELDS = {
    "display_name": DISPLAY_NAME_MAX_CHARS,
    "summary": SUMMARY_MAX_CHARS,
    "preferred_language": 16,
    "tone": TONE_MAX_CHARS,
}
PATCH_LIST_FIELDS = {
    "aliases": (ALIAS_MAX_ITEMS, ALIAS_ITEM_MAX_CHARS),
    "likes": (LIST_FIELD_MAX_ITEMS, LIST_ITEM_MAX_CHARS),
    "topics": (LIST_FIELD_MAX_ITEMS, LIST_ITEM_MAX_CHARS),
    "boundaries_seen": (LIST_FIELD_MAX_ITEMS, LIST_ITEM_MAX_CHARS),
    "observations": (LIST_FIELD_MAX_ITEMS, LIST_ITEM_MAX_CHARS),
}
PATCH_INT_FIELDS = ("familiarity", "trust")

# Fields an admin may patch. Identity (subject_id/platform), timestamps, and
# version are engine-owned.
PATCH_FIELDS = frozenset(PATCH_STRING_FIELDS) | frozenset(PATCH_LIST_FIELDS) | frozenset(PATCH_INT_FIELDS)


class ExternalProfileError(ValueError):
    """Invalid platform/external id or patch body."""


def validate_platform(raw: object) -> str:
    """Canonicalize the platform segment (plan 19.2): lowercase ASCII."""
    if not isinstance(raw, str):
        raise ExternalProfileError("'platform' must be a string")
    platform = raw.strip().lower()
    if not PLATFORM_PATTERN.match(platform):
        raise ExternalProfileError(
            "'platform' must match [a-z0-9_-]{1,24} after lowercasing "
            f"(got {raw!r})"
        )
    return platform


def validate_external_id(raw: object) -> str:
    """Validate the adapter's canonical stable id (plan 19.2).

    The id is stored literally; HTTP paths carry it URL-encoded. Slashes are
    rejected because the ASGI router splits on raw path segments.
    """
    if not isinstance(raw, str):
        raise ExternalProfileError("'external_id' must be a string")
    external_id = raw
    if not external_id or len(external_id) > EXTERNAL_ID_MAX_CHARS:
        raise ExternalProfileError(
            f"'external_id' must be 1-{EXTERNAL_ID_MAX_CHARS} characters"
        )
    if "/" in external_id or any(ord(ch) < 0x20 for ch in external_id):
        raise ExternalProfileError(
            "'external_id' must not contain slashes or control characters"
        )
    return external_id


def subject_id(platform: str, external_id: str) -> str:
    return f"{platform}:{external_id}"


def default_profile(platform: str, external_id: str) -> dict:
    """Plan 19.3 schema with engine-owned identity fields filled."""
    now_ts = time.time()
    return {
        "subject_id": subject_id(platform, external_id),
        "platform": platform,
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
        "created_ts": now_ts,
        "updated_ts": now_ts,
        "version": STORE_VERSION,
    }


def validate_patch(patch: object) -> dict:
    """Validate a partial PATCH body; unknown fields are errors (plan 29).

    Returns the normalized updates; identity fields are engine-owned and
    never patchable.
    """
    if not isinstance(patch, dict) or not patch:
        raise ExternalProfileError("PATCH body must be a non-empty JSON object")
    updates: dict = {}
    for key in patch:
        if key not in PATCH_FIELDS and key != "version":
            raise ExternalProfileError(f"Unknown field {key!r}")
    for field_name, max_chars in PATCH_STRING_FIELDS.items():
        if field_name not in patch:
            continue
        value = patch[field_name]
        if not isinstance(value, str):
            raise ExternalProfileError(f"{field_name!r} must be a string")
        if len(value) > max_chars:
            raise ExternalProfileError(
                f"{field_name!r} must be at most {max_chars} characters"
            )
        if field_name == "preferred_language":
            language = value.strip().lower()
            if language and language not in SUPPORTED_LANGUAGES:
                raise ExternalProfileError(
                    "'preferred_language' must be blank or one of "
                    f"{', '.join(SUPPORTED_LANGUAGES)}"
                )
            updates[field_name] = language
        elif field_name == "tone":
            tone = value.strip()
            updates[field_name] = tone or "neutral"
        else:
            updates[field_name] = value
    for field_name, (max_items, item_max_chars) in PATCH_LIST_FIELDS.items():
        if field_name not in patch:
            continue
        value = patch[field_name]
        if not isinstance(value, list) or len(value) > max_items:
            raise ExternalProfileError(
                f"{field_name!r} must be a list of at most {max_items} strings"
            )
        if any(
            not isinstance(item, str)
            or not item.strip()
            or len(item) > item_max_chars
            for item in value
        ):
            raise ExternalProfileError(
                f"{field_name!r} items must be non-empty strings of at most "
                f"{item_max_chars} characters"
            )
        updates[field_name] = [item.strip() for item in value]
    for field_name in PATCH_INT_FIELDS:
        if field_name not in patch:
            continue
        value = patch[field_name]
        if not isinstance(value, int) or isinstance(value, bool):
            raise ExternalProfileError(f"{field_name!r} must be an integer")
        if not 0 <= value <= 100:
            raise ExternalProfileError(f"{field_name!r} must be between 0 and 100")
        updates[field_name] = value
    if "version" in patch:
        version = patch["version"]
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            raise ExternalProfileError("'version' must be a positive integer")
        updates["version"] = version
    return updates


class ExternalProfileStore:
    """Owner-scoped dormant profile documents (plan section 19)."""

    def __init__(self, config, cache: RedisCache) -> None:
        self.config = config
        self.cache = cache

    @property
    def available(self) -> bool:
        return bool(self.config.EXTERNAL_USER_PROFILE_STORE_ENABLED)

    def key(self, owner: str, platform: str, external_id: str) -> str:
        return external_profile_key(owner, platform, external_id)

    async def get(self, owner: str, platform: str, external_id: str) -> dict | None:
        raw = await self.cache.get_value(self.key(owner, platform, external_id))
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("Corrupt external profile ignored")
            return None
        return data if isinstance(data, dict) else None

    async def put(self, owner: str, platform: str, external_id: str, profile: dict) -> dict:
        await self.cache.set_value(
            self.key(owner, platform, external_id), json.dumps(profile)
        )
        return profile

    async def delete(self, owner: str, platform: str, external_id: str) -> bool:
        key = self.key(owner, platform, external_id)
        existed = await self.cache.get_value(key) is not None
        await self.cache.delete(key)
        return existed

    async def list_profiles(self, owner: str) -> list[dict]:
        """Every stored profile for the owner; deterministic sort belongs to
        the route layer (updated_ts desc, subject_id asc per plan 29)."""
        profiles: list[dict] = []
        for key in await self.cache.keys(f"core:external_profile:{owner}:*"):
            raw = await self.cache.get_value(key)
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                log.warning("Corrupt external profile ignored during list")
                continue
            if isinstance(data, dict):
                profiles.append(data)
        return profiles
