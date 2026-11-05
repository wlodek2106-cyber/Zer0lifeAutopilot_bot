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
        self.assertEqual(trade["exit_index"], 32)
        self.assertEqual(trade["exit_price"], closes[32])
        self.assertEqual(result["open_trades"], 0)

    def test_backtest_uptrend_and_downtrend_forced_exit_metrics(self):
        def buy_once(history):
            if len(history) == 30:
                return Signal("BUY", 0.9, history[-1], ("fixture buy",))
            return Signal("HOLD", 0.5, history[-1], ("fixture hold",))

        rising = [100.0] * 30 + [100.0 + i for i in range(1, 11)]
        falling = [100.0] * 30 + [100.0 - i for i in range(1, 11)]
        results = []
        for prices in (rising, falling):
            with patch("zerolife_ai_trader.backtest.analyze", side_effect=buy_once):
                results.append(run_backtest(prices, starting_cash=1000,
                                             fee_bps=10, slippage_bps=5,
                                             order_fraction=0.5))
        up, down = results
        self.assertEqual(up["closed_trades"], 1)
        self.assertTrue(up["trades"][0].get("forced_exit"))
        self.assertGreater(up["net_pnl"], 0)
        self.assertEqual(down["closed_trades"], 1)
        self.assertTrue(down["trades"][0].get("forced_exit"))
        self.assertLess(down["net_pnl"], 0)
        self.assertGreater(down["max_drawdown_pct"], 0)
        self.assertGreater(up["fees_paid"], 0)
        self.assertGreater(down["fees_paid"], 0)
        self.assertGreater(up["gross_profit"], 0)
        self.assertGreater(down["gross_loss"], 0)
        self.assertIn("buy_hold_return_pct", up)
        self.assertIn("excess_return_vs_buy_hold_pct", up)
        self.assertGreater(up["buy_hold_final_equity"], 0)
        self.assertGreater(down["buy_hold_fees_paid"], 0)
        self.assertGreater(up["expectancy_per_trade"], 0)
        self.assertLess(down["expectancy_per_trade"], 0)
        self.assertGreaterEqual(up["average_win"], 0)
        self.assertLessEqual(down["average_loss"], 0)

    def test_segmented_evaluation_covers_chronological_data(self):
        from zerolife_ai_trader.backtest import run_segmented_evaluation
        closes = [100.0 + i * 0.1 for i in range(120)]
        result = run_segmented_evaluation(closes, segments=3)
        self.assertEqual(result["segment_count"], 3)
        self.assertEqual(result["segments"][0]["start_index"], 0)
        self.assertEqual(result["segments"][-1]["end_index_exclusive"], 120)
        self.assertEqual(sum(s["bars"] for s in result["segments"]), 120)
        self.assertTrue(all(s["bars"] >= 31 for s in result["segments"]))
        self.assertIn("worst_segment_return_pct", result)

    def test_segmented_evaluation_rejects_too_few_bars_and_invalid_segments(self):
        from zerolife_ai_trader.backtest import run_segmented_evaluation
        with self.assertRaises(ValueError):
            run_segmented_evaluation([100.0] * 60, segments=3)
        with self.assertRaises(ValueError):
            run_segmented_evaluation([100.0] * 90, segments=True)

    def test_ohlcv_validation_accepts_valid_contiguous_candles(self):
        from zerolife_ai_trader.market_data import validate_candles
        candles = [
            {"timestamp_ms": i * 60_000, "open": 10, "high": 12,
             "low": 9, "close": 11, "volume": 100}
            for i in range(4)
        ]
        result = validate_candles(candles, interval_ms=60_000)
        self.assertEqual(result["count"], 4)
        self.assertEqual(result["quality"], "complete")
        self.assertEqual(result["gap_count"], 0)

    def test_ohlcv_validation_rejects_gaps_and_bad_candle_values(self):
        from zerolife_ai_trader.market_data import validate_candles
        candles = [
            {"timestamp_ms": 0, "open": 10, "high": 12,
             "low": 9, "close": 11, "volume": 100},
            {"timestamp_ms": 120_000, "open": 11, "high": 13,
             "low": 10, "close": 12, "volume": 100},
        ]
        with self.assertRaises(ValueError):
            validate_candles(candles, interval_ms=60_000)
        report = validate_candles(candles, interval_ms=60_000, reject_gaps=False)
        self.assertEqual(report["gap_count"], 1)
        candles[1]["high"] = 8
        with self.assertRaises(ValueError):
            validate_candles(candles, interval_ms=60_000, reject_gaps=False)

    def test_ohlcv_csv_import_validates_and_normalizes_rows(self):
        from zerolife_ai_trader.csv_data import load_ohlcv_csv
        csv_text = (
            "timestamp_ms,open,high,low,close,volume\n"
            "0,10,12,9,11,100\n"
            "60000,11,13,10,12,120\n"
        )
        report = load_ohlcv_csv(csv_text, interval_ms=60_000)
        self.assertEqual(report["count"], 2)
        self.assertEqual(report["candles"][0]["close"], 11.0)
        self.assertEqual(report["quality"], "complete")

    def test_ohlcv_csv_import_rejects_bad_schema_and_gaps(self):
        from zerolife_ai_trader.csv_data import load_ohlcv_csv
        with self.assertRaises(ValueError):
            load_ohlcv_csv("timestamp_ms,close\n0,10\n", interval_ms=60_000)
        csv_text = (
            "timestamp_ms,open,high,low,close,volume\n"
            "0,10,12,9,11,100\n"
            "120000,11,13,10,12,120\n"
        )
        with self.assertRaises(ValueError):
            load_ohlcv_csv(csv_text, interval_ms=60_000)

    def test_ohlcv_timestamp_must_align_to_utc_interval_anchor(self):
        from zerolife_ai_trader.market_data import validate_candles
        candles = [
            {"timestamp_ms": 30_000, "open": 10, "high": 12,
             "low": 9, "close": 11, "volume": 100},
            {"timestamp_ms": 90_000, "open": 11, "high": 13,
             "low": 10, "close": 12, "volume": 100},
        ]
        with self.assertRaises(ValueError):
            validate_candles(candles, interval_ms=60_000)
        report = validate_candles(candles, interval_ms=60_000,
                                   anchor_ms=30_000)
        self.assertEqual(report["quality"], "complete")
        self.assertEqual(report["anchor_ms"], 30_000)

    def test_candle_backtest_requires_validated_complete_ohlcv(self):
        from zerolife_ai_trader.backtest import run_candle_backtest
        candles = [
            {"timestamp_ms": i * 60_000, "open": 100 + i, "high": 102 + i,
             "low": 99 + i, "close": 101 + i, "volume": 1000}
            for i in range(40)
        ]
        result = run_candle_backtest(candles, interval_ms=60_000)
        self.assertEqual(result["data_quality"]["quality"], "complete")
        self.assertEqual(result["data_quality"]["candle_count"], 40)
        self.assertIn("buy_hold_return_pct", result)
        candles[5]["timestamp_ms"] += 30_000
        with self.assertRaises(ValueError):
            run_candle_backtest(candles, interval_ms=60_000)

    def test_backtest_report_exports_versioned_json_and_requires_clean_data(self):
        import json
        from zerolife_ai_trader.report import build_backtest_report, report_to_json
        from zerolife_ai_trader.backtest import run_candle_backtest
        candles = [
            {"timestamp_ms": i * 60_000, "open": 100 + i, "high": 102 + i,
             "low": 99 + i, "close": 101 + i, "volume": 1000}
            for i in range(40)
        ]
        result = run_candle_backtest(candles, interval_ms=60_000)
        report = build_backtest_report(
            result, symbol="SOL/USDC", interval="1m",
            data_source="test-fixture", period_start_ms=0,
            period_end_ms=39 * 60_000,
        )
        payload = json.loads(report_to_json(report))
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["symbol"], "SOL/USDC")
        self.assertEqual(payload["data_quality"]["quality"], "complete")
        result["data_quality"]["quality"] = "gaps_detected"
        with self.assertRaises(ValueError):
            build_backtest_report(
                result, symbol="SOL/USDC", interval="1m",
                data_source="test-fixture", period_start_ms=0,
                period_end_ms=39 * 60_000,
            )

if __name__ == "__main__":
    unittest.main()
