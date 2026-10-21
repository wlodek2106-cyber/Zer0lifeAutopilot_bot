import math
import unittest

import chart_analysis


class ChartAnalysisTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
