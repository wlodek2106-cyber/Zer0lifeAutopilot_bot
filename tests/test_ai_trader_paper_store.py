import os
import tempfile
import unittest

from zerolife_ai_trader.paper_store import PaperTradeStore


class PaperTradeStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = PaperTradeStore(os.path.join(self.temp.name, "paper.sqlite3"))

    def tearDown(self):
        self.temp.cleanup()

    def test_records_and_summarizes_fills_per_user(self):
        self.store.record_fill(
            telegram_id=101, symbol="solusdc", side="BUY", quantity=2,
            price=100, fee=0.2, cash_after=799.8, fill_id="buy-1",
        )
        self.store.record_fill(
            telegram_id=101, symbol="SOLUSDC", side="SELL", quantity=1,
            price=110, fee=0.11, realized_pnl=9.69,
            cash_after=909.69, fill_id="sell-1",
        )
        self.store.record_fill(
            telegram_id=202, symbol="BTCUSDT", side="BUY", quantity=0.01,
            price=50000, fee=0.5, cash_after=499.5, fill_id="other-user",
        )
        self.assertEqual(self.store.summary(telegram_id=101),
                         {"fills": 2, "fees_paid": 0.31, "realized_pnl": 9.69})
        history = self.store.history(telegram_id=101)
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["id"], "sell-1")
        self.assertEqual(history[0]["symbol"], "SOLUSDC")
        self.assertEqual(len(self.store.history(telegram_id=202)), 1)

    def test_history_is_immutable(self):
        self.store.record_fill(
            telegram_id=101, symbol="BTCUSDT", side="BUY", quantity=0.1,
            price=50000, fee=0.5, cash_after=499.5, fill_id="immutable",
        )
        with self.store._connect() as db:
            with self.assertRaisesRegex(Exception, "immutable"):
                db.execute("UPDATE ai_trader_paper_fills SET fee=0 WHERE id='immutable'")
            with self.assertRaisesRegex(Exception, "immutable"):
                db.execute("DELETE FROM ai_trader_paper_fills WHERE id='immutable'")

    def test_duplicate_fill_id_is_rejected(self):
        args = dict(telegram_id=101, symbol="ETHUSDT", side="BUY",
                    quantity=1, price=100, fee=0, cash_after=900,
                    fill_id="once")
        self.store.record_fill(**args)
        with self.assertRaises(Exception):
            self.store.record_fill(**args)
        self.assertEqual(self.store.summary(telegram_id=101)["fills"], 1)

    def test_invalid_values_fail_closed(self):
        base = dict(telegram_id=101, symbol="BTCUSDT", side="BUY",
                    quantity=1, price=100, fee=0, cash_after=900)
        invalid = (
            {"telegram_id": True}, {"symbol": ""}, {"side": "HOLD"},
            {"quantity": float("nan")}, {"price": float("inf")},
            {"fee": -1}, {"cash_after": -1}, {"realized_pnl": float("nan")},
        )
        for change in invalid:
            with self.subTest(change=change):
                with self.assertRaises(ValueError):
                    self.store.record_fill(**(base | change))

    def test_empty_account_is_safe_to_read_before_first_fill(self):
        account = self.store.account(telegram_id=101)
        self.assertIsNone(account["cash"])
        self.assertEqual(account["positions"], [])

    def test_execute_buy_persists_account_position_and_fill_once(self):
        first = self.store.execute_buy(
            telegram_id=101, signal_id=44, symbol="SOLUSDC", quantity=2,
            market_price=100, fee_bps=10, slippage_bps=5, starting_cash=1000,
        )
        self.assertTrue(first["executed"])
        self.assertFalse(first["duplicate"])
        self.assertAlmostEqual(first["fill"]["price"], 100.05)
        self.assertAlmostEqual(first["fill"]["fee"], 0.2001)
        self.assertAlmostEqual(first["fill"]["cash_after"], 799.6999)
        account = self.store.account(telegram_id=101)
        self.assertAlmostEqual(account["cash"], 799.6999)
        self.assertEqual(len(account["positions"]), 1)
        self.assertEqual(account["positions"][0]["symbol"], "SOLUSDC")
        second = self.store.execute_buy(
            telegram_id=101, signal_id=44, symbol="SOLUSDC", quantity=2,
            market_price=100, fee_bps=10, slippage_bps=5, starting_cash=1000,
        )
        self.assertFalse(second["executed"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(self.store.summary(telegram_id=101)["fills"], 1)

    def test_execute_buy_is_scoped_and_rejects_insufficient_cash(self):
        self.store.execute_buy(
            telegram_id=101, signal_id=1, symbol="BTCUSDT", quantity=0.01,
            market_price=50000, fee_bps=10, slippage_bps=5, starting_cash=1000,
        )
        self.assertIsNone(self.store.account(telegram_id=202)["cash"])
        with self.assertRaisesRegex(ValueError, "Insufficient paper cash"):
            self.store.execute_buy(
                telegram_id=101, signal_id=2, symbol="BTCUSDT", quantity=1,
                market_price=50000, fee_bps=10, slippage_bps=5, starting_cash=1000,
            )
        self.assertEqual(self.store.summary(telegram_id=101)["fills"], 1)

    def test_limit_is_bounded(self):
        with self.assertRaises(ValueError):
            self.store.history(telegram_id=101, limit=201)
        with self.assertRaises(ValueError):
            self.store.history(telegram_id=101, limit=True)


if __name__ == "__main__":
    unittest.main()
