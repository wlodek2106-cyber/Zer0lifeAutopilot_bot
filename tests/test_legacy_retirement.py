import json
import unittest
from unittest.mock import AsyncMock
from datetime import datetime, timezone
from legacy_retirement import prove_retirement
from wallet_runtime import WalletError, GENESIS, SYSTEM


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
