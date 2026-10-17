import copy
import json
import time
import unittest
from solders.signature import Signature
from unittest.mock import AsyncMock

from entry_costs import assess_entry, quote_amounts, EntryCostError
from custody_ledger import USDC
from custody_runtime import WRAPPED_SOL
import test_custody as fixtures

JTO = "jtojtomepa8beP8AuQc6eXt5FriJwfFMwQx2v2f9mCL"
ORCA = "orcaEKTdK7LKz57vaAYr9QeNsVEPfiu6QeMU1kektZE"


def quote(source=USDC, dest=JTO, amount=30_000_000, output=50_000_000_000):
    return {"inputMint": source, "outputMint": dest, "inAmount": str(amount),
            "outAmount": str(output), "otherAmountThreshold": str(output * 996 // 1000),
            "swapMode": "ExactIn", "slippageBps": 40, "priceImpactPct": "0.01",
            "contextSlot": 100, "routePlan": [{"swapInfo": {"label": "fixture"}}]}


class CostMathTests(unittest.TestCase):
    def context(self, **kwargs):
        return {"input_raw": 30_000_000, "exit_minimum_raw": 29_760_000,
                "quoted_at": 1000, "sol_usdc": "100", "exit_rent_lamports": 2_039_280, **kwargs}

    def test_includes_both_routes_network_tip_and_rent(self):
        result = assess_entry(self.context(), 2_119_280, now=1001)
        self.assertEqual(result["status"], "ALLOW")
        self.assertEqual(result["route_loss_usdc"], .24)
        self.assertEqual(result["network_and_rent_usdc"], .423856)
        self.assertAlmostEqual(result["estimated_cost_usdc"], .663856)

    def test_small_orders_fail_cost_budget(self):
        result = assess_entry(self.context(input_raw=1_000_000, exit_minimum_raw=992000), 2_119_280, now=1001)
        self.assertEqual(result["status"], "SKIP")

    def test_exact_budget_boundary(self):
        context = self.context(exit_minimum_raw=29_250_082, sol_usdc="1", exit_rent_lamports=1)
        self.assertEqual(assess_entry(context, 1000, now=1001)["status"], "ALLOW")
        context["exit_minimum_raw"] -= 1
        self.assertEqual(assess_entry(context, 1000, now=1001)["status"], "SKIP")

    def test_expired_future_missing_and_invalid_data_fail_closed(self):
        for change in ({"quoted_at": 960}, {"quoted_at": 1002}, {"sol_usdc": "NaN"},
                       {"sol_usdc": "Infinity"}, {"sol_usdc": 0}, {"exit_rent_lamports": None},
                       {"exit_minimum_raw": 0}, {"input_raw": True}):
            with self.subTest(change=change), self.assertRaises(EntryCostError):
                assess_entry(self.context(**change), 80000, now=1001)

    def test_roundtrip_gain_does_not_cancel_network_costs(self):
        result = assess_entry(self.context(exit_minimum_raw=40_000_000), 80000, now=1001)
        self.assertEqual(result["route_loss_usdc"], 0)
        self.assertGreater(result["estimated_cost_usdc"], 0)

    def test_invalid_quote_fields_and_impact_are_blocked(self):
        for change in ({"priceImpactPct": "NaN"}, {"priceImpactPct": "0.51"}, {"routePlan": []},
                       {"slippageBps": 41}, {"inputMint": ORCA}, {"inAmount": "1"},
                       {"otherAmountThreshold": "0"}, {"contextSlot": None}, {"swapMode": "ExactOut"}):
            with self.subTest(change=change), self.assertRaises(EntryCostError):
                quote_amounts({**quote(), **change}, USDC, JTO, 30_000_000)

    def test_correct_quote_is_accepted(self):
        self.assertEqual(quote_amounts(quote(), USDC, JTO, 30_000_000), (50_000_000_000, 49_800_000_000))


class QuoteSession:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(kwargs)
        return fixtures.NotificationSession(result=self.results.pop(0))


class CostRuntimeTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.RuntimeTests.setUp
    rpc = fixtures.RuntimeTests.rpc

    async def test_jto_and_orca_can_quote_core_entries_without_any_send(self):
        for mint in (JTO, ORCA):
            forward = quote(dest=mint)
            minimum = int(forward["otherAmountThreshold"])
            reverse = quote(mint, USDC, minimum, 29_900_000)
            reference = quote(WRAPPED_SOL, USDC, 1_000_000_000, 100_000_000)
            session = QuoteSession([reverse, reference])
            self.service.rpc = AsyncMock(return_value=2_039_280)
            context = await self.service.core_entry_cost_context(
                session, 101, forward, 30_000_000, mint, {}, "JTO" if mint == JTO else "ORCA")
            self.assertEqual(context["sol_usdc"], "100")
            self.assertEqual(session.calls[0]["params"]["amount"], minimum)
            self.assertEqual(assess_entry(context, 80000)["status"], "ALLOW")
            self.assertEqual(self.ledger.pending(), [])

    async def test_bad_reverse_quote_records_skip_only_for_owner(self):
        reverse = quote(JTO, USDC, 49_800_000_000, 29_900_000)
        reverse["priceImpactPct"] = "5"
        with self.assertRaises(EntryCostError):
            await self.service.core_entry_cost_context(
                QuoteSession([reverse]), 101, quote(), 30_000_000, JTO, {}, "JTO")
        self.assertEqual(self.ledger.entry_cost_report(101)["status"], "SKIP")
        self.assertIsNone(self.ledger.entry_cost_report(202))
        self.assertEqual(self.ledger.pending(), [])

    async def test_old_reverse_quote_fails_closed(self):
        reverse = quote(JTO, USDC, 49_800_000_000, 29_900_000)
        reverse["contextSlot"] = 200
        with self.assertRaises(EntryCostError):
            await self.service.core_entry_cost_context(
                QuoteSession([reverse]), 101, quote(), 30_000_000, JTO, {}, "JTO")

    async def test_expensive_core_entry_releases_unsent_reserve_without_signing_or_sending(self):
        self.ledger.settings(101, enabled=True, raw=30_000_000)
        self.service.watchlist["JTO"] = {"mint": JTO, "decimals": 9}
        self.service.main_symbols.append("JTO")
        original_rpc = self.service.rpc
        async def policy_rpc(session, method, params=None):
            if method == "getSlot":
                return 100
            return await original_rpc(session, method, params)
        self.service.rpc = policy_rpc
        invoice = self.ledger.invoice(101, USDC, 30_000_000)
        self.ledger.credit_deposit(invoice["id"], str(Signature.new_unique()),
                                   USDC, invoice["raw"], invoice["created_at"])
        initial = self.ledger.balance(101, USDC)
        reverse = quote(JTO, USDC, 49_800_000_000, 29_900_000)
        reverse["priceImpactPct"] = "5"
        session = QuoteSession([quote(), reverse])
        self.service.build_swap_transaction = AsyncMock()
        self.service.send_via_jito = AsyncMock()
        with self.assertRaises(EntryCostError):
            await self.service.trade(session, 101, USDC, JTO, 30_000_000, "BUY",
                                     {"output_asset": JTO, "symbol": "JTO", "decimals": 9})
        self.assertEqual(self.ledger.balance(101, USDC), initial)
        self.assertEqual(self.ledger.pending(), [])
        self.service.build_swap_transaction.assert_not_awaited()
        self.service.send_via_jito.assert_not_awaited()
        self.assertTrue(self.ledger.audit())

    async def test_new_pairs_in_real_core_catalog_and_not_sniper(self):
        import main
        catalog = main.WATCHLIST
        self.assertEqual(catalog["JTO"], {"mint": JTO, "decimals": 9})
        self.assertEqual(catalog["ORCA"], {"mint": ORCA, "decimals": 6})
        self.service.watchlist = copy.deepcopy(catalog)
        self.service.main_symbols = [s for s in catalog if s != "WIF"]
        self.assertTrue({"JTO", "ORCA"}.issubset(self.service.ai_universes()["main"]))
        self.assertNotIn("JTO", self.service.ai_universes()["sniper"])
