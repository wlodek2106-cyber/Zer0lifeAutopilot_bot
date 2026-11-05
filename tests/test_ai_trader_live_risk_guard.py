import unittest
from zerolife_ai_trader.live_risk_guard import (
    LIVE_MODE_DEFAULT_ENABLED, LiveRiskGuardError, check_live_order,
)


class DualChainLiveRiskGuardTests(unittest.TestCase):
    def check(self, **overrides):
        values = {
            "chain": "solana", "order_usdc": "100",
            "chain_exposure_usdc": "0", "other_chain_exposure_usdc": "0",
            "pending_usdc": "0", "daily_realized_loss_usdc": "0",
            "daily_loss_limit_usdc": "500",
            "total_cap_usdc": "250",
            "chain_caps_usdc": {"solana": "125", "base": "125"},
            "live_enabled": True,
        }
        values.update(overrides)
        return check_live_order(**values)

    def test_live_mode_defaults_off(self):
        self.assertFalse(LIVE_MODE_DEFAULT_ENABLED)

    def test_missing_user_approved_cap_fails_closed(self):
        with self.assertRaisesRegex(LiveRiskGuardError, "Explicitly approved"):
            self.check(total_cap_usdc=None)
        with self.assertRaisesRegex(LiveRiskGuardError, "Explicitly approved"):
            self.check(chain_caps_usdc=None)

    def test_order_inside_explicit_caps_is_allowed(self):
        result = self.check(order_usdc="100", other_chain_exposure_usdc="100")
        self.assertTrue(result["allowed"])
        self.assertEqual(result["remaining_combined_usdc"], "50")

    def test_per_chain_cap_includes_pending_reservations(self):
        with self.assertRaisesRegex(LiveRiskGuardError, "Per-chain"):
            self.check(chain_exposure_usdc="100", pending_usdc="20",
                       order_usdc="6")

    def test_combined_cap_is_shared_across_chains(self):
        with self.assertRaisesRegex(LiveRiskGuardError, "Combined"):
            self.check(chain_exposure_usdc="100", other_chain_exposure_usdc="50",
                       order_usdc="101")

    def test_chain_caps_cannot_exceed_combined_cap(self):
        with self.assertRaisesRegex(LiveRiskGuardError, "exceed"):
            self.check(chain_caps_usdc={"solana": "200", "base": "100"})

    def test_daily_loss_limit_fails_closed(self):
        with self.assertRaisesRegex(LiveRiskGuardError, "Daily realized-loss"):
            self.check(daily_realized_loss_usdc="500")
        with self.assertRaisesRegex(LiveRiskGuardError, "not configured"):
            self.check(daily_loss_limit_usdc=None)

    def test_live_mode_must_be_explicitly_enabled(self):
        with self.assertRaisesRegex(LiveRiskGuardError, "disabled"):
            self.check(live_enabled=False)

    def test_bad_chain_and_invalid_amount_rejected(self):
        with self.assertRaisesRegex(LiveRiskGuardError, "Unsupported"):
            self.check(chain="ethereum")
        with self.assertRaises(LiveRiskGuardError):
            self.check(order_usdc="-1")
        with self.assertRaises(LiveRiskGuardError):
            self.check(order_usdc="NaN")


if __name__ == "__main__":
    unittest.main()
