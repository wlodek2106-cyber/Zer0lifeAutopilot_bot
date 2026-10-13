import asyncio
import base64
import json
import os
import sqlite3
import tempfile
import unittest
import uuid
import hashlib
import hmac
import time
from urllib.parse import urlencode
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from unittest.mock import AsyncMock, patch

from solders.hash import Hash
from solders.keypair import Keypair
from solders.transaction import Transaction
import wallet_runtime as wallet


class Request(dict):
    def __init__(self, body=None, owner=428821665):
        super().__init__(telegram_user={"id": owner, "username": "owner"})
        self.body = body or {}

    async def json(self):
        return self.body


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.signer, self.recipient = Keypair(), Keypair().pubkey()
        self.path = self.temp.name + "/wallet.db"
        self.service = self.service_factory()
        self.mock_storage = patch.object(wallet, "durable", return_value=True)
        self.mock_storage.start()
        self.addCleanup(self.mock_storage.stop)
        self.balance = {wallet.USDC: {"raw": 7_000_000, "decimals": 6},
                        wallet.SOL: {"raw": 40_000_000, "decimals": 9}}
        self.service.balances = AsyncMock(side_effect=lambda session: self.balance)
        self.service.rpc = self.rpc
        self.calls = []
        self.send_count = 0
        self.timeout = False
        self.status = None
        self.transaction = None
        self.sim_error = None
        self.missing_ata = False
        self.network = wallet.GENESIS

    def service_factory(self):
        return wallet.WalletRuntime(self.path, "https://invalid.example", 428821665,
            str(self.signer.pubkey()), lambda: self.signer,
            lambda user, pin: (pin == "123456", "invalid"), AsyncMock(return_value={}),
            {"SOL": {"mint": wallet.SOL, "decimals": 9}}, lambda message: None)

    def request(self, token="USDC", amount="1", **kwargs):
        return Request({"token": token, "amount": amount, "recipient_address": str(self.recipient),
                        "pin": "123456", "request_id": str(uuid.uuid4()), **kwargs})

    def account(self, owner):
        return {"owner": str(wallet.TOKEN), "data": {"parsed": {"info": {
            "owner": str(owner), "mint": wallet.USDC, "state": "initialized",
            "tokenAmount": {"amount": "7000000", "decimals": 6}}}}}

    async def rpc(self, session, method, params=None):
        self.calls.append((method, params))
        if method == "getGenesisHash":
            return self.network
        if method == "getMultipleAccounts":
            return {"value": [self.account(self.signer.pubkey()),
                              None if self.missing_ata else self.account(self.recipient)]}
        if method == "getMinimumBalanceForRentExemption":
            return 2_039_280
        if method == "getLatestBlockhash":
            return {"value": {"blockhash": str(Hash.default())}}
        if method == "getFeeForMessage":
            return {"value": 5000}
        if method == "simulateTransaction":
            tx = Transaction.from_bytes(base64.b64decode(params[0]))
            tx.verify()
            return {"value": {"err": self.sim_error}}
        if method == "sendTransaction":
            self.send_count += 1
            tx = Transaction.from_bytes(base64.b64decode(params[0]))
            signature = str(tx.signatures[0])
            self.assertTrue(any(r["signature"] == signature for r in self.service.pending()))
            self.assertEqual(params[1]["maxRetries"], 0)
            if self.timeout:
                raise asyncio.TimeoutError()
            return signature
        if method == "getSignatureStatuses":
            return {"value": [self.status]}
        if method == "getTransaction":
            return self.transaction
        self.fail(method)

    async def test_usdc_checked_transfer_and_chain_balance(self):
        response = await self.service.handle(self.request(), self.service.withdraw)
        self.assertEqual(response.status, 202)
        self.assertEqual(json.loads(response.text)["state"], "SUBMITTED")
        wire = next(p[0] for m, p in self.calls if m == "simulateTransaction")
        tx = Transaction.from_bytes(base64.b64decode(wire))
        self.assertEqual(bytes(tx.message.instructions[-1].data),
            bytes([12]) + (1_000_000).to_bytes(8, "little") + bytes([6]))
        with sqlite3.connect(self.path) as db:
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertNotIn("users", tables)

    async def test_sol_uses_native_lamports_not_usdc_ledger(self):
        response = await self.service.handle(self.request("SOL", ".001"), self.service.withdraw)
        self.assertEqual(response.status, 202)
        wire = next(p[0] for m, p in self.calls if m == "simulateTransaction")
        tx = Transaction.from_bytes(base64.b64decode(wire))
        ix = tx.message.instructions[-1]
        self.assertEqual(tx.message.account_keys[ix.program_id_index], wallet.SYSTEM)
        self.assertEqual(int.from_bytes(bytes(ix.data)[4:], "little"), 1_000_000)

    async def test_sol_cannot_spend_fee_reserve(self):
        response = await self.service.handle(self.request("SOL", ".04"), self.service.withdraw)
        self.assertEqual(response.status, 409)
        self.assertEqual(self.send_count, 0)

    async def test_usdc_cannot_exceed_actual_balance(self):
        response = await self.service.handle(self.request(amount="8"), self.service.withdraw)
        self.assertEqual(response.status, 409)
        self.assertEqual(self.send_count, 0)

    async def test_wrong_owner_and_pin(self):
        req = self.request()
        req["telegram_user"]["id"] = 1
        self.assertEqual((await self.service.handle(req, self.service.withdraw)).status, 403)
        self.assertEqual((await self.service.handle(self.request(pin="bad"), self.service.withdraw)).status, 401)
        self.assertEqual(self.send_count, 0)

    async def test_configuration_and_disk_guards(self):
        with patch.object(wallet, "durable", return_value=False):
            self.assertEqual((await self.service.handle(self.request(), self.service.withdraw)).status, 503)
        self.service.wallet = str(Keypair().pubkey())
        self.assertEqual((await self.service.handle(self.request(), self.service.withdraw)).status, 503)
        self.assertEqual(self.send_count, 0)

    async def test_invalid_requests(self):
        for args in ({"token": "BTC"}, {"amount": "NaN"}, {"amount": "Infinity"},
                     {"amount": ".0000001"}, {"amount": "-1"}, {"amount": "0"},
                     {"request_id": "bad"}, {"recipient_address": "bad"}):
            self.assertEqual((await self.service.handle(self.request(**args), self.service.withdraw)).status, 400)
        self.assertEqual(self.send_count, 0)

    async def test_preflight_and_network_rejection_do_not_reserve(self):
        self.network = "devnet"
        self.assertEqual((await self.service.handle(self.request(), self.service.withdraw)).status, 503)
        self.network = wallet.GENESIS
        self.sim_error = {"InstructionError": [0, "InvalidAccountData"]}
        self.assertEqual((await self.service.handle(self.request(), self.service.withdraw)).status, 409)
        self.assertEqual(self.service.pending(), [])

    async def test_missing_ata_is_created_idempotently(self):
        self.missing_ata = True
        response = await self.service.handle(self.request(), self.service.withdraw)
        self.assertEqual(response.status, 202)
        tx = Transaction.from_bytes(base64.b64decode(next(p[0] for m, p in self.calls if m == "simulateTransaction")))
        self.assertEqual(bytes(tx.message.instructions[-2].data), bytes([1]))
        self.assertEqual(len(tx.message.instructions), 3)

    async def test_timeout_restart_same_or_new_id_never_resends(self):
        self.timeout = True
        req = self.request()
        response = await self.service.handle(req, self.service.withdraw)
        self.assertEqual(json.loads(response.text)["state"], "UNKNOWN")
        restarted = self.service_factory()
        restarted.rpc = self.rpc
        self.assertEqual((await restarted.handle(req, restarted.withdraw)).status, 202)
        self.assertEqual((await restarted.handle(self.request(), restarted.withdraw)).status, 409)
        self.assertEqual(self.send_count, 1)

    async def test_failure_not_completion(self):
        self.status = {"err": {"InstructionError": [0, "error"]}, "confirmationStatus": "finalized"}
        response = await self.service.handle(self.request(), self.service.withdraw)
        self.assertEqual(json.loads(response.text)["state"], "FAILED")
        self.assertEqual(response.status, 409)

    async def test_finalized_and_not_confirmed_sets_success(self):
        req = self.request()
        self.status = {"err": None, "confirmationStatus": "confirmed"}
        response = await self.service.handle(req, self.service.withdraw)
        self.assertEqual(json.loads(response.text)["state"], "SUBMITTED")
        self.status["confirmationStatus"] = "finalized"
        self.transaction = {"meta": {"err": None}}
        await self.service.reconcile(None)
        response = await self.service.handle(req, self.service.withdraw)
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(response.text)["state"], "COMPLETED")
        self.assertEqual(self.send_count, 1)

    async def test_two_processes_reservation(self):
        self.service.reserve("one", "sig1", "WITHDRAW", {})
        other = self.service_factory()
        with self.assertRaises(wallet.WalletError):
            other.reserve("two", "sig2", "WITHDRAW", {})

    def incoming_tx(self, native=False):
        return {"transaction": {"message": {"accountKeys": [
            {"pubkey": str(self.recipient), "signer": True},
            {"pubkey": self.service.wallet, "signer": False}]}},
            "meta": {"err": None, "fee": 5000, "preBalances": [20_000_000, 0],
                "postBalances": [18_995_000, 1_000_000 if native else 0],
                "preTokenBalances": [], "postTokenBalances": [] if native else [{
                    "owner": self.service.wallet, "mint": wallet.USDC, "uiTokenAmount": {"amount": "1000000"}}]}}

    async def test_usdc_and_sol_deposits_no_fictional_credit_and_no_duplicate(self):
        for native in (False, True):
            self.transaction = self.incoming_tx(native)
            req = Request({"tx_signature": ("S" if native else "U") * 88})
            response = await self.service.handle(req, self.service.deposit)
            self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(response.text)["received"], {"SOL": .001} if native else {"USDC": 1})
            await self.service.handle(req, self.service.deposit)
        with self.service.db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM wallet_operations").fetchone()[0], 2)

    async def test_deposit_wrong_wallet_failed_or_self_transaction(self):
        req = Request({"tx_signature": "x" * 88})
        self.transaction = self.incoming_tx()
        self.transaction["meta"]["err"] = "failed"
        self.assertEqual((await self.service.handle(req, self.service.deposit)).status, 409)
        self.transaction = self.incoming_tx()
        self.transaction["transaction"]["message"]["accountKeys"][1]["signer"] = True
        self.assertEqual((await self.service.handle(req, self.service.deposit)).status, 409)
        self.transaction = self.incoming_tx()
        self.transaction["meta"]["postTokenBalances"][0]["owner"] = str(self.recipient)
        self.assertEqual((await self.service.handle(req, self.service.deposit)).status, 409)

    async def test_trade_size_cannot_exceed_funds_and_never_autostarts(self):
        self.assertFalse(self.service.trading)
        self.assertEqual((await self.service.handle(Request({"trade_amount_usdc": "20"}), self.service.toggle)).status, 409)
        response = await self.service.handle(Request({"trade_amount_usdc": "1"}), self.service.toggle)
        self.assertEqual(response.status, 200)
        self.assertTrue(self.service.trading)
        await self.service.handle(Request(), self.service.toggle)
        self.assertFalse(self.service.trading)
        self.assertEqual(self.send_count, 0)

    async def test_actual_fill_not_quote_creates_position_once(self):
        self.service.reserve("buy", "buy-signature", "BUY", {"symbol": "SOL", "mint": wallet.SOL, "decimals": 9})
        tx = {"transaction": {"message": {"accountKeys": [{"pubkey": self.service.wallet}]}},
              "meta": {"fee": 5000, "preBalances": [40_000_000], "postBalances": [49_995_000],
                       "preTokenBalances": [{"owner": self.service.wallet, "mint": wallet.USDC,
                            "uiTokenAmount": {"amount": "7000000"}}],
                       "postTokenBalances": [{"owner": self.service.wallet, "mint": wallet.USDC,
                            "uiTokenAmount": {"amount": "6000000"}}]}}
        row = self.service.operation("buy")
        self.service.apply_effects(row, tx)
        self.service.apply_effects(row, tx)
        with self.service.db() as db:
            positions = list(db.execute("SELECT * FROM wallet_positions"))
        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0]["raw_amount"], 10_000_000)
        self.assertEqual(positions[0]["cost_usdc"], 1)
        self.assertEqual(positions[0]["entry_price"], 100)

    async def test_stats_shows_chain_balances_without_legacy_users(self):
        response = await self.service.handle(Request(), self.service.stats)
        result = json.loads(response.text)
        self.assertEqual(result["user_balance"], 7)
        self.assertEqual(result["sol_balance"], .04)
        self.assertEqual(result["withdrawable"], 7)

    async def test_same_request_id_cannot_change_payment(self):
        req = self.request()
        await self.service.handle(req, self.service.withdraw)
        req.body["amount"] = "2"
        self.assertEqual((await self.service.handle(req, self.service.withdraw)).status, 409)
        self.assertEqual(self.send_count, 1)

    async def test_actual_sale_proceeds_close_once(self):
        with self.service.db() as db:
            db.execute("""INSERT INTO wallet_positions
                (id,symbol,mint,raw_amount,cost_usdc,entry_price,state,buy_tx)
                VALUES('position','SOL',?,10000000,1,100,'OPEN','buy')""", (wallet.SOL,))
        self.service.reserve("sell", "sell-signature", "SELL", {"position_id": "position", "decimals": 9})
        tx = {"meta": {
            "preTokenBalances": [{"owner": self.service.wallet, "mint": wallet.USDC, "uiTokenAmount": {"amount": "6000000"}}],
            "postTokenBalances": [{"owner": self.service.wallet, "mint": wallet.USDC, "uiTokenAmount": {"amount": "7100000"}}]}}
        row = self.service.operation("sell")
        self.service.apply_effects(row, tx)
        self.service.apply_effects(row, tx)
        with self.service.db() as db:
            position = db.execute("SELECT * FROM wallet_positions WHERE id='position'").fetchone()
        self.assertEqual(position["state"], "CLOSED")
        self.assertAlmostEqual(position["profit_usdc"], .1)
        self.assertEqual(position["exit_price"], 110)

    async def test_http_telegram_auth_not_client_id(self):
        import main
        app = web.Application(middlewares=[main.telegram_auth_middleware])
        async def handler(request):
            return await self.service.handle(request, self.service.stats)
        app.router.add_get("/api/stats", handler)
        token = "offline-test-token"
        def header(owner):
            fields = {"auth_date": str(int(time.time())), "user": json.dumps({"id": owner})}
            checked = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
            secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
            fields["hash"] = hmac.new(secret, checked.encode(), hashlib.sha256).hexdigest()
            return {"X-Telegram-Init-Data": urlencode(fields)}
        async with TestClient(TestServer(app)) as client:
            with patch.object(main, "TELEGRAM_TOKEN", token), patch.object(main, "ADMIN_TELEGRAM_ID", "428821665"):
                self.assertEqual((await client.get("/api/stats?telegram_id=428821665")).status, 401)
                self.assertEqual((await client.get("/api/stats?telegram_id=428821665", headers=header(1))).status, 403)
                response = await client.get("/api/stats?telegram_id=1", headers=header(428821665))
                self.assertEqual(response.status, 200)
                self.assertEqual((await response.json())["user_balance"], 7)

    def test_normal_routes_and_no_test_ui(self):
        import main
        import inspect
        self.assertIn("wallet_handler(runtime.withdraw)", inspect.getsource(main.main))
        self.assertIn("runtime.worker()", inspect.getsource(main.main))
        self.assertNotIn("asyncio.create_task(real_trading_worker())", inspect.getsource(main.main))
        self.assertNotIn("owner-test-panel", main.HTML_CONTENT)
        self.assertNotIn("/api/test_withdraw", inspect.getsource(main.main))
        self.assertIn('id="withdraw-amount"', main.HTML_CONTENT)


if __name__ == "__main__":
    unittest.main()
