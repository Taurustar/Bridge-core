"""Tailscale bind validation (plan section 27.2).

Production startup with ``TAILSCALE_REQUIRED=true`` must fail unless one of:

1. ``BRIDGE_HOST`` is loopback (local development default), or
2. ``BRIDGE_HOST`` is a local address assigned to ``tailscale0``, or
3. ``TAILSCALE_FIREWALL_ACK=true`` (operator verified a firewall rule).

The interface address enumerator is injectable for tests.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import shutil

from .config import Config

log = logging.getLogger("bridge.tailscale")

_INET_RE = re.compile(r"inet6?\s+([0-9a-fA-F:.%]+)")


class TailscaleValidationError(RuntimeError):
    """Raised when the configured bind violates the Tailscale requirement."""


async def enumerate_tailscale_addresses(interface: str = "tailscale0") -> set[str]:
    """Best-effort stdlib enumeration of addresses assigned to tailscale0.

    Linux: ``ip -o addr show dev tailscale0``. macOS: ``ifconfig tailscale0``
    (utun interfaces vary; absence simply yields an empty set).
    """
    if shutil.which("ip"):
        cmd = ["ip", "-o", "addr", "show", "dev", interface]
    elif shutil.which("ifconfig"):
        cmd = ["ifconfig", interface]
    else:
        return set()
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
    except (OSError, asyncio.TimeoutError):
        return set()
    if proc.returncode != 0:
        return set()
    addresses: set[str] = set()
    for match in _INET_RE.finditer(stdout.decode("utf-8", "replace")):
        address = match.group(1).split("%")[0]
        try:
            addresses.add(str(ipaddress.ip_address(address)))
        except ValueError:
            continue
    return addresses


def _is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host.strip().lower() == "localhost"


async def validate_bind(
    config: Config,
    tailscale_addresses: set[str] | None = None,
) -> str:
    """Validate the bind against the Tailscale requirement.

    Returns the validated deployment mode: ``loopback-dev``, ``tailscale``,
    or ``firewall-ack``. Raises ``TailscaleValidationError`` on refusal.
    """
    host = config.BRIDGE_HOST.strip()
    if not config.TAILSCALE_REQUIRED:
        log.warning(
            "TAILSCALE_REQUIRED=false: bind %s is not validated against the "
            "Tailscale deployment requirement",
            host,
        )
        return "unvalidated"

    if _is_loopback(host):
        log.info("Bind %s is loopback: local development mode", host)
        return "loopback-dev"

    if tailscale_addresses is None:
        tailscale_addresses = await enumerate_tailscale_addresses()
    try:
        normalized = str(ipaddress.ip_address(host))
    except ValueError:
        normalized = ""
    if normalized and normalized in tailscale_addresses:
        log.info("Bind %s is assigned to tailscale0: tailnet deployment", host)
        return "tailscale"

    if config.TAILSCALE_FIREWALL_ACK:
        log.warning(
            "Bind %s is not a tailscale0 address; proceeding because "
            "TAILSCALE_FIREWALL_ACK=true (operator-verified firewall rule)",
            host,
        )
        return "firewall-ack"

    raise TailscaleValidationError(
        f"Startup refused: BRIDGE_HOST={host!r} is neither loopback nor an "
        "address assigned to tailscale0, and TAILSCALE_FIREWALL_ACK is not "
        "true. Bind the server's Tailscale address, or apply and verify a "
        "firewall rule for tailscale0/100.64.0.0/10 and set "
        "TAILSCALE_FIREWALL_ACK=true (plan section 27.2)."
    )
