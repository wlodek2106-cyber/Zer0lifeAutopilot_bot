import unittest

from zerolife_ai_trader.configuration import (
    build_backtest_options, build_paper_broker, build_risk_limits,
)
from zerolife_ai_trader.settings import DEFAULT_SETTINGS


class AITraderConfigurationTests(unittest.TestCase):
    def payload(self):
        return dict(DEFAULT_SETTINGS)

    def test_settings_map_into_risk_limits(self):
        payload = self.payload()
        payload.update(orderRisk=1.5, dailyLoss=2.5, maxPositions=3, confidence=72)
        limits = build_risk_limits(payload)
        self.assertAlmostEqual(limits.max_order_fraction, 0.015)
        self.assertAlmostEqual(limits.max_daily_loss_fraction, 0.025)
        self.assertEqual(limits.max_open_positions, 3)
        self.assertAlmostEqual(limits.min_confidence, 0.72)

    def test_settings_map_into_paper_broker(self):
        payload = self.payload()
        payload.update(capital=2500, fee=12, slippage=8)
        broker = build_paper_broker(payload)
        self.assertEqual(broker.cash, 2500)
        self.assertEqual(broker.fee_bps, 12)
        self.assertEqual(broker.slippage_bps, 8)

    def test_settings_map_into_backtest_options(self):
        payload = self.payload()
        payload.update(capital=500, fee=15, slippage=7, orderRisk=4, confidence=65)
        options = build_backtest_options(payload)
        self.assertEqual(options, {
            "starting_cash": 500, "fee_bps": 15, "slippage_bps": 7,
            "order_fraction": 0.04, "min_confidence": 0.65,
        })

    def test_invalid_settings_fail_closed(self):
        payload = self.payload()
        payload["dailyLoss"] = 0
        for factory in (build_risk_limits, build_paper_broker, build_backtest_options):
            with self.subTest(factory=factory.__name__), self.assertRaises(ValueError):
                factory(payload)


if __name__ == "__main__":
    unittest.main()
