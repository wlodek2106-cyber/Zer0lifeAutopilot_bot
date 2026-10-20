"""Low-latency, detection-only Solana whale buy stream via Helius WebSockets."""
import asyncio
import inspect
import json
import time
from collections import deque
from urllib.parse import quote

import aiohttp
from solders.pubkey import Pubkey
from solders.signature import Signature

SOL_MINT = "So11111111111111111111111111111111111111112"
MIN_WHALE_LAMPORTS = 1_000_000_000
MAX_WHALE_LAMPORTS = 10_000_000_000
HELIUS_WS = "wss://mainnet.helius-rpc.com/"

# Curated mainnet programs, sourced from the official Pump, Raydium, Meteora,
# and Orca repositories. This is supported DEX coverage, not every Solana DEX.
DEX_PROGRAMS = {
    "Pump.fun": "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
    "PumpSwap": "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA",
    "Raydium AMM v4": "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",
    "Raydium CPMM": "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C",
    "Raydium CLMM": "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK",
    "Meteora DLMM": "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo",
    "Orca Whirlpools": "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc",
}
PROGRAM_NAMES = {program_id: name for name, program_id in DEX_PROGRAMS.items()}


def _pubkey(value):
    if not isinstance(value, str):
        return False
    try:
        return str(Pubkey.from_string(value)) == value
    except (ValueError, TypeError):
        return False


def _signature(value):
    if not isinstance(value, str):
        return False
    try:
        return str(Signature.from_string(value)) == value
    except (ValueError, TypeError):
        return False


def _raw_token_amount(row):
    amount = (row.get("uiTokenAmount") or {}).get("amount")
    if not isinstance(amount, str) or not amount.isdigit():
        return None
    return int(amount)


def _message_parts(notification):
    if not isinstance(notification, dict):
        return None
    if notification.get("method") not in (None, "transactionNotification"):
        return None
    params = notification.get("params") or {}
    result = params.get("result") if isinstance(params, dict) else None
    if result is None:
        result = notification.get("result", notification)
    if not isinstance(result, dict):
        return None
    context_slot = (result.get("context") or {}).get("slot")
    value = result.get("value", result)
    if not isinstance(value, dict):
        return None

    transaction_wrapper = value.get("transaction")
    if not isinstance(transaction_wrapper, dict):
        return None
    transaction = transaction_wrapper.get("transaction", transaction_wrapper)
    if not isinstance(transaction, dict):
        return None
    meta = value.get("meta") or transaction_wrapper.get("meta")
    if not isinstance(meta, dict) or "err" not in meta or meta.get("err") is not None:
        return None
    message = transaction.get("message") or transaction_wrapper.get("message")
    if not isinstance(message, dict):
        return None

    signatures = transaction.get("signatures") or transaction_wrapper.get("signatures") or []
    signature = value.get("signature") or (signatures[0] if signatures else None)
    slot = value.get("slot") or context_slot
    if not _signature(signature) or type(slot) is not int or slot <= 0:
        return None
    return value, transaction, message, meta, signature, slot


def _program_ids(message, meta, account_keys):
    found = set()
    instructions = list(message.get("instructions") or [])
    for group in meta.get("innerInstructions") or []:
        if isinstance(group, dict):
            instructions.extend(group.get("instructions") or [])
    for instruction in instructions:
        if not isinstance(instruction, dict):
            continue
        program_id = instruction.get("programId")
        index = instruction.get("programIdIndex")
        if not isinstance(program_id, str) and type(index) is int and 0 <= index < len(account_keys):
            program_id = account_keys[index]
        if isinstance(program_id, str):
            found.add(program_id)
    return found


def parse_whale_buy(notification, observed_at=None):
    """Parse one confirmed DEX transaction into a conservative SOL-buy signal.

    The SOL amount is the fee- and new-token-account-rent-adjusted net debit
    of the transaction fee payer. It is an observed transaction estimate,
    not a decoded quote or a promise that the buy was profitable.
    """
    parts = _message_parts(notification)
    if not parts:
        return None
    value, transaction, message, meta, signature, slot = parts
    account_rows = message.get("accountKeys") or []
    account_keys = []
    signers = []
    for row in account_rows:
        if isinstance(row, dict):
            key = row.get("pubkey")
            if row.get("signer") is True and isinstance(key, str):
                signers.append(key)
        else:
            key = row
        account_keys.append(key if isinstance(key, str) else "")
    if not account_keys:
        return None
    pre_balances = meta.get("preBalances")
    post_balances = meta.get("postBalances")
    fee = meta.get("fee")
    if (not isinstance(pre_balances, list) or not isinstance(post_balances, list)
            or len(pre_balances) != len(post_balances) or type(fee) is not int or fee < 0):
        return None
    if len(account_keys) < len(pre_balances):
        loaded = meta.get("loadedAddresses") or {}
        if isinstance(loaded, dict):
            for address_type in ("writable", "readonly"):
                values = loaded.get(address_type) or []
                if isinstance(values, list):
                    account_keys.extend(value for value in values if isinstance(value, str))
    if len(account_keys) != len(pre_balances):
        return None
    payer = signers[0] if signers else account_keys[0]
    # Solana's fee payer is the first account key; nonstandard signer layouts
    # are rejected rather than attributing another account's SOL movement.
    if not _pubkey(payer) or account_keys[0] != payer:
        return None

    program_ids = _program_ids(message, meta, account_keys)
    program_name = next((name for program_id, name in PROGRAM_NAMES.items()
                         if program_id in program_ids), None)
    if not program_name:
        return None

    payer_index = 0
    if (type(pre_balances[payer_index]) is not int or type(post_balances[payer_index]) is not int
            or pre_balances[payer_index] < 0 or post_balances[payer_index] < 0):
        return None

    pre_token_rows = meta.get("preTokenBalances")
    post_token_rows = meta.get("postTokenBalances")
    if not isinstance(pre_token_rows, list) or not isinstance(post_token_rows, list):
        return None
    pre_by_account = {}
    for row in pre_token_rows:
        if not isinstance(row, dict):
            continue
        amount = _raw_token_amount(row)
        index, mint = row.get("accountIndex"), row.get("mint")
        if type(index) is int and isinstance(mint, str) and amount is not None:
            pre_by_account[(index, mint)] = amount

    deltas = {}
    created_token_accounts = []
    for row in post_token_rows:
        if not isinstance(row, dict) or row.get("owner") != payer:
            continue
        mint, index = row.get("mint"), row.get("accountIndex")
        amount = _raw_token_amount(row)
        if not _pubkey(mint) or mint == SOL_MINT or type(index) is not int or amount is None:
            continue
        prior = pre_by_account.get((index, mint), 0)
        delta = amount - prior
        if delta > 0:
            deltas[mint] = deltas.get(mint, 0) + delta
            if (index, mint) not in pre_by_account:
                created_token_accounts.append(index)
    if len(deltas) != 1:
        return None

    native_spend = pre_balances[payer_index] - post_balances[payer_index] - fee
    for index in created_token_accounts:
        if index >= len(pre_balances) or index >= len(post_balances):
            return None
        if type(pre_balances[index]) is not int or type(post_balances[index]) is not int:
            return None
        native_spend -= max(0, post_balances[index] - pre_balances[index])
    if not MIN_WHALE_LAMPORTS <= native_spend <= MAX_WHALE_LAMPORTS:
        return None

    mint = next(iter(deltas))
    return {
        "signature": signature,
        "slot": slot,
        "mint": mint,
        "program": program_name,
        "sol_lamports": native_spend,
        "seen_at": int(time.time() if observed_at is None else observed_at),
    }


class HeliusWhaleStream:
    """One reconnecting Helius subscription. It never submits or signs trades."""

    def __init__(self, api_key="", on_signal=None):
        self.api_key = str(api_key or "").strip()
        self.on_signal = on_signal
        self.state = "connecting" if self.api_key else "waiting_for_key"
        self.last_error = ""
        self.reconnects = 0
        self.total_signals = 0
        self.last_signal_at = None
        self.recent_signals = deque(maxlen=12)
        self._seen_order = deque()
        self._seen = set()
        self._connected_at = None
        self.message = self._state_message()

    @property
    def enabled(self):
        return bool(self.api_key)

    def _state_message(self):
        if not self.api_key:
            return ("Waiting for HELIUS_API_KEY in Render. The stream is off; "
                    "no paid-plan upgrade or trade is triggered.")
        if self.state == "connected" and self.last_error == "SignalHandlerError":
            return "Connected to Helius, but a signal could not be queued for screening."
        if self.state == "connected":
            return ("Connected to Helius confirmed-transaction WebSocket. "
                    "Signals go through the existing candidate screen; a signal does not submit a trade.")
        if self.state == "retrying":
            return ("Helius stream disconnected; reconnecting with bounded backoff. "
                    "New entries still require the existing screening and execution guard.")
        if self.state == "stopped":
            return "Helius stream stopped."
        return "Connecting to Helius confirmed-transaction WebSocket."

    def subscription(self):
        return {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "transactionSubscribe",
            "params": [
                {"accountInclude": list(DEX_PROGRAMS.values())},
                {
                    "commitment": "confirmed",
                    "encoding": "jsonParsed",
                    "transactionDetails": "full",
                    "showRewards": False,
                    "maxSupportedTransactionVersion": 1,
                },
            ],
        }

    def snapshot(self):
        return {
            "provider": "Helius Enhanced WebSocket",
            "state": self.state,
            "enabled": self.enabled,
            "message": self._state_message(),
            "supported_programs": list(DEX_PROGRAMS),
            "reconnects": self.reconnects,
            "last_error": self.last_error,
            "last_signal_at": self.last_signal_at,
            "signals_seen": self.total_signals,
            "recent_signals": [
                {**event, "sol": event["sol_lamports"] / 1_000_000_000}
                for event in self.recent_signals
            ],
        }

    async def handle_notification(self, payload):
        event = parse_whale_buy(payload)
        if not event or event["signature"] in self._seen:
            return False
        self._seen.add(event["signature"])
        self._seen_order.append(event["signature"])
        if len(self._seen_order) > 5000:
            self._seen.discard(self._seen_order.popleft())
        self.total_signals += 1
        self.last_signal_at = event["seen_at"]
        self.recent_signals.appendleft(event)
        if self.on_signal:
            try:
                result = self.on_signal(event)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                # Keep the socket alive; the visible error avoids silent loss.
                self.last_error = "SignalHandlerError"
                self.message = "A whale signal could not be queued for screening."
        return True

    async def _connect_once(self):
        url = HELIUS_WS + "?api-key=" + quote(self.api_key, safe="")
        timeout = aiohttp.ClientTimeout(total=None, connect=15, sock_connect=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.ws_connect(
                url, heartbeat=30, timeout=15, max_msg_size=4 * 1024 * 1024
            ) as socket:
                self.state = "connected"
                self.last_error = ""
                self._connected_at = time.time()
                self.message = self._state_message()
                await socket.send_json(self.subscription())
                async for message in socket:
                    if message.type == aiohttp.WSMsgType.TEXT:
                        try:
                            payload = json.loads(message.data)
                        except (TypeError, json.JSONDecodeError):
                            continue
                        if isinstance(payload, dict) and payload.get("error"):
                            raise ConnectionError("Helius subscription rejected")
                        await self.handle_notification(payload)
                    elif message.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSE):
                        break
                    elif message.type == aiohttp.WSMsgType.ERROR:
                        raise ConnectionError("Helius WebSocket transport error")
        raise ConnectionError("Helius WebSocket closed")

    async def run(self):
        if not self.enabled:
            self.state = "waiting_for_key"
            self.message = self._state_message()
            return
        delay = 1
        while True:
            try:
                self.state = "connecting"
                self.message = self._state_message()
                await self._connect_once()
            except asyncio.CancelledError:
                self.state = "stopped"
                self.message = self._state_message()
                raise
            except Exception as exc:
                connected_at = self._connected_at
                if connected_at and time.time() - connected_at >= 60:
                    delay = 1
                self.reconnects += 1
                self.state = "retrying"
                self.last_error = type(exc).__name__
                self.message = self._state_message()
                await asyncio.sleep(delay)
                delay = min(300, delay * 2)
