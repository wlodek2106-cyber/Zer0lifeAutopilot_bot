"""Synthetic chain receipts and real signed WebAuthn, never real payments."""
import asyncio
import json
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from solders.keypair import Keypair
from account_security import AccountSecurity, digest
from custody_ledger import CustodyLedger, SOL, USDC, FEE_CAP
from wallet_runtime import WalletError, GENESIS, SYSTEM, TOKEN
from web_access import WebAccess, RECEIVER, PRICE, PERIOD, SESSION_COOKIE, LOGIN_COOKIE
from test_account_security import Device, Request, ORIGIN, decoded

OWNER = 428821665


class BrowserRequest(Request):
    def __init__(self, body=None, uid=101, path="/web-auth/start", cookies=None, headers=None, method="POST"):
        super().__init__(body or {}, uid)
        self.path, self.method, self.remote = path, method, "127.0.0.1"
        self.cookies = cookies or {}
        self.headers = {"Origin": ORIGIN, **(headers or {})}
        raw = json.dumps(body or {}).encode()
        self.content_length = len(raw)
        self.content = SimpleNamespace(read=AsyncMock(return_value=raw))
        self["auth_method"] = "web"


class WebAccessTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.ledger = CustodyLedger(self.temp.name + "/ledger")
        self.sender = str(Keypair().pubkey())
        self.accounts = AccountSecurity(self.ledger, ORIGIN,
            lambda uid, pin: (pin == "test-code", "ok"), str(Keypair().pubkey()))
        self.runtime = SimpleNamespace(ledger=self.ledger, accounts=self.accounts, owner_id=OWNER,
            wallet=self.accounts.vault, public_enabled=False, beta_users={OWNER, 101, 102},
            uid=lambda r: int(r["telegram_user"]["id"]), admit=lambda uid: None)
        self.access = WebAccess(self.runtime)
        self.runtime.web_access = self.access
        self.runtime.rpc = AsyncMock(side_effect=self.rpc)
        self.signature = str(Keypair().sign_message(b"receipt"))
        self.tx = self.transfer(self.signature)
        for uid in (OWNER, 101, 102):
            self.ledger.user(uid, "Trader" + str(uid))
        with self.ledger.db() as db:
            db.execute("INSERT INTO custody_wallet_links VALUES(?,?,?)", (101, self.sender, int(time.time())))
            db.execute("INSERT INTO custody_wallet_links VALUES(?,?,?)", (OWNER, RECEIVER, int(time.time())))
            for uid in (OWNER, 101, 102):
                nick = "Trader" + str(uid)
                db.execute("""INSERT INTO app_profiles
                    (uid,nickname,nickname_key,accepted_at) VALUES(?,?,?,?)""",
                    (uid, nick, nick.lower(), int(time.time())))

    def tearDown(self):
        self.temp.cleanup()

    def transfer(self, signature, raw=PRICE, sender=None, receiver=RECEIVER, stamp=None):
        sender = sender or self.sender
        return {"blockTime": int(time.time()) if stamp is None else stamp,
            "transaction": {"signatures": [signature], "message": {
                "accountKeys": [{"pubkey": sender, "signer": True}, {"pubkey": receiver, "signer": False}],
                "instructions": [{"programId": str(SYSTEM), "parsed": {"type": "transfer",
                    "info": {"source": sender, "destination": receiver, "lamports": raw}}}]}},
            "meta": {"err": None, "innerInstructions": [], "preBalances": [10**10, 0],
                "postBalances": [10**10 - raw - 5000, raw], "preTokenBalances": [], "postTokenBalances": []}}

    async def rpc(self, session, method, params=None):
        return GENESIS if method == "getGenesisHash" else self.tx

    async def new_order(self, uid=101):
        return decoded(await self.access.order(BrowserRequest(uid=uid), None))["order"]

    async def pay(self, order, signature=None, uid=101):
        return decoded(await self.access.verify(BrowserRequest(
            {"order_id": order["id"], "tx_signature": signature or self.signature}, uid=uid), None))

    async def enroll(self, uid=101):
        self.device = Device()
        ticket = decoded(await self.accounts.create_ticket(Request({"purpose":"enroll","pin":"test-code"}, uid)))["ticket"]
        options = decoded(await self.accounts.begin(Request({"ticket":ticket,"pin":"test-code"}, uid)))
        await self.accounts.complete(Request({"ticket":ticket,"pin":"test-code",
            "credential":self.device.response(options["options"],"register",uid=uid)},uid))

    async def login_start(self):
        response = await self.access.public_action(BrowserRequest({"nickname":"Trader101","pin":"test-code","uid":OWNER,"admin":True}))
        return decoded(response)["ticket"], response.cookies[LOGIN_COOKIE].value

    async def approve_login(self, ticket):
        options = decoded(await self.accounts.begin(Request({"ticket":ticket,"pin":"test-code"})))
        return decoded(await self.accounts.complete(Request({"ticket":ticket,"pin":"test-code",
            "credential":self.device.response(options["options"],"auth")})))

    async def test_only_trusted_owner_is_permanently_free(self):
        self.assertTrue(self.access.state(OWNER)["admin_free"])
        self.assertIsNone(self.access.state(OWNER)["expires_at"])
        for uid in (101, 102):
            self.assertFalse(self.access.state(uid)["active"])
            with self.assertRaises(WalletError):
                self.access.require(uid)
        self.access.require(OWNER)
        with self.assertRaises(WalletError):
            await self.new_order(OWNER)
        self.runtime.rpc.assert_not_called()

    async def test_payment_is_not_a_custody_deposit_and_never_enables_modes(self):
        order = await self.new_order()
        before = self.ledger.balance(101, SOL)
        now = int(time.time())
        result = await self.pay(order)
        self.assertTrue(result["active"])
        self.assertEqual(result["expires_at"], now + PERIOD)
        self.assertEqual(self.ledger.balance(101, SOL), before)
        self.assertFalse(self.ledger.settings(101)["trading"])
        self.assertFalse(self.ledger.settings(101)["sniper_trading"])
        self.assertFalse(self.access.state(102)["active"])

    async def test_idempotent_payment_and_concurrent_duplicate_do_not_extend_twice(self):
        order = await self.new_order()
        first, second = await asyncio.gather(self.pay(order), self.pay(order))
        self.assertEqual(first["expires_at"], second["expires_at"])
        self.assertEqual((await self.pay(order))["expires_at"], first["expires_at"])
        with self.ledger.db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM access_receipts").fetchone()[0], 1)

    async def test_renewal_adds_30_days_to_remaining_time_and_not_just_now(self):
        first = await self.pay(await self.new_order())
        self.signature = str(Keypair().sign_message(b"second-payment"))
        self.tx = self.transfer(self.signature)
        second = await self.pay(await self.new_order())
        self.assertEqual(second["expires_at"], first["expires_at"] + PERIOD)
        self.assertFalse(self.ledger.settings(101)["trading"])

    async def test_foreign_uid_order_and_other_wallet_cannot_claim(self):
        order = await self.new_order()
        with self.assertRaises(WalletError):
            await self.pay(order, uid=102)
        self.tx = self.transfer(self.signature, sender=str(Keypair().pubkey()))
        with self.assertRaises(WalletError):
            await self.pay(order)
        self.assertFalse(self.access.state(101)["active"])

    async def test_wrong_amount_asset_destination_and_failed_tx_rejected(self):
        order = await self.new_order()
        for raw in (PRICE - 1, PRICE + 1):
            self.tx = self.transfer(self.signature, raw=raw)
            with self.assertRaises(WalletError):
                await self.pay(order)
        self.tx = self.transfer(self.signature)
        self.tx["transaction"]["message"]["instructions"][0]["programId"] = str(TOKEN)
        with self.assertRaises(WalletError):
            await self.pay(order)
        self.tx = self.transfer(self.signature, receiver=str(Keypair().pubkey()))
        with self.assertRaises(WalletError):
            await self.pay(order)
        self.tx = self.transfer(self.signature)
        self.tx["meta"]["err"] = {"InstructionError":[0,"error"]}
        with self.assertRaises(WalletError):
            await self.pay(order)
        self.tx = None
        with self.assertRaises(WalletError):
            await self.pay(order)
        self.assertFalse(self.access.state(101)["active"])

    async def test_old_future_unmatched_and_devnet_receipts_rejected(self):
        order = await self.new_order()
        for stamp in (order["created"]-31, order["deadline"]+1, int(time.time())+120):
            self.tx = self.transfer(self.signature, stamp=stamp)
            with self.assertRaises(WalletError):
                await self.pay(order)
        self.tx = self.transfer(str(Keypair().sign_message(b"wrong-signature")))
        with self.assertRaises(WalletError):
            await self.pay(order)
        self.runtime.rpc = AsyncMock(return_value="devnet")
        with self.assertRaises(WalletError):
            await self.pay(order)

    async def test_expiry_stops_both_modes_but_preserves_explicit_close_request(self):
        self.ledger.settings(101, enabled=True, sniper_enabled=True, close=True)
        self.ledger.settings(OWNER, enabled=True, sniper_enabled=True)
        self.access.pause_expired()
        settings = self.ledger.settings(101)
        self.assertFalse(settings["trading"])
        self.assertFalse(settings["sniper_trading"])
        self.assertTrue(settings["close_requested"])
        self.assertTrue(self.ledger.settings(OWNER)["trading"])
        with self.ledger.db() as db:
            db.execute("INSERT INTO access_memberships VALUES(?,?)", (101,int(time.time())+1))
        self.access.require(101)
        with patch("web_access.time.time",return_value=time.time()+2):
            with self.assertRaises(WalletError):
                self.access.require(101, "SELL")

    async def test_no_telegram_paid_trading_or_memo_or_key_bypass(self):
        req = BrowserRequest(uid=101)
        req["auth_method"] = "telegram"
        with self.assertRaises(WalletError):
            self.access.trading_request(req)
        self.access.trading_request(BrowserRequest(uid=OWNER))
        with self.assertRaises(WalletError):
            self.access.require(101, "SELL", "someone-elses-position", manual=True)

    async def test_previous_payment_cannot_activate_second_order_and_expired_renewal_starts_now(self):
        first_order=await self.new_order()
        await self.pay(first_order)
        second_order=await self.new_order()
        with self.assertRaises(WalletError):
            await self.pay(second_order)
        with self.ledger.db() as db:
            db.execute("UPDATE access_memberships SET expires=? WHERE uid=101",(int(time.time())-1,))
        self.signature=str(Keypair().sign_message(b"renew-after-expiry"))
        self.tx=self.transfer(self.signature)
        result=await self.pay(second_order)
        self.assertAlmostEqual(result["expires_at"],int(time.time())+PERIOD,delta=1)

    async def test_confirmed_order_survives_reload_and_late_verification(self):
        order=await self.new_order()
        state=decoded(await self.access.status(BrowserRequest(),None))
        self.assertEqual(state["pending_order"]["id"],order["id"])
        with patch("web_access.time.time",return_value=time.time()+4000):
            result=await self.pay(order)
        self.assertTrue(result["active"])

    def funded_buy(self):
        self.ledger.seed_fees(50_000_000, 100_000_000)
        invoice = self.ledger.invoice(101, USDC, 2_000_000)
        self.ledger.credit_deposit(invoice["id"], str(Keypair().sign_message(b"funds")),
                                   USDC, 2_000_000, invoice["created_at"])
        metadata = {"output_asset":SOL,"symbol":"SOL","decimals":9}
        row = self.ledger.reserve(101, "buy-fixture", "BUY", USDC, 1_000_000, metadata)
        return row

    async def test_expiry_during_simulation_releases_reservation_without_signing_or_broadcast(self):
        from custody_runtime import CustodyRuntime
        row = self.funded_buy()
        runtime = SimpleNamespace(ledger=self.ledger, web_access=self.access,
            safe_simulation=AsyncMock(return_value="offline-wire"), rpc=AsyncMock(), reconcile=AsyncMock())
        with self.assertRaises(WalletError):
            await CustodyRuntime.send_reserved(runtime, None, SimpleNamespace(signatures=["offline"]), row, {})
        self.assertEqual(self.ledger.operation(row["id"])["state"],"FAILED")
        self.assertEqual(self.ledger.balance(101,USDC),2_000_000)
        self.assertEqual(self.ledger.pending(),[])
        runtime.rpc.assert_not_called()
        self.assertTrue(self.ledger.audit())

    async def test_expired_user_can_explicitly_close_own_position_even_after_reservation(self):
        row = self.funded_buy()
        self.ledger.prepared(row["id"],str(Keypair().sign_message(b"opened")),"offline")
        self.ledger.finalize(row["id"],1_000_000,5000,10_000_000)
        position = self.ledger.positions(101,"OPEN")[0]
        self.ledger.settings(101,close=True)
        self.access.require(101,"SELL",position["id"],manual=True)
        self.ledger.reserve(101,"close-fixture","SELL",SOL,position["raw"],
                            {"output_asset":USDC},position_id=position["id"])
        self.access.require(101,"SELL",position["id"],manual=True)
        with self.assertRaises(WalletError):
            self.access.require(102,"SELL",position["id"],manual=True)
        with self.assertRaises(WalletError):
            self.access.require(101,"SELL",position["id"],manual=False)
        self.assertTrue(self.ledger.audit())

    async def test_login_needs_real_passkey_and_is_bound_to_browser_not_body_uid(self):
        await self.enroll()
        ticket, binder = await self.login_start()
        req = BrowserRequest({"ticket":ticket}, path="/web-auth/finish", cookies={LOGIN_COOKIE:binder})
        self.assertEqual((await self.access.public_action(req)).status, 202)
        await self.approve_login(ticket)
        with self.assertRaises(WalletError):
            await self.access.public_action(BrowserRequest({"ticket":ticket}, path="/web-auth/finish"))
        result = await self.access.public_action(req)
        cookie = result.cookies[SESSION_COOKIE]
        self.assertTrue(cookie["secure"] and cookie["httponly"])
        self.assertEqual(cookie["samesite"], "Strict")
        browser = BrowserRequest(path="/api/stats", cookies={SESSION_COOKIE:cookie.value}, method="GET")
        self.assertEqual(self.access.authenticate(browser)["id"], 101)
        self.assertNotEqual(self.access.authenticate(browser)["id"], OWNER)
        with self.assertRaises(WalletError):
            await self.access.public_action(req)
        with self.ledger.db() as db:
            stored = db.execute("SELECT hash FROM web_sessions").fetchone()[0]
        self.assertNotEqual(stored, cookie.value)
        self.assertEqual(stored, digest(cookie.value))

    async def test_sessions_require_csrf_and_revoked_passkeys_revoke_login(self):
        await self.enroll()
        ticket,binder = await self.login_start()
        await self.approve_login(ticket)
        result = await self.access.public_action(BrowserRequest({"ticket":ticket}, path="/web-auth/finish",
                                                               cookies={LOGIN_COOKIE:binder}))
        token = result.cookies[SESSION_COOKIE].value
        with self.assertRaises(WalletError):
            self.access.authenticate(BrowserRequest(cookies={SESSION_COOKIE:token}))
        req = BrowserRequest(cookies={SESSION_COOKIE:token},headers={"X-ZRL-CSRF":digest("csrf:"+token)})
        self.assertEqual(self.access.authenticate(req)["id"],101)
        req.headers["Origin"]="https://evil.test"
        with self.assertRaises(WalletError):
            self.access.authenticate(req)
        req.headers["Origin"]=ORIGIN
        with self.ledger.db() as db:
            db.execute("DELETE FROM app_passkeys WHERE uid=101")
        with self.assertRaises(WalletError):
            self.access.authenticate(req)

    async def test_http_middleware_trusts_session_not_requested_admin_uid(self):
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer
        import main
        await self.enroll()
        ticket,binder = await self.login_start()
        await self.approve_login(ticket)
        result = await self.access.public_action(BrowserRequest({"ticket":ticket}, path="/web-auth/finish",
                                                               cookies={LOGIN_COOKIE:binder}))
        token = result.cookies[SESSION_COOKIE].value
        app = web.Application(middlewares=[main.telegram_auth_middleware])
        app["wallet_runtime"] = self.runtime
        async def identity(req):
            return web.json_response({"uid":req["telegram_user"]["id"]})
        app.router.add_get("/api/stats",identity)
        app.router.add_post("/api/write",identity)
        app.router.add_get("/api/admin/status",identity)
        async with TestClient(TestServer(app)) as client:
            headers={"Cookie":SESSION_COOKIE+"="+token}
            response=await client.get("/api/stats?uid="+str(OWNER),headers=headers)
            self.assertEqual((await response.json())["uid"],101)
            response=await client.post("/api/write",headers=headers,json={"uid":OWNER})
            self.assertEqual(response.status,403)
            headers.update({"Origin":ORIGIN,"X-ZRL-CSRF":digest("csrf:"+token)})
            response=await client.post("/api/write",headers=headers,json={"uid":OWNER,"admin":True})
            self.assertEqual((await response.json())["uid"],101)
            with patch.object(main,"ADMIN_TELEGRAM_ID",OWNER):
                response=await client.get("/api/admin/status",headers=headers)
                self.assertEqual(response.status,403)

    async def test_pin_and_origin_and_rate_limit_and_no_passkey_login_fail_closed(self):
        with self.assertRaises(WalletError):
            await self.access.public_action(BrowserRequest({"nickname":"Trader101","pin":"wrong"}))
        with self.assertRaises(WalletError):
            await self.access.public_action(BrowserRequest({"nickname":"Trader101","pin":"test-code"}))
        with self.assertRaises(WalletError):
            await self.access.public_action(BrowserRequest(headers={"Origin":"https://evil.test"}))
        for i in range(18):
            self.access.rate_limit(BrowserRequest(),"trader101")
        with self.assertRaises(WalletError):
            self.access.rate_limit(BrowserRequest(),"trader101")


if __name__ == "__main__":
    unittest.main()
