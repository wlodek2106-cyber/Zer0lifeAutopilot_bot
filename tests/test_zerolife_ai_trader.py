import unittest

from zerolife_ai_trader.indicators import ema, rsi
from zerolife_ai_trader.strategy import analyze
from zerolife_ai_trader.risk import assess_order
from zerolife_ai_trader.paper import PaperBroker


class TraderFoundationTests(unittest.TestCase):
    def test_indicators(self):
        prices = [float(x) for x in range(1, 31)]
        self.assertGreater(ema(prices, 9), ema(prices, 21))
        self.assertGreater(rsi(prices, 14), 50)

    def test_strategy_returns_valid_action(self):
        prices = [100 + i * 0.2 for i in range(30)]
        self.assertIn(analyze(prices).action, {"BUY", "SELL", "HOLD"})

    def test_risk_gate_caps_order_and_denies_low_confidence(self):
        decision = assess_order(equity=1000, cash=1000, price=10, confidence=0.9)
        self.assertTrue(decision.allowed)
        self.assertAlmostEqual(decision.quantity, 2.0)
        denied = assess_order(equity=1000, cash=1000, price=10, confidence=0.1)
        self.assertFalse(denied.allowed)

    def test_paper_broker_accounts_for_costs(self):
        broker = PaperBroker(1000)
        fill = broker.buy("BTC", 1, 100)
        self.assertLess(broker.cash, 900)
        sale = broker.sell("BTC", 1, 110)
        self.assertGreater(sale.cash_after, fill.cash_after)
        self.assertNotIn("BTC", broker.positions)


    def test_invalid_risk_limits_fail_closed(self):
        from zerolife_ai_trader.risk import RiskLimits, assess_order
        for limits in (
            RiskLimits(max_order_fraction=2.0),
            RiskLimits(max_daily_loss_fraction=0.0),
            RiskLimits(min_confidence=True),
            RiskLimits(max_open_positions=True),
        ):
            decision = assess_order(equity=1000, cash=1000, price=10,
                                    confidence=0.9, limits=limits)
            self.assertFalse(decision.allowed)
            self.assertEqual(decision.quantity, 0.0)

if __name__ == "__main__":
    unittest.main()
