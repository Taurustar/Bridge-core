"""Bridge Core Engine 0.1.0 — entrypoint.

A self-hosted backend for a persistent character companion. Single-owner,
Tailscale-only, no application auth (plan sections 2, 27).

Run:  python bridge_core.py        (loads core.env automatically)
"""

from __future__ import annotations

import uvicorn

from core.app import create_app
from core.config import Config
from core.constants import VERSION


def main() -> None:
    config = Config.from_env()
    app = create_app(config)
    uvicorn.run(app, host=config.BRIDGE_HOST, port=config.BRIDGE_PORT, log_level=config.LOG_LEVEL)


if __name__ == "__main__":
    main()

__version__ = VERSION
