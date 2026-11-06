import unittest
from unittest.mock import AsyncMock, patch

from zerolife_ai_trader import market_adapters


class TechnicalMarketAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_binance_adapter_uses_public_klines_and_normalizes_closed_bars(self):
        rows=[]
        start=1_800_000_000_000
        for n in range(60):
            p=100+n
            rows.append({"timestamp_ms":start+n*300_000,"open":p-0.2,
                         "high":p+0.5,"low":p-0.5,"close":p,"volume":1000+n})
        now=(start/1000)+60*300
        with patch.object(market_adapters,"fetch_public_klines",
                          new=AsyncMock(return_value={"candles":rows,"count":60})):
            with patch.object(market_adapters,"analyze_market",return_value={"action":"HOLD"}) as analyze:
                result=await market_adapters.analyze_binance("BTCUSDT",["5m"],now=now)
        self.assertEqual(result["action"],"HOLD")
        candles=analyze.call_args.args[2]["5m"]
        self.assertEqual(len(candles),59)
        self.assertEqual(candles[-1]["close"],158)

    async def test_binance_rejects_unknown_timeframe(self):
        with self.assertRaisesRegex(ValueError,"Unsupported"):
            await market_adapters.analyze_binance("BTCUSDT",["2m"],now=1_800_000_000)

    async def test_solana_rejects_non_pool_address(self):
        with self.assertRaisesRegex(ValueError,"pool address"):
            await market_adapters.analyze_solana_pool("SOL",["5m"],now=1_800_000_000)


if __name__ == "__main__":
    unittest.main()
