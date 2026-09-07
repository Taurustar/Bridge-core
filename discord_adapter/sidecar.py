"""Lifespan hooks. core/ discovers sibling sidecar.py without naming this package."""

from __future__ import annotations

import logging

from .adapter import Adapter
from .config import load

log = logging.getLogger("bridge.discord")

_adapter: Adapter | None = None


async def start(app) -> None:
    global _adapter
    config = load()
    if config is None or not config.DISCORD_ENABLED:
        return
    adapter = Adapter(config)
    await adapter.start(app.state.bridge)
    _adapter = adapter


async def stop(app) -> None:
    global _adapter
    adapter = _adapter
    _adapter = None
    if adapter is not None:
        await adapter.stop()
