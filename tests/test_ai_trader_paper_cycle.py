import unittest
from unittest.mock import AsyncMock, patch

from zerolife_ai_trader.paper_cycle import run_paper_cycle
from zerolife_ai_trader.settings import DEFAULT_SETTINGS


class FakePaperStore:
    def account(self, *, telegram_id):
        return {"cash": None, "positions": []}


class FakeDispatchStore:
    pass


def settings(**changes):
    value = dict(DEFAULT_SETTINGS)
    value.update(changes)
    return value


def candles():
    closes = [100 + i * 0.2 for i in range(120)]
    return {"candles": [{"close": price, "timestamp_ms": i * 900000}
                        for i, price in enumerate(closes)]}


class PaperCycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_cycle_fetches_market_history_and_returns_analysis(self):
        with patch("zerolife_ai_trader.paper_cycle.fetch_public_klines",
                   new=AsyncMock(return_value=candles())) as fetch:
            result = await run_paper_cycle(
                telegram_id=9, chain="solana", settings=settings(),
                dispatch_store=FakeDispatchStore(), paper_store=FakePaperStore(),
                token_allowlist={"solana": {"SOLUSDC": "mint"}}, now=1_800_000_000,
            )
        self.assertEqual(result["source"], "binance_public")
        self.assertEqual(len(result["analysis"]), 3)
        self.assertEqual(result["executions"], [])
        self.assertEqual(fetch.await_count, 3)

    async def test_cycle_does_not_execute_when_paper_auto_execute_off(self):
        with patch("zerolife_ai_trader.paper_cycle.fetch_public_klines",
                   new=AsyncMock(return_value=candles())), \
             patch("zerolife_ai_trader.paper_cycle.execute_paper_signal") as execute:
            await run_paper_cycle(
                telegram_id=9, chain="base",
                settings=settings(paperAutoExecute=False),
                dispatch_store=FakeDispatchStore(), paper_store=FakePaperStore(),
                token_allowlist={"base": {}}, now=1_800_000_000,
            )
        execute.assert_not_called()

    async def test_rejects_non_binance_source_and_bad_chain(self):
        common = dict(telegram_id=9, dispatch_store=FakeDispatchStore(),
                      paper_store=FakePaperStore(), token_allowlist={})
        with self.assertRaisesRegex(ValueError, "chain"):
            await run_paper_cycle(chain="ethereum", settings=settings(), **common)
        with self.assertRaisesRegex(ValueError, "Binance"):
            await run_paper_cycle(chain="solana", settings=settings(source="csv"), **common)


if __name__ == "__main__":
    unittest.main()
