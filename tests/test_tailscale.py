"""Tailscale bind validation tests (plan section 27.2)."""

from __future__ import annotations

import unittest

from core.tailscale import TailscaleValidationError, validate_bind

from fakes import make_config


class ValidateBindTest(unittest.IsolatedAsyncioTestCase):
    async def test_loopback_allowed_for_development(self):
        config = make_config(BRIDGE_HOST="127.0.0.1", TAILSCALE_REQUIRED=True)
        mode = await validate_bind(config, tailscale_addresses=set())
        self.assertEqual(mode, "loopback-dev")

    async def test_tailscale_address_allowed(self):
        config = make_config(BRIDGE_HOST="100.64.1.2", TAILSCALE_REQUIRED=True)
        mode = await validate_bind(config, tailscale_addresses={"100.64.1.2"})
        self.assertEqual(mode, "tailscale")

    async def test_all_interfaces_bind_refused_in_production(self):
        config = make_config(
            BRIDGE_HOST="0.0.0.0", TAILSCALE_REQUIRED=True, TAILSCALE_FIREWALL_ACK=False
        )
        with self.assertRaises(TailscaleValidationError):
            await validate_bind(config, tailscale_addresses={"100.64.1.2"})

    async def test_non_tailscale_bind_refused_in_production(self):
        config = make_config(
            BRIDGE_HOST="192.168.1.10", TAILSCALE_REQUIRED=True,
            TAILSCALE_FIREWALL_ACK=False,
        )
        with self.assertRaises(TailscaleValidationError):
            await validate_bind(config, tailscale_addresses={"100.64.1.2"})

    async def test_firewall_ack_allows_non_tailscale_bind(self):
        config = make_config(
            BRIDGE_HOST="0.0.0.0", TAILSCALE_REQUIRED=True, TAILSCALE_FIREWALL_ACK=True
        )
        with self.assertLogs("bridge.tailscale", level="WARNING"):
            mode = await validate_bind(config, tailscale_addresses=set())
        self.assertEqual(mode, "firewall-ack")

    async def test_tailscale_not_required_warns_and_allows(self):
        config = make_config(BRIDGE_HOST="0.0.0.0", TAILSCALE_REQUIRED=False)
        with self.assertLogs("bridge.tailscale", level="WARNING"):
            mode = await validate_bind(config, tailscale_addresses=set())
        self.assertEqual(mode, "unvalidated")


if __name__ == "__main__":
    unittest.main()
