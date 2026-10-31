import asyncio
import os
import tempfile
import unittest

import base_paper


OWNER_ID = 17
TOKEN_A = "0x" + "1" * 40
TOKEN_B = "0x" + "2" * 40
POOL_A = "0x" + "a" * 40


def valid_candidate(address=TOKEN_A, symbol="TEST", price=1.0, candle=700_000):
    return {
        "token_address": address,
        "pool_address": POOL_A,
        "symbol": symbol,
        "quote_asset": "USDC",
        "price_usd": price,
        "liquidity_usd": 500_000,
        "volume_24h_usd": 1_000_000,
        "transactions_24h": 500,
        "age_hours": 48,
        "change_m5_pct": 0.5,
        "change_h1_pct": 1.0,
        "chart_ready": 1,
        "entry_eligible": True,
        "rejection_reasons": [],
        "metrics": {
            "chart_ready": 1,
            "chart_buy_setup": 1,
            "chart_trend_h1": 1,
            "change_m5_pct": 0.5,
            "change_h1_pct": 1.0,
            "change_h6_pct": 0.5,
            "chart_m5_closed_at": candle,
        },
    }


def gecko_pool(symbol="TEST", quote_address=base_paper.BASE_USDC, address=TOKEN_A):
    quote_id = "base_" + quote_address
    token_id = "base_" + address
    pool = {
        "id": "base_" + POOL_A,
        "attributes": {
            "address": POOL_A,
            "base_token_price_usd": "1.25",
            "quote_token_price_usd": "1",
            "reserve_in_usd": "500000",
            "volume_usd": {"h24": "1000000"},
            "transactions": {"h24": {"buys": 300, "sells": 250}},
            "pool_created_at": "2026-10-28T00:00:00Z",
            "price_change_percentage": {"m5": "0", "h1": "1.4", "h6": "2.0"},
        },
        "relationships": {
            "base_token": {"data": {"id": token_id}},
            "quote_token": {"data": {"id": quote_id}},
        },
    }
    included = {
        token_id: {"address": address, "symbol": symbol, "name": symbol + " Token"},
        quote_id: {
            "address": quote_address,
            "symbol": "USDC" if quote_address == base_paper.BASE_USDC else "WETH",
            "name": "USD Coin" if quote_address == base_paper.BASE_USDC else "Wrapped Ether",
        },
    }
    return pool, included


class BasePoolFilteringTests(unittest.TestCase):
    def test_only_exact_base_usdc_and_weth_quotes_are_accepted(self):
        for quote in (base_paper.BASE_USDC, base_paper.BASE_WETH):
            pool, included = gecko_pool(quote_address=quote)
            candidate = base_paper.pool_candidate(pool, included, now=1_798_000_000)
            self.assertIsNotNone(candidate)
            self.assertEqual(candidate["quote_asset"], base_paper.BASE_QUOTES[quote])
        pool, included = gecko_pool(quote_address="0x" + "9" * 40)
        self.assertIsNone(base_paper.pool_candidate(pool, included, now=1_798_000_000))

    def test_equity_tokens_and_ticker_clones_are_excluded(self):
        for symbol in ("AAPLx", "TSLAc", "XSTOCK"):
            with self.subTest(symbol=symbol):
                pool, included = gecko_pool(symbol=symbol)
                candidate = base_paper.pool_candidate(pool, included, now=1_798_000_000)
                self.assertIsNotNone(candidate)
                self.assertTrue(any("stocks and ETFs" in reason
                                    for reason in candidate["rejection_reasons"]))

    def test_zero_price_change_is_valid_data_but_not_an_entry_signal(self):
        pool, included = gecko_pool()
        candidate = base_paper.pool_candidate(pool, included, now=1_798_000_000)
        self.assertTrue(candidate["market_metrics_present"])
        self.assertFalse(any("price-change data is unavailable" in reason
                             for reason in candidate["rejection_reasons"]))


class BasePaperLedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.engine = base_paper.BasePaperEngine(
            os.path.join(self.temp_dir.name, "paper.sqlite"), OWNER_ID)

    def test_budget_is_virtual_and_isolated_by_user(self):
        report = self.engine.report(OWNER_ID, now=1_000_000)
        self.assertTrue(report["paper_only"])
        self.assertFalse(report["live_trading"])
        self.assertEqual(report["budget_usd"], 100)
        other = self.engine.report(OWNER_ID + 1, now=1_000_000)
        self.assertEqual(other["budget_usd"], 0)
        self.assertEqual(other["open_positions"], [])

    def test_non_owner_cannot_start_base_market_scans(self):
        with self.assertRaises(base_paper.BasePaperError) as caught:
            asyncio.run(self.engine.scan(None, OWNER_ID + 1))
        self.assertEqual(caught.exception.status, 403)

    def test_tranches_are_candle_gated_and_no_stop_loss_is_used(self):
        now = 1_000_000
        first = self.engine.apply_snapshot(
            OWNER_ID, [valid_candidate(candle=700_000)], now=now)
        self.assertEqual(first["open_lot_count"], 1)
        first_cost = first["open_cost_usd"]
        self.assertAlmostEqual(first_cost, 100 / 15, places=6)

        # A retry on the same closed candle cannot buy the next tranche again.
        same_candle = self.engine.apply_snapshot(
            OWNER_ID, [valid_candidate(price=1.01, candle=700_000)], now=now + 1)
        self.assertEqual(same_candle["open_lot_count"], 1)

        # A later closed candle can add only above the preceding tranche's price.
        second = self.engine.apply_snapshot(
            OWNER_ID, [valid_candidate(price=1.02, candle=700_300)], now=now + 2)
        self.assertEqual(second["open_lot_count"], 2)

        # A sharp fall does not stop or close either paper tranche.
        falling = self.engine.apply_snapshot(
            OWNER_ID, [valid_candidate(price=0.70, candle=700_600)], now=now + 3)
        self.assertEqual(falling["open_lot_count"], 2)
        self.assertEqual(falling["realized_gross_pnl_usd"], 0)

        # Tranche one exits at its target; tranche two stays open.
        exit_only_snapshot = valid_candidate(price=1.04, candle=700_600)
        exit_only_snapshot["chart_ready"] = 0
        exit_only_snapshot["rejection_reasons"] = ["Fresh chart confirmation unavailable."]
        exit_only_snapshot["metrics"]["chart_ready"] = 0
        take_profit = self.engine.apply_snapshot(
            OWNER_ID, [exit_only_snapshot], now=now + 4)
        self.assertEqual(take_profit["open_lot_count"], 1)
        self.assertAlmostEqual(take_profit["realized_gross_pnl_usd"],
                               first_cost * 0.04, places=6)
        self.assertEqual(take_profit["closed_trades"][0]["exit_reason"],
                         "INVESTOR_TAKE_PROFIT_STEP_1")

    def test_five_slot_cap_and_budget_below_open_cost_are_enforced(self):
        candidates = [
            valid_candidate("0x" + format(index, "x") * 40,
                            symbol="T" + str(index), candle=700_000 + index * 300)
            for index in range(1, 7)
        ]
        report = self.engine.apply_snapshot(OWNER_ID, candidates, now=1_000_000)
        self.assertEqual(report["open_cycle_count"], base_paper.MAX_SLOTS)
        self.assertEqual(report["open_lot_count"], base_paper.MAX_SLOTS)
        blocked = next(item for item in report["candidates"]
                       if item["symbol"] == "T6")
        self.assertIn("slots are occupied", blocked["signal_reason"])
        with self.assertRaisesRegex(ValueError, "below open paper-position cost"):
            self.engine.set_budget(OWNER_ID, 1, now=1_000_001)


if __name__ == "__main__":
    unittest.main()
