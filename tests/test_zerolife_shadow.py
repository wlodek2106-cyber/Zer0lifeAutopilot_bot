import unittest

from zerolife_ai_trader.shadow import evaluate_context


class ShadowAdapterTests(unittest.TestCase):
    def valid(self):
        return {"SOL": {
            "chart_price_usd": 150.0,
            "change_m5_pct": 0.8,
            "change_h1_pct": 0.2,
            "chart_trend_h1": 1,
            "chart_ready": 1,
            "chart_buy_setup": 1,
            "chart_m5_age_seconds": 30,
            "chart_h1_age_seconds": 300,
        }}

    def test_valid_context_produces_shadow_only_recommendation(self):
        result = evaluate_context(self.valid())
        self.assertEqual(result[0]["action"], "PAPER_BUY")
        self.assertTrue(result[0]["risk"]["allowed"])

    def test_stale_or_missing_chart_data_fails_closed(self):
        context = self.valid()
        context["SOL"]["chart_m5_age_seconds"] = 9999
        self.assertEqual(evaluate_context(context)[0]["action"], "HOLD")
        self.assertEqual(evaluate_context({}), [])

    def test_risk_limit_can_block_valid_signal(self):
        result = evaluate_context(self.valid(), open_positions=5)
        self.assertEqual(result[0]["action"], "HOLD")
        self.assertFalse(result[0]["risk"]["allowed"])

    def test_negative_hourly_change_is_blocked(self):
        context = self.valid()
        context["SOL"]["change_h1_pct"] = -0.01
        self.assertEqual(evaluate_context(context)[0]["action"], "HOLD")


    def test_boolean_chart_flags_do_not_pass_integer_gates(self):
        context = self.valid()
        context["SOL"]["chart_ready"] = True
        self.assertEqual(evaluate_context(context)[0]["action"], "HOLD")
        context = self.valid()
        context["SOL"]["chart_trend_h1"] = True
        self.assertEqual(evaluate_context(context)[0]["action"], "HOLD")

    def test_invalid_limits_are_not_silently_replaced(self):
        from zerolife_ai_trader.risk import RiskLimits
        result = evaluate_context(self.valid(), limits=RiskLimits(max_order_fraction=2.0))
        self.assertEqual(result[0]["action"], "HOLD")
        self.assertFalse(result[0]["risk"]["allowed"])

if __name__ == "__main__":
    unittest.main()
