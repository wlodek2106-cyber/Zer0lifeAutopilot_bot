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
from market_context import fetch_main_market_context
from english_text import english_json_middleware
from account_security import AccountSecurity
from web_access import WebAccess
from wallet_runtime import WalletError

logging.basicConfig(level=logging.INFO, format='%(asctime)s - [%(levelname)s] - %(message)s')

# --- КОНФИГУРАЦИЯ ---
TELEGRAM_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_TOKEN") or "").strip()
PORT = int(os.getenv("PORT", 10000))
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL", "https://zer0lifeautopilot-bot.onrender.com").strip()
ADMIN_TELEGRAM_ID = os.getenv("ADMIN_TELEGRAM_ID", "428821665").strip() or "428821665"
TELEGRAM_INIT_DATA_MAX_AGE_SECONDS = 86400
PIN_HASH_ITERATIONS = 310000
PIN_MAX_ATTEMPTS = 5
PIN_LOCK_SECONDS = 900
ADMIN_API_PATHS = {"/api/admin/status", "/api/admin/pending_operations", "/api/admin/fee_reserve", "/api/admin/release_fee_reserve", "/api/admin/opening_balance", "/api/admin/retire_legacy"}
WITHDRAW_LOCKS = {}
CUSTODY_SAFE_MODE = True
CUSTODY_PAUSE_MESSAGE = "Операции с реальными средствами временно приостановлены до сверки балансов и миграции реестра."

DB_DIR = "/var/data"
DB_FILE = os.path.join(DB_DIR, "zer0life_users.db") if os.path.exists(DB_DIR) else "zer0life_users.db"

SOLANA_RPC = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com").strip()
SHARED_DEPOSIT_WALLET = os.getenv("DEPOSIT_WALLET", "8hxiCofyaKCBkhR5nsDqvUivmfgxcVx8zo2WiCzSdM6L").strip()
SPLASH_IMAGE_URL = "/static/zerolife-ai-trader-core.webp"

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
    "PYTH": {"mint": "HZ1JovNiDcZvKhgWNgQX5Z59C3b22DoPtRUC544HLD37", "decimals": 6},
    # Official Jito/Orca mint addresses; decimals verified on Solana mainnet.
    "JTO": {"mint": "jtojtomepa8beP8AuQc6eXt5FriJwfFMwQx2v2f9mCL", "decimals": 9},
    "ORCA": {"mint": "orcaEKTdK7LKz57vaAYr9QeNsVEPfiu6QeMU1kektZE", "decimals": 6},
    "KMNO": {"mint": "KMNo3nJsBXfcpJTVhZcXLW7RmTwTt4GVFE7suUBo9sS", "decimals": 6},
    "NOS": {"mint": "nosXBVoaCTtYdLvKY6Csb4AC8JCdQKKAaWYtx2ZMoo7", "decimals": 6},
    "SHDW": {"mint": "SHDWyBxihqiCj6YekG2GUr7wqKLeLAMK1gHZck9pL6y", "decimals": 9},
    "BONK": {"mint": "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263", "decimals": 5},
    "BOME": {"mint": "ukHH6c7mMyiWCf1b9pnWe25TSpkDDt3H5pQZgZ74J82", "decimals": 6},
    "POPCAT": {"mint": "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr", "decimals": 9},
    "MEW": {"mint": "MEW1gQWJ3nEXg2qgERiKu7FAFj79PHvQVREQUzScPP5", "decimals": 5},
    "PENGU": {"mint": "2zMMhcVQEXDtdE6vsFS7S7D5oUodfJHE8vd1gnBouauv", "decimals": 6}
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
async def security_headers_middleware(request, handler):
    """Apply browser protections consistently, including JSON and error responses."""
    try:
        response = await handler(request)
    except web.HTTPException as exc:
        response = exc

    response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault(
        "Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    if request.path == "/health" or request.path.startswith(("/api/", "/web-auth/")):
        response.headers["Cache-Control"] = "no-store"
    return response


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
    <title>AI Trader · Solana / USDC</title>
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
        .solpay-panel { background:#0b141c; border:1px solid #214337; border-radius:16px; padding:14px; margin-bottom:14px; }
        .solpay-panel h3 { margin:0 0 6px; font-size:15px; color:#f8fafc; }
        .solpay-panel .solpay-note, .solpay-fallback-note { color:#94a5bb; font-size:11px; line-height:1.55; }
        .solpay-panel label { display:block; color:#94a5bb; font-size:11px; margin:10px 0 6px; }
        .solpay-panel .sig-input { box-sizing:border-box; margin-bottom:8px; }
        .solpay-panel .token-selector { margin-bottom:8px; }
        .solpay-presets .token-btn { padding:8px 5px; font-size:10px; }
        .solpay-checkout { margin-top:12px; padding:12px; border:1px solid #263449; border-radius:14px; background:#080d14; }
        .solpay-checkout[hidden] { display:none; }
        .solpay-checkout-row { display:flex; justify-content:space-between; gap:10px; padding:7px 0; font-size:11px; color:#94a5bb; }
        .solpay-checkout-row strong { color:#dce8da; text-align:right; overflow-wrap:anywhere; }
        .solpay-open-wallet { display:block; margin:10px 0; text-align:center; text-decoration:none; }
        .solpay-status { min-height:18px; color:#aebdcc; font-size:11px; line-height:1.45; }
        .solpay-status[data-state="success"] { color:#86efac; }
        .solpay-status[data-state="error"] { color:#fca5a5; }
        .solpay-fallback { margin-top:12px; border:1px solid #1c2436; border-radius:14px; background:#090d14; }
        .solpay-fallback summary { padding:13px; cursor:pointer; color:#b7c3d1; font-size:12px; font-weight:700; }
        .solpay-fallback-content { padding:0 12px 12px; }

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
        .tariff-packages { display:grid;grid-template-columns:1fr 1fr;gap:10px;margin:16px 0; }
        .tariff-package { display:flex;flex-direction:column;gap:8px;width:100%;min-height:120px;
            text-align:left;font:inherit;color:inherit;background:#0b111b;border:1px solid #263548;
            border-radius:14px;padding:14px;cursor:pointer; }
        .tariff-package:hover,.tariff-package:focus-visible { border-color:#b6ef79;outline:2px solid rgba(182,239,121,.25); }
        .tariff-package strong { display:block;color:#b6ef79;margin-bottom:6px; }
        #telegram-tariff-modal { position:fixed;inset:0;z-index:1100;display:none;place-items:center;
            padding:18px;background:rgba(0,0,0,.78);backdrop-filter:blur(8px); }
        #telegram-tariff-modal.active { display:grid; }
        #telegram-tariff-modal .card { width:min(440px,100%);max-height:calc(100dvh - 36px);overflow:auto;
            padding:24px!important;background:#101823!important;border:1px solid #334454!important;
            box-shadow:0 24px 80px rgba(0,0,0,.6); }
        #telegram-package-modal { position:fixed;inset:0;z-index:1110;display:none;place-items:center;
            padding:18px;background:rgba(0,0,0,.78);backdrop-filter:blur(8px); }
        #telegram-package-modal.active { display:grid; }
        #telegram-package-modal .card { width:min(440px,100%);max-height:calc(100dvh - 36px);overflow:auto;
            padding:24px!important;background:#101823!important;border:1px solid #334454!important;
            box-shadow:0 24px 80px rgba(0,0,0,.6); }
        #telegram-tariff-countdown { color:#b6ef79;font-weight:700; }
        .quick-actions { display:grid;grid-template-columns:1fr 1fr;gap:10px;margin:18px 0; }
        #app .stat-box { background:#0b111b;border:1px solid #1d2b3d;padding:16px;border-radius:14px; }
        #app .stat-label { color:#8495ad;letter-spacing:.7px;font-size:10px; }
        #app .stat-val { font-size:18px;line-height:1.5;overflow-wrap:anywhere; }
        #app .btn-copy { background:#172334;border:1px solid #2a3a50;border-radius:13px; padding:14px;font-size:13px; }
        #app .btn-green-main { background:#3bd19a;color:#061b12;font-weight:750;border-radius:14px; }
        #app .btn-sell-all { background:transparent;color:#e49b9b;border:1px solid #694343;border-radius:12px;font-size:12px; }
        #app .position-close { width:100%; margin-top:10px; padding:11px 14px; border-radius:11px;
            border:1px solid #684146; background:#281a1c; color:#f1b3b8; font-size:12px; font-weight:700; cursor:pointer; }
        #app .position-close:disabled { opacity:.65; cursor:wait; }
        .bottom-nav { box-sizing:border-box;background:#0c1420;border-color:#243247;gap:6px;padding:12px 16px calc(12px + env(safe-area-inset-bottom,0px)); }
        .nav-btn { flex:1;padding:10px 3px;border-radius:12px;font-size:11px; }
        .nav-btn span { display:block;margin-bottom:4px;font-size:19px; }
        .nav-btn.active { background:#163126;color:#66dfac; }
        .settings-label { display:block;font-size:13px;color:#a7b6c9;margin:16px 0 8px; }
        details summary { color:#b6c6da;font-size:13px;cursor:pointer;padding:10px 0; }
        .status-output { white-space:pre-wrap;font-size:13px;color:#b2c4da;line-height:1.6; }

        :root {
            color-scheme: light;
            --paper: #f2f4f0;
            --surface: #fbfcf9;
            --surface-raised: #ffffff;
            --ink: #182b34;
            --muted: #64777d;
            --line: #d9e1de;
            --teal: #087b68;
            --teal-soft: #e1f1eb;
            --blue: #376c86;
            --danger: #a23f42;
        }
        * { box-sizing: border-box; }
        html { background: var(--paper); }
        body {
            width: 100%; height: 100%; min-height: 100dvh; overflow: hidden;
            background:
                radial-gradient(ellipse at 100% 0%, rgba(189, 215, 205, .42), transparent 36%),
                var(--paper);
            color: var(--ink);
            font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
            letter-spacing: -.012em;
        }
        button, input { font: inherit; }
        button { touch-action: manipulation; }
        :focus-visible { outline: 3px solid #347b88 !important; outline-offset: 3px; }
        #splash-screen {
            z-index: 9999;
            width: 100%; height: 100%; min-height: 100dvh; inset: 0;
            justify-content: center; align-items: stretch;
            padding: max(28px, env(safe-area-inset-top)) 25px calc(26px + env(safe-area-inset-bottom, 0px));
            overflow: hidden;
            background:
                radial-gradient(ellipse at 75% 23%, rgba(184, 213, 201, .46), transparent 42%),
                linear-gradient(145deg, #f5f7f2 0%, #e9efea 100%);
            color: var(--ink);
        }
        #splash-screen::before {
            content: ""; position: absolute; width: min(82vw, 350px); aspect-ratio: 1;
            left: 50%; top: 8%; transform: translateX(-50%) rotateX(58deg) rotateZ(-35deg);
            border-radius: 30%; border: 1px solid rgba(255,255,255,.88);
            background:
                linear-gradient(135deg, rgba(255,255,255,.82), rgba(218,232,224,.55)),
                repeating-linear-gradient(0deg, transparent 0 31px, rgba(73,111,101,.07) 32px);
            box-shadow: 22px 33px 40px rgba(40,76,66,.12), inset 0 1px 0 #fff;
            pointer-events: none;
        }
        #splash-screen::after {
            content: ""; position: absolute; width: min(54vw, 218px); aspect-ratio: 1;
            left: 50%; top: 16%; transform: translateX(-50%) rotateX(56deg) rotateZ(45deg);
            border-radius: 50%;
            background: radial-gradient(circle at 32% 28%, #f8fffc 0 4%, #8dc4b0 5% 16%, #237c70 39%, #174f50 73%, #103d43 100%);
            box-shadow: 12px 24px 24px rgba(20,75,67,.25), inset -10px -12px 18px rgba(6,35,40,.38), inset 7px 8px 13px rgba(255,255,255,.45);
            pointer-events: none;
        }
        .brand-title, .brand-run, #splash-screen .start-btn, #splash-screen .splash-caption {
            position: relative; z-index: 1; text-align: center;
        }
        .brand-title {
            margin-top: auto; color: #193a43; font-size: clamp(38px, 12vw, 54px);
            font-weight: 780; letter-spacing: -.075em; line-height: .96;
        }
        .brand-run {
            margin-top: 11px; color: #187865; font-size: 12px; font-weight: 750;
            letter-spacing: .21em; line-height: 1.4;
        }
        .splash-caption {
            max-width: 330px; margin: 15px auto 24px; color: #5d7075;
            font-size: 14px; line-height: 1.55; font-weight: 450;
        }
        .start-btn {
            margin: 0 auto; width: 100%; max-width: 420px; min-height: 58px;
            padding: 17px 20px; border: 1px solid #0b6e5d; border-radius: 16px;
            background: linear-gradient(180deg, #178671, #0d705f);
            color: #f4fffb; font-size: 15px; font-weight: 720; letter-spacing: .01em;
            box-shadow: 0 8px 0 #075648, 0 15px 25px rgba(12,94,78,.19);
            cursor: pointer; transition: transform .16s ease, box-shadow .16s ease, opacity .16s ease;
        }
        .start-btn:active { transform: translateY(4px); box-shadow: 0 4px 0 #075648, 0 8px 15px rgba(12,94,78,.16); }
        .start-btn:disabled { opacity: .68; cursor: wait; }
        #account-signup {
            background: var(--surface-raised) !important; color: var(--ink);
            border: 1px solid #d4dfda; border-radius: 22px; padding: 24px;
            box-shadow: 0 22px 60px rgba(22,51,52,.2), 0 3px 8px rgba(22,51,52,.08);
        }
        #account-signup h2 { margin: 0 0 8px; font-size: 24px; letter-spacing: -.04em; }
        #account-signup label { color: #354e54; font-size: 13px; line-height: 1.55; }
        #account-signup .muted { color: var(--muted); font-size: 13px; line-height: 1.55; }
        .card, .deposit-modal {
            color: var(--ink); background: var(--surface-raised) !important;
            border: 1px solid var(--line) !important; border-radius: 20px;
            box-shadow: 0 5px 16px rgba(35,67,66,.045), 0 1px 2px rgba(35,67,66,.035);
        }
        #app .tab-panel {
            width: 100%; max-width: 660px; height: var(--tg-viewport-stable-height, 100dvh);
            margin: auto; padding: max(22px, env(safe-area-inset-top)) 18px calc(118px + env(safe-area-inset-bottom, 0px));
            color: var(--ink); overscroll-behavior-y: contain; scrollbar-width: thin;
        }
        .page-heading { margin: 4px 0 20px; }
        .page-heading h1 { color: #183740; font-size: 29px; font-weight: 760; letter-spacing: -.055em; }
        .eyebrow { color: #67817f; font-size: 11px; font-weight: 700; letter-spacing: .1em; text-transform: uppercase; }
        .section-note, .muted { color: #65777c; font-size: 13px; line-height: 1.6; }
        #app .card { padding: 18px; margin-bottom: 14px; }
        #app .banner-limit {
            background: #e8f3ed !important; border: 1px solid #cfdfd6 !important;
            color: #244c45; border-radius: 16px;
        }
        #app .banner-limit [style*="color"] { color: #345f57 !important; }
        #app .stat-box {
            min-width: 0; background: #f1f5f2 !important; border: 1px solid #dfe7e2 !important;
            border-radius: 13px; padding: 13px;
        }
        #app .stat-label { color: #6a7d7d !important; font-size: 10px; letter-spacing: .075em; }
        #app .stat-val { color: #1d3740 !important; font-size: 17px; line-height: 1.4; font-variant-numeric: tabular-nums; }
        #app [style*="color: #fff"], #app [style*="color: #f8fafc"] { color: #203941 !important; }
        #app [style*="color: #94a3b8"], #app [style*="color: #64748b"] { color: #697d80 !important; }
        #app [style*="background: #06080e"], #app [style*="background:#06080e"],
        #app [style*="background: #080a11"], #app [style*="background:#0b111b"] {
            background: #f0f4f1 !important; color: #29434a !important;
        }
        #app [style*="color: #22c55e"], #app [style*="color: #4ade80"] { color: #087b68 !important; }
        #app [style*="color: #60a5fa"] { color: #376c86 !important; }
        #app [style*="color: #f87171"] { color: #a23f42 !important; }
        .control-bar { gap: 9px; }
        .btn-ctl, .btn-copy, .btn-green-main, .btn-verify, .action-btn, .wallet-tab, .token-btn {
            min-height: 46px; border-radius: 13px; font-weight: 680; transition: transform .16s ease, background .16s ease, box-shadow .16s ease;
        }
        .btn-toggle-on { background: #e0f1e9; border: 1px solid #b9d8ca; color: #146c59; }
        .btn-toggle-off { background: #f8eaea; border: 1px solid #e4c6c4; color: #984448; }
        .btn-refresh, .btn-copy { background: #eff3f0; border: 1px solid #d7e0db; color: #304b51; }
        #app .btn-green-main, .btn-green-main, .btn-verify {
            color: #f4fffb !important; background: linear-gradient(180deg, #178671, #0d705f) !important;
            border: 1px solid #0c6c5b; box-shadow: 0 3px 0 #075648;
        }
        .btn-sell-all { background: #fbf2ef; border: 1px solid #e5c7c0; color: #934d48; border-radius: 13px; }
        .order-card { background: #f5f7f4; color: #32484d; border: 1px solid #dce4df; border-radius: 14px; }
        .order-grid { color: #68797d; }
        .badge-open { background: #e7f0f3; color: #376c86; border-color: #c8dce4; }
        .badge-closed { background: #e3f1e9; color: #14765e; border-color: #c5dfd1; }
        .log-console { color: #236b59; background: #f1f5f2; border-color: #dce5df; }
        .deposit-modal { padding: 18px; }
        .wallet-tabs { background: #edf2ef; border-color: #dce5df; }
        .wallet-tab { color: #6b7d7f; }
        .wallet-tab.active { color: #175b50; background: #dcece4; }
        .token-btn { color: #607276; background: #f5f7f4; border-color: #dce5df; }
        .token-btn.active { color: #087b68; background: #e5f2ec; border-color: #9fcbbb; }
        .sig-input {
            color: #213c43; background: #f7f9f7; border: 1px solid #cfdcd5;
            border-radius: 12px; min-height: 48px; font: 13px ui-monospace, SFMono-Regular, Menlo, monospace;
        }
        .sig-input::placeholder { color: #8a9999; }
        .address-box { color: #126d5c; background: #f1f5f2; border-color: #d5e1da; }
        .bottom-nav {
            background: rgba(249,251,248,.94); border-top: 1px solid #d7e0db;
            box-shadow: 0 -8px 24px rgba(34,62,59,.07); backdrop-filter: blur(16px);
            padding: 8px 12px calc(8px + env(safe-area-inset-bottom, 0px));
        }
        .nav-btn { min-height: 54px; border-radius: 13px; color: #718184; font-weight: 650; }
        .nav-btn span { font-size: 18px; color: currentColor; }
        .nav-btn.active { color: #146c59; background: #e5f1eb; }
        details summary { color: #34565b; font-weight: 650; }
        a { color: #176d72; text-underline-offset: 3px; }
        button:hover:not(:disabled) { filter: brightness(.98); }
        button:disabled { cursor: not-allowed; opacity: .58; }
        @media (min-width: 640px) {
            #splash-screen { align-items: center; padding-left: max(28px, calc((100vw - 460px) / 2)); padding-right: max(28px, calc((100vw - 460px) / 2)); }
            #splash-screen::before { top: 7%; }
            #splash-screen::after { top: 15%; }
            #app .tab-panel { padding-left: 24px; padding-right: 24px; }
        }
        @media (max-height: 700px) {
            #splash-screen { padding-top: max(18px, env(safe-area-inset-top)); padding-bottom: calc(18px + env(safe-area-inset-bottom, 0px)); }
            #splash-screen::before { width: min(58vw, 245px); top: 3%; }
            #splash-screen::after { width: min(38vw, 155px); top: 9%; }
            .brand-title { font-size: 38px; }
            .splash-caption { margin: 10px auto 17px; }
        }
        @media (prefers-reduced-motion: reduce) {
            *, *::before, *::after { scroll-behavior: auto !important; transition-duration: .01ms !important; animation-duration: .01ms !important; animation-iteration-count: 1 !important; }
        }
        :root {
            color-scheme: dark;
            --paper: #080d13;
            --surface: #101923;
            --surface-raised: #121d27;
            --ink: #e9f0f2;
            --muted: #93a4ad;
            --line: #263744;
            --teal: #32d39b;
            --teal-soft: #14372f;
            --blue: #78a9c6;
            --danger: #e18b8b;
        }
        html { background: #080d13; }
        body {
            background:
                radial-gradient(ellipse at 82% -8%, rgba(32,100,91,.22), transparent 34%),
                radial-gradient(ellipse at 4% 55%, rgba(41,78,106,.12), transparent 38%),
                #080d13;
            color: #e9f0f2;
        }
        :focus-visible { outline-color: #5ad9b0 !important; }
        #splash-screen {
            background:
                radial-gradient(ellipse at 50% 27%, rgba(18,91,78,.23), transparent 37%),
                radial-gradient(ellipse at 78% 20%, rgba(45,88,120,.15), transparent 40%),
                linear-gradient(145deg, #0c141c 0%, #080d13 65%, #0a1118 100%);
            color: #e9f0f2;
        }
        #splash-screen::before {
            border: 1px solid rgba(151,190,190,.19);
            background:
                linear-gradient(135deg, rgba(34,56,65,.72), rgba(10,22,29,.35)),
                repeating-linear-gradient(0deg, transparent 0 31px, rgba(123,165,169,.09) 32px),
                repeating-linear-gradient(90deg, transparent 0 31px, rgba(123,165,169,.07) 32px);
            box-shadow: 22px 34px 48px rgba(0,0,0,.4), inset 0 1px 0 rgba(218,243,237,.22), inset 0 -16px 34px rgba(8,16,23,.55);
        }
        #splash-screen::after {
            background:
                radial-gradient(circle at 31% 25%, rgba(255,255,255,.98) 0 1.2%, rgba(201,255,237,.94) 1.8% 3.5%, transparent 4.5%),
                radial-gradient(circle at 34% 29%, #a7f5d5 0 5%, #40d7a6 14%, #168b78 34%, #155f63 57%, #122f43 80%, #0b1b29 100%);
            border: 1px solid rgba(208,246,233,.55);
            box-shadow: 0 0 34px rgba(44,211,158,.21), 12px 24px 30px rgba(0,0,0,.5),
                inset -12px -15px 22px rgba(4,18,28,.74), inset 8px 8px 16px rgba(230,255,248,.38);
        }
        .brand-title { color: #f2f8f7; text-shadow: 0 2px 24px rgba(72,201,161,.12); }
        .brand-run { color: #57d8ac; }
        .splash-caption { color: #a6b6bd; }
        .start-btn {
            background: linear-gradient(180deg, #36d9a2, #19ae81);
            color: #062219; border-color: #4de0ad;
            box-shadow: 0 8px 0 #087153, 0 16px 34px rgba(20,178,127,.2), inset 0 1px 0 rgba(255,255,255,.32);
        }
        .start-btn:active { box-shadow: 0 4px 0 #087153, 0 9px 20px rgba(20,178,127,.16); }
        #account-signup {
            background: #111c26 !important; color: #e8f0f2;
            border-color: #30414d; box-shadow: 0 22px 70px rgba(0,0,0,.58), 0 1px 0 rgba(222,244,240,.06) inset;
        }
        #account-signup h2, #account-signup label { color: #e5edef; }
        #account-signup .muted { color: #9aabb3; }
        .card, .deposit-modal {
            color: #e6eef0; background: #111b25 !important; border-color: #263744 !important;
            box-shadow: 0 10px 28px rgba(0,0,0,.2), inset 0 1px 0 rgba(220,240,236,.035);
        }
        #app .tab-panel { color: #e6eef0; }
        .page-heading h1 { color: #f1f6f5; }
        .eyebrow { color: #8da6ae; }
        .section-note, .muted { color: #a0b0b7; }
        #app .banner-limit {
            background: linear-gradient(135deg, #13251f, #111e25) !important;
            border-color: #245242 !important; color: #d9eee7;
            box-shadow: inset 0 1px 0 rgba(218,245,232,.045);
        }
        #app .banner-limit [style*="color"] { color: #9fb7ba !important; }
        #app .banner-limit > div:first-child { color: #53d7a4 !important; }
        #app .stat-box {
            background: linear-gradient(145deg, #0d151d, #121e28) !important;
            border-color: #263744 !important; box-shadow: inset 0 1px 0 rgba(229,246,244,.035);
        }
        #app .stat-label { color: #91a5ad !important; }
        #app .stat-val { color: #f0f6f5 !important; }
        #app [style*="color: #fff"], #app [style*="color: #f8fafc"] { color: #eaf1f2 !important; }
        #app [style*="color: #94a3b8"], #app [style*="color: #64748b"] { color: #91a3ad !important; }
        #app [style*="background: #06080e"], #app [style*="background:#06080e"],
        #app [style*="background: #080a11"], #app [style*="background:#0b111b"] {
            background: #0b131c !important; color: #d7e3e5 !important;
        }
        #app [style*="color: #22c55e"], #app [style*="color: #4ade80"] { color: #54d9a5 !important; }
        #app [style*="color: #60a5fa"] { color: #83b9d7 !important; }
        #app [style*="color: #f87171"] { color: #f08a8c !important; }
        .btn-toggle-on { background: #14362e; border-color: #28644f; color: #67e0b1; }
        .btn-toggle-off { background: #352124; border-color: #694044; color: #ef9999; }
        .btn-refresh, .btn-copy { background: #172530; border-color: #30424f; color: #d6e3e6; }
        #app .btn-green-main, .btn-green-main, .btn-verify {
            color: #062219 !important; background: linear-gradient(180deg, #36d9a2, #19ae81) !important;
            border-color: #38c99a; box-shadow: 0 3px 0 #087153, inset 0 1px 0 rgba(255,255,255,.24);
        }
        .btn-sell-all { background: #291d21; border-color: #58363d; color: #e89a9b; }
        .order-card { background: #0d151e; color: #d8e4e6; border-color: #293a46; }
        .order-grid { color: #9aabb2; }
        .badge-open { background: #142b38; color: #86b9d6; border-color: #2d5367; }
        .badge-closed { background: #14342b; color: #65dbae; border-color: #286048; }
        .log-console { color: #70d9ad; background: #09121a; border-color: #263944; }
        .wallet-tabs { background: #0b141c; border-color: #273944; }
        .wallet-tab { color: #8ea0a8; }
        .wallet-tab.active { color: #6be0b5; background: #15352d; }
        .token-btn { color: #9aabb2; background: #0d171f; border-color: #2b3c47; }
        .token-btn.active { color: #55d7a4; background: #14362e; border-color: #32745b; }
        .sig-input { color: #e6eff0; background: #0b141c; border-color: #30424e; }
        .sig-input::placeholder { color: #788a93; }
        .address-box { color: #61d9ad; background: #0b141b; border-color: #2b3c46; }
        .bottom-nav {
            background: rgba(10,17,24,.95); border-top-color: #263641;
            box-shadow: 0 -8px 24px rgba(0,0,0,.24);
        }
        .nav-btn { color: #91a1a8; }
        .nav-btn.active { color: #65ddb1; background: #15342c; }
        details summary { color: #c2d2d5; }
        a { color: #7bbbd5; }
        .action-btn {
            display: inline-flex; width: 100%; min-height: 46px;
            align-items: center; justify-content: center;
            padding: 12px 16px; border: 1px solid #38c99a; border-radius: 13px;
            background: linear-gradient(180deg, #36d9a2, #19ae81);
            color: #062219; font-size: 14px; font-weight: 720; line-height: 1.25;
            box-shadow: 0 3px 0 #087153, inset 0 1px 0 rgba(255,255,255,.24);
            cursor: pointer; touch-action: manipulation;
            transition: transform .14s ease, opacity .14s ease;
        }
        .action-btn:hover:not(:disabled) { filter: brightness(1.04); }
        .action-btn:active:not(:disabled) { transform: translateY(2px); }
        .action-btn:focus-visible {
            outline: 3px solid #83b9d7 !important; outline-offset: 3px;
        }
        .action-btn:disabled {
            color: #a8b5b7; background: #25333b; border-color: #3a4b53;
            box-shadow: none; opacity: .72; cursor: not-allowed;
        }

        /* Zer0Life AI Trader splash: cinematic 3D, without simulated market data. */
        #splash-screen.zl-prism {
            position: fixed; inset: 0; z-index: 9999;
            display: flex; flex-direction: column; justify-content: flex-start; align-items: stretch;
            width: 100%; height: 100vh; height: 100dvh; min-height: 100dvh;
            padding: max(22px, env(safe-area-inset-top)) max(20px, calc((100vw - 460px) / 2)) max(18px, env(safe-area-inset-bottom));
            overflow-x: hidden; overflow-y: auto; isolation: isolate;
            color: #f0f4e9;
            background:
                radial-gradient(ellipse at 48% 41%, rgba(85,133,15,.19), transparent 42%),
                radial-gradient(ellipse at 92% 72%, rgba(79,116,14,.11), transparent 32%),
                linear-gradient(152deg, #0a0d08 0%, #050704 58%, #090d06 100%);
        }
        #splash-screen.zl-prism::before {
            content: ""; position: absolute; z-index: 0; inset: 0; pointer-events: none; opacity: .12;
            background-image: url("data:image/svg+xml,%3Csvg viewBox='0 0 180 180' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='.9' numOctaves='3' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)' opacity='.2'/%3E%3C/svg%3E");
            mix-blend-mode: screen;
        }
        #splash-screen.zl-prism::after { display: none; }
        #splash-screen.zl-prism > * { position: relative; z-index: 1; }
        .zl-prism__top {
            display: flex; align-items: center; justify-content: space-between;
            width: 100%; max-width: 1100px; margin: 0 auto;
        }
        .zl-prism__wordmark { display: inline-flex; align-items: center; gap: 10px; color: #f3f5ed; text-decoration: none; }
        .zl-prism__mark {
            display: flex; align-items: center; gap: 2px; height: 18px; transform: skew(-22deg);
        }
        .zl-prism__mark i {
            display: block; width: 4px; height: 13px; border-radius: 2px;
            background: #c8ff37; box-shadow: 0 0 10px rgba(200,255,55,.35);
        }
        .zl-prism__mark i:nth-child(2) { height: 18px; }
        .zl-prism__mark i:nth-child(3) { height: 10px; }
        .zl-prism__brand { font-size: 15px; font-weight: 760; letter-spacing: -.045em; }
        .zl-prism__brand-zero { color: #c8ff37; }
        .zl-prism__network {
            display: inline-flex; align-items: center; gap: 8px; padding: 8px 11px;
            border: 1px solid rgba(222,239,193,.14); border-radius: 999px;
            color: #a9b29a; background: rgba(255,255,255,.035);
            font: 600 9px/1 ui-monospace, SFMono-Regular, Menlo, monospace; letter-spacing: .13em;
        }
        .zl-prism__network-dot, .zl-prism__pulse {
            width: 6px; height: 6px; flex: 0 0 6px; border-radius: 50%;
            background: #c8ff37; box-shadow: 0 0 11px rgba(200,255,55,.75);
        }
        .zl-prism__hero {
            position: relative; display: grid; grid-template-rows: minmax(300px, 1fr) auto;
            align-items: center; width: 100%; max-width: 1100px; flex: 1; margin: 0 auto;
            padding: 2vh 0 1vh;
        }
        .zl-prism__art {
            position: relative; display: grid; place-items: center; width: 100%;
            min-height: 285px; max-height: 540px; margin: 0 auto; perspective: 900px;
        }
        .zl-prism__halo {
            position: absolute; width: min(80vw, 440px); aspect-ratio: 1; border-radius: 50%;
            background: radial-gradient(circle, rgba(182,245,38,.16), rgba(101,148,19,.08) 34%, transparent 69%);
            filter: blur(10px);
        }
        .zl-prism__image-frame {
            position: relative; width: min(91vw, 470px); aspect-ratio: 1; overflow: hidden;
            border-radius: 38% 38% 43% 43%; transform: translateY(-2%) rotate(-3deg);
            -webkit-mask-image: radial-gradient(ellipse at 50% 48%, #000 52%, rgba(0,0,0,.94) 64%, transparent 81%);
            mask-image: radial-gradient(ellipse at 50% 48%, #000 52%, rgba(0,0,0,.94) 64%, transparent 81%);
            animation: zl-prism-float 8s ease-in-out infinite;
        }
        .zl-prism__image-frame::after {
            content: ""; position: absolute; inset: 0; pointer-events: none;
            background: linear-gradient(180deg, rgba(5,7,4,.02) 55%, #050704 100%),
                radial-gradient(ellipse at 50% 45%, transparent 39%, rgba(5,7,4,.21) 72%, #050704 100%);
        }
        .zl-prism__image-frame img {
            display: block; width: 100%; height: 100%; object-fit: cover;
            filter: saturate(.9) contrast(1.08);
        }
        .zl-prism__orbit {
            position: absolute; left: 50%; top: 48%; width: min(88vw, 500px); height: min(32vw, 170px);
            border: 1px solid rgba(201,255,64,.28); border-left-color: rgba(201,255,64,.06);
            border-bottom-color: rgba(201,255,64,.12); border-radius: 50%;
            transform: translate(-50%,-50%) rotate(-25deg); pointer-events: none;
            box-shadow: 0 0 16px rgba(171,233,31,.07), inset 0 0 12px rgba(171,233,31,.035);
        }
        .zl-prism__orbit--two {
            width: min(75vw, 410px); height: min(24vw, 124px);
            transform: translate(-50%,-50%) rotate(58deg);
            border-color: rgba(218,242,183,.17); border-right-color: rgba(201,255,64,.35);
        }
        .zl-prism__annotation {
            position: absolute; z-index: 2; color: rgba(212,230,188,.7);
            font: 600 8px/1.4 ui-monospace, SFMono-Regular, Menlo, monospace; letter-spacing: .16em;
        }
        .zl-prism__annotation--top { top: 13%; right: 2%; display: flex; align-items: center; gap: 8px; color: #c8ff37; }
        .zl-prism__annotation--top i { display: block; width: 28px; height: 1px; background: #c8ff37; opacity: .7; }
        .zl-prism__annotation--bottom { bottom: 7%; left: 2%; }
        .zl-prism__copy {
            position: relative; z-index: 2; display: flex; flex-direction: column; align-items: flex-start;
            width: 100%; max-width: 510px; margin: -2.5vh auto 0;
        }
        .zl-prism__eyebrow {
            display: flex; align-items: center; gap: 9px; margin: 0 0 13px;
            color: #c2d39f; font: 650 9px/1.2 ui-monospace, SFMono-Regular, Menlo, monospace; letter-spacing: .2em;
        }
        .zl-prism__eyebrow-line { width: 17px; height: 1px; background: #c8ff37; box-shadow: 0 0 9px rgba(200,255,55,.55); }
        .zl-prism__title {
            margin: 0; color: #f4f6ee; font-size: clamp(44px, 13vw, 68px);
            font-weight: 760; letter-spacing: -.075em; line-height: .91; text-shadow: 0 8px 36px rgba(0,0,0,.5);
        }
        .zl-prism__title em { color: #c8ff37; font-style: normal; font-weight: 650; letter-spacing: -.08em; }
        #splash-screen .splash-caption {
            max-width: 330px; margin: 17px 0 23px; color: #a9b09f;
            font-size: 14px; line-height: 1.55; font-weight: 450; text-align: left;
        }
        #splash-screen .zl-prism__launch {
            display: flex; width: 100%; min-height: 58px; align-items: center; justify-content: space-between;
            margin: 0; padding: 0 18px 0 21px; border: 1px solid rgba(229,255,165,.7); border-radius: 13px;
            color: #101605; background: linear-gradient(105deg, #d4ff63 0%, #b9f535 62%, #a4e72a 100%);
            box-shadow: 0 8px 26px rgba(167,221,44,.15), inset 0 1px 0 rgba(255,255,255,.65);
            font: 750 14px/1 system-ui, sans-serif; letter-spacing: -.015em; cursor: pointer;
            transition: transform .18s ease, filter .18s ease;
        }
        #splash-screen .zl-prism__launch::after {
            content: "↗"; display: grid; width: 30px; height: 30px; place-items: center;
            border: 1px solid rgba(12,27,0,.16); border-radius: 9px; font-size: 17px; font-weight: 500;
        }
        #splash-screen .zl-prism__launch:hover:not(:disabled) { transform: translateY(-2px); filter: brightness(1.06); }
        #splash-screen .zl-prism__launch:active:not(:disabled) { transform: translateY(0); }
        #splash-screen .zl-prism__launch:focus-visible { outline: 2px solid #f1ffcf !important; outline-offset: 4px; }
        #splash-screen .zl-prism__launch:disabled { opacity: .72; cursor: not-allowed; }
        .zl-prism__note { display: flex; align-items: center; gap: 9px; margin: 13px 2px 0; color: #747d68; font: 550 8px/1.2 ui-monospace, SFMono-Regular, Menlo, monospace; letter-spacing: .13em; }
        .zl-prism__note i { width: 2px; height: 2px; border-radius: 50%; background: #525a49; }
        .zl-prism__footer {
            display: flex; align-items: center; gap: 12px; width: 100%; max-width: 1100px;
            margin: 16px auto 0; color: #626b58;
            font: 550 7px/1.2 ui-monospace, SFMono-Regular, Menlo, monospace; letter-spacing: .14em;
        }
        .zl-prism__footer-rule { flex: 1; height: 1px; background: rgba(212,232,181,.13); }
        @keyframes zl-prism-float {
            0%, 100% { transform: translateY(-2%) rotate(-3deg); }
            50% { transform: translateY(0) rotate(-1deg); }
        }
        @media (min-width: 760px) {
            #splash-screen.zl-prism { padding: max(32px, env(safe-area-inset-top)) 7vw max(24px, env(safe-area-inset-bottom)); }
            .zl-prism__hero { grid-template-columns: 1.08fr .92fr; grid-template-rows: 1fr; gap: clamp(24px, 5vw, 80px); padding: 3vh 0; }
            .zl-prism__art { grid-column: 1; grid-row: 1; max-height: 680px; }
            .zl-prism__copy { grid-column: 2; grid-row: 1; margin: 0; }
            .zl-prism__title { font-size: clamp(58px, 7.2vw, 92px); }
            #splash-screen .splash-caption { font-size: 15px; }
            .zl-prism__annotation--top { right: -2%; }
            .zl-prism__annotation--bottom { left: -4%; bottom: 13%; }
        }
        @media (max-height: 720px) and (max-width: 759px) {
            #splash-screen.zl-prism { padding-top: max(14px, env(safe-area-inset-top)); padding-bottom: max(12px, env(safe-area-inset-bottom)); }
            .zl-prism__hero { grid-template-rows: minmax(220px, 42vh) auto; padding-top: 1vh; }
            .zl-prism__art { min-height: 220px; }
            .zl-prism__image-frame { width: min(67vw, 320px); }
            .zl-prism__copy { margin-top: -5px; }
            .zl-prism__title { font-size: clamp(38px, 10vw, 52px); }
            #splash-screen .splash-caption { margin: 11px 0 15px; font-size: 13px; }
            #splash-screen .zl-prism__launch { min-height: 52px; }
            .zl-prism__footer { margin-top: 10px; }
        }
        @media (max-width: 359px) {
            #splash-screen.zl-prism { padding-right: 15px; padding-left: 15px; }
            .zl-prism__hero { grid-template-rows: minmax(260px, 1fr) auto; }
            .zl-prism__title { font-size: 42px; }
        }
        @media (prefers-reduced-motion: reduce) {
            .zl-prism__image-frame { animation: none; }
            #splash-screen .zl-prism__launch { transition: none; }
        }

        /* Partner program: compact level ledger, live status, and explicit payout rules. */
        #partner-program.partner-card {
            --partner-lime: #c9f36a; --partner-muted: #929c89;
            position: relative; overflow: hidden; padding: 18px 15px 16px;
            border: 1px solid rgba(197,238,135,.17);
            background: linear-gradient(148deg, rgba(24,30,21,.98), rgba(14,18,14,.98) 65%);
            box-shadow: 0 18px 40px rgba(0,0,0,.3), inset 0 1px 0 rgba(255,255,255,.045);
        }
        #partner-program.partner-card::before {
            content: ""; position: absolute; width: 230px; height: 230px; right: -150px; top: -130px;
            border: 1px solid rgba(201,243,106,.09); border-radius: 50%;
            box-shadow: 0 0 0 25px rgba(201,243,106,.018), 0 0 0 55px rgba(201,243,106,.012);
            pointer-events: none;
        }
        #partner-program .partner-head, #partner-program .partner-table,
        #partner-program .partner-wallet, #partner-program .partner-earnings,
        #partner-program .partner-invite, #partner-program .partner-rules { position: relative; z-index: 1; }
        #partner-program .partner-head { display:flex; align-items:flex-start; justify-content:space-between; gap:12px; margin-bottom:14px; }
        #partner-program .partner-kicker { margin:0 0 6px; color:var(--partner-lime); font-size:9px; font-weight:750; letter-spacing:.16em; }
        #partner-program .partner-head h3 { margin:0; color:#eef2e9; font-size:19px; line-height:1.15; letter-spacing:-.035em; }
        #partner-program .partner-intro { margin:8px 0 0; color:#a3ab9b; font-size:12px; line-height:1.45; }
        #partner-program .partner-level-count { flex:none; margin-top:2px; padding:6px 8px; border:1px solid rgba(201,243,106,.2); border-radius:7px; color:#a8c87a; font:700 8px ui-monospace,monospace; letter-spacing:.1em; }
        #partner-program .partner-table { overflow:hidden; border:1px solid rgba(201,243,106,.12); border-radius:13px; background:rgba(7,10,7,.48); }
        #partner-program .partner-table-head, #partner-program .partner-table-row {
            display:grid; grid-template-columns:minmax(112px,1fr) 52px 68px; align-items:center; column-gap:7px;
        }
        #partner-program .partner-table-head {
            min-height:34px; padding:0 9px; border-bottom:1px solid rgba(255,255,255,.08);
            color:#77816e; font:700 8px ui-monospace,monospace; letter-spacing:.075em;
        }
        #partner-program .partner-table-head span:last-child { text-align:right; }
        #partner-program .partner-table-row { min-height:57px; padding:7px 9px; border-bottom:1px solid rgba(255,255,255,.055); }
        #partner-program .partner-table-row:last-child { border-bottom:0; }
        #partner-program .partner-level-name { display:flex; align-items:center; gap:9px; min-width:0; }
        #partner-program .partner-level-number {
            display:grid; flex:none; width:27px; height:27px; place-items:center; border:1px solid rgba(201,243,106,.19);
            border-radius:8px; color:#bddc84; background:rgba(201,243,106,.055); font:700 10px ui-monospace,monospace;
        }
        #partner-program .partner-level-name strong { display:block; overflow:hidden; color:#e4eadc; font-size:11px; font-weight:650; text-overflow:ellipsis; white-space:nowrap; }
        #partner-program .partner-level-name small, #partner-program .partner-count small { display:block; margin-top:3px; color:#788171; font-size:8px; letter-spacing:.055em; }
        #partner-program .partner-rate { color:var(--partner-lime); font-size:15px; font-weight:740; letter-spacing:-.04em; }
        #partner-program .partner-count { text-align:right; }
        #partner-program .partner-count strong { display:block; color:#e3e9dc; font:600 12px ui-monospace,monospace; }
        #partner-program .partner-network-total { display:flex; justify-content:space-between; padding:10px 10px 1px; color:#9ba391; font-size:10px; }
        #partner-program .partner-network-total strong { color:#e7ecdf; font:650 12px ui-monospace,monospace; }
        #partner-program .partner-wallet {
            display:flex; align-items:center; gap:10px; min-height:60px; margin:11px 0; padding:10px 12px;
            border:1px solid rgba(201,243,106,.15); border-radius:13px;
            background:linear-gradient(100deg,rgba(163,208,79,.09),rgba(17,23,15,.86));
        }
        #partner-program .partner-wallet-icon {
            display:grid; flex:none; width:32px; height:32px; place-items:center; border-radius:10px;
            color:var(--partner-lime); background:rgba(201,243,106,.11); font-size:16px;
        }
        #partner-program .partner-wallet-copy { flex:1; min-width:0; }
        #partner-program .partner-wallet-copy strong, #partner-program .partner-wallet-copy small { display:block; }
        #partner-program .partner-wallet-copy strong { color:#e6ebdf; font-size:11px; font-weight:650; }
        #partner-program .partner-wallet-copy small { margin-top:3px; color:#8e9884; font-size:9px; line-height:1.35; }
        #partner-program .partner-wallet-badge { display:flex; flex:none; align-items:center; gap:4px; color:#bddc84; font:700 8px ui-monospace,monospace; letter-spacing:.04em; }
        #partner-program .partner-wallet-badge::before { content:""; width:5px; height:5px; border-radius:50%; background:var(--partner-lime); }
        #partner-program .partner-wallet.is-unlinked .partner-wallet-badge { color:#e7bd73; }
        #partner-program .partner-wallet.is-unlinked .partner-wallet-badge::before { background:#e7bd73; }
        #partner-program .partner-earnings { margin:0 0 12px; padding:12px; border:1px solid rgba(255,255,255,.075); border-radius:12px; background:rgba(7,10,7,.42); }
        #partner-program .partner-earnings-title { margin:0 0 7px; color:#87917d; font:700 10px ui-monospace,monospace; letter-spacing:.1em; text-transform:uppercase; }
        #partner-program .partner-currency-totals { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:8px; margin:8px 0 10px; }
        #partner-program .partner-currency-card { min-width:0; padding:10px; border:1px solid rgba(201,243,106,.12); border-radius:10px; background:rgba(13,18,12,.68); }
        #partner-program .partner-currency-card > span { display:block; color:#a3ad96; font:700 10px ui-monospace,monospace; letter-spacing:.1em; text-transform:uppercase; }
        #partner-program .partner-currency-card > strong { display:block; margin:6px 0 0; color:#d4f77b; font:700 18px ui-monospace,monospace; overflow-wrap:anywhere; }
        #partner-program .partner-earnings-empty { margin:0; color:#a4ad9a; font-size:11px; }
        #partner-program .partner-invite { padding:14px 13px 12px; border:1px solid rgba(201,243,106,.13); border-radius:13px; background:rgba(7,10,7,.38); }
        #partner-program .partner-invite-title { margin:0 0 4px; color:#e5ebdc; font-size:13px; font-weight:680; }
        #partner-program .partner-invite-note { margin:0 0 11px; color:#929c89; font-size:10px; line-height:1.45; }
        #partner-program .partner-link-label { display:block; margin:0 0 6px; color:#818b77; font:700 8px ui-monospace,monospace; letter-spacing:.11em; }
        #partner-program .partner-link-row { display:flex; align-items:center; gap:7px; }
        #partner-program .partner-link-row .partner-link-input {
            width:100%; min-width:0; flex:1; height:42px; margin:0; padding:0 10px; border:1px solid rgba(255,255,255,.1);
            border-radius:9px; outline:none; background:#0a0d09; color:#d7e0cc;
            font:11px ui-monospace,SFMono-Regular,Menlo,monospace; text-overflow:ellipsis;
        }
        #partner-program .partner-link-row .partner-link-input:focus-visible { border-color:#a9d64c; box-shadow:0 0 0 2px rgba(201,243,106,.18); }
        #partner-program .partner-copy-button {
            display:flex; width:100%; min-height:43px; align-items:center; justify-content:space-between; margin:8px 0 0; padding:0 13px;
            border:1px solid rgba(201,243,106,.32); border-radius:9px;
            color:#131a0c; background:linear-gradient(145deg,#c9f36a,#83b631); font-size:11px; font-weight:700;
        }
        #partner-program .partner-copy-button span { font-size:15px; font-weight:500; }
        #partner-program .partner-copy-button:focus-visible, #partner-program .btn-copy:focus-visible { outline:2px solid #e5ffb3; outline-offset:3px; }
        #partner-program .partner-copy-status { min-height:12px; margin:7px 0 0; color:#75816c; font-size:9px; }
        #partner-program .partner-rules { margin-top:13px; border-top:1px solid rgba(255,255,255,.08); padding-top:11px; }
        #partner-program .partner-rules summary { color:#c7d1ba; font-size:11px; font-weight:650; cursor:pointer; }
        #partner-program .partner-rules ul { margin:9px 0 0; padding-left:17px; color:#9aa493; font-size:10px; line-height:1.55; }
        #partner-program .partner-rules li { margin:6px 0; padding-left:2px; }
        #partner-program .partner-sr-only {
            position:absolute !important; width:1px; height:1px; padding:0; margin:-1px; overflow:hidden;
            clip:rect(0,0,0,0); white-space:nowrap; border:0;
        }
        @media (max-width: 360px) {
            #partner-program.partner-card { padding:15px 12px 13px; }
            #partner-program .partner-table-head, #partner-program .partner-table-row { grid-template-columns:minmax(100px,1fr) 46px 58px; column-gap:5px; padding-right:7px; padding-left:7px; }
            #partner-program .partner-table-head { font-size:7px; }
            #partner-program .partner-level-name { gap:7px; }
            #partner-program .partner-level-name strong { font-size:10px; }
            #partner-program .partner-rate { font-size:14px; }
            #partner-program .partner-wallet { gap:7px; padding-right:8px; padding-left:8px; }
            #partner-program .partner-wallet-badge { font-size:7px; }
        }
        /* Zer0Life AI Trader: carry the Solana Prism material language into the terminal. */
        :root {
            --zl-acid: #c8ff37;
            --zl-mint: #69dda7;
            --zl-glass-line: rgba(202, 241, 152, .15);
            --zl-panel-top: rgba(21, 29, 18, .96);
            --zl-panel-bottom: rgba(10, 14, 10, .97);
        }
        body {
            background:
                radial-gradient(ellipse at 84% 0%, rgba(123, 177, 31, .13), transparent 34%),
                radial-gradient(ellipse at 4% 60%, rgba(32, 89, 61, .1), transparent 38%),
                #070906;
        }
        #app .tab-panel {
            position: relative;
            isolation: isolate;
            background:
                radial-gradient(ellipse at 94% 7%, rgba(161, 221, 50, .075), transparent 27%),
                linear-gradient(180deg, rgba(8, 12, 8, .14), rgba(6, 8, 6, .04));
        }
        #app .tab-panel::before {
            content: "";
            position: absolute;
            z-index: 0;
            top: 4px;
            right: -9px;
            width: 172px;
            height: 142px;
            pointer-events: none;
            opacity: .2;
            background: url("/static/zerolife-ai-trader-core.webp") center / cover no-repeat;
            mix-blend-mode: screen;
            -webkit-mask-image: radial-gradient(ellipse at 53% 48%, #000 22%, rgba(0,0,0,.86) 47%, transparent 77%);
            mask-image: radial-gradient(ellipse at 53% 48%, #000 22%, rgba(0,0,0,.86) 47%, transparent 77%);
        }
        #app .tab-panel > * { position: relative; z-index: 1; }
        #app .card:not(#partner-program), #app .deposit-modal {
            position: relative;
            overflow: hidden;
            color: #edf2e7;
            background: linear-gradient(148deg, var(--zl-panel-top), var(--zl-panel-bottom) 72%) !important;
            border: 1px solid var(--zl-glass-line) !important;
            border-radius: 21px;
            box-shadow:
                0 16px 34px rgba(0, 0, 0, .34),
                inset 0 1px 0 rgba(238, 255, 220, .065),
                inset 0 -1px 0 rgba(0, 0, 0, .42);
            -webkit-backdrop-filter: blur(12px);
            backdrop-filter: blur(12px);
        }
        #app .banner-limit {
            position: relative;
            overflow: hidden;
            background: linear-gradient(132deg, rgba(29, 40, 18, .97), rgba(12, 18, 12, .96) 72%) !important;
            border: 1px solid rgba(200, 255, 55, .2) !important;
            border-radius: 17px;
            box-shadow: 0 12px 28px rgba(0, 0, 0, .27), inset 0 1px 0 rgba(239, 255, 223, .08);
        }
        #app .banner-limit::after, #passkey-settings::after, #tab-deposit .deposit-modal::after {
            content: "";
            position: absolute;
            z-index: 0;
            top: -18px;
            right: -13px;
            width: 104px;
            height: 104px;
            pointer-events: none;
            opacity: .22;
            background: url("/static/zerolife-ai-trader-core.webp") center / cover no-repeat;
            mix-blend-mode: screen;
            -webkit-mask-image: radial-gradient(circle at center, #000 22%, rgba(0,0,0,.82) 48%, transparent 76%);
            mask-image: radial-gradient(circle at center, #000 22%, rgba(0,0,0,.82) 48%, transparent 76%);
        }
        #app .banner-limit > *, #passkey-settings > *, #tab-deposit .deposit-modal > * {
            position: relative;
            z-index: 1;
        }
        #app .banner-limit > div:first-child { color: var(--zl-acid) !important; }
        #app .banner-limit [style*="color"] { color: #b5c4a9 !important; }
        #app .page-heading { min-height: 52px; margin-top: 3px; }
        #app .page-heading h1 {
            color: #f2f5eb;
            font-weight: 760;
            letter-spacing: -.06em;
            text-shadow: 0 2px 22px rgba(177, 228, 68, .11);
        }
        #app .eyebrow {
            color: #b7cd8e;
            font: 650 10px/1.4 ui-monospace, SFMono-Regular, Menlo, monospace;
            letter-spacing: .15em;
        }
        #app .section-note, #app .muted { color: #a3ad9b; }
        #app .stat-box {
            background: linear-gradient(145deg, rgba(8, 13, 8, .9), rgba(18, 27, 16, .92)) !important;
            border: 1px solid rgba(210, 235, 194, .12) !important;
            border-radius: 14px;
            box-shadow: inset 0 1px 0 rgba(240, 255, 224, .035), inset 0 -10px 18px rgba(0, 0, 0, .14);
        }
        #app .stat-label {
            color: #a9b59c !important;
            letter-spacing: .13em;
        }
        #app .stat-val { color: #f1f4e9 !important; }
        #app #port-withdrawable { color: var(--zl-acid) !important; }
        #app [style*="color: #22c55e"], #app [style*="color: #4ade80"] { color: var(--zl-mint) !important; }
        #app [style*="color: #60a5fa"] { color: #91c2de !important; }
        #app [style*="color: #f87171"] { color: #ed9290 !important; }
        #app .btn-ctl, #app .btn-copy, #app .wallet-tab, #app .token-btn,
        #app .btn-green-main, #app .btn-verify, #app .action-btn {
            min-height: 46px;
            border-radius: 13px;
            transition: transform .16s ease, box-shadow .16s ease, filter .16s ease;
        }
        #app .btn-copy, #app .btn-refresh, #app .wallet-tab, #app .token-btn {
            color: #dce6d7;
            background: linear-gradient(145deg, rgba(24, 33, 23, .98), rgba(12, 18, 14, .98));
            border: 1px solid rgba(218, 239, 196, .14);
            box-shadow: inset 0 1px 0 rgba(255,255,255,.045), 0 3px 9px rgba(0,0,0,.18);
        }
        #app .wallet-tab.active, #app .token-btn.active {
            color: #e0ff9c;
            background: linear-gradient(145deg, rgba(53, 75, 28, .9), rgba(24, 42, 20, .96));
            border-color: rgba(200, 255, 55, .36);
            box-shadow: inset 0 1px 0 rgba(240,255,210,.12), 0 0 18px rgba(171, 222, 52, .08);
        }
        #app .btn-green-main, #app .btn-verify, #app .action-btn {
            color: #152006 !important;
            background: linear-gradient(145deg, #d8ff78 0%, #c8ff37 55%, #a9e62c 100%) !important;
            border: 1px solid rgba(230, 255, 169, .72) !important;
            box-shadow: inset 0 1px 0 rgba(255,255,255,.55), 0 3px 0 #668d19, 0 9px 22px rgba(155, 205, 43, .13);
            font-weight: 760;
        }
        #app .btn-green-main:active:not(:disabled), #app .btn-verify:active:not(:disabled),
        #app .action-btn:active:not(:disabled) {
            transform: translateY(2px);
            box-shadow: inset 0 1px 0 rgba(255,255,255,.4), 0 1px 0 #668d19;
        }
        #app .btn-toggle-off {
            color: #e8a2a0;
            background: linear-gradient(145deg, rgba(66, 31, 31, .8), rgba(35, 19, 21, .94));
            border: 1px solid rgba(225, 125, 116, .3);
            box-shadow: inset 0 1px 0 rgba(255,255,255,.04);
        }
        #app .sig-input {
            color: #edf3e8;
            background: linear-gradient(145deg, #080d09, #101810) !important;
            border: 1px solid rgba(210, 235, 194, .16) !important;
            border-radius: 12px;
            box-shadow: inset 0 3px 9px rgba(0,0,0,.28), inset 0 1px 0 rgba(255,255,255,.025);
        }
        #app .sig-input:focus-visible {
            border-color: rgba(200, 255, 55, .62) !important;
            box-shadow: 0 0 0 3px rgba(200, 255, 55, .1), inset 0 2px 7px rgba(0,0,0,.22);
        }
        #app .address-box, #app .order-card, #app .log-console {
            color: #cce995;
            background: linear-gradient(145deg, #080d09, #111910);
            border-color: rgba(210, 235, 194, .13);
            border-radius: 13px;
        }
        #app .wallet-tabs {
            background: rgba(5, 9, 6, .8);
            border-color: rgba(210, 235, 194, .13);
        }
        #app .nav-btn { color: #919b8d; }
        #nav.bottom-nav {
            background: rgba(8, 12, 8, .93);
            border-top: 1px solid rgba(210, 235, 194, .14);
            box-shadow: 0 -12px 30px rgba(0, 0, 0, .34), inset 0 1px 0 rgba(255,255,255,.025);
            -webkit-backdrop-filter: blur(20px);
            backdrop-filter: blur(20px);
        }
        #nav .nav-btn {
            min-height: 56px;
            border: 1px solid transparent;
            border-radius: 13px;
        }
        #nav .nav-btn.active {
            color: #d7ff78;
            background: linear-gradient(145deg, rgba(45, 64, 23, .78), rgba(20, 35, 18, .88));
            border-color: rgba(200, 255, 55, .14);
            box-shadow: inset 0 1px 0 rgba(244,255,226,.07), 0 3px 14px rgba(0,0,0,.18);
        }
        #app details summary { color: #d3dfc8; }
        #app a { color: #c9ed7d; }
        @media (max-width: 380px) {
            #app .tab-panel { padding-right: 14px; padding-left: 14px; }
            #app .card:not(#partner-program), #app .deposit-modal { padding: 15px; }
            #app .stat-box { padding: 11px; }
            #app .stat-val { font-size: 15px; }
        }
        @media (prefers-reduced-motion: reduce) {
            #app .btn-ctl, #app .btn-copy, #app .wallet-tab, #app .token-btn,
            #app .btn-green-main, #app .btn-verify, #app .action-btn {
                transition-duration: .01ms !important;
            }
        }
    </style>
</head>
<body>
    <div id="splash-screen" class="zl-prism">
        <header class="zl-prism__top">
            <div class="zl-prism__wordmark" aria-label="Zer0Life AI Trader">
                <span class="zl-prism__mark" aria-hidden="true"><i></i><i></i><i></i></span>
                <span class="zl-prism__brand">Zer<span class="zl-prism__brand-zero">0</span>Life</span>
            </div>
            <span class="zl-prism__network"><i class="zl-prism__network-dot"></i> SOLANA / USDC</span>
        </header>
        <section class="zl-prism__hero" aria-labelledby="splash-title">
            <div class="zl-prism__art" aria-hidden="true">
                <div class="zl-prism__halo"></div>
                <div class="zl-prism__image-frame"><img src="__SPLASH_URL__" alt=""></div>
                <span class="zl-prism__orbit"></span>
                <span class="zl-prism__orbit zl-prism__orbit--two"></span>
                <span class="zl-prism__annotation zl-prism__annotation--top">01 <i></i> AI SIGNAL / REVIEW</span>
                <span class="zl-prism__annotation zl-prism__annotation--bottom">PERSONAL EXECUTION LAYER</span>
            </div>
            <div class="zl-prism__copy">
                <p class="zl-prism__eyebrow"><span class="zl-prism__eyebrow-line"></span> YOUR CAPITAL. YOUR CALL.</p>
                <h1 id="splash-title" class="zl-prism__title">Zer0Life<br><em>AI Trader</em></h1>
                <p class="splash-caption">See what the AI sees. Stay in control of every move.</p>
                <button id="launch-terminal" class="start-btn zl-prism__launch" onclick="openTerminal()">Launch Terminal</button>
                <p class="zl-prism__note"><span class="zl-prism__pulse"></span> SOLANA · USDC <i></i> DIRECT CONTROL</p>
            </div>
        </section>
        <footer class="zl-prism__footer"><span>AI-ASSISTED · USER-DIRECTED</span><span class="zl-prism__footer-rule"></span><span>TRADE WITH CLARITY</span></footer>
    </div>

    <section id="account-signup" class="card" data-account-version="telegram-passkey-v1" data-entry-fix="signup-above-splash-v1" style="display:none;position:fixed;inset:7vh 18px auto;z-index:10001;max-width:460px;max-height:82vh;overflow-y:auto;-webkit-overflow-scrolling:touch;margin:auto">
        <h2>Create your account</h2><p class="muted">Your verified Telegram account is your login. No email or password required.</p>
        <label for="account-nickname">Nickname</label>
        <input id="account-nickname" class="sig-input" autocomplete="nickname" minlength="3" maxlength="24" pattern="[A-Za-z0-9_]{3,24}" placeholder="Your nickname">
        <p class="muted">3–24 letters, numbers or underscores. Your balance stays linked to your Telegram account, not this name.</p>
        <label style="display:flex;gap:10px"><input id="account-risk" type="checkbox" style="width:auto">I understand trading can lose money and returns are not guaranteed.</label>
        <div id="affiliate-terms" hidden>
            <label style="display:flex;gap:10px;margin-top:12px"><input id="account-partner-terms" type="checkbox" style="width:auto">I understand this account was referred. Up to 1.75% of realized trading profit may be paid to its referral partners: 1% / 0.5% / 0.25% by level, after prior trading losses are recovered. Profit means sale proceeds minus recorded purchase cost; separately paid network fees are not netted here. Deposits and open trades are excluded; USDC and SOL are accounted for separately.</label>
        </div>
        <button id="account-create" class="btn-green-main" onclick="createAccount()">Create account</button>
        <p id="account-error" class="status-output" role="status"></p>
    </section>
    <div id="app" data-ui-version="console-ai-v1" style="display: none;">
        <div id="tab-terminal" class="tab-panel active">
            <header class="page-heading"><div><p class="eyebrow">AI Trader · Personal account</p><h1>Overview</h1></div></header>
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
                    <span id="refresh-icon" aria-hidden="true">↻</span>
                    <span>Refresh</span>
                </button>
            </div>

            <button class="btn-sell-all" onclick="closeAllPositions()">
                <span>Close all my positions</span>
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
                <p id="sniper-position-cap" class="section-note">Screened Solana candidates · Checking position limit…</p>
                <div id="discovery-status" class="section-note">Candidate scan pending.</div>
                <button class="action-btn" id="scan-candidates" onclick="scanCandidates()">Scan candidates</button>
                <details><summary>Candidate shortlist</summary><div id="candidate-list" class="section-note" style="white-space:pre-line">No verified shortlist yet.</div></details>
                <details><summary>Whale buys · 1–10 SOL</summary><div id="whale-signal-status" class="section-note" style="white-space:pre-line">Checking Helius stream...</div></details>
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
                <p class="section-note">Core entry cost budget: 2.50% of the order. Includes quoted DEX fees, price impact, slippage, estimated network fees, Jito tips and account rent for entry/exit. Estimates do not guarantee profit or exit costs. SELL exits are not blocked by this filter.</p>
                <p id="entry-cost-check" class="section-note" aria-live="polite">No entry cost check yet.</p>
            </div>

            <div class="card">
                <div style="font-size: 12px; font-weight: 700; color: #94a3b8; margin-bottom: 6px;">ACTIVITY LOG</div>
                <div style="font-size:11px;color:#94a3b8;margin-bottom:8px;">AI decisions are not confirmed trades · times in your timezone</div>
                <div id="log-box" class="log-console" data-activity-version="decision-history-v1">Loading activity...</div>
            </div>
        </div>

        <div id="tab-stats" class="tab-panel">
            <div class="card">
                <div style="display:grid;grid-template-columns:minmax(0,1fr) auto;gap:12px;align-items:start;">
                    <div>
                        <div style="font-size: 11px; color: #94a3b8; text-transform: uppercase;">Realized PnL · USDC</div>
                        <div id="pnl-val" style="font-size: 24px; font-weight: 900; color: #4ade80; font-family: monospace; margin-top: 4px;">+$0.00</div>
                        <div style="font-size: 11px; color: #94a3b8; text-transform: uppercase; margin-top:12px;">Realized PnL · SOL</div>
                        <div id="pnl-sol-val" style="font-size: 20px; font-weight: 900; color: #4ade80; font-family: monospace; margin-top: 4px;">+0.000000000 SOL</div>
                    </div>
                    <div style="text-align: right;">
                        <div style="font-size: 11px; color: #94a3b8;">Closed trades</div>
                        <div id="closed-count" style="font-size: 18px; font-weight: 800; color: #fff; font-family: monospace;">0</div>
                    </div>
                </div>
                <div class="section-note" style="margin-top:12px;">Totals are shown separately in their settlement currencies; SOL is not converted to USDC.</div>
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
                    <div class="solpay-panel">
                        <h3>Pay with Solana Pay</h3>
                        <div class="solpay-note">Choose USDC or native SOL on Solana mainnet. The exact amount and invoice reference are added to the wallet request.</div>
                        <div class="token-selector" role="group" aria-label="Deposit asset" style="margin-top:12px;">
                            <button type="button" class="token-btn active" id="deposit-token-usdc" onclick="selectDepositToken('USDC')">USDC</button>
                            <button type="button" class="token-btn" id="deposit-token-sol" onclick="selectDepositToken('SOL')">SOL</button>
                        </div>
                        <label id="solpay-amount-label" for="solpay-amount">Amount in USDC</label>
                        <input id="solpay-amount" class="sig-input" type="number" inputmode="decimal"
                            min="0.000001" step="0.000001" value="10" autocomplete="off">
                        <div id="solpay-presets" class="token-selector solpay-presets" role="group" aria-label="Quick amount choices"></div>
                        <button class="btn-verify" id="solpay-create" type="button" onclick="createSolanaPayInvoice()">Create payment request</button>
                        <p id="solpay-status" class="solpay-status" role="status" aria-live="polite">Select an asset and amount to continue.</p>
                        <div id="solpay-checkout" class="solpay-checkout" hidden>
                            <div class="solpay-checkout-row"><span>Pay exactly</span><strong id="solpay-checkout-amount"></strong></div>
                            <div class="solpay-checkout-row"><span>Recipient</span><strong id="solpay-recipient"></strong></div>
                            <div class="solpay-checkout-row"><span>Request expires</span><strong id="solpay-expiration"></strong></div>
                            <a id="solpay-open-wallet" class="btn-green-main solpay-open-wallet">Open in Solana wallet</a>
                            <a id="solpay-status-page" href="#" target="_blank" rel="noopener noreferrer"
                                style="display:block;color:#86efac;font-size:11px;text-align:center;margin:8px 0;">Open payment status page</a>
                            <div class="solpay-note">Your wallet needs the full amount in the selected asset and a little SOL for network fees. It shows the fee before approval. The bot credits your balance after Solana finalizes the exact transfer. Do not pay twice.</div>
                        </div>
                    </div>

                    <details class="solpay-fallback">
                        <summary>Use a standard transfer and verify its transaction</summary>
                        <div class="solpay-fallback-content">
                            <p class="solpay-fallback-note">For manual deposits, verify your wallet once, send USDC or SOL to the address below, then submit the transaction signature.</p>
                    <div class="banner-limit">
                        <div style="color:#4ade80; font-weight:800;">USDC / SOL · Solana mainnet</div>
                        <div style="font-size:11px; margin-top:6px;">Manual transfer fallback. No Memo required.</div>
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
                    </details>
                </div>

                <div id="section-withdraw" style="display: none;">
                    <div class="banner-limit">
                        <div style="color:#4ade80;font-weight:800;">USDC / SOL · Solana mainnet</div>
                        <div style="font-size:11px;margin-top:6px;">Confirm withdrawals with a one-time Telegram code.</div>
                    </div>
                    <div id="chain-withdraw-balances" style="font-size:12px; margin-bottom:8px;">Loading balance...</div>
                    <div class="token-selector">
                        <button class="token-btn active" id="wtoken-usdc" onclick="selectWithdrawToken('USDC')">USDC</button>
                        <button class="token-btn" id="wtoken-sol" onclick="selectWithdrawToken('SOL')">Solana (SOL)</button>
                    </div>
                    <input id="withdraw-address" class="sig-input" placeholder="Recipient wallet address" oninput="clearWithdrawalCode()">
                    <input id="withdraw-amount" type="number" step="any" class="sig-input" placeholder="Amount" oninput="clearWithdrawalCode();updateWithdrawalFeePreview()">
                    <p id="withdrawal-fee-preview" class="status-output" role="status" aria-live="polite">
                        Non-admin withdrawals have a 1.5% fee, credited to the administrator in the same currency.
                    </p>
                    <button class="btn-copy" id="btn-withdraw-code" onclick="requestWithdrawalCode()">Send code in Telegram</button>
                    <input id="withdraw-code" class="sig-input" type="text" inputmode="numeric" autocomplete="one-time-code"
                        maxlength="6" pattern="[0-9]{6}" placeholder="6-digit Telegram code">
                    <p id="withdraw-code-status" class="status-output" role="status" aria-live="polite"></p>
                    <p id="withdraw-progress" class="status-output" role="status" aria-live="polite" hidden></p>
                    <button class="btn-verify" id="btn-withdraw-action" onclick="secureWithdraw()">Withdraw</button>
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
                <button class="btn-copy" onclick="checkBalanceCoverage()">Check wallet funds against account balances (read-only)</button>
                <div id="balance-coverage-details" class="status-output" role="status"></div>
                <h3>Fee reserve · SOL</h3>
                <div id="fee-status" class="status-output">Reserve not checked yet.</div>
                <p class="muted">Allocation reduces your available SOL balance. No on-chain transfer. Only your funds may be allocated.</p>
                <label for="fee-amount">Your SOL to allocate</label>
                <input id="fee-amount" class="sig-input" type="text" inputmode="decimal" value="0.01" autocomplete="off">
                <label style="display:flex;gap:10px;align-items:flex-start;font-size:12px;"><input id="fee-confirm" type="checkbox" style="width:auto;flex:0 0 auto;">I confirm these SOL belong to me, not to customers.</label>
                <button id="fee-submit" class="btn-copy" onclick="setupFeeReserve()">Allocate my SOL to fees</button>
                <div id="fee-result" class="status-output" role="status"></div>
                <p class="muted">You may return only the amount above the required minimum. This changes your bot balance only; it does not send a Solana transaction.</p>
                <label style="display:flex;gap:10px;align-items:flex-start;font-size:12px;"><input id="fee-release-confirm" type="checkbox" style="width:auto;flex:0 0 auto;">I confirm the fee reserve being returned is my SOL, not customer funds.</label>
                <button id="fee-release-submit" class="btn-copy" onclick="releaseExcessFeeReserve()">Return excess fee reserve</button>
                <div id="fee-release-result" class="status-output" role="status"></div>
                <button class="btn-copy" onclick="importOwnOpeningBalance()">Import my legacy balance</button>
                <details style="margin-top:14px;">
                    <summary>Import backed old customer balances</summary>
                    <p class="muted">One-time USDC ledger migration. Preview checks old balances, new-ledger activity, and unassigned on-chain backing. Accounts with ambiguous new activity are skipped. This updates the internal ledger only; it never sends a Solana transaction.</p>
                    <button id="legacy-balance-preview-button" class="btn-copy" onclick="previewLegacyBalances()">Preview eligible balances (read-only)</button>
                    <div id="legacy-balance-migration-status" class="status-output" role="status">No legacy balance preview has been run.</div>
                    <button id="legacy-balance-import-button" class="btn-copy" onclick="importLegacyBalances()" hidden disabled>Import eligible balances</button>
                </details>
            </div>
        </div>
        <div id="tab-trading" class="tab-panel">
            <header class="page-heading"><div><p class="eyebrow">Your capital only</p><h1>Trading</h1></div></header>
            <div id="trading-controls"></div>
            <div class="card"><h3 style="margin-top:0">AI activity</h3>
                <p id="ai-status" class="status-output">Checking AI status...</p>
                <button class="btn-copy" onclick="previewAI()">Check AI · no trade</button>
                <p id="ai-decisions" class="status-output"></p>
                <p id="ai-chart-zones" class="status-output"></p>
                <details>
                    <summary>Independent execution guard</summary>
                    <p class="section-note">AI proposes BUY / SELL / HOLD only. Server-owned checks decide whether an order is allowed. AI cannot change limits or disable checks.</p>
                    <p id="execution-policy-summary" class="section-note">Policy loading...</p>
                    <p id="execution-policy-result" class="status-output">No execution check recorded yet.</p>
                    <p class="section-note">Liquidity is checked through the executable route and price impact at the requested size, not a promise of market depth. Entry cost estimates are not profit forecasts. Entry-efficiency checks never block owned-position exits or withdrawals. A passed check is not proof of a filled trade.</p>
                </details>
            </div>
            <div id="trading-strategy"></div>
            <div id="trading-sniper"></div>
            <div class="card">
                <button class="btn-copy" onclick="checkTradingReadiness()">Check readiness · no trade</button>
                <div id="trading-readiness" class="status-output">Trading involves risk. No guaranteed returns.</div>
            </div>
            <details><summary>Logs &amp; position controls</summary><div id="trading-activity"></div></details>
        </div>
        <div id="tab-tariffs" class="tab-panel">
            <header class="page-heading"><div><p class="eyebrow">ACCESS PACKAGES</p><h1>Tariffs</h1></div></header>
            <div class="card">
                <h3 style="margin-top:0">AI Trader access</h3>
                <p class="section-note">Access packages do not deposit funds into your trading balance or change custody admission. Trading modes are not started by a payment.</p>
                <section id="telegram-tariff-view" hidden>
                    <p id="telegram-tariff-status" role="status" aria-live="polite">Checking access…</p>
                    <p id="telegram-tariff-countdown" class="section-note" aria-live="polite"></p>
                    <div class="tariff-packages">
                        <button type="button" class="tariff-package" data-telegram-package="zrl_7d">
                            <strong>7 days · 100,000 ZRL</strong><span>Tap to view package details.</span>
                        </button>
                        <button type="button" class="tariff-package" data-telegram-package="sol_30d">
                            <strong>30 days · 0.5 SOL</strong><span>Tap to view package details.</span>
                        </button>
                    </div>
                    <p id="telegram-tariff-note" class="section-note">Payment is separate from your trading balance. It does not enable Core or Sniper.</p>
                    <button id="telegram-tariff-checkout" class="btn-green-main" onclick="openTariffCheckout()">Open secure checkout</button>
                </section>
            </div>
        </div>
        <div id="tab-settings" class="tab-panel">
            <header class="page-heading"><div><p class="eyebrow">Account preferences</p><h1>Settings</h1></div></header>
            <div class="card" id="passkey-settings"><h3>Account protection</h3>
                <p id="passkey-state" class="status-output">Withdrawals use a one-time Telegram code. A passkey is required for standalone browser sign-in.</p>
                <button class="btn-copy" onclick="setupPasskey()">Set up / add browser passkey</button>
                <button class="btn-copy" onclick="setupPasskey(true)">Recover using a saved code</button>
                <p id="passkey-progress" class="status-output" role="status"></p>
                <a id="passkey-browser-link" hidden target="_blank" rel="noopener noreferrer">Open verification · hold to copy link</a>
                <details><summary>Passkeys &amp; account recovery</summary>
                    <p class="muted">Your device verifies you using Face ID, Touch ID or another system verification method. We verify the passkey signature, not a face photo. Face-only verification cannot be enforced on every device.</p>
                    <p class="muted">Each withdrawal is approved by a one-time code sent to your Telegram account. The code is tied to the exact amount, token and recipient address. Passkeys are used for standalone browser sign-in and account recovery, not for withdrawals.</p>
                    <p class="muted">Save recovery codes shown in the secure browser. Recovery requires your Telegram account, account security code and an unused recovery code; it replaces old passkeys.</p>
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
            <div class="card partner-card" id="partner-program">
                <div class="partner-head">
                    <div><p class="partner-kicker">ZER0LIFE · PARTNER DESK</p><h3>Partner program</h3><p class="partner-intro">Share eligible realized trading profit across three referral levels.</p></div>
                    <span class="partner-level-count">03 LEVELS</span>
                </div>
                <div class="partner-table" role="table" aria-label="Partner commission rates and referrals by level">
                    <div class="partner-table-head" role="row"><span role="columnheader">LEVEL / NETWORK</span><span role="columnheader">RATE</span><span role="columnheader">REFERRALS</span></div>
                    <div id="partner-level-rows" role="rowgroup">
                        <div class="partner-table-row" role="row"><div class="partner-level-name" role="cell"><span class="partner-level-number">01</span><span><strong>Direct</strong><small>DIRECT REFERRALS</small></span></div><strong class="partner-rate" role="cell">1%</strong><span class="partner-count" role="cell"><strong>0</strong><small>partners</small></span></div>
                        <div class="partner-table-row" role="row"><div class="partner-level-name" role="cell"><span class="partner-level-number">02</span><span><strong>Second level</strong><small>SECOND CIRCLE</small></span></div><strong class="partner-rate" role="cell">0.5%</strong><span class="partner-count" role="cell"><strong>0</strong><small>partners</small></span></div>
                        <div class="partner-table-row" role="row"><div class="partner-level-name" role="cell"><span class="partner-level-number">03</span><span><strong>Third level</strong><small>THIRD CIRCLE</small></span></div><strong class="partner-rate" role="cell">0.25%</strong><span class="partner-count" role="cell"><strong>0</strong><small>partners</small></span></div>
                    </div>
                    <div class="partner-network-total"><span>Active referral network</span><strong id="partner-network-total">0 partners</strong></div>
                </div>
                <div class="partner-wallet" id="partner-wallet-card">
                    <span class="partner-wallet-icon" aria-hidden="true">◇</span>
                    <span class="partner-wallet-copy"><strong id="partner-wallet-label">Partner earnings credit to your main balance</strong><small id="partner-wallet-detail">Withdraw credited USDC or SOL through Wallet.</small></span>
                    <span class="partner-wallet-badge" id="partner-wallet-badge">IN-APP</span>
                </div>
                <p id="partner-program-status" class="partner-sr-only" role="status">Loading partner balances…</p>
                <div class="partner-earnings">
                    <p class="partner-earnings-title">Accumulated partner commissions</p>
                    <div id="partner-currency-totals" class="partner-currency-totals" aria-live="polite">
                        <p class="partner-earnings-empty">Loading partner balances…</p>
                    </div>
                </div>
                <div class="partner-invite">
                    <h4 class="partner-invite-title">Your referral link</h4>
                    <p class="partner-invite-note">Share your link. Eligible partner activity is tracked automatically.</p>
                    <label class="partner-link-label" for="partner-invite-link">REFERRAL LINK</label>
                    <div class="partner-link-row">
                        <input id="partner-invite-link" class="sig-input partner-link-input" type="text" readonly value="Loading referral link…" aria-label="Your referral link">
                    </div>
                    <button class="partner-copy-button" type="button" onclick="copyPartnerLink()" aria-label="Copy referral link">Copy referral link <span aria-hidden="true">↗</span></button>
                    <p class="partner-copy-status" aria-live="polite">Loading your account-specific link…</p>
                </div>
                <details class="partner-rules">
                    <summary>How commissions and payouts work</summary>
                    <ul>
                        <li>Commissions use realized trade profit — sale proceeds minus recorded purchase cost — after prior trading losses are recovered.</li>
                        <li>USDC and SOL are accounted for separately. Open trades and deposits do not count.</li>
                        <li>Partner commissions are credited to the main available balance. Withdraw them through Wallet when ready.</li>
                        <li>Solana network fees are paid from the operator reserve and are not deducted from the partner’s commission.</li>
                    </ul>
                </details>
            </div>
            <details id="strategy-help"><summary>Strategy limits &amp; risks</summary>
                <p class="section-note">Core trades use your USDC; sniper trades use your SOL. Other users' funds and the operator fee reserve are never your trading capital. Automatic conversion or profit allocation between strategies is not implemented.</p>
                <p class="section-note">Core: SOL, JUP, RAY, PYTH, JTO, ORCA, KMNO, NOS, SHDW; up to 5 USDC-funded positions. ORCA currently retains mint authority (additional tokens may be issued); no token is risk-free. Sniper seeds: WIF, BONK, BOME, POPCAT, MEW, PENGU; up to 5 SOL-funded positions, limited by your available SOL and saved order size. Every 15 minutes a bounded DexScreener profiles/search scan checks up to 90 Solana mints, screens up to 20 and sends at most 8 current candidates plus held assets to AI. This is not every project or a profit forecast; profiles alone do not establish meme status.</p>
                <p class="section-note">Core BUY requires the existing trend and flow checks plus fresh 5-minute/1-hour OHLCV data and a confirmed support-hold or breakout-retest setup. Chart zones use closed candles from the selected Solana pool; missing or stale candles block new Core buys. Zones and indicators are estimates, not predictions, and this strategy is not backtested. Core TP/SL and Sniper rules are unchanged.</p>
                <p class="section-note">Confirmed buy, sell, take-profit, and stop-loss alerts are sent to each participant's own Telegram chat. Enable Telegram notifications on your phone. Alerts are sent after Solana finalizes the trade; a safely failed stop-loss sale gets a separate alert saying the position remains open.</p>
                <p class="section-note">Sniper entry filters: approved DEX, at least $10k pool liquidity, $100k 24h volume, 200 daily trades, pool age 24h+, extreme-pump exclusion, standard SPL mint with no mint/freeze authority. The shortlist rotates every 15 minutes through up to 20 on-chain-verified candidates; AI sees up to 8 at a time. After any completed close, all participants are blocked from re-buying that mint for 24 hours. Fresh eligibility is required for every buy. Scores rank liquidity/activity, not expected profit. Holder concentration, LP locks and every contract/market risk are not fully checked. Standard mint checks do not guarantee token safety.</p>
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
        <button id="nav-tariffs" class="nav-btn" onclick="gotoTab('tariffs', this)"><span>▤</span>Tariffs</button>
    </div>
    <div id="telegram-tariff-modal" role="dialog" aria-modal="true" aria-labelledby="telegram-tariff-modal-title" aria-hidden="true">
        <div class="card">
            <h2 id="telegram-tariff-modal-title">Free access has ended</h2>
            <p id="telegram-tariff-modal-message" class="section-note">Choose a time-limited package to continue.</p>
            <button id="telegram-tariff-modal-checkout" class="btn-green-main" onclick="openTariffCheckout()">View tariffs and pay</button>
            <button class="btn-copy" onclick="dismissTelegramTariffModal()">Not now</button>
        </div>
    </div>
    <div id="telegram-package-modal" role="dialog" aria-modal="true" aria-labelledby="telegram-package-modal-title" aria-hidden="true">
        <div class="card">
            <h2 id="telegram-package-modal-title">Access package</h2>
            <p id="telegram-package-modal-description" class="section-note"></p>
            <p id="telegram-package-modal-status" class="section-note" role="status" aria-live="polite">Checking account access…</p>
            <button id="telegram-package-modal-checkout" class="btn-green-main" hidden>Continue to secure checkout</button>
            <button class="btn-copy" onclick="closeTelegramPackage()">Close</button>
        </div>
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
        async function loadPartnerProgram() {
            const link = document.getElementById('partner-invite-link');
            const status = document.getElementById('partner-program-status');
            const levelRows = document.getElementById('partner-level-rows');
            const networkTotal = document.getElementById('partner-network-total');
            const walletCard = document.getElementById('partner-wallet-card');
            const walletLabel = document.getElementById('partner-wallet-label');
            const walletDetail = document.getElementById('partner-wallet-detail');
            const walletBadge = document.getElementById('partner-wallet-badge');
            const currencyTotals = document.getElementById('partner-currency-totals');
            const copyStatus = document.querySelector('.partner-copy-status');
            const levelInfo = [
                {name:'Direct', depth:'DIRECT REFERRALS'},
                {name:'Second level', depth:'SECOND CIRCLE'},
                {name:'Third level', depth:'THIRD CIRCLE'}
            ];
            const formatRate = value => {
                const bps=Number(value);
                return Number.isFinite(bps)&&bps>=0 ?
                    (bps/100).toFixed(2).replace(/\.?0+$/,'')+'%' : '—';
            };
            const formatAmount = (raw, asset) => {
                const decimals=asset==='SOL'?9:6;
                const value=Number(raw||0);
                return Number.isFinite(value)?(value/10**decimals).toFixed(decimals):'—';
            };
            try {
                const response=await apiFetch('/api/partners');
                const data=await response.json();
                if(!response.ok||!data.success)throw Error(data.message||'Partner program is unavailable.');
                link.value=data.invite_url||'Open this bot through Telegram to generate your referral link.';
                if(copyStatus)copyStatus.textContent=data.invite_url?'Your account-specific link is ready to share.':'Open this bot through Telegram to generate your link.';
                const rates=Array.isArray(data.rates_bps)?data.rates_bps:[100,50,25];
                const rawCounts=Array.isArray(data.referrals_by_level)?data.referrals_by_level:[0,0,0];
                const counts=[0,1,2].map(index=>Math.max(0,Math.floor(Number(rawCounts[index])||0)));
                if(levelRows){
                    levelRows.replaceChildren();
                    levelInfo.forEach((level,index)=>{
                        const row=document.createElement('div');row.className='partner-table-row';row.setAttribute('role','row');
                        const nameCell=document.createElement('div');nameCell.className='partner-level-name';nameCell.setAttribute('role','cell');
                        const number=document.createElement('span');number.className='partner-level-number';number.textContent=String(index+1).padStart(2,'0');
                        const nameWrap=document.createElement('span');
                        const name=document.createElement('strong');name.textContent=level.name;
                        const depth=document.createElement('small');depth.textContent=level.depth;
                        nameWrap.append(name,depth);nameCell.append(number,nameWrap);
                        const rate=document.createElement('strong');rate.className='partner-rate';rate.setAttribute('role','cell');rate.textContent=formatRate(rates[index]);
                        const referralCell=document.createElement('span');referralCell.className='partner-count';referralCell.setAttribute('role','cell');
                        const referralCount=document.createElement('strong');referralCount.textContent=counts[index].toLocaleString('en-US');
                        const referralLabel=document.createElement('small');referralLabel.textContent='partners';
                        referralCell.append(referralCount,referralLabel);row.append(nameCell,rate,referralCell);levelRows.appendChild(row);
                    });
                }
                const totalReferrals=counts.reduce((total,count)=>total+count,0);
                if(networkTotal)networkTotal.textContent=totalReferrals.toLocaleString('en-US')+' partners';
                if(walletCard)walletCard.classList.remove('is-unlinked');
                if(walletLabel)walletLabel.textContent='Partner earnings credit to your main balance';
                if(walletDetail)walletDetail.textContent='Withdraw credited USDC or SOL through Wallet.';
                if(walletBadge)walletBadge.textContent='IN-APP';
                status.textContent='Referrals by level: '+counts.join(' / ')+
                    '. Partner commissions are credited to your main balance for normal withdrawal.';
                if(currencyTotals){
                    currencyTotals.replaceChildren();
                    const totalsData=Array.isArray(data.earnings_totals)?data.earnings_totals:[];
                    const displayAsset=asset=>{
                        if(asset==='USDC'||asset==='EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v')return 'USDC';
                        if(asset==='SOL')return 'SOL';
                        if(asset==='ZRL')return 'ZRL';
                        return '';
                    };
                    const totalsByAsset=new Map();
                    for(const item of totalsData){
                        const asset=displayAsset(item.asset);
                        if(!asset)continue;
                        const total=totalsByAsset.get(asset)||{earned_raw:0};
                        total.earned_raw+=Number(item.earned_raw||0);
                        totalsByAsset.set(asset,total);
                    }
                    const assets=['USDC','SOL'];
                    if(totalsByAsset.has('ZRL'))assets.push('ZRL');
                    for(const asset of assets){
                        const item=totalsByAsset.get(asset)||{};
                        const card=document.createElement('div');card.className='partner-currency-card';
                        const label=document.createElement('span');label.textContent=asset;
                        const total=document.createElement('strong');
                        total.textContent=formatAmount(item.earned_raw,asset)+' '+asset;
                        card.append(label,total);currencyTotals.appendChild(card);
                    }
                }
            } catch(error) {
                if(link)link.value='Referral link unavailable.';
                if(status)status.textContent=error.message||'Partner program unavailable.';
                if(walletLabel)walletLabel.textContent='Partner status unavailable';
                if(walletDetail)walletDetail.textContent='Could not load wallet or payout status.';
                if(walletBadge)walletBadge.textContent='UNAVAILABLE';
                if(currencyTotals)currencyTotals.textContent='Could not load partner earnings.';
                if(earningsSummary)earningsSummary.textContent='Could not load partner earnings.';
                if(copyStatus)copyStatus.textContent='Partner referral link is unavailable.';
            }
        }
        async function copyPartnerLink() {
            const link=document.getElementById('partner-invite-link');
            const status=document.querySelector('.partner-copy-status');
            if(!link.value||!link.value.startsWith('https://t.me/')){
                if(status)status.textContent='Referral link is not ready yet.';
                return;
            }
            try {
                if(navigator.clipboard&&window.isSecureContext)await navigator.clipboard.writeText(link.value);
                else{
                    link.focus();link.select();
                    if(!document.execCommand('copy'))throw Error('Clipboard is unavailable.');
                }
                if(status)status.textContent='Referral link copied.';
            } catch(e) {
                link.focus();link.select();
                if(status)status.textContent='Could not copy. Select the link and copy it manually.';
            }
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
        function renderCoreChartZones(context, labels) {
            const output=document.getElementById('ai-chart-zones');
            if(!output)return;
            const rows=Object.entries(context||{});
            if(!rows.length) {
                output.textContent='Core chart zones · 5m/1h: waiting for fresh OHLCV candles. New Core buys are blocked until chart confirmation is available.';
                return;
            }
            const price=value=>Number.isFinite(Number(value))&&Number(value)>0?Number(value).toPrecision(5):'not established';
            output.textContent='Core chart zones · 5m entry / 1h context · USD\\n'+rows.map(([symbol,c])=>{
                const label=(labels||{})[symbol]||symbol;
                if(c.chart_ready!==1)
                    return label+': fresh candles unavailable ('+(c.chart_m5_candles||0)+' 5m / '+(c.chart_h1_candles||0)+' 1h); Core BUY blocked.';
                const trend=c.chart_trend_h1>0?'up':c.chart_trend_h1<0?'down':'range/unclear';
                const buy=Number(c.chart_support_low_usd)>0&&Number(c.chart_support_high_usd)>0
                    ?price(c.chart_support_low_usd)+'–'+price(c.chart_support_high_usd):'not established';
                const sell=Number(c.chart_resistance_low_usd)>0&&Number(c.chart_resistance_high_usd)>0
                    ?price(c.chart_resistance_low_usd)+'–'+price(c.chart_resistance_high_usd):'not established';
                const rsi=Number.isFinite(Number(c.chart_rsi14_5m))?Number(c.chart_rsi14_5m).toFixed(1):'n/a';
                const volume=Number.isFinite(Number(c.chart_volume_ratio_5m))?Number(c.chart_volume_ratio_5m).toFixed(2)+'×':'n/a';
                return label+': 1h '+trend+' · buy/support '+buy+' · sell/resistance '+sell
                    +' · 5m RSI '+rsi+' · relative volume '+volume
                    +' · confirmed entry '+(c.chart_buy_setup===1?'YES':'NO')
                    +(c.chart_sell_setup===1?' · exit evidence near resistance':'');
            }).join('\\n');
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

        let selectedDepositAsset = 'USDC';
        let activeSolanaPayInvoice = null;
        let solanaPayPollTimer = null;
        function solanaPayStorageKey() {
            const user = getUserData();
            return user?.id ? 'ai-trader-solpay-invoice:' + user.id : null;
        }
        function setSolanaPayStatus(message, state='') {
            const status = document.getElementById('solpay-status');
            status.textContent = message;
            status.dataset.state = state;
        }
        function lockSolanaPayInputs(locked) {
            document.getElementById('deposit-token-usdc').disabled = locked;
            document.getElementById('deposit-token-sol').disabled = locked;
            document.getElementById('solpay-amount').disabled = locked;
            document.querySelectorAll('#solpay-presets button').forEach(button => button.disabled = locked);
            document.getElementById('solpay-create').disabled = locked;
        }
        function selectDepositToken(token) {
            if (!['USDC', 'SOL'].includes(token)) return;
            selectedDepositAsset = token;
            const usdc = token === 'USDC';
            document.getElementById('deposit-token-usdc').classList.toggle('active', usdc);
            document.getElementById('deposit-token-sol').classList.toggle('active', !usdc);
            const input = document.getElementById('solpay-amount');
            input.step = usdc ? '0.000001' : '0.000000001';
            input.min = usdc ? '0.000001' : '0.000000001';
            input.value = usdc ? '10' : '0.1';
            document.getElementById('solpay-amount-label').textContent = 'Amount in ' + token;
            const values = usdc ? ['5', '10', '25', '50'] : ['0.05', '0.1', '0.25', '0.5'];
            const presets = document.getElementById('solpay-presets');
            presets.replaceChildren(...values.map(value => {
                const button = document.createElement('button');
                button.type = 'button';
                button.className = 'token-btn';
                button.textContent = value + ' ' + token;
                button.addEventListener('click', () => { input.value = value; });
                return button;
            }));
        }
        async function createSolanaPayInvoice() {
            const button = document.getElementById('solpay-create');
            if (activeSolanaPayInvoice?.state === 'OPEN' &&
                Number(activeSolanaPayInvoice.expires_at) > Date.now() / 1000) {
                setSolanaPayStatus('A payment request is already open. Use the same wallet link; do not create or pay twice.');
                document.getElementById('solpay-checkout').hidden = false;
                return;
            }
            const amount = document.getElementById('solpay-amount').value.trim();
            const decimals = selectedDepositAsset === 'USDC' ? 6 : 9;
            const validAmount = selectedDepositAsset === 'USDC'
                ? /^\d+(?:\.\d{1,6})?$/.test(amount)
                : /^\d+(?:\.\d{1,9})?$/.test(amount);
            if (!validAmount || Number(amount) <= 0) {
                setSolanaPayStatus('Enter a positive amount with up to ' + decimals + ' decimal places.', 'error');
                return;
            }
            button.disabled = true;
            setSolanaPayStatus('Creating your exact payment request…');
            try {
                const response = await apiFetch('/api/deposit_invoice', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({token: selectedDepositAsset, amount})
                });
                const invoice = await response.json();
                if (!response.ok || !invoice.success || !invoice.solana_pay_uri) {
                    throw new Error('Could not create a Solana Pay request. Check your account access and try again.');
                }
                activeSolanaPayInvoice = invoice;
                const storageKey = solanaPayStorageKey();
                if (storageKey) {
                    try { sessionStorage.setItem(storageKey, invoice.invoice_id); } catch(e) {}
                }
                await showSolanaPayInvoice(invoice);
                lockSolanaPayInputs(invoice.state === 'OPEN' &&
                    Number(invoice.expires_at) > Date.now() / 1000);
                if (solanaPayPollTimer) clearInterval(solanaPayPollTimer);
                solanaPayPollTimer = setInterval(refreshSolanaPayInvoice, 5000);
            } catch(e) {
                setSolanaPayStatus('Could not create a Solana Pay request. Check your account access and try again.', 'error');
            } finally {
                button.disabled = !!(activeSolanaPayInvoice?.state === 'OPEN' &&
                    Number(activeSolanaPayInvoice.expires_at) > Date.now() / 1000);
            }
        }
        async function showSolanaPayInvoice(invoice) {
            const panel = document.getElementById('solpay-checkout');
            panel.hidden = false;
            document.getElementById('solpay-checkout-amount').textContent = invoice.amount + ' ' + invoice.token;
            document.getElementById('solpay-recipient').textContent = invoice.deposit_wallet;
            document.getElementById('solpay-expiration').textContent =
                new Date(Number(invoice.expires_at) * 1000).toLocaleString('en-GB');
            const openWallet = document.getElementById('solpay-open-wallet');
            const statusPage = document.getElementById('solpay-status-page');
            const expired = Number(invoice.expires_at) <= Date.now() / 1000;
            if (invoice.state === 'CREDITED') {
                openWallet.hidden = true;
                statusPage.hidden = true;
                setSolanaPayStatus('Payment confirmed and credited to your account.', 'success');
                lockSolanaPayInputs(false);
                const storageKey = solanaPayStorageKey();
                if (storageKey) { try { sessionStorage.removeItem(storageKey); } catch(e) {} }
                return;
            }
            if (expired || !invoice.solana_pay_uri) {
                openWallet.hidden = true;
                statusPage.hidden = true;
                setSolanaPayStatus('This request has expired. Do not pay to it; create a new request.', 'error');
                lockSolanaPayInputs(false);
                const storageKey = solanaPayStorageKey();
                if (storageKey) { try { sessionStorage.removeItem(storageKey); } catch(e) {} }
                return;
            }
            openWallet.href = invoice.solana_pay_uri;
            openWallet.hidden = false;
            statusPage.href = invoice.payment_url;
            statusPage.hidden = !invoice.payment_url;
            lockSolanaPayInputs(true);
            setSolanaPayStatus('Waiting for payment confirmation. Do not send this payment again.');
        }
        async function refreshSolanaPayInvoice() {
            if (!activeSolanaPayInvoice?.invoice_id) return;
            const previousState = activeSolanaPayInvoice.state;
            try {
                const response = await apiFetch('/deposit/' + encodeURIComponent(activeSolanaPayInvoice.invoice_id) + '/status',
                    {cache: 'no-store'});
                const invoice = await response.json();
                if (!response.ok || !invoice.success) throw new Error(invoice.message || 'Status check unavailable.');
                activeSolanaPayInvoice = invoice;
                if (invoice.token === 'USDC' || invoice.token === 'SOL') {
                    if (invoice.token !== selectedDepositAsset) selectDepositToken(invoice.token);
                    document.getElementById('solpay-amount').value = invoice.amount;
                }
                await showSolanaPayInvoice(invoice);
                if (invoice.state === 'CREDITED' && previousState !== 'CREDITED') await refresh();
            } catch(e) {
                setSolanaPayStatus('Payment status check is temporarily unavailable. Do not pay again.', 'error');
            }
        }
        function restoreSolanaPayInvoice() {
            const storageKey = solanaPayStorageKey();
            if (!storageKey) return;
            let invoiceId = '';
            try { invoiceId = sessionStorage.getItem(storageKey) || ''; } catch(e) {}
            if (!invoiceId) return;
            activeSolanaPayInvoice = {invoice_id: invoiceId, state: 'OPEN'};
            document.getElementById('solpay-checkout').hidden = false;
            lockSolanaPayInputs(true);
            refreshSolanaPayInvoice();
            if (solanaPayPollTimer) clearInterval(solanaPayPollTimer);
            solanaPayPollTimer = setInterval(refreshSolanaPayInvoice, 5000);
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
        async function checkBalanceCoverage() {
            const box=document.getElementById('balance-coverage-details');
            box.textContent='Reading finalized wallet balances and the ledger…';
            try {
                const response=await apiFetch('/api/admin/balance_check');
                const result=await response.json();
                if(!response.ok||!result.success)throw new Error(result.message||'Balance check unavailable.');
                box.textContent=['USDC: wallet '+result.assets.USDC.wallet+
                    ' · account balances '+result.assets.USDC.ledger+
                    ' · difference '+result.assets.USDC.difference,
                    'SOL: wallet '+result.assets.SOL.wallet+
                    ' · account balances '+result.assets.SOL.ledger+
                    ' · difference '+result.assets.SOL.difference,
                    'Read-only: no ledger or on-chain balance was changed.'].join('\n');
            } catch(e) { box.textContent=e.message||'Could not read wallet coverage.'; }
        }
        async function retireLegacyOperation(signature) {
            if (!confirm('For a legacy personal operation only. The server checks transaction absence, blockhash expiry and nonce history. Without evidence nothing changes. The record stays unconfirmed; this is not a refund or a successful trade. Continue?')) return;
            const pin = prompt('Enter your confirmation PIN:');
            if (!pin) return;
            await adminLedgerAction('/api/admin/retire_legacy', {signature, pin, confirm_archive_unknown:true});
            await checkLegacyOperations();
        }
        let operatorFeeState = null;
        function renderFeeStatus(fees) {
            if(!fees)return;
            operatorFeeState=fees;
            document.getElementById('fee-status').textContent=
                'Reserve: '+fees.reserve_sol+' SOL\nRequired minimum: '+fees.minimum_sol+
                ' SOL\nExcess returnable to your balance: '+fees.releasable_sol+
                ' SOL\nYour available balance: '+fees.own_available_sol+' SOL\n'+
                (fees.sufficient?'Reserve sufficient for readiness checks.':'Reserve insufficient.');
            const releaseButton=document.getElementById('fee-release-submit');
            if(releaseButton){
                releaseButton.textContent='Return '+fees.releasable_sol+' SOL to available balance';
                releaseButton.disabled=Number(fees.releasable_sol)<=0;
            }
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
        async function releaseExcessFeeReserve() {
            const resultBox=document.getElementById('fee-release-result');
            const button=document.getElementById('fee-release-submit');
            const expected=operatorFeeState?.releasable_sol;
            if(!expected||Number(expected)<=0){
                resultBox.textContent='There is no amount above the required fee reserve to return.';return;
            }
            if(!document.getElementById('fee-release-confirm').checked){
                resultBox.textContent='Confirm that these are your own SOL, not customer funds.';return;
            }
            if(button.disabled)return;
            button.disabled=true;resultBox.textContent='Checking the balance and returning '+expected+' SOL to your available bot balance…';
            try{
                const response=await apiFetch('/api/admin/release_fee_reserve',{method:'POST',headers:{'Content-Type':'application/json'},
                    body:JSON.stringify({expected_release_sol:expected,confirm_excess_only:true,confirm_own_funds:true})});
                const result=await response.json();
                if(!response.ok||!result.success)throw new Error(result.message||'Reserve release not confirmed.');
                renderFeeStatus(result.operator_fees);
                document.getElementById('fee-release-confirm').checked=false;
                resultBox.textContent=result.message+'\nReserve after return: '+result.operator_fees.reserve_sol+' SOL.';
                await refresh();
            }catch(e){
                resultBox.textContent=(e.message||'No response from server.')+
                    '\nRefresh the fee status before trying again; do not retry blindly.';
            }finally{button.disabled=Number(operatorFeeState?.releasable_sol)<=0;}
        }
        async function importOwnOpeningBalance() {
            const token = prompt('Asset to import from your legacy funds: USDC or SOL', 'USDC');
            if (!token) return;
            const amount = prompt('Exact amount of your own legacy funds (not a virtual balance):');
            if (!amount || !confirm('Confirm this amount contains no other users’ funds. Import is allowed only before public opening, once per asset.')) return;
            await adminLedgerAction('/api/admin/opening_balance', {token, amount, confirm_own_unassigned:true});
        }
        let legacyBalancePreview = null;
        async function previewLegacyBalances() {
            const button=document.getElementById('legacy-balance-preview-button');
            const importButton=document.getElementById('legacy-balance-import-button');
            const box=document.getElementById('legacy-balance-migration-status');
            button.disabled=true;
            importButton.hidden=true;
            importButton.disabled=true;
            legacyBalancePreview=null;
            box.textContent='Checking legacy records and on-chain backing. No balances are being changed…';
            try {
                const response=await apiFetch('/api/admin/legacy_balances/preview');
                const result=await response.json();
                if(!response.ok||!result.success)throw new Error(result.message||'Preview failed.');
                legacyBalancePreview=result.can_import?result:null;
                const skipped=Object.entries(result.skipped||{})
                    .map(([reason,count])=>reason+': '+count).join(' · ')||'none';
                box.textContent=[
                    'Candidate old accounts: '+result.candidate_count,
                    'Eligible accounts: '+result.eligible_count,
                    'Eligible amount: '+result.eligible_usdc+' USDC',
                    'Candidate old balance: '+result.candidate_usdc+' USDC',
                    'Unassigned on-chain backing: '+result.backing_available_usdc+' USDC',
                    'Skipped: '+skipped,
                    result.backed?'Backing check passed.':'Not enough unassigned USDC backing; nothing can be imported.',
                    'Preview only. No balance or transaction was changed.'
                ].join('\n');
                importButton.hidden=!result.can_import;
                importButton.disabled=!result.can_import;
            } catch(e) {
                box.textContent=e.message||'Could not preview old balances. No balances were changed.';
            } finally {
                button.disabled=false;
            }
        }
        async function importLegacyBalances() {
            const button=document.getElementById('legacy-balance-import-button');
            const box=document.getElementById('legacy-balance-migration-status');
            const preview=legacyBalancePreview;
            if(!preview?.can_import||button.disabled)return;
            if(!confirm('Import '+preview.eligible_usdc+' USDC for '+preview.eligible_count+
                ' accounts from the exact preview? This changes the internal ledger only; no Solana transaction will be sent.'))return;
            button.disabled=true;
            box.textContent='Rechecking the preview and on-chain backing before the internal ledger update…';
            try {
                const response=await apiFetch('/api/admin/legacy_balances/import',{method:'POST',
                    headers:{'Content-Type':'application/json'},
                    body:JSON.stringify({confirm:true,preview_digest:preview.digest})});
                const result=await response.json();
                if(!response.ok||!result.success)throw new Error(result.message||'Import was not confirmed.');
                legacyBalancePreview=null;
                button.hidden=true;
                box.textContent=result.message+'\nImported accounts: '+result.imported_count+
                    '\nTotal: '+result.amount_usdc+' USDC.';
                await refresh();
            } catch(e) {
                box.textContent=(e.message||'Import could not be verified.')+
                    '\nNo Solana transaction was sent. Run a fresh preview before trying again.';
            } finally {
                button.disabled=!legacyBalancePreview?.can_import;
            }
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

        let telegramTariffState=null,telegramTariffClockOffset=0,telegramTariffBusy=false;
        let selectedTelegramPackage=null;
        const telegramPackageDetails={
            zrl_7d:{title:'7-day AI Trader access',description:'100,000 ZRL · verified as a package payment; never added to trading balance.'},
            sol_30d:{title:'30-day AI Trader access',description:'0.5 SOL · verified as a package payment; never added to trading balance.'}
        };
        let telegramTariffDismissed=false,nextTelegramTariffCheck=0;
        async function refreshTelegramTariffs(){
            if(window.ZRL_WEB||telegramTariffBusy||!getUserData())return;
            telegramTariffBusy=true;
            try{
                const response=await apiFetch('/api/subscription'),data=await response.json();
                if(!response.ok||!data.success)throw Error(data.message||'Could not check access.');
                telegramTariffState=data;
                telegramTariffClockOffset=Number(data.server_time||0)*1000-Date.now();
                const status=document.getElementById('telegram-tariff-status');
                const note=document.getElementById('telegram-tariff-note');
                const checkout=document.getElementById('telegram-tariff-checkout');
                if(data.admin_free){
                    status.textContent='Administrator access is permanent; package payment is not required.';
                    note.textContent='The public free-access countdown below is for other accounts. A subscription never changes trading admission or starts Core or Sniper.';
                }else if(data.free_access){
                    status.textContent='Free AI Trader subscription is active for everyone through 1 November 2026.';
                    note.textContent='No payment is needed before the countdown ends. Payment is not credited to trading balance and never turns on Core or Sniper.';
                }else if(data.active){
                    status.textContent='Paid access is active until '+new Date(Number(data.expires_at)*1000).toLocaleString()+'.';
                    note.textContent='Access renewal does not change trading admission or start trading modes.';
                }else{
                    status.textContent='Free access has ended. Choose a package to request continued access.';
                    note.textContent=data.purchase_available?
                        'Checkout uses a separate browser passkey flow. The exact payment is verified before access is added; it is never credited to trading balance.':
                        'This account is not admitted for paid trading. A subscription does not change custody or trading admission.';
                }
                checkout.hidden=!data.purchase_available||data.admin_free||data.active||data.free_access;
                const modalCheckout=document.getElementById('telegram-tariff-modal-checkout');
                modalCheckout.hidden=!data.purchase_available||data.admin_free||data.free_access;
                if(!data.payment_required)telegramTariffDismissed=false;
                const modal=document.getElementById('telegram-tariff-modal');
                if(data.payment_required&&!telegramTariffDismissed){
                    document.getElementById('telegram-tariff-modal-message').textContent=
                        data.purchase_available?
                        'Choose 7 days for 100,000 ZRL or 30 days for 0.5 SOL. Payment is verified in the secure browser checkout and does not enter your trading balance.':
                        'This account is not admitted for paid trading. Buying a subscription would not change custody or enable trading.';
                    modal.classList.add('active');modal.setAttribute('aria-hidden','false');
                }else{
                    modal.classList.remove('active');modal.setAttribute('aria-hidden','true');
                }
                if(selectedTelegramPackage)renderTelegramPackageDetails();
                tickTelegramTariffCountdown();
            }catch(error){
                document.getElementById('telegram-tariff-status').textContent=error.message||'Could not check access.';
            }finally{telegramTariffBusy=false;}
        }
        function tickTelegramTariffCountdown(){
            if(window.ZRL_WEB||!telegramTariffState)return;
            const label=document.getElementById('telegram-tariff-countdown');
            const until=Number(telegramTariffState.free_access_until||0);
            if(!until){label.textContent='';return;}
            const seconds=until-
                Math.floor((Date.now()+telegramTariffClockOffset)/1000);
            if(seconds<=0){
                label.textContent=telegramTariffState.admin_free?
                    'Public free access has ended · your administrator access remains permanent.':
                    telegramTariffState.active?
                    'Public free access has ended · your paid access remains active.':
                    'Free access has ended. Checking your access…';
                if(telegramTariffState.free_access&&Date.now()>=nextTelegramTariffCheck){
                    nextTelegramTariffCheck=Date.now()+10000;refreshTelegramTariffs();
                }
                return;
            }
            const days=Math.floor(seconds/86400),hours=Math.floor(seconds%86400/3600);
            const minutes=Math.floor(seconds%3600/60),remaining=seconds%60;
            const accessNote=telegramTariffState.admin_free?' · your administrator access remains permanent':'';
            label.textContent=`Public free access ends in ${days}d ${hours}h ${minutes}m ${remaining}s · 1 Nov 2026, 00:00 Europe/Warsaw${accessNote}`;
        }
        function openTariffCheckout(packageId=null){
            if(!telegramTariffState?.purchase_available||telegramTariffState.admin_free||telegramTariffState.free_access)return;
            const url=new URL('/terminal',window.location.origin);
            if(['zrl_7d','sol_30d'].includes(packageId))url.searchParams.set('package',packageId);
            if(window.Telegram?.WebApp?.openLink)window.Telegram.WebApp.openLink(url.href);
            else window.open(url.href,'_blank','noopener,noreferrer');
        }
        function openTelegramPackage(packageId){
            if(!telegramPackageDetails[packageId])return;
            selectedTelegramPackage=packageId;
            renderTelegramPackageDetails();
            const modal=document.getElementById('telegram-package-modal');
            modal.classList.add('active');modal.setAttribute('aria-hidden','false');
            if(!telegramTariffState)refreshTelegramTariffs();
        }
        function renderTelegramPackageDetails(){
            const plan=telegramPackageDetails[selectedTelegramPackage];
            if(!plan)return;
            document.getElementById('telegram-package-modal-title').textContent=plan.title;
            document.getElementById('telegram-package-modal-description').textContent=plan.description;
            const status=document.getElementById('telegram-package-modal-status');
            const checkout=document.getElementById('telegram-package-modal-checkout');
            const data=telegramTariffState;
            if(!data){
                status.textContent='Checking account access…';
            }else if(data.admin_free){
                status.textContent='Your administrator account has permanent free access. No package payment is needed.';
            }else if(data.free_access){
                status.textContent='Your free access is still active. Checkout opens after the public free-access period ends.';
            }else if(!data.purchase_available){
                status.textContent='This account is not admitted for paid access. Payment would not change custody or trading admission.';
            }else if(data.active){
                status.textContent='Your access is active until '+new Date(Number(data.expires_at)*1000).toLocaleString()+'. You can renew securely.';
            }else{
                status.textContent='Continue to the secure browser checkout to create an exact payment invoice.';
            }
            checkout.hidden=!data?.purchase_available||data.admin_free||data.free_access;
            checkout.onclick=()=>openTariffCheckout(selectedTelegramPackage);
        }
        function closeTelegramPackage(){
            selectedTelegramPackage=null;
            const modal=document.getElementById('telegram-package-modal');
            modal.classList.remove('active');modal.setAttribute('aria-hidden','true');
        }
        document.querySelectorAll('[data-telegram-package]').forEach(button=>
            button.addEventListener('click',()=>openTelegramPackage(button.dataset.telegramPackage)));
        document.getElementById('telegram-package-modal').addEventListener('click',event=>{
            if(event.target.id==='telegram-package-modal')closeTelegramPackage();
        });
        function dismissTelegramTariffModal(){
            telegramTariffDismissed=true;
            const modal=document.getElementById('telegram-tariff-modal');
            modal.classList.remove('active');modal.setAttribute('aria-hidden','true');
        }
        setInterval(tickTelegramTariffCountdown,1000);
        setInterval(()=>{if(telegramTariffState?.free_access)refreshTelegramTariffs();},15000);

        let accountState = null;
        async function loadAccount() {
            const response=await apiFetch('/api/account'),data=await response.json();
            if(!response.ok||!data.success)throw Error(data.message||'Could not verify account.');
            accountState=data;
            document.getElementById('passkey-state').textContent='Telegram code required for withdrawals. '+
                (data.has_passkey?'Browser passkey active · '+data.passkey_count+' key(s) · '+data.recovery_codes_remaining+' recovery codes remaining.':
                    'No browser passkey set up. Telegram withdrawals remain available.');
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
                    document.getElementById('affiliate-terms').hidden=!profile.partner_terms_required;
                    document.getElementById('account-partner-terms').checked=false;
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
                        accept_risk:document.getElementById('account-risk').checked,
                        accept_partner_terms:document.getElementById('account-partner-terms').checked})});
                const data=await response.json();if(!response.ok||!data.success)throw Error(data.message||'Could not create account.');
                accountState=data;document.getElementById('account-signup').style.display='none';
                await finishAccount();
            }catch(e){document.getElementById('account-error').textContent=e.message;document.getElementById('account-signup').style.display='block';}
            finally{button.disabled=false;}
        }
        async function finishAccount(){
            try{
                proceedToTerminal();
                if(window.ZRL_WEB&&typeof window.focusRequestedPackage==='function')window.focusRequestedPackage();
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
            for(let i=0;i<150;i++){
                await new Promise(resolve=>setTimeout(resolve,2000));
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
            const security=await apiFetch('/api/security_status').then(r=>r.json());
            if(!security.has_pin && !(await setupPasswordProtection()))return;
            const pin=prompt('Enter your account security code:');if(!pin)return;
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
            const pwd = prompt("Create an account security code for optional passkey management (at least 6 characters):");
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
                alert("Account security code saved.");
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
            if(!window.ZRL_WEB)refreshTelegramTariffs();
        }

        function gotoTab(id, btn) {
            document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
            document.querySelectorAll('.nav-btn').forEach(b => b.classList.remove('active'));
            const target = document.getElementById('tab-' + id);
            if (target) target.classList.add('active');
            if (btn) btn.classList.add('active');
            if(id==='tariffs'&&!window.ZRL_WEB){
                document.getElementById('telegram-tariff-view').hidden=false;
                refreshTelegramTariffs();
            }
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
            clearWithdrawalCode();
            updateWithdrawalFeePreview();
        }

        function withdrawalRawFromAmount(amount, token) {
            const decimals=token==='SOL'?9:6;
            const text=String(amount||'').trim();
            if(!/^\d+(?:\.\d*)?$/.test(text))return null;
            const [whole,fraction='']=text.split('.');
            if(fraction.length>decimals)return null;
            const scale=10n**BigInt(decimals);
            return BigInt(whole)*scale+BigInt(fraction.padEnd(decimals,'0')||'0');
        }
        function formatWithdrawalRaw(raw, token) {
            const decimals=token==='SOL'?9:6;
            const amount=BigInt(raw);
            const scale=10n**BigInt(decimals);
            const whole=(amount/scale).toString();
            const fraction=(amount%scale).toString().padStart(decimals,'0').replace(/0+$/,'');
            return fraction?whole+'.'+fraction:whole;
        }
        function updateWithdrawalFeePreview() {
            const preview=document.getElementById('withdrawal-fee-preview');
            if(!preview)return;
            const token=currentWithdrawToken||'USDC';
            const gross=withdrawalRawFromAmount(document.getElementById('withdraw-amount')?.value,token);
            if(gross===null){preview.textContent='Enter a valid amount to see the withdrawal breakdown.';return;}
            const fee=(window.latestAccountStats?.is_admin===true)?0n:(gross*150n/10000n);
            const net=gross-fee;
            if(window.latestAccountStats?.is_admin===true) {
                preview.textContent='Administrator withdrawals have no 1.5% fee.';
                return;
            }
            if(fee===0n) {
                preview.textContent='At this amount, the 1.5% fee rounds down to zero in the currency smallest unit; recipient gets the full amount.';
                return;
            }
            preview.textContent='Requested: '+formatWithdrawalRaw(gross,token)+' '+token+
                ' · fee: '+formatWithdrawalRaw(fee,token)+' '+token+
                ' · recipient gets: '+formatWithdrawalRaw(net,token)+' '+token+
                '. Fee is credited to the administrator balance.';
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

        async function closePosition(position, button) {
            if (!position || position.status !== 'OPEN' || !position.position_id) {
                alert('This position is no longer open. Refresh and check its status.');
                return;
            }
            const quantity=Number(position.amount_tokens).toLocaleString(undefined,{maximumFractionDigits:8});
            const symbol=position.symbol||position.token_symbol||'token';
            const funding=position.funding_currency||'account currency';
            if (!confirm('Sell only this position: '+quantity+' '+symbol+' for '+funding+
                '? This sends a real Solana transaction; the final amount can vary with price and slippage.')) return;
            button.disabled=true;
            button.textContent='Submitting sell…';
            try {
                const response=await apiFetch('/api/close_position',{
                    method:'POST',headers:{'Content-Type':'application/json'},
                    body:JSON.stringify({position_id:position.position_id,confirm:true})
                });
                const data=await response.json();
                if(!response.ok||!data.success)throw new Error(data.message||'Could not submit the sale.');
                alert(data.message||'Sale submitted; waiting for Solana confirmation. Do not submit again.');
                await refresh();
            } catch(e) {
                alert(e.message||'Could not submit the sale.');
                await refresh();
            }
        }

        let withdrawalCodeBinding = '';
        let withdrawalQuote = null;
        let activeWithdrawalSignature = '';
        function newWithdrawalRequestId() {
            const cryptoApi = window.crypto;
            if (cryptoApi && typeof cryptoApi.randomUUID === 'function') {
                return cryptoApi.randomUUID();
            }
            if (cryptoApi && typeof cryptoApi.getRandomValues === 'function') {
                const bytes = cryptoApi.getRandomValues(new Uint8Array(16));
                bytes[6] = (bytes[6] & 0x0f) | 0x40;
                bytes[8] = (bytes[8] & 0x3f) | 0x80;
                const hex = Array.from(bytes, byte => ('0' + byte.toString(16)).slice(-2)).join('');
                return hex.slice(0,8) + '-' + hex.slice(8,12) + '-' +
                    hex.slice(12,16) + '-' + hex.slice(16,20) + '-' + hex.slice(20);
            }
            throw new Error('Secure request IDs are unavailable. Reopen AI Trader in Telegram.');
        }
        function withdrawalContext() {
            const token = currentWithdrawToken;
            const addr = document.getElementById('withdraw-address').value.trim();
            const amount = document.getElementById('withdraw-amount').value.trim();
            const intent = JSON.stringify({token, recipient_address:addr, amount});
            let storage, old;
            try {
                storage = window.localStorage;
                old = JSON.parse(storage.getItem('withdraw_intent') || 'null');
            } catch (e) {
                throw new Error('Browser storage is unavailable. Reopen the Mini App in Telegram; no code or transfer was sent.');
            }
            const oldIdIsUuid = old && typeof old.id === 'string' &&
                /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(old.id);
            const requestId = old && old.intent === intent && oldIdIsUuid
                ? old.id : newWithdrawalRequestId();
            try {
                storage.setItem('withdraw_intent', JSON.stringify({intent, id:requestId}));
            } catch (e) {
                throw new Error('Browser storage is unavailable. Reopen the Mini App in Telegram; no code or transfer was sent.');
            }
            return {token, addr, amount, intent, requestId};
        }
        function clearWithdrawalCode() {
            const input=document.getElementById('withdraw-code');
            if(input)input.value='';
            withdrawalCodeBinding='';
            withdrawalQuote=null;
            const status=document.getElementById('withdraw-code-status');
            if(status)status.textContent='';
            clearTerminalWithdrawalResult();
            updateWithdrawalFeePreview();
        }
        function clearTerminalWithdrawalResult() {
            const progress=document.getElementById('withdraw-progress');
            if(!progress||!activeWithdrawalSignature)return;
            const history=(window.latestAccountStats?.wallet_history||[])
                .find(row=>row.type==='WITHDRAW'&&row.tx===activeWithdrawalSignature);
            const pending=(window.latestAccountStats?.pending_operations||[])
                .find(row=>row.signature===activeWithdrawalSignature);
            const state=history?.status||pending?.state;
            if(!['COMPLETED','FAILED'].includes(state))return;
            progress.hidden=true;
            progress.textContent='';
            activeWithdrawalSignature='';
        }
        async function requestWithdrawalCode() {
            const status=document.getElementById('withdraw-code-status');
            const button=document.getElementById('btn-withdraw-code');
            clearTerminalWithdrawalResult();
            if(withdrawSending){
                status.textContent='A withdrawal is already processing. Wait for its status; do not submit it again.';
                return;
            }
            button.disabled=true;button.textContent='Checking balance…';
            status.textContent='Checking the wallet and requesting a Telegram code…';
            try {
                const context=withdrawalContext();
                if(!context.addr||!context.amount)throw Error('Enter the recipient address and amount first.');
                const response=await apiFetch('/api/withdraw/code',{method:'POST',
                    headers:{'Content-Type':'application/json'},
                    body:JSON.stringify({token:context.token,recipient_address:context.addr,
                        amount:context.amount,request_id:context.requestId})});
                const data=await response.json();
                if(!response.ok||!data.success)throw Error(data.message||'Could not send a Telegram code.');
                withdrawalCodeBinding=context.intent;
                const grossRaw=data.requested_raw
                    ?BigInt(data.requested_raw):withdrawalRawFromAmount(context.amount,context.token);
                if(grossRaw===null)throw Error('Enter a valid amount before requesting the code.');
                const feeRaw=data.withdrawal_fee_raw!==undefined
                    ?BigInt(data.withdrawal_fee_raw)
                    :(window.latestAccountStats?.is_admin===true?0n:grossRaw*150n/10000n);
                const netRaw=data.net_raw!==undefined?BigInt(data.net_raw):grossRaw-feeRaw;
                withdrawalQuote={intent:context.intent,token:context.token,grossRaw,feeRaw,netRaw};
                status.textContent='Code sent. Requested '+formatWithdrawalRaw(grossRaw,context.token)+
                    ' '+context.token+'; fee '+formatWithdrawalRaw(feeRaw,context.token)+
                    ' '+context.token+'; recipient gets '+formatWithdrawalRaw(netRaw,context.token)+
                    ' '+context.token+'. The fee goes to the administrator balance.';
                document.getElementById('withdraw-code').focus();
            } catch(e) {
                const message=e.message||'Could not request a Telegram code.';
                status.textContent=message.toLowerCase().includes('wallet funds do not cover recorded account balances')
                    ? 'Withdrawal paused: wallet funds do not cover recorded balances. No code or transfer was sent.'
                    : message;
            } finally {
                button.disabled=false;button.textContent='Send code in Telegram';
            }
        }

        async function secureWithdraw() {
            const status=document.getElementById('withdraw-code-status');
            if (withdrawSending) {
                status.textContent='A withdrawal is already processing. Wait for its status; do not submit it again.';
                return;
            }
            clearTerminalWithdrawalResult();
            let context;
            try {
                context=withdrawalContext();
            } catch(e) {
                status.textContent=e.message||'Could not prepare the withdrawal request.';
                return;
            }
            const code=document.getElementById('withdraw-code').value.trim();
            if(!context.addr||!context.amount){status.textContent='Enter the recipient address and amount.';return;}
            if(!/^\d{6}$/.test(code)){
                if(!status.textContent.startsWith('Withdrawal paused:'))status.textContent='Request a Telegram code first.';
                return;
            }
            if(withdrawalCodeBinding!==context.intent){
                if(!status.textContent.startsWith('Withdrawal paused:'))status.textContent='Request a new code for these details.';
                return;
            }
            if(!withdrawalQuote||withdrawalQuote.intent!==context.intent) {
                status.textContent='Request a new Telegram code to confirm the exact withdrawal fee and amount.';
                return;
            }
            if(!confirm('Withdraw '+formatWithdrawalRaw(withdrawalQuote.grossRaw,context.token)+' '+
                    context.token+'? Fee: '+formatWithdrawalRaw(withdrawalQuote.feeRaw,context.token)+
                    ' '+context.token+'. Recipient receives '+
                    formatWithdrawalRaw(withdrawalQuote.netRaw,context.token)+
                    ' '+context.token+'. The fee is credited to the administrator balance.'))return;
            await submitWithdraw(code,context);
        }

        async function submitWithdraw(telegramCode, context) {
            if (withdrawSending) {
                const progress=document.getElementById('withdraw-progress');
                progress.hidden=false;
                progress.textContent='A withdrawal is already processing. Do not submit it again.';
                return;
            }
            const {token,addr,amount,requestId}=context;
            withdrawSending = true;
            const btn = document.getElementById('btn-withdraw-action');
            const codeButton=document.getElementById('btn-withdraw-code');
            const progress = document.getElementById('withdraw-progress');
            progress.hidden = false;
            progress.textContent = 'Checking your withdrawal limit and sending the transfer to Solana…';
            btn.disabled=true;codeButton.disabled=true;

            try {
                const res = await apiFetch('/api/withdraw', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({token,recipient_address:addr,amount,request_id:requestId,
                        telegram_code:telegramCode})
                });
                const d = await res.json();
                if (typeof d.tx_signature === 'string' && d.tx_signature.length > 20) {
                    activeWithdrawalSignature=d.tx_signature;
                }
                if (d.success) {
                    progress.textContent=d.state==='COMPLETED'
                        ? 'Sent '+(d.net_amount||context.amount)+' '+(d.asset||context.token)+
                            (d.commission_amount&&d.commission_raw!=='0'
                                ?'; fee '+d.commission_amount+' '+(d.asset||context.token)+
                                    ' credited to administrator balance.'
                                :' and confirmed on Solana.')
                        : (d.message||'Withdrawal submitted. Status is checked automatically; do not submit it again.');
                    document.getElementById('withdraw-code').value='';
                    withdrawalCodeBinding='';
                    window.latestAccountStats=null;
                    await refresh();
                    if(d.tx_signature)syncWithdrawalStatusFromStats(d.tx_signature);
                }
                if(!d.success)progress.textContent=d.tx_signature
                    ? 'Withdrawal status is being checked automatically. Do not submit it again.'
                    : (d.message||'Withdrawal failed; no new transfer was sent.');
                if (d.state === 'COMPLETED' || d.state === 'FAILED') {
                    localStorage.removeItem('withdraw_intent');
                    document.getElementById('withdraw-code').value='';
                    withdrawalCodeBinding='';
                }
            } catch (e) {
                progress.hidden = false;
                progress.textContent = e.message || "Could not check the withdrawal status. Do not repeat it until history is refreshed.";
            }
            finally {
                withdrawSending = false;
                btn.innerText = "Withdraw 🚀";
                btn.disabled = false;codeButton.disabled=false;
            }
        }

        function syncWithdrawalStatusFromStats(signature) {
            const history=window.latestAccountStats?.wallet_history||[];
            const pending=window.latestAccountStats?.pending_operations||[];
            const operation=signature
                ? history.find(row=>row.type==='WITHDRAW'&&row.tx===signature) ||
                    pending.find(row=>row.kind==='WITHDRAW'&&row.signature===signature)
                : pending.slice().reverse().find(row=>row.kind==='WITHDRAW'&&
                    ['PREPARED','SUBMITTED','UNKNOWN'].includes(row.state));
            if(!operation)return false;
            const state=operation.status||operation.state;
            activeWithdrawalSignature=operation.tx||operation.signature||activeWithdrawalSignature;
            const progress=document.getElementById('withdraw-progress');
            if(!progress)return false;
            progress.hidden=false;
            progress.textContent=state==='COMPLETED'
                ? operation.amount+' sent · confirmed on Solana · Telegram receipt '+
                    (operation.telegram_notified?'sent.':'pending.')
                : state==='FAILED'
                    ? 'Withdrawal failed on Solana. The amount was returned to your balance.'
                    : state==='UNKNOWN'
                        ? 'Withdrawal status is unclear. Do not submit it again; the server is checking automatically.'
                        : 'Withdrawal is awaiting network confirmation. Do not submit it again.';
            return true;
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

        function renderRealizedPnl(stats) {
            const usdc = Number(stats.profit_usdc ?? 0);
            const sol = Number(stats.profit_sol ?? 0);
            const usdcNode = document.getElementById('pnl-val');
            const solNode = document.getElementById('pnl-sol-val');
            if (usdcNode) {
                const validUsdc = Number.isFinite(usdc) ? usdc : 0;
                usdcNode.textContent = (validUsdc >= 0 ? '+$' : '-$') + Math.abs(validUsdc).toFixed(2);
                usdcNode.style.color = validUsdc < 0 ? '#f87171' : '#4ade80';
            }
            if (solNode) {
                const validSol = Number.isFinite(sol) ? sol : 0;
                solNode.textContent = (validSol >= 0 ? '+' : '-') + Math.abs(validSol).toFixed(9) + ' SOL';
                solNode.style.color = validSol < 0 ? '#f87171' : '#4ade80';
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
                window.latestAccountStats=s;
                if(s.success)syncWithdrawalStatusFromStats(activeWithdrawalSignature||'');
                
                if (s.success) {
                    const withdrawalBalance=document.getElementById('chain-withdraw-balances');
                    if(withdrawalBalance)withdrawalBalance.textContent =
                        'Balance: ' + Number(s.user_balance||0).toFixed(6) + ' USDC | ' +
                        Number(s.sol_balance||0).toFixed(9) + ' SOL. Max withdrawal: ' +
                        Number(s.withdrawable||0).toFixed(6) + ' USDC | ' +
                        Number(s.withdrawable_sol||0).toFixed(9) +
                        ' SOL. Network fees paid by operator reserve.';
                    await loadPartnerProgram();
                    const tradingAllowed=!!(s.is_admin||s.trading_admitted);
                    const navButtons=document.querySelectorAll('.nav-btn');
                    if(navButtons[1])navButtons[1].style.display=tradingAllowed?'':'none';
                    document.querySelectorAll('[data-target-tab="trading"]').forEach(b=>b.style.display=tradingAllowed?'':'none');
                    const tradingPanel=document.getElementById('tab-trading');
                    if(tradingPanel){
                        tradingPanel.style.display=tradingAllowed?'':'none';
                        if(!tradingAllowed&&tradingPanel.classList.contains('active')){
                            gotoTab('terminal',navButtons[0]);
                        }
                    }
                    const flags = s.wallet_status || {};
                    document.getElementById('wallet-runtime-status').textContent =
                        !flags.wallet_configured ? 'Server key does not match the wallet address.' :
                        !flags.persistent_storage ? 'Operations blocked: Persistent Disk /var/data required.' :
                        !flags.finance_allowed ? 'Public deposits are not open yet.' :
                        s.operation_pending ? 'An operation is awaiting Solana confirmation. The server checks it automatically; do not repeat it.' :
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
                                const instruction = op.kind === 'SELL' ? 'Do not sell this position again.' :
                                    op.kind === 'WITHDRAW' ? 'Do not submit this withdrawal again.' :
                                    op.kind === 'BUY' ? 'Do not repeat this purchase.' : 'Do not repeat this operation.';
                                return 'Operation: ' + (op.kind || 'unknown') + ' / ' + (op.state || 'unknown')
                                    + '\nAmount: ' + (op.reserved_amount || 'unknown') + ' ' + (op.reserved_asset || '')
                                    + '\nCreated: ' + created
                                    + '\nSignature: ' + (op.signature || 'not recorded')
                                    + '\n' + instruction + ' The server checks the result automatically.';
                            }).join('\n\n');
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
                    const cost=s.entry_cost_check;
                    document.getElementById('entry-cost-check').textContent=cost
                        ? 'Last entry check ('+(cost.symbol||'Core')+'): '+cost.status+' · '
                          +(Number.isFinite(cost.estimated_cost_pct)
                            ? cost.estimated_cost_pct.toFixed(2)+'% / 2.50% budget · ' : '')
                          +new Date(cost.checked_at*1000).toLocaleString()+' · '+cost.reason
                        : 'No entry cost check yet. Checked before each USDC-funded BUY; missing evidence blocks entry.';
                    const policy=s.execution_policy;
                    const sniperPositionCap=document.getElementById('sniper-position-cap');
                    const sniperMax=Number(policy?.max_positions?.sniper);
                    if(sniperPositionCap)sniperPositionCap.textContent=
                        Number.isSafeInteger(sniperMax)&&sniperMax>0
                            ? 'Screened Solana candidates · up to '+sniperMax+' positions'
                            : 'Screened Solana candidates · position limit unavailable';
                    document.getElementById('execution-policy-summary').textContent=policy
                        ? 'Decision ≤ '+policy.decision_max_age_seconds+'s · Quote ≤ '+policy.quote_max_age_seconds
                          +'s / '+policy.quote_max_slot_distance+' slots · Slippage ≤ '+(policy.slippage_bps/100)
                          +'% · Buy impact ≤ '+policy.buy_max_price_impact_pct+'% · Core round-trip costs ≤ '
                          +policy.core_max_cost_pct+'% · Positions: Core '+policy.max_positions.main
                          +' / Sniper '+policy.max_positions.sniper+'. Order sizes come from your saved settings.'
                        : 'Independent policy unavailable. Do not enable real trading.';
                    const gate=s.execution_check;
                    document.getElementById('execution-policy-result').textContent=gate
                        ? new Date(gate.checked_at*1000).toLocaleString()+' · '+gate.status+' · '
                          +gate.stage+' / '+gate.action+(gate.symbol?' '+gate.symbol:'')+'\\n'+gate.reason
                        : 'No execution check recorded yet. AI suggestions are not executed trades.';
                    const ai=s.ai_status||{};
                    aiConfigured=!!ai.configured;
                    updateMemeToggleUI(s.sniper_active);
                    const whaleAI=ai.whale_analysis||{};
                    const whaleAIState=whaleAI.state&&whaleAI.state!=='idle'
                        ? '\nWhale-triggered analysis: '+whaleAI.state
                          +((whaleAI.symbols||[]).length?' · '+whaleAI.symbols.map(code=>(ai.asset_labels||{})[code]||code).join(', '):'')
                          +(whaleAI.message?' · '+whaleAI.message:'')
                        : '';
                    document.getElementById('ai-status').textContent=(aiConfigured?
                        ai.model+' · '+Math.round(ai.interval_seconds/60)+' min interval'+(ai.last_analyzed_at?' · analyzed':' · connection unverified')
                          +(ai.last_trigger==='whale_signal'?' · whale-triggered':''):
                        'AI not connected. Operator setup required.')+whaleAIState;
                    if(ai.last_decisions)document.getElementById('ai-decisions').textContent='Last analysis: '+new Date(ai.last_analyzed_at*1000).toLocaleString('en-GB')+'\n'+Object.entries(ai.last_decisions).map(([mode,d])=>(mode==='main'?'USDC':'SOL')+': '+d.action+' '+((ai.asset_labels||{})[d.symbol]||d.symbol||'')+' — '+d.reason).join('\n');
                    renderCoreChartZones(ai.chart_context,ai.asset_labels);
                    const discovery=ai.discovery||{};
                    document.getElementById('discovery-status').textContent=discovery.entry_paused?discovery.message:
                        discovery.checked_at?(discovery.eligible||0)+' screened · '+(discovery.scanned||0)+' scanned · refreshed '+new Date(discovery.checked_at*1000).toLocaleTimeString('en-GB'):discovery.message||'Candidate scan pending.';
                    if(discovery.whale_prioritized)document.getElementById('discovery-status').textContent+=' · whale mints screened: '+discovery.whale_prioritized+' ('+(discovery.whale_candidates||0)+' eligible)';
                    document.getElementById('candidate-list').textContent=(ai.sniper_assets||[]).map(code=>{
                        const row=(discovery.candidates||[]).find(item=>item.code===code);
                        return ((ai.asset_labels||{})[code]||code)+(row?' · liquidity $'+Math.round(row.liquidity_usd).toLocaleString('en-US'):' · held asset');
                    }).join('\n')||'No verified shortlist yet.';
                    const whale=ai.whale_stream||{};
                    const whaleRows=(whale.recent_signals||[]).slice(0,8).map(event=>{
                        const mint=String(event.mint||'');
                        const shortMint=mint.length>14?mint.slice(0,6)+'…'+mint.slice(-5):mint;
                        const sol=Number(event.sol||0);
                        const seen=Number(event.seen_at||0);
                        return (seen?new Date(seen*1000).toLocaleTimeString('en-GB'):'Time unavailable')
                            +' · '+(Number.isFinite(sol)?sol.toFixed(2):'—')+' SOL · '+(event.program||'DEX')
                            +' · '+shortMint;
                    });
                    const whaleQueue='RPC checks '+(whale.transaction_lookups||0)
                        +' · queued '+(whale.queued_lookups||0)
                        +(whale.dropped_lookups?' · dropped '+whale.dropped_lookups:'');
                    const whaleRejections=(discovery.whale_rejections||[]).slice(0,4).map(item=>
                        item.mint+' — '+(item.reasons||[]).join('; '));
                    document.getElementById('whale-signal-status').textContent=(whale.message||'Whale stream status unavailable.')
                        +'\nSignal DEXs: Pump.fun, PumpSwap, Raydium, Meteora and Orca; confirmed BUY/SWAP logs are checked.'
                        +'\nSignals are not orders; transactions and candidates must pass the existing safety screen.'
                        +'\n'+whaleQueue
                        +(whaleRejections.length?'\nWhy recent whale candidates were rejected:\n'+whaleRejections.join('\n'):'')
                        +(whaleRows.length?'\n'+whaleRows.join('\n'):'\nNo qualifying signals received yet.');
                    const sniper=s.sniper_settings||{order_sol:.005};
                    document.getElementById('sniper-order-size').textContent=sniper.order_sol+' SOL';
                    const sniperInput=document.getElementById('settings-sniper-order');
                    if(!settingsOrderDirty&&document.activeElement!==sniperInput)sniperInput.value=sniper.order_sol;
                    document.getElementById('sniper-pnl').textContent='Realized PnL: '+Number(s.profit_sol||0).toFixed(9)+' SOL';
                    document.getElementById('strategy-order-size').textContent=currentTradingSettings.order_usdc.toFixed(2)+' USDC';
                    const sniperOnly=new Set(['WIF','BONK','BOME','POPCAT','MEW','PENGU',...(ai.sniper_assets||[])]);
                    document.getElementById('strategy-assets').textContent=(currentTradingSettings.assets||[]).filter(symbol=>!sniperOnly.has(symbol)).join(' · ');
                    const settingsInput=document.getElementById('settings-order');
                    if(!settingsOrderDirty&&document.activeElement!==settingsInput)settingsInput.value=currentTradingSettings.order_usdc;
                    document.getElementById('settings-budget').textContent='Saved: '+currentTradingSettings.order_usdc+' USDC · up to '+(currentTradingSettings.max_positions||5)+' positions, limited by available funds.';
                    document.getElementById('worker-status').textContent=s.worker_error?'Last worker error: '+s.worker_error:'Autopilot: '+(s.trading_active?'ON':'OFF')+'. Readiness check is in Trading.';
                    const userEl = document.getElementById('display-username');
                    if (userEl) userEl.innerText = "@" + s.username;
                    
                    renderRealizedPnl(s);
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
                                ? `<a href="https://solscan.io/tx/${encodeURIComponent(h.tx)}" target="_blank" rel="noopener noreferrer" style="color: #60a5fa; text-decoration: none; font-size: 11px;">Tx ↗</a>`
                                : `<span style="color: #64748b; font-size: 10px;">${h.status}</span>`;
                            
                            return `
                                <div style="display: flex; justify-content: space-between; align-items: center; padding: 10px 0; border-bottom: 1px solid #1c2333; text-align: left;">
                                    <div>
                                        <div style="color: ${color}; font-weight: 800; font-size: 13px;">${sign} ${h.amount}</div>
                                        <div style="font-size: 10px; color: #64748b; margin-top: 2px;">${h.time}</div>
                                    </div>
                                    <div style="text-align: right;">
                                        <div style="font-size: 10px; color: ${h.status === 'FAILED' ? '#f87171' : h.status === 'VERIFIED' || h.status === 'COMPLETED' ? '#4ade80' : '#facc15'}; margin-bottom: 2px;">${h.status}</div>
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
                            const isOpen = t.status === 'OPEN';
                            const isPending = !isClosed && !isOpen;
                            const pnlColor = Number(t.profit_units??t.profit_usdc) >= 0 ? '#4ade80' : '#f87171';

                            return `
                                <div class="order-card">
                                    <div class="order-top">
                                        <div style="font-weight: 800; color: #fff; font-size: 13px;">${t.token_symbol} / ${t.funding_currency||'USDC'}</div>
                                        <div class="order-badge ${isClosed ? 'badge-closed' : 'badge-open'}">
                                            ${isClosed ? 'CLOSED' : isPending ? 'SELL PENDING' : 'OPEN'}
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
                                            ${isClosed ? (Number(t.profit_units??t.profit_usdc).toFixed(t.funding_currency==='SOL'?9:2)+' '+(t.funding_currency||'USDC')+' ('+t.profit_pct.toFixed(2)+'%)') : isPending ? 'Sale awaiting confirmation' : 'Open position'}
                                        </div>
                                    </div>
                                    <div class="order-tx-row">
                                        ${t.buy_tx ? '<a class="tx-link" href="https://solscan.io/tx/' + t.buy_tx + '" target="_blank">↗ Buy transaction</a>' : ''}
                                        ${t.sell_tx ? '<a class="tx-link" style="color: #4ade80;" href="https://solscan.io/tx/' + t.sell_tx + '" target="_blank">↗ Sell transaction</a>' : ''}
                                    </div>
                                    ${isOpen && t.position_id ? '<button type="button" class="position-close" data-position-id="' + t.position_id + '">Sell this position</button>' : ''}
                                </div>
                            `;
                        }).join('');
                        document.getElementById('history-box').querySelectorAll('.position-close').forEach(button => {
                            const position=s.trades.find(trade=>trade.position_id===button.dataset.positionId);
                            button.addEventListener('click',()=>closePosition(position,button));
                        });
                    }
                }
            } catch(e) {}
        }
        selectDepositToken('USDC');
        restoreSolanaPayInvoice();
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

async def splash_asset_handler(request):
    asset = Path(__file__).with_name("static") / "zerolife-ai-trader-core.webp"
    if not asset.is_file():
        raise web.HTTPNotFound()
    return web.FileResponse(asset, headers={
        "Content-Type": "image/webp",
        "Cache-Control": "public, max-age=3600",
        "X-Content-Type-Options": "nosniff"
    })

async def health_handler(request):
    runtime = request.app["wallet_runtime"]
    flags = runtime.readiness()
    return web.json_response({"status": "custody_beta", **flags,
        "build_commit": os.getenv("RENDER_GIT_COMMIT", ""),
        "deposit_parser_version": "phantom-authority-v1",
        "ai_configured": bool(os.getenv("OPENAI_API_KEY")),
        "web_terminal_version": "access-packages-v2",
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
                already_registered = c.execute(
                    "SELECT 1 FROM users WHERE telegram_id=?", (chat_id,)).fetchone()
                payload = text.split(maxsplit=1)[1] if len(text.split(maxsplit=1)) > 1 else ""
                sender = message.get("from", {})
                if (not already_registered and message.get("chat", {}).get("type") == "private"
                        and int(sender.get("id", 0)) == int(chat_id) and payload.startswith("ref_")):
                    try:
                        request.app["wallet_runtime"].ledger.bind_referral_code(chat_id, payload[4:])
                    except Exception:
                        pass
                c.execute("""INSERT INTO users (telegram_id, username) 
                             VALUES (?, ?) 
                             ON CONFLICT(telegram_id) DO UPDATE SET username=excluded.username""", (chat_id, username))
                conn.commit()
                conn.close()

                keyboard = {"inline_keyboard": [[{"text": "🚀 Launch Terminal", "web_app": {"url": RENDER_URL}}]]}
                send_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
                payload = {
                    "chat_id": chat_id,
                    "text": "👋 Your **AI Trader** terminal is ready. If you joined through a partner link, partners can receive 1% / 0.5% / 0.25% of realized trading profit (sale proceeds minus recorded purchase cost), only after previous trading losses are recovered. Separately paid network fees are not netted here. The total is capped at 1.75%; deposits and open trades do not count.",
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
    app = web.Application(middlewares=[
        security_headers_middleware, english_json_middleware, telegram_auth_middleware])
    runtime = CustodyRuntime(DB_FILE, SOLANA_RPC, ADMIN_TELEGRAM_ID, SHARED_DEPOSIT_WALLET,
        get_signer_keypair, verify_user_pin, get_token_prices, WATCHLIST, add_log,
        base_url=RENDER_URL, notification_token=TELEGRAM_TOKEN,
        market_context_fn=lambda session: fetch_main_market_context(
            session, WATCHLIST, runtime.main_symbols))
    runtime.recent_logs = lambda: RECENT_LOGS
    runtime.accounts = AccountSecurity(runtime.ledger, RENDER_URL, verify_user_pin, runtime.wallet,
        notification_token=TELEGRAM_TOKEN)
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
    payment_html = Path(__file__).with_name("solana_pay_checkout.html").read_text(encoding="utf-8")
    async def payment_page(request):
        if not runtime.ledger.get_invoice(request.match_info["invoice"]):
            raise web.HTTPNotFound()
        return web.Response(text=payment_html, content_type="text/html", headers={
            "Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": "frame-ancestors 'none'; object-src 'none'; base-uri 'none'",
            "X-Content-Type-Options": "nosniff"})
    app.router.add_get('/', index_handler)
    app.router.add_get('/terminal', index_handler)
    app.router.add_get('/static/zerolife-ai-trader-core.webp', splash_asset_handler)
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
    app.router.add_get('/api/partners', wallet_handler(runtime.partner_program))
    app.router.add_post('/api/partners/retry', wallet_handler(runtime.retry_partner_payout))
    app.router.add_get('/api/stats', wallet_handler(runtime.stats))
    app.router.add_post('/api/toggle_trading', wallet_handler(runtime.toggle))
    app.router.add_post('/api/trading_settings', wallet_handler(runtime.save_trading_settings))
    app.router.add_post('/api/trading_readiness', wallet_handler(runtime.trading_readiness))
    app.router.add_post('/api/ai_preview', wallet_handler(runtime.ai_preview))
    app.router.add_post('/api/recover_expired_operation', wallet_handler(runtime.recover_expired_operation))
    app.router.add_post('/api/toggle_sniper', wallet_handler(runtime.toggle_sniper))
    app.router.add_post('/api/meme_scan', wallet_handler(runtime.scan_candidates))
    app.router.add_post('/api/toggle_meme', api_toggle_meme)
    app.router.add_post('/api/verify_deposit', wallet_handler(runtime.deposit))
    app.router.add_post('/api/wallet_link', wallet_handler(runtime.wallet_link))
    app.router.add_post('/api/deposit_invoice', wallet_handler(runtime.create_invoice))
    app.router.add_get('/api/admin/status', wallet_handler(runtime.admin_status))
    app.router.add_get('/api/admin/pending_operations', wallet_handler(runtime.admin_pending_operations))
    app.router.add_get('/api/admin/balance_check', wallet_handler(runtime.admin_balance_check))
    app.router.add_post('/api/admin/record_external_withdrawal', wallet_handler(runtime.admin_record_external_withdrawal))
    app.router.add_post('/api/admin/retire_legacy', wallet_handler(runtime.retire_legacy))
    app.router.add_post('/api/admin/fee_reserve', wallet_handler(runtime.reserve_fees))
    app.router.add_post('/api/admin/release_fee_reserve', wallet_handler(runtime.release_fee_reserve))
    app.router.add_post('/api/admin/opening_balance', wallet_handler(runtime.opening_balance))
    app.router.add_get('/api/admin/legacy_balances/preview', wallet_handler(runtime.preview_legacy_balances))
    app.router.add_post('/api/admin/legacy_balances/import', wallet_handler(runtime.import_legacy_balances))
    app.router.add_post('/api/close_all', wallet_handler(runtime.close_all))
    app.router.add_post('/api/close_position', wallet_handler(runtime.close_position))
    app.router.add_post('/api/withdraw', wallet_handler(runtime.withdraw))
    app.router.add_post('/api/withdraw/code', wallet_handler(runtime.request_withdrawal_code))
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
    asyncio.create_task(runtime.recovery_notification_worker())
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
