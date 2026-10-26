import copy
import asyncio
import json
import os
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

from solders.keypair import Keypair
from solders.signature import Signature

import ai_decisions
from custody_ledger import CustodyLedger, LedgerError, SOL, USDC
from custody_runtime import CustodyRuntime, WRAPPED_SOL
from wallet_runtime import WalletError


class AILedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ledger = CustodyLedger(self.tmp.name + "/db")
        for uid in (101, 202):
            self.ledger.user(uid)
        self.ledger.seed_fees(20_000_000, 100_000_000)
        for uid, asset, raw in [(101, USDC, 10_000_000), (101, SOL, 20_000_000), (202, SOL, 30_000_000)]:
            invoice = self.ledger.invoice(uid, asset, raw)
            self.ledger.credit_deposit(invoice["id"], str(Signature.new_unique()), asset, raw, int(time.time()))
        self.mint = str(Keypair().pubkey())

    def buy(self):
        row = self.ledger.reserve(101, "native-buy", "BUY", SOL, 5_000_000,
            {"output_asset": self.mint, "symbol": "WIF", "decimals": 6})
        self.ledger.prepared(row["id"], str(Signature.new_unique()), "test-wire-not-broadcast")
        self.ledger.finalize(row["id"], 5_000_000, 2000, 40_000_000)
        return self.ledger.positions(101, "OPEN")[0]

    def test_native_round_trip_preserves_units_and_other_users(self):
        position = self.buy()
        self.assertEqual(position["funding_asset"], SOL)
        self.assertEqual(position["cost_raw"], 5_000_000)
        self.assertEqual(self.ledger.balance(101, SOL), 15_000_000)
        self.assertEqual(self.ledger.balance(101, USDC), 10_000_000)
        row = self.ledger.reserve(101, "native-sell", "SELL", self.mint, position["raw"],
            {"output_asset": SOL}, position_id=position["id"])
        self.ledger.prepared(row["id"], str(Signature.new_unique()), "test-wire-not-broadcast")
        self.ledger.finalize(row["id"], position["raw"], 3000, 6_000_000)
        closed = self.ledger.positions(101, "CLOSED")[0]
        self.assertEqual(closed["proceeds_raw"], 6_000_000)
        self.assertIsNone(closed["proceeds_usdc"])
        self.assertEqual(self.ledger.balance(101, SOL), 21_000_000)
        self.assertEqual(self.ledger.balance(202, SOL), 30_000_000)
        self.assertEqual(self.ledger.balance(101, USDC), 10_000_000)
        self.assertTrue(self.ledger.audit())

    def test_native_position_cannot_be_sold_to_usdc_or_by_other_user(self):
        position = self.buy()
        for uid, currency in [(101, USDC), (202, SOL)]:
            with self.assertRaises(LedgerError):
                self.ledger.reserve(uid, "bad-sell", "SELL", self.mint, position["raw"],
                    {"output_asset": currency}, position_id=position["id"])
        self.assertEqual(self.ledger.positions(101)[0]["state"], "OPEN")
        self.assertTrue(self.ledger.audit())

    def test_ai_quota_and_cooldown_are_persistent(self):
        with patch("custody_ledger.time.time", return_value=100000):
            aid = self.ledger.ai_allow_call(900, 2)
            self.ledger.ai_record(aid, {"decisions": {}})
            with self.assertRaises(LedgerError):
                self.ledger.ai_allow_call(900, 2)
        with patch("custody_ledger.time.time", return_value=101000):
            reopened = CustodyLedger(self.tmp.name + "/db")
            aid = reopened.ai_allow_call(900, 2)
            reopened.ai_record(aid, {"decisions": {}})
        with patch("custody_ledger.time.time", return_value=102000):
            with self.assertRaises(LedgerError):
                reopened.ai_allow_call(900, 2)

    def test_failed_ai_call_retries_after_short_backoff_and_counts_toward_quota(self):
        with patch("custody_ledger.time.time", return_value=100000):
            self.ledger.ai_allow_call(900, 3)  # No analysis record: provider attempt failed.
        with patch("custody_ledger.time.time", return_value=100030):
            with self.assertRaisesRegex(LedgerError, "через 60 секунд"):
                self.ledger.ai_allow_call(900, 3)
        with patch("custody_ledger.time.time", return_value=100061):
            reopened = CustodyLedger(self.tmp.name + "/db")
            reopened.ai_allow_call(900, 3)
        with patch("custody_ledger.time.time", return_value=100062):
            with self.assertRaises(LedgerError):
                reopened.ai_allow_call(900, 2)

    def test_decision_claim_once_per_user_and_mode(self):
        self.assertTrue(self.ledger.ai_claim("analysis", 101, "main"))
        self.assertFalse(self.ledger.ai_claim("analysis", 101, "main"))
        self.assertTrue(self.ledger.ai_claim("analysis", 202, "main"))
        self.assertTrue(self.ledger.ai_claim("analysis", 101, "sniper"))

    def test_whale_and_scheduled_cadences_are_separate_with_shared_daily_limit(self):
        with patch("custody_ledger.time.time", return_value=100000):
            scheduled = self.ledger.ai_allow_call(90, 3, trigger="scheduled")
            self.ledger.ai_record(scheduled, {"trigger": "scheduled"})
        with patch("custody_ledger.time.time", return_value=100060):
            whale = self.ledger.ai_allow_call(60, 3, trigger="whale_signal")
            self.ledger.ai_record(whale, {"trigger": "whale_signal"})
            with self.assertRaises(LedgerError):
                self.ledger.ai_allow_call(60, 3, trigger="whale_signal")
        with patch("custody_ledger.time.time", return_value=100090):
            self.ledger.ai_allow_call(90, 3, trigger="scheduled")
            self.assertEqual(self.ledger.ai_latest(trigger="scheduled")["id"], scheduled)
            self.assertEqual(self.ledger.ai_latest(trigger="whale_signal")["id"], whale)
            with self.assertRaises(LedgerError):
                self.ledger.ai_allow_call(60, 3, trigger="whale_signal")

    def test_restart_disables_both_modes_preserving_funds(self):
        self.ledger.settings(101, enabled=True, sniper_enabled=True)
        self.ledger.restart_pause()
        self.assertFalse(self.ledger.settings(101)["trading"])
        self.assertFalse(self.ledger.settings(101)["sniper_trading"])
        self.assertEqual(self.ledger.balance(101, SOL), 20_000_000)
        self.assertTrue(self.ledger.audit())

    def test_old_schema_upgrade_preserves_usdc_positions_and_backup(self):
        row = self.ledger.reserve(101, "old-buy", "BUY", USDC, 1_000_000,
            {"output_asset": self.mint, "symbol": "WIF", "decimals": 6})
        self.ledger.prepared(row["id"], str(Signature.new_unique()), "test")
        self.ledger.finalize(row["id"], 1_000_000, 1000, 40_000_000)
        with self.ledger.db() as db:
            for name in ("funding_asset", "cost_raw", "proceeds_raw"):
                db.execute("ALTER TABLE custody_positions DROP COLUMN " + name)
        upgraded = CustodyLedger(self.tmp.name + "/db")
        position = upgraded.positions(101)[0]
        self.assertEqual(position["funding_asset"], USDC)
        self.assertEqual(position["cost_raw"], 1_000_000)
        self.assertEqual(upgraded.balance(101, USDC), 9_000_000)
        self.assertTrue(os.path.isfile(self.tmp.name + "/db.pre-ai.bak"))
        self.assertTrue(upgraded.audit())


class AIControllerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        signer = Keypair()
        self.mint = str(Keypair().pubkey())
        self.service = CustodyRuntime(self.tmp.name + "/db", "https://invalid.example",
            101, str(signer.pubkey()), lambda: signer, lambda uid, pin: (True, ""),
            AsyncMock(return_value={}), {"SOL": {"mint": WRAPPED_SOL, "decimals": 9},
            "WIF": {"mint": self.mint, "decimals": 6}}, lambda msg: None, base_url="https://invalid.example")
        self.service.ledger.user(101)
        self.service.ledger.seed_fees(20_000_000, 100_000_000)
        for currency in (USDC, SOL):
            inv = self.service.ledger.invoice(101, currency, 20_000_000)
            self.service.ledger.credit_deposit(inv["id"], str(Signature.new_unique()), currency, 20_000_000, int(time.time()))
        self.service.trade = AsyncMock()
        self.service.verify_sniper_mint = AsyncMock()
        self.service.discovery.blocked = False
        with self.service.ledger.db() as db:
            db.execute("INSERT INTO custody_meme_assets VALUES(?,?,?,?,?)", (
                "WIF", self.mint, 6, json.dumps({"symbol":"WIF","code":"WIF",
                "eligible":True,"score":20,"liquidity_usd":200000,"volume_24h_usd":200000,
                "transactions_24h":1000,"age_hours":48,"change_m5_pct":1,"change_h1_pct":2}),
                int(time.time())))
        self.service.ledger.settings(101, enabled=True, sniper_enabled=True)
        self.prices = {"SOL": 100, "WIF": 2}
        self.snapshot = {"id": "test-analysis", "created_at": int(time.time()),
            "prices": self.prices.copy(), "samples": {"SOL": 5, "WIF": 5},
            "main_market_context": {"SOL": {
                "change_m5_pct": 0.8, "change_h1_pct": 1.5, "change_h6_pct": 0.2,
                "buys_m5": 8, "sells_m5": 4, "chart_ready": 1, "chart_buy_setup": 1,
                "chart_trend_h1": 1}},
            "sniper_market_context": {"WIF": {"chart_ready": 1, "chart_buy_setup": 1,
                "change_m5_pct": 0.8, "change_h1_pct": 1.2, "change_h6_pct": -4.9}},
            "decisions": {"main": {"action": "BUY", "symbol": "SOL", "reason": "signal"},
                          "sniper": {"action": "HOLD", "symbol": None, "reason": "wait"}}}

    async def test_default_analysis_model_is_gpt_41_and_can_be_overridden(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OPENAI_MODEL", None)
            self.assertEqual(ai_decisions.configuration()["model"], "gpt-4.1")
        with patch.dict(os.environ, {"OPENAI_MODEL": "gpt-4.1-mini"}):
            self.assertEqual(ai_decisions.configuration()["model"], "gpt-4.1-mini")

    async def test_analysis_refresh_defaults_before_decision_expiry(self):
        config = ai_decisions.configuration()
        self.assertEqual(config["interval_seconds"], 90)
        self.assertEqual(config["max_calls_per_day"], 960)

    async def test_legacy_environment_overrides_cannot_restore_stale_schedule(self):
        with patch.dict(os.environ, {
            "AI_INTERVAL_SECONDS": "900",
            "AI_MAX_CALLS_PER_DAY": "96",
        }):
            config = ai_decisions.configuration()
        self.assertEqual(config["interval_seconds"], 90)
        self.assertEqual(config["max_calls_per_day"], 960)

    async def apply(self, snapshot=None, prices=None, positions=None):
        await self.service.apply_ai_decision(None, 101, self.service.ledger.settings(101),
            positions or [], prices or self.prices, snapshot or self.snapshot)

    async def test_main_uses_saved_usdc_only_and_once(self):
        await self.apply()
        self.assertEqual(self.service.trade.await_args.args[2:6], (USDC, SOL, 1_000_000, "BUY"))
        await self.apply()
        self.assertEqual(self.service.trade.await_count, 1)

    async def test_sniper_uses_saved_sol_not_usdc(self):
        self.snapshot["decisions"]["main"] = {"action": "HOLD", "symbol": None, "reason": "wait"}
        self.snapshot["decisions"]["sniper"] = {"action": "BUY", "symbol": "WIF", "reason": "signal"}
        await self.apply()
        self.service.verify_sniper_mint.assert_awaited_once_with(None, "WIF")
        self.assertEqual(self.service.trade.await_args.args[2:6], (SOL, self.mint, 5_000_000, "BUY"))

    async def test_whale_snapshot_cannot_buy_usdc_even_with_a_core_buy_proposal(self):
        self.snapshot["trigger"] = "whale_signal"
        await self.apply()
        self.service.trade.assert_not_awaited()

    async def test_stale_core_does_not_hide_fresh_sniper_or_refresh_core_timestamp(self):
        whale = copy.deepcopy(self.snapshot)
        whale.update(id="whale-only", trigger="whale_signal")
        whale["decisions"]["main"] = {"action": "HOLD", "symbol": None, "reason": "Sniper only"}
        whale["decisions"]["sniper"] = {"action": "BUY", "symbol": "WIF", "reason": "screened"}
        self.snapshot["created_at"] -= 121
        self.snapshot["sniper_snapshot"] = whale
        await self.apply()
        self.assertEqual(self.service.trade.await_count, 1)
        self.assertEqual(self.service.trade.await_args.args[2:6], (SOL, self.mint, 5_000_000, "BUY"))
        self.assertEqual(self.service.trade.await_args.args[6]["ai_analysis"], "whale-only")

    async def test_fresh_core_keeps_its_own_decision_despite_a_newer_whale_snapshot(self):
        whale = copy.deepcopy(self.snapshot)
        whale.update(id="whale-only", trigger="whale_signal")
        whale["decisions"]["main"] = {"action": "HOLD", "symbol": None, "reason": "Sniper only"}
        self.snapshot["sniper_snapshot"] = whale
        await self.apply()
        self.assertEqual(self.service.trade.await_args.args[2:6], (USDC, SOL, 1_000_000, "BUY"))
        self.assertEqual(self.service.trade.await_args.args[6]["ai_analysis"], "test-analysis")

    async def test_repeated_whale_results_do_not_postpone_scheduled_core_refresh(self):
        now = int(time.time())
        record = {key: copy.deepcopy(value) for key, value in self.snapshot.items()
                  if key not in ("id", "created_at")}
        self.service.ledger.ai_record("scheduled", record)
        whale = copy.deepcopy(record)
        whale["trigger"] = "whale_signal"
        whale["decisions"]["main"] = {"action": "HOLD", "symbol": None, "reason": "Sniper only"}
        self.service.ledger.ai_record("whale", whale)
        with self.service.ledger.db() as db:
            db.execute("UPDATE custody_ai_analyses SET created_at=? WHERE id='scheduled'", (now - 91,))
        self.service.background_ai = AsyncMock(return_value=None)
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key-not-real"}):
            snapshot = self.service.poll_ai(self.prices)
            self.assertIsNotNone(self.service.ai_task)
            await self.service.ai_task
        self.assertEqual(snapshot["id"], "scheduled")
        self.assertEqual(snapshot["sniper_snapshot"]["id"], "whale")
        status = self.service.ai_status()
        self.assertEqual(status["last_decisions"]["main"]["action"], "BUY")
        self.assertEqual(status["decision_created_at"]["main"], now - 91)
        self.assertEqual(status["chart_context"], self.snapshot["main_market_context"])

    async def test_sniper_chart_gate_blocks_missing_chart_for_buys(self):
        self.snapshot["decisions"]["main"] = {"action": "HOLD", "symbol": None, "reason": "wait"}
        self.snapshot["decisions"]["sniper"] = {"action": "BUY", "symbol": "WIF", "reason": "signal"}
        self.snapshot["sniper_market_context"] = {}
        await self.apply()
        self.service.trade.assert_not_awaited()
        self.assertEqual(self.service.ledger.execution_report(101)["code"], "sniper_chart_data")

    def new_candidate(self):
        mint = str(Keypair().pubkey())
        code = "M_test_verified"
        with self.service.ledger.db() as db:
            row = db.execute("SELECT metrics FROM custody_meme_assets WHERE code='WIF'").fetchone()
            metrics = {**json.loads(row["metrics"]), "code":code, "symbol":"CAT"}
            db.execute("INSERT INTO custody_meme_assets VALUES(?,?,?,?,?)", (
                code, mint, 6, json.dumps(metrics), int(time.time())))
        self.service.restore_catalog()
        self.snapshot.setdefault("sniper_market_context", {})[code] = {
            "chart_ready": 1, "chart_buy_setup": 1,
            "change_m5_pct": 0.8, "change_h1_pct": 1.2, "change_h6_pct": -4.9}
        return code, mint

    async def test_discovered_asset_uses_native_capital_and_cannot_leak_into_core(self):
        code, mint = self.new_candidate()
        self.assertNotIn(code,self.service.ai_universes()["main"])
        self.assertIn(code,self.service.ai_universes()["sniper"])
        self.snapshot["decisions"]={"main":{"action":"HOLD","symbol":None,"reason":"wait"},
                                   "sniper":{"action":"BUY","symbol":code,"reason":"signal"}}
        self.snapshot["universes"]=self.service.ai_universes()
        self.snapshot["prices"][code]=.01
        self.snapshot["samples"][code]=5
        await self.apply(prices={**self.prices,code:.01})
        self.assertEqual(self.service.trade.await_args.args[2:6],(SOL,mint,5_000_000,"BUY"))
        self.service.verify_sniper_mint.assert_awaited_once_with(None,code)

    async def test_cached_candidate_buy_is_blocked_if_scan_fails(self):
        code,mint=self.new_candidate()
        self.snapshot["decisions"]={"main":{"action":"HOLD","symbol":None,"reason":"wait"},
                                   "sniper":{"action":"BUY","symbol":code,"reason":"signal"}}
        self.snapshot["universes"]=self.service.ai_universes()
        self.snapshot["prices"][code]=.01
        self.snapshot["samples"][code]=5
        self.service.discovery.fail()
        await self.apply(prices={**self.prices,code:.01})
        self.service.trade.assert_not_awaited()
        self.service.verify_sniper_mint.assert_not_awaited()

    async def test_confirmed_whale_candidate_triggers_ai_after_screen_without_trade(self):
        code, mint = self.new_candidate()
        with self.service.ledger.db() as db:
            row = json.loads(db.execute(
                "SELECT metrics FROM custody_meme_assets WHERE code=?", (code,)).fetchone()["metrics"])
        row["mint"] = mint
        self.assertTrue(self.service.on_whale_signal({"mint": mint}))

        async def screened_scan(_session):
            self.service.discovery.blocked = False
            self.service.discovery.report = {"candidates": [row], "eligible": 1, "scanned": 1}

        self.service.discovery.scan = AsyncMock(side_effect=screened_scan)
        self.service.current_prices = AsyncMock(return_value={**self.prices, code: .01})
        decisions = {
            "main": {"action": "HOLD", "symbol": None, "reason": "No clear USDC signal."},
            "sniper": {"action": "HOLD", "symbol": code, "reason": "Wait for more market data."},
        }
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key-not-real"}), patch(
                "ai_decisions.analyze", AsyncMock(return_value=decisions)) as analyze:
            await self.service.background_discovery()

        analyze.assert_awaited_once()
        self.assertEqual(analyze.await_args.args[2]["main"], [])
        self.assertIn(code, analyze.await_args.args[2]["sniper"])
        self.assertIn(code, analyze.await_args.args[3])
        self.assertEqual(self.service.whale_analysis_status["state"], "complete")
        self.assertEqual(self.service.ai_status()["last_trigger"], "whale_signal")
        self.service.trade.assert_not_awaited()

    async def test_whale_signal_that_fails_screen_never_calls_ai(self):
        mint = str(Keypair().pubkey())
        self.assertTrue(self.service.on_whale_signal({"mint": mint}))

        async def empty_scan(_session):
            self.service.discovery.blocked = False
            self.service.discovery.report = {"candidates": [], "eligible": 0, "scanned": 1}

        self.service.discovery.scan = AsyncMock(side_effect=empty_scan)
        self.service.current_prices = AsyncMock(return_value=self.prices)
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key-not-real"}), patch(
                "ai_decisions.analyze", AsyncMock()) as analyze:
            await self.service.background_discovery()

        analyze.assert_not_awaited()
        self.service.current_prices.assert_not_awaited()
        self.assertEqual(self.service.whale_analysis_status["state"], "screened_out")
        self.service.trade.assert_not_awaited()

    async def test_candidate_scan_failure_is_reported_safely_and_retried_with_backoff(self):
        messages = []
        self.service.log = messages.append
        self.service.discovery.scan = AsyncMock(
            side_effect=ValueError("DexScreener unavailable: HTTP 429"))

        await self.service.background_discovery()
        self.assertTrue(self.service.discovery.blocked)
        self.assertIn("market-data HTTP 429", self.service.discovery.report["message"])
        self.assertIn("market-data HTTP 429", messages[0])
        self.assertNotIn("DexScreener", messages[0])
        self.assertEqual(
            self.service.discovery.failure_category(KeyError("sensitive detail")),
            "internal scan error (KeyError)")
        self.assertEqual(self.service.discovery_failures, 1)
        self.assertLessEqual(self.service.discovery_due - time.time(), 60)

        await self.service.background_discovery()
        self.assertEqual(self.service.discovery_failures, 2)
        self.assertLessEqual(self.service.discovery_due - time.time(), 120)

    async def test_successful_candidate_scan_resets_retry_backoff(self):
        self.service.discovery_failures = 3

        async def successful_scan(_session):
            self.service.discovery.blocked = False
            self.service.discovery.report = {"candidates": [], "eligible": 0, "scanned": 0}

        self.service.discovery.scan = AsyncMock(side_effect=successful_scan)
        await self.service.background_discovery()

        self.assertFalse(self.service.discovery.blocked)
        self.assertEqual(self.service.discovery_failures, 0)
        self.assertGreaterEqual(self.service.discovery_due - time.time(), 899)

    async def test_whale_ai_bypasses_scheduled_cache_but_respects_one_minute_quota(self):
        self.service.market_context_fn = AsyncMock(return_value={
            "SOL": {"change_m5_pct": 1.2}
        })
        decisions = {
            "main": {"action": "HOLD", "symbol": None, "reason": "No clear USDC signal."},
            "sniper": {"action": "HOLD", "symbol": "WIF", "reason": "Wait for more market data."},
        }
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key-not-real"}), patch(
                "ai_decisions.analyze", AsyncMock(return_value=decisions)) as analyze:
            snapshot = await self.service.ai_snapshot(None, self.prices, whale_symbols=["WIF"])
            self.assertEqual(snapshot["trigger"], "whale_signal")
            with self.assertRaises(WalletError):
                await self.service.ai_snapshot(None, self.prices, whale_symbols=["WIF"])

        analyze.assert_awaited_once()
        self.assertEqual(snapshot["universes"]["main"], [])
        self.assertEqual(snapshot["decisions"]["main"]["action"], "HOLD")
        self.service.market_context_fn.assert_not_awaited()
        self.service.trade.assert_not_awaited()

    async def test_removed_candidate_position_can_still_exit_and_other_users_are_untouched(self):
        code,mint=self.new_candidate()
        row=self.service.ledger.reserve(101,"dynamic-buy","BUY",SOL,5_000_000,
            {"output_asset":mint,"symbol":code,"decimals":6})
        self.service.ledger.prepared(row["id"],str(Signature.new_unique()),"not-broadcast")
        self.service.ledger.finalize(row["id"],5_000_000,1000,50_000_000)
        positions=self.service.ledger.positions(101,"OPEN")
        self.service.discovery.fail()
        self.assertIn(code,self.service.ai_universes()["sniper"])
        self.snapshot["decisions"]={"main":{"action":"HOLD","symbol":None,"reason":"wait"},
                                   "sniper":{"action":"SELL","symbol":code,"reason":"exit"}}
        self.snapshot["universes"]=self.service.ai_universes()
        self.snapshot["prices"][code]=.01
        await self.apply(prices={**self.prices,code:.01},positions=positions)
        self.assertEqual(self.service.trade.await_args.args[2:6],(mint,SOL,50_000_000,"SELL"))
        self.assertTrue(self.service.ledger.audit())

    async def test_no_buy_for_stale_drifting_nan_or_unwarmed_data(self):
        for case in ("stale", "drift", "nan", "warmup"):
            snapshot = copy.deepcopy(self.snapshot)
            prices = self.prices.copy()
            if case == "stale": snapshot["created_at"] -= 180
            if case == "drift": prices["SOL"] = 105
            if case == "nan": prices["SOL"] = float("nan")
            if case == "warmup": snapshot["samples"]["SOL"] = 1
            await self.apply(snapshot, prices)
        self.service.trade.assert_not_awaited()

    async def test_hold_no_trade_and_sell_without_position_no_trade(self):
        self.snapshot["decisions"]["main"] = {"action": "HOLD", "symbol": None, "reason": "wait"}
        await self.apply()
        self.snapshot["decisions"]["main"] = {"action": "SELL", "symbol": "SOL", "reason": "signal"}
        await self.apply()
        self.service.trade.assert_not_awaited()

    async def test_native_cap_is_one_position(self):
        self.snapshot["decisions"]["main"] = {"action": "HOLD", "symbol": None, "reason": "wait"}
        self.snapshot["decisions"]["sniper"] = {"action": "BUY", "symbol": "WIF", "reason": "signal"}
        await self.apply(positions=[{"funding_asset": SOL, "symbol": "WIF"}])
        self.service.trade.assert_not_awaited()

    async def test_missing_model_key_never_falls_back_to_rules(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(WalletError):
                await self.service.ai_snapshot(None, self.prices)
            self.assertIsNone(self.service.poll_ai(self.prices))
        self.service.trade.assert_not_awaited()

    async def test_ai_retry_cooldown_returns_clear_http_429(self):
        message = ("AI предыдущий запрос завершился ошибкой. Повторите через 60 секунд; "
                   "попытка учитывается в дневном лимите.")
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key-not-real"}):
            with patch.object(self.service.ledger, "ai_allow_call",
                              side_effect=LedgerError(message)):
                with self.assertRaises(WalletError) as raised:
                    await self.service.ai_snapshot(None, self.prices)
        self.assertEqual(raised.exception.status, 429)
        self.assertEqual(str(raised.exception), message)

    async def test_invalid_model_commands_fail_closed(self):
        for edit in [
                {"action": "WITHDRAW"}, {"amount": 1000}, {"symbol": "UNKNOWN"},
                {"wallet": "foreign"}, {"reason": ""}, {"reason": "   "},
                {"reason": "x" * 351}, {"reason": None}]:
            value = copy.deepcopy(self.snapshot["decisions"])
            value["main"].update(edit)
            with self.assertRaises(ai_decisions.AIError):
                ai_decisions.validate_decisions(value, self.service.ai_universes())

    async def test_ai_http_payload_has_market_data_no_identity_or_wallet(self):
        outer = self
        class Response:
            status = 200
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def json(self):
                return {"choices": [{"finish_reason": "stop", "message": {
                    "content": json.dumps(outer.snapshot["decisions"])}}]}
        class Session:
            def post(self, url, **kwargs):
                outer.payload = kwargs["json"]
                return Response()
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key-not-real"}):
            await ai_decisions.analyze(Session(), {"SOL": [100]*5, "WIF": [2]*5},
                self.service.ai_universes(), main_context={
                    "SOL": {"change_m5_pct": 0.8, "change_h1_pct": 2.1, "buys_h1": 12,
                            "chart_trend_h1": 1},
                    "WIF": {"change_h1_pct": 999},
                    "UNKNOWN": {"change_h1_pct": 500},
                    "SOL_BAD": {"change_h1_pct": float("nan")},
                })
        content = json.dumps(self.payload)
        self.assertEqual(self.payload["model"], "gpt-4.1")
        self.assertNotIn(self.service.wallet, content)
        self.assertNotIn("telegram", content.lower())
        self.assertNotIn("test-key-not-real", content)
        self.assertFalse(self.payload["store"])
        user_payload = json.loads(self.payload["messages"][3]["content"])
        self.assertEqual(user_payload["main_dex_market_context_numeric"], {
            "SOL": {"change_m5_pct": 0.8, "change_h1_pct": 2.1, "buys_h1": 12,
                    "chart_trend_h1": 1}
        })
        self.assertEqual(user_payload["main_dex_context_applies_only_to"],
                         "main (USDC), never sniper (SOL)")
        self.assertIn("Consider token/SOL relative changes for sniper.",
                      self.payload["messages"][0]["content"])
        for mode in ("main", "sniper"):
            reason_schema = self.payload["response_format"]["json_schema"]["schema"][
                "properties"][mode]["properties"]["reason"]
            self.assertEqual(reason_schema["minLength"], 1)
            self.assertEqual(reason_schema["maxLength"], 350)
        self.assertIn("both Core (USDC) and Sniper (SOL)", self.payload["messages"][1]["content"])
        self.assertIn("not backtested", self.payload["messages"][1]["content"])
        self.assertIn("OHLCV chart features", self.payload["messages"][1]["content"])
        self.assertIn("chart data is missing or stale", self.payload["messages"][1]["content"])
        self.assertIn("only say the floor was breached when the value is less than -5%",
                      self.payload["messages"][1]["content"])
        self.assertIn("confirmed bounce from repeated support",
                      self.payload["messages"][2]["content"])
        self.assertIn("relative strength versus SOL is not applicable",
                      self.payload["messages"][1]["content"])
        self.assertTrue(any(item.startswith("5-minute EMA20/50, RSI14, MACD histogram")
                            for item in user_payload["main_chart_data_capabilities"]["available"]))
        self.assertEqual(user_payload["main_usdc_signal_evidence_numeric"]["SOL"]["change_m5_pct"], 0.8)
        self.assertTrue(user_payload["main_usdc_signal_evidence_numeric"]["SOL"]["trend_aligned_positive"])
        self.assertNotIn("WIF", user_payload["main_usdc_signal_evidence_numeric"])

    async def test_main_usdc_signal_evidence_compares_recent_trend_and_buy_pressure(self):
        evidence = ai_decisions.main_usdc_signal_evidence(
            {"SOL": [100.0] * 15 + [101.0], "JUP": [100.0] * 15 + [103.0]},
            {"JUP": {
                "change_m5_pct": 1.2, "change_h1_pct": 2.5,
                "chart_trend_h1": 1,
                "buys_m5": 14, "sells_m5": 8,
                "liquidity_usd": 250_000, "volume_24h_usd": 80_000,
            }},
            ["SOL", "JUP"],
        )
        self.assertAlmostEqual(evidence["JUP"]["recent_sample_change_pct"], 3.0)
        self.assertAlmostEqual(evidence["JUP"]["relative_change_vs_SOL_pct"], 2.0)
        self.assertTrue(evidence["JUP"]["buyers_exceed_sellers_m5"])
        self.assertTrue(evidence["JUP"]["momentum_positive"])
        self.assertTrue(evidence["JUP"]["trend_aligned_positive"])
        self.assertTrue(evidence["JUP"]["buyer_pressure_positive"])
        self.assertTrue(evidence["JUP"]["relative_strength_positive"])
        self.assertEqual(evidence["JUP"]["liquidity_usd"], 250_000)
        self.assertNotIn("buyers_exceed_sellers_h1", evidence["JUP"])
        self.assertNotIn("buyers_exceed_sellers_m5", evidence["SOL"])

    async def test_ai_snapshot_fetches_and_persists_both_mode_chart_contexts(self):
        context = {
            "SOL": {"change_m5_pct": 0.8, "liquidity_usd": 200_000},
            "WIF": {"change_m5_pct": 50},
        }
        self.service.market_context_fn = AsyncMock(return_value=context)
        sniper_context = {"WIF": {"chart_ready": 1, "chart_buy_setup": 1}}
        self.service.sniper_market_context_fn = AsyncMock(return_value=sniper_context)
        decisions = {
            "main": {"action": "HOLD", "symbol": None, "reason": "No clear USDC signal."},
            "sniper": {"action": "HOLD", "symbol": None, "reason": "No signal."},
        }
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key-not-real"}), patch(
                "ai_decisions.analyze", AsyncMock(return_value=decisions)) as analyze:
            snapshot = await self.service.ai_snapshot(None, self.prices)

        self.service.market_context_fn.assert_awaited_once_with(None)
        self.service.sniper_market_context_fn.assert_awaited_once_with(
            None, self.service.watchlist, ["WIF"])
        self.assertEqual(analyze.await_args.args[4], {
            "SOL": {"change_m5_pct": 0.8, "liquidity_usd": 200_000},
        })
        self.assertEqual(snapshot["main_market_context"], {
            "SOL": {"change_m5_pct": 0.8, "liquidity_usd": 200_000},
        })
        self.assertEqual(analyze.await_args.args[5], sniper_context)
        self.assertEqual(snapshot["sniper_market_context"], sniper_context)

    async def test_model_failure_does_not_consume_user_funds(self):
        before = self.service.ledger.balance(101, SOL)
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key-not-real"}), patch(
                "ai_decisions.analyze", AsyncMock(side_effect=ai_decisions.AIError("unavailable"))):
            with self.assertRaises(WalletError):
                await self.service.ai_snapshot(None, self.prices)
        self.assertEqual(self.service.ledger.balance(101, SOL), before)
        self.service.trade.assert_not_awaited()

    async def test_sol_risk_exit_uses_sol_relative_price_not_usdc(self):
        position = {"symbol": "WIF", "funding_asset": SOL, "cost_raw": 5_000_000,
                    "raw": 250_000, "decimals": 6, "peak_price": 0.02}
        self.assertFalse(self.service.exit_triggered(position, {"WIF": 2, "SOL": 100}))
        position["peak_price"] = 0.0213
        self.assertTrue(self.service.exit_triggered(position, {"WIF": 2.08, "SOL": 100}))
        self.assertTrue(self.service.exit_triggered(position, {"WIF": 1.94, "SOL": 100}))
        self.assertTrue(self.service.exit_triggered(position, {"WIF": 1.79, "SOL": 100}))
        self.assertEqual(self.service.exit_reason(position, {"WIF": 2.08, "SOL": 100}), "TRAILING_STOP")
        self.assertEqual(self.service.exit_reason(position, {"WIF": 1.94, "SOL": 100}), "TRAILING_STOP")
        self.assertEqual(self.service.exit_reason(position, {"WIF": 1.79, "SOL": 100}), "STOP_LOSS")

    async def test_sniper_does_not_sell_at_fixed_profit_target_without_a_peak_retrace(self):
        position = {"symbol": "WIF", "funding_asset": SOL, "cost_raw": 5_000_000,
                    "raw": 250_000, "decimals": 6, "peak_price": 0.0213}
        self.assertIsNone(self.service.exit_reason(position, {"WIF": 2.13, "SOL": 100}))

    async def test_native_realized_profit_is_sol_not_fabricated_usdc(self):
        ledger = self.service.ledger
        buy = ledger.reserve(101, "buy", "BUY", SOL, 5_000_000,
            {"output_asset": self.mint, "symbol": "WIF", "decimals": 6})
        ledger.prepared(buy["id"], str(Signature.new_unique()), "test-not-sent")
        ledger.finalize(buy["id"], 5_000_000, 1000, 2_600_000)
        position = ledger.positions(101, "OPEN")[0]
        sell = ledger.reserve(101, "sell", "SELL", self.mint, position["raw"],
            {"output_asset": SOL}, position_id=position["id"])
        ledger.prepared(sell["id"], str(Signature.new_unique()), "test-not-sent")
        ledger.finalize(sell["id"], position["raw"], 1000, 6_000_000)
        self.service.reconcile = AsyncMock()
        response = await self.service.stats({"telegram_user": {"id": 101, "username": "test"}}, None)
        data = json.loads(response.text)
        self.assertEqual(data["profit_sol"], .001)
        self.assertEqual(data["profit_usdc"], 0)
        self.assertEqual(data["trades"][0]["funding_currency"], "SOL")

    async def test_slow_model_does_not_block_risk_polling(self):
        async def slow(prices):
            await asyncio.sleep(30)
        self.service.background_ai = slow
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key-not-real"}):
            self.assertIsNone(self.service.poll_ai(self.prices))
            self.assertIsNotNone(self.service.ai_task)
            self.assertFalse(self.service.ai_task.done())
        self.service.ai_task.cancel()
        await asyncio.gather(self.service.ai_task, return_exceptions=True)
