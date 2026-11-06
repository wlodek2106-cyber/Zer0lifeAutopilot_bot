import os
import tempfile
import unittest

from zerolife_ai_trader.signal_dispatcher import (
    SignalDispatchError, SignalDispatchStore, dispatch_signal, prepare_signal,
)


ALLOWLIST = {
    "solana": {"SOLUSDC": "So11111111111111111111111111111111111111112"},
    "base": {"ETHUSDC": "0x0000000000000000000000000000000000000001"},
}


def signal(**changes):
    value = {
        "telegram_id": 123, "chain": "solana", "symbol": "SOLUSDC",
        "side": "BUY", "timeframe": "5m", "candle_id": "candle-100",
        "observed_at": 1000.0, "confidence": 0.8, "amount_usdc": 25.0,
    }
    value.update(changes)
    return value


class SignalDispatcherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = SignalDispatchStore(os.path.join(self.tmp.name, "signals.sqlite"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_valid_buy_resolves_server_owned_token_address(self):
        plan = prepare_signal(signal(), token_allowlist=ALLOWLIST, now=1001)
        self.assertEqual(plan["token_address"], ALLOWLIST["solana"]["SOLUSDC"])
        self.assertEqual(plan["mode"], "paper_candidate")

    def test_stale_or_future_signal_rejected(self):
        with self.assertRaisesRegex(SignalDispatchError, "stale"):
            prepare_signal(signal(observed_at=800), token_allowlist=ALLOWLIST, now=1000)
        with self.assertRaisesRegex(SignalDispatchError, "future"):
            prepare_signal(signal(observed_at=1010), token_allowlist=ALLOWLIST, now=1000)

    def test_unknown_chain_symbol_or_side_rejected(self):
        for changes in ({"chain": "ethereum"}, {"symbol": "SCAM"},
                        {"side": "SHORT"}):
            with self.subTest(changes=changes), self.assertRaises(SignalDispatchError):
                prepare_signal(signal(**changes), token_allowlist=ALLOWLIST, now=1000)

    def test_sell_requires_position_reference_and_quantity(self):
        value = signal(side="SELL")
        value.pop("amount_usdc")
        with self.assertRaisesRegex(SignalDispatchError, "position_id"):
            prepare_signal(value, token_allowlist=ALLOWLIST, now=1000)
        value["position_id"] = "position-1"
        with self.assertRaisesRegex(SignalDispatchError, "quantity"):
            prepare_signal(value, token_allowlist=ALLOWLIST, now=1000)
        value["quantity"] = 1.0
        plan = prepare_signal(value, token_allowlist=ALLOWLIST, now=1000)
        self.assertEqual(plan["position_id"], "position-1")

    def test_signal_key_can_be_claimed_only_once(self):
        plan = prepare_signal(signal(), token_allowlist=ALLOWLIST, now=1000)
        self.assertTrue(self.store.claim(plan))
        self.assertFalse(self.store.claim(plan))

    def test_live_mode_fails_closed_even_with_valid_signal(self):
        result = dispatch_signal(signal(), store=self.store,
                                 token_allowlist=ALLOWLIST, now=1000,
                                 live_enabled=True)
        self.assertFalse(result["accepted"])
        self.assertIn("not connected", result["reason"])
        self.assertEqual(self.store.get(telegram_id=123,
                         signal_key=result["plan"]["signal_key"])["status"], "BLOCKED")

    def test_paper_executor_runs_once(self):
        calls = []
        kwargs = dict(store=self.store, token_allowlist=ALLOWLIST, now=1000,
                      paper_executor=lambda plan: calls.append(plan["side"]) or {"ok": True})
        first = dispatch_signal(signal(), **kwargs)
        second = dispatch_signal(signal(), **kwargs)
        self.assertEqual(first["status"], "PAPER_DONE")
        self.assertTrue(second["duplicate"])
        self.assertEqual(calls, ["BUY"])

    def test_sell_deduplication_is_separate_from_buy(self):
        buy = prepare_signal(signal(), token_allowlist=ALLOWLIST, now=1000)
        sell_signal = signal(side="SELL", position_id="pos-1", quantity=0.5)
        sell_signal.pop("amount_usdc")
        sell = prepare_signal(sell_signal, token_allowlist=ALLOWLIST, now=1000)
        self.assertNotEqual(buy["signal_key"], sell["signal_key"])


if __name__ == "__main__":
    unittest.main()
