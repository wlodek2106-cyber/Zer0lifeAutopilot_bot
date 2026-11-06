import os
import tempfile
import unittest

from zerolife_ai_trader.paper_store import PaperTradeStore


class DailyRealizedPnlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = PaperTradeStore(os.path.join(self.tmp.name, "paper.sqlite"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_daily_realized_pnl_sums_only_current_utc_date(self):
        self.store.record_fill(
            telegram_id=42, symbol="SOLANA:SOLUSDC", side="SELL",
            quantity=1.0, price=110.0, fee=1.0,
            realized_pnl=9.0, cash_after=500.0, fill_id="today-1",
        )
        self.store.record_fill(
            telegram_id=42, symbol="SOLANA:SOLUSDC", side="SELL",
            quantity=1.0, price=90.0, fee=1.0,
            realized_pnl=-11.0, cash_after=400.0, fill_id="today-2",
        )
        self.store.record_fill(
            telegram_id=99, symbol="SOLANA:SOLUSDC", side="SELL",
            quantity=1.0, price=100.0, fee=1.0,
            realized_pnl=100.0, cash_after=900.0, fill_id="other-user",
        )
        self.assertAlmostEqual(self.store.daily_realized_pnl(telegram_id=42), -2.0)

    def test_daily_realized_pnl_rejects_invalid_owner(self):
        with self.assertRaises(ValueError):
            self.store.daily_realized_pnl(telegram_id=0)


if __name__ == "__main__":
    unittest.main()
