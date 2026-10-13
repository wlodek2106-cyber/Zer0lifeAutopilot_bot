import asyncio
import base64
import copy
import json
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch
from solders.keypair import Keypair
from solders.signature import Signature
from wallet_runtime import WalletError, GENESIS, SYSTEM, TOKEN, associated
from solders.pubkey import Pubkey
from custody_ledger import CustodyLedger, LedgerError, SOL, USDC
from custody_runtime import CustodyRuntime
from tx_deposits import incoming_amounts


class Request(dict):
    def __init__(self, uid=101, body=None, nonce=None):
        super().__init__(telegram_user={"id": uid})
        self.body, self.match_info = body or {}, {"nonce": nonce}
    async def json(self):
        return self.body


class TxDepositTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root, self.sender = Keypair(), Keypair()
        self.wallet, self.address = str(self.root.pubkey()), str(self.sender.pubkey())
        self.service = CustodyRuntime(self.tmp.name + "/db", "https://invalid.example", 101,
            self.wallet, lambda: self.root, lambda uid,pin:(True,""), AsyncMock(), {},
            lambda log:None, base_url="https://example.invalid")
        self.service.public_enabled = True
        durable = patch("wallet_runtime.durable", return_value=True)
        durable.start(); self.addCleanup(durable.stop)
        self.ledger = self.service.ledger
        self.ledger.user(101); self.ledger.user(202)
        self.service.solvent = AsyncMock(return_value={SOL:{"raw":20_000_000},USDC:{"raw":20_000_000}})
        self.tx = self.transaction(SOL)
        self.signature = str(Signature.new_unique())
        self.service.rpc = AsyncMock(side_effect=self.rpc)
        nonce = self.ledger.wallet_challenge(101, "https://example.invalid")
        self.ledger.link_wallet(nonce, self.address)

    async def rpc(self, session, method, params=None):
        if method == "getGenesisHash": return GENESIS
        if method == "getTransaction": return self.tx
        raise AssertionError(method)

    def transaction(self, asset):
        source_ata, target_ata = str(Keypair().pubkey()), str(associated(self.root.pubkey(), Pubkey.from_string(USDC)))
        tx = {"blockTime":int(time.time()),"transaction":{"message":{"accountKeys":[
            {"pubkey":self.address,"signer":True},{"pubkey":self.wallet,"signer":False},
            {"pubkey":source_ata,"signer":False},{"pubkey":target_ata,"signer":False}],
            "instructions":[]}},"meta":{"err":None,"innerInstructions":[],"fee":5000,
            "preBalances":[100_000_000,10_000_000,2_000_000,2_000_000],
            "postBalances":[98_995_000,11_000_000,2_000_000,2_000_000],
            "preTokenBalances":[],"postTokenBalances":[]}}
        if asset == SOL:
            tx["transaction"]["message"]["instructions"]=[{"programId":str(SYSTEM),"parsed":{
                "type":"transfer","info":{"source":self.address,"destination":self.wallet,"lamports":1_000_000}}}]
        else:
            tx["meta"]["postBalances"][1]=10_000_000
            tx["meta"]["preTokenBalances"]=[{"accountIndex":2,"owner":self.address,"mint":USDC,"uiTokenAmount":{"amount":"2000000"}}]
            tx["meta"]["postTokenBalances"]=[{"accountIndex":2,"owner":self.address,"mint":USDC,"uiTokenAmount":{"amount":"1000000"}},
                {"accountIndex":3,"owner":self.wallet,"mint":USDC,"uiTokenAmount":{"amount":"1000000"}}]
            tx["transaction"]["message"]["instructions"]=[{"programId":str(TOKEN),"parsed":{
                "type":"transferChecked","info":{"source":source_ata,"destination":target_ata,
                    "authority":self.address,"mint":USDC,"tokenAmount":{"amount":"1000000","decimals":6}}}}]
        return tx

    async def claim(self, uid=101):
        return await self.service.deposit(Request(uid, {"tx_signature":self.signature}), None)

    async def test_sol_tx_without_memo_credits_only_linked_user_and_deduplicates(self):
        await self.claim(); await self.claim()
        self.assertEqual(self.ledger.balance(101,SOL),1_000_000)
        self.assertEqual(self.ledger.balance(202,SOL),0)
        self.assertTrue(all(c.args[1] != "sendTransaction" for c in self.service.rpc.call_args_list))

    async def test_usdc_tx_without_memo_uses_chain_amount(self):
        self.tx = self.transaction(USDC)
        await self.claim()
        self.assertEqual(self.ledger.balance(101,USDC),1_000_000)

    async def test_phantom_duplicate_authority_parsed_as_multisig_accepted(self):
        self.tx = self.transaction(USDC)
        info = self.tx["transaction"]["message"]["instructions"][0]["parsed"]["info"]
        info["multisigAuthority"] = info.pop("authority")
        info["signers"] = [self.address]
        await self.claim()
        self.assertEqual(self.ledger.balance(101,USDC),1_000_000)
        self.assertEqual(self.ledger.balance(202,USDC),0)

    async def test_foreign_or_additional_multisig_signer_not_accepted(self):
        self.tx = self.transaction(USDC)
        info = self.tx["transaction"]["message"]["instructions"][0]["parsed"]["info"]
        info["multisigAuthority"] = info.pop("authority")
        info["signers"] = [str(Keypair().pubkey())]
        with self.assertRaises(WalletError): await self.claim()
        info["signers"] = [self.address, str(Keypair().pubkey())]
        with self.assertRaises(WalletError): await self.claim()
        self.assertEqual(self.ledger.balance(101,USDC),0)

    async def test_copying_public_tx_not_enough(self):
        with self.assertRaises(WalletError): await self.claim(202)
        self.assertEqual(self.ledger.balance(202,SOL),0)

    async def test_different_linked_wallet_cannot_claim(self):
        nonce=self.ledger.wallet_challenge(202,"https://example.invalid")
        self.ledger.link_wallet(nonce,str(Keypair().pubkey()))
        with self.assertRaises(WalletError): await self.claim(202)

    async def test_sender_wallet_cannot_bind_to_two_accounts(self):
        nonce=self.ledger.wallet_challenge(202,"https://example.invalid")
        with self.assertRaises(LedgerError): self.ledger.link_wallet(nonce,self.address)

    async def test_failed_or_unfinalized_tx_no_credit(self):
        self.tx["meta"]["err"]={"InstructionError":[0,"InvalidAccountData"]}
        with self.assertRaises(WalletError): await self.claim()
        self.tx=None
        with self.assertRaises(WalletError): await self.claim()
        self.assertEqual(self.ledger.balance(101,SOL),0)

    async def test_old_tx_before_activation_no_credit(self):
        self.tx["blockTime"]=self.ledger.tx_epoch()-1
        with self.assertRaises(LedgerError): await self.claim()

    async def test_insufficient_actual_backing_no_credit(self):
        self.service.solvent.return_value[SOL]["raw"]=1
        with self.assertRaises(LedgerError): await self.claim()
        self.assertEqual(self.ledger.balance(101,SOL),0)

    async def test_root_signed_outgoing_never_deposit(self):
        self.tx["transaction"]["message"]["accountKeys"][1]["signer"]=True
        with self.assertRaises(WalletError): await self.claim()

    async def test_spoofed_delegated_usdc_source_rejected(self):
        self.tx=self.transaction(USDC)
        self.tx["meta"]["preTokenBalances"][0]["owner"]=str(Keypair().pubkey())
        with self.assertRaises(WalletError): await self.claim()

    async def test_missing_inner_or_wrong_net_receipt_rejected(self):
        self.tx["meta"]["innerInstructions"]=None
        with self.assertRaises(WalletError): await self.claim()
        self.tx=self.transaction(SOL)
        self.tx["meta"]["postBalances"][1]=10_000_001
        with self.assertRaises(WalletError): await self.claim()

    async def test_actual_message_signature_linking_and_replay_block(self):
        user=Keypair()
        nonce=self.ledger.wallet_challenge(202,"https://example.invalid")
        message=self.ledger.wallet_proof(nonce)["message"]
        signature=base64.b64encode(bytes(user.sign_message(message.encode()))).decode()
        req=Request(202,{"address":str(user.pubkey()),"signature":signature},nonce)
        reply=await self.service.wallet_link_confirm(req,None)
        self.assertEqual(reply.status,200)
        self.assertEqual(self.ledger.wallet_address(202),str(user.pubkey()))
        with self.assertRaises(WalletError): await self.service.wallet_link_confirm(req,None)

    async def test_bad_signature_and_root_wallet_not_linked(self):
        user=Keypair()
        nonce=self.ledger.wallet_challenge(202,"https://example.invalid")
        req=Request(202,{"address":str(user.pubkey()),"signature":base64.b64encode(bytes(Signature.new_unique())).decode()},nonce)
        with self.assertRaises(WalletError): await self.service.wallet_link_confirm(req,None)
        self.assertIsNone(self.ledger.wallet_address(202))
        req.body["address"]=self.wallet
        with self.assertRaises(WalletError): await self.service.wallet_link_confirm(req,None)

    async def test_cross_flow_double_credit_blocked(self):
        await self.claim()
        invoice=self.ledger.invoice(101,SOL,1_000_000)
        with self.assertRaises(LedgerError):
            self.ledger.credit_deposit(invoice["id"],self.signature,SOL,1_000_000,int(time.time()))

    async def test_imported_old_coins_not_credited_again_by_tx(self):
        self.ledger.opening(101,SOL,500_000,20_000_000)
        with self.assertRaises(LedgerError): await self.claim()
        self.assertEqual(self.ledger.balance(101,SOL),500_000)

    async def test_expired_wallet_proof_rejected(self):
        nonce=self.ledger.wallet_challenge(202,"https://example.invalid")
        with self.ledger.db() as db:
            db.execute("UPDATE custody_wallet_proofs SET expires_at=? WHERE nonce=?", (int(time.time())-1,nonce))
        with self.assertRaises(WalletError):
            await self.service.wallet_link_confirm(Request(202,{},nonce),None)
