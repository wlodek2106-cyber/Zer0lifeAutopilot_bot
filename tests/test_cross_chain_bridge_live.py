import base64
import asyncio
import os
import json
import tempfile
import time
import uuid
import unittest
from unittest.mock import AsyncMock, Mock, patch

from eth_account import Account
from solders.address_lookup_table_account import AddressLookupTableAccount
from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.transaction import VersionedTransaction

from custody_ledger import (
    CustodyLedger, LedgerError, SOL, FEE_CAP, available, position_account,
)
from custody_runtime import CustodyRuntime, GENESIS
from cross_chain_bridge import (
    BASE_CHAIN_ID,
    BASE_USDC_ADDRESS,
    SOLANA_CHAIN_ID,
    SOLANA_ASSOCIATED_TOKEN_PROGRAM,
    SOLANA_LOOKUP_TABLE_PROGRAM,
    SOLANA_CLOCK_SYSVAR,
    SOLANA_LIFI_FEE_OWNER,
    SOLANA_USDC,
    SOLANA_LIFI_TRACKING_PROGRAM,
    BridgeError,
    CrossChainBridge,
    _ata,
    base_signer_address,
    build_exact_usdc_approval,
    normalize_quote,
    parse_usdc_raw,
    sign_base_transaction,
    validate_solana_transaction,
)
from spl_token_support import TOKEN_PROGRAM_ID

BASE_WALLET = "0x4574D50bB68D0A65F10b62655338A1C8b4315040"
SOLANA_WALLET = "11111111111111111111111111111111"
TEST_KEY = "0x" + "0" * 63 + "1"


class EmptyLedger:
    def __init__(self, path, balance=0):
        self.path = path
        self.available = balance

    def db(self):
        import sqlite3
        from contextlib import contextmanager

        @contextmanager
        def connection():
            db = sqlite3.connect(self.path, timeout=10)
            db.row_factory = sqlite3.Row
            try:
                yield db
                db.commit()
            except Exception:
                db.rollback()
                raise
            finally:
                db.close()
        return connection()

    def balance(self, uid, asset):
        return self.available


class FakeResponse:
    status = 200

    def __init__(self, payload):
        self.payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def json(self):
        return self.payload


class FakeSession:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return FakeResponse(self.payload)


def base_route():
    return {
        "tool": "lifiIntents",
        "action": {
            "fromChainId": BASE_CHAIN_ID, "toChainId": SOLANA_CHAIN_ID,
            "fromAmount": "5000000", "fromAddress": BASE_WALLET,
            "toAddress": SOLANA_WALLET,
            "fromToken": {"address": BASE_USDC_ADDRESS},
            "toToken": {"address": SOLANA_USDC},
        },
        "estimate": {
            "fromAmount": "5000000", "toAmount": "4980000",
            "toAmountMin": "4950000", "approvalAddress": "0x" + "b" * 40,
        },
        "transactionRequest": {
            "chainId": BASE_CHAIN_ID, "from": BASE_WALLET,
            "to": "0x" + "b" * 40, "data": "0x1234",
            "value": "0x0", "gasLimit": "0x5208",
        },
    }


def make_lookup_table_transaction(extra_transfer=False, create_recipient_atas=True,
                                 fee_destination=None, fee_amount=None, tracker_valid=True,
                                 use_lookup_table=True):
    wallet = Keypair()
    mint = Pubkey.from_string(SOLANA_USDC)
    source = Pubkey.from_string(_ata(str(wallet.pubkey())))
    fee_ata = Pubkey.from_string(_ata(SOLANA_LIFI_FEE_OWNER))
    if fee_destination is not None:
        fee_ata = Pubkey.from_string(fee_destination)
    recipient_owners = []
    ata_instructions = []

    def make_recipient():
        owner = Pubkey.new_unique()
        recipient_owners.append(str(owner))
        destination = Pubkey.find_program_address(
            [bytes(owner), bytes(Pubkey.from_string(TOKEN_PROGRAM_ID)), bytes(mint)],
            Pubkey.from_string(SOLANA_ASSOCIATED_TOKEN_PROGRAM),
        )[0]
        if create_recipient_atas:
            ata_instructions.append(Instruction(
                Pubkey.from_string(SOLANA_ASSOCIATED_TOKEN_PROGRAM), b"\x01",
                [AccountMeta(wallet.pubkey(), True, True), AccountMeta(destination, False, True),
                 AccountMeta(owner, False, False), AccountMeta(mint, False, False),
                 AccountMeta(Pubkey.default(), False, False),
                 AccountMeta(Pubkey.from_string(TOKEN_PROGRAM_ID), False, False)]))
        return destination

    raw_amount = 5_000_000
    default_fee = (raw_amount * 25 + 9_999) // 10_000
    actual_fee = default_fee if fee_amount is None else int(fee_amount)
    destination = make_recipient()

    def transfer_instruction(amount, recipient):
        transfer_data = bytes([12]) + int(amount).to_bytes(8, "little") + bytes([6])
        return Instruction(
            Pubkey.from_string(TOKEN_PROGRAM_ID), transfer_data,
            [AccountMeta(source, False, True), AccountMeta(mint, False, False),
             AccountMeta(recipient, False, True), AccountMeta(wallet.pubkey(), True, False)])

    instructions = list(ata_instructions)
    instructions.append(transfer_instruction(actual_fee, fee_ata))
    instructions.append(transfer_instruction(raw_amount - actual_fee, destination))
    addresses = [source, mint, destination, fee_ata]
    transfer_recipients = [str(fee_ata), str(destination)]
    if extra_transfer:
        extra_destination = make_recipient()
        instructions.extend(ata_instructions[-1:])
        instructions.append(transfer_instruction(1, extra_destination))
        addresses.append(extra_destination)
        transfer_recipients.append(str(extra_destination))
    epoch_account = Pubkey.new_unique()
    intent_data = b"\x00" + (1).to_bytes(8, "little") if tracker_valid else b"\x01"
    intent = Instruction(
        Pubkey.from_string(SOLANA_LIFI_TRACKING_PROGRAM), intent_data,
        [AccountMeta(Pubkey.from_string(SOLANA_CLOCK_SYSVAR), False, False),
         AccountMeta(epoch_account, False, False)])
    instructions.append(intent)
    table_key = Pubkey.new_unique()
    table = AddressLookupTableAccount(table_key, addresses)
    message = MessageV0.try_compile(
        wallet.pubkey(), instructions, [table] if use_lookup_table else [], Hash.default())
    tx = VersionedTransaction.populate(message, [Signature.default()])
    raw_table = bytearray(56)
    raw_table[:4] = (1).to_bytes(4, "little")
    raw_table[4:12] = ((1 << 64) - 1).to_bytes(8, "little")
    raw_table[12:20] = (1).to_bytes(8, "little")
    raw_table[20] = 0
    raw_table[21] = 0
    raw_table.extend(b"".join(bytes(address) for address in table.addresses))
    return (base64.b64encode(bytes(tx)).decode("ascii"), str(wallet.pubkey()),
            base64.b64encode(bytes(raw_table)).decode("ascii"), str(table_key),
            transfer_recipients, recipient_owners)


class LiveBridgePrimitiveTests(unittest.TestCase):
    def test_status_lookup_error_is_visible_without_changing_unknown_transfer(self):
        transfer = {
            "id": "pending-transfer",
            "direction": "solana-to-base",
            "state": "UNKNOWN",
            "received_raw": None,
            "source_tx_hash": "test-signature",
        }
        bridge = Mock()
        bridge.transfers_for_user.return_value = [transfer.copy()]
        bridge.approvals_for_user.return_value = []
        service = object.__new__(CustodyRuntime)
        service.uid = lambda _request: 77
        service.accounts = None
        service.cross_chain_bridge = bridge
        service._reconcile_exchange_transfer = AsyncMock(
            side_effect=BridgeError("Bridge status is unavailable.", 503))

        response = asyncio.run(service.exchange_transfers({}, None))
        payload = json.loads(response.text)

        self.assertTrue(payload["success"])
        self.assertEqual(payload["transfers"][0]["state"], "UNKNOWN")
        self.assertEqual(payload["transfers"][0]["status_check_error"],
                         "Bridge status is unavailable.")
        self.assertEqual(transfer["state"], "UNKNOWN")
        service._reconcile_exchange_transfer.assert_awaited_once()

    def test_amount_is_integer_six_decimals_and_capped(self):
        self.assertEqual(parse_usdc_raw("10.000001"), 10_000_001)
        for invalid in ("0", "-1", "1.0000001", "10000.000001", "NaN", "Infinity", True):
            with self.subTest(invalid=invalid), self.assertRaises(BridgeError):
                parse_usdc_raw(invalid)

    def test_signer_must_match_configured_base_wallet(self):
        expected = Account.from_key(TEST_KEY).address
        self.assertEqual(base_signer_address(TEST_KEY), expected.lower())
        signed = sign_base_transaction(
            {"chainId": BASE_CHAIN_ID, "nonce": 0, "gas": 21000, "gasPrice": 1,
             "to": BASE_WALLET, "value": 0, "data": "0x"},
            TEST_KEY, expected)
        self.assertRegex(signed["raw_transaction"], r"^0x[0-9a-fA-F]+$")
        self.assertRegex(signed["transaction_hash"], r"^0x[0-9a-fA-F]{64}$")
        with self.assertRaises(BridgeError):
            sign_base_transaction(
                {"chainId": BASE_CHAIN_ID, "nonce": 0, "gas": 21000, "gasPrice": 1,
                 "to": BASE_WALLET, "value": 0, "data": "0x"},
                TEST_KEY, "0x" + "a" * 40)

    def test_approval_is_exact_amount_and_spender_only(self):
        tx = build_exact_usdc_approval(base_route(), 7, 1_000_000_000)
        self.assertEqual(tx["to"].lower(), BASE_USDC_ADDRESS.lower())
        self.assertEqual(tx["nonce"], 7)
        self.assertEqual(tx["data"][:10], "0x095ea7b3")
        self.assertEqual(tx["data"][-64:], format(5_000_000, "064x"))
        self.assertEqual(tx["data"][34:74].lower(), ("b" * 40).lower())
        self.assertEqual(tx["value"], 0)

    def test_base_route_must_match_both_wallets_chains_assets_and_spender(self):
        route = base_route()
        expected = {
            "from_chain": BASE_CHAIN_ID, "to_chain": SOLANA_CHAIN_ID,
            "amount_raw": 5_000_000, "from_address": BASE_WALLET,
            "to_address": SOLANA_WALLET, "from_token": BASE_USDC_ADDRESS,
            "to_token": SOLANA_USDC,
        }
        self.assertIs(normalize_quote(route, expected), route)
        route["action"]["toAddress"] = "0x" + "c" * 40
        with self.assertRaises(BridgeError):
            normalize_quote(route, expected)

    def test_malformed_solana_payload_is_rejected_before_signing(self):
        with self.assertRaises(BridgeError):
            validate_solana_transaction("not-base64", 0, SOLANA_WALLET, 5_000_000)

    def test_solana_v0_lookup_table_is_resolved_and_transaction_still_fully_validated(self):
        encoded, wallet, table_data, table_key, _recipients, _owners = make_lookup_table_transaction()

        async def rpc(_session, method, params):
            self.assertEqual(method, "getMultipleAccounts")
            self.assertEqual(params[0], [table_key])
            return {"context": {"slot": 10}, "value": [{
                "owner": str(SOLANA_LOOKUP_TABLE_PROGRAM),
                "data": [table_data, "base64"],
            }]}

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "ledger.sqlite")
            bridge = CrossChainBridge(path, EmptyLedger(path), SOLANA_WALLET, solana_rpc=rpc)
            tables = __import__("asyncio").run(bridge.resolve_solana_lookup_tables(None, encoded))
            tx, details = validate_solana_transaction(
                encoded, 0, wallet, 5_000_000, lookup_table_accounts=tables,
                return_details=True)
            self.assertEqual(len(tx.message.address_table_lookups), 1)
            self.assertEqual(details["fee_raw"], 12_500)
            self.assertEqual(details["deposit_address"], _owners[0])
            self.assertEqual(details["deposit_token_account"], _recipients[1])
            with self.assertRaisesRegex(BridgeError, "LOOKUP_TABLE_DATA_MISSING"):
                validate_solana_transaction(encoded, 0, wallet, 5_000_000)

    def test_existing_recipient_ata_is_accepted_only_after_finalized_account_proof(self):
        encoded, wallet, _table_data, _table_key, recipients, owners = make_lookup_table_transaction(
            create_recipient_atas=False, use_lookup_table=False)
        destination = recipients[1]
        returned_account = {
            "owner": TOKEN_PROGRAM_ID,
            "data": {"program": "spl-token", "parsed": {"type": "account", "info": {
                "mint": SOLANA_USDC, "owner": owners[0], "state": "initialized",
            }}},
        }

        async def rpc(_session, method, params):
            self.assertEqual(method, "getMultipleAccounts")
            self.assertEqual(params[0], [destination])
            self.assertEqual(params[1]["commitment"], "finalized")
            return {"context": {"slot": 123}, "value": [returned_account]}

        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
            "LIFI_API_KEY": "test-only-key",
            "BASE_HOT_WALLET_ADDRESS": BASE_WALLET,
        }):
            path = os.path.join(tmp, "ledger.sqlite")
            bridge = CrossChainBridge(
                path, EmptyLedger(path, balance=10_000_000), wallet, solana_rpc=rpc)
            existing_owners = asyncio.run(
                bridge.resolve_solana_existing_atas(None, encoded, []))
            _tx, details = validate_solana_transaction(
                encoded, 0, wallet, 5_000_000, return_details=True,
                existing_associated_token_owners=existing_owners)
            self.assertEqual(existing_owners, {destination: owners[0]})
            self.assertEqual(details["deposit_address"], owners[0])
            route_payload = {
                "tool": "near",
                "action": {
                    "fromChainId": SOLANA_CHAIN_ID, "toChainId": BASE_CHAIN_ID,
                    "fromAmount": "5000000", "fromAddress": wallet,
                    "toAddress": BASE_WALLET,
                    "fromToken": {"address": SOLANA_USDC},
                    "toToken": {"address": BASE_USDC_ADDRESS},
                },
                "estimate": {
                    "fromAmount": "5000000", "toAmount": "4900000",
                    "toAmountMin": "4800000", "feeCosts": [], "gasCosts": [],
                },
                "transactionRequest": {"data": encoded, "svmTransactionVersion": 0},
            }
            quote = asyncio.run(bridge.quote(
                FakeSession(route_payload), 77, "solana-to-base", "5"))
            self.assertEqual(quote["provider_solana_deposit_address"], owners[0])
            self.assertEqual(quote["provider_solana_deposit_token_account"], destination)

    def test_existing_recipient_ata_with_wrong_mint_is_rejected(self):
        encoded, wallet, _table_data, _table_key, recipients, owners = make_lookup_table_transaction(
            create_recipient_atas=False, use_lookup_table=False)
        destination = recipients[1]
        returned_account = {
            "owner": TOKEN_PROGRAM_ID,
            "data": {"parsed": {"type": "account", "info": {
                "mint": "11111111111111111111111111111111",
                "owner": owners[0], "state": "initialized",
            }}},
        }

        async def rpc(_session, _method, _params):
            return {"context": {"slot": 123}, "value": [returned_account]}

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "ledger.sqlite")
            bridge = CrossChainBridge(path, EmptyLedger(path), wallet, solana_rpc=rpc)
            with self.assertRaisesRegex(BridgeError, "not a verified USDC token account"):
                asyncio.run(bridge.resolve_solana_existing_atas(None, encoded, []))

    def test_solana_lookup_table_must_be_live_and_owned_by_lookup_program(self):
        encoded, _wallet, table_data, table_key, _recipients, _owners = make_lookup_table_transaction()

        async def rpc(_session, method, params):
            return {"context": {"slot": 10}, "value": [{
                "owner": str(Pubkey.default()), "data": [table_data, "base64"],
            }]}

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "ledger.sqlite")
            bridge = CrossChainBridge(path, EmptyLedger(path), SOLANA_WALLET, solana_rpc=rpc)
            with self.assertRaisesRegex(BridgeError, "ALT_OWNER_MISMATCH"):
                __import__("asyncio").run(bridge.resolve_solana_lookup_tables(None, encoded))

    def test_solana_transfer_count_error_reports_safe_count(self):
        encoded, wallet, table_data, table_key, recipients, owners = make_lookup_table_transaction(
            extra_transfer=True, create_recipient_atas=True)

        async def rpc(_session, _method, _params):
            return {"context": {"slot": 10}, "value": [{
                "owner": str(SOLANA_LOOKUP_TABLE_PROGRAM), "data": [table_data, "base64"],
            }]}

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "ledger.sqlite")
            bridge = CrossChainBridge(path, EmptyLedger(path), SOLANA_WALLET, solana_rpc=rpc)
            tables = __import__("asyncio").run(bridge.resolve_solana_lookup_tables(None, encoded))
            with self.assertRaises(BridgeError) as error:
                validate_solana_transaction(
                    encoded, 0, wallet, 5_000_000, lookup_table_accounts=tables)
            message = str(error.exception)
            self.assertIn("USDC_TRANSFER_COUNT_3", message)
            self.assertIn(f"0.012500 USDC to {recipients[0]} (ATA owner not created in this transaction)", message)
            self.assertIn(f"4.987500 USDC to {recipients[1]} (ATA owner {owners[0]})", message)
            self.assertIn(f"0.000001 USDC to {recipients[2]} (ATA owner {owners[1]})", message)

    def test_li_fi_solana_fee_and_deposit_must_match_exact_amounts_and_destinations(self):
        cases = (
            ({"fee_destination": str(Pubkey.new_unique())}, "LIFI_FEE_RECIPIENT_MISMATCH"),
            ({"fee_amount": 12_501}, "LIFI_FEE_AMOUNT_MISMATCH"),
            ({"tracker_valid": False}, "LIFI_TRACKING_INSTRUCTION"),
        )
        for options, expected_code in cases:
            encoded, wallet, table_data, table_key, _recipients, _owners = (
                make_lookup_table_transaction(**options))

            async def rpc(_session, _method, _params):
                return {"context": {"slot": 10}, "value": [{
                    "owner": str(SOLANA_LOOKUP_TABLE_PROGRAM), "data": [table_data, "base64"],
                }]}

            with self.subTest(expected_code=expected_code), tempfile.TemporaryDirectory() as tmp:
                path = os.path.join(tmp, "ledger.sqlite")
                bridge = CrossChainBridge(path, EmptyLedger(path), SOLANA_WALLET, solana_rpc=rpc)
                tables = __import__("asyncio").run(bridge.resolve_solana_lookup_tables(None, encoded))
                with self.assertRaisesRegex(BridgeError, expected_code):
                    validate_solana_transaction(
                        encoded, 0, wallet, 5_000_000, lookup_table_accounts=tables)

    def test_destination_credit_requires_explicit_chain_proofs(self):
        with self.assertRaises(BridgeError):
            CrossChainBridge.settle_transfer(
                object(), 1, "transfer", 1, "0x" + "1" * 64)

    def test_missing_base_address_does_not_break_app_startup(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
            "BASE_HOT_WALLET_ADDRESS": "", "LIFI_API_KEY": "",
        }):
            bridge = CrossChainBridge(os.path.join(tmp, "ledger.sqlite"),
                                      EmptyLedger(os.path.join(tmp, "ledger.sqlite")),
                                      SOLANA_WALLET)
            self.assertEqual(bridge.base_wallet, "")

    def test_live_bridge_quote_is_uid_bound_and_never_returns_signed_payload(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
            "BASE_HOT_WALLET_ADDRESS": BASE_WALLET,
            "LIFI_API_KEY": "test-only-key",
        }):
            ledger = EmptyLedger(os.path.join(tmp, "ledger.sqlite"), balance=20_000_000)
            bridge = CrossChainBridge(ledger.path, ledger, SOLANA_WALLET)
            route = base_route()
            route["estimate"]["gasCosts"] = [{"amountUSD": "0.01", "token": {"symbol": "ETH"}}]
            session = FakeSession(route)
            quote = __import__("asyncio").run(
                bridge.quote(session, 77, "base-to-solana", "5"))
            self.assertEqual(quote["amount_out_min"], "4.95")
            self.assertEqual(quote["source_available"], "20")
            self.assertNotIn("transactionRequest", quote)
            self.assertNotIn("0x1234", str(quote))
            self.assertTrue(quote["quote_only"])
            self.assertFalse(quote["transfer_enabled"])
            self.assertEqual(session.calls[0][1]["params"]["toAddress"], SOLANA_WALLET)
            self.assertEqual(session.calls[0][1]["params"]["fromAddress"].lower(), BASE_WALLET.lower())

    def test_solana_to_base_quote_exposes_verified_fee_and_manual_review_addresses(self):
        encoded, wallet, _table, _table_key, recipients, owners = make_lookup_table_transaction(
            use_lookup_table=False)
        payload = {
            "tool": "near",
            "action": {
                "fromChainId": SOLANA_CHAIN_ID, "toChainId": BASE_CHAIN_ID,
                "fromAmount": "5000000", "fromAddress": wallet, "toAddress": BASE_WALLET,
                "fromToken": {"address": SOLANA_USDC},
                "toToken": {"address": BASE_USDC_ADDRESS},
            },
            "estimate": {
                "fromAmount": "5000000", "toAmount": "4900000", "toAmountMin": "4800000",
                "feeCosts": [], "gasCosts": [],
            },
            "transactionRequest": {
                "data": encoded, "svmTransactionVersion": 0,
            },
        }
        expected = {
            "from_chain": SOLANA_CHAIN_ID, "to_chain": BASE_CHAIN_ID,
            "amount_raw": 5_000_000, "from_address": wallet, "to_address": BASE_WALLET,
            "from_token": SOLANA_USDC, "to_token": BASE_USDC_ADDRESS,
        }
        route = normalize_quote(payload, expected)
        self.assertEqual(route["_solana_lifi_bridge_details"]["fee_raw"], 12_500)
        self.assertEqual(route["_solana_lifi_bridge_details"]["deposit_address"], owners[0])
        self.assertEqual(route["_solana_lifi_bridge_details"]["deposit_token_account"], recipients[1])

        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
            "BASE_HOT_WALLET_ADDRESS": BASE_WALLET,
            "LIFI_API_KEY": "test-only-key",
        }):
            path = os.path.join(tmp, "ledger.sqlite")
            bridge = CrossChainBridge(path, EmptyLedger(path, balance=10_000_000), wallet)
            quote = __import__("asyncio").run(
                bridge.quote(FakeSession(payload), 77, "solana-to-base", "5"))
            self.assertTrue(quote["quote_only"])
            self.assertFalse(quote["transfer_enabled"])
            self.assertEqual(quote["destination_address"].lower(), BASE_WALLET.lower())
            self.assertEqual(quote["provider_solana_deposit_address"], owners[0])
            self.assertEqual(quote["provider_solana_deposit_token_account"], recipients[1])
            self.assertEqual(quote["solana_provider_fee_usdc"], "0.0125")

    def test_bridge_lock_blocks_new_wallet_operations(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = CustodyLedger(os.path.join(tmp, "ledger.sqlite"))
            ledger.user(77, "test-user")
            position_id = "owned-solana-position"
            with ledger.db() as db:
                ledger.post(db, "seed-exit-test", "test-only funds", [
                    ("external", "TEST_MINT", -100),
                    (position_account(position_id), "TEST_MINT", 100),
                    ("external", SOL, -FEE_CAP),
                    ("treasury", SOL, FEE_CAP),
                ])
                db.execute("""INSERT INTO custody_positions
                    (id,uid,asset,symbol,decimals,raw,cost_usdc,state,buy_tx,
                     funding_asset,created_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (position_id, 77, "TEST_MINT", "TEST", 6, 100, 5,
                     "OPEN", "buy-signature", SOLANA_USDC, int(time.time())))
            with ledger.db() as db:
                db.execute("""CREATE TABLE custody_bridge_transfers
                    (id TEXT PRIMARY KEY, state TEXT NOT NULL)""")
                db.execute("INSERT INTO custody_bridge_transfers VALUES('pending','UNKNOWN')")
                db.execute("""CREATE TABLE custody_bridge_approvals
                    (id TEXT PRIMARY KEY, state TEXT NOT NULL)""")
                db.execute("INSERT INTO custody_bridge_approvals VALUES('pending-approval','SUBMITTED')")
            with self.assertRaisesRegex(LedgerError, "bridge transfer"):
                ledger.reserve(77, "request-1", "BUY", SOLANA_USDC, 1,
                               {"output_asset": "mint"})
            sale = ledger.reserve(77, "manual-exit", "SELL", "TEST_MINT", 100,
                                  {"output_asset": SOLANA_USDC, "symbol": "TEST"},
                                  position_id=position_id)
            self.assertEqual(sale["state"], "RESERVED")
            with self.assertRaisesRegex(LedgerError, "предыдущей операции"):
                ledger.reserve(77, "duplicate-exit", "SELL", "TEST_MINT", 100,
                               {"output_asset": SOLANA_USDC, "symbol": "TEST"},
                               position_id=position_id)
            with ledger.db() as db:
                self.assertEqual(db.execute(
                    "SELECT state FROM custody_bridge_transfers WHERE id='pending'"
                ).fetchone()["state"], "UNKNOWN")
                self.assertEqual(db.execute(
                    "SELECT state FROM custody_bridge_approvals WHERE id='pending-approval'"
                ).fetchone()["state"], "SUBMITTED")

    def test_transfer_reserves_only_requesting_users_balance_and_locks_shared_wallet(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
            "BASE_HOT_WALLET_ADDRESS": BASE_WALLET,
            "LIFI_API_KEY": "test-only-key",
        }):
            path = os.path.join(tmp, "ledger.sqlite")
            ledger = CustodyLedger(path)
            ledger.user(77, "test-user")
            ledger.user(88, "other-user")
            with ledger.db() as db:
                ledger.post(db, "seed-bridge-test", "test backing", [
                    ("external", SOLANA_USDC, -10_000_000),
                    (available(77), SOLANA_USDC, 10_000_000),
                ])
            bridge = CrossChainBridge(path, ledger, SOLANA_WALLET)
            quote_id = str(uuid.uuid4())
            now = int(time.time())
            route = {"estimate": {"toAmountMin": "4_950_000"}}
            with ledger.db() as db:
                db.execute("""INSERT INTO custody_bridge_quotes
                    (id,uid,direction,amount_raw,route_json,created_at,expires_at,state)
                    VALUES(?,?,?,?,?,?,?,'QUOTED')""",
                    (quote_id, 77, "solana-to-base", 5_000_000,
                     json.dumps(route), now, now + 45))
            transfer = bridge.reserve_transfer(77, quote_id)
            self.assertEqual(transfer["state"], "RESERVED")
            self.assertEqual(ledger.balance(77, SOLANA_USDC), 5_000_000)
            self.assertEqual(ledger.balance(88, SOLANA_USDC), 0)
            self.assertEqual(ledger.liabilities()[SOLANA_USDC], 10_000_000)
            self.assertEqual(ledger.backed_liabilities()[SOLANA_USDC], 5_000_000)
            runtime = CustodyRuntime.__new__(CustodyRuntime)
            runtime.ledger = ledger

            async def fake_rpc(session, method, params=None):
                return GENESIS

            async def fake_backing(session):
                return {SOLANA_USDC: {"raw": 5_000_000}}

            runtime.rpc = fake_rpc
            runtime.total_backing_balances = fake_backing
            asyncio.run(runtime.solvent(None))
            with self.assertRaisesRegex(LedgerError, "bridge transfer"):
                ledger.reserve(88, "wallet-op", "BUY", SOLANA_USDC, 1,
                               {"output_asset": "another-mint"})
            with self.assertRaises(BridgeError):
                bridge.transfer_for_user(88, transfer["id"])


if __name__ == "__main__":
    unittest.main()
