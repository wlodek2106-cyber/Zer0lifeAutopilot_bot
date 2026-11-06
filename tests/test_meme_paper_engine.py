import tempfile
import unittest
from pathlib import Path

from meme_paper_engine import MemePaperEngine


def candidate(symbol="TEST", price=1.0, **overrides):
    item = {
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

    def test_eligible_candidate_creates_only_paper_buy(self):
        report = self.engine.process_snapshot(
            telegram_id=123, candidates=[candidate()], starting_cash=1000, candle_id=1)
        self.assertEqual(report["execution"], "solana_meme_paper_only")
        self.assertEqual(report["live_transactions_sent"], 0)
        self.assertFalse(report["wallet_accessed"])
        self.assertEqual(len(report["actions"]), 1)
        self.assertEqual(report["actions"][0]["action"], "PAPER_BUY")
        self.assertLess(report["cash"], 1000)
        self.assertEqual(len(report["positions"]), 1)

    def test_rejected_candidate_does_not_buy(self):
        report = self.engine.process_snapshot(
            telegram_id=123,
            candidates=[candidate(liquidity_usd=9999)],
            starting_cash=1000, candle_id=1)
        self.assertEqual(report["actions"], [])
        self.assertEqual(report["cash"], 1000)
        self.assertEqual(report["positions"], [])

    def test_duplicate_snapshot_does_not_duplicate_fill(self):
        first = self.engine.process_snapshot(
            telegram_id=123, candidates=[candidate()], starting_cash=1000, candle_id=77)
        second = self.engine.process_snapshot(
            telegram_id=123, candidates=[candidate()], starting_cash=1000, candle_id=77)
        self.assertEqual(first["summary"]["fills"], 1)
        self.assertEqual(second["summary"]["fills"], 1)
        self.assertEqual(second["actions"], [])

    def test_hard_stop_closes_virtual_position(self):
        self.engine.process_snapshot(
            telegram_id=123, candidates=[candidate()], starting_cash=1000, candle_id=1)
        report = self.engine.process_snapshot(
            telegram_id=123, candidates=[candidate(price=0.94)], starting_cash=1000, candle_id=2)
        self.assertEqual(report["actions"][0]["action"], "PAPER_SELL")
        self.assertEqual(report["actions"][0]["reason"], "STOP_LOSS")
        self.assertEqual(report["positions"], [])
        self.assertEqual(report["live_transactions_sent"], 0)

    def test_trailing_stop_uses_persisted_peak_and_does_not_reenter(self):
        self.engine.process_snapshot(
            telegram_id=123, candidates=[candidate()], starting_cash=1000, candle_id=1)
        self.engine.process_snapshot(
            telegram_id=123, candidates=[candidate(price=1.05)], starting_cash=1000, candle_id=2)
        report = self.engine.process_snapshot(
            telegram_id=123, candidates=[candidate(price=1.028)], starting_cash=1000, candle_id=3)
        self.assertEqual(report["actions"][0]["action"], "PAPER_SELL")
        self.assertEqual(report["actions"][0]["reason"], "TRAILING_STOP")
        self.assertEqual(report["positions"], [])
        self.assertEqual(report["live_transactions_sent"], 0)

    def test_users_have_separate_paper_accounts(self):
        self.engine.process_snapshot(
            telegram_id=123, candidates=[candidate()], starting_cash=1000, candle_id=1)
        other = self.engine.process_snapshot(
            telegram_id=456, candidates=[], starting_cash=500, candle_id=1)
        self.assertEqual(other["cash"], 500)
        self.assertEqual(other["positions"], [])

    def test_bad_candidate_input_fails_closed(self):
        self.assertFalse(self.engine.candidate_is_eligible({"eligible": True, "price": float("nan")}))
        with self.assertRaises(ValueError):
            self.engine.process_snapshot(telegram_id=0, candidates=[])


if __name__ == "__main__":
    unittest.main()
