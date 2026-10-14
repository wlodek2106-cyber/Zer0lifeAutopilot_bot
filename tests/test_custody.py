import asyncio
import base64
import copy
import hashlib
import hmac
import json
import sqlite3
import tempfile
import time
import unittest
import uuid
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlencode
from unittest.mock import AsyncMock, patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from solders.hash import Hash
from solders.keypair import Keypair
from solders.message import Message, MessageV0
from solders.signature import Signature
from solders.transaction import Transaction, VersionedTransaction

import custody_runtime as runtime
from custody_ledger import CustodyLedger, LedgerError, USDC, SOL, FEE_CAP, available, position_account
import wallet_runtime as legacy


class Request(dict):
    def __init__(self, uid=101, body=None):
        super().__init__(telegram_user={"id": uid, "username": "user" + str(uid)})
        self.body = body or {}
        self.match_info = {}

    async def json(self):
        return self.body


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = self.tmp.name + "/ledger.db"
        self.ledger = CustodyLedger(self.path)
        for uid in (101, 202):
            self.ledger.user(uid)
        self.ledger.seed_fees(20_000_000, 50_000_000)
        self.credit(101, USDC, 10_000_000)
        self.credit(202, USDC, 30_000_000)

    def tearDown(self):
        self.assertTrue(self.ledger.audit())

    def credit(self, uid, asset, raw):
        invoice = self.ledger.invoice(uid, asset, raw)
        signature = str(Signature.new_unique())
        self.ledger.credit_deposit(invoice["id"], signature, asset, raw, invoice["created_at"])
        return invoice, signature

    def reserve(self, uid=101, kind="WITHDRAW", asset=USDC, raw=1_000_000, info=None, position=None):
        return self.ledger.reserve(uid, str(uuid.uuid4()), kind, asset, raw, info or {}, "recipient", position)

    def complete(self, op, spent=1_000_000, fee=5000, received=0):
        self.ledger.prepared(op["id"], str(Signature.new_unique()), "offline-wire")
        self.ledger.finalize(op["id"], spent, fee, received)

    def test_separate_balances_on_one_wallet(self):
        self.assertEqual(self.ledger.balance(101, USDC), 10_000_000)
        self.assertEqual(self.ledger.balance(202, USDC), 30_000_000)

    def test_no_borrowing_from_another_customer(self):
        with self.assertRaises(LedgerError):
            self.reserve(raw=11_000_000)
        self.assertEqual(self.ledger.balance(202, USDC), 30_000_000)

    def test_deposit_replay_once(self):
        invoice, sig = self.credit(101, SOL, 1_000_000)
        self.ledger.credit_deposit(invoice["id"], sig, SOL, 1_000_000, invoice["created_at"])
        self.assertEqual(self.ledger.balance(101, SOL), 1_000_000)

    def test_same_signature_cannot_credit_two_accounts(self):
        invoice, sig = self.credit(101, SOL, 1_000_000)
        second = self.ledger.invoice(202, SOL, 1_000_000)
        with self.assertRaises(LedgerError):
            self.ledger.credit_deposit(second["id"], sig, SOL, 1_000_000, second["created_at"])
        self.assertEqual(self.ledger.balance(202, SOL), 0)

    def test_second_transfer_to_used_memo_not_double_credited(self):
        invoice, sig = self.credit(101, SOL, 1_000_000)
        with self.assertRaises(LedgerError):
            self.ledger.credit_deposit(invoice["id"], "new-sig", SOL, 1_000_000, invoice["created_at"])

    def test_wrong_deposit_asset_and_amount(self):
        i = self.ledger.invoice(101, USDC, 1_000_000)
        for asset, raw in ((SOL, 1_000_000), (USDC, 999999)):
            with self.assertRaises(LedgerError):
                self.ledger.credit_deposit(i["id"], "invalid", asset, raw, i["created_at"])

    def test_expired_onchain_payment_not_credited(self):
        i = self.ledger.invoice(101, USDC, 1_000_000)
        with self.assertRaises(LedgerError):
            self.ledger.credit_deposit(i["id"], "invalid", USDC, 1_000_000, i["expires_at"] + 1)

    def test_delayed_finalization_uses_block_time(self):
        i = self.ledger.invoice(101, USDC, 1_000_000)
        with patch("custody_ledger.time.time", return_value=i["expires_at"] + 3600):
            self.ledger.credit_deposit(i["id"], "late-confirmation", USDC, 1_000_000, i["created_at"])
        self.assertEqual(self.ledger.balance(101, USDC), 11_000_000)

    def test_atomic_reservation_is_not_completion(self):
        op = self.reserve()
        self.assertEqual(op["state"], "RESERVED")
        self.assertEqual(self.ledger.balance(101, USDC), 9_000_000)
        self.assertEqual(self.ledger.balance(202, USDC), 30_000_000)

    def test_user_sol_withdrawal_never_debits_usdc(self):
        self.credit(101, SOL, 3_000_000)
        op = self.reserve(asset=SOL, raw=1_000_000)
        self.complete(op)
        self.assertEqual(self.ledger.balance(101, SOL), 2_000_000)
        self.assertEqual(self.ledger.balance(101, USDC), 10_000_000)

    def test_fee_is_operator_only(self):
        self.credit(202, SOL, 9_000_000)
        op = self.reserve()
        self.complete(op, fee=2_044_280)
        self.assertEqual(self.ledger.balance(202, SOL), 9_000_000)
        with self.ledger.db() as db:
            self.assertEqual(db.execute("SELECT raw FROM custody_accounts WHERE account='treasury'").fetchone()[0], 17_955_720)

    def test_unsent_failure_restores_only_its_owner(self):
        op = self.reserve()
        self.ledger.fail(op["id"])
        self.ledger.fail(op["id"])
        self.assertEqual(self.ledger.balance(101, USDC), 10_000_000)
        self.assertEqual(self.ledger.balance(202, USDC), 30_000_000)

    def test_ambiguous_send_cannot_refund_or_spend_again(self):
        op = self.reserve()
        self.ledger.prepared(op["id"], "signature", "wire")
        self.ledger.submitted(op["id"], False)
        with self.assertRaises(LedgerError):
            self.ledger.fail(op["id"])
        with self.assertRaises(LedgerError):
            self.reserve(uid=202)
        self.assertEqual(self.ledger.balance(101, USDC), 9_000_000)

    def test_proven_failure_charges_network_fee_atomically(self):
        op = self.reserve()
        self.ledger.prepared(op["id"], "signature", "wire")
        self.ledger.fail(op["id"], network_proven=True, fee=5000)
        self.assertEqual(self.ledger.balance(101, USDC), 10_000_000)
        with self.ledger.db() as db:
            self.assertEqual(db.execute("SELECT raw FROM custody_accounts WHERE account='treasury'").fetchone()[0], 19_995_000)

    def test_completion_replay_no_second_debit(self):
        op = self.reserve()
        self.complete(op)
        self.ledger.finalize(op["id"], 1_000_000, 5000)
        self.assertEqual(self.ledger.balance(101, USDC), 9_000_000)

    def test_actual_fill_belongs_to_one_user(self):
        op = self.reserve(kind="BUY", info={"output_asset": SOL, "symbol": "SOL", "decimals": 9})
        self.complete(op, received=8_888_888)
        self.assertEqual(self.ledger.positions(101)[0]["raw"], 8_888_888)
        self.assertEqual(self.ledger.positions(202), [])
        self.assertEqual(self.ledger.balance(202, USDC), 30_000_000)

    def test_cannot_sell_another_users_position(self):
        op = self.reserve(kind="BUY", info={"output_asset": SOL, "symbol": "SOL", "decimals": 9})
        self.complete(op, received=8_888_888)
        with self.assertRaises(LedgerError):
            self.reserve(uid=202, kind="SELL", asset=SOL, raw=8_888_888, position=op["id"])

    def test_actual_sale_profit_and_loss_owner_only(self):
        for proceeds in (1_100_000, 900_000):
            buy = self.reserve(kind="BUY", info={"output_asset": SOL, "symbol": "SOL", "decimals": 9})
            self.complete(buy, received=8_888_888)
            sale = self.reserve(kind="SELL", asset=SOL, raw=8_888_888, position=buy["id"], info={"output_asset": USDC})
            self.complete(sale, spent=8_888_888, received=proceeds)
            self.assertEqual(self.ledger.positions(101)[0]["state"], "CLOSED")
        self.assertEqual(self.ledger.balance(101, USDC), 10_000_000)
        self.assertEqual(self.ledger.balance(202, USDC), 30_000_000)

    def test_reject_over_cap_finalization_keeps_reserve(self):
        op = self.reserve()
        self.ledger.prepared(op["id"], "signature", "wire")
        with self.assertRaises(LedgerError):
            self.ledger.finalize(op["id"], 1_000_001, 5000)
        self.assertEqual(self.ledger.operation(op["id"])["state"], "PREPARED")

    def test_request_id_scoped_to_user_and_idempotent(self):
        key = str(uuid.uuid4())
        first = self.ledger.reserve(101, key, "WITHDRAW", USDC, 1_000_000, {})
        same = self.ledger.reserve(101, key, "WITHDRAW", USDC, 1_000_000, {})
        self.assertEqual(first["id"], same["id"])
        self.ledger.fail(first["id"])
        second = self.ledger.reserve(202, key, "WITHDRAW", USDC, 1_000_000, {})
        self.assertNotEqual(first["id"], second["id"])

    def test_concurrent_processes_cannot_double_reserve(self):
        other = CustodyLedger(self.path)
        def attempt(ledger):
            try:
                return ledger.reserve(101, str(uuid.uuid4()), "WITHDRAW", USDC, 7_000_000, {})["id"]
            except LedgerError:
                return None
        with ThreadPoolExecutor(2) as executor:
            results = list(executor.map(attempt, [self.ledger, other]))
        self.assertEqual(sum(r is not None for r in results), 1)
        self.assertEqual(self.ledger.balance(101, USDC), 3_000_000)

    def test_restart_pauses_and_refunds_unsent_not_prepared(self):
        self.ledger.settings(101, enabled=True)
        op = self.reserve()
        self.ledger.restart_pause()
        self.assertFalse(self.ledger.settings(101)["trading"])
        self.assertEqual(self.ledger.operation(op["id"])["state"], "FAILED")
        op2 = self.reserve()
        self.ledger.prepared(op2["id"], "signature", "wire")
        self.ledger.restart_pause()
        self.assertEqual(self.ledger.operation(op2["id"])["state"], "PREPARED")

    def test_fee_seed_cannot_use_existing_claims(self):
        with self.assertRaises(LedgerError):
            self.ledger.seed_fees(1_000_000, 20_000_000)

    def test_operator_can_move_own_sol_to_fees_not_other_customers(self):
        self.credit(101, SOL, 10_000_000)
        self.credit(202, SOL, 9_000_000)
        self.ledger.seed_fees(10_000_000, 39_000_000, owner_uid=101)
        self.assertEqual(self.ledger.balance(101, SOL), 0)
        self.assertEqual(self.ledger.balance(202, SOL), 9_000_000)
        with self.assertRaises(LedgerError):
            self.ledger.seed_fees(1, 39_000_000, owner_uid=101)

    def test_opening_is_backed_explicit_single_time(self):
        self.ledger.opening(101, USDC, 5_000_000, 45_000_000)
        self.assertEqual(self.ledger.balance(101, USDC), 15_000_000)
        with self.assertRaises(LedgerError):
            self.ledger.opening(101, USDC, 1, 100_000_000)
        with self.assertRaises(LedgerError):
            self.ledger.opening(101, SOL, 50_000_000, 30_000_000)

    def test_virtual_legacy_balances_never_imported(self):
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("CREATE TABLE users(telegram_id INTEGER,balance_usdc REAL)")
            db.execute("INSERT INTO users VALUES(303,999999)")
            db.commit()
        again = CustodyLedger(self.path)
        again.user(303)
        self.assertEqual(again.balance(303, USDC), 0)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT balance_usdc FROM users").fetchone()[0], 999999)

    def test_first_migration_creates_consistent_legacy_backup(self):
        import os
        path = self.tmp.name + "/old.db"
        with closing(sqlite3.connect(path)) as db:
            db.execute("CREATE TABLE users(telegram_id INTEGER,balance_usdc REAL)")
            db.execute("INSERT INTO users VALUES(101,123.45)")
            db.commit()
        CustodyLedger(path)
        with closing(sqlite3.connect(path + ".pre-custody.bak")) as db:
            self.assertEqual(db.execute("SELECT balance_usdc FROM users").fetchone()[0], 123.45)
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='custody_accounts'").fetchone())
        self.assertEqual(os.stat(path + ".pre-custody.bak").st_mode & 0o777, 0o600)

    def test_journal_is_immutable(self):
        with self.assertRaises(sqlite3.IntegrityError):
            with self.ledger.db() as db:
                db.execute("UPDATE custody_entries SET delta=1")

    def test_audit_detects_tampered_cached_balance(self):
        with self.ledger.db() as db:
            db.execute("UPDATE custody_accounts SET raw=raw+1 WHERE account=? AND asset=?", (available(101), USDC))
        with self.assertRaises(LedgerError):
            self.ledger.audit()
        with self.ledger.db() as db:
            db.execute("UPDATE custody_accounts SET raw=raw-1 WHERE account=? AND asset=?", (available(101), USDC))


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_personal_trade_size_saved_without_enabling_or_spending(self):
        self.ledger.credit_deposit(self.ledger.invoice(101, USDC, 3_000_000)["id"],
            str(Signature.new_unique()), USDC, 3_000_000, int(time.time()))
        response = await self.service.save_trading_settings(Request(101, {"order_usdc": "2", "uid": 202}), None)
        self.assertEqual(response.status, 200)
        self.assertEqual(self.ledger.settings(101)["order_raw"], 2_000_000)
        self.assertEqual(self.ledger.settings(202)["order_raw"], 1_000_000)
        self.assertFalse(self.ledger.settings(101)["trading"])
        self.assertEqual(self.ledger.balance(101, USDC), 3_000_000)
        self.assertFalse(any(method == "sendTransaction" for method, _ in self.calls))

    async def test_trade_size_cannot_exceed_own_cash(self):
        with self.assertRaises(legacy.WalletError):
            await self.service.save_trading_settings(Request(101, {"order_usdc": "2"}), None)
        self.assertFalse(self.ledger.settings(101)["trading"])

    async def test_trade_size_cannot_change_while_running(self):
        self.ledger.settings(101, enabled=True)
        with self.assertRaises(legacy.WalletError):
            await self.service.save_trading_settings(Request(101, {"order_usdc": "1"}), None)
        self.assertEqual(self.ledger.settings(101)["order_raw"], 1_000_000)

    async def test_readiness_does_not_start_trading(self):
        self.ledger.credit_deposit(self.ledger.invoice(101, USDC, 2_000_000)["id"],
            str(Signature.new_unique()), USDC, 2_000_000, int(time.time()))
        self.service.trading_preflight = AsyncMock()
        response = await self.service.trading_readiness(Request(), None)
        self.assertEqual(response.status, 200)
        self.service.trading_preflight.assert_awaited_once_with(101, None, 1_000_000)
        self.assertFalse(self.ledger.settings(101)["trading"])
        self.assertFalse(any(method == "sendTransaction" for method, _ in self.calls))

    async def test_failed_preflight_leaves_autopilot_off(self):
        self.ledger.credit_deposit(self.ledger.invoice(101, USDC, 2_000_000)["id"],
            str(Signature.new_unique()), USDC, 2_000_000, int(time.time()))
        self.service.trading_preflight = AsyncMock(side_effect=legacy.WalletError("Market unavailable", 503))
        with self.assertRaises(legacy.WalletError):
            await self.service.toggle(Request(101, {"trade_amount_usdc": "1"}), None)
        self.assertFalse(self.ledger.settings(101)["trading"])

    async def test_preflight_requires_market_prices_without_sending(self):
        with self.assertRaisesRegex(legacy.WalletError, "котировок недоступен"):
            await self.service.trading_preflight(101, None, 1_000_000)
        self.assertFalse(self.ledger.settings(101)["trading"])
        self.assertFalse(any(method == "sendTransaction" for method, _ in self.calls))

    async def test_stats_shows_saved_size_and_tx_only_deposit(self):
        sender = str(Keypair().pubkey())
        self.ledger.link_wallet(self.ledger.wallet_challenge(101, "https://example.invalid"), sender)
        with self.ledger.db() as db:
            db.execute("UPDATE custody_tx_epoch SET started_at=?", (int(time.time()) - 100,))
        self.ledger.credit_tx(101, str(Signature.new_unique()), sender, {USDC: 1_500_000},
            int(time.time()), {USDC: 100_000_000, SOL: 50_000_000})
        response = await self.service.stats(Request(), None)
        data = json.loads(response.text)
        self.assertEqual(data["total_deposited"], 1.5)
        self.assertEqual(data["trading_settings"]["order_usdc"], 1)
        self.assertEqual(data["wallet_history"][0]["amount"], "1.5 USDC")

    async def test_stats_exposes_pending_operation_only_to_its_own_account(self):
        invoice = self.ledger.invoice(202, USDC, 2_000_000)
        self.ledger.credit_deposit(invoice["id"], str(Signature.new_unique()),
                                   USDC, 2_000_000, invoice["created_at"])
        operation = self.ledger.reserve(202, "account-pending-diagnostic", "BUY", USDC, 1_000_000,
                                       {"output_asset": runtime.WRAPPED_SOL}, str(self.recipient))
        signature = str(Signature.new_unique())
        self.ledger.prepared(operation["id"], signature, "signed-wire")
        self.service.reconcile = AsyncMock()

        own_response = await self.service.stats(Request(202), None)
        own = json.loads(own_response.text)
        self.assertTrue(own["operation_pending"])
        self.assertEqual(len(own["pending_operations"]), 1)
        pending = own["pending_operations"][0]
        self.assertEqual(pending["kind"], "BUY")
        self.assertEqual(pending["state"], "PREPARED")
        self.assertEqual(pending["signature"], signature)
        self.assertEqual(set(pending), {"kind", "state", "created_at", "signature"})

        other_response = await self.service.stats(Request(101), None)
        other = json.loads(other_response.text)
        self.assertFalse(other["operation_pending"])
        self.assertEqual(other["pending_operations"], [])
        self.assertEqual(self.ledger.operation(operation["id"])["state"], "PREPARED")
        self.assertEqual(self.calls, [])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.signer = Keypair()
        self.recipient = Keypair().pubkey()
        self.service = runtime.CustodyRuntime(self.tmp.name + "/db", "https://invalid.example",
            101, str(self.signer.pubkey()), lambda: self.signer, lambda uid, pin: (pin == "123456", ""),
            AsyncMock(return_value={}), {"SOL": {"mint": runtime.WRAPPED_SOL, "decimals": 9}},
            lambda msg: None, base_url="https://example.invalid")
        self.service.public_enabled = True
        storage = patch.object(legacy, "durable", return_value=True)
        storage.start()
        self.addCleanup(storage.stop)
        self.ledger = self.service.ledger
        self.ledger.user(101)
        self.ledger.user(202)
        self.ledger.seed_fees(20_000_000, 50_000_000)
        self.network = legacy.GENESIS
        self.transactions = {}
        self.status = None
        self.calls = []
        self.service.rpc = self.rpc
        self.service.balances = AsyncMock(return_value={
            SOL: {"raw": 50_000_000, "decimals": 9},
            runtime.WRAPPED_SOL: {"raw": 50_000_000, "decimals": 9},
            USDC: {"raw": 100_000_000, "decimals": 6}})

    async def rpc(self, session, method, params=None):
        self.calls.append((method, params))
        if method == "getGenesisHash":
            return self.network
        if method == "getTransaction":
            return self.transactions.get(params[0])
        if method == "getSignatureStatuses":
            return {"value": [self.status]}
        raise AssertionError("Unexpected RPC: " + method)

    def payment(self, uid=101, asset=USDC, raw=1_000_000):
        invoice = self.ledger.invoice(uid, asset, raw)
        signature = str(Signature.new_unique())
        wallet = self.service.wallet
        tx = {"blockTime": invoice["created_at"], "transaction": {"message": {
            "accountKeys": [{"pubkey": str(Keypair().pubkey()), "signer": True},
                            {"pubkey": wallet, "signer": False}],
            "instructions": [{"programId": str(legacy.MEMO), "parsed": "ZL1:" + invoice["id"]}]}},
            "meta": {"err": None, "fee": 5000, "preBalances": [100_000_000, 50_000_000],
                "postBalances": [100_000_000 - 5000 - (raw if asset == SOL else 0),
                                 50_000_000 + (raw if asset == SOL else 0)],
                "preTokenBalances": [], "postTokenBalances": []}}
        if asset == USDC:
            tx["meta"]["postTokenBalances"] = [{"owner": wallet, "mint": USDC, "accountIndex": 1,
                                                 "uiTokenAmount": {"amount": str(raw)}}]
        self.transactions[signature] = tx
        return invoice, signature, tx

    async def test_finalized_memo_credits_real_owner_only(self):
        invoice, sig, tx = self.payment(uid=202)
        await self.service.apply_deposit(None, sig)
        self.assertEqual(self.ledger.balance(202, USDC), 1_000_000)
        self.assertEqual(self.ledger.balance(101, USDC), 0)

    async def test_cannot_claim_other_invoice_signature(self):
        invoice, sig, tx = self.payment(uid=202)
        req = Request(101, {"invoice_id": invoice["id"], "tx_signature": sig})
        response = await self.service.handle(req, self.service.deposit)
        self.assertEqual(response.status, 403)
        self.assertEqual(self.ledger.balance(101, USDC), 0)

    async def test_missing_memo_not_credited(self):
        invoice, sig, tx = self.payment()
        tx["transaction"]["message"]["instructions"] = []
        with self.assertRaises(legacy.WalletError):
            await self.service.apply_deposit(None, sig)
        self.assertEqual(self.ledger.balance(101, USDC), 0)

    async def test_mismatched_memo_not_credited(self):
        invoice, sig, tx = self.payment(uid=202)
        own = self.ledger.invoice(101, USDC, 1_000_000)
        with self.assertRaises(legacy.WalletError):
            await self.service.apply_deposit(None, sig, own["id"])

    async def test_custodian_signed_transaction_not_deposit(self):
        invoice, sig, tx = self.payment()
        tx["transaction"]["message"]["accountKeys"][1]["signer"] = True
        with self.assertRaises(legacy.WalletError):
            await self.service.apply_deposit(None, sig)

    async def test_rent_not_native_sol_deposit(self):
        invoice, sig, tx = self.payment(asset=SOL, raw=2_039_280)
        tx["meta"]["postBalances"][1] = 50_000_000
        tx["transaction"]["message"]["accountKeys"].append({"pubkey": "ata", "signer": False})
        tx["meta"]["preBalances"].append(0)
        tx["meta"]["postBalances"].append(2_039_280)
        tx["meta"]["postTokenBalances"] = [{"owner": self.service.wallet, "mint": USDC,
            "accountIndex": 2, "uiTokenAmount": {"amount": "0"}}]
        with self.assertRaises(legacy.WalletError):
            await self.service.apply_deposit(None, sig)

    async def test_non_mainnet_never_credits(self):
        invoice, sig, tx = self.payment()
        self.network = "devnet"
        with self.assertRaises(legacy.WalletError):
            await self.service.apply_deposit(None, sig)
        self.assertEqual(self.ledger.balance(101, USDC), 0)

    async def test_stats_never_exposes_common_wallet_balance(self):
        invoice, sig, tx = self.payment(uid=202, raw=7_000_000)
        await self.service.apply_deposit(None, sig)
        req = Request(101)
        response = await self.service.handle(req, self.service.stats)
        data = json.loads(response.text)
        self.assertEqual(data["user_balance"], 0)
        self.assertEqual(data["wallet_history"], [])
        self.assertEqual(data["trades"], [])
        self.assertNotIn(sig, response.text)

    async def test_user_logs_do_not_return_global_logs(self):
        self.service.recent_logs = lambda: ["secret-other-user-withdrawal"]
        response = await self.service.handle(Request(202), self.service.logs)
        self.assertNotIn("secret-other-user", response.text)

    async def test_public_default_is_closed_owner_beta_allowed(self):
        self.service.public_enabled = False
        self.service.admit(101)
        with self.assertRaises(legacy.WalletError):
            self.service.admit(202)

    async def test_beta_allowlist_enables_second_account_not_admin_access(self):
        self.service.public_enabled = False
        self.service.beta_users.add(202)
        self.service.admit(202)
        response = await self.service.handle(Request(202), self.service.admin_status)
        self.assertEqual(response.status, 403)

    async def test_toggle_and_close_only_current_user(self):
        self.service.trading_preflight = AsyncMock()
        invoice, sig, tx = self.payment(uid=202, raw=7_000_000)
        await self.service.apply_deposit(None, sig)
        await self.service.handle(Request(202, {"trade_amount_usdc": "1"}), self.service.toggle)
        self.assertTrue(self.ledger.settings(202)["trading"])
        self.assertFalse(self.ledger.settings(101)["trading"])
        await self.service.handle(Request(101, {"confirm": True}), self.service.close_all)
        self.assertTrue(self.ledger.settings(101)["close_requested"])
        self.assertFalse(self.ledger.settings(202)["close_requested"])

    async def test_cannot_enable_trading_using_treasury_balance(self):
        response = await self.service.handle(Request(202, {"trade_amount_usdc": "1"}), self.service.toggle)
        self.assertEqual(response.status, 409)

    async def test_resume_owned_positions_without_free_cash_does_not_borrow(self):
        self.service.trading_preflight = AsyncMock()
        invoice, sig, tx = self.payment(uid=202, raw=1_000_000)
        await self.service.apply_deposit(None, sig)
        buy = self.ledger.reserve(202, str(uuid.uuid4()), "BUY", USDC, 1_000_000,
            {"output_asset": SOL, "symbol": "SOL", "decimals": 9})
        self.ledger.prepared(buy["id"], "buy-signature", "offline-wire")
        self.ledger.finalize(buy["id"], 1_000_000, 5000, 10_000_000)
        self.assertEqual(self.ledger.balance(202, USDC), 0)
        response = await self.service.handle(Request(202, {"trade_amount_usdc": "1"}), self.service.toggle)
        self.assertEqual(response.status, 200)
        self.assertEqual(self.ledger.balance(202, USDC), 0)
        self.assertEqual(self.ledger.balance(101, USDC), 0)

    async def test_withdrawal_rejects_other_funds_before_broadcast(self):
        self.service.send_reserved = AsyncMock()
        body = {"token": "USDC", "amount": "1", "recipient_address": str(self.recipient),
                "pin": "123456", "request_id": str(uuid.uuid4()), "telegram_id": 101}
        response = await self.service.handle(Request(202, body), self.service.withdraw)
        self.assertEqual(response.status, 409)
        self.service.send_reserved.assert_not_called()
        self.assertFalse(self.ledger.pending())

    async def test_invalid_pin_never_reserves(self):
        body = {"token": "USDC", "amount": "1", "recipient_address": str(self.recipient),
                "pin": "bad", "request_id": str(uuid.uuid4())}
        response = await self.service.handle(Request(101, body), self.service.withdraw)
        self.assertEqual(response.status, 401)
        self.assertFalse(self.ledger.pending())

    async def test_user_cannot_allocate_fees_or_import_opening(self):
        for action in (self.service.reserve_fees, self.service.opening_balance, self.service.admin_status):
            self.assertEqual((await self.service.handle(Request(202), action)).status, 403)

    async def test_insolvent_wallet_fails_closed(self):
        invoice, sig, tx = self.payment(raw=1_000_000)
        await self.service.apply_deposit(None, sig)
        self.service.balances.return_value[USDC]["raw"] = 999999
        with self.assertRaises(legacy.WalletError):
            await self.service.solvent(None)

    async def test_legacy_pending_blocks_until_finalized_failure(self):
        self.service.legacy_runtime.reserve("old-operation", "old-signature", "WITHDRAW", {})
        self.status = {"confirmationStatus": "confirmed", "err": "failed"}
        await self.service.reconcile(None)
        with self.assertRaises(legacy.WalletError):
            self.service.ready()
        self.status = {"confirmationStatus": "finalized", "err": "failed"}
        await self.service.reconcile(None)
        self.assertEqual(self.service.legacy_runtime.operation("old-operation")["state"], "FAILED")
        self.assertEqual(self.service.ready(), self.signer)

    async def test_financial_handler_reconciles_finalized_old_result_before_guard(self):
        self.service.legacy_runtime.reserve("old-operation", "old-signature", "WITHDRAW", {})
        self.status = {"confirmationStatus": "finalized", "err": "failed"}
        response = await self.service.handle(Request(101, {"token": "USDC", "amount": "1"}), self.service.create_invoice)
        self.assertEqual(response.status, 410)
        self.assertEqual(self.service.legacy_runtime.operation("old-operation")["state"], "FAILED")
        self.assertFalse(self.service.readiness()["legacy_operation_pending"])

    async def test_unknown_legacy_result_still_blocks_invoice_and_keeps_record(self):
        self.service.legacy_runtime.reserve("old-operation", "old-signature", "WITHDRAW", {})
        response = await self.service.handle(Request(101, {"token": "USDC", "amount": "1"}), self.service.create_invoice)
        self.assertEqual(response.status, 409)
        self.assertEqual(self.service.legacy_runtime.operation("old-operation")["state"], "PREPARED")
        self.assertFalse(any(method == "sendTransaction" for method, _ in self.calls))

    async def test_owner_diagnostics_show_old_signature_without_spending(self):
        self.service.legacy_runtime.reserve("old-operation", "old-signature", "WITHDRAW", {})
        response = await self.service.admin_status(Request(101), None)
        result = json.loads(response.text)
        self.assertEqual(result["legacy_operations"][0]["signature"], "old-signature")
        self.assertEqual(self.service.legacy_runtime.operation("old-operation")["state"], "PREPARED")
        self.assertFalse(any(method == "sendTransaction" for method, _ in self.calls))
        with self.assertRaises(legacy.WalletError) as error:
            await self.service.admin_status(Request(202), None)
        self.assertEqual(error.exception.status, 403)

    async def test_owner_can_read_global_pending_operation_without_private_fields_or_rpc(self):
        invoice = self.ledger.invoice(202, USDC, 2_000_000)
        self.ledger.credit_deposit(invoice["id"], str(Signature.new_unique()),
                                   USDC, 2_000_000, invoice["created_at"])
        operation = self.ledger.reserve(202, "pending-diagnostic", "BUY", USDC, 1_000_000,
                                       {"output_asset": runtime.WRAPPED_SOL}, str(self.recipient))
        signature = str(Signature.new_unique())
        self.ledger.prepared(operation["id"], signature, "signed-wire")

        response = await self.service.admin_pending_operations(Request(101), None)
        result = json.loads(response.text)
        self.assertEqual(response.status, 200)
        self.assertEqual(len(result["operations"]), 1)
        row = result["operations"][0]
        self.assertEqual(row["kind"], "BUY")
        self.assertEqual(row["state"], "PREPARED")
        self.assertEqual(row["signature"], signature)
        self.assertEqual(set(row), {"kind", "state", "created_at", "signature"})
        self.assertEqual(self.ledger.operation(operation["id"])["state"], "PREPARED")
        self.assertEqual(self.calls, [])

        with self.assertRaises(legacy.WalletError) as error:
            await self.service.admin_pending_operations(Request(202), None)
        self.assertEqual(error.exception.status, 403)

    async def test_owner_diagnostics_survive_rpc_failure_keep_unknown(self):
        self.service.legacy_runtime.reserve("old-operation", "old-signature", "WITHDRAW", {})
        self.service.rpc = AsyncMock(side_effect=legacy.WalletError("Solana RPC недоступен."))
        result = json.loads((await self.service.admin_status(Request(101), None)).text)
        self.assertTrue(result["success"])
        self.assertIn("RPC", result["legacy_check_error"])
        self.assertIn("RPC", result["balance_check_error"])
        self.assertEqual(result["legacy_operations"][0]["state"], "PREPARED")

    async def test_legacy_retirement_requires_owner_pin_and_preserves_money(self):
        self.service.public_enabled = False
        self.service.legacy_runtime.reserve("old-operation", "old-signature", "BUY", {})
        before = self.ledger.liabilities()
        body = {"signature": "old-signature", "pin": "123456", "confirm_archive_unknown": True}
        with self.assertRaises(legacy.WalletError):
            await self.service.retire_legacy(Request(202, body), None)
        with self.assertRaises(legacy.WalletError):
            await self.service.retire_legacy(Request(101, {**body, "pin": "wrong"}), None)
        self.assertEqual(self.service.legacy_runtime.operation("old-operation")["state"], "PREPARED")
        with patch.object(runtime, "prove_retirement", AsyncMock(return_value={"outcome": "UNKNOWN", "refund": False})):
            reply = await self.service.retire_legacy(Request(101, body), None)
        self.assertEqual(reply.status, 200)
        self.assertEqual(self.service.legacy_runtime.operation("old-operation")["state"], "EXPIRED_UNCONFIRMED")
        self.assertEqual(self.ledger.liabilities(), before)
        with self.ledger.db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM legacy_retirement_audit").fetchone()[0], 1)
        self.assertEqual(self.service.ready(), self.signer)

    async def test_failed_expiration_proof_keeps_old_pending_and_funds(self):
        self.service.public_enabled = False
        self.service.legacy_runtime.reserve("old-operation", "old-signature", "BUY", {})
        before = self.ledger.liabilities()
        with patch.object(runtime, "prove_retirement", AsyncMock(side_effect=legacy.WalletError("Proof missing"))):
            with self.assertRaises(legacy.WalletError):
                await self.service.retire_legacy(Request(101, {"signature": "old-signature",
                    "pin": "123456", "confirm_archive_unknown": True}), None)
        self.assertEqual(self.service.legacy_runtime.operation("old-operation")["state"], "PREPARED")
        self.assertEqual(self.ledger.liabilities(), before)

    async def test_scan_paginates_across_cycles_without_losing_old_payments(self):
        self.ledger.invoice(101, SOL, 1_000_000)
        now = int(time.time())
        records = [{"signature": "history-" + str(i), "blockTime": now, "err": "failed"} for i in range(650)]
        source = self.service.wallet
        requests = []
        async def rpc(session, method, params=None):
            if method != "getSignaturesForAddress":
                raise AssertionError(method)
            address, options = params
            requests.append((address, options.get("before")))
            if address != source:
                return []
            start = 0 if not options.get("before") else next(i for i, r in enumerate(records) if r["signature"] == options["before"]) + 1
            return records[start:start + 100]
        self.service.rpc = rpc
        await self.service.scan_deposits(None)
        with self.ledger.db() as db:
            checkpoint = db.execute("SELECT * FROM custody_scan_progress WHERE address=?", (source,)).fetchone()
            self.assertEqual(checkpoint["before_signature"], "history-499")
        await self.service.scan_deposits(None)
        self.assertIn((source, "history-499"), requests)
        with self.ledger.db() as db:
            self.assertIsNone(db.execute("SELECT * FROM custody_scan_progress WHERE address=?", (source,)).fetchone())
            self.assertEqual(db.execute("SELECT signature FROM custody_scan_cursor WHERE address=?", (source,)).fetchone()[0], "history-0")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM custody_seen_signatures").fetchone()[0], 650)

    async def test_http_signed_user_overrides_query_and_body(self):
        import main
        token = "offline-token"
        app = web.Application(middlewares=[main.telegram_auth_middleware])
        async def handler(request):
            return await self.service.handle(request, self.service.stats)
        app.router.add_get("/api/stats", handler)
        app.router.add_get("/api/admin/status", handler)
        def header(uid):
            fields = {"auth_date": str(int(time.time())), "user": json.dumps({"id": uid})}
            canonical = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
            secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
            fields["hash"] = hmac.new(secret, canonical.encode(), hashlib.sha256).hexdigest()
            return {"X-Telegram-Init-Data": urlencode(fields)}
        invoice, sig, tx = self.payment(uid=202, raw=7_000_000)
        await self.service.apply_deposit(None, sig)
        async with TestClient(TestServer(app)) as client:
            with patch.object(main, "TELEGRAM_TOKEN", token), patch.object(main, "ADMIN_TELEGRAM_ID", "101"):
                self.assertEqual((await client.get("/api/stats?telegram_id=101")).status, 401)
                response = await client.get("/api/stats?telegram_id=101", headers=header(202))
                self.assertEqual(response.status, 200)
                self.assertEqual((await response.json())["user_balance"], 7)
                self.assertEqual((await client.get("/api/admin/status", headers=header(202))).status, 403)
                forged = header(202)
                forged["X-Telegram-Init-Data"] += "0"
                self.assertEqual((await client.get("/api/stats", headers=forged)).status, 401)

    async def test_pin_cannot_be_reset_without_old_pin_or_to_bypass_lockout(self):
        import main
        with patch.object(main, "DB_FILE", self.tmp.name + "/pin.db"):
            main.init_db()
            first = await main.api_set_security(Request(101, {"pin": "123456"}))
            self.assertEqual(first.status, 200)
            no_old = await main.api_set_security(Request(101, {"pin": "654321"}))
            self.assertEqual(no_old.status, 409)
            self.assertTrue(main.verify_user_pin(101, "123456")[0])
            self.assertEqual((await main.api_set_security(Request(101,
                {"pin": "654321", "current_pin": "123456"}))).status, 200)
            for i in range(5):
                response = await main.api_set_security(Request(101,
                    {"pin": "999999", "current_pin": "wrong"}))
            self.assertEqual(response.status, 423)
            self.assertEqual((await main.api_set_security(Request(101,
                {"pin": "123456", "current_pin": "654321"}))).status, 423)

    async def test_payment_status_does_not_disclose_identity_or_balances(self):
        invoice, sig, tx = self.payment(uid=202)
        request = Request()
        request.match_info["invoice"] = invoice["id"]
        response = await self.service.payment_status(request, None)
        data = json.loads(response.text)
        self.assertNotIn("uid", data)
        self.assertNotIn("username", data)
        self.assertNotIn("balance", data)

    async def test_unsigned_sol_transfer_preserves_amount_recipient_without_memo(self):
        invoice = self.ledger.invoice(101, SOL, 1_000_000)
        sender = Keypair()
        req = Request(body={"sender": str(sender.pubkey())})
        req.match_info["invoice"] = invoice["id"]
        async def rpc(session, method, params=None):
            if method == "getGenesisHash":
                return legacy.GENESIS
            if method == "getLatestBlockhash":
                return {"value": {"blockhash": str(Hash.new_unique())}}
            raise AssertionError(method)
        self.service.rpc = rpc
        response = await self.service.deposit_transaction(req, None)
        tx = Transaction.from_bytes(base64.b64decode(json.loads(response.text)["transaction"]))
        self.assertEqual(str(tx.message.account_keys[0]), str(sender.pubkey()))
        self.assertNotIn(legacy.MEMO, tx.message.account_keys)
        self.assertEqual(len(tx.message.instructions), 1)
        self.assertEqual(int.from_bytes(bytes(tx.message.instructions[0].data)[4:], "little"), 1_000_000)
        self.assertTrue(all(s == Signature.default() for s in tx.signatures))

    async def test_unsigned_usdc_deposit_uses_transfer_checked_only_customer_signs(self):
        invoice = self.ledger.invoice(101, USDC, 1_000_000)
        sender = Keypair()
        request = Request(body={"sender": str(sender.pubkey())})
        request.match_info["invoice"] = invoice["id"]
        def account(owner, raw):
            return {"owner": str(legacy.TOKEN), "data": {"parsed": {"info": {
                "owner": owner, "mint": USDC, "state": "initialized",
                "tokenAmount": {"amount": str(raw), "decimals": 6}}}}}
        async def rpc(session, method, params=None):
            if method == "getGenesisHash":
                return legacy.GENESIS
            if method == "getLatestBlockhash":
                return {"value": {"blockhash": str(Hash.new_unique())}}
            if method == "getMultipleAccounts":
                return {"value": [account(str(sender.pubkey()), 2_000_000), account(self.service.wallet, 0)]}
            raise AssertionError(method)
        self.service.rpc = rpc
        response = await self.service.deposit_transaction(request, None)
        transaction = Transaction.from_bytes(base64.b64decode(json.loads(response.text)["transaction"]))
        self.assertEqual(transaction.message.header.num_required_signatures, 1)
        self.assertEqual(bytes(transaction.message.instructions[0].data),
                         bytes([12]) + (1_000_000).to_bytes(8, "little") + bytes([6]))
        self.assertNotIn(legacy.MEMO, transaction.message.account_keys)

    async def run_withdrawal(self, timeout=False, failed=False):
        invoice, sig, tx = self.payment(uid=101, raw=2_000_000)
        await self.service.apply_deposit(None, sig)
        invoice, sig, tx = self.payment(uid=202, raw=7_000_000)
        await self.service.apply_deposit(None, sig)
        sends = []
        genesis_rpc = self.rpc
        finalized = not timeout
        async def rpc(session, method, params=None):
            if method == "getMultipleAccounts":
                def account(owner):
                    return {"owner": str(legacy.TOKEN), "data": {"parsed": {"info": {
                        "owner": str(owner), "mint": USDC, "state": "initialized",
                        "tokenAmount": {"amount": "100000000", "decimals": 6}}}}}
                return {"value": [account(self.signer.pubkey()), account(self.recipient)]}
            if method == "getLatestBlockhash":
                return {"value": {"blockhash": str(Hash.new_unique())}}
            if method == "getFeeForMessage":
                return {"value": 5000}
            if method == "sendTransaction":
                row = self.ledger.pending()[0]
                self.assertEqual(row["state"], "PREPARED")
                self.assertEqual(self.ledger.balance(101, USDC), 1_000_000)
                sends.append(row["signature"])
                if timeout:
                    raise legacy.WalletError("Simulated network timeout")
                return row["signature"]
            if method == "getSignatureStatuses":
                if not finalized:
                    return {"value": [None]}
                return {"value": [{"confirmationStatus": "finalized", "err": "failed" if failed else None}]}
            if method == "getTransaction" and params[0] in sends:
                return {"transaction": {"message": {"accountKeys": [
                    {"pubkey": self.service.wallet, "signer": True}]}}, "meta": {
                    "err": "failed" if failed else None, "fee": 5000,
                    "preBalances": [50_000_000], "postBalances": [49_995_000],
                    "preTokenBalances": [{"owner": self.service.wallet, "mint": USDC,
                                           "uiTokenAmount": {"amount": "100000000"}}],
                    "postTokenBalances": [{"owner": self.service.wallet, "mint": USDC,
                                            "uiTokenAmount": {"amount": "100000000" if failed else "99000000"}}]}}
            return await genesis_rpc(session, method, params)
        self.service.rpc = rpc
        async def simulate(session, transaction, row, info):
            return base64.b64encode(bytes(transaction)).decode()
        self.service.safe_simulation = simulate
        request = Request(101, {"token": "USDC", "amount": "1", "recipient_address": str(self.recipient),
            "pin": "123456", "request_id": str(uuid.uuid4())})
        response = await self.service.handle(request, self.service.withdraw)
        if timeout:
            self.assertEqual(response.status, 202)
            self.assertEqual(self.ledger.pending()[0]["state"], "UNKNOWN")
            self.assertEqual(self.ledger.balance(101, USDC), 1_000_000)
            finalized = True
            await self.service.reconcile(None)
        else:
            self.assertEqual(response.status, 409 if failed else 200)
        repeat = await self.service.handle(request, self.service.withdraw)
        self.assertEqual(repeat.status, 409 if failed else 200)
        self.assertEqual(len(sends), 1)
        self.assertEqual(self.ledger.balance(101, USDC), 2_000_000 if failed else 1_000_000)
        self.assertEqual(self.ledger.balance(202, USDC), 7_000_000)
        self.assertTrue(self.ledger.audit())

    async def test_complete_withdrawal_pipeline_is_owner_scoped_and_idempotent(self):
        await self.run_withdrawal()

    async def test_timeout_then_finalization_never_resends_or_refunds(self):
        await self.run_withdrawal(timeout=True)

    async def test_failed_mainnet_outcome_returns_owner_reserve_charges_operator(self):
        await self.run_withdrawal(failed=True)

    def test_economic_sol_excludes_returned_rent_and_old_wrapped(self):
        wallet = self.service.wallet
        tx = {"transaction": {"message": {"accountKeys": [{"pubkey": wallet}, {"pubkey": "owned-ata"}]}},
            "meta": {"fee": 5000, "preBalances": [50_000_000, 3_039_280], "postBalances": [63_034_280, 0],
                     "preTokenBalances": [{"accountIndex": 1, "owner": wallet, "mint": runtime.WRAPPED_SOL,
                                           "uiTokenAmount": {"amount": "1000000"}}], "postTokenBalances": []}}
        self.assertEqual(self.service.effects(tx, wallet, SOL), 10_000_000)
        native_delta = 63_034_280 - 50_000_000
        operator_fee = 10_000_000 - native_delta - self.service.wrapped_delta(tx, wallet)
        self.assertEqual(operator_fee, 5000 - 2_039_280)

    def test_exact_decimal_invoice_display(self):
        self.assertEqual(runtime.display_raw(9223372036854775807, 9), "9223372036.854775807")
        self.assertEqual(runtime.display_raw(100_000_000, 6), "100")


class SimulationTests(unittest.IsolatedAsyncioTestCase):
    setUp = RuntimeTests.setUp
    rpc = RuntimeTests.rpc
    def token_account(self, mint, amount, lamports=2_039_280):
        data = bytearray(165)
        data[:32] = bytes(legacy.Pubkey.from_string(mint))
        data[32:64] = bytes(self.signer.pubkey())
        data[64:72] = amount.to_bytes(8, "little")
        data[108] = 1
        return {"owner": str(legacy.TOKEN), "lamports": lamports,
                "data": [base64.b64encode(data).decode(), "base64"], "executable": False}

    async def simulate(self, before, after, asset=USDC, raw=1_000_000, kind="WITHDRAW", info=None, versioned=False):
        message = Message.new_with_blockhash([], self.signer.pubkey(), Hash.new_unique())
        tx = Transaction.new_unsigned(message)
        if versioned:
            message = MessageV0.try_compile(self.signer.pubkey(), [], [], Hash.new_unique())
            tx = VersionedTransaction(message, [self.signer])
        fee_message = []
        async def rpc(session, method, params=None):
            if method == "getTokenAccountsByOwner":
                return {"value": [{"pubkey": str(legacy.associated(self.signer.pubkey(),
                    legacy.Pubkey.from_bytes(base64.b64decode(item["data"][0])[:32]))), "account": item}
                    for item in before[1:] if item]}
            if method == "getMultipleAccounts":
                return {"value": before}
            if method == "getFeeForMessage":
                fee_message.append(base64.b64decode(params[0]))
                return {"value": 5000}
            if method == "simulateTransaction":
                return {"value": {"err": None, "accounts": after}}
            raise AssertionError(method)
        self.service.rpc = rpc
        row = {"asset": asset, "raw": raw, "fee_cap": FEE_CAP, "kind": kind}
        result = await self.service.safe_simulation(None, tx, row, info or {})
        if versioned:
            self.assertEqual(fee_message[0][0], 0x80)
        return result

    def native(self, amount):
        return {"owner": str(legacy.SYSTEM), "lamports": amount, "data": ["", "base64"], "executable": False}

    async def test_simulation_accepts_exact_usdc_withdrawal(self):
        before = [self.native(50_000_000), self.token_account(USDC, 10_000_000)]
        after = [self.native(49_995_000), self.token_account(USDC, 9_000_000)]
        self.assertTrue(await self.simulate(before, after))

    async def test_simulation_blocks_extra_usdc_spend(self):
        before = [self.native(50_000_000), self.token_account(USDC, 10_000_000)]
        after = [self.native(49_995_000), self.token_account(USDC, 8_999_999)]
        with self.assertRaises(legacy.WalletError):
            await self.simulate(before, after)

    async def test_simulation_blocks_wallet_owner_change(self):
        before = [self.native(50_000_000), self.token_account(USDC, 10_000_000)]
        after = [self.native(49_995_000), self.token_account(USDC, 9_000_000)]
        after[0]["owner"] = str(legacy.TOKEN)
        with self.assertRaises(legacy.WalletError):
            await self.simulate(before, after)

    async def test_simulation_blocks_nonce_conversion_without_owner_change(self):
        before = [self.native(50_000_000), self.token_account(USDC, 10_000_000)]
        after = [self.native(49_995_000), self.token_account(USDC, 9_000_000)]
        after[0]["data"] = [base64.b64encode(b"nonce authority").decode(), "base64"]
        with self.assertRaises(legacy.WalletError):
            await self.simulate(before, after)

    async def test_simulation_blocks_new_delegate(self):
        before = [self.native(50_000_000), self.token_account(USDC, 10_000_000)]
        after = [self.native(49_995_000), self.token_account(USDC, 9_000_000)]
        data = bytearray(base64.b64decode(after[1]["data"][0]))
        data[72] = 1
        after[1]["data"][0] = base64.b64encode(data).decode()
        with self.assertRaises(legacy.WalletError):
            await self.simulate(before, after)

    async def test_simulation_blocks_operator_fee_overflow(self):
        before = [self.native(50_000_000), self.token_account(USDC, 10_000_000)]
        after = [self.native(44_999_999), self.token_account(USDC, 9_000_000)]
        with self.assertRaises(legacy.WalletError):
            await self.simulate(before, after)

    async def test_simulation_blocks_unrelated_asset_spend(self):
        other = str(Keypair().pubkey())
        before = [self.native(50_000_000), self.token_account(USDC, 10_000_000), self.token_account(other, 100)]
        after = [self.native(49_995_000), self.token_account(USDC, 9_000_000), self.token_account(other, 99)]
        with self.assertRaises(legacy.WalletError):
            await self.simulate(before, after)

    async def test_simulation_accepts_sol_buy_and_versioned_fee_message(self):
        before = [self.native(50_000_000), self.token_account(USDC, 10_000_000)]
        after = [self.native(59_995_000), self.token_account(USDC, 9_000_000)]
        self.assertTrue(await self.simulate(before, after, kind="BUY",
            info={"output_asset": SOL, "minimum_out": 9_000_000}, versioned=True))

    async def test_native_sniper_buy_with_operator_paid_new_ata_rent(self):
        mint = str(Keypair().pubkey())
        before = [self.native(50_000_000), self.token_account(USDC, 10_000_000), None]
        after = [self.native(42_955_720), self.token_account(USDC, 10_000_000), self.token_account(mint, 2_600_000)]
        self.assertTrue(await self.simulate(before, after, asset=SOL, raw=5_000_000, kind="BUY",
            info={"output_asset": mint, "minimum_out": 2_500_000}, versioned=True))

    async def test_native_sniper_buy_cannot_spend_usdc(self):
        mint = str(Keypair().pubkey())
        before = [self.native(50_000_000), self.token_account(USDC, 10_000_000), self.token_account(mint, 0)]
        after = [self.native(44_995_000), self.token_account(USDC, 9_999_999), self.token_account(mint, 2_600_000)]
        with self.assertRaises(legacy.WalletError):
            await self.simulate(before, after, asset=SOL, raw=5_000_000, kind="BUY",
                info={"output_asset": mint, "minimum_out": 2_500_000})

    async def test_native_sniper_sell_returns_sol(self):
        mint = str(Keypair().pubkey())
        before = [self.native(50_000_000), self.token_account(USDC, 10_000_000), self.token_account(mint, 2_600_000)]
        after = [self.native(55_995_000), self.token_account(USDC, 10_000_000), self.token_account(mint, 0)]
        self.assertTrue(await self.simulate(before, after, asset=mint, raw=2_600_000, kind="SELL",
            info={"output_asset": SOL, "minimum_out": 5_900_000}, versioned=True))


if __name__ == "__main__":
    unittest.main()
