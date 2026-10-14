import asyncio
import math
import aiohttp
from aiohttp import web
import sqlite3
import os
import logging
from datetime import datetime
import json
import base58
import base64
import hashlib
import hmac
import math
import time
from pathlib import Path
from decimal import Decimal, InvalidOperation
from urllib.parse import parse_qsl
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction, Transaction
from solders.pubkey import Pubkey
from solders.system_program import transfer, TransferParams
from solders.message import Message
from solders.hash import Hash
from solders.instruction import Instruction, AccountMeta
from custody_runtime import CustodyRuntime
from english_text import english_json_middleware
from account_security import AccountSecurity
from web_access import WebAccess
from wallet_runtime import WalletError

logging.basicConfig(level=logging.INFO, format='%(asctime)s - [%(levelname)s] - %(message)s')

# --- КОНФИГУРАЦИЯ ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
PORT = int(os.getenv("PORT", 10000))
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL", "https://zer0lifeautopilot-bot.onrender.com").strip()
ADMIN_TELEGRAM_ID = os.getenv("ADMIN_TELEGRAM_ID", "428821665").strip() or "428821665"
TELEGRAM_INIT_DATA_MAX_AGE_SECONDS = 86400
PIN_HASH_ITERATIONS = 310000
PIN_MAX_ATTEMPTS = 5
PIN_LOCK_SECONDS = 900
ADMIN_API_PATHS = {"/api/admin/status", "/api/admin/pending_operations", "/api/admin/fee_reserve", "/api/admin/opening_balance", "/api/admin/retire_legacy"}
WITHDRAW_LOCKS = {}
CUSTODY_SAFE_MODE = True
CUSTODY_PAUSE_MESSAGE = "Операции с реальными средствами временно приостановлены до сверки балансов и миграции реестра."

DB_DIR = "/var/data"
DB_FILE = os.path.join(DB_DIR, "zer0life_users.db") if os.path.exists(DB_DIR) else "zer0life_users.db"

SOLANA_RPC = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com").strip()
SHARED_DEPOSIT_WALLET = os.getenv("DEPOSIT_WALLET", "8hxiCofyaKCBkhR5nsDqvUivmfgxcVx8zo2WiCzSdM6L").strip()
SPLASH_IMAGE_URL = "https://i.postimg.cc/52n94Pzg/57D47DDD-9BED-4D4B-AA69-2B59CAB400AD.png"

USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
ASSOCIATED_TOKEN_PROGRAM_ID = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"

MIN_DEPOSIT_USDC = 50.0
MAX_DEPOSIT_USDC = 10000.0
MIN_BOT_BALANCE_USDC = 50.0

WATCHLIST = {
    "SOL":  {"mint": "So11111111111111111111111111111111111111112", "decimals": 9},
    "JUP":  {"mint": "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN", "decimals": 6},
    "RAY":  {"mint": "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R", "decimals": 6},
    "WIF":  {"mint": "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm", "decimals": 6},
    "PYTH": {"mint": "HZ1JovNiDcZvKhgWNgQX5Z59C3b22DoPtRUC544HLD37", "decimals": 6}
}

TRADE_AMOUNT_USDC = 20.0
TRADE_AMOUNT_RAW = int(TRADE_AMOUNT_USDC * 1_000_000)
TP_PCT = 0.035
SL_PCT = 0.020
MAX_SLIPPAGE_BPS = 40

MEME_ALLOCATION_SOL = 0.1
MEME_TARGET_MULTIPLIER = 10.0
MEME_HALF_MULTIPLIER = 5.0
MEME_STOP_LOSS_PCT = 0.80

IS_TRADING_ACTIVE = False
IS_MEME_ACTIVE = False
RECENT_LOGS = ["⚜️ Multi-Asset & Meme Sniper Engine v6.9 (Full Enterprise Recovery) инициализирован."]

def add_log(msg: str):
    global RECENT_LOGS
    timestamp = datetime.now().strftime('%H:%M:%S')
    entry = f"[{timestamp}] {msg}"
    RECENT_LOGS.insert(0, entry)
    if len(RECENT_LOGS) > 50:
        RECENT_LOGS.pop()
    logging.info(msg)

class TelegramAuthError(Exception):
    pass

def verify_telegram_init_data(raw_init_data: str) -> dict:
    if not TELEGRAM_TOKEN or not raw_init_data:
        raise TelegramAuthError("Open this app from Telegram.")
    try:
        pairs = parse_qsl(raw_init_data, keep_blank_values=True)
        fields = dict(pairs)
        if len(fields) != len(pairs):
            raise TelegramAuthError("Duplicate Telegram auth fields.")
        received_hash = fields.pop("hash", "")
        if not received_hash:
            raise TelegramAuthError("Missing Telegram auth signature.")
        auth_date = int(fields.get("auth_date", "0"))
        now = int(time.time())
        if auth_date <= 0 or auth_date > now + 60 or now - auth_date > TELEGRAM_INIT_DATA_MAX_AGE_SECONDS:
            raise TelegramAuthError("Telegram session expired.")
        check_string = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
        secret_key = hmac.new(b"WebAppData", TELEGRAM_TOKEN.encode("utf-8"), hashlib.sha256).digest()
        expected_hash = hmac.new(secret_key, check_string.encode("utf-8"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(received_hash, expected_hash):
            raise TelegramAuthError("Invalid Telegram auth signature.")
        user = json.loads(fields.get("user", "{}"))
        if not isinstance(user, dict) or int(user.get("id", 0)) <= 0:
            raise TelegramAuthError("Telegram user is missing.")
        return user
    except TelegramAuthError:
        raise
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise TelegramAuthError("Malformed Telegram auth data.") from exc

@web.middleware
async def telegram_auth_middleware(request, handler):
    if not request.path.startswith("/api/"):
        return await handler(request)
    try:
        access = getattr(request.app.get("wallet_runtime"), "web_access", None)
        if access and not request.headers.get("X-Telegram-Init-Data") and request.cookies.get("__Host-zrl-session"):
            telegram_user = access.authenticate(request)
            request["auth_method"] = "web"
        else:
            telegram_user = verify_telegram_init_data(request.headers.get("X-Telegram-Init-Data", ""))
            request["auth_method"] = "telegram"
    except (TelegramAuthError, WalletError) as exc:
        return web.json_response({"success": False, "message": str(exc)}, status=getattr(exc, "status", 401))
    request["telegram_user"] = telegram_user
    if request.path in ADMIN_API_PATHS:
        try:
            admin_id = int(ADMIN_TELEGRAM_ID)
        except (TypeError, ValueError):
            return web.json_response({"success": False, "message": "ADMIN_TELEGRAM_ID is not configured."}, status=503)
        if int(telegram_user["id"]) != admin_id:
            return web.json_response({"success": False, "message": "Admin access required."}, status=403)
    return await handler(request)

def hash_pin(pin: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), salt, PIN_HASH_ITERATIONS)
    return "pbkdf2_sha256$" + str(PIN_HASH_ITERATIONS) + "$" + salt.hex() + "$" + digest.hex()

def verify_pin_hash(pin: str, stored_hash: str):
    if stored_hash.startswith("pbkdf2_sha256$"):
        try:
            _, iterations, salt_hex, digest_hex = stored_hash.split("$", 3)
            calculated = hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), bytes.fromhex(salt_hex), int(iterations)).hex()
            return hmac.compare_digest(calculated, digest_hex), False
        except (ValueError, TypeError):
            return False, False
    legacy = hashlib.sha256(pin.encode("utf-8")).hexdigest()
    return hmac.compare_digest(legacy, stored_hash), True

def verify_user_pin(telegram_id: int, pin: str):
    if not pin or len(pin) > 128:
        return False, "invalid"
    conn = sqlite3.connect(DB_FILE, timeout=10)
    try:
        c = conn.cursor()
        c.execute("BEGIN IMMEDIATE")
        c.execute("SELECT pin_hash, pin_attempts, pin_locked_until FROM user_security WHERE telegram_id = ?", (telegram_id,))
        row = c.fetchone()
        now = int(time.time())
        if not row or not row[0]:
            conn.commit()
            return False, "not_set"
        if int(row[2] or 0) > now:
            conn.commit()
            return False, "locked"
        valid, legacy_hash = verify_pin_hash(pin, row[0])
        if valid:
            replacement = hash_pin(pin) if legacy_hash else row[0]
            c.execute("UPDATE user_security SET pin_hash = ?, pin_attempts = 0, pin_locked_until = 0 WHERE telegram_id = ?", (replacement, telegram_id))
            conn.commit()
            return True, "ok"
        previous_attempts = int(row[1] or 0)
        if int(row[2] or 0) and int(row[2]) <= now:
            previous_attempts = 0
        attempts = previous_attempts + 1
        locked_until = now + PIN_LOCK_SECONDS if attempts >= PIN_MAX_ATTEMPTS else 0
        c.execute("UPDATE user_security SET pin_attempts = ?, pin_locked_until = ? WHERE telegram_id = ?", (attempts, locked_until, telegram_id))
        conn.commit()
        return False, "locked" if locked_until else "invalid"
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def get_signer_keypair():
    pk_env = os.getenv("SOLANA_PRIVATE_KEY", "").strip()
    if not pk_env:
        return None
    try:
        pk_clean = pk_env.replace('"', '').replace("'", "").strip()
        if pk_clean.startswith("["):
            return Keypair.from_bytes(bytes(json.loads(pk_clean)))
        else:
            return Keypair.from_bytes(base58.b58decode(pk_clean))
    except Exception:
        return None

def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        telegram_id INTEGER PRIMARY KEY, username TEXT, balance_usdc REAL DEFAULT 0.0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS user_security (
        telegram_id INTEGER PRIMARY KEY, pin_hash TEXT, auth_type TEXT DEFAULT 'PIN',
        biometric_enabled INTEGER DEFAULT 0, pin_attempts INTEGER DEFAULT 0,
        pin_locked_until INTEGER DEFAULT 0)''')

    c.execute("PRAGMA table_info(user_security)")
    security_cols = [col[1] for col in c.fetchall()]
    if "pin_attempts" not in security_cols:
        c.execute("ALTER TABLE user_security ADD COLUMN pin_attempts INTEGER DEFAULT 0")
    if "pin_locked_until" not in security_cols:
        c.execute("ALTER TABLE user_security ADD COLUMN pin_locked_until INTEGER DEFAULT 0")

    c.execute("PRAGMA table_info(users)")
    u_cols = [col[1] for col in c.fetchall()]
    if "balance_usdc" not in u_cols:
        try: c.execute("ALTER TABLE users ADD COLUMN balance_usdc REAL DEFAULT 0.0")
        except Exception: pass

    c.execute('''CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT, telegram_id INTEGER, token_symbol TEXT, token_mint TEXT,
        buy_price REAL, amount_tokens REAL, sell_price REAL DEFAULT 0,
        profit_usdc REAL DEFAULT 0.0, buy_tx TEXT, sell_tx TEXT, timestamp TEXT, half_taken INTEGER DEFAULT 0)''')

    c.execute('''CREATE TABLE IF NOT EXISTS deposits (
        id INTEGER PRIMARY KEY AUTOINCREMENT, telegram_id INTEGER, tx_signature TEXT UNIQUE,
        amount_usdc REAL, status TEXT, timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')

    c.execute('''CREATE TABLE IF NOT EXISTS withdrawals (
        id INTEGER PRIMARY KEY AUTOINCREMENT, telegram_id INTEGER, token TEXT,
        amount REAL, recipient_address TEXT, tx_signature TEXT, status TEXT, 
        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')

    c.execute("PRAGMA table_info(trades)")
    t_cols = [col[1] for col in c.fetchall()]
    if "telegram_id" not in t_cols:
        try: c.execute("ALTER TABLE trades ADD COLUMN telegram_id INTEGER")
        except Exception: pass
    if "half_taken" not in t_cols:
        try: c.execute("ALTER TABLE trades ADD COLUMN half_taken INTEGER DEFAULT 0")
        except Exception: pass

    conn.commit()
    conn.close()

async def get_associated_token_address(wallet_pubkey: Pubkey, mint_pubkey: Pubkey) -> Pubkey:
    assoc, _ = Pubkey.find_program_address(
        [bytes(wallet_pubkey), bytes(Pubkey.from_string(TOKEN_PROGRAM_ID)), bytes(mint_pubkey)],
        Pubkey.from_string(ASSOCIATED_TOKEN_PROGRAM_ID)
    )
    return assoc

async def verify_solana_usdc_tx(session: aiohttp.ClientSession, tx_sig: str, target_wallet: str):
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getTransaction",
        "params": [tx_sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}]
    }
    try:
        async with session.post(SOLANA_RPC, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return False, 0.0, "Ошибка связи с Solana RPC."
            data = await resp.json()
            tx_info = data.get("result")
            if not tx_info:
                return False, 0.0, "Транзакция еще не подтверждена или не найдена в сети."

            meta = tx_info.get("meta")
            if not meta or meta.get("err") is not None:
                return False, 0.0, "Транзакция отменена сетью или завершилась ошибкой."

            pre_token_balances = meta.get("preTokenBalances", []) or []
            post_token_balances = meta.get("postTokenBalances", []) or []

            pre_amount, post_amount, found_target = 0.0, 0.0, False

            for b in post_token_balances:
                if b.get("mint") == USDC_MINT and b.get("owner") == target_wallet:
                    post_amount = float(b.get("uiTokenAmount", {}).get("uiAmount", 0.0) or 0.0)
                    found_target = True
                    break

            for b in pre_token_balances:
                if b.get("mint") == USDC_MINT and b.get("owner") == target_wallet:
                    pre_amount = float(b.get("uiTokenAmount", {}).get("uiAmount", 0.0) or 0.0)
                    break

            diff_usdc = round(post_amount - pre_amount, 2)
            if not found_target or diff_usdc <= 0:
                return False, 0.0, f"Не найдено зачисление USDC на адрес {target_wallet[:6]}..."
            if diff_usdc < MIN_DEPOSIT_USDC:
                return False, 0.0, f"Сумма ${diff_usdc:.2f} USDC меньше минимума (${MIN_DEPOSIT_USDC:.0f} USDC)."
            if diff_usdc > MAX_DEPOSIT_USDC:
                return False, 0.0, f"Сумма ${diff_usdc:.2f} USDC превышает максимум (${MAX_DEPOSIT_USDC:.0f} USDC)."

            return True, diff_usdc, "Успешно верифицировано."
    except Exception as e:
        return False, 0.0, f"Ошибка RPC: {str(e)[:40]}"

async def get_token_prices(session: aiohttp.ClientSession) -> dict:
    prices = {}
    liquidity_seen = {}
    mints_str = ",".join([v["mint"] for v in WATCHLIST.values()])
    url = f"https://api.dexscreener.com/tokens/v1/solana/{mints_str}"
    try:
        headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            if resp.status == 200:
                data = await resp.json()
                pairs = data if isinstance(data, list) else []
                for pair in pairs:
                    base_token = pair.get("baseToken", {}).get("address")
                    price_usd = pair.get("priceUsd")
                    liquidity = float(pair.get("liquidity", {}).get("usd", 0) or 0)
                    volume = float(pair.get("volume", {}).get("h24", 0) or 0)
                    if (pair.get("chainId") == "solana" and base_token and price_usd
                            and math.isfinite(liquidity) and liquidity >= 100000
                            and math.isfinite(volume) and volume >= 10000):
                        for sym, info in WATCHLIST.items():
                            price = float(price_usd)
                            if (info["mint"] == base_token and math.isfinite(price) and price > 0
                                    and liquidity > liquidity_seen.get(sym, 0)):
                                prices[sym] = price
                                liquidity_seen[sym] = liquidity
    except Exception:
        pass
    return prices

async def execute_jupiter_swap(session: aiohttp.ClientSession, input_mint: str, output_mint: str, amount_raw: int, signer: Keypair, slippage_bps: int = MAX_SLIPPAGE_BPS, priority_fee: int = 25000) -> str:
    if CUSTODY_SAFE_MODE:
        return ""
    try:
        user_pubkey = str(signer.pubkey())
        headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
        quote_url = f"https://api.jup.ag/swap/v1/quote?inputMint={input_mint}&outputMint={output_mint}&amount={amount_raw}&slippageBps={slippage_bps}"
        async with session.get(quote_url, headers=headers, timeout=aiohttp.ClientTimeout(total=6)) as resp:
            if resp.status != 200:
                return ""
            quote_data = await resp.json()

        payload = {
            "quoteResponse": quote_data,
            "userPublicKey": user_pubkey,
            "wrapAndUnwrapSol": True,
            "prioritizationFeeLamports": priority_fee
        }
        async with session.post("https://api.jup.ag/swap/v1/swap", headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=7)) as resp:
            if resp.status != 200:
                return ""
            res = await resp.json()
            swap_b64 = res.get("swapTransaction")

        if not swap_b64:
            return ""

        raw_bytes = base64.b64decode(swap_b64)
        tx = VersionedTransaction.from_bytes(raw_bytes)
        signed_tx = VersionedTransaction(tx.message, [signer])
        signed_b64 = base64.b64encode(bytes(signed_tx)).decode("utf-8")

        rpc_payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "sendTransaction",
            "params": [signed_b64, {"skipPreflight": False, "encoding": "base64"}]
        }
        async with session.post(SOLANA_RPC, json=rpc_payload, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            if resp.status == 200:
                data = await resp.json()
                sig = data.get("result")
                if sig:
                    return str(sig)
    except Exception:
        pass
    return ""

async def execute_native_sol_transfer(session: aiohttp.ClientSession, signer: Keypair, recipient_addr: str, amount_sol: float):
    if CUSTODY_SAFE_MODE:
        return None
    try:
        lamports = int(amount_sol * 1_000_000_000)
        recipient_pubkey = Pubkey.from_string(recipient_addr)

        payload = {"jsonrpc": "2.0", "id": 1, "method": "getLatestBlockhash", "params": [{"commitment": "finalized"}]}
        async with session.post(SOLANA_RPC, json=payload) as resp:
            data = await resp.json()
            blockhash_str = data["result"]["value"]["blockhash"]

        blockhash = Hash.from_string(blockhash_str)
        ix = transfer(TransferParams(from_pubkey=signer.pubkey(), to_pubkey=recipient_pubkey, lamports=lamports))
        msg = Message.new_with_blockhash([ix], signer.pubkey(), blockhash)
        tx = Transaction([signer], msg, blockhash)
        tx_b64 = base64.b64encode(bytes(tx)).decode('utf-8')

        send_payload = {
            "jsonrpc": "2.0", "id": 1, "method": "sendTransaction",
            "params": [tx_b64, {"encoding": "base64", "skipPreflight": False}]
        }
        async with session.post(SOLANA_RPC, json=send_payload) as resp:
            send_data = await resp.json()
            if "error" in send_data:
                add_log(f"⚠️ Ошибка RPC при выводе SOL: {send_data['error']}")
                return None
            return send_data.get("result")
    except Exception as e:
        add_log(f"⚠️ Ошибка формирования перевода SOL: {e}")
        return None

async def execute_spl_usdc_transfer(session: aiohttp.ClientSession, signer: Keypair, recipient_addr: str, amount_ui: Decimal) -> str:
    if CUSTODY_SAFE_MODE:
        return None
    try:
        raw_amount = int(amount_ui * Decimal(1_000_000))
        recipient_pubkey = Pubkey.from_string(recipient_addr)
        mint_pubkey = Pubkey.from_string(USDC_MINT)
        
        sender_ata = await get_associated_token_address(signer.pubkey(), mint_pubkey)
        recipient_ata = await get_associated_token_address(recipient_pubkey, mint_pubkey)

        payload_acc = {
            "jsonrpc": "2.0", "id": 1,
            "method": "getAccountInfo",
            "params": [str(recipient_ata), {"encoding": "jsonParsed"}]
        }
        async with session.post(SOLANA_RPC, json=payload_acc) as resp:
            acc_data = await resp.json()
            needs_creation = acc_data.get("result", {}).get("value") is None

        payload_bh = {"jsonrpc": "2.0", "id": 1, "method": "getLatestBlockhash", "params": [{"commitment": "finalized"}]}
        async with session.post(SOLANA_RPC, json=payload_bh) as resp:
            bh_data = await resp.json()
            blockhash_str = bh_data["result"]["value"]["blockhash"]
        blockhash = Hash.from_string(blockhash_str)

        instructions = []
        if needs_creation:
            create_ata_ix = Instruction(
                program_id=Pubkey.from_string(ASSOCIATED_TOKEN_PROGRAM_ID),
                accounts=[
                    AccountMeta(signer.pubkey(), is_signer=True, is_writable=True),
                    AccountMeta(recipient_ata, is_signer=False, is_writable=True),
                    AccountMeta(recipient_pubkey, is_signer=False, is_writable=False),
                    AccountMeta(mint_pubkey, is_signer=False, is_writable=False),
                    AccountMeta(Pubkey.from_string("11111111111111111111111111111111"), is_signer=False, is_writable=False),
                    AccountMeta(Pubkey.from_string(TOKEN_PROGRAM_ID), is_signer=False, is_writable=False),
                ],
                data=bytes([1])
            )
            instructions.append(create_ata_ix)

        data_bytes = bytes([12]) + raw_amount.to_bytes(8, 'little') + bytes([6])
        transfer_ix = Instruction(
            program_id=Pubkey.from_string(TOKEN_PROGRAM_ID),
            accounts=[
                AccountMeta(sender_ata, is_signer=False, is_writable=True),
                AccountMeta(mint_pubkey, is_signer=False, is_writable=False),
                AccountMeta(recipient_ata, is_signer=False, is_writable=True),
                AccountMeta(signer.pubkey(), is_signer=True, is_writable=False),
            ],
            data=data_bytes
        )
        instructions.append(transfer_ix)

        msg = Message.new_with_blockhash(instructions, signer.pubkey(), blockhash)
        tx = Transaction([signer], msg, blockhash)
        tx_b64 = base64.b64encode(bytes(tx)).decode('utf-8')

        send_payload = {
            "jsonrpc": "2.0", "id": 1, "method": "sendTransaction",
            "params": [tx_b64, {"encoding": "base64", "skipPreflight": False}]
        }
        async with session.post(SOLANA_RPC, json=send_payload) as resp:
            send_data = await resp.json()
            if "error" in send_data:
                add_log(f"⚠️ Ошибка RPC USDC transfer: {send_data['error']}")
                return None
            return send_data.get("result")
    except Exception as e:
        add_log(f"⚠️ Исключение при отправке USDC: {e}")
        return None

# --- ВОРКЕР 1: ОСНОВНОЙ (Топ-5 Ликвидных Активов) ---
async def real_trading_worker():
    global IS_TRADING_ACTIVE
    signer = get_signer_keypair()
    if not signer:
        return

    wallet_addr = str(signer.pubkey())
    add_log(f"🤖 Сканер Топ-5 запущен. Кошелек: {wallet_addr[:6]}...{wallet_addr[-4:]}")

    history = {sym: [] for sym in WATCHLIST}
    cooldown_until = 0

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                now_ts = datetime.now().timestamp()
                prices = await get_token_prices(session)

                for sym, p in prices.items():
                    history[sym].append(p)
                    if len(history[sym]) > 10:
                        history[sym].pop(0)

                conn = sqlite3.connect(DB_FILE)
                c = conn.cursor()
                
                c.execute("SELECT id, telegram_id, token_symbol, token_mint, buy_price, amount_tokens FROM trades WHERE sell_price = 0 AND token_symbol IN ('SOL', 'JUP', 'RAY', 'WIF', 'PYTH')")
                active_trades = c.fetchall()
                
                mints_to_sell = set()
                for trade in active_trades:
                    trade_id, t_tg_id, sym, mint, entry_px, amount_tokens = trade
                    cur_px = prices.get(sym, entry_px)
                    pnl_pct = (cur_px - entry_px) / entry_px
                    if pnl_pct >= TP_PCT or pnl_pct <= -SL_PCT:
                        mints_to_sell.add((sym, mint))

                for sym, mint in mints_to_sell:
                    total_tokens = sum([t[5] for t in active_trades if t[3] == mint])
                    raw_amount = int(total_tokens * (10 ** WATCHLIST[sym]["decimals"]))
                    sell_sig = await execute_jupiter_swap(session, mint, USDC_MINT, raw_amount, signer)
                    if sell_sig:
                        cur_px = prices.get(sym, 0)
                        for trade in active_trades:
                            if trade[3] == mint:
                                trade_id, t_tg_id, _, _, entry_px, amount_tokens = trade
                                profit_usd = round((cur_px - entry_px) * amount_tokens, 2)
                                c.execute("UPDATE trades SET sell_price = ?, profit_usdc = ?, sell_tx = ? WHERE id = ?", (cur_px, profit_usd, sell_sig, trade_id))
                                if t_tg_id:
                                    c.execute("UPDATE users SET balance_usdc = balance_usdc + ? WHERE telegram_id = ?", (profit_usd, t_tg_id))
                        conn.commit()
                        add_log(f"✅ Ордер {sym} закрыт. Прибыль зачислена! Tx: {sell_sig[:10]}...")
                        cooldown_until = now_ts + 30

                if not IS_TRADING_ACTIVE or now_ts < cooldown_until:
                    conn.close()
                    await asyncio.sleep(20)
                    continue

                best_candidate = None
                max_dip = 0.0
                for sym, p_list in history.items():
                    if len(p_list) >= 5:
                        avg_p = sum(p_list) / len(p_list)
                        cur_p = p_list[-1]
                        dip = (avg_p - cur_p) / avg_p
                        if dip >= 0.003 and dip > max_dip:
                            max_dip = dip
                            best_candidate = (sym, cur_p)

                if best_candidate:
                    c.execute("SELECT telegram_id FROM users WHERE balance_usdc >= ?", (MIN_BOT_BALANCE_USDC,))
                    active_users = c.fetchall()
                    
                    if active_users:
                        sym, cur_p = best_candidate
                        mint = WATCHLIST[sym]["mint"]
                        add_log(f"🚀 Сигнал {sym}. Копируем сделку для {len(active_users)} юзеров...")
                        buy_sig = await execute_jupiter_swap(session, USDC_MINT, mint, TRADE_AMOUNT_RAW, signer)
                        if buy_sig:
                            bought_tokens = TRADE_AMOUNT_USDC / cur_p
                            for u in active_users:
                                t_tg_id = u[0]
                                c.execute("""INSERT INTO trades 
                                    (telegram_id, token_symbol, token_mint, buy_price, amount_tokens, sell_price, profit_usdc, buy_tx, sell_tx, timestamp)
                                    VALUES (?, ?, ?, ?, ?, 0, 0, ?, '', ?)""",
                                    (t_tg_id, sym, mint, cur_p, bought_tokens, buy_sig, datetime.now().strftime('%H:%M:%S')))
                            conn.commit()
                            cooldown_until = now_ts + 30

                conn.close()
            except Exception:
                pass
            await asyncio.sleep(20)

# --- ВОРКЕР 2: ПАРАЛЛЕЛЬНЫЙ (Meme Sniper Mode) ---
async def meme_sniper_worker():
    global IS_MEME_ACTIVE
    signer = get_signer_keypair()
    if not signer:
        return

    SOL_MINT = "So11111111111111111111111111111111111111112"

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                if not IS_MEME_ACTIVE:
                    await asyncio.sleep(5)
                    continue

                conn = sqlite3.connect(DB_FILE)
                c = conn.cursor()
                c.execute("SELECT id, telegram_id, token_symbol, token_mint, buy_price, amount_tokens, half_taken FROM trades WHERE sell_price = 0 AND token_symbol LIKE 'MEME-%' LIMIT 1")
                active_meme = c.fetchone()

                if active_meme:
                    trade_id, t_tg_id, sym, mint, entry_sol_price, amount_tokens, half_taken = active_meme
                    url = f"https://api.dexscreener.com/latest/dex/tokens/{mint}"
                    headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
                    async with session.get(url, headers=headers, timeout=5) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            pairs = data.get("pairs", [])
                            if pairs:
                                cur_sol_price = float(pairs[0].get("priceNative", entry_sol_price))
                                multiplier = cur_sol_price / entry_sol_price if entry_sol_price > 0 else 1.0
                                pnl_pct = (cur_sol_price - entry_sol_price) / entry_sol_price
                                current_total_sol = MEME_ALLOCATION_SOL * multiplier

                                if current_total_sol >= 0.5 and half_taken == 0:
                                    half_tokens_raw = int((amount_tokens / 2) * (10 ** 6))
                                    sell_sig = await execute_jupiter_swap(session, mint, USDC_MINT, half_tokens_raw, signer, slippage_bps=300, priority_fee=100000)
                                    if sell_sig:
                                        c.execute("UPDATE trades SET half_taken = 1, amount_tokens = amount_tokens / 2 WHERE id = ?", (trade_id,))
                                        conn.commit()

                                elif multiplier >= MEME_TARGET_MULTIPLIER or pnl_pct <= -MEME_STOP_LOSS_PCT:
                                    rem_tokens_raw = int(amount_tokens * (10 ** 6))
                                    sell_sig = await execute_jupiter_swap(session, mint, SOL_MINT, rem_tokens_raw, signer, slippage_bps=300, priority_fee=100000)
                                    if sell_sig:
                                        profit_sol = current_total_sol - MEME_ALLOCATION_SOL
                                        profit_usd = profit_sol * 150
                                        c.execute("UPDATE trades SET sell_price = ?, profit_usdc = ?, sell_tx = ? WHERE id = ?", (cur_sol_price, profit_usd, sell_sig, trade_id))
                                        if t_tg_id:
                                            c.execute("UPDATE users SET balance_usdc = balance_usdc + ? WHERE telegram_id = ?", (profit_usd, t_tg_id))
                                        conn.commit()
                else:
                    c.execute("SELECT telegram_id FROM users WHERE balance_usdc >= ?", (MIN_BOT_BALANCE_USDC,))
                    active_users = c.fetchall()
                    if active_users:
                        search_url = "https://api.dexscreener.com/latest/dex/search?q=SOL/USDC"
                        headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
                        
                        async with session.get(search_url, headers=headers, timeout=6) as resp:
                            if resp.status == 200:
                                data = await resp.json()
                                pairs = data.get("pairs", [])
                                target_mint, target_sym, init_price = None, None, 0.0

                                for pair in pairs:
                                    if pair.get("chainId") == "solana":
                                        m_address = pair.get("baseToken", {}).get("address")
                                        p_native = float(pair.get("priceNative", 0.0) or 0.0)
                                        if m_address and m_address != SOL_MINT and p_native > 0:
                                            target_mint = m_address
                                            target_sym = f"MEME-{pair.get('baseToken', {}).get('symbol')}"
                                            init_price = p_native
                                            break

                                if target_mint:
                                    amount_lamports = int(MEME_ALLOCATION_SOL * 1_000_000_000)
                                    buy_sig = await execute_jupiter_swap(session, SOL_MINT, target_mint, amount_lamports, signer, slippage_bps=500, priority_fee=100000)
                                    if buy_sig:
                                        bought_tokens = MEME_ALLOCATION_SOL / init_price if init_price > 0 else 1000.0
                                        for u in active_users:
                                            c.execute("""INSERT INTO trades 
                                                (telegram_id, token_symbol, token_mint, buy_price, amount_tokens, sell_price, profit_usdc, buy_tx, sell_tx, timestamp, half_taken)
                                                VALUES (?, ?, ?, ?, ?, 0, 0, ?, '', ?, 0)""",
                                                (u[0], target_sym, target_mint, init_price, bought_tokens, buy_sig, datetime.now().strftime('%H:%M:%S')))
                                        conn.commit()

                conn.close()
            except Exception:
                pass
            await asyncio.sleep(15)

# --- ИНТЕРФЕЙС HTML СО СТРОГОЙ ЗАЩИТОЙ ---
RAW_HTML = r"""<!DOCTYPE html>
<html lang="en" data-ui-language="en" data-copy-version="clean-english-v1" data-discovery-version="bounded-solana-v1">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no, viewport-fit=cover">
    <title>Zer0Life Multi-Asset</title>
    <script src="https://telegram.org/js/telegram-web-app.js"></script>
    <style>
        * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
        body { background: #07080c; color: #f1f5f9; font-family: -apple-system, BlinkMacSystemFont, sans-serif; margin: 0; padding: 0; width: 100vw; height: 100vh; overflow: hidden; }
        
        #splash-screen {
            position: fixed; top: 0; left: 0; width: 100vw; height: 100vh;
            background: linear-gradient(180deg, rgba(7,8,12,0.2) 0%, rgba(7,8,12,0.95) 100%), url('__SPLASH_URL__') center/cover;
            display: flex; flex-direction: column; justify-content: flex-end; align-items: center;
            padding: 24px; padding-bottom: calc(34px + env(safe-area-inset-bottom, 16px)); z-index: 9999;
        }
        .brand-title { font-size: 34px; font-weight: 900; color: #fff; }
        .brand-run { font-size: 42px; font-weight: 900; color: #22c55e; margin-top: -6px; }
        .start-btn { background: #22c55e; color: #000; border: none; width: 100%; max-width: 400px; padding: 18px; border-radius: 18px; font-weight: 800; font-size: 16px; cursor: pointer; margin-top: 18px; }

        .card { background: #0e111a; border: 1px solid #1c2235; border-radius: 20px; padding: 16px; margin-bottom: 12px; }
        .grid-stats { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-top: 10px; }
        .stat-box { background: #080a11; border: 1px solid #161b2c; border-radius: 12px; padding: 10px; }
        .stat-label { color: #64748b; font-size: 10px; font-weight: 600; text-transform: uppercase; }
        .stat-val { color: #f8fafc; font-size: 15px; font-weight: 800; margin-top: 2px; font-family: monospace; }
        .log-console { background: #05070a; border: 1px solid #141926; border-radius: 16px; padding: 12px; font-family: monospace; font-size: 11px; color: #4ade80; height: 190px; overflow-y: auto; white-space: pre-line; line-height: 1.5; }

        .control-bar { display: flex; gap: 10px; margin-bottom: 12px; }
        .btn-ctl {
            flex: 1; padding: 12px 14px; border-radius: 14px; font-weight: 800; font-size: 12px;
            cursor: pointer; display: flex; align-items: center; justify-content: center; gap: 6px;
            border: none; transition: transform 0.15s ease;
        }
        .btn-ctl:active { transform: scale(0.97); }
        .btn-toggle-on { background: rgba(34, 197, 94, 0.15); border: 1px solid rgba(34, 197, 94, 0.4); color: #4ade80; }
        .btn-toggle-off { background: rgba(239, 68, 68, 0.15); border: 1px solid rgba(239, 68, 68, 0.4); color: #f87171; }
        .btn-refresh { background: #151a27; border: 1px solid #232c42; color: #94a3b8; max-width: 110px; }

        .btn-sell-all {
            background: rgba(239, 68, 68, 0.15); border: 1px solid rgba(239, 68, 68, 0.4); color: #f87171;
            width: 100%; padding: 14px; border-radius: 14px; font-weight: 800; font-size: 13px;
            cursor: pointer; display: flex; align-items: center; justify-content: center; gap: 6px;
            margin-bottom: 12px; transition: transform 0.15s ease;
        }

        .order-card {
            background: #080a11; border: 1px solid #192033; border-radius: 14px;
            padding: 12px; margin-bottom: 8px; font-family: monospace; font-size: 12px;
        }
        .order-top { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }
        .order-badge { padding: 3px 8px; border-radius: 6px; font-size: 10px; font-weight: 800; }
        .badge-open { background: rgba(59, 130, 246, 0.15); color: #60a5fa; border: 1px solid rgba(59, 130, 246, 0.3); }
        .badge-closed { background: rgba(34, 197, 94, 0.15); color: #4ade80; border: 1px solid rgba(34, 197, 94, 0.3); }
        .order-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 6px; margin: 6px 0; color: #94a3b8; font-size: 11px; }
        .order-tx-row { display: flex; gap: 8px; margin-top: 8px; padding-top: 8px; border-top: 1px dashed #161c2d; font-size: 10px; }
        .tx-link { color: #60a5fa; text-decoration: none; display: inline-flex; align-items: center; gap: 2px; }

        .deposit-modal { background: #0b0d14; border: 1px solid #1c2333; border-radius: 24px; padding: 18px; margin-bottom: 12px;}
        .banner-limit { background: rgba(22, 101, 52, 0.12); border: 1px solid rgba(34, 197, 94, 0.3); border-radius: 14px; padding: 12px; text-align: center; margin-bottom: 12px; }
        .btn-green-main { background: #22c55e; color: #000; border: none; width: 100%; padding: 14px; border-radius: 14px; font-size: 14px; font-weight: 800; cursor: pointer; margin-top: 8px; }
        .qr-section { text-align: center; margin: 16px 0; }
        .qr-img { background: #fff; padding: 10px; border-radius: 16px; }
        .address-box { background: #06080e; border: 1px solid #192030; border-radius: 12px; padding: 12px; color: #22c55e; font-family: monospace; font-size: 11px; word-break: break-all; text-align: center; margin-bottom: 10px; }
        .btn-copy { background: #111522; color: #e2e8f0; border: 1px solid #232c42; width: 100%; padding: 12px; border-radius: 12px; font-size: 13px; font-weight: 700; cursor: pointer; margin-bottom: 18px; }
        .sig-input { background: #06080e; border: 1px solid #1c2436; border-radius: 12px; padding: 14px; width: 100%; color: #fff; font-family: monospace; font-size: 12px; outline: none; margin-bottom: 12px; }
        .btn-verify { background: #86efac; color: #000; border: none; width: 100%; padding: 15px; border-radius: 14px; font-size: 14px; font-weight: 800; cursor: pointer; }

        .wallet-tabs { display: flex; gap: 8px; margin-bottom: 14px; background: #06080e; padding: 4px; border-radius: 14px; border: 1px solid #192030; }
        .wallet-tab { flex: 1; padding: 10px; text-align: center; font-size: 13px; font-weight: 700; border-radius: 10px; cursor: pointer; color: #64748b; border: none; background: transparent; }
        .wallet-tab.active { background: #192033; color: #fff; }

        .token-selector { display: flex; gap: 8px; margin-bottom: 12px; }
        .token-btn { flex: 1; padding: 10px; border-radius: 12px; border: 1px solid #1c2436; background: #06080e; color: #64748b; font-weight: 700; font-size: 12px; cursor: pointer; text-align: center; }
        .token-btn.active { border-color: #22c55e; color: #22c55e; background: rgba(34, 197, 94, 0.1); }

        .bottom-nav { position: fixed; bottom: 0; left: 0; right: 0; background: rgba(10, 12, 18, 0.95); backdrop-filter: blur(20px); border-top: 1px solid #192033; padding: 12px 16px; padding-bottom: calc(12px + env(safe-area-inset-bottom, 16px)); display: flex; justify-content: space-around; z-index: 900; }
        .nav-btn { background: transparent; border: none; color: #64748b; font-size: 11px; font-weight: 600; cursor: pointer; }
        .nav-btn.active { color: #22c55e; }
        .tab-panel { display: none; height: 100vh; overflow-y: auto; padding: 16px; padding-bottom: 125px; }
        .tab-panel.active { display: block; }
        :root { color-scheme: dark; }
        body { background:#080d14; color:#e7edf5; }
        #app .tab-panel { box-sizing:border-box; height:var(--tg-viewport-stable-height,100dvh); max-width:640px; margin:auto; padding:24px 20px 115px; }
        #app .card { background:#101823!important; border:1px solid #223043!important; border-radius:20px; padding:20px; margin-bottom:16px; }
        #app .banner-limit { background:#10221c!important; border:1px solid #214337!important; padding:14px; border-radius:16px; }
        .page-heading { margin:0 0 22px; display:flex; justify-content:space-between; align-items:center; }
        .page-heading h1 { margin:0;font-size:26px;font-weight:750;letter-spacing:-.7px; }
        .eyebrow { color:#8798ae;font-size:12px;margin:0 0 6px; }
        .section-note { color:#94a5bb;font-size:13px;line-height:1.65; }
        .quick-actions { display:grid;grid-template-columns:1fr 1fr;gap:10px;margin:18px 0; }
        #app .stat-box { background:#0b111b;border:1px solid #1d2b3d;padding:16px;border-radius:14px; }
        #app .stat-label { color:#8495ad;letter-spacing:.7px;font-size:10px; }
        #app .stat-val { font-size:18px;line-height:1.5;overflow-wrap:anywhere; }
        #app .btn-copy { background:#172334;border:1px solid #2a3a50;border-radius:13px; padding:14px;font-size:13px; }
        #app .btn-green-main { background:#3bd19a;color:#061b12;font-weight:750;border-radius:14px; }
        #app .btn-sell-all { background:transparent;color:#e49b9b;border:1px solid #694343;border-radius:12px;font-size:12px; }
        .bottom-nav { box-sizing:border-box;background:#0c1420;border-color:#243247;gap:6px;padding:12px 16px calc(12px + env(safe-area-inset-bottom,0px)); }
        .nav-btn { flex:1;padding:10px 3px;border-radius:12px;font-size:11px; }
        .nav-btn span { display:block;margin-bottom:4px;font-size:19px; }
        .nav-btn.active { background:#163126;color:#66dfac; }
        .settings-label { display:block;font-size:13px;color:#a7b6c9;margin:16px 0 8px; }
        details summary { color:#b6c6da;font-size:13px;cursor:pointer;padding:10px 0; }
        .status-output { white-space:pre-wrap;font-size:13px;color:#b2c4da;line-height:1.6; }
    </style>
</head>
<body>
    <div id="splash-screen">
        <div class="brand-title">ZERØLIFE</div>
        <div class="brand-run">MULTI-ASSET</div>
        <button id="launch-terminal" class="start-btn" onclick="openTerminal()">Launch Terminal</button>
    </div>

    <section id="account-signup" class="card" data-account-version="telegram-passkey-v1" data-entry-fix="signup-above-splash-v1" style="display:none;position:fixed;inset:7vh 18px auto;z-index:10001;max-width:460px;max-height:82vh;overflow-y:auto;-webkit-overflow-scrolling:touch;margin:auto">
        <h2>Create your account</h2><p class="muted">Your verified Telegram account is your login. No email or password required.</p>
        <label for="account-nickname">Nickname</label>
        <input id="account-nickname" class="sig-input" autocomplete="nickname" minlength="3" maxlength="24" pattern="[A-Za-z0-9_]{3,24}" placeholder="Your nickname">
        <p class="muted">3–24 letters, numbers or underscores. Your balance stays linked to your Telegram account, not this name.</p>
        <label style="display:flex;gap:10px"><input id="account-risk" type="checkbox" style="width:auto">I understand trading can lose money and returns are not guaranteed.</label>
        <button id="account-create" class="btn-green-main" onclick="createAccount()">Create account</button>
        <p id="account-error" class="status-output" role="status"></p>
    </section>
    <div id="app" data-ui-version="console-ai-v1" style="display: none;">
        <div id="tab-terminal" class="tab-panel active">
            <header class="page-heading"><div><p class="eyebrow">ZERØLIFE · Personal account</p><h1>Overview</h1></div></header>
            <div class="banner-limit" style="border-color:#ef4444; background:#2a1014; margin-bottom:12px;">
                <div style="color:#4ade80; font-size:13px; font-weight:800;">Your account · USDC / SOL</div>
                <div id="wallet-runtime-status" style="color:#cbd5e1; font-size:11px; margin-top:4px;">Personal funds. Trading carries risk.</div>
                <div id="account-pending-operation-details" role="status" style="display:none;color:#cbd5e1;font-size:11px;margin-top:8px;overflow-wrap:anywhere;white-space:pre-wrap;"></div>
            </div>
            <div class="control-bar">
                <button id="btn-toggle" class="btn-ctl btn-toggle-off" onclick="toggleTrading()">
                    <span id="toggle-dot">●</span>
                    <span id="toggle-text">Autopilot: OFF</span>
                </button>
                <button id="btn-refresh-top" class="btn-ctl btn-refresh" onclick="manualRefresh()">
                    <span id="refresh-icon">🔄</span>
                    <span>Refresh</span>
                </button>
            </div>

            <button class="btn-sell-all" onclick="closeAllPositions()">
                <span>⚠️ Close all my positions</span>
            </button>

            <div class="card" style="border: 1px solid rgba(34, 197, 94, 0.3); background: linear-gradient(135deg, #0e111a 0%, #0a150d 100%);">
                <div style="font-size: 13px; font-weight: 800; color: #22c55e; margin-bottom: 8px; display: flex; justify-content: space-between;">
                    <span>Available funds</span>
                    <span id="display-username" style="color: #fff; font-size: 12px;"></span>
                </div>
                <div class="grid-stats">
                    <div class="stat-box"><div class="stat-label">USDC · available</div><div class="stat-val" id="port-balance" style="color: #fff;">Loading...</div></div>
                    <div class="stat-box"><div class="stat-label">SOL · available</div><div class="stat-val" id="port-withdrawable" style="color: #22c55e;">0 SOL</div></div>
                </div>
                <div style="margin-top: 8px; display: flex; justify-content: space-between; align-items: center; background: #06080e; padding: 8px 12px; border-radius: 10px;">
                    <div style="font-size: 11px; color: #94a3b8;">Your realized PnL:</div>
                    <div id="port-pnl" style="font-size: 13px; font-weight: 900; font-family: monospace; color: #4ade80;">+$0.00 (0.0%)</div>
                </div>
            </div>

            <div class="card" id="meme-sniper-card">
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
                    <div>
                        <div style="font-size: 17px; font-weight: 800; color: #fff;">Meme Sniper · SOL</div>
                        <div class="section-note">Memecoin trades funded in SOL</div>
                    </div>
                    <button id="btn-meme-toggle" class="btn-ctl btn-toggle-off" style="padding: 8px 12px; font-size: 11px;" disabled onclick="toggleMemeTrading()">
                        <span id="meme-toggle-text">NOT CONNECTED</span>
                    </button>
                </div>
                <div class="grid-stats">
                    <div class="stat-box"><div class="stat-label">Available SOL</div><div class="stat-val" id="sniper-sol-balance">Loading...</div></div>
                    <div class="stat-box"><div class="stat-label">Order size</div><div class="stat-val" id="sniper-order-size">Loading...</div></div>
                </div>
                <p class="section-note">Screened Solana candidates · 1 position max</p>
                <div id="discovery-status" class="section-note">Candidate scan pending.</div>
                <button class="action-btn" id="scan-candidates" onclick="scanCandidates()">Scan candidates</button>
                <details><summary>Candidate shortlist</summary><div id="candidate-list" class="section-note" style="white-space:pre-line">No verified shortlist yet.</div></details>
                <p id="sniper-pnl" class="section-note">Realized SOL PnL: loading...</p>
            </div>

            <div class="card">
                <div style="display: flex; justify-content: space-between; align-items: center;">
                    <div>
                        <div style="font-size: 17px; font-weight: 800; color: #fff;">Core strategy · USDC</div>
                        <div class="section-note">AI · BUY / SELL / HOLD</div>
                        <div id="strategy-assets" style="font-size: 11px; color: #64748b; margin-top: 2px;">Loading assets...</div>
                    </div>
                    <div id="user-bal-small" style="font-size: 12px; color: #22c55e; font-family: monospace; font-weight: 700;">$0.00 USDC</div>
                </div>
                <div class="grid-stats">
                    <div class="stat-box"><div class="stat-label">Order size</div><div class="stat-val" id="strategy-order-size">Loading...</div></div>
                    <div class="stat-box"><div class="stat-label">Slippage</div><div class="stat-val" style="color: #60a5fa;">0.40%</div></div>
                    <div class="stat-box"><div class="stat-label">Take Profit</div><div class="stat-val" style="color: #4ade80;">+3.50%</div></div>
                    <div class="stat-box"><div class="stat-label">Stop Loss</div><div class="stat-val" style="color: #f87171;">-2.00%</div></div>
                </div>
            </div>

            <div class="card">
                <div style="font-size: 12px; font-weight: 700; color: #94a3b8; margin-bottom: 6px;">ACTIVITY LOG</div>
                <div style="font-size:11px;color:#94a3b8;margin-bottom:8px;">AI decisions are not confirmed trades · times in your timezone</div>
                <div id="log-box" class="log-console" data-activity-version="decision-history-v1">Loading activity...</div>
            </div>
        </div>

        <div id="tab-stats" class="tab-panel">
            <div class="card">
                <div style="display: flex; justify-content: space-between; align-items: center;">
                    <div>
                        <div style="font-size: 12px; color: #94a3b8; text-transform: uppercase;">Your realized returns</div>
                        <div id="pnl-val" style="font-size: 26px; font-weight: 900; color: #4ade80; font-family: monospace; margin-top: 4px;">+$0.00 USDC</div>
                    </div>
                    <div style="text-align: right;">
                        <div style="font-size: 11px; color: #94a3b8;">Closed trades</div>
                        <div id="closed-count" style="font-size: 18px; font-weight: 800; color: #fff; font-family: monospace;">0</div>
                    </div>
                </div>
            </div>

            <div class="card">
                <div style="font-size: 13px; font-weight: 800; color: #f8fafc; margin-bottom: 12px;">YOUR TRADES</div>
                <div id="history-box" style="color: #64748b; font-size: 11px; text-align: center; padding: 20px;">Loading trades...</div>
            </div>
        </div>

        <div id="tab-deposit" class="tab-panel">
            <div class="deposit-modal">
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 14px;">
                    <div style="font-size: 17px; font-weight: 800; color: #fff;" id="wallet-modal-title">Deposit Crypto (USDC)</div>
                    <span style="color: #64748b; cursor: pointer; font-size: 18px;" onclick="gotoTab('terminal', document.querySelectorAll('.nav-btn')[0])">✕</span>
                </div>

                <div class="wallet-tabs">
                    <button class="wallet-tab active" id="wtab-deposit" onclick="switchWalletTab('deposit')">Deposit</button>
                    <button class="wallet-tab" id="wtab-withdraw" onclick="switchWalletTab('withdraw')">Withdraw</button>
                </div>

                <div id="section-deposit">
                    <div class="banner-limit">
                        <div style="color:#4ade80; font-weight:800;">USDC / SOL · Solana mainnet</div>
                        <div style="font-size:11px; margin-top:6px;">Verify your wallet once. Send funds, then paste the transaction signature. No Memo required.</div>
                    </div>
                    <button class="btn-copy" onclick="linkDepositWallet()">Verify your wallet</button>
                    <a id="wallet-verification-link" hidden target="_blank" rel="noopener noreferrer">Open wallet verification · hold to copy link</a>
                    <div id="deposit-invoice-details">
                        <div class="address-box" id="wallet-text">__DEPOSIT_WALLET__</div>
                        <button class="btn-copy" onclick="copyWallet()">Copy deposit address</button>
                    </div>
                    <input id="dep-signature" class="sig-input" placeholder="Transaction signature or Solscan link">
                    <button class="btn-verify" id="btn-verify-action" onclick="verifyOnchainDeposit()">Verify deposit</button>
                </div>

                <div id="section-withdraw" style="display: none;">
                    <div id="chain-withdraw-balances" style="font-size:12px; margin-bottom:8px;">Loading balance...</div>
                    <div class="token-selector">
                        <button class="token-btn active" id="wtoken-usdc" onclick="selectWithdrawToken('USDC')">USDC</button>
                        <button class="token-btn" id="wtoken-sol" onclick="selectWithdrawToken('SOL')">Solana (SOL)</button>
                    </div>
                    <input id="withdraw-address" class="sig-input" placeholder="Recipient wallet address">
                    <input id="withdraw-amount" type="number" step="any" class="sig-input" placeholder="Amount">
                    <button class="btn-green-main" id="btn-withdraw-action" onclick="secureWithdraw()">Withdraw</button>
                </div>
            </div>

            <div class="card" style="background: #0b0d14; border-color: #1c2333;">
                <div style="font-size: 13px; font-weight: 800; color: #fff; margin-bottom: 12px;">Transactions</div>
                <div id="wallet-history-box" style="font-size: 11px; color: #64748b; text-align: center;">No transactions yet.</div>
            </div>
            <div id="admin-custody-panel" data-fee-ui-version="inline-reserve-v1" class="card" style="display:none;">
                <div style="font-weight:800;">Operator tools</div>
                <div style="font-size:11px;margin-top:8px;">Fees are paid from a separate operator reserve.</div>
                <button class="btn-copy" onclick="checkLegacyOperations()">Check legacy operations</button>
                <div id="legacy-operation-details" style="font-size:12px;overflow-wrap:anywhere;white-space:pre-wrap;margin-top:8px;"></div>
                <button class="btn-copy" onclick="checkSharedPendingOperations()">Check current shared-wallet lock (read-only)</button>
                <div id="shared-pending-operation-details" style="font-size:12px;overflow-wrap:anywhere;white-space:pre-wrap;margin-top:8px;"></div>
                <h3>Fee reserve · SOL</h3>
                <div id="fee-status" class="status-output">Reserve not checked yet.</div>
                <p class="muted">Allocation reduces your available SOL balance. No on-chain transfer. Only your funds may be allocated.</p>
                <label for="fee-amount">Your SOL to allocate</label>
                <input id="fee-amount" class="sig-input" type="text" inputmode="decimal" value="0.01" autocomplete="off">
                <label style="display:flex;gap:10px;align-items:flex-start;font-size:12px;"><input id="fee-confirm" type="checkbox" style="width:auto;flex:0 0 auto;">I confirm these SOL belong to me, not to customers.</label>
                <button id="fee-submit" class="btn-copy" onclick="setupFeeReserve()">Allocate my SOL to fees</button>
                <div id="fee-result" class="status-output" role="status"></div>
                <button class="btn-copy" onclick="importOwnOpeningBalance()">Import my legacy balance</button>
            </div>
        </div>
        <div id="tab-trading" class="tab-panel">
            <header class="page-heading"><div><p class="eyebrow">Your capital only</p><h1>Trading</h1></div></header>
            <div id="trading-controls"></div>
            <div class="card"><h3 style="margin-top:0">AI activity</h3>
                <p id="ai-status" class="status-output">Checking AI status...</p>
                <button class="btn-copy" onclick="previewAI()">Check AI · no trade</button>
                <p id="ai-decisions" class="status-output"></p>
            </div>
            <div id="trading-strategy"></div>
            <div id="trading-sniper"></div>
            <div class="card">
                <button class="btn-copy" onclick="checkTradingReadiness()">Check readiness · no trade</button>
                <div id="trading-readiness" class="status-output">Trading involves risk. No guaranteed returns.</div>
            </div>
            <details><summary>Logs &amp; position controls</summary><div id="trading-activity"></div></details>
        </div>
        <div id="tab-settings" class="tab-panel">
            <header class="page-heading"><div><p class="eyebrow">Account preferences</p><h1>Settings</h1></div></header>
            <div class="card" id="passkey-settings"><h3>Withdrawal protection</h3>
                <p id="passkey-state" class="status-output">Passkey not checked.</p>
                <button class="btn-copy" onclick="setupPasskey()">Set up / add passkey</button>
                <button class="btn-copy" onclick="setupPasskey(true)">Recover using a saved code</button>
                <p id="passkey-progress" class="status-output" role="status"></p>
                <a id="passkey-browser-link" hidden target="_blank" rel="noopener noreferrer">Open verification · hold to copy link</a>
                <details><summary>Face ID &amp; account recovery</summary>
                    <p class="muted">Your device verifies you using Face ID, Touch ID or another system verification method. We verify the passkey signature, not a face photo. Face-only verification cannot be enforced on every device.</p>
                    <p class="muted">Every withdrawal needs your code and a fresh passkey approval bound to its amount, token, recipient and request ID. Changing any field requires another approval.</p>
                    <p class="muted">Save the one-time recovery codes displayed in the secure browser. Add a backup passkey before losing your device. Recovery requires your Telegram account, withdrawal code and an unused recovery code; it replaces the old passkeys. No PIN-only reset is available.</p>
                </details>
            </div>
            <div class="card"><h3 style="margin-top:0">Order sizes</h3>
                <p class="section-note">Saving settings does not start trading.</p>
                <label class="settings-label" for="settings-order">USDC per core order</label>
                <input id="settings-order" class="sig-input" type="number" min="0.000001" step="0.000001" value="1">
                <label class="settings-label" for="settings-sniper-order">SOL per sniper order</label>
                <input id="settings-sniper-order" class="sig-input" type="number" min="0.000000001" step="0.000000001" value="0.005">
                <button class="btn-green-main" onclick="saveTradingSettings()">Save order sizes</button>
                <p id="settings-budget" class="section-note"></p>
            </div>
            <details id="strategy-help"><summary>Strategy limits &amp; risks</summary>
                <p class="section-note">Core trades use your USDC; sniper trades use your SOL. Other users' funds and the operator fee reserve are never your trading capital. Automatic conversion or profit allocation between strategies is not implemented.</p>
                <p class="section-note">Core: SOL, JUP, RAY, PYTH; up to 5 positions. Sniper: up to 1 SOL-funded position. Every 15 minutes a bounded DexScreener profiles/search scan checks up to 90 Solana mints, screens up to 20 and sends at most 8 current candidates plus held assets to AI. This is not every project or a profit forecast; profiles alone do not establish meme status.</p>
                <p class="section-note">Sniper entry filters: approved DEX, at least $100k pool liquidity and 24h volume, 200 daily trades, pool age 24h+, extreme-pump exclusion, standard SPL mint with no mint/freeze authority. Fresh eligibility is required for every buy. Scores rank liquidity/activity, not expected profit. Holder concentration, LP locks and every contract/market risk are not fully checked. Standard mint checks do not guarantee token safety.</p>
                <p class="section-note">AI makes decisions after you enable a mode. Orders require personal funds, permitted assets, at least 5 price samples, a fresh decision and stable pricing. Only one shared-wallet operation can be pending at a time.</p>
                <p class="section-note">TP/SL are checked approximately every 20 seconds independently of AI availability. Model failures stop new AI orders. After a server restart both modes pause and need manual reactivation.</p>
                <p class="section-note">TP/SL are execution conditions, not guaranteed fill prices. Fees, slippage and liquidity affect results. You can lose money; returns are not guaranteed.</p>
            </details>
            <details><summary>Diagnostics</summary><p id="worker-status" class="status-output">Loading...</p></details>
            <details><summary>AI connection &amp; costs</summary>
                <p class="section-note">Set OPENAI_API_KEY only in Render Environment. Never enter secrets in chat or app fields. OPENAI_MODEL defaults to gpt-4.1-mini.</p>
                <p class="section-note">Default: one analysis every 15 minutes, up to 96 attempted API calls per UTC day across the service, including failed calls. OpenAI API billing is separate and paid by the operator. Only market prices are sent to the model, not keys, Telegram identities or personal balances.</p>
            </details>
            <details id="operator-settings" style="display:none;"><summary>Operator tools · fees &amp; migration</summary><div id="settings-admin"></div></details>
        </div>
    </div>

    <div id="nav" class="bottom-nav" style="display: none;">
        <button class="nav-btn active" onclick="gotoTab('terminal', this)"><span>◈</span>Overview</button>
        <button class="nav-btn" onclick="gotoTab('trading', this)"><span>↗</span>Trading</button>
        <button class="nav-btn" onclick="gotoTab('deposit', this)"><span>⇄</span>Wallet</button>
        <button class="nav-btn" onclick="gotoTab('settings', this)"><span>⚙</span>Settings</button>
    </div>

    <script>
        let tg;
        try {
            tg = window.Telegram?.WebApp;
            if (tg) { tg.expand(); tg.ready(); }
        } catch (e) {}

        let bioManager = null;
        try {
            bioManager = window.Telegram?.WebApp?.BiometricManager;
            if (bioManager) { bioManager.init(); }
        } catch (e) {}

        let currentWithdrawToken = 'USDC';
        let tradingActive = false;
        let currentTradingSettings = {order_usdc:1};
        let settingsOrderDirty=false;
        let aiConfigured=false;
        let sniperActive=false;
        document.getElementById('settings-order').addEventListener('input',()=>{settingsOrderDirty=true;});
        document.getElementById('settings-sniper-order').addEventListener('input',()=>{settingsOrderDirty=true;});
        document.getElementById('trading-controls').appendChild(document.querySelector('.control-bar'));
        document.getElementById('trading-strategy').appendChild(document.getElementById('user-bal-small').closest('.card'));
        document.getElementById('trading-activity').appendChild(document.querySelector('.btn-sell-all'));
        document.getElementById('trading-activity').appendChild(document.getElementById('log-box').closest('.card'));
        document.getElementById('settings-admin').appendChild(document.getElementById('admin-custody-panel'));
        document.getElementById('trading-sniper').appendChild(document.getElementById('meme-sniper-card'));
        const overviewActions = document.createElement('div');
        overviewActions.className='quick-actions';
        for (const [label,tab,index] of [['Deposit / withdraw','deposit',2],['Trading dashboard','trading',1]]) {
            const button=document.createElement('button');button.className='btn-copy';button.textContent=label;
            button.dataset.targetTab=tab;
            button.onclick=()=>gotoTab(tab,document.querySelectorAll('.nav-btn')[index]);overviewActions.appendChild(button);
        }
        document.getElementById('tab-terminal').appendChild(overviewActions);
        const resultsButton=document.createElement('button');
        resultsButton.className='btn-copy';resultsButton.textContent='Trade history & results';
        resultsButton.onclick=()=>gotoTab('stats',document.querySelectorAll('.nav-btn')[0]);
        document.getElementById('tab-terminal').appendChild(resultsButton);
        async function saveTradingSettings() {
            try {
                const response=await apiFetch('/api/trading_settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({order_usdc:document.getElementById('settings-order').value,order_sol:document.getElementById('settings-sniper-order').value})});
                const result=await response.json();if(!response.ok||!result.success)throw new Error(result.message);
                settingsOrderDirty=false;alert(result.message);await refresh();
            } catch(e) {alert(e.message);}
        }
        async function checkTradingReadiness() {
            const output=document.getElementById('trading-readiness');output.textContent='Checking funds, fees and market APIs…';
            try {
                const response=await apiFetch('/api/trading_readiness',{method:'POST'});
                const result=await response.json();output.textContent=result.message||'Check incomplete.';
            } catch(e) {output.textContent='Server did not respond. This check does not start trading.';}
        }
        async function previewAI() {
            const box=document.getElementById('ai-decisions');box.textContent='Waiting for AI… No trades are sent.';
            try {
                const response=await apiFetch('/api/ai_preview',{method:'POST'});
                const result=await response.json();
                if(!response.ok||!result.success)throw new Error(result.message);
                box.textContent=Object.entries(result.decisions).map(([mode,d])=>(mode==='main'?'USDC':'SOL')+': '+d.action+' '+(d.symbol||'')+' — '+d.reason).join('\n');
                await refresh();
            } catch(e) {box.textContent=e.message;}
        }
        let withdrawSending = false;
        async function scanCandidates() {
            const button=document.getElementById('scan-candidates');
            button.disabled=true;
            try {
                const response=await apiFetch('/api/meme_scan',{method:'POST'});
                const data=await response.json();
                if(!response.ok||!data.success)throw new Error(data.message||'Scan unavailable.');
                document.getElementById('discovery-status').textContent=data.message;
            }catch(e){document.getElementById('discovery-status').textContent=e.message||'Scan unavailable.';}
            finally{button.disabled=false;}
        }

        async function linkDepositWallet() {
            try {
                const response = await apiFetch('/api/wallet_link', {method:'POST'});
                const result = await response.json();
                if (!response.ok || !result.success) throw new Error(result.message);
                if (result.address) { alert('Your verified wallet: ' + result.address); return; }
                const link=document.getElementById('wallet-verification-link');
                link.href=result.url;link.hidden=false;
                if (tg?.openLink) tg.openLink(result.url); else window.open(result.url, '_blank', 'noopener');
            } catch(e) { alert(e.message || 'Could not open wallet verification.'); }
        }
        async function checkLegacyOperations() {
            const box = document.getElementById('legacy-operation-details');
            box.textContent = 'Checking the network. No funds are sent…';
            try {
                const response = await apiFetch('/api/admin/status');
                const result = await response.json();
                if (!response.ok || !result.success) throw new Error(result.message || 'Check unavailable.');
                const lines = [];
                if (result.legacy_check_error) lines.push(result.legacy_check_error);
                if (result.balance_check_error) lines.push(result.balance_check_error);
                if (result.unassigned_usdc !== undefined) lines.push('Unassigned legacy funds: ' + result.unassigned_usdc + ' USDC; ' + result.unassigned_sol + ' SOL. Not part of the new personal account balance.');
                const operations = result.legacy_operations || [];
                if (!operations.length && !result.legacy_check_error) lines.push('No pending legacy operations.');
                operations.forEach(op => lines.push('Legacy operation: ' + op.kind + ' / ' + op.state + '\nSignature: ' + op.signature + '\nDate: ' + op.timestamp));
                (result.retired_legacy_operations || []).forEach(op => lines.push('Archived: ' + op.kind + ' / ' + op.state + '\nSignature: ' + op.signature + '\nOutcome unconfirmed. No refund or repeat broadcast.'));
                if (operations.length) lines.push('Outcome unknown. Record preserved; no repeat broadcast. Do not delete it manually.');
                box.textContent = lines.join('\n\n');
                operations.forEach(op => {
                    const a = document.createElement('a');
                    a.href = 'https://solscan.io/tx/' + encodeURIComponent(op.signature);
                    a.textContent = '\nView legacy transaction on Solscan';
                    a.target = '_blank'; a.rel = 'noopener noreferrer';
                    box.appendChild(a);
                    const button = document.createElement('button');
                    button.className = 'btn-copy';
                    button.textContent = 'Check expiry and archive this record';
                    button.onclick = () => retireLegacyOperation(op.signature);
                    box.appendChild(button);
                });
                await refresh();
            } catch(e) { box.textContent = e.message || 'Could not check legacy operations.'; }
        }
        async function checkSharedPendingOperations() {
            const box = document.getElementById('shared-pending-operation-details');
            box.textContent = 'Reading the current shared-wallet lock (read-only)…';
            try {
                const response = await apiFetch('/api/admin/pending_operations');
                const result = await response.json();
                if (!response.ok || !result.success) throw new Error(result.message || 'Check unavailable.');
                const operations = result.operations || [];
                if (!operations.length) {
                    box.textContent = 'No active shared-wallet operation.';
                    return;
                }
                box.textContent = operations.map(op => {
                    const seconds = Number(op.created_at);
                    const created = Number.isFinite(seconds) && seconds > 0
                        ? new Date(seconds * 1000).toLocaleString() : 'unknown';
                    return 'Operation: ' + (op.kind || 'unknown') + ' / ' + (op.state || 'unknown')
                        + '\nCreated: ' + created
                        + '\nSignature: ' + (op.signature || 'not recorded');
                }).join('\n\n') + '\n\nRead-only check: no transaction was sent or changed.';
            } catch(e) {
                box.textContent = e.message || 'Could not read the current shared-wallet lock.';
            }
        }
        async function retireLegacyOperation(signature) {
            if (!confirm('For a legacy personal operation only. The server checks transaction absence, blockhash expiry and nonce history. Without evidence nothing changes. The record stays unconfirmed; this is not a refund or a successful trade. Continue?')) return;
            const pin = prompt('Enter your confirmation PIN:');
            if (!pin) return;
            await adminLedgerAction('/api/admin/retire_legacy', {signature, pin, confirm_archive_unknown:true});
            await checkLegacyOperations();
        }
        function renderFeeStatus(fees) {
            if(!fees)return;
            document.getElementById('fee-status').textContent=
                'Reserve: '+fees.reserve_sol+' SOL\nRequired minimum: '+fees.minimum_sol+
                ' SOL\nYour available balance: '+fees.own_available_sol+' SOL\n'+
                (fees.sufficient?'Reserve sufficient for readiness checks.':'Reserve insufficient.');
        }
        async function setupFeeReserve() {
            const resultBox=document.getElementById('fee-result');
            const button=document.getElementById('fee-submit');
            const raw=document.getElementById('fee-amount').value.trim();
            if(!/^\d+([.,]\d{1,9})?$/.test(raw)||Number(raw.replace(',','.'))<=0){
                resultBox.textContent='Enter a positive SOL amount, e.g. 0.01 or 0,01.';return;
            }
            if(!document.getElementById('fee-confirm').checked){
                resultBox.textContent='Confirm these are your SOL, not customer funds.';return;
            }
            if(button.disabled)return;
            button.disabled=true;resultBox.textContent='Checking and allocating your SOL…';
            try{
                const response=await apiFetch('/api/admin/fee_reserve',{method:'POST',headers:{'Content-Type':'application/json'},
                    body:JSON.stringify({amount_sol:raw.replace(',','.'),confirm_own_unassigned:true})});
                const result=await response.json();
                if(!response.ok||!result.success)throw new Error(result.message||'Allocation not confirmed.');
                renderFeeStatus(result.operator_fees);
                document.getElementById('fee-confirm').checked=false;
                resultBox.textContent=result.message+'\nReserve after allocation: '+result.operator_fees.reserve_sol+' SOL.';
                await refresh();
            }catch(e){
                resultBox.textContent=(e.message||'No response from server.')+
                    '\nDo not retry blindly. Refresh and check the actual reserve.';
            }finally{button.disabled=false;}
        }
        async function importOwnOpeningBalance() {
            const token = prompt('Asset to import from your legacy funds: USDC or SOL', 'USDC');
            if (!token) return;
            const amount = prompt('Exact amount of your own legacy funds (not a virtual balance):');
            if (!amount || !confirm('Confirm this amount contains no other users’ funds. Import is allowed only before public opening, once per asset.')) return;
            await adminLedgerAction('/api/admin/opening_balance', {token, amount, confirm_own_unassigned:true});
        }
        async function adminLedgerAction(url, body) {
            try {
                const response = await apiFetch(url, {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
                const result = await response.json();
                alert(result.message);
                await refresh();
            } catch(e) { alert('Could not update the ledger. Check current state; do not retry blindly.'); }
        }

        function getUserData() {
            if(window.ZRL_WEB)return window.webSession?.user||null;
            const user = tg?.initDataUnsafe?.user;
            if (!tg?.initData || !user?.id) return null;
            return { id: Number(user.id), username: user.username || user.first_name || 'User' };
        }

        async function apiFetch(url, options = {}) {
            if(window.ZRL_WEB){
                if(!window.webSession?.user)throw Error('Sign in to the web terminal.');
                return fetch(url,{...options,credentials:'same-origin',
                    headers:{...(options.headers||{}),'X-ZRL-CSRF':window.webSession.csrf}});
            }
            if (!tg?.initData) throw new Error('Open this app through Telegram.');
            const headers = Object.assign({}, options.headers || {}, { 'X-Telegram-Init-Data': tg.initData });
            return fetch(url, Object.assign({}, options, { headers }));
        }

        let accountState = null;
        async function loadAccount() {
            const response=await apiFetch('/api/account'),data=await response.json();
            if(!response.ok||!data.success)throw Error(data.message||'Could not verify account.');
            accountState=data;
            document.getElementById('passkey-state').textContent=data.has_passkey?
                'Enabled · '+data.passkey_count+' passkey(s) · '+data.recovery_codes_remaining+' recovery codes remaining.':
                'Not configured. Set up a passkey before withdrawing.';
            return data;
        }
        async function openTerminal() {
            if(window.ZRL_WEB&&!window.webSession?.user){
                document.getElementById('web-login').hidden=false;return;
            }
            const user = getUserData();
            if (!user) { alert("Open this app from Telegram."); return; }
            const launch=document.getElementById('launch-terminal');
            if(launch.disabled)return;
            launch.disabled=true;launch.textContent='Checking account...';
            try {
                const profile=await loadAccount();
                if(!profile.registered){
                    document.getElementById('account-nickname').value=user.username.replace(/[^A-Za-z0-9_]/g,'').slice(0,24);
                    document.getElementById('account-signup').style.display='block';
                    return;
                }
                await finishAccount();
            } catch(e){alert(e.message||'Could not verify account.');}
            finally{launch.disabled=false;launch.textContent='Launch Terminal';}
        }
        async function createAccount() {
            const button=document.getElementById('account-create');button.disabled=true;
            try{
                const response=await apiFetch('/api/account/register',{method:'POST',headers:{'Content-Type':'application/json'},
                    body:JSON.stringify({nickname:document.getElementById('account-nickname').value,
                        accept_risk:document.getElementById('account-risk').checked})});
                const data=await response.json();if(!response.ok||!data.success)throw Error(data.message||'Could not create account.');
                accountState=data;document.getElementById('account-signup').style.display='none';
                await finishAccount();
            }catch(e){document.getElementById('account-error').textContent=e.message;document.getElementById('account-signup').style.display='block';}
            finally{button.disabled=false;}
        }
        async function finishAccount(){
            try{
                const res = await apiFetch('/api/security_status');
                const state = await res.json();
                if (!res.ok || !state.success) throw new Error(state.message || 'Could not check account security.');
                if (!state.has_pin && !(await setupPasswordProtection())) return;
                proceedToTerminal();
                if(!accountState?.has_passkey){
                    gotoTab('settings',document.querySelectorAll('.nav-btn')[3]);
                    document.getElementById('passkey-progress').textContent='Next: set up your device passkey for secure withdrawals.';
                }
            } catch (e) {
                alert(e.message || "Could not verify authorization. Reopen the app through Telegram.");
            }
        }

        async function passkeyTicket(body){
            const response=await apiFetch('/api/account/passkey',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
            const data=await response.json();if(!response.ok||!data.success)throw Error(data.message||'Could not start verification.');
            const link=document.getElementById('passkey-browser-link');link.href=data.url;link.hidden=false;
            document.getElementById('passkey-progress').textContent='Complete verification in the browser, then return here. No funds are sent by the browser.';
            if(tg?.openLink)tg.openLink(data.url);else window.open(data.url,'_blank','noopener,noreferrer');
            return data.ticket;
        }
        async function waitPasskey(ticket,phase){
            for(let i=0;i<100;i++){
                await new Promise(resolve=>setTimeout(resolve,3000));
                const response=await apiFetch('/api/account/passkey_status?ticket='+encodeURIComponent(ticket)),data=await response.json();
                if(!response.ok||!data.success)throw Error(data.message||'Could not check verification.');
                if(data.phase===phase&&(phase==='enrolled'||!data.expired))return;
                if(data.used||data.expired)throw Error('Verification expired or was replaced. Start again.');
            }
            throw Error('Verification timed out. Start again when ready.');
        }
        let passkeySetupBusy=false;
        async function setupPasskey(recover=false){
            if(passkeySetupBusy)return;
            const pin=prompt('Enter your withdrawal confirmation code:');if(!pin)return;
            const recovery_code=recover?prompt('Enter one unused recovery code saved during passkey registration:'):null;
            if(recover&&!recovery_code)return;
            passkeySetupBusy=true;
            try{
                const ticket=await passkeyTicket({purpose:recover?'recover':'enroll',pin,recovery_code});
                await waitPasskey(ticket,'enrolled');await loadAccount();
                document.getElementById('passkey-progress').textContent='Passkey registered. Save the recovery codes shown in the browser.';
                document.getElementById('passkey-browser-link').hidden=true;
            }catch(e){document.getElementById('passkey-progress').textContent=e.message;}
            finally{passkeySetupBusy=false;}
        }

        async function setupPasswordProtection() {
            const pwd = prompt("Create a withdrawal confirmation code (at least 6 characters):");
            if (!pwd || pwd.length < 6) {
                alert("The code must contain at least 6 characters.");
                return false;
            }
            try {
                const res = await apiFetch('/api/set_security', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ pin: pwd })
                });
                const data = await res.json();
                if (!res.ok || !data.success) throw new Error(data.message || 'Could not save your code.');
                alert("Withdrawal code saved.");
                return true;
            } catch (e) {
                alert(e.message || "Could not save your code.");
                return false;
            }
        }

        function proceedToTerminal() {
            document.getElementById('splash-screen').style.display = 'none';
            document.getElementById('app').style.display = 'block';
            document.getElementById('nav').style.display = 'flex';
            refresh();
        }

        function gotoTab(id, btn) {
            document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
            document.querySelectorAll('.nav-btn').forEach(b => b.classList.remove('active'));
            const target = document.getElementById('tab-' + id);
            if (target) target.classList.add('active');
            if (btn) btn.classList.add('active');
        }

        function switchWalletTab(tab) {
            const isDep = tab === 'deposit';
            document.getElementById('wtab-deposit').className = isDep ? 'wallet-tab active' : 'wallet-tab';
            document.getElementById('wtab-withdraw').className = !isDep ? 'wallet-tab active' : 'wallet-tab';
            document.getElementById('section-deposit').style.display = isDep ? 'block' : 'none';
            document.getElementById('section-withdraw').style.display = !isDep ? 'block' : 'none';
            document.getElementById('wallet-modal-title').innerText = isDep ? 'Deposit Crypto (USDC)' : 'Withdraw Crypto';
        }

        function selectWithdrawToken(token) {
            currentWithdrawToken = token;
            document.getElementById('wtoken-usdc').className = token === 'USDC' ? 'token-btn active' : 'token-btn';
            document.getElementById('wtoken-sol').className = token === 'SOL' ? 'token-btn active' : 'token-btn';
        }

        async function closeAllPositions() {
            if (!confirm("Sell all your open positions?")) return;
            try {
                const user = getUserData();
                const res = await apiFetch('/api/close_all', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({confirm: true})
                });
                const d = await res.json();
                alert(d.message);
                refresh();
            } catch (e) { alert("Connection error."); }
        }

        async function secureWithdraw() {
            if (withdrawSending) return;
            const addr = document.getElementById('withdraw-address').value.trim();
            const amount = document.getElementById('withdraw-amount').value.trim();
            if (!addr || !amount) { alert('Enter an address and amount.'); return; }
            if (!confirm('Send real funds: ' + amount + ' ' + currentWithdrawToken + ' to ' + addr + '?')) return;
            const enteredPin = prompt("Enter your withdrawal confirmation code:");
            if (!enteredPin) return;
            await submitWithdraw(enteredPin);
        }

        async function submitWithdraw(enteredPin) {
            const token = currentWithdrawToken;
            const addr = document.getElementById('withdraw-address').value.trim();
            const amount = document.getElementById('withdraw-amount').value.trim();
            if (!addr || !amount) { alert("Enter an address and amount."); return; }
            if (withdrawSending) return;
            withdrawSending = true;
            const intent = JSON.stringify({token, recipient_address: addr, amount});
            const old = JSON.parse(localStorage.getItem('withdraw_intent') || 'null');
            const requestId = old && old.intent === intent ? old.id : crypto.randomUUID();
            localStorage.setItem('withdraw_intent', JSON.stringify({intent, id: requestId}));

            const btn = document.getElementById('btn-withdraw-action');
            btn.innerText = "Processing on-chain...";
            btn.disabled = true;

            try {
                const ticket=await passkeyTicket({purpose:'withdraw',token,recipient_address:addr,
                    amount,pin:enteredPin,request_id:requestId});
                await waitPasskey(ticket,'approved');
                const res = await apiFetch('/api/withdraw', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ token, recipient_address: addr, amount: amount, pin: enteredPin, request_id: requestId, passkey_ticket:ticket })
                });
                const d = await res.json();
                alert(d.message);
                if (d.success) {
                    document.getElementById('withdraw-address').value = '';
                    document.getElementById('withdraw-amount').value = '';
                    refresh();
                }
                if (d.state === 'COMPLETED' || d.state === 'FAILED') localStorage.removeItem('withdraw_intent');
            } catch (e) { alert(e.message || "Network error."); }
            finally {
                withdrawSending = false;
                btn.innerText = "Withdraw 🚀";
                btn.disabled = false;
            }
        }

        async function toggleTrading() {
            let size = '1';
            if (!tradingActive) {
                size = String(currentTradingSettings.order_usdc || 1);
                if (!size || !confirm('Start trading on your own account? Order size: ' + size + ' USDC. You can lose money; returns are not guaranteed.')) return;
            }
            try {
                const response = await apiFetch('/api/toggle_trading', {
                    method: 'POST', headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({trade_amount_usdc: size})
                });
                const data = await response.json();
                if (!data.success) alert(data.message);
                await refresh();
            } catch(e) { alert('Could not change the trading mode.'); }
        }

        async function toggleMemeTrading() {
            if(!sniperActive&&!confirm('Start autonomous AI sniper with your SOL? You can lose money. Order size is set in Settings.'))return;
            try {
                const response=await apiFetch('/api/toggle_sniper',{method:'POST'});
                const result=await response.json();if(!result.success)alert(result.message);await refresh();
            }catch(e){alert('Sniper mode unchanged: no server response.');}
        }

        function updateToggleUI(isActive) {
            tradingActive = !!isActive;
            const btn = document.getElementById('btn-toggle');
            if (btn) {
                if(window.ZRL_WEB)btn.disabled=!window.webAccessActive;
                btn.className = isActive ? "btn-ctl btn-toggle-on" : "btn-ctl btn-toggle-off";
                document.getElementById('toggle-text').innerText = isActive ? "Autopilot: ON" : "Autopilot: OFF";
            }
        }

        function updateMemeToggleUI(isActive) {
            const btn = document.getElementById('btn-meme-toggle');
            if (btn) {
                btn.className = isActive ? "btn-ctl btn-toggle-on" : "btn-ctl btn-toggle-off";
                sniperActive=!!isActive;
                btn.disabled = !aiConfigured;
                document.getElementById('meme-toggle-text').innerText = !aiConfigured?"NOT CONNECTED":isActive?"AI: ON":"AI: OFF";
            }
        }

        async function manualRefresh() {
            const icon = document.getElementById('refresh-icon');
            if (icon) {
                icon.style.display = "inline-block";
                icon.style.transform = "rotate(360deg)";
                icon.style.transition = "transform 0.5s ease";
            }
            await refresh();
            setTimeout(() => { 
                if (icon) { icon.style.transform = "rotate(0deg)"; icon.style.transition = "none"; }
            }, 500);
        }

        function toggleDepositDetails() {
            document.getElementById('deposit-details').style.display = 'block';
            document.getElementById('btn-show-addr').style.display = 'none';
        }

        function copyWallet() {
            const addr = document.getElementById('wallet-text').innerText;
            navigator.clipboard.writeText(addr).then(() => alert("Address copied."));
        }

        async function verifyOnchainDeposit() {
            const sig = document.getElementById('dep-signature').value.trim();
            if (!sig) { alert("Paste the transaction signature."); return; }

            const btn = document.getElementById('btn-verify-action');
            btn.innerText = "Verifying...";
            btn.disabled = true;

            try {
                const res = await apiFetch('/api/verify_deposit', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ tx_signature: sig })
                });
                const d = await res.json();
                alert(d.message);
                if (d.success) {
                    document.getElementById('dep-signature').value = "";
                    refresh();
                }
            } catch (e) { alert("Connection error."); }
            finally {
                btn.innerText = "Verify deposit";
                btn.disabled = window.ZRL_WEB ? !window.webAccessActive : false;
            }
        }

        async function refresh() {
            try {
                const l = await apiFetch('/api/logs').then(r => r.json()).catch(() => ({success:false}));
                if (l.success) {
                    const logBox = document.getElementById('log-box');
                    if (logBox) {
                        const entries = Array.isArray(l.entries) ? l.entries : [];
                        const lines = entries.map(e => {
                            const stamp = new Date(e.created_at * 1000).toLocaleString();
                            return stamp + ' [' + e.category + '] ' + e.message +
                                (e.signature ? '\nTx: ' + e.signature : '');
                        });
                        logBox.textContent = lines.length ?
                            [l.activity_status, ...lines].filter(Boolean).join('\n\n') :
                            (Array.isArray(l.logs) && l.logs.length ? l.logs.join('\n') :
                             'Waiting for activity. No completed trade is implied.');
                        logBox.style.overflowWrap = 'anywhere';
                    }
                    if (l.trading_active !== undefined) updateToggleUI(l.trading_active);
                    if (l.sniper_active !== undefined) updateMemeToggleUI(l.sniper_active);
                } else {
                    document.getElementById('log-box').textContent =
                        'Activity log unavailable. Refresh or sign in again; no trade outcome is confirmed here.';
                }

                const user = getUserData();
                if (!user) return;
                const customNick = accountState?.nickname || user.username;
                const params = new URLSearchParams({ username: customNick });
                const s = await apiFetch('/api/stats?' + params.toString()).then(r => r.json());
                
                if (s.success) {
                    if(!window.ZRL_WEB&&!s.is_admin){
                        document.querySelectorAll('.nav-btn')[1].style.display='none';
                        document.querySelectorAll('[data-target-tab="trading"]').forEach(b=>b.style.display='none');
                        document.getElementById('tab-trading').style.display='none';
                    }
                    const flags = s.wallet_status || {};
                    document.getElementById('wallet-runtime-status').textContent =
                        !flags.wallet_configured ? 'Server key does not match the wallet address.' :
                        !flags.persistent_storage ? 'Operations blocked: Persistent Disk /var/data required.' :
                        !flags.finance_allowed ? 'Public deposits are not open yet.' :
                        s.operation_pending ? 'Your operation is pending. Do not resend.' :
                        'Personal funds. Trading carries risk.';
                    const pendingDetails = document.getElementById('account-pending-operation-details');
                    const pendingOperations = s.pending_operations || [];
                    if (pendingDetails) {
                        if (pendingOperations.length) {
                            pendingDetails.style.display = 'block';
                            pendingDetails.textContent = pendingOperations.map(op => {
                                const seconds = Number(op.created_at);
                                const created = Number.isFinite(seconds) && seconds > 0
                                    ? new Date(seconds * 1000).toLocaleString() : 'unknown';
                                return 'Operation: ' + (op.kind || 'unknown') + ' / ' + (op.state || 'unknown')
                                    + '\nCreated: ' + created
                                    + '\nSignature: ' + (op.signature || 'not recorded');
                            }).join('\n\n') + '\n\nDo not resend this operation.';
                        } else {
                            pendingDetails.textContent = '';
                            pendingDetails.style.display = 'none';
                        }
                    }
                    document.getElementById('admin-custody-panel').style.display = s.is_admin ? 'block' : 'none';
                    if(s.is_admin)renderFeeStatus(s.operator_fees);
                    document.getElementById('operator-settings').style.display=s.is_admin?'block':'none';
                    updateToggleUI(s.trading_active);
                    currentTradingSettings=s.trading_settings||{order_usdc:1};
                    const ai=s.ai_status||{};
                    aiConfigured=!!ai.configured;
                    updateMemeToggleUI(s.sniper_active);
                    document.getElementById('ai-status').textContent=aiConfigured?
                        ai.model+' · '+Math.round(ai.interval_seconds/60)+' min interval'+(ai.last_analyzed_at?' · analyzed':' · connection unverified'):
                        'AI not connected. Operator setup required.';
                    if(ai.last_decisions)document.getElementById('ai-decisions').textContent='Last analysis: '+new Date(ai.last_analyzed_at*1000).toLocaleString('en-GB')+'\n'+Object.entries(ai.last_decisions).map(([mode,d])=>(mode==='main'?'USDC':'SOL')+': '+d.action+' '+((ai.asset_labels||{})[d.symbol]||d.symbol||'')+' — '+d.reason).join('\n');
                    const discovery=ai.discovery||{};
                    document.getElementById('discovery-status').textContent=discovery.entry_paused?discovery.message:
                        discovery.checked_at?(discovery.eligible||0)+' screened · '+(discovery.scanned||0)+' scanned · refreshed '+new Date(discovery.checked_at*1000).toLocaleTimeString('en-GB'):discovery.message||'Candidate scan pending.';
                    document.getElementById('candidate-list').textContent=(ai.sniper_assets||[]).map(code=>{
                        const row=(discovery.candidates||[]).find(item=>item.code===code);
                        return ((ai.asset_labels||{})[code]||code)+(row?' · liquidity $'+Math.round(row.liquidity_usd).toLocaleString('en-US'):' · held asset');
                    }).join('\n')||'No verified shortlist yet.';
                    const sniper=s.sniper_settings||{order_sol:.005};
                    document.getElementById('sniper-order-size').textContent=sniper.order_sol+' SOL';
                    const sniperInput=document.getElementById('settings-sniper-order');
                    if(!settingsOrderDirty&&document.activeElement!==sniperInput)sniperInput.value=sniper.order_sol;
                    document.getElementById('sniper-pnl').textContent='Realized PnL: '+Number(s.profit_sol||0).toFixed(9)+' SOL';
                    document.getElementById('strategy-order-size').textContent=currentTradingSettings.order_usdc.toFixed(2)+' USDC';
                    document.getElementById('strategy-assets').textContent=(currentTradingSettings.assets||[]).filter(symbol=>!(ai.sniper_assets||['WIF']).includes(symbol)).join(' · ');
                    const settingsInput=document.getElementById('settings-order');
                    if(!settingsOrderDirty&&document.activeElement!==settingsInput)settingsInput.value=currentTradingSettings.order_usdc;
                    document.getElementById('settings-budget').textContent='Saved: '+currentTradingSettings.order_usdc+' USDC · up to '+(currentTradingSettings.max_positions||5)+' positions, limited by available funds.';
                    document.getElementById('worker-status').textContent=s.worker_error?'Last worker error: '+s.worker_error:'Autopilot: '+(s.trading_active?'ON':'OFF')+'. Readiness check is in Trading.';
                    document.getElementById('chain-withdraw-balances').textContent = 'Available: ' + s.user_balance.toFixed(6) + ' USDC | ' + s.sol_balance.toFixed(9) + ' SOL. Fees paid by operator reserve.';
                    const userEl = document.getElementById('display-username');
                    if (userEl) userEl.innerText = "@" + s.username;
                    
                    const p = s.profit_usdc ? s.profit_usdc : 0;
                    document.getElementById('pnl-val').innerText = (p >= 0 ? "+$" : "-$") + Math.abs(p).toFixed(2) + " USDC";
                    document.getElementById('closed-count').innerText = s.closed_count ? s.closed_count : 0;
                    
                    const dep = s.total_deposited ? s.total_deposited : 0;
                    const bal = s.user_balance ? s.user_balance : 0;
                    const wth = s.withdrawable ? s.withdrawable : 0;

                    document.getElementById('port-balance').innerText = bal.toFixed(6) + ' USDC';
                    document.getElementById('port-withdrawable').innerText = s.sol_balance.toFixed(6) + ' SOL';
                    document.getElementById('sniper-sol-balance').textContent=s.sol_balance.toFixed(6)+' SOL';
                    
                    const userBalSmall = document.getElementById('user-bal-small');
                    if(userBalSmall) userBalSmall.innerText = "$" + bal.toFixed(2) + " USDC";
                    
                    const portPnlEl = document.getElementById('port-pnl');
                    const portPnlVal = s.total_pnl ? s.total_pnl : 0;
                    const portRoiVal = s.total_roi ? s.total_roi : 0;
                    const pnlSign = portPnlVal >= 0 ? "+" : "";
                    portPnlEl.innerText = pnlSign + "$" + portPnlVal.toFixed(2) + " (" + pnlSign + portRoiVal.toFixed(1) + "%)";
                    portPnlEl.style.color = portPnlVal >= 0 ? '#4ade80' : '#f87171';

                    if (s.wallet_history && s.wallet_history.length > 0) {
                        document.getElementById('wallet-history-box').innerHTML = s.wallet_history.map(h => {
                            const isDep = h.type === 'DEPOSIT';
                            const color = isDep ? '#4ade80' : '#f87171';
                            const sign = isDep ? '+' : '-';
                            const txLink = (h.tx && h.tx !== 'SIMULATED_TX' && h.tx.length > 20) 
                                ? `<a href="https://solscan.io/tx/${h.tx}" target="_blank" style="color: #60a5fa; text-decoration: none; font-size: 11px;">Tx ↗</a>` 
                                : `<span style="color: #64748b; font-size: 10px;">${h.status}</span>`;
                            
                            return `
                                <div style="display: flex; justify-content: space-between; align-items: center; padding: 10px 0; border-bottom: 1px solid #1c2333; text-align: left;">
                                    <div>
                                        <div style="color: ${color}; font-weight: 800; font-size: 13px;">${sign} ${h.amount}</div>
                                        <div style="font-size: 10px; color: #64748b; margin-top: 2px;">${h.time}</div>
                                    </div>
                                    <div style="text-align: right;">
                                        <div style="font-size: 10px; color: ${h.status === 'VERIFIED' || h.status === 'COMPLETED' ? '#4ade80' : '#facc15'}; margin-bottom: 2px;">${h.status}</div>
                                        ${txLink}
                                    </div>
                                </div>
                            `;
                        }).join('');
                    } else {
                        document.getElementById('wallet-history-box').innerHTML = "No transactions";
                    }

                    if (s.trades && s.trades.length > 0) {
                        document.getElementById('history-box').innerHTML = s.trades.map(t => {
                            const isClosed = t.status === 'CLOSED';
                            const pnlColor = Number(t.profit_units??t.profit_usdc) >= 0 ? '#4ade80' : '#f87171';
                            const tPnlSign = Number(t.profit_units??t.profit_usdc) >= 0 ? '+' : '';

                            return `
                                <div class="order-card">
                                    <div class="order-top">
                                        <div style="font-weight: 800; color: #fff; font-size: 13px;">${t.token_symbol} / ${t.funding_currency||'USDC'}</div>
                                        <div class="order-badge ${isClosed ? 'badge-closed' : 'badge-open'}">
                                            ${isClosed ? 'CLOSED' : 'OPEN'}
                                        </div>
                                    </div>
                                    <div class="order-grid">
                                        <div>Entry: <span style="color: #fff;">${t.buy_price.toPrecision(4)} ${t.funding_currency||'USDC'}</span></div>
                                        <div>Exit: <span style="color: #fff;">${isClosed ? t.sell_price.toPrecision(4)+' '+(t.funding_currency||'USDC') : 'Pending'}</span></div>
                                        <div>Quantity: <span style="color: #fff;">${t.amount_tokens.toFixed(3)}</span></div>
                                        <div>Time: <span style="color: #fff;">${t.timestamp}</span></div>
                                    </div>
                                    <div style="display: flex; justify-content: space-between; align-items: center; margin-top: 6px; padding-top: 6px; border-top: 1px solid #141b2a;">
                                        <div style="font-size: 11px; color: #64748b;">Result:</div>
                                        <div style="font-weight: 800; font-size: 13px; color: ${isClosed ? pnlColor : '#60a5fa'};">
                                            ${isClosed ? (Number(t.profit_units??t.profit_usdc).toFixed(t.funding_currency==='SOL'?9:2)+' '+(t.funding_currency||'USDC')+' ('+t.profit_pct.toFixed(2)+'%)') : 'Open position'}
                                        </div>
                                    </div>
                                    <div class="order-tx-row">
                                        ${t.buy_tx ? '<a class="tx-link" href="https://solscan.io/tx/' + t.buy_tx + '" target="_blank">↗ Buy transaction</a>' : ''}
                                        ${t.sell_tx ? '<a class="tx-link" style="color: #4ade80;" href="https://solscan.io/tx/' + t.sell_tx + '" target="_blank">↗ Sell transaction</a>' : ''}
                                    </div>
                                </div>
                            `;
                        }).join('');
                    }
                }
            } catch(e) {}
        }
        setInterval(refresh, 5000);
    </script>
</body>
</html>
"""

HTML_CONTENT = RAW_HTML.replace("__SPLASH_URL__", SPLASH_IMAGE_URL).replace("__DEPOSIT_WALLET__", SHARED_DEPOSIT_WALLET)

async def index_handler(request):
    page = HTML_CONTENT
    if request.path == "/terminal":
        panel = Path(__file__).with_name("web_login.html").read_text(encoding="utf-8")
        page = page.replace("</head>", "<script>window.ZRL_WEB=true;</script></head>")
        page = page.replace("</body>", panel + '<script src="/web-terminal.js"></script></body>')
    return web.Response(text=page, content_type='text/html', headers={"Cache-Control": "no-store",
        "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "frame-ancestors 'none'; object-src 'none'; base-uri 'none'"})

async def health_handler(request):
    runtime = request.app["wallet_runtime"]
    flags = runtime.readiness()
    return web.json_response({"status": "custody_beta", **flags,
        "build_commit": os.getenv("RENDER_GIT_COMMIT", ""),
        "deposit_parser_version": "phantom-authority-v1",
        "ai_configured": bool(os.getenv("OPENAI_API_KEY")),
        "web_terminal_version": "sol-access-v1",
        "trading_active": bool(runtime.ledger.active_users())})

async def api_get_logs(request):
    return web.json_response({"success": True, "logs": RECENT_LOGS, "trading_active": IS_TRADING_ACTIVE, "meme_active": IS_MEME_ACTIVE})

async def api_toggle_trading(request):
    global IS_TRADING_ACTIVE
    if CUSTODY_SAFE_MODE:
        return web.json_response({"success": False, "message": CUSTODY_PAUSE_MESSAGE}, status=503)
    IS_TRADING_ACTIVE = not IS_TRADING_ACTIVE
    add_log(f"⚙️ Автопилот Топ-5: {'ВКЛ' if IS_TRADING_ACTIVE else 'ВЫКЛ'}")
    return web.json_response({"success": True, "trading_active": IS_TRADING_ACTIVE})

async def api_toggle_meme(request):
    global IS_MEME_ACTIVE
    if CUSTODY_SAFE_MODE:
        return web.json_response({"success": False, "message": CUSTODY_PAUSE_MESSAGE}, status=503)
    IS_MEME_ACTIVE = not IS_MEME_ACTIVE
    add_log(f"🐸 Meme Sniper Mode: {'ВКЛ' if IS_MEME_ACTIVE else 'ВЫКЛ'}")
    return web.json_response({"success": True, "meme_active": IS_MEME_ACTIVE})

async def api_security_status(request):
    tg_id = int(request["telegram_user"]["id"])
    conn = sqlite3.connect(DB_FILE)
    try:
        c = conn.cursor()
        c.execute("SELECT pin_hash FROM user_security WHERE telegram_id = ?", (tg_id,))
        row = c.fetchone()
        return web.json_response({"success": True, "has_pin": bool(row and row[0])})
    finally:
        conn.close()

async def api_set_security(request):
    try:
        body = await request.json()
        tg_id = int(request["telegram_user"]["id"])
        pin = str(body.get("pin", ""))
        if len(pin) < 6 or len(pin) > 128:
            return web.json_response({"success": False, "message": "Код должен содержать от 6 до 128 символов."}, status=400)
        pin_hash = hash_pin(pin)
        conn = sqlite3.connect(DB_FILE, timeout=10)
        try:
            c = conn.cursor()
            c.execute("BEGIN IMMEDIATE")
            old = c.execute("SELECT pin_hash,pin_attempts,pin_locked_until FROM user_security WHERE telegram_id=?", (tg_id,)).fetchone()
            if old and old[0]:
                now = int(time.time())
                if int(old[2] or 0) > now:
                    return web.json_response({"success": False, "message": "PIN временно заблокирован."}, status=423)
                current_pin = str(body.get("current_pin", ""))
                if not current_pin:
                    return web.json_response({"success": False, "message": "Для изменения нужен текущий PIN."}, status=409)
                correct, _ = verify_pin_hash(current_pin, old[0]) if len(current_pin) <= 128 else (False, False)
                if not correct:
                    previous = 0 if old[2] and old[2] <= now else int(old[1] or 0)
                    attempts = previous + 1
                    until = now + PIN_LOCK_SECONDS if attempts >= PIN_MAX_ATTEMPTS else 0
                    c.execute("UPDATE user_security SET pin_attempts=?,pin_locked_until=? WHERE telegram_id=?", (attempts, until, tg_id))
                    conn.commit()
                    return web.json_response({"success": False, "message": "Неверный текущий PIN."}, status=423 if until else 401)
            c.execute("""INSERT INTO user_security (telegram_id, pin_hash, auth_type, biometric_enabled, pin_attempts, pin_locked_until)
                         VALUES (?, ?, 'PIN', 0, 0, 0)
                         ON CONFLICT(telegram_id) DO UPDATE SET pin_hash=excluded.pin_hash, auth_type='PIN', biometric_enabled=0, pin_attempts=0, pin_locked_until=0""",
                      (tg_id, pin_hash))
            conn.commit()
        finally:
            conn.close()
        add_log(f"🔒 PIN settings updated for Telegram user {tg_id}")
        return web.json_response({"success": True, "message": "Код подтверждения сохранён."})
    except Exception:
        return web.json_response({"success": False, "message": "Не удалось сохранить код."}, status=500)

async def api_verify_security(request):
    try:
        body = await request.json()
        tg_id = int(request["telegram_user"]["id"])
        valid, state = verify_user_pin(tg_id, str(body.get("pin", "")))
        return web.json_response({"success": valid, "locked": state == "locked"}, status=200 if valid else 401)
    except Exception:
        return web.json_response({"success": False}, status=500)

async def api_get_stats(request):
    tg_id = int(request["telegram_user"]["id"])
    username = request.query.get("username", request["telegram_user"].get("username", "User"))[:64]

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    
    c.execute("INSERT OR IGNORE INTO users (telegram_id, username, balance_usdc) VALUES (?, ?, 0.0)", (tg_id, username))
    c.execute("UPDATE users SET username = ? WHERE telegram_id = ?", (username, tg_id))
    conn.commit()

    c.execute("""SELECT id, token_symbol, buy_price, amount_tokens, sell_price, profit_usdc, buy_tx, sell_tx, timestamp 
                 FROM trades WHERE telegram_id = ? ORDER BY id DESC LIMIT 25""", (tg_id,))
    rows = c.fetchall()
    
    trades = []
    tot_profit = 0.0
    closed_count = 0
    for r in rows:
        buy_px = r[2]
        sell_px = r[4]
        is_closed = sell_px > 0
        pnl_pct = round(((sell_px - buy_px) / buy_px) * 100, 2) if is_closed else 0.0
        
        if is_closed:
            tot_profit += r[5]
            closed_count += 1

        trades.append({
            "id": r[0], "token_symbol": r[1], "buy_price": buy_px, "amount_tokens": r[3],
            "sell_price": sell_px, "profit_usdc": r[5], "profit_pct": pnl_pct,
            "buy_tx": r[6], "sell_tx": r[7], "timestamp": r[8],
            "status": "CLOSED" if is_closed else "OPEN"
        })

    c.execute("SELECT balance_usdc, username FROM users WHERE telegram_id = ?", (tg_id,))
    u_row = c.fetchone()
    user_bal = u_row[0] if u_row else 0.0
    db_username = u_row[1] if u_row else username

    c.execute("SELECT SUM(amount_usdc) FROM deposits WHERE telegram_id = ? AND status = 'VERIFIED'", (tg_id,))
    dep_row = c.fetchone()
    total_deposited = dep_row[0] if dep_row and dep_row[0] else 0.0

    c.execute("SELECT amount_usdc, tx_signature, timestamp, status FROM deposits WHERE telegram_id = ? ORDER BY id DESC LIMIT 15", (tg_id,))
    deps = [{"type": "DEPOSIT", "amount": f"${r[0]:.2f} USDC", "tx": r[1], "time": r[2][:19], "status": r[3]} for r in c.fetchall()]

    c.execute("SELECT amount, token, tx_signature, timestamp, status FROM withdrawals WHERE telegram_id = ? ORDER BY id DESC LIMIT 15", (tg_id,))
    withs = [{"type": "WITHDRAW", "amount": f"{r[0]} {r[1]}", "tx": r[2], "time": r[3][:19], "status": r[4]} for r in c.fetchall()]
    
    wallet_history = deps + withs
    wallet_history.sort(key=lambda x: x["time"], reverse=True)
    wallet_history = wallet_history[:20]

    conn.close()

    total_pnl = tot_profit
    total_roi = (total_pnl / total_deposited * 100) if total_deposited > 0 else 0.0
    withdrawable = 0.0 if CUSTODY_SAFE_MODE else max(0.0, user_bal - MIN_BOT_BALANCE_USDC)

    return web.json_response({
        "success": True, "profit_usdc": tot_profit, "closed_count": closed_count,
        "trades": trades, "user_balance": user_bal,
        "total_deposited": total_deposited, "total_pnl": total_pnl, "total_roi": total_roi,
        "withdrawable": withdrawable, "balance_reconciled": not CUSTODY_SAFE_MODE, "username": db_username,
        "wallet_history": wallet_history
    })

async def api_verify_deposit(request):
    try:
        if CUSTODY_SAFE_MODE:
            return web.json_response({"success": False, "message": CUSTODY_PAUSE_MESSAGE}, status=503)
        body = await request.json()
        tx = body.get("tx_signature", "").strip()
        tg_id = int(request["telegram_user"]["id"])
        username = body.get("username", "Unknown")

        if not tx or len(tx) < 60:
            return web.json_response({"success": False, "message": "Некорректная сигнатура транзакции."})

        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT id FROM deposits WHERE tx_signature = ?", (tx,))
        if c.fetchone():
            conn.close()
            return web.json_response({"success": False, "message": "Эта транзакция уже зачислена!"})

        async with aiohttp.ClientSession() as session:
            valid, amount_usdc, msg = await verify_solana_usdc_tx(session, tx, SHARED_DEPOSIT_WALLET)

        if not valid:
            conn.close()
            return web.json_response({"success": False, "message": f"Отказ: {msg}"})

        c.execute("INSERT INTO deposits (telegram_id, tx_signature, amount_usdc, status) VALUES (?, ?, ?, 'VERIFIED')", (tg_id, tx, amount_usdc))
        c.execute("UPDATE users SET balance_usdc = balance_usdc + ?, username = ? WHERE telegram_id = ?", (amount_usdc, username, tg_id))
        conn.commit()
        conn.close()

        add_log(f"📥 Зачислен депозит: +${amount_usdc} USDC для ID {tg_id}")
        return web.json_response({"success": True, "message": f"✅ Успешно! Зачислено ${amount_usdc} USDC на ваш счет."})
    except Exception as e:
        return web.json_response({"success": False, "message": f"Ошибка сервера: {str(e)}"})

async def api_close_all_positions(request):
    try:
        if CUSTODY_SAFE_MODE:
            return web.json_response({"success": False, "message": CUSTODY_PAUSE_MESSAGE}, status=503)
        body = await request.json()
        tg_id = int(request["telegram_user"]["id"])
        signer = get_signer_keypair()
        if not signer:
            return web.json_response({"success": False, "message": "Ошибка ключа сервера."})

        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT id, token_symbol, token_mint, buy_price, amount_tokens FROM trades WHERE sell_price = 0 AND telegram_id = ?", (tg_id,))
        open_trades = c.fetchall()

        if not open_trades:
            conn.close()
            return web.json_response({"success": True, "message": "Нет открытых позиций для закрытия."})

        closed_count = 0
        async with aiohttp.ClientSession() as session:
            prices = await get_token_prices(session)
            for trade in open_trades:
                trade_id, sym, mint, buy_price, amount_tokens = trade
                decimals = WATCHLIST.get(sym, {}).get("decimals", 6)
                raw_amount = int(amount_tokens * (10 ** decimals))
                
                sell_sig = await execute_jupiter_swap(session, mint, USDC_MINT, raw_amount, signer)
                if sell_sig:
                    cur_px = prices.get(sym, buy_price)
                    profit_usd = round((cur_px - buy_price) * amount_tokens, 2)
                    c.execute("UPDATE trades SET sell_price = ?, profit_usdc = ?, sell_tx = ? WHERE id = ?", (cur_px, profit_usd, sell_sig, trade_id))
                    c.execute("UPDATE users SET balance_usdc = balance_usdc + ? WHERE telegram_id = ?", (profit_usd, tg_id))
                    closed_count += 1

        conn.commit()
        conn.close()
        add_log(f"⚡ Экстренно закрыто позиций пользователем {tg_id}: {closed_count}")
        return web.json_response({"success": True, "message": f"✅ Закрыто позиций: {closed_count}. Балансы обновлены."})
    except Exception as e:
        return web.json_response({"success": False, "message": f"Ошибка при закрытии позиций: {str(e)}"})

async def api_withdraw_impl(request):
    try:
        if CUSTODY_SAFE_MODE:
            return web.json_response({"success": False, "message": CUSTODY_PAUSE_MESSAGE}, status=503)
        body = await request.json()
        tg_id = int(request["telegram_user"]["id"])
        token = str(body.get("token", "USDC")).upper()
        recipient = str(body.get("recipient_address", "")).strip()
        pin = str(body.get("pin", ""))
        if token not in {"USDC", "SOL"}:
            return web.json_response({"success": False, "message": "Поддерживаются только USDC и SOL."}, status=400)
        try:
            amount_decimal = Decimal(str(body.get("amount", "0")))
            Pubkey.from_string(recipient)
        except (InvalidOperation, ValueError):
            return web.json_response({"success": False, "message": "Неверный адрес или сумма."}, status=400)
        max_decimals = 6 if token == "USDC" else 9
        if (not amount_decimal.is_finite() or amount_decimal <= 0 or
                amount_decimal.normalize().as_tuple().exponent < -max_decimals):
            return web.json_response({"success": False, "message": "Проверьте сумму и число знаков после запятой."}, status=400)
        amount = float(amount_decimal)
        pin_ok, pin_state = verify_user_pin(tg_id, pin)
        if not pin_ok:
            status = 423 if pin_state == "locked" else 401
            message = "Слишком много попыток. Попробуйте через 15 минут." if pin_state == "locked" else "Неверный код подтверждения."
            return web.json_response({"success": False, "message": message}, status=status)

        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT balance_usdc FROM users WHERE telegram_id = ?", (tg_id,))
        u_row = c.fetchone()
        user_bal = u_row[0] if u_row else 0.0

        usdc_deduction = amount
        if token == "SOL":
            async with aiohttp.ClientSession() as session:
                prices = await get_token_prices(session)
                sol_price = prices.get("SOL", 0)
                if sol_price <= 0:
                    conn.close()
                    return web.json_response({"success": False, "message": "Не удалось получить текущий курс SOL."})
                usdc_deduction = amount * sol_price

        withdrawable = max(0.0, user_bal - MIN_BOT_BALANCE_USDC)
        if usdc_deduction > withdrawable:
            conn.close()
            return web.json_response({"success": False, "message": f"Сумма превышает доступную. Доступно: ${withdrawable:.2f}."})

        signer = get_signer_keypair()
        if not signer:
            conn.close()
            return web.json_response({"success": False, "message": "Внутренняя ошибка: серверный кошелек недоступен."})

        tx_signature = None
        async with aiohttp.ClientSession() as session:
            if token == "SOL":
                sig = await execute_native_sol_transfer(session, signer, recipient, amount)
                if not sig:
                    conn.close()
                    return web.json_response({"success": False, "message": "Ошибка отправки SOL в сеть."})
                tx_signature = sig
            elif token == "USDC":
                sig = await execute_spl_usdc_transfer(session, signer, recipient, amount_decimal)
                if not sig:
                    conn.close()
                    return web.json_response({"success": False, "message": "Ошибка отправки USDC в сеть Solana."})
                tx_signature = sig

        new_bal = user_bal - usdc_deduction
        c.execute("UPDATE users SET balance_usdc = ? WHERE telegram_id = ?", (new_bal, tg_id))
        
        c.execute("""INSERT INTO withdrawals (telegram_id, token, amount, recipient_address, tx_signature, status)
                     VALUES (?, ?, ?, ?, ?, 'COMPLETED')""", (tg_id, token, amount, recipient, tx_signature))
        conn.commit()
        conn.close()

        add_log(f"📤 Вывод {amount} {token} на {recipient[:6]}... выполнен. Tx: {tx_signature[:10]}")
        return web.json_response({"success": True, "message": f"✅ Успешно! Вывод отправлен в сеть. Tx: {tx_signature[:10]}..."})
    except Exception as e:
        add_log(f"⚠️ Ошибка вывода: {type(e).__name__}")
        return web.json_response({"success": False, "message": "Ошибка обработки вывода. Проверьте журнал сервера."}, status=500)

async def api_withdraw(request):
    telegram_id = int(request["telegram_user"]["id"])
    lock = WITHDRAW_LOCKS.setdefault(telegram_id, asyncio.Lock())
    async with lock:
        return await api_withdraw_impl(request)

async def telegram_webhook_handler(request):
    expected_secret = hmac.new(TELEGRAM_TOKEN.encode(), b"zerolife-webhook-v1", hashlib.sha256).hexdigest()
    if not TELEGRAM_TOKEN or not hmac.compare_digest(
            request.headers.get("X-Telegram-Bot-Api-Secret-Token", ""), expected_secret):
        return web.Response(status=403, text="Forbidden")
    try:
        data = await request.json()
        if "message" in data:
            message = data["message"]
            chat_id = message.get("chat", {}).get("id")
            text = message.get("text", "")
            username = message.get("from", {}).get("username", "Unknown")

            if text.startswith("/start") and chat_id and TELEGRAM_TOKEN:
                conn = sqlite3.connect(DB_FILE)
                c = conn.cursor()
                c.execute("""INSERT INTO users (telegram_id, username) 
                             VALUES (?, ?) 
                             ON CONFLICT(telegram_id) DO UPDATE SET username=excluded.username""", (chat_id, username))
                conn.commit()
                conn.close()

                keyboard = {"inline_keyboard": [[{"text": "🚀 Launch Terminal", "web_app": {"url": RENDER_URL}}]]}
                send_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
                payload = {
                    "chat_id": chat_id,
                    "text": "👋 Your **ZERØLIFE Multi-Asset & Meme Sniper** terminal is ready.",
                    "parse_mode": "Markdown", "reply_markup": keyboard
                }
                async with aiohttp.ClientSession() as session:
                    await session.post(send_url, json=payload)
        return web.Response(text="OK")
    except Exception:
        return web.Response(text="Error", status=500)

async def set_telegram_webhook():
    if not TELEGRAM_TOKEN or not RENDER_URL:
        return
    webhook_url = f"{RENDER_URL.rstrip('/')}/webhook"
    set_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setWebhook"
    secret = hmac.new(TELEGRAM_TOKEN.encode(), b"zerolife-webhook-v1", hashlib.sha256).hexdigest()
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
        try:
            async with session.post(set_url, json={"url": webhook_url, "secret_token": secret}) as response:
                result = await response.json()
                if response.status != 200 or not result.get("ok"):
                    add_log("Telegram webhook: настройка не подтверждена.")
        except Exception:
            add_log("Telegram webhook: сервис недоступен, сверка операций продолжает работать.")

async def main():
    init_db()
    app = web.Application(middlewares=[english_json_middleware, telegram_auth_middleware])
    runtime = CustodyRuntime(DB_FILE, SOLANA_RPC, ADMIN_TELEGRAM_ID, SHARED_DEPOSIT_WALLET,
        get_signer_keypair, verify_user_pin, get_token_prices, WATCHLIST, add_log, base_url=RENDER_URL)
    runtime.recent_logs = lambda: RECENT_LOGS
    runtime.accounts = AccountSecurity(runtime.ledger, RENDER_URL, verify_user_pin, runtime.wallet)
    runtime.web_access = WebAccess(runtime)
    app["wallet_runtime"] = runtime
    def wallet_handler(action):
        async def handler(request):
            return await runtime.handle(request, action)
        return handler
    def payment_handler(action):
        async def handler(request):
            return await runtime.public_handle(request, action)
        return handler
    payment_html = Path(__file__).with_name("deposit_payment.html").read_text(encoding="utf-8")
    async def payment_page(request):
        if not runtime.ledger.get_invoice(request.match_info["invoice"]):
            raise web.HTTPNotFound()
        return web.Response(text=payment_html, content_type="text/html", headers={
            "Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": "frame-ancestors 'none'; object-src 'none'; base-uri 'none'",
            "X-Content-Type-Options": "nosniff"})
    app.router.add_get('/', index_handler)
    app.router.add_get('/terminal', index_handler)
    async def web_script(request):
        return web.Response(text=Path(__file__).with_name("web_terminal.js").read_text(encoding="utf-8"),
                            content_type="application/javascript", headers={"Cache-Control":"no-store"})
    app.router.add_get('/web-terminal.js', web_script)
    app.router.add_get('/web-auth/me', runtime.web_access.public)
    app.router.add_post('/web-auth/start', runtime.web_access.public)
    app.router.add_post('/web-auth/finish', runtime.web_access.public)
    app.router.add_post('/web-auth/logout', runtime.web_access.public)
    app.router.add_get('/api/subscription', wallet_handler(runtime.web_access.status))
    app.router.add_post('/api/subscription/order', wallet_handler(runtime.web_access.order))
    app.router.add_post('/api/subscription/verify', wallet_handler(runtime.web_access.verify))
    app.router.add_get('/health', health_handler)
    app.router.add_get('/api/logs', wallet_handler(runtime.logs))
    app.router.add_get('/api/stats', wallet_handler(runtime.stats))
    app.router.add_post('/api/toggle_trading', wallet_handler(runtime.toggle))
    app.router.add_post('/api/trading_settings', wallet_handler(runtime.save_trading_settings))
    app.router.add_post('/api/trading_readiness', wallet_handler(runtime.trading_readiness))
    app.router.add_post('/api/ai_preview', wallet_handler(runtime.ai_preview))
    app.router.add_post('/api/toggle_sniper', wallet_handler(runtime.toggle_sniper))
    app.router.add_post('/api/meme_scan', wallet_handler(runtime.scan_candidates))
    app.router.add_post('/api/toggle_meme', api_toggle_meme)
    app.router.add_post('/api/verify_deposit', wallet_handler(runtime.deposit))
    app.router.add_post('/api/wallet_link', wallet_handler(runtime.wallet_link))
    app.router.add_post('/api/deposit_invoice', wallet_handler(runtime.create_invoice))
    app.router.add_get('/api/admin/status', wallet_handler(runtime.admin_status))
    app.router.add_get('/api/admin/pending_operations', wallet_handler(runtime.admin_pending_operations))
    app.router.add_post('/api/admin/retire_legacy', wallet_handler(runtime.retire_legacy))
    app.router.add_post('/api/admin/fee_reserve', wallet_handler(runtime.reserve_fees))
    app.router.add_post('/api/admin/opening_balance', wallet_handler(runtime.opening_balance))
    app.router.add_post('/api/close_all', wallet_handler(runtime.close_all))
    app.router.add_post('/api/withdraw', wallet_handler(runtime.withdraw))
    app.router.add_get('/api/security_status', api_security_status)
    app.router.add_get('/api/account', wallet_handler(runtime.accounts.status))
    app.router.add_post('/api/account/register', wallet_handler(runtime.accounts.register))
    app.router.add_post('/api/account/passkey', wallet_handler(runtime.accounts.create_ticket))
    app.router.add_get('/api/account/passkey_status', wallet_handler(runtime.accounts.ticket_status))
    passkey_html = Path(__file__).with_name("passkey.html").read_text(encoding="utf-8")
    async def passkey_page(request):
        return web.Response(text=passkey_html,content_type="text/html",headers={
            "Cache-Control":"no-store","Referrer-Policy":"no-referrer","X-Content-Type-Options":"nosniff",
            "Content-Security-Policy":"default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; form-action 'none'; base-uri 'none'",
            "Permissions-Policy":"camera=(), microphone=(), publickey-credentials-create=(self), publickey-credentials-get=(self)"})
    async def passkey_browser_handler(request):
        return await runtime.accounts.browser_handle(request)
    app.router.add_get('/passkey', passkey_page)
    app.router.add_post('/passkey/options', passkey_browser_handler)
    app.router.add_post('/passkey/verify', passkey_browser_handler)
    app.router.add_post('/api/set_security', api_set_security)
    app.router.add_post('/api/verify_security', api_verify_security)
    app.router.add_post('/webhook', telegram_webhook_handler)
    app.router.add_get('/deposit/{invoice}', payment_page)
    app.router.add_get('/deposit/{invoice}/status', payment_handler(runtime.payment_status))
    app.router.add_post('/deposit/{invoice}/transaction', payment_handler(runtime.payment_disabled))
    app.router.add_post('/deposit/{invoice}/confirm', payment_handler(runtime.payment_confirm))
    wallet_link_html = Path(__file__).with_name("wallet_link.html").read_text(encoding="utf-8")
    async def wallet_link_page(request):
        if not runtime.ledger.wallet_proof(request.match_info["nonce"]):
            raise web.HTTPNotFound()
        return web.Response(text=wallet_link_html, content_type="text/html", headers={
            "Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": "frame-ancestors 'none'; object-src 'none'; base-uri 'none'",
            "X-Content-Type-Options": "nosniff"})
    app.router.add_get('/wallet-link/{nonce}', wallet_link_page)
    app.router.add_get('/wallet-link/{nonce}/status', payment_handler(runtime.wallet_link_status))
    app.router.add_post('/wallet-link/{nonce}/confirm', payment_handler(runtime.wallet_link_confirm))

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()

    runtime.poll_discovery()
    asyncio.create_task(runtime.worker())
    await set_telegram_webhook()
    if TELEGRAM_TOKEN:
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
                async with session.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getMe") as response:
                    details = await response.json()
                    if details.get("ok"):
                        runtime.web_access.bot_username = details["result"]["username"]
        except Exception:
            add_log("Web sign-in: Telegram registration link temporarily unavailable.")
    
    add_log("⚜️ Гибридное ядро заведено на порту " + str(PORT))
    while True:
        await asyncio.sleep(3600)

if __name__ == "__main__":
    asyncio.run(main())
