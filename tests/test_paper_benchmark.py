"""Offline SOL baseline accounting, chronology, isolation and outage regressions."""
import copy
import tempfile
import time
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import execution_policy as policy
from custody_ledger import USDC
from paper_core import PaperCore
from paper_market import Evidence
from test_paper_core import Market, WATCH
from wallet_runtime import WalletError


class SolMarket(Market):
    def __init__(self):
        super().__init__()
        self.price = 100
        self.fail_sol = False

    async def quote(self, source, destination, raw):
        if policy.WRAPPED_SOL not in (source, destination):
            return await super().quote(source, destination, raw)
        if self.fail_sol:
            raise WalletError("Offline outage", 503)
        self.calls.append((source, destination, raw))
        if self.hook:
            hook, self.hook = self.hook, None
            hook()
        minimum = raw*1000//self.price if source == USDC else raw*self.price//1000
        return Evidence({"inputMint": source, "outputMint": destination, "inAmount": str(raw),
            "outAmount": str(minimum+1), "otherAmountThreshold": str(minimum),
            "swapMode": "ExactIn", "slippageBps": 40, "priceImpactPct": self.impact,
            "contextSlot": self.slot, "routePlan": [{"swapInfo": {"ammKey": "offline"}}]},
            time.time()-self.age, self.slot)


class BenchmarkTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = self.tmp.name+"/paper.db"
        self.core = PaperCore(self.path, copy.deepcopy(WATCH), lambda: ["SOL", "JTO"])
        self.store = self.core.store
        self.store.start(1, 100_000_000, 30_000_000)
        self.market = SolMarket()

    async def tick(self, snapshot=None):
        await self.core.tick(1, self.market, snapshot, {"JTO": 1.})

    def comparison(self):
        return self.store.report(1)["run"]["sol_comparison"]

    async def test_exact_minimum_entry_fixed_quantity_expenses_and_profit_partner(self):
        self.assertEqual(self.comparison()["status"], "unavailable")
        await self.tick()
        b = self.store.benchmark(self.store.current(1)["id"])
        self.assertEqual(b["raw"], 997_920_000)
        c = self.comparison()
        self.assertEqual(c["hold_equity_usdc"], 99.584)
        self.assertEqual(c["hold_modeled_partner_usdc"], 0)
        self.assertEqual(c["core_equity_usdc"], 100)
        self.assertAlmostEqual(c["advantage_usdc"], .416)
        self.market.price = 120
        await self.tick()
        c = self.comparison()
        pre = 119_750_400-208_000-100_000_000
        partner = (pre*175+9999)//10000
        self.assertEqual(c["hold_modeled_partner_usdc"], partner/1e6)
        self.assertEqual(c["hold_equity_usdc"], (119_750_400-208_000-partner)/1e6)
        self.assertAlmostEqual(c["advantage_percentage_points"], c["advantage_usdc"])
        self.assertEqual(self.store.benchmark(b["run_id"])["raw"], b["raw"])
        self.assertEqual(sum(s == USDC and d == policy.WRAPPED_SOL for s,d,_ in self.market.calls), 1)
        reloaded = PaperCore(self.path, WATCH, lambda: ["SOL", "JTO"])
        self.assertEqual(reloaded.store.report(1)["run"]["sol_comparison"], c)

    async def test_no_backfill_for_legacy_and_no_cross_user_results(self):
        run = self.store.current(1)
        with self.store.db() as db:
            db.execute("DELETE FROM sol_benchmarks WHERE run_id=?", (run["id"],))
        await self.tick()
        self.assertIn("predates", self.comparison()["reason"])
        self.store.start(2, 50_000_000, 10_000_000)
        await self.core.tick(2, self.market, None, {})
        self.assertEqual(self.store.report(2)["run"]["sol_comparison"]["core_equity_usdc"], 50)
        self.assertEqual(self.comparison()["status"], "unavailable")
        self.assertIsNone(self.store.report(3)["run"])

    async def test_entry_outage_blocks_core_buy_until_common_market_start(self):
        self.market.fail_sol = True
        snapshot = {"id": "buy", "created_at": time.time(), "prices": {"JTO": 1.},
                    "samples": {"JTO": 5}, "decisions": {"main": {
                        "action": "BUY", "symbol": "JTO", "reason": "Offline signal"}}}
        await self.tick(snapshot)
        run = self.store.current(1)
        self.assertEqual(self.store.positions(run["id"]), [])
        self.assertEqual(run["cash_raw"], 100_000_000)
        self.assertIsNone(self.store.benchmark(run["id"])["started_at"])
        self.market.fail_sol = False
        await self.tick(snapshot)
        self.assertEqual(len(self.store.positions(run["id"])), 1)
        self.assertEqual(self.comparison()["status"], "ready")

    async def test_missing_stale_or_misaligned_marks_never_show_advantage(self):
        await self.tick()
        run = self.store.current(1)
        for timestamp in (None, int(time.time())-61, int(time.time())+1):
            self.store.benchmark_save(run, {"mark_at": timestamp})
            self.assertEqual(self.comparison()["status"], "unavailable")
            self.assertNotIn("advantage_usdc", self.comparison())
        await self.tick()
        self.store.buy(run, "JTO", WATCH["JTO"], 30_000_000, 208_000, 1., "manual", "Offline")
        position = self.store.positions(run["id"])[0]
        self.store.mark(run, position, 29_940_000, 208_000, time.time()-40)
        self.assertEqual(self.comparison()["status"], "unavailable")
        self.store.mark(run, position, 29_940_000, 208_000)
        self.assertEqual(self.comparison()["status"], "ready")
        self.market.fail_sol = True
        await self.tick()
        self.assertEqual(self.comparison()["status"], "unavailable")

    async def test_outage_after_entry_does_not_block_core_risk_exit(self):
        await self.tick()
        run = self.store.current(1)
        self.store.buy(run, "JTO", WATCH["JTO"], 30_000_000, 208_000, .9, "entry", "Offline")
        self.market.fail_sol = True
        await self.tick()
        self.assertEqual(self.store.report(1)["run"]["closed_count"], 1)
        self.assertEqual(self.comparison()["status"], "unavailable")

    async def test_pause_resume_new_run_races_preserve_quantity_and_archive(self):
        self.market.hook = lambda: self.store.pause(1, False)
        await self.tick()
        run = self.store.current(1)
        self.assertIsNone(self.store.benchmark(run["id"])["raw"])
        self.store.pause(1, True)
        await self.tick()
        old = self.store.current(1)
        baseline = self.store.benchmark(old["id"])
        self.store.pause(1, False)
        self.market.price = 50
        await self.tick()
        self.assertEqual(self.store.benchmark(old["id"]), baseline)
        self.store.pause(1, True)
        await self.tick()
        self.assertEqual(self.store.benchmark(old["id"])["raw"], baseline["raw"])
        self.store.start(1, 200_000_000, 30_000_000, True)
        self.assertIsNone(self.comparison()["started_at"])
        await self.tick()
        self.assertEqual(self.comparison()["core_equity_usdc"], 200)
        self.assertEqual(self.store.benchmark(old["id"])["raw"], baseline["raw"])

    async def test_entry_checks_stale_slot_impact_and_insufficient_virtual_capital(self):
        for attribute, value in (("age", 31), ("slot", 1), ("impact", "1"), ("fee", 100_000_001)):
            with self.subTest(attribute=attribute):
                self.market = SolMarket()
                setattr(self.market, attribute, value)
                await self.tick()
                self.assertIsNone(self.store.benchmark(self.store.current(1)["id"])["raw"])
                self.assertEqual(self.comparison()["status"], "unavailable")


if __name__ == "__main__":
    unittest.main()
