import unittest

from zerolife_ai_trader.backtest import run_backtest


class BacktestStrategySelectionTests(unittest.TestCase):
    def test_selected_trend_strategy_is_reported_and_runs(self):
        closes = [float(x) for x in range(100, 180)]
        report = run_backtest(closes, strategy="trend_following")
        self.assertEqual(report["strategy"], "trend_following")
        self.assertEqual(report["execution_model"],
                         "next_bar_close_with_fee_and_slippage")

    def test_manual_strategy_does_not_open_positions(self):
        closes = [float(x) for x in range(100, 150)]
        report = run_backtest(closes, strategy="manual")
        self.assertEqual(report["strategy"], "manual")
        self.assertEqual(report["closed_trades"], 0)
        self.assertEqual(report["open_trades"], 0)

    def test_unknown_strategy_fails_closed(self):
        with self.assertRaises(ValueError):
            run_backtest([float(x) for x in range(100, 140)],
                         strategy="unknown")


if __name__ == "__main__":
    unittest.main()
