"""Offline virtual-account, policy-parity and no-real-money regression tests."""
import asyncio
import copy
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import execution_policy as policy
from custody_ledger import USDC, SOL
from paper_core import PaperCore
from paper_market import Evidence
from paper_store import PaperStore
from wallet_runtime import WalletError
import test_custody as fixtures

MINT = "test-mint"
WATCH = {"SOL": {"mint": policy.WRAPPED_SOL, "decimals": 9},
         "JTO": {"mint": MINT, "decimals": 6}}


class Market:
    def __init__(self):
        self.proceeds = 29_940_000
        self.impact = "0.01"
        self.slot = 100
        self.age = 0
        self.hook = None
        self.calls = []
        self.fee = 208_000

    async def head(self):
        return 100

    async def quote(self, source, destination, raw):
        self.calls.append((source, destination, raw))
        if self.hook:
            hook, self.hook = self.hook, None
            hook()
        minimum = 30_000_000 if source == USDC else self.proceeds
        return Evidence({"inputMint": source, "outputMint": destination,
            "inAmount": str(raw), "outAmount": str(minimum+1000),
            "otherAmountThreshold": str(minimum), "swapMode": "ExactIn", "slippageBps": 40,
            "priceImpactPct": self.impact, "contextSlot": self.slot,
            "routePlan": [{"swapInfo": {"ammKey": "offline"}}]},
            time.time()-self.age, self.slot)

    async def expenses(self):
        return {"quoted_at": time.time(), "context_slot": 100, "rent_lamports": 2_000_000,
                "expense_lamports": 2_080_000, "sol_usdc": "100", "expense_raw": self.fee}


class PaperTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.universe = ["SOL", "JTO"]
        self.core = PaperCore(self.tmp.name+"/paper.sqlite3", copy.deepcopy(WATCH), lambda: self.universe)
        self.store = self.core.store
        self.store.start(101, 100_000_000, 30_000_000)
        self.market = Market()
        self.snapshot = {"id": "first", "created_at": time.time(), "prices": {"JTO": 1.},
            "samples": {"JTO": 5}, "decisions": {
                "main": {"action": "BUY", "symbol": "JTO", "reason": "Offline test signal"}}}

    async def tick(self):
        await self.core.tick(101, self.market, self.snapshot, {"JTO": 1.})

    async def buy(self):
        await self.tick()
        self.assertEqual(len(self.store.positions(self.store.current(101)["id"])), 1)

    async def test_buy_sell_exact_accounting_quote_thresholds_and_duplicate_analysis(self):
        await self.buy()
        run = self.store.current(101)
        self.assertEqual(run["cash_raw"], 69_792_000)
        self.assertEqual(self.store.positions(run["id"])[0]["raw"], 30_000_000)
        await self.tick()
        self.assertEqual(len(self.store.positions(run["id"])), 1)
        self.snapshot["id"] = "sell"
        self.snapshot["decisions"]["main"]["action"] = "SELL"
        self.market.proceeds = 30_900_000
        await self.tick()
        report = self.store.report(101)["run"]
        self.assertEqual(report["closed_count"], 1)
        # .9 gross - .416 fees = .484 before the worst-case 1.75% partner model.
        self.assertAlmostEqual(report["gross_realized_usdc"], .9)
        self.assertAlmostEqual(report["realized_expenses_usdc"], .416)
        self.assertAlmostEqual(report["modeled_partner_usdc"], .00847)
        self.assertAlmostEqual(report["realized_net_usdc"], .47553)
        self.assertAlmostEqual(report["cash_usdc"], 100.47553)
        self.assertAlmostEqual(report["equity_usdc"], report["cash_usdc"])

    async def test_same_controller_rejects_cost_impact_stale_slot_stale_ai_and_overrides(self):
        cases = [
            lambda: setattr(self.market, "proceeds", 28_000_000),
            lambda: setattr(self.market, "impact", "1"),
            lambda: setattr(self.market, "slot", 1),
            lambda: setattr(self.market, "age", 31),
            lambda: self.snapshot.update(created_at=time.time()-121),
            lambda: self.snapshot["decisions"]["main"].update(override=True),
            lambda: self.snapshot["samples"].update(JTO=0),
            lambda: self.universe.clear()]
        for mutate in cases:
            with self.subTest(mutate=mutate):
                self.market = Market()
                self.universe = ["SOL", "JTO"]
                self.snapshot = {"id": "first", "created_at": time.time(), "prices": {"JTO": 1.},
                    "samples": {"JTO": 5}, "decisions": {"main": {
                        "action": "BUY", "symbol": "JTO", "reason": "Signal"}}}
                mutate()
                await self.tick()
                self.assertEqual(self.store.current(101)["cash_raw"], 100_000_000)
                self.assertFalse(self.store.positions(self.store.current(101)["id"]))

    async def test_pause_resume_and_new_run_invalidate_inflight_fill(self):
        self.market.hook = lambda: (self.store.pause(101, False), self.store.pause(101, True))
        await self.tick()
        self.assertEqual(self.store.current(101)["cash_raw"], 100_000_000)
        self.market.hook = lambda: self.store.start(101, 200_000_000, 30_000_000, True)
        await self.tick()
        self.assertEqual(self.store.current(101)["cash_raw"], 200_000_000)
        self.assertFalse(self.store.positions(self.store.current(101)["id"]))
        self.assertEqual(len(self.store.report(101)["run"]["archives"]), 1)

    async def test_pause_stops_all_fills_resume_persists_and_expired_marks_are_unavailable(self):
        await self.buy()
        self.store.pause(101, False)
        self.market.quote = AsyncMock(side_effect=AssertionError("paused must not read quotes"))
        await self.tick()
        self.assertEqual(self.store.report(101)["run"]["enabled"], False)
        persisted = PaperStore(self.store.path)
        self.assertFalse(persisted.current(101)["enabled"])
        persisted.pause(101, True)
        with self.store.db() as db:
            db.execute("UPDATE positions SET mark_at=?,mark_net_raw=1", (int(time.time())-61,))
        self.assertIsNone(self.store.report(101)["run"]["equity_usdc"])

    async def test_entry_efficiency_does_not_block_owned_exit(self):
        await self.buy()
        self.snapshot["id"] = "exit"
        self.snapshot["decisions"]["main"]["action"] = "SELL"
        self.market.impact = "99"
        self.market.proceeds = 10_000_000
        await self.tick()
        self.assertEqual(self.store.report(101)["run"]["closed_count"], 1)
        self.assertLess(self.store.report(101)["run"]["realized_net_usdc"], 0)

    async def test_risk_exit_without_ai_and_removed_catalog_uses_fill_cost_basis(self):
        await self.buy()
        del self.core.watchlist["JTO"]
        self.universe.clear()
        self.market.proceeds = 29_000_000
        await self.core.tick(101, self.market, None, {"JTO": .97})
        self.assertEqual(self.store.report(101)["run"]["closed_count"], 1)
        self.assertIn("risk exit", self.store.report(101)["run"]["status"])

    async def test_missing_data_and_holds_are_explicit_and_do_not_fill(self):
        await self.core.tick(101, self.market, None, {})
        self.assertIn("WAIT", self.store.current(101)["status"])
        self.snapshot["decisions"]["main"] = {"action": "HOLD", "symbol": None, "reason": "Wait"}
        await self.tick()
        self.assertIn("HOLD", self.store.current(101)["status"])
        self.assertEqual(self.store.current(101)["cash_raw"], 100_000_000)

    async def test_losses_carried_before_new_high_water_commission_and_other_users_isolated(self):
        run = self.store.current(101)
        for index, proceeds in enumerate([29_416_000, 30_916_000, 30_916_000]):
            self.assertTrue(self.store.buy(run, "JTO", WATCH["JTO"], 30_000_000,
                                          208_000, 1, str(index), "test"))
            position = self.store.positions(run["id"])[0]
            self.assertTrue(self.store.close(run, position, proceeds, 208_000, "test"))
            run = self.store.current(101)
        # Pre-commission results: -1, +.5, +.5 => no new profit above original HWM.
        report = self.store.report(101)["run"]
        self.assertEqual(report["modeled_partner_usdc"], 0)
        self.assertEqual(report["realized_net_usdc"], 0)
        self.assertEqual(report["longest_loss_streak"], 1)
        self.assertIsNone(self.store.report(202)["run"])
        self.store.start(202, 500_000_000, 30_000_000)
        self.assertEqual(self.store.report(202)["run"]["cash_usdc"], 500)

    async def test_archive_requires_explicit_confirmation_and_retains_old_data(self):
        await self.buy()
        with self.assertRaises(WalletError):
            self.store.start(101, 100_000_000, 30_000_000)
        self.store.start(101, 100_000_000, 30_000_000, True)
        archive = self.store.report(101)["run"]["archives"][0]
        self.assertEqual(archive["open_count"], 1)
        with self.store.db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM positions").fetchone()[0], 1)

    async def test_quote_equity_drawdown_and_failed_valuation_never_fabricate_profit(self):
        await self.buy()
        report = self.store.report(101)["run"]
        self.assertAlmostEqual(report["equity_usdc"], 99.524)
        self.assertGreaterEqual(report["max_sampled_drawdown_pct"], .47)
        self.market.quote = AsyncMock(side_effect=WalletError("Unavailable", 503))
        self.snapshot["id"] = "next"
        await self.tick()
        self.assertEqual(self.store.report(101)["run"]["closed_count"], 0)
        self.assertIn("cannot be valued", self.store.current(101)["status"])


class PaperRuntimeTests(unittest.IsolatedAsyncioTestCase):
    rpc = fixtures.RuntimeTests.rpc

    def setUp(self):
        fixtures.RuntimeTests.setUp(self)
        self.service.trade = AsyncMock(side_effect=AssertionError("REAL TRADE FORBIDDEN"))
        self.service.send_via_jito = AsyncMock(side_effect=AssertionError("REAL SEND FORBIDDEN"))
        self.service.key_fn = lambda: (_ for _ in ()).throw(AssertionError("SIGNER FORBIDDEN"))

    async def test_control_and_report_bind_only_verified_uid_and_never_enable_real_modes(self):
        class Request:
            def __getitem__(self, key):
                return {"verified_uid": 101, "auth_verified": True}[key]
            async def json(self):
                return {"action": "start", "initial_usdc": "100", "order_usdc": "30"}
        # Exercise the method behind the existing verified-auth wrapper.
        with patch.object(self.service, "uid", return_value=101):
            response = await self.service.paper_control(Request(), None)
            data = json.loads(response.text)
            self.assertTrue(data["paper"]["run"]["enabled"])
            self.assertFalse(self.service.ledger.settings(101)["trading"])
            self.assertFalse(self.service.ledger.settings(101)["sniper_trading"])
            self.assertIsNone(self.service.paper.store.current(202))
            request = Request()
            request.json = AsyncMock(return_value={"action": "start", "uid": 202})
            with self.assertRaises(WalletError):
                await self.service.paper_control(request, None)
        self.service.trade.assert_not_awaited()
        self.service.send_via_jito.assert_not_awaited()
        self.assertTrue(self.service.ledger.audit())

    async def test_paper_fill_cannot_change_any_custody_account_or_send_transaction(self):
        self.service.paper.store.start(101, 100_000_000, 30_000_000)
        symbol = "SOL"
        snapshot = {"id": "a", "created_at": time.time(), "prices": {symbol: 1.},
            "samples": {symbol: 5}, "decisions": {"main": {
                "action": "BUY", "symbol": symbol, "reason": "Offline"}}}
        with self.service.ledger.db() as db:
            before = list(db.iterdump())
        await self.service.paper.tick(101, Market(), snapshot, {symbol: 1.})
        with self.service.ledger.db() as db:
            self.assertEqual(list(db.iterdump()), before)
        self.service.trade.assert_not_awaited()
        self.service.send_via_jito.assert_not_awaited()

    async def test_worker_services_virtual_accounts_with_real_modes_off_and_pending_real_operation(self):
        self.service.paper.store.start(101, 100_000_000, 30_000_000)
        self.service.reconcile = AsyncMock()
        self.service.scan_deposits = AsyncMock()
        self.service.process_partner_payout = AsyncMock()
        self.service.current_prices = AsyncMock(return_value={"SOL": 100.})
        self.service.pending = Mock(return_value=True)
        self.service.poll_paper = Mock()
        self.service.poll_ai = Mock(return_value=None)
        self.service.poll_discovery = Mock()
        with patch("custody_runtime.asyncio.sleep", side_effect=asyncio.CancelledError):
            with self.assertRaises(asyncio.CancelledError):
                await self.service.worker()
        self.service.poll_paper.assert_called_once()
        self.service.poll_discovery.assert_not_called()
        self.service.trade.assert_not_awaited()
