import asyncio
import json
import os
import sqlite3
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import AsyncMock, patch

from solders.signature import Signature

from custody_ledger import CustodyLedger, LedgerError, SOL, USDC
from custody_runtime import CustodyRuntime
import execution_policy
import trade_audit


REPORT = {"assessment": "The recorded stop caused the sale.",
          "lesson": "One outcome is limited evidence.", "evidence_quality": "LIMITED"}


class TradeAuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = self.tmp.name + "/db"
        self.ledger = CustodyLedger(self.path)
        self.ledger.seed_fees(20_000_000, 100_000_000)
        for uid in (101, 202):
            self.ledger.user(uid)
            for asset, raw in ((SOL, 50_000_000), (USDC, 50_000_000)):
                invoice = self.ledger.invoice(uid, asset, raw)
                self.ledger.credit_deposit(invoice["id"], str(Signature.new_unique()),
                                           asset, raw, int(time.time()))

    def buy(self, funding=SOL, uid=101, ai=True, complete=True):
        mint = "mint" + str(Signature.new_unique())
        info = {"output_asset": mint, "symbol": "TOKEN", "decimals": 6}
        if ai:
            aid = self.ledger.ai_allow_call(0, 100)
            mode = "sniper" if funding == SOL else "main"
            self.ledger.ai_record(aid, {
                "decisions": {mode: {"action": "BUY", "symbol": "TOKEN",
                                    "reason": "Closed green candle confirms upward continuation."}},
                "risk_review": {"analysis_id": aid, "verdicts": {mode: {
                    "symbol": "TOKEN", "approved": True, "trend": "UP",
                    "reason": "Upward trend and bounded entry."}}},
                ("sniper_market_context" if funding == SOL else "main_market_context"): {
                    "TOKEN": {"chart_ready": 1, "chart_trend_h1": 1, "chart_buy_setup": 1,
                              "wallet": "DO_NOT_SEND"}}})
            info["ai_analysis"] = aid
        row = self.ledger.reserve(uid, mint, "BUY", funding, 5_000_000, info)
        if complete:
            self.ledger.prepared(row["id"], str(Signature.new_unique()), "SECRET_WIRE")
            self.ledger.finalize(row["id"], 5_000_000, 2000, 1_000_000)
        return row

    def sell(self, buy, reason="STOP_LOSS", snapshot=True, manual=False):
        position = self.ledger.positions(buy["uid"], "OPEN")[0]
        entry = CustodyRuntime.position_entry_price(position)
        info = {"output_asset": position["funding_asset"], "exit_reason": reason,
                "manual_close": manual}
        if snapshot:
            info["exit_snapshot"] = trade_audit.exit_snapshot(
                position, entry * .94, entry, int(time.time()), execution_policy.VERSION)
            info["exit_snapshot"]["wire"] = "DO_NOT_SAVE"
        row = self.ledger.reserve(buy["uid"], "sell-" + buy["id"], "SELL", position["asset"],
            position["raw"], info, position_id=position["id"])
        self.ledger.prepared(row["id"], str(Signature.new_unique()), "SECRET_WIRE")
        self.ledger.finalize(row["id"], position["raw"], 3000, 4_600_000)
        return row

    def test_each_mode_gets_bound_entry_and_exit_evidence_and_correct_currency(self):
        for funding in (SOL, USDC):
            buy = self.buy(funding)
            sell = self.sell(buy)
            rows = {r["kind"]: r for r in self.ledger.trade_review_history(101)
                    if r["position_id"] == buy["id"]}
            self.assertEqual(set(rows), {"BUY", "SELL"})
            self.assertEqual(rows["BUY"]["facts"]["trigger"]["source"], "AI_BUY")
            self.assertTrue(rows["BUY"]["facts"]["trigger"]["risk_approved"])
            self.assertEqual(rows["SELL"]["facts"]["trigger"]["source"], "STOP_LOSS")
            self.assertEqual(rows["SELL"]["facts"]["funding_currency"],
                             "SOL" if funding == SOL else "USDC")
            self.assertAlmostEqual(rows["SELL"]["facts"]["return_pct"], -8)
            self.assertAlmostEqual(rows["SELL"]["facts"]["trigger"]["snapshot"]["observed_return_pct"], -6)
            self.assertEqual(rows["SELL"]["facts"]["entry_evidence"]["source"], "AI_BUY")
            self.ledger.finalize(sell["id"], 1_000_000, 3000, 4_600_000)
            self.assertEqual(len([r for r in self.ledger.trade_review_history(101)
                                  if r["position_id"] == buy["id"]]), 2)
        self.assertTrue(self.ledger.audit())

    def test_pending_failed_and_unknown_do_not_create_completed_evidence(self):
        buy = self.buy(complete=False)
        self.assertEqual(self.ledger.trade_review_history(101), [])
        self.ledger.prepared(buy["id"], str(Signature.new_unique()), "SECRET_WIRE")
        self.ledger.submitted(buy["id"], False)
        self.assertEqual(self.ledger.trade_review_history(101), [])
        self.ledger.fail(buy["id"], network_proven=True, failure_reason="proven failure")
        self.assertEqual(self.ledger.trade_review_history(101), [])

    def test_missing_reason_is_unknown_and_manual_is_explicit(self):
        buy = self.buy(ai=False)
        self.sell(buy, reason=None, snapshot=False)
        facts = self.ledger.trade_review_history(101)[0]["facts"]
        self.assertEqual(facts["trigger"], {"source": "UNKNOWN", "snapshot": {}})
        self.assertEqual(facts["entry_evidence"]["source"], "UNKNOWN")
        other = self.buy(ai=False)
        self.sell(other, manual=True, reason=None, snapshot=False)
        self.assertEqual(self.ledger.trade_review_history(101)[0]["facts"]["trigger"]["source"],
                         "MANUAL_SELL")

    def test_only_owner_reads_audit_and_no_wire_or_wallet_saved(self):
        buy = self.buy()
        self.sell(buy)
        self.assertEqual(self.ledger.trade_review_history(202), [])
        serialized = json.dumps(self.ledger.trade_review_history(101))
        for secret in ("SECRET_WIRE", "DO_NOT_SEND", "DO_NOT_SAVE"):
            self.assertNotIn(secret, serialized)
        reopened = CustodyLedger(self.path)
        self.assertEqual(reopened.trade_review_history(101), self.ledger.trade_review_history(101))

    def test_finalized_facts_are_immutable(self):
        buy = self.buy()
        with self.assertRaises(sqlite3.IntegrityError):
            with self.ledger.db() as db:
                db.execute("UPDATE custody_trade_reviews SET facts='{}' WHERE operation_id=?", (buy["id"],))
        with self.assertRaises(sqlite3.IntegrityError):
            with self.ledger.db() as db:
                db.execute("DELETE FROM custody_trade_reviews")
        self.assertTrue(self.ledger.audit())

    def test_shared_budget_reserves_room_for_entry_and_does_not_charge_on_wait(self):
        self.buy()
        with self.ledger.db() as db:
            before = db.execute("SELECT COUNT(*) FROM custody_ai_calls").fetchone()[0]
        self.assertIsNone(self.ledger.claim_trade_review(3))
        row = self.ledger.trade_review_history(101)[0]
        self.assertEqual(row["state"], "WAITING_BUDGET")
        with self.ledger.db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM custody_ai_calls").fetchone()[0], before)

    def test_concurrent_workers_cannot_claim_same_event(self):
        self.buy()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.ledger.claim_trade_review(100), range(2)))
        self.assertEqual(sum(row is not None for row in results), 1)
        claim = next(row for row in results if row)
        before = self.ledger.balance(101, SOL)
        self.ledger.finish_trade_review(claim, REPORT, "test-model")
        self.ledger.finish_trade_review(claim, {**REPORT, "assessment": "overwrite"}, "test-model")
        self.assertEqual(self.ledger.trade_review_history(101)[0]["report"], REPORT)
        self.assertEqual(self.ledger.balance(101, SOL), before)

    def test_retry_lease_is_bounded_and_old_worker_cannot_replace_new_report(self):
        self.buy()
        now = int(time.time())
        first = self.ledger.claim_trade_review(100, now)
        second = self.ledger.claim_trade_review(100, now + 121)
        self.assertEqual(second["attempts"], 2)
        self.ledger.finish_trade_review(first, REPORT, "stale", now + 122)
        self.assertEqual(self.ledger.trade_review_history(101)[0]["state"], "RUNNING")
        self.ledger.finish_trade_review(second, None, "test", now + 123)
        third = self.ledger.claim_trade_review(100, now + 800)
        self.assertEqual(third["attempts"], 3)
        self.ledger.finish_trade_review(third, None, "test", now + 801)
        self.assertIsNone(self.ledger.claim_trade_review(100, now + 2000))
        self.assertEqual(self.ledger.trade_review_history(101)[0]["state"], "UNAVAILABLE")

    def test_audit_reservation_does_not_trigger_analyst_failure_backoff(self):
        self.buy()
        now = int(time.time()) + 2
        with patch("custody_ledger.time.time", return_value=now):
            self.ledger.claim_trade_review(100, now)
            self.assertIsInstance(self.ledger.ai_allow_call(0, 100), str)

    def test_ai_decision_asset_mismatch_cannot_be_recorded_as_entry_reason(self):
        buy = self.buy(complete=False)
        with self.ledger.db() as db:
            record = {"decisions": {"sniper": {"action": "BUY", "symbol": "OTHER",
                                              "reason": "wrong token"}}}
            db.execute("UPDATE custody_ai_analyses SET decisions=?", (json.dumps(record),))
        self.ledger.prepared(buy["id"], str(Signature.new_unique()), "test")
        self.ledger.finalize(buy["id"], 5_000_000, 0, 1_000_000)
        self.assertEqual(self.ledger.trade_review_history(101)[0]["facts"]["trigger"]["source"], "UNKNOWN")


class TradeReviewRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_network_error_never_touches_trade_or_lock_and_keeps_facts(self):
        service = object.__new__(CustodyRuntime)
        service.ai_lock = asyncio.Lock()
        service.log = lambda _: None
        helper = TradeAuditTests()
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        service.ledger = helper.ledger
        helper.buy()
        before = helper.ledger.balance(101, SOL)
        service.trade = AsyncMock(side_effect=AssertionError("Audit cannot trade"))
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test"}), \
                patch("trade_audit.review", AsyncMock(side_effect=TimeoutError())):
            await service.process_trade_review(None)
        service.trade.assert_not_called()
        self.assertEqual(helper.ledger.balance(101, SOL), before)
        self.assertEqual(helper.ledger.trade_review_history(101)[0]["attempts"], 1)
        self.assertFalse(service.ai_lock.locked())

    async def test_existing_entry_review_has_priority_and_missing_key_spends_nothing(self):
        service = object.__new__(CustodyRuntime)
        service.ai_lock = asyncio.Lock()
        service.ledger = unittest.mock.Mock()
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            await service.process_trade_review(None)
        service.ledger.claim_trade_review.assert_not_called()
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test"}):
            async with service.ai_lock:
                await service.process_trade_review(None)
        service.ledger.claim_trade_review.assert_not_called()

    async def test_model_payload_is_advisory_private_and_schema_validated(self):
        class Session:
            def post(self, url, **kwargs):
                self.payload = kwargs["json"]
                return self
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                return False
            async def json(self):
                return {"choices": [{"finish_reason": "stop",
                    "message": {"content": json.dumps(self.report)}}]}
            status = 200
            report = REPORT
        session = Session()
        facts = {"kind": "SELL", "funding_currency": "SOL", "trigger": {"source": "UNKNOWN"},
                 "uid": 101, "wire": "SECRET", "signature": "PRIVATE_SIGNATURE",
                 "token_quantity": 500, "funding_raw": 99000000}
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test"}):
            self.assertEqual(await trade_audit.review(session, facts, "test-model"), REPORT)
            content = session.payload["messages"][1]["content"]
            for private in ("uid", "SECRET", "PRIVATE_SIGNATURE", "funding_raw", "token_quantity"):
                self.assertNotIn(private, content)
            self.assertFalse(session.payload["store"])
            self.assertIn("Never invent", session.payload["messages"][0]["content"])
            session.report = {**REPORT, "place_order": True}
            with self.assertRaises(trade_audit.AuditReviewError):
                await trade_audit.review(session, facts, "test-model")

    def test_ui_escapes_evidence_and_keeps_facts_separate_from_ai(self):
        source = Path(__file__).resolve().parents[1].joinpath("main.py").read_text()
        self.assertIn("function tradeAuditText", source)
        self.assertIn("tradeAuditText(report.assessment)", source)
        self.assertIn("AI interpretation — not execution evidence", source)
        self.assertIn("No audit recorded for this historical trade", source)
        self.assertIn("${renderTradeAudit(t)}", source)


if __name__ == "__main__":
    unittest.main()
