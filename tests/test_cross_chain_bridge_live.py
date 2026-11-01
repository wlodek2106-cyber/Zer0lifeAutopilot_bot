import base64
import os
import json
import tempfile
import time
import uuid
import unittest
from unittest.mock import patch

from eth_account import Account
from solders.address_lookup_table_account import AddressLookupTableAccount
from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.transaction import VersionedTransaction

from custody_ledger import CustodyLedger, LedgerError, available
from cross_chain_bridge import (
    BASE_CHAIN_ID,
    BASE_USDC_ADDRESS,
    SOLANA_CHAIN_ID,
    SOLANA_LOOKUP_TABLE_PROGRAM,
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


def make_lookup_table_transaction(extra_transfer=False):
    wallet = Keypair()
    mint = Pubkey.from_string(SOLANA_USDC)
    source = Pubkey.from_string(_ata(str(wallet.pubkey())))
    destination = Pubkey.new_unique()
    raw_amount = 5_000_000
    transfer_data = bytes([12]) + raw_amount.to_bytes(8, "little") + bytes([6])
    transfer = Instruction(
        Pubkey.from_string(TOKEN_PROGRAM_ID), transfer_data,
        [AccountMeta(source, False, True), AccountMeta(mint, False, False),
         AccountMeta(destination, False, True), AccountMeta(wallet.pubkey(), True, False)])
    instructions = [transfer]
    addresses = [source, mint, destination]
    if extra_transfer:
        extra_destination = Pubkey.new_unique()
        extra_data = bytes([12]) + (1).to_bytes(8, "little") + bytes([6])
        instructions.append(Instruction(
            Pubkey.from_string(TOKEN_PROGRAM_ID), extra_data,
            [AccountMeta(source, False, True), AccountMeta(mint, False, False),
             AccountMeta(extra_destination, False, True), AccountMeta(wallet.pubkey(), True, False)]))
        addresses.append(extra_destination)
    intent = Instruction(Pubkey.from_string(SOLANA_LIFI_TRACKING_PROGRAM), b"\x01", [])
    instructions.append(intent)
    table_key = Pubkey.new_unique()
    table = AddressLookupTableAccount(table_key, addresses)
    message = MessageV0.try_compile(wallet.pubkey(), instructions, [table], Hash.default())
    tx = VersionedTransaction.populate(message, [Signature.default()])
    raw_table = bytearray(56)
    raw_table[:4] = (1).to_bytes(4, "little")
    raw_table[4:12] = ((1 << 64) - 1).to_bytes(8, "little")
    raw_table[12:20] = (1).to_bytes(8, "little")
    raw_table[20] = 0
    raw_table[21] = 0
    raw_table.extend(b"".join(bytes(address) for address in table.addresses))
    return (base64.b64encode(bytes(tx)).decode("ascii"), str(wallet.pubkey()),
            base64.b64encode(bytes(raw_table)).decode("ascii"), str(table_key))


class LiveBridgePrimitiveTests(unittest.TestCase):
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
        encoded, wallet, table_data, table_key = make_lookup_table_transaction()

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
            tx = validate_solana_transaction(
                encoded, 0, wallet, 5_000_000, lookup_table_accounts=tables)
            self.assertEqual(len(tx.message.address_table_lookups), 1)
            with self.assertRaisesRegex(BridgeError, "LOOKUP_TABLE_DATA_MISSING"):
                validate_solana_transaction(encoded, 0, wallet, 5_000_000)

    def test_solana_lookup_table_must_be_live_and_owned_by_lookup_program(self):
        encoded, _wallet, table_data, table_key = make_lookup_table_transaction()

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
        encoded, wallet, table_data, table_key = make_lookup_table_transaction(extra_transfer=True)

        async def rpc(_session, _method, _params):
            return {"context": {"slot": 10}, "value": [{
                "owner": str(SOLANA_LOOKUP_TABLE_PROGRAM), "data": [table_data, "base64"],
            }]}

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "ledger.sqlite")
            bridge = CrossChainBridge(path, EmptyLedger(path), SOLANA_WALLET, solana_rpc=rpc)
            tables = __import__("asyncio").run(bridge.resolve_solana_lookup_tables(None, encoded))
            with self.assertRaisesRegex(BridgeError, "USDC_TRANSFER_COUNT_2"):
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
            self.assertEqual(session.calls[0][1]["params"]["toAddress"], SOLANA_WALLET)
            self.assertEqual(session.calls[0][1]["params"]["fromAddress"].lower(), BASE_WALLET.lower())

    def test_bridge_lock_blocks_new_wallet_operations(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = CustodyLedger(os.path.join(tmp, "ledger.sqlite"))
            with ledger.db() as db:
                db.execute("""CREATE TABLE custody_bridge_transfers
                    (id TEXT PRIMARY KEY, state TEXT NOT NULL)""")
                db.execute("INSERT INTO custody_bridge_transfers VALUES('pending','UNKNOWN')")
            with self.assertRaisesRegex(LedgerError, "bridge transfer"):
                ledger.reserve(77, "request-1", "BUY", SOLANA_USDC, 1,
                               {"output_asset": "mint"})

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
            with self.assertRaisesRegex(LedgerError, "bridge transfer"):
                ledger.reserve(88, "wallet-op", "BUY", SOLANA_USDC, 1,
                               {"output_asset": "another-mint"})
            with self.assertRaises(BridgeError):
                bridge.transfer_for_user(88, transfer["id"])


if __name__ == "__main__":
    unittest.main()
