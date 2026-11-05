import unittest

from zerolife_ai_trader.shadow import assess_market_context


def valid_context():
    return {
        "chart_ready": 1, "chart_buy_setup": 1, "chart_price_usd": 10.0,
        "chart_trend_h1": 1, "change_m5_pct": 0.8, "change_h1_pct": 1.2,
        "chart_ema20_5m_usd": 10.1, "chart_ema50_5m_usd": 10.0,
        "chart_ema20_h1_usd": 10.0, "chart_ema50_h1_usd": 9.9,
        "chart_rsi14_5m": 55.0,
    }


class ShadowAdapterTests(unittest.TestCase):
    def test_valid_existing_market_context_produces_candidate_only(self):
        result = assess_market_context("SOL", valid_context())
        self.assertEqual(result.action, "BUY_CANDIDATE")
        self.assertTrue(any("not an order" in reason for reason in result.reasons))

    def test_stale_or_unconfirmed_chart_fails_closed(self):
        for changes in ({"chart_ready": 0}, {"chart_buy_setup": 0}, {"change_h1_pct": -0.1},
                        {"chart_trend_h1": -1}, {"chart_ema20_5m_usd": None},
                        {"chart_rsi14_5m": 75.0}, {"change_m5_pct": 3.1}):
            with self.subTest(changes=changes):
                result = assess_market_context("SOL", {**valid_context(), **changes})
                self.assertEqual(result.action, "HOLD")

    def test_adapter_does_not_accept_nonfinite_values(self):
        result = assess_market_context("SOL", {**valid_context(), "chart_rsi14_5m": float("nan")})
        self.assertEqual(result.action, "HOLD")


if __name__ == "__main__":
    unittest.main()
