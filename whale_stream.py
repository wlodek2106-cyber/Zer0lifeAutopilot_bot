"""Low-latency, detection-only Solana whale buy stream via Helius WebSockets."""
import asyncio
import inspect
import json
import math
import time
from collections import deque
from urllib.parse import quote

import aiohttp
from solders.pubkey import Pubkey
from solders.signature import Signature

SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDC_DECIMALS = 6
MIN_WHALE_LAMPORTS = 10_000_000_000
MAX_WHALE_LAMPORTS = 1_000_000_000_000
SOL_PRICE_CACHE_SECONDS = 30
HELIUS_WS = "wss://mainnet.helius-rpc.com/"
HELIUS_RPC = "https://mainnet.helius-rpc.com/"
# Keep transaction lookups at a conservative 2 requests/second until the
# account's exact provider rate limit is known; bursts are bounded by the queue.
LOOKUP_INTERVAL_SECONDS = 0.5
STALE_LOOKUP_SECONDS = 30
MAX_LOOKUP_QUEUE = 32

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
# Standard logsSubscribe is available across Helius plans; the app's supported
# DEX allowlist is curated to these known programs, not every Solana DEX.
WHALE_SIGNAL_PROGRAMS = dict(DEX_PROGRAMS)


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


def _token_decimals(row):
    amount = row.get("uiTokenAmount") or {}
    decimals = amount.get("decimals")
    return decimals if type(decimals) is int and 0 <= decimals <= 18 else None


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


def parse_log_notification(notification):
    """Extract successful BUY/SWAP signatures for later transaction validation."""
    if not isinstance(notification, dict) or notification.get("method") != "logsNotification":
        return None
    params = notification.get("params") or {}
    result = params.get("result") if isinstance(params, dict) else None
    if not isinstance(result, dict):
        return None
    context = result.get("context") or {}
    value = result.get("value") or {}
    if not isinstance(value, dict) or value.get("err") is not None:
        return None
    signature, slot = value.get("signature"), context.get("slot")
    logs = value.get("logs") or []
    if (not _signature(signature) or type(slot) is not int or slot <= 0
            or not isinstance(logs, list)):
        return None
    joined_logs = " ".join(line for line in logs if isinstance(line, str)).lower()
    if not any(action in joined_logs for action in (
            "instruction: buy", "event: buy", "instruction: swap", "event: swap")):
        return None
    return {"signature": signature, "slot": slot}


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


def parse_whale_buy(notification, observed_at=None, sol_usd_price=None, *,
                    allow_unpriced_usdc=False):
    """Parse a confirmed DEX buy sized at 10–1,000 SOL or its USDC equivalent.

    SOL spend is the fee- and new-token-account-rent-adjusted net debit of the
    fee payer. USDC spend is converted using a fresh SOL/USD reference. The
    result is an observed transaction estimate, not a profit forecast.
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
        if not isinstance(row, dict) or row.get("owner") != payer:
            continue
        amount = _raw_token_amount(row)
        index, mint = row.get("accountIndex"), row.get("mint")
        if type(index) is int and _pubkey(mint):
            if amount is None and mint == USDC_MINT:
                return None
            if amount is not None:
                decimals = _token_decimals(row)
                if mint == USDC_MINT and decimals != USDC_DECIMALS:
                    return None
                pre_by_account[(index, mint)] = (amount, decimals)

    post_by_account = {}
    for row in post_token_rows:
        if not isinstance(row, dict) or row.get("owner") != payer:
            continue
        mint, index = row.get("mint"), row.get("accountIndex")
        amount = _raw_token_amount(row)
        if type(index) is not int or not _pubkey(mint):
            continue
        if amount is None and mint == USDC_MINT:
            return None
        if amount is None:
            continue
        decimals = _token_decimals(row)
        if mint == USDC_MINT and decimals != USDC_DECIMALS:
            return None
        post_by_account[(index, mint)] = (amount, decimals)

    deltas = {}
    created_token_accounts = []
    usdc_spend_raw = 0
    for index, mint in pre_by_account.keys() | post_by_account.keys():
        before = pre_by_account.get((index, mint))
        after = post_by_account.get((index, mint))
        prior = before[0] if before else 0
        amount = after[0] if after else 0
        if mint == USDC_MINT:
            if (before and before[1] != USDC_DECIMALS) or (after and after[1] != USDC_DECIMALS):
                return None
            usdc_spend_raw += max(0, prior - amount)
            continue
        if mint == SOL_MINT:
            continue
        delta = amount - prior
        if delta > 0:
            deltas[mint] = deltas.get(mint, 0) + delta
            if before is None and after is not None:
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
    mint = next(iter(deltas))
    sol_usd_price = (float(sol_usd_price) if type(sol_usd_price) in (int, float)
                     and math.isfinite(sol_usd_price) and sol_usd_price > 0 else None)
    if usdc_spend_raw:
        # A transaction with a whale-sized native debit and USDC input is
        # ambiguous; do not count the same swap under either quote asset.
        if native_spend >= MIN_WHALE_LAMPORTS:
            return None
        usdc_amount = usdc_spend_raw / 10**USDC_DECIMALS
        if sol_usd_price is None:
            if not allow_unpriced_usdc:
                return None
            sol_equivalent = None
            price_pending = True
        else:
            sol_equivalent = usdc_amount / sol_usd_price
            if not (MIN_WHALE_LAMPORTS / 1_000_000_000
                    <= sol_equivalent <= MAX_WHALE_LAMPORTS / 1_000_000_000):
                return None
            price_pending = False
        quote_asset, quote_amount = "USDC", usdc_amount
        sol_lamports = None
    else:
        if not MIN_WHALE_LAMPORTS <= native_spend <= MAX_WHALE_LAMPORTS:
            return None
        sol_equivalent = native_spend / 1_000_000_000
        price_pending = False
        quote_asset, quote_amount = "SOL", sol_equivalent
        sol_lamports = native_spend

    return {
        "signature": signature,
        "slot": slot,
        "mint": mint,
        "program": program_name,
        "quote_asset": quote_asset,
        "quote_amount": quote_amount,
        "sol_equivalent": sol_equivalent,
        "sol_price_usd": sol_usd_price if quote_asset == "USDC" else None,
        "price_pending": price_pending,
        "sol_lamports": sol_lamports,
        "seen_at": int(time.time() if observed_at is None else observed_at),
    }


class HeliusStreamError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


class HeliusWhaleStream:
    """One reconnecting Helius subscription. It never submits or signs trades."""

    def __init__(self, api_key="", on_signal=None, enabled=True, sol_price_provider=None):
        self.api_key = str(api_key or "").strip()
        self.on_signal = on_signal
        self.sol_price_provider = sol_price_provider
        self._sol_usd_price = None
        self._sol_price_checked_at = 0.0
        self._sol_price_lock = asyncio.Lock()
        self.configuration_enabled = bool(enabled)
        self.state = ("connecting" if self.api_key else "waiting_for_key"
                      ) if self.configuration_enabled else "stopped"
        self.last_error = ""
        self.reconnects = 0
        self.total_signals = 0
        self.transaction_lookups = 0
        self.dropped_lookups = 0
        self.last_signal_at = None
        self.recent_signals = deque(maxlen=12)
        self._seen_order = deque()
        self._seen = set()
        self._requested_order = deque()
        self._requested = set()
        self._transaction_queue = asyncio.Queue(maxsize=MAX_LOOKUP_QUEUE)
        self._last_lookup_at = 0.0
        self._connected_at = None
        self.message = self._state_message()

    @property
    def enabled(self):
        return self.configuration_enabled and bool(self.api_key)

    def _state_message(self):
        if not self.configuration_enabled:
            return ("Helius whale stream is disabled by configuration. "
                    "Scheduled candidate scanning remains available; whale-triggered signals are off.")
        if not self.api_key:
            return ("Waiting for HELIUS_API_KEY in Render. The stream is off; "
                    "no paid-plan upgrade or trade is triggered.")
        details = {
            "AuthRejected": "Helius rejected the API key (401/403); check the Render secret.",
            "SubscriptionRejected": "Helius rejected a standard logs subscription.",
            "SubscriptionTimeout": "Helius did not confirm the logs subscriptions.",
            "RateLimited": "The Helius/provider rate limit was reached; some transaction checks may be dropped.",
            "TransactionUnavailable": "A transaction lookup returned no confirmed transaction.",
            "StaleTransaction": "The lookup queue was too busy; an old signal was dropped.",
            "ProviderUnavailable": "Helius RPC is temporarily unavailable.",
            "SolUsdUnavailable": "A fresh SOL/USD reference was unavailable; USDC whale-size checks were skipped.",
            "SignalHandlerError": "A signal could not be queued for screening.",
            "TransactionFetchError": "A transaction lookup failed unexpectedly.",
        }
        detail = details.get(self.last_error, "")
        if self.state == "connected":
            if detail:
                return "Connected to standard logsSubscribe. " + detail
            return ("Connected to Helius standard logsSubscribe for confirmed BUY/SWAP logs from "
                    + ", ".join(WHALE_SIGNAL_PROGRAMS)
                    + ". SOL and USDC buys use the same 10–1,000 SOL-equivalent threshold. "
                    "USDC checks require a fresh SOL/USD price; candidates still pass screening.")
        if self.state == "retrying":
            reason = (" " + detail) if detail else ""
            return ("Helius standard stream disconnected; reconnecting with bounded backoff."
                    + reason + " No signal submits a trade.")
        if self.state == "stopped":
            return "Helius stream stopped."
        return "Connecting to Helius standard logsSubscribe stream."

    def subscriptions(self):
        return [{
            "jsonrpc": "2.0",
            "id": index,
            "method": "logsSubscribe",
            "params": [
                {"mentions": [program_id]},
                {"commitment": "confirmed"},
            ],
        } for index, program_id in enumerate(WHALE_SIGNAL_PROGRAMS.values(), start=1)]

    def snapshot(self):
        return {
            "provider": "Helius standard WebSocket",
            "state": self.state,
            "enabled": self.enabled,
            "message": self._state_message(),
            "supported_programs": list(WHALE_SIGNAL_PROGRAMS),
            "reconnects": self.reconnects,
            "last_error": self.last_error,
            "last_signal_at": self.last_signal_at,
            "signals_seen": self.total_signals,
            "transaction_lookups": self.transaction_lookups,
            "queued_lookups": self._transaction_queue.qsize(),
            "dropped_lookups": self.dropped_lookups,
            "lookup_limit_per_second": 2,
            "recent_signals": [
                {**event, "sol": event["sol_equivalent"]}
                for event in self.recent_signals
            ],
        }

    def _remember_requested(self, signature):
        self._requested.add(signature)
        self._requested_order.append(signature)
        if len(self._requested_order) > 5000:
            self._requested.discard(self._requested_order.popleft())

    async def handle_logs_notification(self, payload):
        event = parse_log_notification(payload)
        if not event or event["signature"] in self._requested or event["signature"] in self._seen:
            return False
        if self._transaction_queue.full():
            self._transaction_queue.get_nowait()
            self._transaction_queue.task_done()
            self.dropped_lookups += 1
        self._remember_requested(event["signature"])
        self._transaction_queue.put_nowait(
            (event["signature"], event["slot"], int(time.time())))
        return True

    async def _current_sol_usd_price(self, session):
        now = time.monotonic()
        if now - self._sol_price_checked_at < SOL_PRICE_CACHE_SECONDS:
            return self._sol_usd_price
        async with self._sol_price_lock:
            now = time.monotonic()
            if now - self._sol_price_checked_at < SOL_PRICE_CACHE_SECONDS:
                return self._sol_usd_price
            self._sol_price_checked_at = now
            self._sol_usd_price = None
            if not callable(self.sol_price_provider):
                self.last_error = "SolUsdUnavailable"
                return None
            try:
                result = self.sol_price_provider(session)
                if inspect.isawaitable(result):
                    result = await result
                price = result.get("SOL") if isinstance(result, dict) else None
                if (isinstance(price, bool) or type(price) not in (int, float)
                        or not math.isfinite(price) or price <= 0):
                    raise ValueError("SOL/USD unavailable")
                self._sol_usd_price = float(price)
                if self.last_error == "SolUsdUnavailable":
                    self.last_error = ""
                return self._sol_usd_price
            except Exception:
                self.last_error = "SolUsdUnavailable"
                return None

    async def _fetch_transaction(self, session, signature):
        self.transaction_lookups += 1
        url = HELIUS_RPC + "?api-key=" + quote(self.api_key, safe="")
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getTransaction",
            "params": [signature, {
                "commitment": "confirmed",
                "encoding": "jsonParsed",
                "maxSupportedTransactionVersion": 0,
            }],
        }
        timeout = aiohttp.ClientTimeout(total=10)
        async with session.post(url, json=body, timeout=timeout) as response:
            if response.status in (401, 403):
                raise HeliusStreamError("AuthRejected")
            if response.status == 429:
                raise HeliusStreamError("RateLimited")
            if response.status >= 500:
                raise HeliusStreamError("ProviderUnavailable")
            if response.status >= 400:
                raise HeliusStreamError("TransactionFetchError")
            result = await response.json(content_type=None)
        if not isinstance(result, dict):
            raise HeliusStreamError("TransactionFetchError")
        if result.get("error"):
            error = result["error"]
            code = error.get("code") if isinstance(error, dict) else None
            raise HeliusStreamError("RateLimited" if code in (-32005, -32007) else "TransactionFetchError")
        transaction = result.get("result")
        if not isinstance(transaction, dict):
            raise HeliusStreamError("TransactionUnavailable")
        self.last_error = ""
        payload = {"result": transaction}
        event = parse_whale_buy(payload, allow_unpriced_usdc=True)
        if not event:
            return
        if event["quote_asset"] == "USDC":
            sol_usd_price = await self._current_sol_usd_price(session)
            if sol_usd_price is None:
                return
            event = parse_whale_buy(payload, sol_usd_price=sol_usd_price)
        await self._record_signal(event)

    async def _lookup_worker(self, session):
        loop = asyncio.get_running_loop()
        while True:
            signature, slot, queued_at = await self._transaction_queue.get()
            try:
                if time.time() - queued_at > STALE_LOOKUP_SECONDS:
                    self.dropped_lookups += 1
                    self.last_error = "StaleTransaction"
                    continue
                delay = LOOKUP_INTERVAL_SECONDS - (loop.time() - self._last_lookup_at)
                if delay > 0:
                    await asyncio.sleep(delay)
                if time.time() - queued_at > STALE_LOOKUP_SECONDS:
                    self.dropped_lookups += 1
                    self.last_error = "StaleTransaction"
                    continue
                self._last_lookup_at = loop.time()
                await self._fetch_transaction(session, signature)
            except asyncio.CancelledError:
                raise
            except HeliusStreamError as exc:
                self.last_error = exc.code
            except Exception:
                self.last_error = "TransactionFetchError"
            finally:
                self._transaction_queue.task_done()

    async def handle_notification(self, payload):
        return await self._record_signal(parse_whale_buy(payload))

    async def _record_signal(self, event):
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
                requests = self.subscriptions()
                pending = {request["id"] for request in requests}
                for request in requests:
                    await socket.send_json(request)
                deadline = asyncio.get_running_loop().time() + 15
                while pending:
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        raise HeliusStreamError("SubscriptionTimeout")
                    try:
                        message = await socket.receive(timeout=remaining)
                    except asyncio.TimeoutError as exc:
                        raise HeliusStreamError("SubscriptionTimeout") from exc
                    if message.type == aiohttp.WSMsgType.ERROR:
                        raise HeliusStreamError("ProviderUnavailable")
                    if message.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSE):
                        raise HeliusStreamError("ProviderUnavailable")
                    if message.type != aiohttp.WSMsgType.TEXT:
                        continue
                    try:
                        payload = json.loads(message.data)
                    except (TypeError, json.JSONDecodeError):
                        continue
                    if not isinstance(payload, dict):
                        continue
                    request_id = payload.get("id")
                    if request_id in pending:
                        if payload.get("error") or type(payload.get("result")) is not int:
                            raise HeliusStreamError("SubscriptionRejected")
                        pending.remove(request_id)
                    elif payload.get("method") == "logsNotification":
                        await self.handle_logs_notification(payload)

                self.state = "connected"
                self.last_error = ""
                self._connected_at = time.time()
                self.message = self._state_message()
                lookup_task = asyncio.create_task(self._lookup_worker(session))
                try:
                    async for message in socket:
                        if message.type == aiohttp.WSMsgType.TEXT:
                            try:
                                payload = json.loads(message.data)
                            except (TypeError, json.JSONDecodeError):
                                continue
                            if isinstance(payload, dict) and payload.get("method") == "logsNotification":
                                await self.handle_logs_notification(payload)
                            elif isinstance(payload, dict) and payload.get("error"):
                                self.last_error = "SubscriptionRejected"
                        elif message.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSE):
                            break
                        elif message.type == aiohttp.WSMsgType.ERROR:
                            raise HeliusStreamError("ProviderUnavailable")
                finally:
                    lookup_task.cancel()
                    await asyncio.gather(lookup_task, return_exceptions=True)
        raise HeliusStreamError("ProviderUnavailable")

    async def run(self):
        if not self.enabled:
            self.state = "stopped" if not self.configuration_enabled else "waiting_for_key"
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
            except HeliusStreamError as exc:
                connected_at = self._connected_at
                if connected_at and time.time() - connected_at >= 60:
                    delay = 1
                self.reconnects += 1
                self.state = "retrying"
                self.last_error = exc.code
                self.message = self._state_message()
                await asyncio.sleep(delay)
                delay = min(300, delay * 2)
            except aiohttp.WSServerHandshakeError as exc:
                self.reconnects += 1
                self.state = "retrying"
                self.last_error = "AuthRejected" if exc.status in (401, 403) else "ProviderUnavailable"
                self.message = self._state_message()
                await asyncio.sleep(delay)
                delay = min(300, delay * 2)
            except Exception as exc:
                connected_at = self._connected_at
                if connected_at and time.time() - connected_at >= 60:
                    delay = 1
                self.reconnects += 1
                self.state = "retrying"
                self.last_error = "ProviderUnavailable"
                self.message = self._state_message()
                await asyncio.sleep(delay)
                delay = min(300, delay * 2)
