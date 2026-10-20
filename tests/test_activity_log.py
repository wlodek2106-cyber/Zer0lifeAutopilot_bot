"""Synthetic diagnostics only; no live AI, wallet, payment, signing or broadcast."""
import ast
import asyncio
import json
import re
import shutil
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from solders.keypair import Keypair
from solders.signature import Signature
import activity_log
from custody_ledger import CustodyLedger, SOL, USDC
from custody_runtime import CustodyRuntime, WRAPPED_SOL
from wallet_runtime import WalletError
import test_account_browser as browser


class ActivityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = temp.name + "/db"
        key = Keypair()
        self.runtime = CustodyRuntime(self.path, "https://invalid.example", 101,
            str(key.pubkey()), lambda: key, lambda uid, pin: (True, ""), AsyncMock(),
            {"SOL": {"mint": WRAPPED_SOL, "decimals": 9},
             "WIF": {"mint": str(Keypair().pubkey()), "decimals": 6}},
            lambda msg: None, base_url="https://invalid.example")
        self.ledger = self.runtime.ledger
        for uid in (101, 202):
            self.ledger.user(uid)
            for currency in (USDC, SOL):
                inv = self.ledger.invoice(uid, currency, 20_000_000)
                self.ledger.credit_deposit(inv["id"], str(Signature.new_unique()),
                                          currency, 20_000_000, int(time.time()))
        self.ledger.seed_fees(20_000_000, 100_000_000)
        self.ledger.settings(101, enabled=True, sniper_enabled=True)
        self.runtime.trade = AsyncMock(return_value={})
        self.runtime.rpc = AsyncMock()
        self.runtime.verify_sniper_mint = AsyncMock()
        p = patch.object(self.runtime.discovery, "entry_allowed", return_value=True)
        p.start()
        self.addCleanup(p.stop)
        self.prices = {"SOL": 100., "WIF": 2.}
        self.snapshot = {"id": "one", "created_at": int(time.time()),
            "universes": {"main": ["SOL"], "sniper": ["WIF"]},
            "prices": self.prices.copy(), "samples": {"SOL": 5, "WIF": 5},
            "decisions": {
                "main": {"action": "HOLD", "symbol": None, "reason": "No trend"},
                "sniper": {"action": "SELL", "symbol": "WIF", "reason": "Weak momentum"}}}

    async def apply(self, positions=None, prices=None):
        await self.runtime.apply_ai_decision(None, 101, self.ledger.settings(101),
            positions or [], self.prices if prices is None else prices, self.snapshot)

    def entries(self, uid=101):
        return activity_log.report(self.runtime, uid)["entries"]

    def codes(self):
        return {e.get("code") for e in self.entries()}

    async def test_jito_stage_is_account_scoped_idempotent_and_includes_elapsed_time(self):
        signature = str(Signature.new_unique())
        operation = {
            "id": "jito-op-fixture", "uid": 101, "kind": "BUY",
            "metadata": json.dumps({"execution_route": "jito_bundle_only"}),
            "created_at": int(time.time()) - 4, "signature": signature,
        }

        activity_log.record_jito_stage(self.runtime, operation, "jito_submission_unknown",
            "Jito acknowledgement is missing; the outcome is unknown.")
        activity_log.record_jito_stage(self.runtime, operation, "jito_submission_unknown",
            "Duplicate observation must not create another event.")

        matching = [entry for entry in self.entries() if entry.get("code") == "jito_submission_unknown"]
        self.assertEqual(len(matching), 1)
        self.assertIn("Elapsed 4s since reservation", matching[0]["message"])
        self.assertIn(signature[:10], matching[0]["message"])

    def open_position(self, uid=101):
        buy = self.ledger.reserve(uid, "close-test-" + str(uuid.uuid4()), "BUY", USDC,
            1_000_000, {"output_asset": self.runtime.watchlist["WIF"]["mint"],
                        "symbol": "WIF", "decimals": 6})
        self.ledger.prepared(buy["id"], str(Signature.new_unique()), "offline-wire")
        self.ledger.finalize(buy["id"], 1_000_000, 5000, 1_000_000)
        return self.ledger.positions(uid, "OPEN")[0]

    async def test_close_queue_status_is_visible_when_automated_modes_are_off(self):
        self.open_position()
        self.ledger.settings(101, enabled=False, sniper_enabled=False, close=True)

        result = activity_log.report(self.runtime, 101)

        self.assertIn("Automatic trading is paused.", result["activity_status"])
        self.assertIn("Manual close queue active: 1 open position(s) remaining.",
                      result["activity_status"])
        self.assertFalse(self.ledger.settings(101)["trading"])
        self.assertFalse(self.ledger.settings(101)["sniper_trading"])

    async def test_close_queue_attempts_owned_position_without_market_scan(self):
        position = self.open_position()
        self.ledger.settings(101, enabled=False, sniper_enabled=False, close=True)

        active = await self.runtime.process_close_queues(None)

        self.assertTrue(active)
        self.runtime.trade.assert_awaited_once_with(None, 101, position["asset"],
            position["funding_asset"], position["raw"], "SELL",
            {"output_asset": position["funding_asset"]}, position["id"], manual=True)
        self.runtime.rpc.assert_not_awaited()

    async def test_close_queue_failure_is_logged_and_uses_retry_backoff(self):
        position = self.open_position()
        self.ledger.settings(101, enabled=False, sniper_enabled=False, close=True)
        self.runtime.trade.side_effect = WalletError("Jupiter route unavailable")

        await self.runtime.process_close_queues(None)
        result = activity_log.report(self.runtime, 101)
        await self.runtime.process_close_queues(None)

        self.assertEqual(self.runtime.trade.await_count, 1)
        self.assertEqual(self.runtime._close_retry_after[101]["attempts"], 1)
        self.assertIn("Jupiter route unavailable", json.dumps(result["entries"]))
        self.assertIn("next retry is no sooner than 20 seconds", json.dumps(result["entries"]))
        self.assertEqual(self.ledger.positions(101, "OPEN")[0]["id"], position["id"])

    async def test_worker_prioritizes_close_queue_before_market_scanning(self):
        events = []
        self.runtime.reconcile = AsyncMock(side_effect=lambda *_: events.append("reconcile"))
        self.runtime.process_close_queues = AsyncMock(
            side_effect=lambda *_: events.append("close") or True)
        self.runtime.scan_deposits = AsyncMock(side_effect=lambda *_: events.append("deposits"))
        self.runtime.current_prices = AsyncMock()
        with patch.object(self.ledger, "credit_safe_partner_earnings",
                          side_effect=lambda: events.append("partners")):
            await self.runtime.worker_cycle(None)

        self.assertEqual(events, ["reconcile", "close", "deposits", "partners"])
        self.runtime.current_prices.assert_not_awaited()

    async def test_close_all_request_is_logged_and_disables_both_modes(self):
        self.open_position()
        self.ledger.settings(101, enabled=True, sniper_enabled=True)
        self.runtime.admit = Mock()

        class CloseRequest(dict):
            def __init__(self):
                super().__init__(telegram_user={"id": 101})

            async def json(self):
                return {"confirm": True}

        response = await self.runtime.close_all(CloseRequest(), None)
        settings = self.ledger.settings(101)
        result = activity_log.report(self.runtime, 101)

        self.assertEqual(response.status, 200)
        self.assertFalse(settings["trading"])
        self.assertFalse(settings["sniper_trading"])
        self.assertTrue(settings["close_requested"])
        self.assertIn("close_queue_queued", self.codes())
        self.assertIn("No new transaction was sent", json.dumps(result["entries"]))
        self.runtime.trade.assert_not_awaited()

    async def test_hold_and_sell_without_position_are_visible_and_deduplicated(self):
        await self.apply()
        before = self.entries()
        await self.apply()
        self.assertEqual(before, self.entries())
        self.assertTrue({"decision", "hold", "sell_skipped"} <= self.codes())
        self.assertIn("no owned open position", json.dumps(before))
        self.runtime.trade.assert_not_awaited()
        self.runtime.rpc.assert_not_awaited()
        self.assertTrue(self.ledger.audit())

    async def test_stale_decision_has_reason_and_cannot_execute(self):
        self.snapshot["created_at"] -= 121
        await self.apply()
        self.assertIn("stale", self.codes())
        self.runtime.trade.assert_not_awaited()

    async def test_price_and_relative_sol_rejections_are_visible(self):
        self.snapshot["decisions"]["main"] = {"action": "BUY", "symbol": "SOL", "reason": "Signal"}
        await self.apply(prices={"SOL": 102., "WIF": 2.})
        self.assertIn("price", self.codes())
        self.snapshot["id"] = "two"
        self.snapshot["decisions"]["main"]["action"] = "HOLD"
        await self.apply(prices={"SOL": 102., "WIF": 2.})
        self.assertIn("relative_price", self.codes())
        self.runtime.trade.assert_not_awaited()

    async def test_warmup_balance_duplicate_position_and_position_cap(self):
        self.snapshot["decisions"]["main"] = {"action": "BUY", "symbol": "SOL", "reason": "Signal"}
        self.snapshot["decisions"]["sniper"]["action"] = "HOLD"
        self.snapshot["samples"]["SOL"] = 1
        await self.apply()
        self.assertIn("warmup", self.codes())
        self.snapshot["samples"]["SOL"] = 5
        self.snapshot["id"] = "two"
        self.ledger.settings(101, raw=30_000_000)
        await self.apply()
        self.assertIn("balance", self.codes())
        self.snapshot["id"] = "three"
        await self.apply(positions=[{"funding_asset": USDC, "symbol": "SOL"}])
        self.assertIn("already_open", self.codes())
        self.snapshot["id"] = "four"
        await self.apply(positions=[{"funding_asset": USDC, "symbol": "SOL"}] * 5)
        self.assertIn("position_limit", self.codes())
        self.runtime.trade.assert_not_awaited()

    async def test_ineligible_candidate_and_failed_mint_check(self):
        self.snapshot["decisions"]["sniper"]["action"] = "BUY"
        self.runtime.discovery.entry_allowed.return_value = False
        await self.apply()
        self.assertIn("candidate", self.codes())
        self.runtime.discovery.entry_allowed.return_value = True
        self.runtime.verify_sniper_mint.side_effect = WalletError("unsafe")
        with self.assertRaises(WalletError):
            await self.apply()
        self.assertIn("mint_check", self.codes())
        self.runtime.trade.assert_not_awaited()

    async def test_execution_check_is_not_a_completed_trade_and_claims_unchanged(self):
        self.snapshot["decisions"]["main"] = {"action": "BUY", "symbol": "SOL", "reason": "Signal"}
        await self.apply()
        await self.apply()
        self.assertEqual(self.runtime.trade.await_count, 1)
        self.assertEqual(self.runtime.trade.await_args.args[2:6], (USDC, SOL, 1_000_000, "BUY"))
        self.assertTrue({"execution_check", "already_claimed"} <= self.codes())
        self.assertFalse(any(e["category"] == "TRANSACTION" for e in self.entries()))

    async def test_failed_execution_preserves_exception_and_does_not_log_secrets(self):
        self.snapshot["decisions"]["main"] = {"action": "BUY", "symbol": "SOL", "reason": "Signal"}
        self.runtime.trade.side_effect = WalletError("SECRET_PROVIDER_DATA")
        with self.assertRaises(WalletError):
            await self.apply()
        self.assertIn("execution_error", self.codes())
        self.assertNotIn("SECRET_PROVIDER_DATA", json.dumps(self.entries()))
        await self.apply()
        self.assertEqual(self.runtime.trade.await_count, 1)

    async def test_observation_failure_does_not_change_execution(self):
        self.snapshot["decisions"]["main"] = {"action": "BUY", "symbol": "SOL", "reason": "Signal"}
        original = self.ledger.db
        self.runtime.log = Mock()
        # Only activity writes fail, while claims/ledger reads still function.
        def db():
            import inspect
            if any(frame.function == "record" and frame.filename.endswith("activity_log.py")
                   for frame in inspect.stack()):
                raise OSError("synthetic disk error")
            return original()
        with patch.object(self.ledger, "db", side_effect=db):
            await self.apply()
        self.runtime.trade.assert_awaited_once()
        self.assertTrue(self.runtime.log.called)

    async def test_report_is_user_scoped_read_only_and_never_blank(self):
        balances = (self.ledger.balance(101, USDC), self.ledger.balance(202, SOL))
        activity_log.record(self.runtime, 202, self.snapshot, "main", "private", "OTHER_USER_PRIVATE")
        status = activity_log.report(self.runtime, 101)
        self.assertTrue(status["logs"] and status["entries"])
        self.assertNotIn("OTHER_USER_PRIVATE", json.dumps(status))
        self.assertEqual(balances, (self.ledger.balance(101, USDC), self.ledger.balance(202, SOL)))
        self.runtime.trade.assert_not_awaited()
        self.runtime.rpc.assert_not_awaited()
        self.assertTrue(self.ledger.settings(101)["trading"])

    async def test_history_survives_restart_and_is_bounded_per_user(self):
        for i in range(205):
            activity_log.record(self.runtime, 101, {**self.snapshot, "id": str(i)}, "main", "decision", str(i))
        activity_log.record(self.runtime, 202, self.snapshot, "main", "private", "private")
        reopened = CustodyLedger(self.path)
        with reopened.db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM custody_activity_events WHERE uid=101").fetchone()[0], 200)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM custody_activity_events WHERE uid=202").fetchone()[0], 1)
        self.assertTrue(reopened.audit())

    async def test_latest_result_is_visible_without_implying_execution_or_paused_mode_start(self):
        self.ledger.ai_record("one", self.snapshot)
        self.ledger.settings(101, enabled=False, sniper_enabled=False)
        result = activity_log.report(self.runtime, 101)
        self.assertIn("paused", result["activity_status"])
        self.assertIn("Decision only", json.dumps(result))
        self.assertEqual(len(result["entries"]), 2)
        self.assertFalse(self.ledger.settings(101)["trading"])
        self.runtime.trade.assert_not_awaited()

    async def test_provider_failure_status_is_safe_and_clears_on_success(self):
        self.ledger.ai_record("one", self.snapshot)
        future = asyncio.get_running_loop().create_future()
        future.set_exception(WalletError("SECRET_PROVIDER_ERROR"))
        self.runtime.ai_task = future
        with patch.dict("os.environ", {"OPENAI_API_KEY": "synthetic-not-a-real-key"}):
            self.runtime.poll_ai(self.prices)
            result = activity_log.report(self.runtime, 101)
            self.assertIn("Last AI refresh failed", result["activity_status"])
            self.assertNotIn("SECRET_PROVIDER_ERROR", json.dumps(result))
            future = asyncio.get_running_loop().create_future()
            future.set_result({})
            self.runtime.ai_task = future
            self.runtime.poll_ai(self.prices)
            self.assertNotIn("refresh failed", activity_log.report(self.runtime, 101)["activity_status"])
        self.runtime.trade.assert_not_awaited()

    async def test_transaction_signature_and_pending_state_not_called_completed(self):
        row = self.ledger.reserve(202, "foreign", "BUY", USDC, 1_000_000,
            {"output_asset": SOL, "symbol": "PRIVATE202", "decimals": 9})
        self.ledger.prepared(row["id"], str(Signature.new_unique()), "not-broadcast")
        foreign = self.ledger.operation(row["id"])["signature"]
        self.ledger.fail(row["id"], network_proven=True)
        row = self.ledger.reserve(101, "own", "BUY", USDC, 1_000_000,
            {"output_asset": SOL, "symbol": "SOL", "decimals": 9})
        signature = str(Signature.new_unique())
        self.ledger.prepared(row["id"], signature, "not-broadcast")
        self.ledger.submitted(row["id"], True)
        result = activity_log.report(self.runtime, 101)
        text = json.dumps(result)
        self.assertIn(signature, text)
        self.assertNotIn(foreign, text)
        self.assertIn("waiting for finalized", text)
        self.assertNotIn("COMPLETED", text)
        self.assertTrue(self.ledger.audit())

    async def test_failed_sell_shows_safe_cause_and_its_own_signature(self):
        mint = str(Keypair().pubkey())
        buy = self.ledger.reserve(101, "diagnostic-buy", "BUY", USDC, 1_000_000,
            {"output_asset": mint, "symbol": "WIF", "decimals": 6})
        self.ledger.prepared(buy["id"], str(Signature.new_unique()), "offline-wire")
        self.ledger.finalize(buy["id"], 1_000_000, 0, 2_000_000)
        position = self.ledger.positions(101, "OPEN")[0]
        sell = self.ledger.reserve(101, "diagnostic-sell", "SELL", mint, position["raw"],
            {"output_asset": USDC}, position_id=position["id"])
        signature = str(Signature.new_unique())
        self.ledger.prepared(sell["id"], signature, "offline-wire")
        self.ledger.fail(sell["id"], network_proven=True, fee=5_000,
                         failure_reason="Solana instruction failed; token=SECRET https://rpc.invalid")

        result = activity_log.report(self.runtime, 101)
        transaction = next(e for e in result["entries"]
                           if e["category"] == "TRANSACTION" and e["message"].startswith("SELL: FAILED"))
        self.assertIn("Cause: Solana instruction failed", transaction["message"])
        self.assertIn("[redacted]", transaction["message"])
        self.assertNotIn("SECRET", transaction["message"])
        self.assertNotIn("https://rpc.invalid", transaction["message"])
        self.assertEqual(transaction["signature"], signature)
        self.assertEqual(self.ledger.positions(101, "OPEN")[0]["state"], "OPEN")
        self.assertTrue(self.ledger.audit())

    async def test_failure_reason_sanitizer_hides_bearer_and_urls(self):
        reason = activity_log.safe_failure_reason(
            "Bearer super-secret api_key=abc123 at https://service.example/path")
        self.assertNotIn("super-secret", reason)
        self.assertNotIn("abc123", reason)
        self.assertNotIn("https://", reason)


@unittest.skipUnless(shutil.which("chromium"), "Chromium not installed")
class ActivityBrowserTests(unittest.TestCase):
    def test_mobile_empty_state_safe_text_local_time_and_transaction_lines(self):
        source = ast.parse(Path(__file__).resolve().parents[1].joinpath("main.py").read_text())
        page = next(ast.literal_eval(n.value) for n in source.body if isinstance(n, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == "RAW_HTML" for t in n.targets))
        page = re.sub(r'<script src="[^"]+"></script>', '', page)
        page = page.replace("</head>", """<script>
        window.Telegram={WebApp:{initData:'fixture',initDataUnsafe:{user:{id:101}},expand(){},ready(){}}};
        window.logFixture={success:true,logs:[]};
        window.fetch=async(url)=>new Response(JSON.stringify(url==='/api/logs'?window.logFixture:
            {success:true,user_balance:0,sol_balance:0,withdrawable:0,trades:[],wallet_history:[],
             trading_settings:{},wallet_status:{}}));
        </script></head>""")
        browser.AccountBrowserTests().run_page(page, """
            await refresh();
            const box=document.getElementById('log-box');
            assert(box.textContent.includes('Waiting for activity'),'empty state is not black');
            window.logFixture={success:true,activity_status:'Core OFF; Manual close queue active: 1 open position(s) remaining',
              entries:[{created_at:1700000000,category:'AI',message:'<img src=x onerror=alert(1)> HOLD'},
                       {created_at:1700000001,category:'TRANSACTION',message:'BUY: SUBMITTED',
                        signature:'123456789ABCDEFGHIJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'}]};
            await refresh();
            assert(box.textContent.includes('Manual close queue active'),'queue status is visible in the log');
            assert(box.querySelector('img')===null,'AI text cannot inject HTML');
            assert(box.textContent.includes('<img'),'AI text rendered literally');
            assert(box.textContent.includes('Tx: 123'),'transaction signature shown');
            assert(box.textContent.includes(new Date(1700000000*1000).toLocaleString()),'device-local timestamp');
            assert(box.textContent.split('\\n').length>=4,'real line breaks, not literal slash-n');
            assert(box.style.overflowWrap==='anywhere','long signatures wrap on mobile');
            window.logFixture={success:false};
            await refresh();
            assert(box.textContent.includes('Activity log unavailable'),'API errors do not leave a blank screen');
        """)
