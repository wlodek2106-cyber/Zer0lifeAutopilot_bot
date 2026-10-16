import json
import unittest
from unittest.mock import AsyncMock
from datetime import datetime, timezone
from legacy_retirement import prove_retirement
from wallet_runtime import WalletError, GENESIS, SYSTEM
import base64
from solders.keypair import Keypair
from solders.hash import Hash
from solders.message import Message
from solders.instruction import Instruction
from solders.transaction import VersionedTransaction


class RetirementProofTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.created = int(datetime(2026, 10, 13, 15, 29, 47, tzinfo=timezone.utc).timestamp())
        self.row = {"timestamp": "2026-10-13 15:29:47", "signature": "old-signature"}
        self.nonces, self.status, self.old_tx = [], None, None
        self.now, self.blocks = self.created + 7200, list(range(501, 652))
        self.kind, self.inner, self.history = "transfer", [], None
        self.blockhash_valid = False
        self.rpc = AsyncMock(side_effect=self.answer)

    async def answer(self, method, params):
        if method == "getGenesisHash": return GENESIS
        if method == "getSignatureStatuses": return {"value": [self.status]}
        if method == "getProgramAccounts": return {"value": self.nonces, "context": {"slot": 1000}}
        if method == "getBlockTime": return self.now
        if method == "getBlocksWithLimit": return self.blocks
        if method == "getSignaturesForAddress":
            return self.history if self.history is not None else [
                {"signature": "newer", "slot": 500, "blockTime": self.created + 600, "err": None},
                {"signature": "older", "slot": 100, "blockTime": self.created - 600, "err": None}]
        if method == "isBlockhashValid": return {"value": self.blockhash_valid}
        if method == "getTransaction":
            if params[0] == "old-signature": return self.old_tx
            return {"meta": {"err": None, "innerInstructions": self.inner},
                    "transaction": {"message": {"instructions": [
                        {"programId": str(SYSTEM), "parsed": {"type": self.kind}}]}}}
        raise AssertionError(method)

    async def test_safe_evidence_never_claims_failed_or_refund(self):
        proof = await prove_retirement(self.rpc, "wallet", self.row)
        self.assertEqual(proof["outcome"], "UNKNOWN")
        self.assertFalse(proof["refund"])
        self.assertFalse(proof["rebroadcast"])
        self.assertNotIn("sendTransaction", [c.args[0] for c in self.rpc.call_args_list])

    async def test_found_status_not_retired(self):
        self.status = {"confirmationStatus": "confirmed"}
        with self.assertRaises(WalletError): await prove_retirement(self.rpc, "wallet", self.row)

    async def test_found_finalized_tx_not_retired(self):
        self.old_tx = {"meta": {"err": None}}
        with self.assertRaises(WalletError): await prove_retirement(self.rpc, "wallet", self.row)

    async def test_current_nonce_blocks_retirement(self):
        self.nonces = [{}]
        with self.assertRaises(WalletError): await prove_retirement(self.rpc, "wallet", self.row)

    async def test_nonce_authority_change_in_history_blocks_retirement(self):
        self.kind = "authorizeNonce"
        with self.assertRaises(WalletError): await prove_retirement(self.rpc, "wallet", self.row)

    async def test_missing_cpi_recording_blocks_retirement(self):
        self.inner = None
        with self.assertRaises(WalletError): await prove_retirement(self.rpc, "wallet", self.row)

    async def test_recent_record_blocks_retirement(self):
        self.now = self.created + 60
        with self.assertRaises(WalletError): await prove_retirement(self.rpc, "wallet", self.row)

    async def test_insufficient_rooted_blocks_blocks_retirement(self):
        self.blocks = [501]
        with self.assertRaises(WalletError): await prove_retirement(self.rpc, "wallet", self.row)

    async def test_no_later_wallet_transaction_uses_signed_recent_blockhash_proof(self):
        self.history = []
        row = {**self.row, "recent_blockhash": "expired-blockhash"}
        proof = await prove_retirement(self.rpc, "wallet", row)
        self.assertEqual(proof["expiry_proof"], "isBlockhashValid_false")
        self.assertEqual(proof["rooted_blocks_after_anchor"], 0)

    async def test_valid_or_unavailable_recent_blockhash_never_retires(self):
        self.history = []
        row = {**self.row, "recent_blockhash": "still-valid"}
        self.blockhash_valid = True
        with self.assertRaises(WalletError): await prove_retirement(self.rpc, "wallet", row)
        self.blockhash_valid = None
        with self.assertRaises(WalletError): await prove_retirement(self.rpc, "wallet", row)


class SignedExpiryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.signer = Keypair()
        self.wallet = str(self.signer.pubkey())
        self.validity = {"context": {"slot": 100}, "value": False}
        self.statuses = {"context": {"slot": 101}, "value": [None]}
        self.found_tx = None
        self.history = []
        self.rpc = AsyncMock(side_effect=self.answer)
        self.row = self.signed_row([])

    def signed_row(self, instructions):
        tx = VersionedTransaction(Message.new_with_blockhash(
            instructions, self.signer.pubkey(), Hash.new_unique()), [self.signer])
        return {"signature": str(tx.signatures[0]),
                "signed_wire": base64.b64encode(bytes(tx)).decode(),
                "timestamp": datetime.now(timezone.utc).isoformat()}

    async def answer(self, method, params):
        if method == "getGenesisHash": return GENESIS
        if method == "isBlockhashValid": return self.validity
        if method == "getSignatureStatuses": return self.statuses
        if method == "getTransaction": return self.found_tx
        if method == "getSignaturesForAddress": return self.history
        raise AssertionError("Unexpected RPC: " + method)

    async def test_recent_signed_swap_recovers_without_one_hour_wait(self):
        proof = await prove_retirement(self.rpc, self.wallet, self.row)
        self.assertEqual(proof["expiry_proof"], "signed_non_nonce_blockhash_finalized_expired")
        self.assertEqual([c.args[0] for c in self.rpc.call_args_list], [
            "getGenesisHash", "isBlockhashValid", "getSignatureStatuses", "getTransaction",
            "getSignaturesForAddress"])
        self.assertFalse(proof["rebroadcast"])

    async def test_live_blockhash_never_releases(self):
        self.validity["value"] = True
        with self.assertRaises(WalletError):
            await prove_retirement(self.rpc, self.wallet, self.row)

    async def test_missing_expiry_context_never_releases(self):
        self.validity = {"value": False}
        with self.assertRaises(WalletError):
            await prove_retirement(self.rpc, self.wallet, self.row)

    async def test_stale_status_context_never_releases(self):
        self.statuses["context"]["slot"] = 99
        with self.assertRaises(WalletError):
            await prove_retirement(self.rpc, self.wallet, self.row)

    async def test_found_processed_signature_never_releases(self):
        self.statuses["value"] = [{"confirmationStatus": "processed"}]
        with self.assertRaises(WalletError):
            await prove_retirement(self.rpc, self.wallet, self.row)

    async def test_found_transaction_never_releases(self):
        self.found_tx = {"meta": {"err": None}}
        with self.assertRaises(WalletError):
            await prove_retirement(self.rpc, self.wallet, self.row)

    async def test_durable_nonce_rejected_without_expiry_check(self):
        self.row = self.signed_row([Instruction(SYSTEM, (4).to_bytes(4, "little"), [])])
        with self.assertRaises(WalletError):
            await prove_retirement(self.rpc, self.wallet, self.row)
        self.assertEqual(self.rpc.await_count, 1)

    async def test_wrong_wallet_rejected(self):
        with self.assertRaises(WalletError):
            await prove_retirement(self.rpc, str(Keypair().pubkey()), self.row)

    async def test_bad_wire_rejected(self):
        self.row["signed_wire"] = "invalid"
        with self.assertRaises(WalletError):
            await prove_retirement(self.rpc, self.wallet, self.row)

    async def test_wrong_signature_rejected(self):
        self.row["signature"] = "other"
        with self.assertRaises(WalletError):
            await prove_retirement(self.rpc, self.wallet, self.row)

    async def test_history_disagreement_never_releases(self):
        self.history = [{"signature": self.row["signature"], "blockTime": 1}]
        with self.assertRaises(WalletError):
            await prove_retirement(self.rpc, self.wallet, self.row)

    async def test_missing_history_time_never_releases(self):
        self.history = [{"signature": "other"}]
        with self.assertRaises(WalletError):
            await prove_retirement(self.rpc, self.wallet, self.row)
