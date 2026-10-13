import asyncio
import base64
import json
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
import hashlib
import hmac
import time
from urllib.parse import urlencode
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from solders.hash import Hash
from solders.keypair import Keypair
from solders.transaction import Transaction

import owner_test_withdrawal as test


class Request(dict):
    def __init__(self, owner=42, body=None):
        super().__init__(telegram_user={"id": owner})
        self.body = body or {}

    async def json(self):
        return self.body


def data(response):
    return json.loads(response.text)


def token_account(owner, amount=2_000_000):
    return {"owner": str(test.TOKEN), "data": {"parsed": {
        "type": "account", "info": {"mint": str(test.MINT), "owner": str(owner),
            "state": "initialized", "tokenAmount": {"amount": str(amount), "decimals": 6}}
    }}}


class OwnerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.signer, self.recipient = Keypair(), Keypair().pubkey()
        self.env = patch.dict(os.environ, {
            "OWNER_TEST_ENABLED": "true", "OWNER_TEST_RECIPIENT": str(self.recipient)})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.path = self.temp.name + "/test.db"
        self.service = self.make_service()
        self.rpc_calls = []
        self.chain_status = None
        self.send_error = False
        self.send_count = 0
        self.destination = token_account(self.recipient)
        self.source = token_account(self.signer.pubkey())
        self.genesis = test.MAINNET_GENESIS
        self.sol = 10_000_000
        self.simulation_error = None
        self.service.rpc = self.rpc

    def make_service(self):
        return test.OwnerTestWithdrawal(
            self.path, "https://invalid.example", "42", str(self.signer.pubkey()),
            lambda: self.signer, lambda owner, pin: (pin == "123456", "invalid"),
            lambda message: None, storage_check=lambda path: True)

    def request(self, **overrides):
        body = {"confirm": True, "recipient_address": str(self.recipient), "pin": "123456"}
        body.update(overrides)
        return Request(body=body)

    async def rpc(self, session, method, params=None):
        self.rpc_calls.append((method, params))
        if method == "getGenesisHash":
            return self.genesis
        if method == "getMultipleAccounts":
            return {"value": [self.source, self.destination]}
        if method == "getMinimumBalanceForRentExemption":
            return 2_039_280
        if method == "getLatestBlockhash":
            return {"value": {"blockhash": str(Hash.default()), "lastValidBlockHeight": 123}}
        if method == "getFeeForMessage":
            return {"value": 5000}
        if method == "getBalance":
            return {"value": self.sol}
        if method == "simulateTransaction":
            tx = Transaction.from_bytes(base64.b64decode(params[0]))
            tx.verify()
            return {"value": {"err": self.simulation_error}}
        if method == "sendTransaction":
            self.send_count += 1
            tx = Transaction.from_bytes(base64.b64decode(params[0]))
            signature = str(tx.signatures[0])
            # Verify persistence BEFORE any network broadcast.
            self.assertEqual(self.service.attempt()["signature"], signature)
            self.assertEqual(params[1]["maxRetries"], 0)
            self.assertFalse(params[1]["skipPreflight"])
            if self.send_error:
                raise asyncio.TimeoutError()
            return signature
        if method == "getSignatureStatuses":
            self.assertTrue(params[1]["searchTransactionHistory"])
            return {"value": [self.chain_status]}
        self.fail("Unexpected RPC method " + method)

    async def test_wrong_owner_cannot_even_read_status(self):
        for method in (self.service.send, self.service.status):
            response = await method(Request(owner=43))
            self.assertEqual(response.status, 403)
        self.assertEqual(self.rpc_calls, [])

    async def test_disabled_by_default(self):
        with patch.dict(os.environ, {"OWNER_TEST_ENABLED": ""}):
            service = self.make_service()
        response = await service.send(self.request())
        self.assertEqual(response.status, 503)
        self.assertEqual(self.rpc_calls, [])

    async def test_bad_config_and_ephemeral_storage(self):
        self.service.recipient = ""
        self.assertEqual((await self.service.send(self.request())).status, 503)
        self.service.recipient = str(self.recipient)
        self.service.storage_check = lambda path: False
        self.assertEqual((await self.service.send(self.request())).status, 503)
        self.assertEqual(self.rpc_calls, [])

    async def test_confirmation_recipient_amount_and_pin(self):
        for args in ({"confirm": False}, {"recipient_address": str(Keypair().pubkey())},
                     {"amount": "2"}, {"token": "SOL"}):
            self.assertEqual((await self.service.send(self.request(**args))).status, 400)
        self.assertEqual((await self.service.send(self.request(pin="wrong"))).status, 401)
        self.assertEqual(self.rpc_calls, [])
        self.assertIsNone(self.service.attempt())

    async def test_signer_mismatch(self):
        self.service.expected_wallet = str(Keypair().pubkey())
        self.assertEqual((await self.service.send(self.request())).status, 503)
        self.assertEqual(self.send_count, 0)

    async def test_wrong_network_balance_token_and_simulation(self):
        self.genesis = "devnet"
        self.assertEqual((await self.service.send(self.request())).status, 503)
        self.genesis = test.MAINNET_GENESIS
        self.source = token_account(self.signer.pubkey(), amount=999_999)
        self.assertEqual((await self.service.send(self.request())).status, 409)
        self.source = token_account(self.signer.pubkey())
        self.destination["data"]["parsed"]["info"]["mint"] = str(Keypair().pubkey())
        self.assertEqual((await self.service.send(self.request())).status, 409)
        self.destination = token_account(self.recipient)
        self.sol = 999
        self.assertEqual((await self.service.send(self.request())).status, 409)
        self.sol = 10_000_000
        self.simulation_error = {"InstructionError": [0, "InvalidAccountData"]}
        self.assertEqual((await self.service.send(self.request())).status, 409)
        self.assertEqual(self.send_count, 0)
        self.assertIsNone(self.service.attempt())

    async def test_submitted_is_not_completed_and_retry_cannot_send(self):
        response = await self.service.send(self.request())
        self.assertEqual(response.status, 202)
        self.assertEqual(data(response)["state"], "SUBMITTED")
        retry = await self.service.send(self.request())
        self.assertEqual(retry.status, 409)
        self.assertEqual(self.send_count, 1)
        # No writes to users/withdrawals/trades, no fictional ledger deduction.
        with sqlite3.connect(self.path) as db:
            names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertEqual(names, {"owner_test_withdrawal"})

    async def test_only_finalized_success_is_completed(self):
        self.chain_status = {"err": None, "confirmationStatus": "confirmed"}
        self.assertEqual(data(await self.service.send(self.request()))["state"], "SUBMITTED")
        self.chain_status = {"err": None, "confirmationStatus": "finalized"}
        self.assertEqual(data(await self.service.status(Request()))["state"], "COMPLETED")
        self.assertEqual(self.send_count, 1)

    async def test_chain_failure_is_not_completed(self):
        self.chain_status = {"err": {"InstructionError": [0, "InvalidAccountData"]},
                             "confirmationStatus": "finalized"}
        self.assertEqual(data(await self.service.send(self.request()))["state"], "FAILED")
        self.assertEqual((await self.service.send(self.request())).status, 409)

    async def test_timeout_then_restart_never_resends(self):
        self.send_error = True
        response = await self.service.send(self.request())
        self.assertEqual(data(response)["state"], "UNKNOWN")
        restarted = self.make_service()
        restarted.rpc = self.rpc
        self.assertEqual((await restarted.send(self.request())).status, 409)
        self.chain_status = {"err": None, "confirmationStatus": "finalized"}
        self.assertEqual(data(await restarted.status(Request()))["state"], "COMPLETED")
        self.assertEqual(self.send_count, 1)

    async def test_simultaneous_processes_only_broadcast_once(self):
        other = self.make_service()
        other.rpc = self.rpc
        reached = 0
        barrier = asyncio.Event()
        prepare = self.service.prepare

        async def racing_prepare(session):
            nonlocal reached
            transaction = await prepare(session)
            reached += 1
            if reached == 2:
                barrier.set()
            await barrier.wait()
            return transaction

        self.service.prepare = other.prepare = racing_prepare
        results = await asyncio.gather(self.service.send(self.request()), other.send(self.request()))
        self.assertEqual(sorted(r.status for r in results), [202, 409])
        self.assertEqual(self.send_count, 1)

    async def test_missing_ata_rent_and_checked_instruction(self):
        self.destination = None
        response = await self.service.send(self.request())
        self.assertEqual(response.status, 202)
        wire = next(params[0] for method, params in self.rpc_calls if method == "simulateTransaction")
        tx = Transaction.from_bytes(base64.b64decode(wire))
        self.assertEqual(len(tx.message.instructions), 2)
        self.assertEqual(bytes(tx.message.instructions[0].data), bytes([1]))
        self.assertEqual(bytes(tx.message.instructions[1].data),
                         bytes([12]) + (1_000_000).to_bytes(8, "little") + bytes([6]))
        self.assertEqual(len(tx.message.instructions[1].accounts), 4)

    async def test_http_auth_and_owner_gate(self):
        import main
        token = "offline-test-token"
        app = web.Application(middlewares=[main.telegram_auth_middleware])
        app.router.add_post("/api/test_withdraw", self.service.send)
        app.router.add_get("/api/test_withdraw", self.service.status)

        def auth_header(owner):
            fields = {"auth_date": str(int(time.time())), "user": json.dumps({"id": owner})}
            checked = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
            secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
            fields["hash"] = hmac.new(secret, checked.encode(), hashlib.sha256).hexdigest()
            return {"X-Telegram-Init-Data": urlencode(fields)}

        async with TestClient(TestServer(app)) as client:
            with patch.object(main, "TELEGRAM_TOKEN", token):
                self.assertEqual((await client.get("/api/test_withdraw")).status, 401)
                self.assertEqual((await client.get("/api/test_withdraw",
                    headers=auth_header(43))).status, 403)
                response = await client.get("/api/test_withdraw", headers=auth_header(42))
                self.assertEqual(response.status, 200)
                self.assertEqual((await response.json())["state"], "READY")
                response = await client.post("/api/test_withdraw",
                    headers=auth_header(42), json=self.request().body)
                self.assertEqual(response.status, 202)
                self.assertEqual((await response.json())["state"], "SUBMITTED")
        self.assertEqual(self.send_count, 1)


class MainIntegrationTests(unittest.TestCase):
    def test_safe_mode_and_owner_routes(self):
        import main
        self.assertTrue(main.CUSTODY_SAFE_MODE)
        import inspect
        for function in (main.execute_jupiter_swap, main.execute_native_sol_transfer,
                         main.execute_spl_usdc_transfer, main.api_toggle_trading,
                         main.api_toggle_meme, main.api_verify_deposit,
                         main.api_close_all_positions, main.api_withdraw_impl):
            self.assertIn("if CUSTODY_SAFE_MODE:", inspect.getsource(function))
        self.assertIn('/api/test_withdraw', inspect.getsource(main.main))
        self.assertIn('X-Telegram-Init-Data', main.HTML_CONTENT)
        self.assertIn('owner-test-panel', main.HTML_CONTENT)

    def test_forged_telegram_data_is_rejected(self):
        import main
        with patch.object(main, "TELEGRAM_TOKEN", "local-test-token"):
            with self.assertRaises(main.TelegramAuthError):
                main.verify_telegram_init_data("user=%7B%22id%22%3A42%7D&auth_date=1&hash=fake")


if __name__ == "__main__":
    unittest.main()
