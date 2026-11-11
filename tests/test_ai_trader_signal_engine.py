import unittest

from zerolife_ai_trader.signal_engine import analyze_with_strategy, evaluate_watchlist
from zerolife_ai_trader.settings import DEFAULT_SETTINGS


class SignalEngineTests(unittest.TestCase):
    def prices(self, values):
        return [float(value) for value in values]

    def settings(self):
        return dict(DEFAULT_SETTINGS)

    def test_manual_strategy_never_creates_automatic_entry(self):
        signal = analyze_with_strategy(self.prices(range(100, 140)), "manual")
        self.assertEqual(signal.action, "HOLD")
        self.assertEqual(signal.confidence, 0.0)

    def test_trend_strategy_detects_uptrend(self):
        prices = self.prices(range(100, 140))
        signal = analyze_with_strategy(prices, "trend_following")
        self.assertEqual(signal.action, "BUY")

    def test_mean_reversion_uses_extreme_rsi(self):
        prices = self.prices([100 - i * 0.8 for i in range(40)])
        signal = analyze_with_strategy(prices, "mean_reversion")
        self.assertEqual(signal.action, "BUY")

    def test_watchlist_risk_gate_emits_paper_signal_only(self):
        settings = self.settings()
        settings["symbols"] = "SOLUSDC"
        settings["strategy"] = "trend_following"
        result = evaluate_watchlist({"SOLUSDC": self.prices(range(100, 140))},
                                    settings)
        self.assertEqual(len(result), 1)
        self.assertIn(result[0]["action"], {"PAPER_BUY", "HOLD"})
        self.assertTrue(result[0]["risk"]["allowed"])
        self.assertGreater(result[0]["risk"]["quantity"], 0)

    def test_missing_data_fails_closed(self):
        settings = self.settings()
        settings["symbols"] = "SOLUSDC"
        result = evaluate_watchlist({}, settings)
        self.assertEqual(result[0]["action"], "HOLD")
        self.assertFalse(result[0]["risk"]["allowed"])

    def test_rejects_invalid_strategy_and_nonfinite_prices(self):
        with self.assertRaises(ValueError):
            analyze_with_strategy([100.0] * 30, "unapproved")
        with self.assertRaises(ValueError):
            analyze_with_strategy([100.0] * 29 + [float("nan")], "ema_rsi")


if __name__ == "__main__":
    unittest.main()
