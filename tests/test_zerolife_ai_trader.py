import unittest
from unittest.mock import patch
from zerolife_ai_trader.strategy import Signal

from zerolife_ai_trader.indicators import ema, rsi
from zerolife_ai_trader.strategy import analyze
from zerolife_ai_trader.risk import assess_order
from zerolife_ai_trader.paper import PaperBroker
from zerolife_ai_trader.backtest import run_backtest


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

    def test_paper_broker_rejects_overflow_without_mutating_state(self):
        broker = PaperBroker(1000, fee_bps=0, slippage_bps=0)
        with self.assertRaises(ValueError):
            broker.buy("BTC", 1e308, 1e308)
        self.assertEqual(broker.cash, 1000.0)
        self.assertEqual(broker.positions, {})
        broker.buy("BTC", 1, 1)
        before_cash = broker.cash
        before_positions = dict(broker.positions)
        with self.assertRaises(ValueError):
            broker.sell("BTC", 1e308, 1e308)
        self.assertEqual(broker.cash, before_cash)
        self.assertEqual(broker.positions, before_positions)

    def test_paper_broker_rejects_boolean_and_blank_orders(self):
        broker = PaperBroker(1000)
        for symbol, quantity, price in (("", 1, 10), ("BTC", True, 10),
                                        ("BTC", 1, False)):
            with self.assertRaises(ValueError):
                broker.buy(symbol, quantity, price)
        self.assertEqual(broker.cash, 1000.0)
        self.assertEqual(broker.positions, {})

    def test_paper_broker_maximum_costs_are_applied_without_negative_proceeds(self):
        broker = PaperBroker(1000, fee_bps=1000, slippage_bps=1000)
        broker.buy("BTC", 1, 100)
        before_cash = broker.cash
        sale = broker.sell("BTC", 1, 100)
        self.assertGreater(sale.price, 0)
        self.assertGreater(sale.cash_after, before_cash)
        self.assertGreaterEqual(sale.fee, 0)
        self.assertNotIn("BTC", broker.positions)

    def test_paper_broker_does_not_accept_missing_or_corrupt_position(self):
        broker = PaperBroker(1000)
        with self.assertRaises(ValueError):
            broker.sell("MISSING", 1, 10)
        broker.positions["CORRUPT"] = float("nan")
        with self.assertRaises(ValueError):
            broker.sell("CORRUPT", 1, 10)
        self.assertEqual(broker.cash, 1000.0)

    def test_paper_broker_reports_insufficient_cash_without_mutation(self):
        broker = PaperBroker(10, fee_bps=0, slippage_bps=0)
        with self.assertRaisesRegex(ValueError, "Insufficient paper cash"):
            broker.buy("BTC", 2, 10)
        self.assertEqual(broker.cash, 10.0)
        self.assertEqual(broker.positions, {})

    def test_indicators_reject_boolean_and_string_prices(self):
        for prices in ([1, 2, True], [1, 2, "3"]):
            with self.assertRaises(ValueError):
                ema(prices, 2)
            with self.assertRaises(ValueError):
                rsi(prices, 2)

    def test_paper_broker_rejects_corrupt_existing_position_on_buy(self):
        broker = PaperBroker(1000)
        broker.positions["BTC"] = float("nan")
        before_cash = broker.cash
        with self.assertRaises(ValueError):
            broker.buy("BTC", 1, 10)
        self.assertEqual(broker.cash, before_cash)
        self.assertTrue(float("nan") != broker.positions["BTC"])

    def test_historical_backtest_reports_costs_and_is_deterministic(self):
        from zerolife_ai_trader.backtest import run_backtest
        closes = [100 + i * 0.2 for i in range(80)]
        first = run_backtest(closes)
        second = run_backtest(closes)
        self.assertEqual(first, second)
        self.assertGreaterEqual(first["fees_paid"], 0)
        self.assertGreaterEqual(first["max_drawdown_pct"], 0)
        self.assertEqual(first["execution_model"], "next_bar_close_with_fee_and_slippage")
        self.assertEqual(first["open_trades"], 0)

    def test_backtest_rejects_invalid_data_and_settings(self):
        from zerolife_ai_trader.backtest import run_backtest
        closes = [100 + i for i in range(40)]
        for bad in ([*closes[:-1], True], [*closes[:-1], float("nan")]):
            with self.assertRaises(ValueError):
                run_backtest(bad)
        with self.assertRaises(ValueError):
            run_backtest(closes, fee_bps=1001)
        with self.assertRaises(ValueError):
            run_backtest(closes, starting_cash=True)

    def test_backtest_is_deterministic_and_accounts_for_costs(self):
        closes = [100.0] * 40
        first = run_backtest(closes, starting_cash=1000, fee_bps=10, slippage_bps=5)
        second = run_backtest(closes, starting_cash=1000, fee_bps=10, slippage_bps=5)
        self.assertEqual(first, second)
        self.assertEqual(first["final_equity"], 1000.0)
        self.assertEqual(first["closed_trades"], 0)
        self.assertEqual(first["execution_model"], "next_bar_close_with_fee_and_slippage")

    def test_backtest_rejects_invalid_prices_and_settings(self):
        with self.assertRaises(ValueError):
            run_backtest([100.0] * 29)
        with self.assertRaises(ValueError):
            run_backtest([100.0] * 30 + [True])
        with self.assertRaises(ValueError):
            run_backtest([100.0] * 31, fee_bps=float("nan"))

    def test_backtest_executes_signals_on_next_bar_not_signal_bar(self):
        closes = [100.0 + i for i in range(34)]

        def controlled_signal(history):
            if len(history) == 30:
                return Signal("BUY", 0.9, history[-1], ("fixture buy",))
            if len(history) == 32:
                return Signal("SELL", 0.9, history[-1], ("fixture sell",))
            return Signal("HOLD", 0.5, history[-1], ("fixture hold",))

        with patch("zerolife_ai_trader.backtest.analyze", side_effect=controlled_signal):
            result = run_backtest(closes, starting_cash=1000, fee_bps=0,
                                  slippage_bps=0, order_fraction=0.5)
        self.assertEqual(result["closed_trades"], 1)
        trade = result["trades"][0]
        self.assertEqual(trade["entry_index"], 30)
        self.assertEqual(trade["entry_price"], closes[30])
        self.assertEqual(trade["exit_index"], 33)
        self.assertEqual(trade["exit_price"], closes[33])
        self.assertEqual(result["open_trades"], 0)

if __name__ == "__main__":
    unittest.main()
