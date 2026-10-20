import base64
import json
import time
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from solders.hash import Hash
from solders.instruction import Instruction
from solders.keypair import Keypair
from solders.message import MessageV0, to_bytes_versioned
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

from custody_runtime import (
    CustodyRuntime,
    JITO_BUNDLE_SEND_URL,
    JITO_TIP_FLOOR_URL,
    JITO_TIP_MIN_LAMPORTS,
    JITO_TIP_MAX_LAMPORTS,
    JITO_TIP_MAX_AGE_SECONDS,
    JUPITER_PRIORITY_FEE_LAMPORTS,
    SOL,
    USDC,
    WalletError,
    adjust_swap_amounts_for_operator_tip,
    jupiter_swap_instructions_payload,
)


class FakeResponse:
    def __init__(self, status=200, payload=None):
        self.status = status
        self.payload = {} if payload is None else payload

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

    def get(self, url, **kwargs):
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
            "jito_tip_lamports": JITO_TIP_MIN_LAMPORTS,
        }
        spent, received = adjust_swap_amounts_for_operator_tip(
            1_000_000 + JITO_TIP_MIN_LAMPORTS, 0, SOL, "token-mint", metadata)
        self.assertEqual((spent, received), (1_000_000, 0))

        spent, received = adjust_swap_amounts_for_operator_tip(
            0, 2_000_000 - JITO_TIP_MIN_LAMPORTS, "token-mint", SOL, metadata)
        self.assertEqual((spent, received), (0, 2_000_000))

    def test_tip_metadata_cannot_exceed_or_bypass_approved_cap(self):
        for metadata in (
            {"execution_route": "jito_bundle_only", "jito_tip_lamports": JITO_TIP_MAX_LAMPORTS + 1},
            {"execution_route": "jito_bundle_only", "jito_tip_lamports": JITO_TIP_MIN_LAMPORTS - 1},
            {"jito_tip_lamports": JITO_TIP_MIN_LAMPORTS},
            {"execution_route": "jito_bundle_only", "jito_tip_lamports": 0},
        ):
            with self.assertRaises(WalletError):
                adjust_swap_amounts_for_operator_tip(10, 20, SOL, "token-mint", metadata)

    async def test_tip_floor_uses_fresh_95th_percentile_and_clamps_to_approved_range(self):
        runtime = object.__new__(CustodyRuntime)
        observed = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        session = FakeSession(FakeResponse(payload=[{
            "time": observed,
            "landed_tips_95th_percentile": 0.0001077384,
        }]))
        quote = await CustodyRuntime.get_jito_tip_quote(runtime, session)
        self.assertEqual(quote, {
            "lamports": 107_739, "source": "recent_95th_percentile"})
        self.assertEqual(session.calls[0][0], JITO_TIP_FLOOR_URL)

        for percentile, expected in (
            (0.00001, JITO_TIP_MIN_LAMPORTS),
            (0.0002, JITO_TIP_MAX_LAMPORTS),
        ):
            runtime = object.__new__(CustodyRuntime)
            session = FakeSession(FakeResponse(payload=[{
                "time": observed,
                "landed_tips_95th_percentile": percentile,
            }]))
            self.assertEqual(
                (await CustodyRuntime.get_jito_tip_quote(runtime, session))["lamports"], expected)

    async def test_tip_floor_rejects_stale_stats_without_fallback(self):
        runtime = object.__new__(CustodyRuntime)
        stale = (datetime.now(timezone.utc) - timedelta(seconds=JITO_TIP_MAX_AGE_SECONDS + 1)
                 ).isoformat().replace("+00:00", "Z")
        session = FakeSession(FakeResponse(payload=[{
            "time": stale, "landed_tips_95th_percentile": 0.0001,
        }]))
        with self.assertRaisesRegex(WalletError, "invalid or unavailable"):
            await CustodyRuntime.get_jito_tip_quote(runtime, session)

    async def test_exit_uses_approved_maximum_if_tip_stats_are_unavailable(self):
        runtime = object.__new__(CustodyRuntime)
        session = FakeSession(FakeResponse(status=503))
        quote = await CustodyRuntime.get_jito_tip_quote(
            runtime, session, allow_max_fallback=True)
        self.assertEqual(quote, {
            "lamports": JITO_TIP_MAX_LAMPORTS,
            "source": "approved_max_exit_fallback",
        })

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
        self.assertEqual(
            int.from_bytes(tip_instruction.data[4:12], "little"), JITO_TIP_MIN_LAMPORTS)

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

    async def test_swap_refresh_changes_only_hash_and_keeps_a_valid_signature(self):
        signer = Keypair()
        old_hash, new_hash = Hash.new_unique(), Hash.new_unique()
        message = MessageV0.try_compile(
            signer.pubkey(), [Instruction(Pubkey.new_unique(), b"swap-fixture", [])], [], old_hash)
        tx = VersionedTransaction(message, [signer])
        runtime = object.__new__(CustodyRuntime)
        runtime.wallet = str(signer.pubkey())
        runtime.ready = lambda: signer
        runtime.rpc = AsyncMock(side_effect=[
            {"context": {"slot": 100}, "value": {
                "blockhash": str(new_hash), "lastValidBlockHeight": 250}},
            200,
        ])

        refreshed = await CustodyRuntime.refresh_swap_blockhash(runtime, None, tx)

        self.assertNotEqual(refreshed.message.recent_blockhash, old_hash)
        self.assertEqual(refreshed.message.header, message.header)
        self.assertEqual(refreshed.message.account_keys, message.account_keys)
        self.assertEqual(refreshed.message.instructions, message.instructions)
        self.assertEqual(refreshed.message.address_table_lookups, message.address_table_lookups)
        self.assertTrue(refreshed.signatures[0].verify(
            signer.pubkey(), to_bytes_versioned(refreshed.message)))
        self.assertEqual(runtime.rpc.await_count, 2)

    async def test_swap_refresh_rejects_a_blockhash_near_expiry(self):
        signer = Keypair()
        tx = VersionedTransaction(
            MessageV0.try_compile(signer.pubkey(), [], [], Hash.new_unique()), [signer])
        runtime = object.__new__(CustodyRuntime)
        runtime.wallet = str(signer.pubkey())
        runtime.ready = lambda: signer
        runtime.rpc = AsyncMock(side_effect=[
            {"context": {"slot": 100}, "value": {
                "blockhash": str(Hash.new_unique()), "lastValidBlockHeight": 250}},
            235,
        ])

        with self.assertRaisesRegex(WalletError, "too close to expiry"):
            await CustodyRuntime.refresh_swap_blockhash(runtime, None, tx)

    async def test_send_reserved_never_falls_back_to_public_rpc_for_swap(self):
        signer = Keypair()
        tx = VersionedTransaction(
            MessageV0.try_compile(signer.pubkey(), [], [], Hash.new_unique()), [signer])
        signature = str(tx.signatures[0])
        ledger = SimpleNamespace(
            prepared=Mock(),
            submitted=Mock(),
            operation=Mock(return_value={"state": "UNKNOWN"}),
        )
        runtime = SimpleNamespace(
            safe_simulation=AsyncMock(return_value="signed-base64"),
            authorize_execution_order=Mock(),
            authorize_execution_quote=AsyncMock(),
            refresh_swap_blockhash=AsyncMock(return_value=tx),
            web_access=None,
            ledger=ledger,
            send_via_jito=AsyncMock(side_effect=TimeoutError()),
            rpc=AsyncMock(),
            reconcile=AsyncMock(),
        )
        row = {"id": "op1", "kind": "BUY", "uid": 101, "asset": SOL, "raw": 1,
               "created_at": int(time.time()),
               "metadata": json.dumps({"execution_route": "jito_bundle_only"})}
        with patch("custody_runtime.activity_log.record_jito_stage") as record_stage:
            result = await CustodyRuntime.send_reserved(
                runtime, None, tx, row, {"output_asset": USDC})

        self.assertEqual(result, {"state": "UNKNOWN"})
        runtime.send_via_jito.assert_awaited_once()
        runtime.refresh_swap_blockhash.assert_awaited_once()
        runtime.rpc.assert_not_awaited()
        ledger.submitted.assert_called_once_with("op1", False)
        self.assertEqual(record_stage.call_args.args[2], "jito_submission_unknown")
        self.assertIn("do not resend", record_stage.call_args.args[3].lower())

    async def test_jito_ack_is_logged_as_submission_not_chain_confirmation(self):
        signer = Keypair()
        tx = VersionedTransaction(
            MessageV0.try_compile(signer.pubkey(), [], [], Hash.new_unique()), [signer])
        signature = str(tx.signatures[0])
        ledger = SimpleNamespace(
            prepared=Mock(),
            submitted=Mock(),
            operation=Mock(return_value={"state": "SUBMITTED"}),
        )
        runtime = SimpleNamespace(
            safe_simulation=AsyncMock(return_value="signed-base64"),
            authorize_execution_order=Mock(),
            authorize_execution_quote=AsyncMock(),
            refresh_swap_blockhash=AsyncMock(return_value=tx),
            web_access=None,
            ledger=ledger,
            send_via_jito=AsyncMock(return_value=signature),
            reconcile=AsyncMock(),
        )
        row = {"id": "op-ack", "kind": "BUY", "uid": 101, "asset": SOL, "raw": 1,
               "created_at": int(time.time()),
               "metadata": json.dumps({"execution_route": "jito_bundle_only"})}

        with patch("custody_runtime.activity_log.record_jito_stage") as record_stage:
            result = await CustodyRuntime.send_reserved(
                runtime, None, tx, row, {"output_asset": USDC})

        self.assertEqual(result, {"state": "SUBMITTED"})
        ledger.submitted.assert_called_once_with("op-ack", True)
        record_stage.assert_called_once()
        self.assertEqual(record_stage.call_args.args[2], "jito_submission_acknowledged")
        self.assertIn("not proof of Solana inclusion", record_stage.call_args.args[3])
        runtime.reconcile.assert_awaited_once()

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
