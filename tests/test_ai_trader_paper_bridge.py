import os
import tempfile
import unittest

from zerolife_ai_trader.paper_store import PaperTradeStore
from zerolife_ai_trader.signal_dispatcher import SignalDispatchStore
from zerolife_ai_trader.paper_bridge import execute_paper_signal

ALLOWLIST = {"solana": {"SOLUSDC": "So11111111111111111111111111111111111111112"},
             "base": {"ETHUSDC": "0x0000000000000000000000000000000000000001"}}

class PaperBridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dispatch = SignalDispatchStore(os.path.join(self.tmp.name, "dispatch.sqlite"))
        self.paper = PaperTradeStore(os.path.join(self.tmp.name, "paper.sqlite"))

    def tearDown(self):
        self.tmp.cleanup()

    def base_signal(self, **changes):
        value = {"telegram_id": 77, "chain": "solana", "symbol": "SOLUSDC",
                 "side": "BUY", "timeframe": "5m", "candle_id": "candle-1",
                 "observed_at": 1000.0, "confidence": 0.8, "amount_usdc": 100.0}
        value.update(changes)
        return value

    def run_signal(self, signal, price=10.0):
        return execute_paper_signal(signal, dispatch_store=self.dispatch,
            paper_store=self.paper, token_allowlist=ALLOWLIST, market_price=price,
            fee_bps=10, slippage_bps=5, starting_cash=500, now=1000)

    def test_buy_creates_persistent_paper_position_and_fill(self):
        result = self.run_signal(self.base_signal())
        self.assertEqual(result["status"], "PAPER_DONE")
        account = self.paper.account(telegram_id=77)
        self.assertEqual(len(account["positions"]), 1)
        self.assertEqual(account["positions"][0]["symbol"], "SOLANA:SOLUSDC")
        self.assertAlmostEqual(account["positions"][0]["quantity"], 10.0)
        self.assertLess(account["cash"], 400.0)
        self.assertEqual(self.paper.history(telegram_id=77)[0]["side"], "BUY")

    def test_duplicate_signal_does_not_create_second_fill(self):
        signal = self.base_signal()
        first = self.run_signal(signal)
        second = self.run_signal(signal)
        self.assertEqual(first["status"], "PAPER_DONE")
        self.assertTrue(second["duplicate"])
        self.assertEqual(len(self.paper.history(telegram_id=77)), 1)

    def test_sell_requires_matching_existing_paper_position(self):
        self.run_signal(self.base_signal())
        sell = self.base_signal(side="SELL", candle_id="candle-2",
            position_id="paper:solana:SOLUSDC", quantity=4.0)
        sell.pop("amount_usdc")
        result = self.run_signal(sell, price=12.0)
        self.assertEqual(result["status"], "PAPER_DONE")
        account = self.paper.account(telegram_id=77)
        self.assertAlmostEqual(account["positions"][0]["quantity"], 6.0)
        self.assertEqual(self.paper.history(telegram_id=77)[0]["side"], "SELL")

    def test_sell_cannot_reference_another_symbol_position(self):
        self.run_signal(self.base_signal())
        sell = self.base_signal(side="SELL", candle_id="candle-3",
            position_id="paper:base:ETHUSDC", quantity=1.0)
        sell.pop("amount_usdc")
        with self.assertRaisesRegex(ValueError, "matching user's paper position"):
            self.run_signal(sell)

    def test_sell_cannot_exceed_held_quantity(self):
        self.run_signal(self.base_signal())
        sell = self.base_signal(side="SELL", candle_id="candle-4",
            position_id="paper:SOLUSDC", quantity=11.0)
        sell.pop("amount_usdc")
        with self.assertRaisesRegex(ValueError, "more than the paper position"):
            self.run_signal(sell)

    def test_invalid_price_rejected_before_claiming_signal(self):
        with self.assertRaises(ValueError):
            execute_paper_signal(self.base_signal(), dispatch_store=self.dispatch,
                paper_store=self.paper, token_allowlist=ALLOWLIST,
                market_price=float("nan"), now=1000)
        self.assertEqual(self.paper.history(telegram_id=77), [])

if __name__ == "__main__":
    unittest.main()
