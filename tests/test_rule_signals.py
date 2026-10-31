import unittest

import ai_risk_review
import execution_policy
import rule_signals


def chart(trend=0, m5=0.8, h1=0.2, h6=-1):
    return {
        "chart_ready": 1,
        "chart_buy_setup": 1,
        "chart_trend_h1": trend,
        "change_m5_pct": m5,
        "change_h1_pct": h1,
        "change_h6_pct": h6,
        "liquidity_usd": 50_000,
        "volume_24h_usd": 100_000,
    }


class RuleSignalTests(unittest.TestCase):
    def test_core_and_sniper_use_existing_entry_gates_without_ai(self):
        universes = {"main": ["SOL"], "sniper": ["M_WIF"]}
        contexts = {"SOL": chart(), "M_WIF": chart()}
        decisions = rule_signals.build_decisions(
            universes, contexts, contexts,
            {"SOL": 100, "M_WIF": 1}, {}, {"M_WIF"})
        self.assertEqual(decisions["main"]["action"], "BUY")
        self.assertEqual(decisions["sniper"]["action"], "BUY")
        self.assertIn("No paid AI used", decisions["main"]["reason"])

    def test_no_signal_stays_hold_and_surfaces_rule_reason(self):
        context = {"SOL": chart(m5=-0.1)}
        decisions = rule_signals.build_decisions(
            {"main": ["SOL"], "sniper": []}, context, {},
            {"SOL": 100}, {}, set())
        self.assertEqual(decisions["main"]["action"], "HOLD")
        self.assertIn("positive 5-minute momentum", decisions["main"]["reason"])

    def test_sniper_requires_current_verified_shortlist(self):
        context = {"M_WIF": chart()}
        decisions = rule_signals.build_decisions(
            {"main": ["SOL"], "sniper": ["M_WIF"]}, {},
            context, {"SOL": 100, "M_WIF": 1}, {}, set())
        self.assertEqual(decisions["sniper"]["action"], "HOLD")
        self.assertIn("verified shortlist", decisions["sniper"]["reason"])

    def test_investor_requires_rising_hourly_trend(self):
        context = {"M_WIF": chart(trend=0)}
        decisions = rule_signals.build_decisions(
            {"main": ["SOL"], "sniper": [], "investor": ["M_WIF"]},
            {}, context, {"SOL": 100, "M_WIF": 1}, {}, {"M_WIF"})
        self.assertEqual(decisions["investor"]["action"], "HOLD")
        self.assertIn("rising setup", decisions["investor"]["reason"])

    def test_server_review_is_bound_to_rule_buy_and_symbol(self):
        context = {"SOL": chart()}
        decisions = {"main": {
            "action": "BUY", "symbol": "SOL", "reason": "validated rule signal"}}
        review = rule_signals.review_decisions(
            "analysis", decisions, context, {}, set(), {"SOL": 100}, 100)
        verdict = ai_risk_review.require_approval(
            review, "analysis", "main", decisions["main"], now=100)
        self.assertEqual(review["analysis_id"], "analysis")
        self.assertTrue(verdict["approved"])


if __name__ == "__main__":
    unittest.main()
