import unittest

from zerolife_ai_trader.portfolio import PaperPortfolio


class PaperPortfolioTests(unittest.TestCase):
    def test_mark_to_market_and_drawdown(self):
        portfolio = PaperPortfolio(1000)
        first = portfolio.snapshot(
            cash=400, positions={"SOLUSDC": 2},
            prices={"SOLUSDC": 300}, cost_basis={"SOLUSDC": 500},
        )
        self.assertEqual(first.market_value, 600)
        self.assertEqual(first.equity, 1000)
        self.assertEqual(first.unrealized_pnl, 100)
        self.assertEqual(first.total_pnl, 0)
        second = portfolio.snapshot(
            cash=400, positions={"SOLUSDC": 2},
            prices={"SOLUSDC": 250}, cost_basis={"SOLUSDC": 500},
        )
        self.assertEqual(second.equity, 900)
        self.assertAlmostEqual(second.drawdown_pct, 10)

    def test_realized_pnl_is_reported_separately(self):
        portfolio = PaperPortfolio(1000)
        portfolio.record_realized_pnl(25)
        snapshot = portfolio.snapshot(cash=1025, positions={}, prices={})
        self.assertEqual(snapshot.realized_pnl, 25)
        self.assertEqual(snapshot.total_pnl, 25)

    def test_missing_mark_is_rejected(self):
        portfolio = PaperPortfolio(1000)
        with self.assertRaisesRegex(ValueError, "Missing mark price"):
            portfolio.snapshot(cash=0, positions={"SOL": 1}, prices={})

    def test_non_finite_and_negative_values_are_rejected(self):
        portfolio = PaperPortfolio(1000)
        with self.assertRaises(ValueError):
            portfolio.snapshot(cash=0, positions={"SOL": float("inf")},
                               prices={"SOL": 1})
        with self.assertRaises(ValueError):
            portfolio.snapshot(cash=-1, positions={}, prices={})

    def test_missing_cost_basis_defaults_to_mark_value(self):
        portfolio = PaperPortfolio(1000)
        snapshot = portfolio.snapshot(
            cash=500, positions={"BTCUSDT": 0.01}, prices={"BTCUSDT": 50000}
        )
        self.assertEqual(snapshot.cost_basis, 500)
        self.assertEqual(snapshot.unrealized_pnl, 0)


if __name__ == "__main__":
    unittest.main()
