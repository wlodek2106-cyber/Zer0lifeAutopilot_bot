"""Server-side, UID-bound USDC bridge preparation and settlement primitives."""
import asyncio
import base64
import json
import os
import re
import sqlite3
import time
import uuid
from decimal import Decimal, InvalidOperation

import aiohttp
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction
from custody_ledger import available as user_account
from spl_token_support import TOKEN_PROGRAM_ID

SOLANA_CHAIN_ID = 1151111081099710
BASE_CHAIN_ID = 8453
SOLANA_USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
BASE_USDC_ASSET = "BASE_USDC"
BASE_USDC_ADDRESS = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
BASE_USDC = BASE_USDC_ADDRESS.lower()
LIFI_API = "https://li.quest/v1"
QUOTE_TTL_SECONDS = 45
MAX_QUOTE_AMOUNT_RAW = 10_000_000_000
SOLANA_NEAR_INTENTS_PROGRAM = "3i5JeuZuUxeKtVysUnwQNGerJP2bSMX9fTFfS4Nxe3Br"
SOLANA_ASSOCIATED_TOKEN_PROGRAM = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
SOLANA_COMPUTE_BUDGET_PROGRAM = "ComputeBudget111111111111111111111111111111"


class BridgeError(Exception):
    def __init__(self, message, status=503):
        super().__init__(message)
        self.status = status


def parse_usdc_raw(value):
    try:
        amount = Decimal(str(value))
        scaled = amount * 1_000_000
        if not amount.is_finite() or amount <= 0 or scaled != scaled.to_integral_value():
            raise ValueError()
        raw = int(scaled)
        if raw > MAX_QUOTE_AMOUNT_RAW:
            raise ValueError()
        return raw
    except (InvalidOperation, ValueError, TypeError, OverflowError):
        raise BridgeError("Enter a USDC amount from 0.000001 to 10,000.", 400) from None


def normalize_evm_address(value):
    value = str(value or "").strip()
    if not re.fullmatch(r"0x[0-9a-fA-F]{40}", value):
        raise BridgeError("The Base hot-wallet address is missing or invalid.", 503)
    return value.lower()


def base_signer_address(private_key):
    if not isinstance(private_key, str) or not re.fullmatch(r"(?:0x)?[0-9a-fA-F]{64}", private_key.strip()):
        raise BridgeError("The Base signing key is not configured in the server secret store.", 503)
    try:
        from eth_account import Account
        return normalize_evm_address(Account.from_key(private_key.strip()).address)
    except Exception:
        raise BridgeError("The Base signing key could not be validated.", 503) from None


def sign_base_transaction(transaction, private_key, expected_wallet):
    if base_signer_address(private_key) != normalize_evm_address(expected_wallet):
        raise BridgeError("The Base signing key does not match the configured wallet.", 503)
    try:
        from eth_account import Account
        from eth_utils import to_checksum_address
        if (int(transaction.get("chainId", 0)) != BASE_CHAIN_ID
                or transaction.get("value", 0) != 0
                or not re.fullmatch(r"0x(?:[0-9a-fA-F]{2})*", str(transaction.get("data", "")))
                or type(transaction.get("gas")) is not int
                or type(transaction.get("gasPrice")) is not int
                or type(transaction.get("nonce")) is not int):
            raise ValueError()
        safe_transaction = dict(transaction)
        safe_transaction["to"] = to_checksum_address(normalize_evm_address(transaction.get("to", "")))
        signed = Account.sign_transaction(safe_transaction, private_key.strip())
        raw = signed.raw_transaction.hex()
        tx_hash = signed.hash.hex()
        return {
            "raw_transaction": raw if raw.startswith("0x") else "0x" + raw,
            "transaction_hash": tx_hash if tx_hash.startswith("0x") else "0x" + tx_hash,
        }
    except Exception:
        raise BridgeError("Could not sign the validated Base transaction. Nothing was broadcast.", 422) from None


def build_exact_usdc_approval(route, nonce, gas_price_wei, gas_limit=65_000):
    """Build a one-time, exact-amount approval for the spender in the checked route."""
    try:
        action, estimate, tx = route["action"], route["estimate"], route["transactionRequest"]
        spender = normalize_evm_address(estimate["approvalAddress"])
        amount = int(action["fromAmount"])
        if (int(action["fromChainId"]) != BASE_CHAIN_ID
                or str(action["fromToken"]["address"]).lower() != BASE_USDC
                or amount <= 0 or int(estimate["fromAmount"]) != amount
                or int(tx["chainId"]) != BASE_CHAIN_ID
                or str(tx["from"]).lower() != normalize_evm_address(action["fromAddress"])
                or str(tx["to"]).lower() != spender
                or type(nonce) is not int or nonce < 0
                or type(gas_price_wei) is not int or gas_price_wei <= 0
                or type(gas_limit) is not int or not 45_000 <= gas_limit <= 100_000):
            raise ValueError()
        return {"chainId": BASE_CHAIN_ID, "nonce": nonce, "gas": gas_limit,
                "gasPrice": gas_price_wei, "to": BASE_USDC_ADDRESS, "value": 0,
                "data": "0x095ea7b3" + spender[2:].rjust(64, "0") + format(amount, "064x")}
    except (KeyError, TypeError, ValueError):
        raise BridgeError("Could not build an exact USDC approval.", 422) from None


def _ata(wallet):
    wallet_key = Pubkey.from_string(str(wallet))
    mint_key = Pubkey.from_string(SOLANA_USDC)
    token_program = Pubkey.from_string(TOKEN_PROGRAM_ID)
    associated_program = Pubkey.from_string(SOLANA_ASSOCIATED_TOKEN_PROGRAM)
    return str(Pubkey.find_program_address(
        [bytes(wallet_key), bytes(token_program), bytes(mint_key)], associated_program)[0])


def validate_solana_transaction(encoded, version, wallet, amount_raw):
    """Fail closed unless the unsigned payload is a narrow USDC bridge transaction."""
    try:
        if type(version) is not int or version not in (0, 1):
            raise ValueError()
        tx = VersionedTransaction.from_bytes(base64.b64decode(encoded, validate=True))
        msg = tx.message
        keys = [str(key) for key in msg.account_keys]
        if (msg.header.num_required_signatures != 1 or not keys or keys[0] != str(wallet)
                or len(tx.signatures) != 1 or bytes(tx.signatures[0]) != bytes(64)
                or getattr(msg, "address_table_lookups", [])):
            raise ValueError()
        allowed = {TOKEN_PROGRAM_ID, SOLANA_NEAR_INTENTS_PROGRAM,
                   SOLANA_ASSOCIATED_TOKEN_PROGRAM, SOLANA_COMPUTE_BUDGET_PROGRAM}
        total, transfers, intents = 0, 0, 0
        if len(msg.instructions) > 30 or len(keys) > 60:
            raise ValueError()
        for ix in msg.instructions:
            if ix.program_id_index >= len(keys):
                raise ValueError()
            program, accounts, data = keys[ix.program_id_index], [keys[i] for i in ix.accounts], bytes(ix.data)
            if program not in allowed or not data:
                raise ValueError()
            if program == TOKEN_PROGRAM_ID:
                if len(data) != 10 or data[0] != 12 or len(accounts) != 4:
                    raise ValueError()
                amount = int.from_bytes(data[1:9], "little")
                if (accounts[0] != _ata(wallet) or accounts[1] != SOLANA_USDC
                        or accounts[3] != str(wallet) or data[9] != 6 or amount <= 0):
                    raise ValueError()
                transfers += 1
                total += amount
            elif program == SOLANA_ASSOCIATED_TOKEN_PROGRAM:
                if data not in (b"\x00", b"\x01") or len(accounts) != 6:
                    raise ValueError()
                owner, mint, token_program = accounts[2], accounts[3], accounts[5]
                if mint != SOLANA_USDC or token_program != TOKEN_PROGRAM_ID:
                    raise ValueError()
                derived = Pubkey.find_program_address(
                    [bytes(Pubkey.from_string(owner)), bytes(Pubkey.from_string(token_program)),
                     bytes(Pubkey.from_string(mint))],
                    Pubkey.from_string(SOLANA_ASSOCIATED_TOKEN_PROGRAM))[0]
                if accounts[1] != str(derived):
                    raise ValueError()
            elif program == SOLANA_COMPUTE_BUDGET_PROGRAM:
                if data[0] == 2 and len(data) == 5:
                    if not 10_000 <= int.from_bytes(data[1:5], "little") <= 1_400_000:
                        raise ValueError()
                elif data[0] == 3 and len(data) == 9:
                    if int.from_bytes(data[1:9], "little") > 2_000_000:
                        raise ValueError()
                else:
                    raise ValueError()
            elif program == SOLANA_NEAR_INTENTS_PROGRAM:
                intents += 1
        if intents != 1 or transfers != 1 or total != int(amount_raw):
            raise ValueError()
        return tx
    except Exception as exc:
        raise BridgeError("The Solana transaction failed USDC signer checks; nothing was sent.", 422) from exc


def normalize_quote(payload, expected):
    try:
        action, estimate, tx = payload["action"], payload["estimate"], payload["transactionRequest"]
        if (int(action["fromChainId"]) != expected["from_chain"]
                or int(action["toChainId"]) != expected["to_chain"]
                or int(action["fromAmount"]) != expected["amount_raw"]
                or int(estimate["fromAmount"]) != expected["amount_raw"]
                or int(estimate["toAmountMin"]) <= 0
                or int(estimate["toAmount"]) < int(estimate["toAmountMin"])
                or int(estimate["toAmount"]) > expected["amount_raw"] * 2
                or int(estimate["toAmountMin"]) > expected["amount_raw"] * 2
                or str(action["fromAddress"]).lower() != expected["from_address"].lower()
                or str(action["toAddress"]).lower() != expected["to_address"].lower()
                or str(action["fromToken"]["address"]).lower() != expected["from_token"].lower()
                or str(action["toToken"]["address"]).lower() != expected["to_token"].lower()
                or not isinstance(tx, dict)):
            raise ValueError()
        if expected["from_chain"] == BASE_CHAIN_ID:
            spender = str(estimate.get("approvalAddress") or "").lower()
            if (payload.get("tool") != "lifiIntents"
                    or int(tx["chainId"]) != BASE_CHAIN_ID
                    or str(tx["from"]).lower() != expected["from_address"].lower()
                    or str(tx["to"]).lower() != spender
                    or not re.fullmatch(r"0x[0-9a-fA-F]{40}", str(tx["to"]))
                    or not re.fullmatch(r"0x(?:[0-9a-fA-F]{2})*", str(tx["data"]))
                    or len(tx["data"]) > 32_000
                    or int(str(tx.get("value", "0x0")), 16) != 0
                    or not 21_000 <= int(str(tx["gasLimit"]), 16) <= 1_500_000):
                raise ValueError()
        else:
            if payload.get("tool") != "near":
                raise ValueError()
            validate_solana_transaction(tx["data"], tx["svmTransactionVersion"],
                                        expected["from_address"], expected["amount_raw"])
        return payload
    except (KeyError, TypeError, ValueError, InvalidOperation):
        raise BridgeError("The provider quote failed chain, wallet, token, amount, or transaction checks.", 502) from None


class CrossChainBridge:
    def __init__(self, db_file, ledger, solana_wallet):
        self.db_file, self.ledger = str(db_file), ledger
        self.solana_wallet = str(solana_wallet)
        try:
            self.base_wallet = normalize_evm_address(os.getenv("BASE_HOT_WALLET_ADDRESS", ""))
        except BridgeError:
            self.base_wallet = ""
        self.api_key = os.getenv("LIFI_API_KEY", "").strip()
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_file, timeout=10) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS custody_bridge_quotes (
                    id TEXT PRIMARY KEY, uid INTEGER NOT NULL, direction TEXT NOT NULL,
                    amount_raw INTEGER NOT NULL, route_json TEXT NOT NULL,
                    created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL,
                    state TEXT NOT NULL DEFAULT 'QUOTED');
                CREATE INDEX IF NOT EXISTS custody_bridge_quotes_user ON custody_bridge_quotes(uid,created_at);
                CREATE TABLE IF NOT EXISTS custody_bridge_quote_attempts_v2 (
                    id TEXT PRIMARY KEY, uid INTEGER NOT NULL, created_at INTEGER NOT NULL);
                CREATE INDEX IF NOT EXISTS custody_bridge_quote_attempts_v2_time
                    ON custody_bridge_quote_attempts_v2(uid,created_at);
                CREATE TABLE IF NOT EXISTS custody_bridge_transfers (
                    id TEXT PRIMARY KEY, uid INTEGER NOT NULL, quote_id TEXT NOT NULL UNIQUE,
                    direction TEXT NOT NULL, source_asset TEXT NOT NULL, destination_asset TEXT NOT NULL,
                    amount_raw INTEGER NOT NULL, minimum_received_raw INTEGER NOT NULL, received_raw INTEGER,
                    state TEXT NOT NULL, source_tx_hash TEXT, signed_transaction TEXT,
                    destination_tx_hash TEXT, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
                CREATE INDEX IF NOT EXISTS custody_bridge_transfers_active
                    ON custody_bridge_transfers(state,created_at);
                CREATE TABLE IF NOT EXISTS custody_bridge_approvals (
                    id TEXT PRIMARY KEY, uid INTEGER NOT NULL, quote_id TEXT NOT NULL UNIQUE,
                    amount_raw INTEGER NOT NULL, spender TEXT NOT NULL, state TEXT NOT NULL,
                    tx_hash TEXT, signed_transaction TEXT, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
            """)

    def source_asset(self, direction):
        if direction == "solana-to-base":
            return SOLANA_USDC
        if direction == "base-to-solana":
            return BASE_USDC_ASSET
        raise BridgeError("Choose a bridge direction.", 400)

    def reserve_quote_attempt(self, uid, now):
        with sqlite3.connect(self.db_file, timeout=10) as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM custody_bridge_quote_attempts_v2 WHERE created_at<?", (now - 3600,))
            last = db.execute("SELECT MAX(created_at) FROM custody_bridge_quote_attempts_v2 WHERE uid=?",
                              (int(uid),)).fetchone()[0]
            count = db.execute("SELECT COUNT(*) FROM custody_bridge_quote_attempts_v2").fetchone()[0]
            if last and now - int(last) < 3:
                raise BridgeError("Wait a few seconds before requesting another quote.", 429)
            if count >= 500:
                raise BridgeError("The temporary quote limit has been reached.", 429)
            db.execute("INSERT INTO custody_bridge_quote_attempts_v2 VALUES(?,?,?)",
                       (str(uuid.uuid4()), int(uid), int(now)))

    async def quote(self, session, uid, direction, amount):
        if not self.api_key:
            raise BridgeError("LI.FI API key is not configured.", 503)
        if not self.base_wallet:
            raise BridgeError("The Base hot-wallet address is not configured.", 503)
        if direction not in ("solana-to-base", "base-to-solana"):
            raise BridgeError("Choose a valid bridge direction.", 400)
        raw = parse_usdc_raw(amount)
        available_raw = int(self.ledger.balance(uid, self.source_asset(direction)))
        if available_raw < raw:
            raise BridgeError("Insufficient available USDC in this network.", 409)
        from_chain, to_chain = ((SOLANA_CHAIN_ID, BASE_CHAIN_ID) if direction == "solana-to-base"
                                else (BASE_CHAIN_ID, SOLANA_CHAIN_ID))
        from_token, to_token = ((SOLANA_USDC, BASE_USDC_ADDRESS) if direction == "solana-to-base"
                                else (BASE_USDC_ADDRESS, SOLANA_USDC))
        from_address, to_address = ((self.solana_wallet, self.base_wallet) if direction == "solana-to-base"
                                    else (self.base_wallet, self.solana_wallet))
        now = int(time.time())
        self.reserve_quote_attempt(uid, now)
        params = {"fromChain": str(from_chain), "toChain": str(to_chain),
                  "fromToken": from_token, "toToken": to_token, "fromAmount": str(raw),
                  "fromAddress": from_address, "toAddress": to_address,
                  "order": "CHEAPEST", "slippage": "0.005"}
        try:
            async with session.get(LIFI_API + "/quote", params=params,
                                   headers={"x-lifi-api-key": self.api_key, "accept": "application/json"}) as res:
                if res.status == 429:
                    raise BridgeError("LI.FI is rate-limiting quotes. Try again shortly.", 429)
                if res.status != 200:
                    raise BridgeError("The quote provider is unavailable; no transfer was sent.", 503)
                payload = await res.json()
        except BridgeError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            raise BridgeError("The quote provider is unavailable; no transfer was sent.", 503) from None
        route = normalize_quote(payload, {"from_chain": from_chain, "to_chain": to_chain,
            "amount_raw": raw, "from_address": from_address, "to_address": to_address,
            "from_token": from_token, "to_token": to_token})
        qid, expires = str(uuid.uuid4()), now + QUOTE_TTL_SECONDS
        with sqlite3.connect(self.db_file, timeout=10) as db:
            db.execute("""INSERT INTO custody_bridge_quotes
                (id,uid,direction,amount_raw,route_json,created_at,expires_at)
                VALUES(?,?,?,?,?,?,?)""",
                (qid, int(uid), direction, raw, json.dumps(route, separators=(",", ":")), now, expires))
        estimate, action = payload["estimate"], payload["action"]
        fees = []
        for kind, key in (("provider", "feeCosts"), ("network", "gasCosts")):
            for fee in (estimate.get(key) or [])[:12]:
                if isinstance(fee, dict):
                    fees.append({"kind": kind, "name": str(fee.get("name") or kind)[:80],
                        "amount_usd": str(fee.get("amountUSD", "0")),
                        "symbol": str((fee.get("token") or {}).get("symbol") or "unknown")[:16],
                        "included": fee.get("included") is True})
        return {
            "quote_id": qid, "direction": direction,
            "amount_in": str(Decimal(raw) / 1_000_000),
            "amount_out": str(Decimal(estimate["toAmount"]) / 1_000_000),
            "amount_out_min": str(Decimal(estimate["toAmountMin"]) / 1_000_000),
            "destination_chain": "Base" if to_chain == BASE_CHAIN_ID else "Solana",
            "provider": str(payload.get("tool") or "Bridge")[:48], "provider_fees": fees,
            "gas_estimate_usd": str(estimate.get("gasCosts", [{}])[0].get("amountUSD", "0")),
            "source_available": str(Decimal(available_raw) / 1_000_000),
            "sufficient_source_balance": True,
            "requires_approval": direction == "base-to-solana",
            "created_at": now, "expires_at": expires, "live_submission_enabled": False,
        }

    def quote_for_user(self, uid, quote_id):
        with sqlite3.connect(self.db_file, timeout=10) as db:
            db.row_factory = sqlite3.Row
            row = db.execute("SELECT * FROM custody_bridge_quotes WHERE id=? AND uid=?",
                             (str(quote_id), int(uid))).fetchone()
        if not row or row["state"] != "QUOTED" or int(row["expires_at"]) < int(time.time()):
            raise BridgeError("The quote expired or was already used. Request a new quote.", 409)
        return dict(row)

    def reserve_transfer(self, uid, quote_id):
        now, tid = int(time.time()), str(uuid.uuid4())
        with self.ledger.db() as db:
            quote = db.execute("SELECT * FROM custody_bridge_quotes WHERE id=? AND uid=?",
                               (str(quote_id), int(uid))).fetchone()
            if not quote or quote["state"] != "QUOTED" or int(quote["expires_at"]) < now:
                raise BridgeError("The quote expired or was already used.", 409)
            if db.execute("SELECT 1 FROM custody_operations WHERE state IN ('RESERVED','PREPARED','SUBMITTED','UNKNOWN') LIMIT 1").fetchone():
                raise BridgeError("The shared wallet is resolving another operation.", 409)
            if db.execute("SELECT 1 FROM custody_bridge_transfers WHERE state IN ('RESERVED','SIGNED','SUBMITTED','UNKNOWN','NEEDS_REVIEW') LIMIT 1").fetchone():
                raise BridgeError("Another bridge transfer is unresolved.", 409)
            if db.execute("SELECT 1 FROM custody_bridge_approvals WHERE state IN ('RESERVED','SIGNED','SUBMITTED','UNKNOWN') LIMIT 1").fetchone():
                raise BridgeError("A Base approval is unresolved; check it before transferring.", 409)
            direction = quote["direction"]
            source = self.source_asset(direction)
            destination = BASE_USDC_ASSET if direction == "solana-to-base" else SOLANA_USDC
            route = json.loads(quote["route_json"])
            amount, minimum = int(quote["amount_raw"]), int(route["estimate"]["toAmountMin"])
            available_row = db.execute("SELECT raw FROM custody_accounts WHERE account=? AND asset=?",
                                       (user_account(uid), source)).fetchone()
            if not available_row or int(available_row["raw"]) < amount:
                raise BridgeError("The available balance changed. Request a new quote.", 409)
            hold = "bridge-operation:" + tid
            self.ledger.post(db, "bridge-reserve:" + tid, "Reserve bridge source funds",
                             [(user_account(uid), source, -amount), (hold, source, amount)])
            db.execute("""INSERT INTO custody_bridge_transfers
                (id,uid,quote_id,direction,source_asset,destination_asset,amount_raw,
                 minimum_received_raw,state,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,'RESERVED',?,?)""",
                (tid, int(uid), str(quote_id), direction, source, destination, amount, minimum, now, now))
            db.execute("UPDATE custody_bridge_quotes SET state='USED' WHERE id=?", (str(quote_id),))
        return self.transfer_for_user(uid, tid)

    def transfer_for_user(self, uid, transfer_id):
        with sqlite3.connect(self.db_file, timeout=10) as db:
            db.row_factory = sqlite3.Row
            row = db.execute("""SELECT id,uid,direction,source_asset,destination_asset,amount_raw,
                minimum_received_raw,received_raw,state,source_tx_hash,destination_tx_hash,
                created_at,updated_at FROM custody_bridge_transfers WHERE id=? AND uid=?""",
                (str(transfer_id), int(uid))).fetchone()
        if not row:
            raise BridgeError("The bridge transfer was not found.", 404)
        return dict(row)

    def save_signed_transfer(self, uid, tid, tx_hash, signed_wire):
        now = int(time.time())
        is_evm = self.transfer_for_user(uid, tid)["direction"] == "base-to-solana"
        valid_hash = (re.fullmatch(r"0x[0-9a-fA-F]{64}", str(tx_hash)) if is_evm
                      else re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{64,90}", str(tx_hash)))
        if not valid_hash or not isinstance(signed_wire, str) or not signed_wire:
            raise BridgeError("The signed transaction did not pass storage checks.", 422)
        with self.ledger.db() as db:
            changed = db.execute("""UPDATE custody_bridge_transfers SET state='SIGNED',
                source_tx_hash=?,signed_transaction=?,updated_at=? WHERE id=? AND uid=? AND state='RESERVED'""",
                (str(tx_hash), str(signed_wire), now, str(tid), int(uid))).rowcount
            if not changed:
                raise BridgeError("Transfer state changed; do not send again.", 409)
        return self.transfer_for_user(uid, tid)

    def mark_transfer_submitted(self, uid, tid, acknowledged):
        state = "SUBMITTED" if acknowledged else "UNKNOWN"
        with self.ledger.db() as db:
            changed = db.execute("""UPDATE custody_bridge_transfers SET state=?,updated_at=?
                WHERE id=? AND uid=? AND state='SIGNED'""",
                (state, int(time.time()), str(tid), int(uid))).rowcount
            if not changed:
                raise BridgeError("Transfer state changed; do not resend.", 409)
        return self.transfer_for_user(uid, tid)

    def route_for_transfer(self, uid, tid):
        with sqlite3.connect(self.db_file, timeout=10) as db:
            row = db.execute("""SELECT q.route_json FROM custody_bridge_transfers t
                JOIN custody_bridge_quotes q ON q.id=t.quote_id WHERE t.id=? AND t.uid=?""",
                (str(tid), int(uid))).fetchone()
        if not row:
            raise BridgeError("The bridge transfer was not found.", 404)
        return json.loads(row[0])

    async def base_rpc(self, session, method, params=None):
        url = os.getenv("BASE_RPC_URL", "https://mainnet.base.org").strip()
        if not url.startswith("https://"):
            raise BridgeError("Base RPC must use HTTPS.", 503)
        try:
            async with session.post(url, json={"jsonrpc":"2.0","id":1,
                                               "method":method,"params":params or []}) as res:
                if res.status != 200:
                    raise BridgeError("Base RPC is unavailable.", 503)
                payload = await res.json()
            if "error" in payload or "result" not in payload:
                raise BridgeError("Base RPC returned an invalid response.", 503)
            return payload["result"]
        except BridgeError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            raise BridgeError("Base RPC is unavailable.", 503) from None

    async def base_usdc_balance(self, session):
        data = "0x70a08231" + self.base_wallet[2:].rjust(64, "0")
        try:
            return int(await self.base_rpc(session, "eth_call", [{"to":BASE_USDC_ADDRESS,"data":data},"finalized"]), 16)
        except (TypeError, ValueError):
            raise BridgeError("Base returned an invalid USDC balance.", 503) from None

    async def base_usdc_allowance(self, session, spender):
        data = "0xdd62ed3e" + self.base_wallet[2:].rjust(64,"0") + normalize_evm_address(spender)[2:].rjust(64,"0")
        return int(await self.base_rpc(session, "eth_call", [{"to":BASE_USDC_ADDRESS,"data":data},"pending"]),16)

    def user_liabilities(self, asset):
        with self.ledger.db() as db:
            return int(db.execute("""SELECT COALESCE(SUM(raw),0) FROM custody_accounts
                WHERE asset=? AND (account LIKE 'user:%' OR account LIKE 'position:%')""",
                (asset,)).fetchone()[0])

    def transfers_for_user(self, uid, limit=20):
        with sqlite3.connect(self.db_file, timeout=10) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute("""SELECT id,direction,amount_raw,minimum_received_raw,received_raw,
                state,source_tx_hash,destination_tx_hash,created_at,updated_at
                FROM custody_bridge_transfers WHERE uid=? ORDER BY created_at DESC LIMIT ?""",
                (int(uid), max(1,min(int(limit),50)))).fetchall()
        return [dict(r) for r in rows]

    def release_unsigned_transfer(self, uid, tid):
        with self.ledger.db() as db:
            row = db.execute("SELECT * FROM custody_bridge_transfers WHERE id=? AND uid=?",
                             (str(tid),int(uid))).fetchone()
            if not row or row["state"] != "RESERVED" or row["source_tx_hash"] or row["signed_transaction"]:
                raise BridgeError("A signed or unresolved transfer cannot be released.",409)
            hold="bridge-operation:"+str(tid)
            self.ledger.post(db,"bridge-abort:"+str(tid),"Return unsigned bridge reservation",
                [(hold,row["source_asset"],-int(row["amount_raw"])),
                 (user_account(uid),row["source_asset"],int(row["amount_raw"]))])
            db.execute("UPDATE custody_bridge_transfers SET state='ABORTED',updated_at=? WHERE id=?",
                       (int(time.time()),str(tid)))
        return self.transfer_for_user(uid,tid)

    def settle_transfer(self, uid, tid, received, destination_hash, *,
                        verified_finality=False, verified_recipient=False, verified_token=False):
        if not (verified_finality and verified_recipient and verified_token):
            raise BridgeError("Destination finality and wallet/token proof are required.", 409)
        if int(received) <= 0:
            raise BridgeError("Verified received amount is invalid.",422)
        with self.ledger.db() as db:
            row=db.execute("SELECT * FROM custody_bridge_transfers WHERE id=? AND uid=?",(str(tid),int(uid))).fetchone()
            if (not row or row["state"] not in ("SUBMITTED","UNKNOWN","NEEDS_REVIEW")
                    or int(received)<int(row["minimum_received_raw"])
                    or int(received)>int(row["amount_raw"])*2):
                raise BridgeError("Transfer does not meet settlement requirements.",409)
            is_evm = row["direction"] == "solana-to-base"
            valid_hash = (re.fullmatch(r"0x[0-9a-fA-F]{64}", str(destination_hash)) if is_evm
                          else re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{64,90}", str(destination_hash)))
            if not valid_hash:
                raise BridgeError("The destination transaction ID is invalid.", 422)
            hold="bridge-operation:"+str(tid)
            self.ledger.post(db,"bridge-settle:"+str(tid),"Settle finalized bridge",
                [(hold,row["source_asset"],-int(row["amount_raw"])),
                 ("external",row["source_asset"],int(row["amount_raw"])),
                 ("external",row["destination_asset"],-int(received)),
                 (user_account(uid),row["destination_asset"],int(received))])
            db.execute("""UPDATE custody_bridge_transfers SET state='COMPLETED',
                received_raw=?,destination_tx_hash=?,signed_transaction=NULL,updated_at=? WHERE id=?""",
                (int(received),str(destination_hash),int(time.time()),str(tid)))
        return self.transfer_for_user(uid,tid)

    async def lifi_status(self, session, transfer):
        source_chain, destination_chain = (
            (SOLANA_CHAIN_ID, BASE_CHAIN_ID) if transfer["direction"] == "solana-to-base"
            else (BASE_CHAIN_ID, SOLANA_CHAIN_ID))
        source_address, destination_address = (
            (self.solana_wallet, self.base_wallet) if transfer["direction"] == "solana-to-base"
            else (self.base_wallet, self.solana_wallet))
        params = {"txHash": transfer["source_tx_hash"], "fromChain": str(source_chain),
                  "toChain": str(destination_chain), "fromAddress": source_address,
                  "toAddress": destination_address}
        headers = {"accept": "application/json"}
        if self.api_key:
            headers["x-lifi-api-key"] = self.api_key
        try:
            async with session.get(LIFI_API + "/status", params=params, headers=headers) as response:
                if response.status == 404:
                    return {"status": "NOT_FOUND"}
                if response.status == 429:
                    raise BridgeError("Bridge status is rate-limited.", 429)
                if response.status != 200:
                    raise BridgeError("Bridge status is unavailable.", 503)
                payload = await response.json()
        except BridgeError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            raise BridgeError("Bridge status is unavailable.", 503) from None
        sending, receiving = payload.get("sending") or {}, payload.get("receiving") or {}
        if (int(sending.get("chainId", 0)) != source_chain
                or int(receiving.get("chainId", 0)) != destination_chain
                or str(payload.get("fromAddress", "")).lower() != source_address.lower()
                or str(payload.get("toAddress", "")).lower() != destination_address.lower()
                or str(sending.get("txHash", "")).lower() != str(transfer["source_tx_hash"]).lower()):
            raise BridgeError("Provider status does not match this transfer; funds remain reserved.", 409)
        return {"status": str(payload.get("status", "")).upper(),
                "substatus": str(payload.get("substatus", payload.get("subStatus", ""))).upper(),
                "sending_hash": str(sending.get("txHash", "")),
                "receiving_hash": str(receiving.get("txHash", ""))}

    def reserve_approval(self, uid, quote_id):
        now, aid = int(time.time()), str(uuid.uuid4())
        with self.ledger.db() as db:
            quote = db.execute("SELECT * FROM custody_bridge_quotes WHERE id=? AND uid=?",
                               (str(quote_id), int(uid))).fetchone()
            if (not quote or quote["direction"] != "base-to-solana"
                    or quote["state"] != "QUOTED" or int(quote["expires_at"]) < now):
                raise BridgeError("The Base quote expired or cannot be approved.", 409)
            if db.execute("SELECT 1 FROM custody_bridge_approvals WHERE quote_id=?",
                          (str(quote_id),)).fetchone():
                raise BridgeError("Approval already exists for this quote; check its status.", 409)
            if db.execute("SELECT 1 FROM custody_bridge_transfers WHERE state IN ('RESERVED','SIGNED','SUBMITTED','UNKNOWN','NEEDS_REVIEW') LIMIT 1").fetchone():
                raise BridgeError("Another bridge transfer is unresolved.", 409)
            if db.execute("SELECT 1 FROM custody_bridge_approvals WHERE state IN ('RESERVED','SIGNED','SUBMITTED','UNKNOWN') LIMIT 1").fetchone():
                raise BridgeError("Another Base approval is unresolved.", 409)
            if db.execute("SELECT 1 FROM custody_operations WHERE state IN ('RESERVED','PREPARED','SUBMITTED','UNKNOWN') LIMIT 1").fetchone():
                raise BridgeError("The shared wallet is resolving another operation.", 409)
            route = json.loads(quote["route_json"])
            spender = normalize_evm_address(route["estimate"]["approvalAddress"])
            db.execute("""INSERT INTO custody_bridge_approvals
                (id,uid,quote_id,amount_raw,spender,state,created_at,updated_at)
                VALUES(?,?,?,?,?,'RESERVED',?,?)""",
                (aid, int(uid), str(quote_id), int(quote["amount_raw"]), spender, now, now))
        return self.approval_for_user(uid, aid)

    def approval_for_user(self, uid, aid):
        with sqlite3.connect(self.db_file, timeout=10) as db:
            db.row_factory = sqlite3.Row
            row = db.execute("""SELECT id,uid,quote_id,amount_raw,spender,state,tx_hash,
                created_at,updated_at FROM custody_bridge_approvals WHERE id=? AND uid=?""",
                (str(aid), int(uid))).fetchone()
        if not row:
            raise BridgeError("Approval record not found.", 404)
        return dict(row)

    def approval_for_quote(self, uid, quote_id):
        with sqlite3.connect(self.db_file, timeout=10) as db:
            db.row_factory = sqlite3.Row
            row = db.execute("""SELECT id,uid,quote_id,amount_raw,spender,state,tx_hash,
                created_at,updated_at FROM custody_bridge_approvals WHERE quote_id=? AND uid=?""",
                (str(quote_id), int(uid))).fetchone()
        return dict(row) if row else None

    def approvals_for_user(self, uid, limit=20):
        with sqlite3.connect(self.db_file, timeout=10) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute("""SELECT id,uid,quote_id,amount_raw,spender,state,tx_hash,
                created_at,updated_at FROM custody_bridge_approvals WHERE uid=?
                ORDER BY created_at DESC LIMIT ?""", (int(uid), max(1, min(int(limit), 50)))).fetchall()
        return [dict(row) for row in rows]

    def save_signed_approval(self, uid, aid, tx_hash, signed_wire):
        if (not re.fullmatch(r"0x[0-9a-fA-F]{64}", str(tx_hash))
                or not isinstance(signed_wire, str) or not signed_wire.startswith("0x")):
            raise BridgeError("Approval transaction ID is invalid.", 422)
        with self.ledger.db() as db:
            changed = db.execute("""UPDATE custody_bridge_approvals SET state='SIGNED',
                tx_hash=?,signed_transaction=?,updated_at=? WHERE id=? AND uid=? AND state='RESERVED'""",
                (str(tx_hash), str(signed_wire), int(time.time()), str(aid), int(uid))).rowcount
            if not changed:
                raise BridgeError("Approval state changed; do not repeat it.", 409)
        return self.approval_for_user(uid, aid)

    def mark_approval_submitted(self, uid, aid, acknowledged):
        state = "SUBMITTED" if acknowledged else "UNKNOWN"
        with self.ledger.db() as db:
            changed = db.execute("""UPDATE custody_bridge_approvals SET state=?,updated_at=?
                WHERE id=? AND uid=? AND state='SIGNED'""",
                (state, int(time.time()), str(aid), int(uid))).rowcount
            if not changed:
                raise BridgeError("Approval state changed; do not resend.", 409)
        return self.approval_for_user(uid, aid)

    def release_unsigned_approval(self, uid, aid):
        with self.ledger.db() as db:
            changed = db.execute("""UPDATE custody_bridge_approvals SET state='ABORTED',updated_at=?
                WHERE id=? AND uid=? AND state='RESERVED' AND tx_hash IS NULL""",
                (int(time.time()), str(aid), int(uid))).rowcount
            if not changed:
                raise BridgeError("A signed or unresolved approval cannot be released.", 409)

    def set_approval_final(self, uid, aid, succeeded):
        state = "COMPLETED" if succeeded else "FAILED"
        with self.ledger.db() as db:
            changed = db.execute("""UPDATE custody_bridge_approvals SET state=?,
                signed_transaction=NULL,updated_at=? WHERE id=? AND uid=?
                AND state IN ('SIGNED','SUBMITTED','UNKNOWN')""",
                (state, int(time.time()), str(aid), int(uid))).rowcount
            if not changed:
                row = db.execute("SELECT state FROM custody_bridge_approvals WHERE id=? AND uid=?",
                                 (str(aid), int(uid))).fetchone()
                if not row or row["state"] != state:
                    raise BridgeError("Approval state changed.", 409)
        return self.approval_for_user(uid, aid)

    def mark_transfer_needs_review(self, uid, tid):
        with self.ledger.db() as db:
            changed = db.execute("""UPDATE custody_bridge_transfers SET state='NEEDS_REVIEW',
                updated_at=? WHERE id=? AND uid=? AND state IN ('SUBMITTED','UNKNOWN')""",
                (int(time.time()), str(tid), int(uid))).rowcount
            if not changed:
                raise BridgeError("Transfer state changed; funds remain reserved.", 409)
        return self.transfer_for_user(uid, tid)

    def release_failed_source(self, uid, tid, *, verified_finalized_failure=False):
        if verified_finalized_failure is not True:
            raise BridgeError("Finalized source-chain failure proof is required.", 409)
        with self.ledger.db() as db:
            row = db.execute("SELECT * FROM custody_bridge_transfers WHERE id=? AND uid=?",
                             (str(tid), int(uid))).fetchone()
            if not row or row["state"] not in ("SUBMITTED", "UNKNOWN", "NEEDS_REVIEW"):
                raise BridgeError("Transfer is not eligible for recovery.", 409)
            hold = "bridge-operation:" + str(tid)
            self.ledger.post(db, "bridge-fail:" + str(tid), "Release finalized failed bridge",
                [(hold, row["source_asset"], -int(row["amount_raw"])),
                 (user_account(uid), row["source_asset"], int(row["amount_raw"]))])
            db.execute("""UPDATE custody_bridge_transfers SET state='ABORTED',
                signed_transaction=NULL,updated_at=? WHERE id=?""",
                (int(time.time()), str(tid)))
        return self.transfer_for_user(uid, tid)
