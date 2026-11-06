import tempfile
import unittest
from pathlib import Path

from meme_paper_engine import MemePaperEngine


MINT_A = "So11111111111111111111111111111111111111112"
MINT_B = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


def candidate(mint=MINT_A, symbol="SAME", price=1.0, **overrides):
    item = {
        "mint": mint, "code": "DISC_" + mint[-6:],
        "symbol": symbol, "price": price, "eligible": True,
        "liquidity_usd": 25_000, "volume_24h_usd": 150_000,
        "transactions_24h": 400, "age_hours": 48,
        "change_m5_pct": 1.0, "change_h1_pct": 2.0,
    }
    item.update(overrides)
    return item


class MemePaperEngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = MemePaperEngine(str(Path(self.tmp.name) / "paper.sqlite3"))

    def tearDown(self):
        self.tmp.cleanup()

    def run_snapshot(self, candidates, snapshot, **kwargs):
        return self.engine.process_snapshot(
            telegram_id=123, candidates=candidates, starting_cash=1000,
            snapshot_id=snapshot, now=snapshot, **kwargs)

    def test_eligible_candidate_creates_only_paper_buy(self):
        report = self.run_snapshot([candidate()], 1)
        self.assertEqual(report["execution"], "solana_meme_paper_only")
        self.assertEqual(report["live_transactions_sent"], 0)
        self.assertFalse(report["wallet_accessed"])
        self.assertEqual(report["actions"][0]["action"], "PAPER_BUY")
        self.assertLess(report["cash"], 1000)
        self.assertEqual(report["positions"][0]["mint"], MINT_A)

    def test_same_ticker_different_mints_are_distinct_positions(self):
        report = self.run_snapshot([candidate(MINT_A), candidate(MINT_B)], 1)
        self.assertEqual(len(report["positions"]), 2)
        self.assertEqual({p["mint"] for p in report["positions"]}, {MINT_A, MINT_B})

    def test_rejected_candidate_does_not_buy(self):
        report = self.run_snapshot([candidate(liquidity_usd=9999)], 1)
        self.assertEqual(report["actions"], [])
        self.assertEqual(report["cash"], 1000)
        self.assertEqual(report["positions"], [])

    def test_duplicate_snapshot_does_not_duplicate_fill(self):
        first = self.run_snapshot([candidate()], 77)
        second = self.run_snapshot([candidate()], 77)
        self.assertEqual(first["summary"]["fills"], 1)
        self.assertEqual(second["summary"]["fills"], 1)
        self.assertEqual(second["actions"], [])

    def test_stop_loss_is_checked_even_when_mint_leaves_shortlist(self):
        self.run_snapshot([candidate()], 1)
        report = self.run_snapshot([], 2, market_prices={MINT_A: 0.94})
        self.assertEqual(report["actions"][0]["action"], "PAPER_SELL")
        self.assertEqual(report["actions"][0]["reason"], "STOP_LOSS")
        self.assertEqual(report["positions"], [])

    def test_missing_held_price_is_reported_not_silently_ignored(self):
        self.run_snapshot([candidate()], 1)
        report = self.run_snapshot([], 2)
        self.assertEqual(len(report["positions"]), 1)
        self.assertEqual(report["stale_positions"][0]["reason"], "fresh_price_unavailable")

    def test_trailing_stop_uses_persisted_peak_and_does_not_reenter(self):
        self.run_snapshot([candidate()], 1)
        self.run_snapshot([candidate(price=1.05)], 2)
        report = self.run_snapshot([], 3, market_prices={MINT_A: 1.028})
        self.assertEqual(report["actions"][0]["reason"], "TRAILING_STOP")
        self.assertEqual(report["positions"], [])

    def test_initial_capital_and_daily_loss_limit_cannot_be_reset(self):
        self.run_snapshot([candidate()], 1)
        report = self.engine.process_snapshot(
            telegram_id=123, candidates=[], starting_cash=100_000,
            market_prices={MINT_A: 0.94}, snapshot_id=2, now=2)
        self.assertEqual(report["starting_cash"], 1000)
        self.assertEqual(report["daily_loss_limit_hit"], False)

    def test_accounts_are_user_scoped(self):
        self.run_snapshot([candidate()], 1)
        other = self.engine.process_snapshot(
            telegram_id=456, candidates=[], starting_cash=500, snapshot_id=1)
        self.assertEqual(other["cash"], 500)
        self.assertEqual(other["positions"], [])

    def test_bad_or_missing_mint_fails_closed(self):
        self.assertFalse(self.engine.candidate_is_eligible(candidate(mint="not-a-mint")))
        with self.assertRaises(ValueError):
            self.engine.process_snapshot(telegram_id=0, candidates=[])


if __name__ == "__main__":
    unittest.main()
