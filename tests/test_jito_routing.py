import base64
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from solders.hash import Hash
from solders.keypair import Keypair
from solders.pubkey import Pubkey

from custody_runtime import (
    CustodyRuntime,
    JITO_BUNDLE_SEND_URL,
    JITO_TIP_LAMPORTS,
    JUPITER_PRIORITY_FEE_LAMPORTS,
    SOL,
    WalletError,
    adjust_swap_amounts_for_operator_tip,
    jupiter_swap_instructions_payload,
)


class FakeResponse:
    def __init__(self, status=200, payload=None):
        self.status = status
        self.payload = payload or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def json(self):
        return self.payload


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


class JitoRoutingTests(unittest.IsolatedAsyncioTestCase):
    def test_jupiter_request_keeps_priority_fee_and_dynamic_compute(self):
        payload = jupiter_swap_instructions_payload({"quote": "fixture"}, "wallet")
        priority = payload["prioritizationFeeLamports"]["priorityLevelWithMaxLamports"]
        self.assertEqual(priority["maxLamports"], JUPITER_PRIORITY_FEE_LAMPORTS)
        self.assertEqual(priority["priorityLevel"], "veryHigh")
        self.assertFalse(priority["global"])
        self.assertTrue(payload["dynamicComputeUnitLimit"])
        self.assertEqual(payload["quoteResponse"], {"quote": "fixture"})
        self.assertEqual(payload["userPublicKey"], "wallet")

    def test_tip_is_removed_from_sol_trade_amount_and_charged_to_reserve(self):
        metadata = {
            "execution_route": "jito_bundle_only",
            "jito_tip_lamports": JITO_TIP_LAMPORTS,
        }
        spent, received = adjust_swap_amounts_for_operator_tip(
            1_000_000 + JITO_TIP_LAMPORTS, 0, SOL, "token-mint", metadata)
        self.assertEqual((spent, received), (1_000_000, 0))

        spent, received = adjust_swap_amounts_for_operator_tip(
            0, 2_000_000 - JITO_TIP_LAMPORTS, "token-mint", SOL, metadata)
        self.assertEqual((spent, received), (0, 2_000_000))

    def test_tip_metadata_cannot_exceed_or_bypass_approved_cap(self):
        for metadata in (
            {"execution_route": "jito_bundle_only", "jito_tip_lamports": JITO_TIP_LAMPORTS + 1},
            {"jito_tip_lamports": JITO_TIP_LAMPORTS},
            {"execution_route": "jito_bundle_only", "jito_tip_lamports": 0},
        ):
            with self.assertRaises(WalletError):
                adjust_swap_amounts_for_operator_tip(10, 20, SOL, "token-mint", metadata)

    async def test_composed_swap_puts_the_capped_tip_last(self):
        signer = Keypair()
        wallet = str(signer.pubkey())
        tip_account = str(Pubkey.new_unique())
        runtime = object.__new__(CustodyRuntime)
        runtime.wallet = wallet
        runtime.ready = lambda: signer

        async def rpc(session, method, params=None):
            self.assertEqual(method, "getLatestBlockhash")
            return {"value": {"blockhash": str(Hash.new_unique())}}

        runtime.rpc = rpc
        data = {
            "computeBudgetInstructions": [],
            "setupInstructions": [],
            "swapInstruction": {
                "programId": str(Pubkey.new_unique()),
                "accounts": [{"pubkey": wallet, "isSigner": True, "isWritable": True}],
                "data": base64.b64encode(b"swap-fixture").decode(),
            },
            "cleanupInstruction": None,
            "otherInstructions": [],
            "addressLookupTableAddresses": [],
        }
        tx = await CustodyRuntime.build_swap_transaction(runtime, None, data, tip_account)
        self.assertEqual(str(tx.message.account_keys[0]), wallet)
        self.assertEqual(tx.message.header.num_required_signatures, 1)
        tip_instruction = tx.message.instructions[-1]
        self.assertEqual(tip_instruction.data[:4], (2).to_bytes(4, "little"))
        self.assertEqual(int.from_bytes(tip_instruction.data[4:12], "little"), JITO_TIP_LAMPORTS)

    async def test_swap_submission_uses_jito_endpoint_and_validates_signature(self):
        expected = str(Keypair().pubkey())
        session = FakeSession(FakeResponse(payload={"jsonrpc": "2.0", "result": expected}))
        runtime = object.__new__(CustodyRuntime)

        result = await CustodyRuntime.send_via_jito(runtime, session, "signed-base64", expected)

        self.assertEqual(result, expected)
        url, request = session.calls[0]
        self.assertEqual(url, JITO_BUNDLE_SEND_URL)
        self.assertEqual(request["json"]["method"], "sendTransaction")
        self.assertEqual(request["json"]["params"], ["signed-base64", {"encoding": "base64"}])

    async def test_swap_submission_rejects_error_or_wrong_signature(self):
        expected = str(Keypair().pubkey())
        for response in (
            FakeResponse(payload={"jsonrpc": "2.0", "error": {"message": "rejected"}}),
            FakeResponse(payload={"jsonrpc": "2.0", "result": str(Keypair().pubkey())}),
            FakeResponse(status=503, payload={"error": "unavailable"}),
        ):
            session = FakeSession(response)
            with self.assertRaises(WalletError):
                await CustodyRuntime.send_via_jito(
                    object.__new__(CustodyRuntime), session, "signed-base64", expected)

    async def test_send_reserved_never_falls_back_to_public_rpc_for_swap(self):
        signature = str(Keypair().pubkey())
        ledger = SimpleNamespace(
            prepared=Mock(),
            submitted=Mock(),
            operation=Mock(return_value={"state": "UNKNOWN"}),
        )
        runtime = SimpleNamespace(
            safe_simulation=AsyncMock(return_value="signed-base64"),
            web_access=None,
            ledger=ledger,
            send_via_jito=AsyncMock(side_effect=TimeoutError()),
            rpc=AsyncMock(),
            reconcile=AsyncMock(),
        )
        result = await CustodyRuntime.send_reserved(
            runtime, None, SimpleNamespace(signatures=[signature]),
            {"id": "op1", "kind": "BUY"}, {})

        self.assertEqual(result, {"state": "UNKNOWN"})
        runtime.send_via_jito.assert_awaited_once()
        runtime.rpc.assert_not_awaited()
        ledger.submitted.assert_called_once_with("op1", False)

    async def test_non_swap_operations_still_use_configured_rpc(self):
        signature = str(Keypair().pubkey())
        ledger = SimpleNamespace(
            prepared=Mock(),
            submitted=Mock(),
            operation=Mock(return_value={"state": "SUBMITTED"}),
        )
        runtime = SimpleNamespace(
            safe_simulation=AsyncMock(return_value="signed-base64"),
            web_access=None,
            ledger=ledger,
            send_via_jito=AsyncMock(),
            rpc=AsyncMock(return_value=signature),
            reconcile=AsyncMock(),
        )
        await CustodyRuntime.send_reserved(
            runtime, None, SimpleNamespace(signatures=[signature]),
            {"id": "op2", "kind": "WITHDRAW"}, {})

        runtime.send_via_jito.assert_not_awaited()
        runtime.rpc.assert_awaited_once()
        ledger.submitted.assert_called_once_with("op2", True)


if __name__ == "__main__":
    unittest.main()
