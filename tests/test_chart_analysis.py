import math
import unittest

import chart_analysis
import execution_policy


class ChartAnalysisTests(unittest.TestCase):
    def continuation_candles(self, now):
        h1 = self.make_candles(3600, 60, now, base=98, step=0.02)
        m5 = []
        for index in range(60):
            close = 100 + index * 0.002 + 0.08 * math.sin(
                2 * math.pi * (index + 2) / 10)
            m5.append({
                "timestamp": int(now - 300 * (60 - index)),
                "open": close - 0.01, "high": close + 0.04,
                "low": close - 0.04, "close": close,
                "volume": 85 if index == 59 else 100,
            })
        return m5, h1

    def test_bounded_continuation_can_enter_both_modes_without_support_zone(self):
        now = 1_000_000
        m5, h1 = self.continuation_candles(now)
        metrics = chart_analysis.analyze_charts(m5, h1, now=now)
        self.assertEqual(metrics["chart_trend_h1"], 1)
        self.assertEqual(metrics["chart_support_bounce"], 0)
        self.assertNotIn("chart_support_low_usd", metrics)
        self.assertEqual(metrics["chart_trend_continuation"], 1)
        self.assertEqual(metrics["chart_buy_setup"], 1)
        self.assertAlmostEqual(metrics["chart_volume_ratio_5m"], 0.85)
        metrics.update(change_m5_pct=0.3, change_h1_pct=1.0,
                       change_h6_pct=0.5, buys_m5=12, sells_m5=8)
        for signal in (execution_policy.core_entry_signal,
                       execution_policy.sniper_entry_signal):
            with self.subTest(mode=signal.__name__):
                self.assertTrue(signal("SOL", {"SOL": metrics}).allowed)

    def test_continuation_still_rejects_weak_volume_red_candles_and_overextension(self):
        now = 1_000_000
        for scenario in ("weak_volume", "red_last", "doji_last",
                         "falling_close", "overextended"):
            with self.subTest(scenario=scenario):
                m5, h1 = self.continuation_candles(now)
                if scenario == "weak_volume":
                    m5[-1]["volume"] = 79
                elif scenario == "red_last":
                    m5[-1]["open"] = m5[-1]["close"] + 0.01
                elif scenario == "doji_last":
                    m5[-1]["open"] = m5[-1]["close"]
                elif scenario == "falling_close":
                    m5[-1]["close"] = m5[-2]["close"] - 0.01
                    m5[-1]["open"] = m5[-1]["close"] - 0.01
                else:
                    m5[-1].update(open=102, close=102.1, high=102.2, low=101.99)
                result = chart_analysis.analyze_charts(m5, h1, now=now)
                self.assertEqual(result["chart_trend_continuation"], 0)
                self.assertEqual(result["chart_buy_setup"], 0)

    def test_one_closed_green_candle_after_red_can_enter_both_modes(self):
        now = 1_000_000
        m5, h1 = self.continuation_candles(now)
        m5[-2]["open"] = m5[-2]["close"] + 0.01
        metrics = chart_analysis.analyze_charts(m5, h1, now=now)
        self.assertLess(m5[-2]["close"], m5[-2]["open"])
        self.assertGreater(m5[-1]["close"], m5[-1]["open"])
        self.assertEqual(metrics["chart_trend_continuation"], 1)
        self.assertEqual(metrics["chart_support_bounce"], 0)
        metrics.update(change_m5_pct=0.3, change_h1_pct=1.0,
                       change_h6_pct=0.5, buys_m5=12, sells_m5=8)
        for signal in (execution_policy.core_entry_signal,
                       execution_policy.sniper_entry_signal):
            with self.subTest(mode=signal.__name__):
                self.assertTrue(signal("SOL", {"SOL": metrics}).allowed)

    def test_continuation_still_requires_fresh_and_rising_hourly_chart(self):
        now = 1_000_000
        for scenario in ("provider_stale", "expired", "flat_hourly", "falling_hourly"):
            with self.subTest(scenario=scenario):
                m5, h1 = self.continuation_candles(now)
                options = {}
                if scenario == "provider_stale":
                    options["m5_stale"] = True
                elif scenario == "expired":
                    for bar in m5:
                        bar["timestamp"] -= 1200
                elif scenario == "flat_hourly":
                    h1 = self.make_candles(3600, 60, now, base=100, step=0)
                else:
                    # Valid falling hourly candles, with current price below their EMAs.
                    for index, bar in enumerate(h1):
                        close = 102 - index * 0.02
                        bar.update(open=close + 0.01, high=close + 0.04,
                                   low=close - 0.04, close=close)
                result = chart_analysis.analyze_charts(m5, h1, now=now, **options)
                self.assertEqual(result["chart_trend_continuation"], 0)
                self.assertEqual(result["chart_buy_setup"], 0)

    def make_candles(self, interval, count, now, base=100.0, step=0.1):
        candles = []
        for index in range(count):
            close = base + index * step
            candles.append({
                "timestamp": int(now - interval * (count - index)),
                "open": close - step / 4,
                "high": close + step,
                "low": close - step,
                "close": close,
                "volume": 100.0,
            })
        return candles

    def test_parser_sorts_and_excludes_open_or_invalid_candles(self):
        now = 1_000_000
        payload = {"data": {"attributes": {"ohlcv_list": [
            [now - 300, 100, 102, 99, 101, 10],
            [now - 600, 99, 101, 98, 100, 12],
            [now, 100, 103, 99, 102, 20],
            [now - 900, 100, 99, 98, 99, 8],
        ]}}}

        candles = chart_analysis.parse_ohlcv(payload, 300, now=now)

        self.assertEqual([bar["timestamp"] for bar in candles], [now - 600])
        self.assertEqual(candles[0]["close"], 100)

    def test_fresh_chart_calculates_numeric_indicators_but_does_not_invent_setup(self):
        now = 1_000_000
        m5 = self.make_candles(300, 60, now, base=100, step=0.1)
        h1 = self.make_candles(3600, 60, now, base=100, step=0.1)

        result = chart_analysis.analyze_charts(m5, h1, now=now)

        self.assertEqual(result["chart_ready"], 1)
        self.assertEqual(result["chart_trend_h1"], 1)
        self.assertGreater(result["chart_ema20_5m_usd"], 0)
        self.assertGreaterEqual(result["chart_rsi14_5m"], 0)
        self.assertIn(result["chart_buy_setup"], (0, 1))
        self.assertEqual(result["chart_buy_setup"], 0)

    def test_stale_or_short_history_fails_closed(self):
        now = 1_000_000
        m5 = self.make_candles(300, 20, now, base=100, step=0.1)
        h1 = self.make_candles(3600, 60, now, base=100, step=0.1)

        short_history = chart_analysis.analyze_charts(m5, h1, now=now)
        stale_history = chart_analysis.analyze_charts(
            self.make_candles(300, 60, now - 1200, base=100, step=0.1),
            h1,
            now=now,
        )

        self.assertEqual(short_history["chart_ready"], 0)
        self.assertEqual(short_history["chart_buy_setup"], 0)
        self.assertEqual(stale_history["chart_ready"], 0)

    def test_confirmed_support_reaction_can_qualify_as_a_setup(self):
        now = 1_000_000
        h1 = []
        for index in range(60):
            close = 100 + index * 0.02 + math.sin(2 * math.pi * index / 10)
            h1.append({
                "timestamp": int(now - 3600 * (60 - index)),
                "open": close - 0.08,
                "high": close + 0.25,
                "low": close - 0.25,
                "close": close,
                "volume": 100,
            })
        m5 = []
        for index in range(60):
            close = 100 + index * 0.004 + 0.18 * math.sin(2 * math.pi * index / 12)
            opened = close - 0.04 if index == 59 else close - 0.01
            if index == 59:
                close = 100.35
            m5.append({
                "timestamp": int(now - 300 * (60 - index)),
                "open": opened,
                "high": max(opened, close) + 0.06,
                "low": min(opened, close) - (0.3 if index == 59 else 0.04),
                "close": close,
                "volume": 160 if index == 59 else 100,
            })

        result = chart_analysis.analyze_charts(m5, h1, now=now)

        self.assertEqual(result["chart_ready"], 1)
        self.assertEqual(result["chart_buy_setup"], 1)
        self.assertGreater(result["chart_support_touches_h1"], 1)

        extended_bounce = [dict(bar) for bar in m5]
        extended_bounce[-1].update({
            "open": 100.35, "high": 100.8, "low": 99.99,
            "close": 100.7, "volume": 160,
        })
        extended_result = chart_analysis.analyze_charts(
            extended_bounce, h1, now=now)
        self.assertEqual(extended_result["chart_ready"], 1)
        self.assertEqual(extended_result["chart_buy_setup"], 0)

    def test_one_touch_is_not_a_confirmed_support_or_resistance_zone(self):
        self.assertIsNone(chart_analysis._nearest_zone(
            [100.0], 101.0, 0.5, support=True, minimum_touches=2))
        repeated = chart_analysis._nearest_zone(
            [100.0, 100.1], 101.0, 0.5, support=True, minimum_touches=2)
        self.assertIsNotNone(repeated)
        self.assertEqual(repeated["touches"], 2)

    def test_repeated_resistance_rejection_is_sell_evidence_not_a_breakout_buy(self):
        now = 1_000_000
        h1 = []
        m5 = []
        for index in range(60):
            close = 102 + math.sin(2 * math.pi * index / 12)
            h1.append({
                "timestamp": int(now - 3600 * (60 - index)),
                "open": close, "high": close + 0.08, "low": close - 0.08,
                "close": close, "volume": 100,
            })
            close_5m = 102.7 + 0.1 * math.sin(2 * math.pi * index / 12)
            opened = close_5m - 0.02
            high = max(opened, close_5m) + 0.04
            if index == 59:
                opened, close_5m, high = 103.12, 102.95, 103.4
            m5.append({
                "timestamp": int(now - 300 * (60 - index)),
                "open": opened, "high": high,
                "low": min(opened, close_5m) - 0.04,
                "close": close_5m, "volume": 150 if index == 59 else 100,
            })

        result = chart_analysis.analyze_charts(m5, h1, now=now)

        self.assertEqual(result["chart_ready"], 1)
        self.assertGreaterEqual(result["chart_resistance_touches_h1"], 2)
        self.assertEqual(result["chart_sell_setup"], 1)
        self.assertEqual(result["chart_buy_setup"], 0)


if __name__ == "__main__":
    unittest.main()
