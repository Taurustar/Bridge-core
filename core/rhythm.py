"""Rhythm: lightweight contextual owner-availability model (plan section 15.6).

Metadata-only hourly owner-contact histograms and last-contact timestamps in
owner civil time. Rhythm may advise that the owner is probably asleep/away,
but explicit heartbeat freshness and contextual owner schedule outrank it.
It never reads device screen/app telemetry, never stores message text, and
is disabled by default.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from .cache import RedisCache
from .constants import rhythm_key

log = logging.getLogger("bridge.rhythm")

_MAX_HOURLY_BUCKETS = 48


def _hour_bucket(ts: float, timezone_name: str) -> str:
    """Owner civil-time hour key, e.g. ``2026-09-02T21`` (plan 6.5)."""
    try:
        tz = ZoneInfo(timezone_name or "UTC")
    except Exception:  # noqa: BLE001 - unknown tz falls back to UTC
        tz = ZoneInfo("UTC")
    return datetime.fromtimestamp(ts, tz).strftime("%Y-%m-%dT%H")


def record_contact(hour_bucket: str, record: dict) -> dict:
    """Pure record update: bump the hourly bucket and stamp last contact."""
    histogram = dict(record.get("hourly", {}))
    histogram[hour_bucket] = int(histogram.get(hour_bucket, 0)) + 1
    bounded = dict(sorted(histogram.items())[-_MAX_HOURLY_BUCKETS:])
    return {
        "hourly": bounded,
        "last_contact_ts": float(record.get("last_contact_ts", 0)) or time.time(),
        "last_contact_bucket": hour_bucket,
    }


class RhythmEngine:
    """Owner rhythm record under ``core:rhythm:{owner}`` (plan 15.6)."""

    def __init__(self, config: Any, cache: RedisCache) -> None:
        self.config = config
        self.cache = cache

    @property
    def available(self) -> bool:
        return bool(self.config.RHYTHM_ENABLED)

    def key(self, owner: str) -> str:
        return rhythm_key(owner)

    async def read(self, owner: str) -> dict:
        raw = await self.cache.get_value(self.key(owner))
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    async def stamp_contact(self, owner: str, timezone_name: str, now_ts: float | None = None) -> None:
        """Record one owner contact (metadata only — never message text)."""
        ts = time.time() if now_ts is None else float(now_ts)
        record = await self.read(owner)
        updated = record_contact(_hour_bucket(ts, timezone_name), record)
        await self.cache.set_value(self.key(owner), json.dumps(updated))
