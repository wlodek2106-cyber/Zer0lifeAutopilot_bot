"""Read-only LI.FI quotes for Solana/Base USDC.

This module only requests, validates, and formats estimates. It has no signing,
approval, reservation, broadcast, settlement, or balance-crediting code.
"""
import asyncio
import os
import re
import sqlite3
import time
import uuid
from decimal import Decimal, InvalidOperation

import aiohttp
from custody_ledger import USDC


SOLANA_CHAIN_ID = 1151111081099710
BASE_CHAIN_ID = 8453
SOLANA_USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
BASE_USDC_ASSET = "BASE_USDC"
BASE_USDC_ADDRESS = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
LIFI_API = "https://li.quest/v1"
QUOTE_TTL_SECONDS = 45
MAX_QUOTE_AMOUNT_RAW = 10_000_000_000  # $10,000
QUOTE_SLIPPAGE = "0.005"


class BridgeQuoteError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def parse_usdc_raw(value):
    if isinstance(value, bool):
        raise BridgeQuoteError("Enter a valid USDC amount.")
    try:
        amount = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError):
        raise BridgeQuoteError("Enter a valid USDC amount.") from None
    if (not amount.is_finite() or amount <= 0
            or amount * Decimal(1_000_000) !=
            (amount * Decimal(1_000_000)).to_integral_value()):
        raise BridgeQuoteError("Enter a valid USDC amount with at most six decimals.")
    raw = int(amount * Decimal(1_000_000))
    if raw > MAX_QUOTE_AMOUNT_RAW:
        raise BridgeQuoteError("A quote is limited to 10,000 USDC.")
    return raw


def _evm_address(value):
    value = str(value or "").strip()
    if not re.fullmatch(r"0x[0-9a-fA-F]{40}", value):
        raise BridgeQuoteError("The Base hot-wallet address is not configured correctly.", 503)
    return value


def _same_address(actual, expected):
    actual, expected = str(actual or ""), str(expected or "")
    if expected.startswith("0x"):
        return actual.lower() == expected.lower()
    return actual == expected


def _finite_decimal(value, default="0"):
    try:
        result = Decimal(str(value))
        return result if result.is_finite() and result >= 0 else Decimal(default)
    except (InvalidOperation, ValueError, TypeError):
        return Decimal(default)


def _normalise_quote(payload, expected):
    """Validate the requested route and keep provider transaction data private."""
    try:
        action = payload["action"]
        estimate = payload["estimate"]
        transaction = payload["transactionRequest"]
        source = action["fromToken"]
        destination = action["toToken"]
        from_chain = int(action["fromChainId"])
        to_chain = int(action["toChainId"])
        from_amount = int(action["fromAmount"])
        estimate_from = int(estimate["fromAmount"])
        output = int(estimate["toAmount"])
        minimum = int(estimate["toAmountMin"])
        if (from_chain != expected["from_chain"]
                or to_chain != expected["to_chain"]
                or from_amount != expected["amount_raw"]
                or estimate_from != expected["amount_raw"]
                or not _same_address(action["fromAddress"], expected["from_address"])
                or not _same_address(action["toAddress"], expected["to_address"])
                or not _same_address(source["address"], expected["from_token"])
                or not _same_address(destination["address"], expected["to_token"])
                or not isinstance(transaction, dict)
                or output <= 0 or minimum <= 0 or minimum > output):
            raise ValueError()
        if expected["from_chain"] == BASE_CHAIN_ID:
            tx_value = transaction.get("value", "0x0")
            tx_value = int(tx_value, 16) if isinstance(tx_value, str) else int(tx_value)
            if (int(transaction["chainId"]) != BASE_CHAIN_ID
                    or not _same_address(transaction["from"], expected["from_address"])
                    or tx_value != 0
                    or not re.fullmatch(r"0x[0-9a-fA-F]{40}", str(transaction["to"]))
                    or not re.fullmatch(r"0x(?:[0-9a-fA-F]{2})*", str(transaction["data"]))
                    or len(transaction["data"]) > 32_000):
                raise ValueError()
        else:
            encoded = transaction.get("data")
            version = transaction.get("svmTransactionVersion")
            if (not isinstance(encoded, str) or not encoded or len(encoded) > 200_000
                    or type(version) is not int or version not in (0, 1)):
                raise ValueError()
        return payload
    except (KeyError, TypeError, ValueError, InvalidOperation):
        raise BridgeQuoteError(
            "LI.FI returned a quote for a different route or invalid amount.", 502
        ) from None


def public_quote(payload, quote_id, created_at, source_available_raw):
    """Return display fields only; never return the provider transaction payload."""
    estimate = payload["estimate"]
    action = payload["action"]
    fees = []
    for kind, field in (("provider", "feeCosts"), ("network", "gasCosts")):
        entries = estimate.get(field, [])
        if not isinstance(entries, list):
            continue
        for entry in entries[:12]:
            if not isinstance(entry, dict):
                continue
            token = entry.get("token") or {}
            fees.append({
                "kind": kind,
                "name": str(entry.get("name") or entry.get("type") or kind)[:80],
                "amount_usd": str(_finite_decimal(entry.get("amountUSD"))),
                "symbol": str(token.get("symbol") or "unknown")[:16],
                "included": entry.get("included") is True,
            })
    gas_total = sum(
        (_finite_decimal(item.get("amountUSD"))
         for item in (estimate.get("gasCosts") or [])
         if isinstance(item, dict)),
        Decimal(0),
    )
    source_balance = Decimal(source_available_raw) / Decimal(1_000_000)
    amount_in = Decimal(action["fromAmount"]) / Decimal(1_000_000)
    return {
        "quote_id": quote_id,
        "direction": ("solana-to-base" if int(action["fromChainId"]) == SOLANA_CHAIN_ID
                      else "base-to-solana"),
        "amount_in": str(amount_in),
        "amount_out": str(Decimal(estimate["toAmount"]) / Decimal(1_000_000)),
        "amount_out_min": str(Decimal(estimate["toAmountMin"]) / Decimal(1_000_000)),
        "provider": str((payload.get("toolDetails") or {}).get("name")
                        or payload.get("tool") or "Bridge")[:48],
        "provider_fees": fees,
        "gas_estimate_usd": str(gas_total),
        "source_available": str(source_balance),
        "sufficient_source_balance": source_balance >= amount_in,
        "requires_approval": bool(
            int(action["fromChainId"]) == BASE_CHAIN_ID
            and estimate.get("approvalAddress")
            and str(estimate["approvalAddress"]).lower()
            != "0x0000000000000000000000000000000000000000"
        ),
        "created_at": created_at,
        "expires_at": created_at + QUOTE_TTL_SECONDS,
        "quote_only": True,
        "transfer_enabled": False,
    }


class BridgeQuoteService:
    def __init__(self, db_file, ledger, solana_wallet):
        self.db_file = str(db_file)
        self.ledger = ledger
        self.solana_wallet = str(solana_wallet or "").strip()
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_file, timeout=10) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS custody_bridge_quote_attempts (
                uid INTEGER NOT NULL,
                created_at INTEGER NOT NULL
            )""")
            db.execute("""CREATE INDEX IF NOT EXISTS custody_bridge_quote_attempts_uid_time
                ON custody_bridge_quote_attempts(uid, created_at)""")

    def _reserve_quote_attempt(self, uid, now):
        try:
            with sqlite3.connect(self.db_file, timeout=10) as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute("DELETE FROM custody_bridge_quote_attempts WHERE created_at<?",
                           (now - 3600,))
                last = db.execute(
                    "SELECT MAX(created_at) FROM custody_bridge_quote_attempts WHERE uid=?",
                    (int(uid),),
                ).fetchone()[0]
                user_count = db.execute(
                    "SELECT COUNT(*) FROM custody_bridge_quote_attempts WHERE uid=?",
                    (int(uid),),
                ).fetchone()[0]
                global_count = db.execute(
                    "SELECT COUNT(*) FROM custody_bridge_quote_attempts"
                ).fetchone()[0]
                if last is not None and now - int(last) < 3:
                    raise BridgeQuoteError("Wait a few seconds before requesting another quote.", 429)
                if int(user_count) >= 60 or int(global_count) >= 500:
                    raise BridgeQuoteError("The temporary quote limit has been reached. Try again later.", 429)
                db.execute("INSERT INTO custody_bridge_quote_attempts(uid,created_at) VALUES(?,?)",
                           (int(uid), int(now)))
        except BridgeQuoteError:
            raise
        except sqlite3.Error:
            raise BridgeQuoteError("The quote service is temporarily unavailable.", 503) from None

    async def quote(self, session, uid, direction, amount):
        if direction not in ("solana-to-base", "base-to-solana"):
            raise BridgeQuoteError("Choose a valid quote direction.")
        api_key = os.getenv("LIFI_API_KEY", "").strip()
        if not api_key:
            raise BridgeQuoteError("LI.FI API key is not configured on the server.", 503)
        base_wallet = _evm_address(os.getenv("BASE_HOT_WALLET_ADDRESS", ""))
        try:
            from solders.pubkey import Pubkey
            if str(Pubkey.from_string(self.solana_wallet)) != self.solana_wallet:
                raise ValueError()
        except Exception:
            raise BridgeQuoteError("The Solana wallet is not configured correctly.", 503) from None

        raw = parse_usdc_raw(amount)
        from_chain, to_chain = (
            (SOLANA_CHAIN_ID, BASE_CHAIN_ID) if direction == "solana-to-base"
            else (BASE_CHAIN_ID, SOLANA_CHAIN_ID)
        )
        from_token, to_token = (
            (SOLANA_USDC, BASE_USDC_ADDRESS) if direction == "solana-to-base"
            else (BASE_USDC_ADDRESS, SOLANA_USDC)
        )
        from_address, to_address = (
            (self.solana_wallet, base_wallet) if direction == "solana-to-base"
            else (base_wallet, self.solana_wallet)
        )
        try:
            source_available_raw = int(self.ledger.balance(
                int(uid), USDC if direction == "solana-to-base" else BASE_USDC_ASSET
            ))
        except Exception:
            raise BridgeQuoteError("Could not read your available account balance.", 503) from None

        now = int(time.time())
        self._reserve_quote_attempt(uid, now)
        params = {
            "fromChain": str(from_chain),
            "toChain": str(to_chain),
            "fromToken": from_token,
            "toToken": to_token,
            "fromAmount": str(raw),
            "fromAddress": from_address,
            "toAddress": to_address,
            "order": "CHEAPEST",
            "slippage": QUOTE_SLIPPAGE,
        }
        headers = {"accept": "application/json", "x-lifi-api-key": api_key}
        try:
            async with session.get(LIFI_API + "/quote", params=params, headers=headers) as response:
                if response.status == 429:
                    raise BridgeQuoteError("LI.FI is rate-limiting quotes. Try again shortly.", 429)
                if response.status != 200:
                    raise BridgeQuoteError("The quote provider is unavailable. No transaction was sent.", 503)
                payload = await response.json()
        except BridgeQuoteError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            raise BridgeQuoteError("The quote provider is unavailable. No transaction was sent.", 503) from None

        _normalise_quote(payload, {
            "from_chain": from_chain,
            "to_chain": to_chain,
            "amount_raw": raw,
            "from_address": from_address,
            "to_address": to_address,
            "from_token": from_token,
            "to_token": to_token,
        })
        return public_quote(payload, str(uuid.uuid4()), now, source_available_raw)
