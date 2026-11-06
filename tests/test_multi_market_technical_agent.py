import unittest
from zerolife_ai_trader.technical_agent import analyze_market


def candles(count=60, start=1_800_000_000, step=300, trend=1):
    result=[]
    price=100.0
    for i in range(count):
        price += trend * 0.15
        result.append({"timestamp": start+i*step, "open": price-0.05,
                       "high": price+0.2, "low": price-0.2,
                       "close": price, "volume": 1000+i})
    return result


class MultiMarketTechnicalAgentTests(unittest.TestCase):
    def test_analyzes_both_market_adapters_with_same_ohlcv_contract(self):
        now=1_800_000_000+59*3600+1
        for market in ("solana", "binance"):
            result=analyze_market(market, "SOL/USDC",
                {"5m": candles(start=1_800_000_000),
                 "1h": candles(start=1_800_000_000, step=3600)},
                now=now, max_age_seconds=7200)
            self.assertEqual(result["market"], market)
            self.assertIn(result["action"], {"BUY_WATCH", "SELL_WATCH", "HOLD"})
            self.assertIn("rsi14", result["timeframes"]["5m"])
            self.assertEqual(result["execution_authority"], "none")

    def test_stale_candles_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "stale"):
            analyze_market("binance", "BTCUSDT",
                {"5m": candles()}, now=1_900_000_000)

    def test_rejects_bad_ohlcv_and_unordered_timestamps(self):
        bad=candles()
        bad[-1]["high"]=1
        with self.assertRaises(ValueError):
            analyze_market("solana", "TOKEN", {"5m": bad},
                           now=1_800_000_000+59*300+1, max_age_seconds=7200)
        bad=candles()
        bad[-1]["timestamp"]=bad[-2]["timestamp"]
        with self.assertRaisesRegex(ValueError, "strictly"):
            analyze_market("solana", "TOKEN", {"5m": bad},
                           now=1_800_000_000+59*300+1, max_age_seconds=7200)

    def test_requires_multiple_timeframes_for_actionable_watch(self):
        result=analyze_market("binance", "ETHUSDT",
            {"5m": candles()}, now=1_800_000_000+59*300+1,
            max_age_seconds=7200)
        self.assertEqual(result["action"], "HOLD")
        self.assertFalse(result["timeframes_aligned"])


if __name__ == "__main__":
    unittest.main()
