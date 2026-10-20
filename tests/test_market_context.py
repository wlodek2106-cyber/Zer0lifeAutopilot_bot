import unittest

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
