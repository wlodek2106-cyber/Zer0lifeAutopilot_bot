"""Integration coverage for the complete analysis-to-paper-journal cycle."""
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from zerolife_ai_trader.paper_cycle import run_paper_cycle
from zerolife_ai_trader.paper_store import PaperTradeStore
from zerolife_ai_trader.signal_dispatcher import SignalDispatchStore
from zerolife_ai_trader.settings import DEFAULT_SETTINGS


USER_ID = 987654321
NOW = 1_800_000_000.0
ALLOWLIST = {"solana": {"SOLUSDC": "So11111111111111111111111111111111111111112"}}


def settings():
    value = dict(DEFAULT_SETTINGS)
    value.update({
        "symbols": "SOLUSDC",
        "paperAutoExecute": True,
        "capital": 1000.0,
    })
    return value


def candles():
    return {"candles": [
        {"close": 100.0 + index, "timestamp_ms": 1_700_000_000_000 + index * 900_000}
        for index in range(120)
    ]}


def analysis(action):
    return [{
        "symbol": "SOLUSDC",
        "action": action,
        "confidence": 0.8,
        "price": 100.0,
        "reasons": ["integration fixture"],
        "risk": {"allowed": True, "quantity": 2.0, "reason": "test"},
    }]


class PaperCycleIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_analysis_buy_sell_and_journal_are_connected_end_to_end(self):
        with tempfile.TemporaryDirectory() as directory:
            paper = PaperTradeStore(os.path.join(directory, "paper.sqlite3"))
            dispatch = SignalDispatchStore(os.path.join(directory, "dispatch.sqlite3"))
            common = {
                "telegram_id": USER_ID,
                "chain": "solana",
                "settings": settings(),
                "dispatch_store": dispatch,
                "paper_store": paper,
                "token_allowlist": ALLOWLIST,
                "now": NOW,
            }

            with patch(
                "zerolife_ai_trader.paper_cycle.fetch_public_klines",
                new=AsyncMock(return_value=candles()),
            ), patch(
                "zerolife_ai_trader.paper_cycle.fetch_public_price",
                new=AsyncMock(return_value={"symbol": "SOLUSDC", "price": 100.0}),
            ), patch(
                "zerolife_ai_trader.paper_cycle.evaluate_watchlist",
                return_value=analysis("PAPER_BUY"),
            ):
                bought = await run_paper_cycle(**common)
                # Replaying the same candle/action must not create another fill.
                replay = await run_paper_cycle(**common)

            self.assertEqual(bought["executions"][0]["dispatch"]["status"], "PAPER_DONE")
            self.assertTrue(replay["executions"][0]["dispatch"]["duplicate"])
            account_after_buy = paper.account(telegram_id=USER_ID)
            self.assertEqual(len(account_after_buy["positions"]), 1)
            self.assertEqual(len(paper.history(telegram_id=USER_ID)), 1)
            self.assertEqual(paper.history(telegram_id=USER_ID)[0]["side"], "BUY")

            with patch(
                "zerolife_ai_trader.paper_cycle.fetch_public_klines",
                new=AsyncMock(return_value=candles()),
            ), patch(
                "zerolife_ai_trader.paper_cycle.fetch_public_price",
                new=AsyncMock(return_value={"symbol": "SOLUSDC", "price": 110.0}),
            ), patch(
                "zerolife_ai_trader.paper_cycle.evaluate_watchlist",
                return_value=analysis("SELL"),
            ):
                sold = await run_paper_cycle(**common)

            self.assertEqual(sold["executions"][0]["dispatch"]["status"], "PAPER_DONE")
            self.assertEqual(paper.account(telegram_id=USER_ID)["positions"], [])
            history = paper.history(telegram_id=USER_ID)
            self.assertEqual([item["side"] for item in history], ["SELL", "BUY"])
            self.assertGreater(history[0]["realized_pnl"], 0)
            self.assertEqual(paper.summary(telegram_id=USER_ID)["fills"], 2)


if __name__ == "__main__":
    unittest.main()
