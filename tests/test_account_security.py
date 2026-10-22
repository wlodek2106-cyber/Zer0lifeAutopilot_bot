"""Real P-256/CBOR synthetic WebAuthn ceremonies; no chain, model, email or user funds."""
import asyncio
import hashlib
import json
import os
import sqlite3
from pathlib import Path
import tempfile
import time
import unittest
import uuid
import sys
from unittest.mock import AsyncMock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import cbor2
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import hashes
from webauthn.helpers import bytes_to_base64url as b64
from webauthn import base64url_to_bytes as unb64
from solders.keypair import Keypair
from custody_ledger import CustodyLedger, USDC
from custody_runtime import CustodyRuntime
from wallet_runtime import WalletRuntime
from solders.signature import Signature
from account_security import AccountSecurity, user_handle, intent
from wallet_runtime import WalletError

ORIGIN="https://example.test"


class Request(dict):
    def __init__(self,body=None,uid=101,origin=ORIGIN,query=None):
        super().__init__(telegram_user={"id":uid,"username":"tester"})
        self.body=body or {}
        self.headers={"Origin":origin}
        self.query=query or {}
    async def json(self):
        return self.body


class TelegramResponse:
    status = 200
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        return False
    async def json(self, content_type=None):
        return {"ok": True, "result": {"message_id": 1}}


class TelegramSession:
    def __init__(self):
        self.calls = []
    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return TelegramResponse()


def decoded(response):
    return json.loads(response.text)


class Device:
    def __init__(self):
        self.key=ec.generate_private_key(ec.SECP256R1())
        self.id=bytes(Keypair().pubkey())
        self.counter=0
    def response(self,options,phase,uid=101,uv=True,origin=ORIGIN,rpid="example.test"):
        client=json.dumps({"type":"webauthn.create" if phase=="register" else "webauthn.get",
                           "challenge":options["challenge"],"origin":origin,"crossOrigin":False}).encode()
        flags=1|(4 if uv else 0)
        if phase=="register":
            n=self.key.public_key().public_numbers()
            cose=cbor2.dumps({1:2,3:-7,-1:1,-2:n.x.to_bytes(32,"big"),-3:n.y.to_bytes(32,"big")})
            auth=hashlib.sha256(rpid.encode()).digest()+bytes([flags|64])+bytes(4)+bytes(16)+len(self.id).to_bytes(2,"big")+self.id+cose
            result={"attestationObject":b64(cbor2.dumps({"fmt":"none","attStmt":{},"authData":auth})),
                    "clientDataJSON":b64(client),"transports":["internal"]}
        else:
            self.counter+=1
            auth=hashlib.sha256(rpid.encode()).digest()+bytes([flags])+self.counter.to_bytes(4,"big")
            signature=self.key.sign(auth+hashlib.sha256(client).digest(),ec.ECDSA(hashes.SHA256()))
            result={"authenticatorData":b64(auth),"clientDataJSON":b64(client),"signature":b64(signature),
                    "userHandle":b64(user_handle(uid))}
        return {"id":b64(self.id),"rawId":b64(self.id),"type":"public-key","response":result,
                "clientExtensionResults":{}}


class AccountSecurityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.ledger=CustodyLedger(self.temp.name+"/db")
        self.vault=str(Keypair().pubkey())
        self.accounts=AccountSecurity(self.ledger,ORIGIN,lambda uid,pin:(pin=="test-code","ok"),self.vault,
                                      notification_token="test-telegram-token")
        self.device=Device()
        self.transfer={"purpose":"withdraw","pin":"test-code","token":"USDC","amount":"1.25",
                       "recipient_address":str(Keypair().pubkey()),"request_id":str(uuid.uuid4())}
    def tearDown(self):
        self.temp.cleanup()
    async def register(self,uid=101,nick="TraderOne"):
        return decoded(await self.accounts.register(Request({"nickname":nick,"accept_risk":True,"uid":999},uid)))
    async def ticket(self,purpose="enroll",uid=101,**fields):
        body={"purpose":purpose,"pin":"test-code",**fields}
        return decoded(await self.accounts.create_ticket(Request(body,uid)))["ticket"]
    async def begin(self,ticket):
        return decoded(await self.accounts.begin(Request({"ticket":ticket,"pin":"test-code"})))
    async def finish(self,ticket,device=None,**flags):
        data=await self.begin(ticket)
        response=(device or self.device).response(data["options"],data["phase"],**flags)
        return decoded(await self.accounts.complete(Request({"ticket":ticket,"pin":"test-code","credential":response})))
    async def enrolled(self):
        await self.register()
        ticket=await self.ticket()
        data=await self.finish(ticket)
        self.assertEqual(data["phase"],"enrolled")
        self.assertEqual(len(data["recovery_codes"]),8)
        return data
    async def approved(self):
        ticket=await self.ticket(**{k:v for k,v in self.transfer.items() if k!="pin"})
        self.assertEqual((await self.finish(ticket))["phase"],"approved")
        return ticket
    def store_withdrawal_code(self,uid,body,code):
        _,binding=self.accounts.withdrawal_intent(uid,body)
        now=int(time.time())
        with self.ledger.db() as db:
            db.execute("""INSERT INTO app_withdrawal_codes
                (id,uid,intent_hash,code_hash,created_at,expires)
                VALUES(?,?,?,?,?,?)""",
                (uuid.uuid4().hex,uid,binding,
                 self.accounts._withdrawal_code_hash(uid,binding,code),now,now+300))

    async def test_profile_is_server_unique_and_cannot_select_another_uid(self):
        state=await self.register()
        self.assertTrue(state["registered"])
        self.assertFalse(self.accounts.state(999)["registered"])
        with self.assertRaises(WalletError):
            await self.register(202,"traderone")
        self.assertEqual((await self.register(101,"DifferentName"))["nickname"],"TraderOne")
        with self.assertRaises(WalletError):
            await self.register(303,"<script>")
        with self.assertRaises(WalletError):
            await self.accounts.register(Request({"nickname":"UserGood","accept_risk":"true"},303))
        self.assertTrue(self.ledger.audit())

    async def test_real_registration_and_one_time_exact_withdrawal(self):
        result=await self.enrolled()
        self.assertTrue(self.accounts.state(101)["has_passkey"])
        with self.ledger.db() as db:
            codes=[r["hash"] for r in db.execute("SELECT hash FROM app_recovery")]
        self.assertFalse(set(codes)&set(result["recovery_codes"]))
        ticket=await self.approved()
        for fields in ({"amount":"1.26"},{"token":"SOL"},{"recipient_address":str(Keypair().pubkey())},
                       {"request_id":str(uuid.uuid4())}):
            with self.assertRaises(WalletError):
                self.accounts.consume(101,{**self.transfer,**fields,"passkey_ticket":ticket})
        with self.assertRaises(WalletError):
            self.accounts.consume(202,{**self.transfer,"passkey_ticket":ticket})
        self.accounts.consume(101,{**self.transfer,"amount":"1.250000","passkey_ticket":ticket})
        with self.assertRaises(WalletError):
            self.accounts.consume(101,{**self.transfer,"passkey_ticket":ticket})
        self.assertTrue(self.ledger.audit())

    async def test_telegram_code_is_delivered_once_and_binds_exact_withdrawal(self):
        await self.register()
        body={k:v for k,v in self.transfer.items() if k not in ("purpose","pin")}
        code_body={**body,"withdrawal_fee_raw":18_750,"withdrawal_net_raw":1_231_250}
        session=TelegramSession()
        response=await self.accounts.send_withdrawal_code(Request(body),session,code_body)
        self.assertEqual(decoded(response),{"success":True,"expires_in":300,
            "asset":"USDC","requested_raw":"1250000","withdrawal_fee_raw":"18750",
            "net_raw":"1231250"})
        message=session.calls[0][1]["json"]["text"]
        code=message.split("Code: ",1)[1].splitlines()[0]
        self.assertEqual(len(code),6)
        self.assertIn(body["recipient_address"],message)
        self.assertIn("1.25 USDC",message)
        self.assertIn("Withdrawal fee (1.5%): 0.01875 USDC",message)
        self.assertIn("Recipient receives: 1.23125 USDC",message)
        with self.ledger.db() as db:
            row=db.execute("SELECT * FROM app_withdrawal_codes WHERE uid=101").fetchone()
        self.assertNotIn(code,row["code_hash"])
        for fields in ({"amount":"1.26"}, {"token":"SOL"},
                       {"recipient_address":str(Keypair().pubkey())},
                       {"request_id":str(uuid.uuid4())}):
            with self.assertRaises(WalletError):
                self.accounts.consume_withdrawal_code(101,{**body,**fields,"telegram_code":code})
        with self.ledger.db() as db:
            self.assertEqual(db.execute("SELECT attempts FROM app_withdrawal_codes WHERE uid=101")
                             .fetchone()["attempts"],4)
        self.accounts.consume_withdrawal_code(101,{**body,"telegram_code":code})
        with self.assertRaises(WalletError):
            self.accounts.consume_withdrawal_code(101,{**body,"telegram_code":code})

    async def test_no_pin_only_or_client_biometric_boolean_bypass(self):
        await self.register()
        with self.assertRaises(WalletError):
            await self.ticket(**{k:v for k,v in self.transfer.items() if k!="pin"})
        with self.assertRaises(WalletError):
            self.accounts.consume(101,{**self.transfer,"biometric":True,"face_verified":True})
        with self.assertRaises(WalletError):
            await self.accounts.create_ticket(Request({"purpose":"enroll","pin":"wrong"}))

    async def test_origin_rpid_and_user_verification_are_required(self):
        await self.register()
        ticket=await self.ticket()
        with self.assertRaises(WalletError):
            await self.accounts.begin(Request({"ticket":ticket,"pin":"test-code"},origin="https://evil.test"))
        for flags in ({"uv":False},{"origin":"https://evil.test"},{"rpid":"evil.test"}):
            with self.assertRaises(WalletError):
                await self.finish(ticket,**flags)
        self.assertFalse(self.accounts.state(101)["has_passkey"])
        await self.finish(ticket)
        with self.assertRaises(WalletError):
            await self.finish(ticket)

    async def test_bad_signature_challenge_and_foreign_credential_fail(self):
        await self.enrolled()
        ticket=await self.ticket(**{k:v for k,v in self.transfer.items() if k!="pin"})
        with self.assertRaises(WalletError):
            await self.finish(ticket,Device())
        data=await self.begin(ticket)
        response=self.device.response(data["options"],"auth")
        response["response"]["signature"]=b64(b"not a signature")
        with self.assertRaises(WalletError):
            await self.accounts.complete(Request({"ticket":ticket,"pin":"test-code","credential":response}))
        data["options"]["challenge"]=b64(b"wrong challenge")
        response=self.device.response(data["options"],"auth")
        with self.assertRaises(WalletError):
            await self.accounts.complete(Request({"ticket":ticket,"pin":"test-code","credential":response}))
        with self.assertRaises(WalletError):
            await self.finish(ticket,uid=202)
        self.assertEqual((await self.finish(ticket))["phase"],"approved")

    async def test_additional_passkey_requires_existing_key_first(self):
        await self.enrolled()
        ticket=await self.ticket()
        first=await self.begin(ticket)
        self.assertEqual(first["phase"],"auth")
        registration=await self.finish(ticket)
        self.assertEqual(registration["phase"],"register")
        second=Device()
        response=second.response(registration["options"],"register")
        result=decoded(await self.accounts.complete(Request({"ticket":ticket,"pin":"test-code","credential":response})))
        self.assertEqual(result["recovery_codes"],[])
        self.assertEqual(self.accounts.state(101)["passkey_count"],2)

    async def test_recovery_replaces_keys_and_revokes_outstanding_approvals(self):
        enrolled=await self.enrolled()
        approval=await self.approved()
        with self.assertRaises(WalletError):
            await self.ticket("recover",recovery_code="wrong")
        ticket=await self.ticket("recover",recovery_code=enrolled["recovery_codes"][0])
        await self.finish(ticket,Device())
        self.assertEqual(self.accounts.state(101)["passkey_count"],1)
        with self.assertRaises(WalletError):
            self.accounts.consume(101,{**self.transfer,"passkey_ticket":approval})
        with self.assertRaises(WalletError):
            await self.ticket("recover",recovery_code=enrolled["recovery_codes"][0])
        # Old device cannot approve an additional credential on the recovered account.
        ticket=await self.ticket()
        with self.assertRaises(WalletError):
            await self.finish(ticket)

    async def test_parallel_first_enrollment_cannot_add_unauthorized_second_key(self):
        await self.register()
        a,b=await self.ticket(),await self.ticket()
        await self.finish(a)
        with self.assertRaises(WalletError):
            await self.finish(b,Device())
        self.assertEqual(self.accounts.state(101)["passkey_count"],1)

    async def test_expiry_attempt_limit_status_ownership_and_hash_at_rest(self):
        await self.register()
        ticket=await self.ticket()
        with self.ledger.db() as db:
            self.assertFalse(db.execute("SELECT 1 FROM app_passkey_tickets WHERE hash=?", (ticket,)).fetchone())
        with self.assertRaises(WalletError):
            await self.accounts.ticket_status(Request(uid=202,query={"ticket":ticket}))
        data=await self.begin(ticket)
        for _ in range(8):
            with self.assertRaises(WalletError):
                await self.accounts.complete(Request({"ticket":ticket,"pin":"test-code","credential":{}}))
        with self.assertRaises(WalletError):
            await self.begin(ticket)
        with self.ledger.db() as db:
            self.assertEqual(db.execute("SELECT attempts FROM app_passkey_tickets").fetchone()[0],8)
            db.execute("UPDATE app_passkey_tickets SET expires=?", (int(time.time())-1,))
        with self.assertRaises(WalletError):
            await self.begin(ticket)
        self.assertTrue(self.ledger.audit())

    def test_origin_must_be_fixed_https_and_amounts_are_exact_strings(self):
        with self.assertRaises(ValueError):
            AccountSecurity(self.ledger,"http://evil.test",lambda *a:True)
        a,ha=intent(101,self.transfer)
        b,hb=intent(101,{**self.transfer,"amount":"1.250000"})
        self.assertEqual(a["raw"],"1250000")
        self.assertEqual(ha,hb)

    async def test_private_consistent_backup_is_not_overwritten_by_later_enrollment(self):
        backup=self.ledger.path+".pre-account-security-v1.sqlite3"
        self.assertEqual(os.stat(backup).st_mode&0o777,0o600)
        await self.enrolled()
        AccountSecurity(self.ledger,ORIGIN,self.accounts.pin_fn,self.vault)
        with sqlite3.connect(backup) as db:
            self.assertEqual(db.execute("PRAGMA quick_check").fetchone()[0],"ok")
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='app_passkeys'").fetchone())

    async def test_actual_withdraw_controller_requires_proof_before_reserving_and_never_resends(self):
        await self.enrolled()
        signer=Keypair()
        service=CustodyRuntime(self.ledger.path,"https://invalid.test",101,str(signer.pubkey()),
            lambda:signer,lambda uid,pin:(pin=="test-code","ok"),AsyncMock(return_value={}),{},
            lambda message:None,base_url=ORIGIN)
        service.accounts=self.accounts
        service.public_enabled=True
        service.ready=lambda:signer
        service.reconcile=AsyncMock()
        service.solvent=AsyncMock()
        service.send_reserved=AsyncMock(side_effect=lambda session,tx,row,metadata:row)
        self.ledger.user(101)
        self.ledger.seed_fees(20_000_000,50_000_000)
        invoice=self.ledger.invoice(101,USDC,2_000_000)
        self.ledger.credit_deposit(invoice["id"],str(Signature.new_unique()),USDC,2_000_000,invoice["created_at"])
        body={k:v for k,v in self.transfer.items() if k not in ("purpose","pin")}
        with patch.object(WalletRuntime,"withdraw_transaction",new_callable=AsyncMock) as transaction:
            with self.assertRaises(WalletError):
                await service.withdraw(Request(body),None)
            self.assertIsNone(self.ledger.find_request(101,self.transfer["request_id"]))
            self.assertEqual(self.ledger.balance(101,USDC),2_000_000)
            transaction.assert_not_awaited()
            self.store_withdrawal_code(101,body,"123456")
            body={**body,"telegram_code":"123456"}
            await service.withdraw(Request(body),None)
            transaction.assert_awaited_once()
            self.assertEqual(self.ledger.balance(101,USDC),750_000)
            await service.withdraw(Request(body),None)
            transaction.assert_awaited_once()
            self.assertEqual(self.ledger.balance(101,USDC),750_000)
        self.assertTrue(self.ledger.audit())


if __name__=="__main__":
    unittest.main()
