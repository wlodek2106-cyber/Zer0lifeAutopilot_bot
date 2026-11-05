import unittest
from zerolife_ai_trader.live_risk_guard import (
    CHAIN_CAPS_USDC, TOTAL_CAP_USDC, LiveRiskGuardError, check_live_order,
)


class DualChainLiveRiskGuardTests(unittest.TestCase):
    def check(self, **overrides):
        values = {
            "chain": "solana", "order_usdc": "100",
            "chain_exposure_usdc": "0", "other_chain_exposure_usdc": "0",
            "pending_usdc": "0", "daily_realized_loss_usdc": "0",
            "daily_loss_limit_usdc": "500", "live_enabled": True,
        }
        values.update(overrides)
        return check_live_order(**values)

    def test_confirmed_cap_split_is_exact(self):
        self.assertEqual(CHAIN_CAPS_USDC["solana"], 50000)
        self.assertEqual(CHAIN_CAPS_USDC["base"], 50000)
        self.assertEqual(TOTAL_CAP_USDC, 100000)

    def test_order_inside_both_caps_is_allowed(self):
        result = self.check(order_usdc="250", chain_exposure_usdc="1000",
                            other_chain_exposure_usdc="49000")
        self.assertTrue(result["allowed"])
        self.assertEqual(result["remaining_combined_usdc"], "49750")

    def test_per_chain_cap_includes_pending_reservations(self):
        with self.assertRaisesRegex(LiveRiskGuardError, "Per-chain"):
            self.check(chain_exposure_usdc="49900", pending_usdc="50",
                       order_usdc="51")

    def test_combined_cap_is_shared_across_chains(self):
        with self.assertRaisesRegex(LiveRiskGuardError, "Combined"):
            self.check(chain_exposure_usdc="100", other_chain_exposure_usdc="49950",
                       order_usdc="1")

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
