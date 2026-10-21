import unittest
import time

import market_context
from market_context import context_from_pairs


class MarketContextTests(unittest.TestCase):
    def setUp(self):
        self.watchlist = {
            "SOL": {"mint": "sol-mint"},
            "WIF": {"mint": "wif-mint"},
        }

    def pair(self, mint, liquidity, volume, **overrides):
        pair = {
            "chainId": "solana",
            "baseToken": {"address": mint},
            "priceUsd": "100",
            "liquidity": {"usd": liquidity},
            "volume": {"h24": volume},
            "priceChange": {"m5": 0.5, "h1": 2, "h6": -1, "h24": 4},
            "txns": {"m5": {"buys": 3, "sells": 1}, "h1": {"buys": 10, "sells": 6}},
        }
        pair.update(overrides)
        return pair

    def test_selects_deepest_eligible_pool_and_only_permitted_symbols(self):
        pairs = [
            self.pair("sol-mint", 150_000, 50_000),
            self.pair("sol-mint", 300_000, 80_000),
            self.pair("sol-mint", 900_000, 9_999),
            self.pair("wif-mint", 500_000, 90_000),
        ]

        result = context_from_pairs(pairs, self.watchlist, ["SOL"])

        self.assertEqual(result, {
            "SOL": {
                "liquidity_usd": 300_000.0,
                "volume_24h_usd": 80_000.0,
                "change_m5_pct": 0.5,
                "change_h1_pct": 2.0,
                "change_h6_pct": -1.0,
                "change_h24_pct": 4.0,
                "buys_m5": 3,
                "sells_m5": 1,
                "buys_h1": 10,
                "sells_h1": 6,
            }
        })

    def test_ignores_wrong_chain_invalid_price_and_non_finite_metrics(self):
        bad_chain = self.pair("sol-mint", 200_000, 30_000, chainId="ethereum")
        bad_price = self.pair("sol-mint", 200_000, 30_000, priceUsd="NaN")
        bad_change = self.pair("sol-mint", 200_000, 30_000,
            priceChange={"m5": float("inf"), "h1": -0.4})

        result = context_from_pairs(
            [bad_chain, bad_price, bad_change], self.watchlist, ["SOL"])

        self.assertEqual(result["SOL"]["change_h1_pct"], -0.4)
        self.assertNotIn("change_m5_pct", result["SOL"])

    def test_pool_address_is_only_exposed_for_internal_candle_fetch(self):
        pair = self.pair("sol-mint", 200_000, 30_000,
                         pairAddress="11111111111111111111111111111111")

        public = context_from_pairs([pair], self.watchlist, ["SOL"])
        internal = context_from_pairs(
            [pair], self.watchlist, ["SOL"], include_pool_address=True)

        self.assertNotIn("_pool_address", public["SOL"])
        self.assertEqual(internal["SOL"]["_pool_address"], pair["pairAddress"])


class ChartContextFetchTests(unittest.IsolatedAsyncioTestCase):
    async def test_fetches_closed_candles_and_removes_internal_pool_address(self):
        now = int(time.time())
        pool = "11111111111111111111111111111111"

        def candles(interval):
            last_closed = now // interval * interval - interval
            return [[last_closed - interval * (59 - i), 100 + i * .02,
                     100.1 + i * .02, 99.9 + i * .02,
                     100 + i * .02, 100] for i in reversed(range(60))]

        class Response:
            status = 200

            def __init__(self, interval):
                self.interval = interval

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            async def json(self):
                return {"data": {"attributes": {"ohlcv_list": candles(self.interval)}}}

        class Session:
            def get(self, url, **kwargs):
                interval = 300 if "/minute" in url else 3600
                return Response(interval)

        market_context._CANDLE_CACHE.clear()
        context = {"SOL": {"_pool_address": pool}}
        await market_context._add_chart_context(Session(), context)

        self.assertEqual(context["SOL"]["chart_ready"], 1)
        self.assertEqual(context["SOL"]["chart_m5_candles"], 60)
        self.assertNotIn("_pool_address", context["SOL"])
