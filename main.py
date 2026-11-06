import asyncio
import math
import aiohttp
import certifi
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
from market_context import fetch_main_market_context, fetch_sniper_market_context
import dexscreener_client as dexscreener
from english_text import english_json_middleware
from account_security import AccountSecurity
from web_access import WebAccess
from wallet_runtime import WalletError
from spl_token_support import TOKEN_2022_PROGRAM_ID
from support_inbox import SupportInbox, MAX_SUPPORT_REQUEST_BYTES

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
ADMIN_API_PATHS = {"/api/admin/status", "/api/admin/pending_operations", "/api/admin/fee_reserve", "/api/admin/release_fee_reserve", "/api/admin/opening_balance", "/api/admin/retire_legacy", "/api/admin/owner_shortfall"}
WITHDRAW_LOCKS = {}
CUSTODY_SAFE_MODE = True
CUSTODY_PAUSE_MESSAGE = "Операции с реальными средствами временно приостановлены до сверки балансов и миграции реестра."

DB_DIR = "/var/data"
DB_FILE = os.path.join(DB_DIR, "zer0life_users.db") if os.path.exists(DB_DIR) else "zer0life_users.db"

SOLANA_RPC = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com").strip()
SHARED_HOT_WALLET = os.getenv("DEPOSIT_WALLET", "8hxiCofyaKCBkhR5nsDqvUivmfgxcVx8zo2WiCzSdM6L").strip()
CUSTODY_RESERVE_WALLET = os.getenv("CUSTODY_RESERVE_WALLET", "").strip()
SHARED_DEPOSIT_WALLET = CUSTODY_RESERVE_WALLET or SHARED_HOT_WALLET
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
    "PENGU": {"mint": "2zMMhcVQEXDtdE6vsFS7S7D5oUodfJHE8vd1gnBouauv", "decimals": 6},
    # CoinGecko Pepe on SOL; verified on finalized Solana RPC.
    "PEPE": {"mint": "B5WTLaRwaUQpKk7ir1wniNB6m5o8GgMrimhKMYan2R6B", "decimals": 6},
    "OTC": {"mint": "MukLDtJ8Cx9DxLbeyLRSWPSposTMWuwHANbuaudpump",
            "decimals": 6, "token_program": TOKEN_2022_PROGRAM_ID, "label": "OTC"},
}

TRADE_AMOUNT_USDC = 20.0
TRADE_AMOUNT_RAW = int(TRADE_AMOUNT_USDC * 1_000_000)
TP_PCT = 0.035
SL_PCT = 0.050
MAX_SLIPPAGE_BPS = 40

MEME_ALLOCATION_SOL = 0.1
MEME_TARGET_MULTIPLIER = 10.0
MEME_HALF_MULTIPLIER = 5.0
MEME_STOP_LOSS_PCT = 0.05

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
    # Guard the whole admin namespace here so a newly added /api/admin/* route
    # cannot accidentally ship without the central owner check. Individual
    # runtime methods still enforce ownership as a second line of defense.
    if request.path in ADMIN_API_PATHS or request.path.startswith("/api/admin/"):
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
    get_token_prices.last_error = ""
    try:
        headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
        async with dexscreener.request(session, url, priority=dexscreener.PRICE,
                headers=headers, timeout=aiohttp.ClientTimeout(total=8)) as resp:
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
            else:
                get_token_prices.last_error = (
                    f"Market-data provider returned HTTP {resp.status}; "
                    "fresh prices unavailable. No trade was sent.")
    except dexscreener.ProviderLimited:
        get_token_prices.last_error = (
            f"DexScreener rate limit (HTTP 429); shared market-data requests paused for "
            f"{max(1, dexscreener.cooldown_remaining())} seconds. No trade was sent.")
    except Exception:
        get_token_prices.last_error = (
            "Market-data provider unavailable; fresh prices could not be verified. No trade was sent.")
    if not prices and not get_token_prices.last_error:
        get_token_prices.last_error = (
            "No fresh verified market prices are available. Autopilot was not started; no trade was sent.")
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
                    async with dexscreener.request(session, url, priority=dexscreener.PRICE,
                            headers=headers, timeout=5) as resp:
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
                        
                        async with dexscreener.request(session, search_url, priority=dexscreener.DISCOVERY,
                                headers=headers, timeout=6) as resp:
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
        #account-signup .signup-consent {
            display: grid; grid-template-columns: 24px minmax(0, 1fr); align-items: start;
            gap: 12px; margin: 14px 0 0; padding: 14px; border: 1px solid #40535e;
            border-radius: 14px; background: rgba(8, 16, 23, .55); cursor: pointer;
        }
        #account-signup .signup-consent input[type="checkbox"] {
            appearance: auto; -webkit-appearance: checkbox; width: 22px !important;
            min-width: 22px; height: 22px; margin: 1px 0 0; accent-color: #36d9a2;
            cursor: pointer;
        }
        #account-signup .signup-terms-heading {
            margin: 16px 0 0; color: #79e2bb; font-size: 11px; font-weight: 800;
            letter-spacing: .08em; text-transform: uppercase;
        }
        #account-signup #account-error {
            min-height: 1.5em; margin: 10px 0 0; color: #ffaaa9;
        }
        .support-quick-link {
            display: block; margin: 10px auto 0; padding: 10px 14px; border: 1px solid #36524e;
            border-radius: 12px; background: rgba(13, 31, 31, .55); color: #8de6bd;
            font-size: 13px; font-weight: 700; cursor: pointer;
        }
        #support-overlay[hidden] { display: none !important; }
        #support-overlay:not([hidden]) { display: grid; }
        .support-overlay {
            position: fixed; inset: 0; z-index: 11000; place-items: center;
            padding: 16px; background: rgba(3, 7, 10, .82); backdrop-filter: blur(10px);
        }
        .support-dialog {
            width: min(460px, 100%); max-height: min(88dvh, 760px); overflow-y: auto;
            padding: 20px; border: 1px solid #30434d; border-radius: 20px;
            background: #111c26; color: #e6eef0; box-shadow: 0 24px 70px rgba(0,0,0,.55);
        }
        .support-dialog-head { display:flex; align-items:center; justify-content:space-between; gap:12px; }
        .support-dialog-head h2 { margin:0; font-size:21px; }
        .support-close {
            flex:0 0 40px; width:40px; height:40px; border:1px solid #354954; border-radius:12px;
            background:#172530; color:#e6eef0; font-size:22px; cursor:pointer;
        }
        .support-dialog textarea {
            display:block; width:100%; min-height:130px; resize:vertical; padding:12px;
            border:1px solid #354954; border-radius:12px; background:#09131b; color:#eef5f4;
            font:14px/1.5 -apple-system,BlinkMacSystemFont,sans-serif;
        }
        .support-dialog input[type="file"] { display:block; width:100%; margin-top:8px; color:#c6d4d7; }
        .support-dialog .support-status { min-height:1.5em; margin:10px 0 0; color:#a9bac0; }
        .support-dialog .support-status[data-state="error"] { color:#ffaaa9; }
        .support-dialog .support-status[data-state="success"] { color:#7de0b2; }
        #trade-share-dialog {
            width:min(520px,calc(100vw - 24px)); max-width:calc(100vw - 24px);
            max-height:calc(100dvh - 24px); overflow-y:auto; padding:0;
            border:1px solid #36503a; border-radius:22px; background:#08100b; color:#edf5ed;
            box-shadow:0 24px 80px rgba(0,0,0,.72);
        }
        #trade-share-dialog::backdrop { background:rgba(0,0,0,.78); backdrop-filter:blur(8px); }
        .share-profitable-trade { display:block; width:100%; min-height:42px; margin-top:8px; }
        .trade-share-content { display:grid; gap:12px; padding:clamp(14px,4vw,22px); }
        .trade-share-head { display:flex; justify-content:space-between; align-items:flex-start; gap:12px; }
        .trade-share-head h2 { margin:2px 0 0; font-size:20px; }
        .trade-share-kicker { margin:0; color:#c5f576; font-size:10px; font-weight:800; letter-spacing:.16em; }
        .trade-share-close {
            flex:0 0 40px; width:40px; height:40px; border:1px solid #35483a; border-radius:12px;
            background:#121c14; color:#edf5ed; font-size:22px; cursor:pointer;
        }
        #trade-share-canvas {
            display:block; width:min(100%,330px); height:auto; margin:0 auto; aspect-ratio:4/5;
            border:1px solid #324531; border-radius:16px; background:#08100b;
            box-shadow:0 12px 34px rgba(0,0,0,.38);
        }
        .trade-share-note {
            margin:0; padding:11px 12px; border:1px solid #2d4131; border-radius:12px;
            background:rgba(14,25,16,.88); color:#d3dfd3; font-size:13px; line-height:1.5;
        }
        .trade-share-targets { margin:0; color:#9eb0a0; font-size:12px; line-height:1.5; }
        .trade-share-confirm {
            display:flex; align-items:flex-start; gap:10px; padding:10px 0;
            color:#e7eee6; font-size:13px; line-height:1.45;
        }
        .trade-share-confirm input { flex:0 0 auto; width:18px; height:18px; margin:1px 0 0; accent-color:#b9ed56; }
        .trade-share-caption-label { margin:0 0 -7px; color:#b7c6b9; font-size:12px; font-weight:700; }
        #trade-share-caption {
            display:block; width:100%; min-height:98px; resize:vertical; padding:11px;
            border:1px solid #344437; border-radius:12px; background:#050a06; color:#e8f1e7;
            font:13px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
        }
        .trade-share-actions { display:grid; grid-template-columns:1.3fr 1fr 1fr; gap:8px; }
        .trade-share-actions button {
            min-height:44px; padding:9px 10px; border:1px solid #36483a; border-radius:12px;
            background:#111a13; color:#e8f1e7; font-size:12px; font-weight:750; cursor:pointer;
        }
        .trade-share-actions #trade-share-submit {
            border-color:#b2e747; background:linear-gradient(120deg,#d2ff68,#92d92c);
            color:#10170a; font-size:13px;
        }
        .trade-share-actions button:disabled { opacity:.42; cursor:not-allowed; }
        #trade-share-status { min-height:1.4em; margin:0; color:#aebdaf; font-size:12px; line-height:1.45; }
        @media (max-width:420px) {
            .trade-share-actions { grid-template-columns:1fr 1fr; }
            .trade-share-actions #trade-share-submit { grid-column:1 / -1; }
        }
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
        #app .base-paper-card {
            position: relative;
            overflow: hidden;
            padding: 16px !important;
            background:
                radial-gradient(150px 110px at 9% 0%, rgba(0, 82, 255, .28), transparent 92%),
                linear-gradient(145deg, #101b30 0%, #0b1320 48%, #080d14 100%) !important;
            border: 1px solid rgba(92, 148, 255, .42) !important;
            border-radius: 21px;
            box-shadow:
                inset 0 1px 0 rgba(255,255,255,.11),
                inset 0 -14px 24px rgba(0,0,0,.25),
                0 7px 0 rgba(0, 36, 122, .55),
                0 18px 38px rgba(0,0,0,.4);
        }
        #app .base-paper-card::before {
            content: "";
            position: absolute;
            inset: 0 12% auto;
            height: 1px;
            background: linear-gradient(90deg, transparent, rgba(181,211,255,.68), transparent);
            pointer-events: none;
        }
        #app .base-paper-head, #app .base-network-identity {
            display: flex;
            align-items: center;
        }
        #app .base-paper-head { justify-content: space-between; gap: 10px; }
        #app .base-network-identity { min-width: 0; gap: 12px; }
        #app .base-network-mark {
            position: relative;
            display: grid;
            place-items: center;
            flex: 0 0 44px;
            width: 44px;
            height: 44px;
            border-radius: 13px;
            color: white;
            background: linear-gradient(145deg, #7db2ff 0%, #1769ff 42%, #0043d1 76%, #002f9c 100%);
            box-shadow:
                inset 0 2px 0 rgba(255,255,255,.58),
                inset 0 -4px 8px rgba(0,25,95,.5),
                0 5px 0 #00389f,
                0 10px 17px rgba(0,82,255,.36);
            transform: perspective(420px) rotateX(8deg) rotateY(-10deg);
        }
        #app .base-network-mark::after {
            content: "";
            position: absolute;
            inset: 5px;
            border: 1px solid rgba(255,255,255,.34);
            border-radius: 9px;
            box-shadow: inset 0 0 12px rgba(255,255,255,.12);
        }
        #app .base-network-mark span {
            position: relative;
            z-index: 1;
            font: 850 23px/1 system-ui, sans-serif;
            text-shadow: 0 2px 3px rgba(0,24,91,.55);
        }
        #app .base-network-kicker {
            margin: 0 0 3px;
            color: #83b3ff;
            font: 750 9px/1.3 ui-monospace, SFMono-Regular, Menlo, monospace;
            letter-spacing: .12em;
        }
        #app .base-paper-head h2 { margin: 0; font-size: 18px; line-height: 1.2; }
        #app .base-paper-badge {
            display: inline-flex;
            align-items: center;
            gap: 6px;
            flex: 0 0 auto;
            padding: 7px 9px;
            border: 1px solid rgba(111,166,255,.33);
            border-radius: 999px;
            color: #c4dcff;
            background: linear-gradient(145deg, rgba(31,69,128,.48), rgba(12,27,50,.84));
            box-shadow: inset 0 1px 0 rgba(255,255,255,.1), 0 3px 9px rgba(0,0,0,.2);
            font: 750 9px/1 ui-monospace, SFMono-Regular, Menlo, monospace;
            letter-spacing: .04em;
            white-space: nowrap;
        }
        #app .base-paper-dot {
            width: 6px;
            height: 6px;
            border-radius: 50%;
            background: #6bb3ff;
            box-shadow: 0 0 9px rgba(64,148,255,.9);
        }
        #app .base-paper-caption { margin: 9px 0 11px; color: #9baecb; font-size: 11px; line-height: 1.35; }
        #app .base-paper-metrics { display: grid; grid-template-columns: repeat(3, minmax(0,1fr)); gap: 7px; }
        #app .base-paper-metric {
            min-width: 0;
            padding: 9px 9px 8px;
            border: 1px solid rgba(144,176,224,.17);
            border-radius: 13px;
            background: linear-gradient(150deg, rgba(19,31,51,.95), rgba(8,15,27,.98));
            box-shadow: inset 0 1px 0 rgba(255,255,255,.055), 0 4px 10px rgba(0,0,0,.17);
        }
        #app .base-paper-metric > span {
            display: block;
            overflow: hidden;
            color: #93a8c7;
            font: 700 8px/1.2 ui-monospace, SFMono-Regular, Menlo, monospace;
            letter-spacing: .08em;
            text-overflow: ellipsis;
            white-space: nowrap;
        }
        #app .base-paper-metric strong {
            display: block;
            overflow: hidden;
            margin-top: 4px;
            color: #f1f6ff;
            font: 760 16px/1.15 ui-monospace, SFMono-Regular, Menlo, monospace;
            text-overflow: ellipsis;
            white-space: nowrap;
        }
        #app .base-paper-metric small { display: block; overflow: hidden; margin-top: 3px; color: #95a4bb; font-size: 9px; text-overflow: ellipsis; white-space: nowrap; }
        #app .base-paper-controls {
            display: grid;
            grid-template-columns: minmax(95px,1fr) minmax(75px,.7fr) minmax(116px,1.15fr);
            align-items: end;
            gap: 8px;
            margin-top: 10px;
        }
        #app .base-paper-budget-field { display: block; min-width: 0; color: #a9b9d0; font-size: 9px; font-weight: 700; }
        #app .base-paper-budget-field > span { display: block; margin: 0 0 4px 2px; }
        #app .base-paper-budget-field .sig-input {
            box-sizing: border-box;
            width: 100%;
            min-height: 44px;
            height: 44px;
            margin: 0 !important;
            padding: 8px 10px;
            font-size: 13px;
        }
        #app .base-paper-save, #app .base-paper-scan {
            width: 100%;
            min-width: 0;
            min-height: 44px;
            height: 44px;
            margin: 0 !important;
            padding: 0 9px;
            font-size: 11px;
            white-space: nowrap;
        }
        #app .base-paper-save {
            color: #cfe1ff !important;
            border-color: rgba(105,153,230,.28) !important;
            background: linear-gradient(145deg, #1c2d49, #111c2d) !important;
        }
        #app .base-paper-scan {
            color: #f5f9ff !important;
            border: 1px solid rgba(151,192,255,.72) !important;
            background: linear-gradient(145deg, #5f9aff 0%, #1769ff 54%, #0044c9 100%) !important;
            box-shadow: inset 0 1px 0 rgba(255,255,255,.48), 0 4px 0 #003aab, 0 8px 16px rgba(0,82,255,.28) !important;
        }
        #app .base-paper-scan:active:not(:disabled) { transform: translateY(2px); box-shadow: inset 0 1px 0 rgba(255,255,255,.3), 0 2px 0 #003aab !important; }
        #app .base-paper-status { min-height: 15px; margin: 9px 2px 0; color: #a9bbd4; font-size: 10px; line-height: 1.4; }
        #app .base-live-readiness {
            margin: 10px 0 14px;
            padding: 13px;
            border: 1px solid rgba(176, 75, 75, .34);
            border-radius: 15px;
            background: linear-gradient(145deg, rgba(36,22,28,.94), rgba(13,15,21,.98));
        }
        #app .base-live-readiness-head { display: flex; justify-content: space-between; align-items: center; gap: 10px; }
        #app .base-live-readiness-head h2 { margin: 0; color: #f0f4fb; font-size: 15px; }
        #app .base-live-off-badge {
            flex: 0 0 auto;
            padding: 6px 9px;
            border: 1px solid rgba(244, 126, 126, .36);
            border-radius: 999px;
            color: #ffb0b0;
            background: rgba(104, 30, 39, .32);
            font: 800 8px/1.2 ui-monospace, SFMono-Regular, Menlo, monospace;
            letter-spacing: .06em;
        }
        #app .base-live-readiness-note { margin: 8px 0; color: #c4cedd; font-size: 10px; line-height: 1.45; }
        #app .base-live-checks { margin: 7px 0 0; padding-left: 18px; color: #aab8cb; font-size: 10px; line-height: 1.65; overflow-wrap: anywhere; }
        #app .base-live-address { display: block; margin-top: 7px; color: #dbe6f7; font: 10px/1.45 ui-monospace, SFMono-Regular, Menlo, monospace; overflow-wrap: anywhere; }
        #app .base-live-check-button { min-height: 40px; padding: 8px 12px; border-radius: 11px; }
        #app .base-live-summary { margin-top: 7px; color: #f0b0b0; font-size: 10px; line-height: 1.5; }
        #app .base-live-controls { display: grid; grid-template-columns: minmax(0,1fr) auto; gap: 8px; margin-top: 10px; align-items: end; }
        #app .base-live-budget { min-width: 0; color: #a9b9d0; font-size: 9px; font-weight: 700; }
        #app .base-live-budget input { width: 100%; min-height: 40px; margin-top: 4px; padding: 9px 11px; color: #f1f5f9; border: 1px solid rgba(135,159,190,.3); border-radius: 11px; background: rgba(3,8,13,.72); font: 700 14px ui-monospace,monospace; }
        #app .base-live-save, #app .base-live-toggle { min-height: 40px; padding: 8px 12px; border-radius: 11px; white-space: nowrap; }
        #app .base-live-toggle { grid-column: 1 / -1; width: 100%; color: #ffc5c5 !important; border-color: rgba(244,114,114,.4) !important; background: rgba(83,25,32,.62) !important; }
        #app .base-live-toggle.is-on { color: #faffdf !important; border-color: rgba(170,238,57,.65) !important; background: linear-gradient(145deg,#a7df32,#58a815) !important; }
        #app .base-live-toggle:disabled, #app .base-live-save:disabled { opacity: .5; }
        #app .base-live-fold { margin-top: 9px; padding: 10px; border: 1px solid rgba(135,159,190,.16); border-radius: 11px; background: rgba(2,7,11,.36); }
        #app .base-live-fold summary { color: #c6d7f1; font-size: 10px; font-weight: 700; }
        #app .base-live-items { max-height: 180px; overflow: auto; margin-top: 8px; color: #aab8cb; font: 10px/1.5 ui-monospace,monospace; white-space: pre-line; overflow-wrap: anywhere; }
        #app .base-paper-details { display: grid; grid-template-columns: repeat(2,minmax(0,1fr)); gap: 7px; margin-top: 9px; }
        #app .base-paper-fold {
            min-width: 0;
            padding: 9px 10px;
            border: 1px solid rgba(126,157,204,.17);
            border-radius: 12px;
            background: linear-gradient(145deg, rgba(15,25,41,.8), rgba(8,14,24,.9));
        }
        #app .base-paper-fold summary { display: flex; justify-content: space-between; align-items: center; gap: 8px; color: #c6d7f1; font-size: 10px; }
        #app .base-paper-fold[open] { border-color: rgba(89,146,255,.35); }
        #app .solana-investor-compact-description {
            margin: 8px 2px 0;
            color: #aebdd1;
            font-size: 10px;
            line-height: 1.5;
        }
        #app details.solana-investor-description-disclosure { margin-top: 8px; }
        #app details.solana-investor-description-disclosure > summary {
            min-height: 34px;
            cursor: pointer;
        }
        #app details.solana-investor-description-disclosure > .solana-investor-help {
            margin: 9px 0 0;
        }
        #app .base-paper-fold-count { min-width: 20px; padding: 3px 6px; border-radius: 999px; color: #b7d4ff; background: rgba(49,106,196,.24); text-align: center; font: 700 9px/1.1 ui-monospace, monospace; }
        #app .base-paper-list { max-height: 190px; overflow: auto; margin-top: 9px; color: #b4c3d7; font: 10px/1.55 ui-monospace, SFMono-Regular, Menlo, monospace; white-space: pre-line; overflow-wrap: anywhere; }
        #app .base-paper-disclaimer, #app .base-paper-rules { display: block; margin: 9px 0 0; color: #8fa2bd; font-size: 10px; line-height: 1.5; }
        #app .base-paper-fold-wide { grid-column: 1 / -1; }
        #app .solana-investor-card {
            position: relative;
            overflow: hidden;
            padding: 16px !important;
            background:
                radial-gradient(180px 130px at 8% 0%, rgba(153, 69, 255, .24), transparent 92%),
                radial-gradient(140px 100px at 100% 100%, rgba(20, 241, 149, .10), transparent 90%),
                linear-gradient(145deg, #171329 0%, #0d1117 52%, #080d0d 100%) !important;
            border: 1px solid rgba(153, 110, 230, .42) !important;
            border-radius: 21px;
            box-shadow: inset 0 1px 0 rgba(255,255,255,.10),
                inset 0 -14px 24px rgba(0,0,0,.25), 0 7px 0 rgba(74, 35, 130, .52),
                0 18px 38px rgba(0,0,0,.4);
        }
        #app .solana-investor-card::before {
            content: "";
            position: absolute;
            inset: 0 12% auto;
            height: 1px;
            background: linear-gradient(90deg, transparent, rgba(220,190,255,.7), transparent);
            pointer-events: none;
        }
        #app .solana-investor-card .base-paper-head { justify-content: space-between; gap: 10px; }
        #app .solana-investor-card .base-network-identity { min-width: 0; gap: 12px; }
        #app .solana-network-mark {
            position: relative;
            display: grid;
            place-items: center;
            flex: 0 0 46px;
            width: 46px;
            height: 46px;
            border: 1px solid rgba(226,207,255,.62);
            border-radius: 14px;
            background: linear-gradient(145deg, #492a76, #201333 68%, #140e20);
            box-shadow: inset 0 2px 0 rgba(255,255,255,.3),
                inset 0 -5px 9px rgba(0,0,0,.42), 0 5px 0 #24113c,
                0 10px 18px rgba(153,69,255,.32);
            transform: perspective(420px) rotateX(8deg) rotateY(-10deg);
        }
        #app .solana-network-mark::after {
            content: "";
            position: absolute;
            inset: 4px;
            border: 1px solid rgba(255,255,255,.2);
            border-radius: 10px;
            pointer-events: none;
        }
        #app .solana-network-mark span {
            position: relative;
            z-index: 1;
            display: grid;
            gap: 3px;
            transform: skewY(-13deg);
        }
        #app .solana-network-mark i {
            display: block;
            width: 24px;
            height: 4px;
            border-radius: 4px;
            background: linear-gradient(90deg, #20f59a, #b88bff 76%);
            box-shadow: 0 1px 0 rgba(255,255,255,.36), 0 2px 5px rgba(0,0,0,.38);
        }
        #app .solana-network-mark i:nth-child(2) { background: linear-gradient(90deg, #ab71ff, #18e9aa); }
        #app .solana-network-kicker {
            margin: 0 0 3px;
            color: #bd9aff;
            font: 750 9px/1.3 ui-monospace, SFMono-Regular, Menlo, monospace;
            letter-spacing: .12em;
        }
        #app .solana-investor-card .base-paper-head h2 { margin: 0; font-size: 18px; line-height: 1.2; }
        #app .solana-investor-badge {
            display: inline-flex;
            align-items: center;
            gap: 6px;
            flex: 0 0 auto;
            padding: 7px 9px;
            border: 1px solid rgba(190,151,255,.38);
            border-radius: 999px;
            color: #e0d0ff;
            background: linear-gradient(145deg, rgba(76,49,119,.56), rgba(23,18,39,.9));
            box-shadow: inset 0 1px 0 rgba(255,255,255,.1), 0 3px 9px rgba(0,0,0,.2);
            font: 750 9px/1 ui-monospace, SFMono-Regular, Menlo, monospace;
            letter-spacing: .04em;
            white-space: nowrap;
        }
        #app .solana-investor-caption { margin: 9px 0 11px; color: #b5a8ca; font-size: 11px; line-height: 1.4; }
        #app .solana-investor-metrics { display: grid; grid-template-columns: repeat(2,minmax(0,1fr)); gap: 7px; }
        #app .solana-investor-metric {
            min-width: 0;
            padding: 9px;
            border: 1px solid rgba(188,160,226,.18);
            border-radius: 13px;
            background: linear-gradient(150deg, rgba(31,25,48,.95), rgba(12,13,23,.98));
            box-shadow: inset 0 1px 0 rgba(255,255,255,.055), 0 4px 10px rgba(0,0,0,.17);
        }
        #app .solana-investor-metric > span {
            display: block;
            overflow: hidden;
            color: #b8a6d0;
            font: 700 8px/1.2 ui-monospace, SFMono-Regular, Menlo, monospace;
            letter-spacing: .07em;
            text-overflow: ellipsis;
            white-space: nowrap;
        }
        #app .solana-investor-metric strong {
            display: block;
            overflow: hidden;
            margin-top: 4px;
            color: #f4efff;
            font: 760 15px/1.15 ui-monospace, SFMono-Regular, Menlo, monospace;
            text-overflow: ellipsis;
            white-space: nowrap;
        }
        #app .solana-investor-controls { display: grid; grid-template-columns: 1fr; gap: 8px; margin-top: 9px; }
        #app .solana-investor-toggle {
            width: 100%;
            min-height: 44px;
            margin: 0 !important;
            border-radius: 12px;
            font-size: 11px;
        }
        #app .solana-investor-fold {
            min-width: 0;
            padding: 9px 10px;
            border: 1px solid rgba(171,139,217,.2);
            border-radius: 12px;
            background: linear-gradient(145deg, rgba(25,20,39,.82), rgba(10,13,20,.92));
        }
        #app .solana-investor-fold summary { display: flex; justify-content: space-between; align-items: center; gap: 8px; color: #e0d1fa; font-size: 10px; }
        #app .solana-investor-fold[open] { border-color: rgba(153,69,255,.42); }
        #app .solana-investor-fold-count { min-width: 20px; padding: 3px 6px; border-radius: 999px; color: #e4d5ff; background: rgba(128,75,198,.3); text-align: center; font: 700 9px/1.1 ui-monospace, monospace; }
        #app .solana-investor-list { max-height: 220px; overflow: auto; margin-top: 9px; color: #c6bbd8; font: 10px/1.55 ui-monospace, SFMono-Regular, Menlo, monospace; white-space: pre-line; overflow-wrap: anywhere; }
        #app .solana-investor-help, #app .solana-investor-rules { display: block; margin: 9px 0 0; color: #aa9dbd; font-size: 10px; line-height: 1.5; }
        #app .solana-investor-budget-grid { display: grid; grid-template-columns: repeat(3,minmax(0,1fr)); gap: 6px; margin-top: 9px; }
        #app .solana-investor-budget-cell { min-width: 0; padding: 8px; border: 1px solid rgba(188,160,226,.14); border-radius: 10px; background: rgba(9,11,18,.55); }
        #app .solana-investor-budget-cell span { display: block; color: #ac9fbd; font-size: 8px; line-height: 1.3; }
        #app .solana-investor-budget-cell strong { display: block; overflow: hidden; margin-top: 4px; color: #e8ddf8; font: 700 10px/1.25 ui-monospace, monospace; text-overflow: ellipsis; white-space: nowrap; }
        #app .solana-investor-budget-form { display: grid; grid-template-columns: repeat(2,minmax(0,1fr)); gap: 8px; margin-top: 10px; }
        #app .solana-investor-budget-form .settings-label { margin: 0 0 4px 2px; font-size: 9px; }
        #app .solana-investor-budget-form .sig-input { box-sizing: border-box; width: 100%; height: 42px; margin: 0; padding: 8px 9px; font-size: 12px; }
        #app .solana-investor-budget-save { grid-column: 1 / -1; width: 100%; min-height: 42px; margin: 0 !important; padding: 0 8px; font-size: 11px; }
        /* Keep each network card readable even when desktop shows them side by side. */
        #app .base-paper-card, #app .solana-investor-card {
            container-type: inline-size;
            container-name: investor-panel;
        }
        @container investor-panel (max-width: 480px) {
            #app .base-paper-head {
                display: grid;
                grid-template-columns: minmax(0, 1fr);
                align-items: start;
                gap: 9px;
            }
            #app .base-network-identity { width: 100%; }
            #app .base-network-identity > div { min-width: 0; }
            #app .base-paper-head h2 { overflow-wrap: anywhere; }
            #app .base-paper-badge, #app .solana-investor-badge {
                grid-column: 1;
                justify-self: start;
                max-width: 100%;
                white-space: normal;
                line-height: 1.25;
            }
            #app .base-paper-metrics { grid-template-columns: repeat(2, minmax(0, 1fr)); }
            #app .base-paper-metric:last-child { grid-column: 1 / -1; }
            #app .base-paper-controls { grid-template-columns: repeat(2, minmax(0, 1fr)); }
            #app .base-paper-budget-field { grid-column: 1 / -1; }
            #app .base-paper-save, #app .base-paper-scan {
                height: auto;
                min-height: 44px;
                padding: 7px 8px;
                line-height: 1.2;
                white-space: normal;
                overflow-wrap: anywhere;
            }
            #app .base-paper-status, #app .base-paper-caption,
            #app .solana-investor-caption, #app .solana-investor-help,
            #app .solana-investor-rules { overflow-wrap: anywhere; }
        }
        @container investor-panel (max-width: 340px) {
            #app .base-paper-controls { grid-template-columns: 1fr; }
            #app .base-paper-budget-field { grid-column: 1; }
            #app .base-paper-details { grid-template-columns: 1fr; }
        }
        @media (max-width: 600px) {
            #app .base-paper-head {
                display: grid;
                grid-template-columns: minmax(0, 1fr);
                align-items: start;
                gap: 9px;
            }
            #app .base-network-identity { width: 100%; }
            #app .base-network-identity > div { min-width: 0; }
            #app .base-paper-head h2 { overflow-wrap: anywhere; }
            #app .base-paper-badge, #app .solana-investor-badge {
                grid-column: 1;
                justify-self: start;
                max-width: 100%;
                white-space: normal;
                line-height: 1.25;
            }
            #app .base-paper-metrics { grid-template-columns: repeat(2, minmax(0, 1fr)); }
            #app .base-paper-metric:last-child { grid-column: 1 / -1; }
            #app .base-paper-controls { grid-template-columns: repeat(2, minmax(0, 1fr)); }
            #app .base-paper-budget-field { grid-column: 1 / -1; }
            #app .base-paper-save, #app .base-paper-scan {
                height: auto;
                min-height: 44px;
                padding: 7px 8px;
                line-height: 1.2;
                white-space: normal;
                overflow-wrap: anywhere;
            }
            #app .base-paper-status, #app .base-paper-caption,
            #app .solana-investor-caption, #app .solana-investor-help,
            #app .solana-investor-rules { overflow-wrap: anywhere; }
        }
        @media (max-width: 360px) {
            #app .base-paper-controls { grid-template-columns: 1fr; }
            #app .base-paper-budget-field { grid-column: 1; }
            #app .base-paper-details { grid-template-columns: 1fr; }
        }
        @media (max-width: 380px) {
            #app .tab-panel { padding-right: 14px; padding-left: 14px; }
            #app .card:not(#partner-program), #app .deposit-modal { padding: 15px; }
            #app .stat-box { padding: 11px; }
            #app .stat-val { font-size: 15px; }
            #app .base-paper-card { padding: 13px !important; }
            #app .base-paper-metric { padding: 8px 7px; }
            #app .base-paper-metric strong { font-size: 14px; }
            #app .base-paper-controls { grid-template-columns: 1fr 1fr; }
            #app .base-paper-budget-field { grid-column: 1 / -1; }
            #app .base-paper-details { grid-template-columns: 1fr; }
            #app .base-paper-fold-wide { grid-column: auto; }
            #app .solana-investor-card { padding: 13px !important; }
            #app .solana-investor-metric { padding: 8px 7px; }
            #app .solana-investor-metric strong { font-size: 13px; }
            #app .solana-investor-budget-grid { grid-template-columns: repeat(2,minmax(0,1fr)); }
        }
        @media (prefers-reduced-motion: reduce) {
            #app .btn-ctl, #app .btn-copy, #app .wallet-tab, #app .token-btn,
            #app .btn-green-main, #app .btn-verify, #app .action-btn {
                transition-duration: .01ms !important;
            }
        }
        /* Desktop terminal: fixed command rail, generous workspace, same mobile DOM. */
        @media (min-width: 1024px) {
            html, body { min-height: 100%; }
            body { overflow: hidden; }
            #nav.bottom-nav {
                position: fixed;
                inset: 0 auto 0 0;
                z-index: 900;
                width: 226px;
                padding: 30px 16px;
                display: flex;
                flex-direction: column;
                justify-content: flex-start;
                align-items: stretch;
                gap: 9px;
                border-top: 0;
                border-right: 1px solid rgba(210, 235, 194, .14);
                box-shadow: 12px 0 34px rgba(0, 0, 0, .24), inset -1px 0 0 rgba(255,255,255,.02);
            }
            #nav::before {
                content: "ZER0LIFE";
                display: block;
                margin: 4px 10px 28px;
                padding: 0 0 20px;
                border-bottom: 1px solid rgba(210, 235, 194, .12);
                color: #eff4e8;
                font: 800 15px/1 ui-monospace, SFMono-Regular, Menlo, monospace;
                letter-spacing: .14em;
            }
            #nav .nav-btn {
                flex: 0 0 auto;
                display: flex;
                align-items: center;
                gap: 13px;
                min-height: 50px;
                padding: 0 13px;
                text-align: left;
                font-size: 13px;
                letter-spacing: .01em;
            }
            #nav .nav-btn span {
                display: inline-grid;
                place-items: center;
                width: 23px;
                margin: 0;
                color: #a9bd91;
                font-size: 18px;
            }
            #app .tab-panel {
                width: auto;
                max-width: none;
                height: 100dvh;
                margin: 0 0 0 226px;
                padding: 42px clamp(28px, 4vw, 68px) 52px;
                overflow-x: hidden;
                overflow-y: auto;
                scrollbar-color: rgba(200,255,55,.28) rgba(9,13,9,.5);
                scrollbar-width: thin;
            }
            #app .tab-panel.active {
                display: grid;
                grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
                grid-auto-rows: max-content;
                grid-auto-flow: row;
                align-content: start;
                align-items: start;
                gap: 18px;
            }
            #app .tab-panel > * { min-width: 0; }
            #app .page-heading { grid-column: 1 / -1; margin-bottom: 2px; }
            #app .page-heading h1 { font-size: 32px; }
            #app .card:not(#partner-program), #app .deposit-modal { margin-bottom: 0; }
            #app #tab-terminal .account-summary,
            #app #tab-terminal .overview-funds,
            #app #tab-terminal .quick-actions,
            #app #tab-terminal #btn-ai-crypto-investors { grid-column: 1 / -1; }
            #app #tab-terminal .control-bar { max-width: 680px; margin: 0; }
            #app #tab-terminal .btn-sell-all { margin: 0; min-height: 46px; }
            #app #tab-trading #trading-controls,
            #app #tab-trading #trading-ai-activity { grid-column: 1 / -1; }
            #app #tab-trading #trading-ai-activity {
                height: auto;
                min-height: max-content;
                align-self: stretch;
            }
            #app #tab-trading #trading-strategy,
            #app #tab-trading #trading-sniper,
            #app #tab-trading .trading-readiness-card,
            #app #tab-trading #trading-activity-panel { grid-column: span 1; }
            #app #tab-trading details { padding: 15px 20px; border: 1px solid rgba(210,235,194,.12); border-radius: 17px; background: rgba(10,15,10,.68); }
            #app #tab-deposit .deposit-modal,
            #app #tab-deposit #monthly-trading-fee-card { grid-column: span 1; }
            #app #tab-deposit .deposit-modal { height: fit-content; }
            #app #tab-settings #support-card,
            #app #tab-settings #passkey-settings,
            #app #tab-settings #settings-order-card { grid-column: span 1; }
            #app #tab-settings #partner-program { grid-column: 1 / -1; }
            #app #tab-tariffs > .card,
            #app #tab-stats #stats-summary,
            #app #tab-stats #stats-history { grid-column: 1 / -1; }
            #app .grid-stats { gap: 11px; }
            #app .log-console { height: 230px; }
            #app .tariff-packages { grid-template-columns: repeat(2, minmax(0, 1fr)); }
            #app .partner-table, #app .partner-invite { max-width: 900px; }
            #app .sig-input, #app textarea { max-width: 100%; }
        }
        /* Exchange: compact per-account bridge form with explicit per-transfer confirmation. */
        #app #tab-exchange { max-width: 900px; }
        .exchange-hero {
            position:relative; overflow:hidden; padding:20px;
            border:1px solid rgba(205,218,232,.12); border-radius:24px;
            background:radial-gradient(ellipse at 100% 0,rgba(176,133,255,.1),transparent 36%),
                linear-gradient(150deg,#101114 0%,#08090b 62%,#11120e 100%);
            box-shadow:inset 0 1px 0 rgba(255,255,255,.05),0 24px 55px rgba(0,0,0,.25);
        }
        .exchange-heading { position:relative; z-index:2; display:flex; align-items:flex-start; justify-content:space-between; gap:12px; }
        .exchange-heading h1 { margin:0; color:#f0f3e8; font-size:clamp(26px,6vw,34px); letter-spacing:-.045em; }
        .exchange-kicker { margin:0 0 7px; color:#a7b6c6; font:700 10px/1.2 ui-monospace,SFMono-Regular,Menlo,monospace; letter-spacing:.16em; }
        .exchange-status { flex:none; border:1px solid rgba(239,190,111,.35); border-radius:99px; padding:7px 10px; color:#efc77e; background:rgba(63,45,22,.35); font:700 9px/1.3 ui-monospace,SFMono-Regular,Menlo,monospace; letter-spacing:.06em; text-align:center; }
        .exchange-direction { position:relative; z-index:2; display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:8px; margin:16px 0 0; }
        .exchange-direction button { min-height:42px; border:1px solid rgba(190,205,222,.16); border-radius:13px; color:#aeb8c6; background:rgba(24,26,31,.85); font-size:11px; font-weight:750; cursor:pointer; }
        .exchange-direction button[aria-pressed="true"] { border-color:rgba(196,238,142,.55); color:#eff8e4; background:linear-gradient(145deg,rgba(61,79,47,.62),rgba(26,32,26,.95)); box-shadow:inset 0 1px 0 rgba(255,255,255,.08),0 5px 20px rgba(120,177,87,.1); }
        .exchange-direction button:focus-visible { outline:2px solid #c5ee9a; outline-offset:2px; }
        .exchange-quote-panel { padding:14px; border:1px solid rgba(190,205,222,.15); border-radius:22px; background:linear-gradient(155deg,rgba(20,21,25,.97),rgba(10,11,13,.97)); box-shadow:0 20px 50px rgba(0,0,0,.24); }
        .exchange-destination { display:flex; flex-wrap:wrap; align-items:center; gap:7px; margin:14px 0; color:#91a59e; font:700 9px/1.4 ui-monospace,SFMono-Regular,Menlo,monospace; letter-spacing:.08em; }
        .exchange-destination span { padding:8px 10px; border:1px solid rgba(183,216,189,.17); border-radius:9px; background:rgba(9,16,15,.45); }
        .exchange-disabled { width:100%; min-height:48px; margin-top:15px; border:1px solid #39443b; border-radius:13px; color:#a8b0a5; background:linear-gradient(180deg,#30382f,#252d26); font-size:12px; font-weight:750; cursor:not-allowed; opacity:.78; }
        .exchange-disabled-note { margin:8px 0 0; color:#a0a99e; text-align:center; font-size:10px; line-height:1.5; }
        .exchange-swap-card { padding:15px 15px 12px; border:1px solid rgba(201,210,223,.11); border-radius:19px; background:linear-gradient(145deg,#151619,#0e0f11); }
        .exchange-swap-label { display:block; margin-bottom:11px; color:#aeb3bd; font-size:12px; font-weight:700; }
        .exchange-swap-main { display:grid; grid-template-columns:minmax(0,1fr) auto; align-items:center; gap:11px; }
        .exchange-quote-input { width:100%; min-width:0; min-height:50px; padding:0; border:0; color:#f3f4f7; background:transparent; font:700 clamp(28px,8vw,38px)/1 ui-monospace,SFMono-Regular,Menlo,monospace; }
        .exchange-quote-input:focus-visible { outline:2px solid #c5ee9a; outline-offset:3px; border-radius:5px; }
        .exchange-swap-output { min-width:0; overflow:hidden; color:#f3f4f7; font:700 clamp(28px,8vw,38px)/1 ui-monospace,SFMono-Regular,Menlo,monospace; text-overflow:ellipsis; }
        .exchange-token-pill { display:inline-flex; align-items:center; gap:7px; min-height:38px; padding:0 12px; border:1px solid rgba(194,203,219,.1); border-radius:999px; color:#f3f4f7; background:#202125; font-size:13px; font-weight:800; white-space:nowrap; }
        .exchange-token-mark { display:grid; place-items:center; width:21px; height:21px; border-radius:50%; color:#10110d; background:linear-gradient(145deg,#e4ff9b,#a2de45); font-size:10px; font-weight:900; }
        .exchange-swap-meta { display:flex; justify-content:space-between; gap:8px; margin-top:9px; color:#8f949e; font-size:10px; }
        .exchange-swap-divider { position:relative; z-index:2; display:grid; place-items:center; height:20px; margin:-2px 0; color:#f2eafa; }
        .exchange-swap-divider span { display:grid; place-items:center; width:34px; height:34px; border:3px solid #101114; border-radius:50%; background:#a98bff; box-shadow:0 5px 18px rgba(169,139,255,.24); font-size:16px; font-weight:900; }
        .exchange-percentages { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:8px; margin:15px 0 11px; }
        .exchange-percentages button { min-height:40px; border:1px solid rgba(195,204,220,.1); border-radius:999px; color:#d8dbe2; background:#1b1c20; font-size:12px; font-weight:750; cursor:pointer; }
        .exchange-percentages button:disabled { cursor:not-allowed; opacity:.45; }
        .exchange-percentages button:not(:disabled):active { transform:scale(.98); background:#2a2c31; }
        .exchange-quote-form { display:grid; grid-template-columns:minmax(0,1fr); gap:10px; align-items:end; margin-top:12px; }
        .exchange-quote-field label { display:block; margin-bottom:7px; color:#a8b6a9; font-size:11px; font-weight:700; }
        .exchange-quote-input:focus-visible { outline:2px solid #c5ee9a; outline-offset:2px; }
        .exchange-quote-button { width:100%; min-height:49px; padding:0 18px; border:1px solid rgba(198,238,154,.6); border-radius:14px; color:#10170d; background:linear-gradient(135deg,#dcff99,#91d522); box-shadow:0 8px 20px rgba(145,213,34,.14); font-size:13px; font-weight:850; cursor:pointer; }
        .exchange-quote-button:disabled { cursor:wait; opacity:.6; }
        .exchange-quote-status { min-height:20px; margin:9px 0 0; color:#b8c4b5; font-size:11px; line-height:1.5; }
        .exchange-transfer-proof { display:grid; gap:7px; margin-top:8px; color:#a4b1a2; font-size:10px; line-height:1.5; }
        .exchange-transfer-proof a { color:#d7ff91; overflow-wrap:anywhere; word-break:break-all; }
        .exchange-quote-result { margin-top:14px; padding:15px; border:1px solid rgba(158,190,211,.24); border-radius:16px; background:linear-gradient(145deg,rgba(14,24,28,.96),rgba(13,20,22,.94)); }
        .exchange-quote-grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:9px; }
        .exchange-quote-value { min-width:0; padding:11px; border:1px solid rgba(190,218,179,.12); border-radius:12px; background:rgba(5,11,10,.34); }
        .exchange-quote-value span { display:block; color:#9ba99a; font-size:10px; }
        .exchange-quote-value strong { display:block; overflow-wrap:anywhere; margin-top:5px; color:#e7f5dc; font:750 13px ui-monospace,SFMono-Regular,Menlo,monospace; }
        .exchange-quote-fees { margin:12px 0 0; padding:0; list-style:none; color:#aebaae; font-size:11px; line-height:1.55; }
        .exchange-quote-fees li { padding:5px 0; border-bottom:1px solid rgba(190,218,179,.08); }
        .exchange-quote-footnote { margin:10px 0 0; color:#a4b1a2; font-size:10px; line-height:1.55; }
        .exchange-cost-details { margin-top:12px; border-top:1px solid rgba(190,205,222,.1); padding-top:10px; }
        .exchange-cost-details summary { color:#cbd1dc; font-size:11px; font-weight:750; cursor:pointer; }
        .exchange-fee-total { margin:7px 0 0; color:#aeb7c3; font-size:10px; }
        .exchange-fee-total strong { color:#eaf0f4; }
        .exchange-safety { margin-top:11px; color:#9299a2; font-size:10px; line-height:1.5; }
        .exchange-confirm-note { margin:10px 0 0; color:#aab3bf; font-size:10px; line-height:1.5; }
        @media (max-width: 430px) {
            #nav .nav-btn { padding-right:1px; padding-left:1px; font-size:9px; }
            #nav .nav-btn span { font-size:16px; }
            .exchange-hero { padding:17px 14px 12px; }
            .exchange-quote-panel { padding:11px; }
            .exchange-swap-card { padding:14px 12px 11px; }
            .exchange-quote-button { width:100%; }
        }
        @media (min-width: 1024px) {
            #app #tab-exchange.active { display:block; }
            #app #tab-exchange > .exchange-hero, #app #tab-exchange > .exchange-callout,
        #app #tab-exchange > .exchange-flow, #app #tab-exchange > .exchange-quote-panel,
            #app #tab-exchange > .exchange-safety { max-width:900px; }
        }
        /* Overview / Human × AI cockpit: dimensional graphite, signal-cyan, and a warm human trace. */
        #app #tab-terminal {
            --overview-edge: rgba(103, 211, 228, .2);
            --overview-ink: #edf5f7;
            --overview-muted: #a7b9c0;
            background:
                radial-gradient(ellipse at 84% 2%, rgba(42, 174, 200, .13), transparent 30%),
                radial-gradient(ellipse at 7% 44%, rgba(188, 105, 88, .07), transparent 32%),
                linear-gradient(145deg, rgba(8, 13, 19, .28), rgba(7, 10, 15, .12));
        }
        #app #tab-terminal .page-heading {
            position: relative;
            padding: 2px 0 11px;
            border-bottom: 1px solid rgba(112, 195, 211, .17);
            isolation: isolate;
        }
        #app #tab-terminal .page-heading::after {
            content: "";
            position: absolute;
            z-index: -1;
            top: -30px;
            right: 2%;
            width: 118px;
            height: 118px;
            border: 1px solid rgba(119, 222, 232, .23);
            border-radius: 50%;
            pointer-events: none;
            opacity: .68;
            background:
                radial-gradient(circle at 36% 38%, rgba(233, 165, 135, .18) 0 2px, transparent 3px),
                radial-gradient(circle at 67% 62%, rgba(100, 224, 235, .22) 0 2px, transparent 3px),
                conic-gradient(from 38deg, transparent 0 20%, rgba(102, 220, 232, .42) 22% 23%, transparent 26% 61%, rgba(218, 148, 125, .4) 64% 65%, transparent 69%),
                radial-gradient(circle, transparent 49%, rgba(92, 204, 222, .1) 50%, transparent 67%);
            box-shadow: inset 0 0 28px rgba(69, 190, 211, .07), 0 0 25px rgba(50, 171, 198, .06);
            animation: overview-synthesis-drift 34s linear infinite;
        }
        #app #tab-terminal .page-heading h1 {
            font-size: clamp(29px, 4vw, 40px);
            line-height: 1.05;
            letter-spacing: -.055em;
        }
        #app #tab-terminal #btn-ai-crypto-investors {
            min-height: 47px;
            border: 1px solid rgba(135, 231, 239, .78);
            border-radius: 13px;
            color: #07171c;
            background: linear-gradient(180deg, #b7f3f0 0%, #72d1d9 48%, #4bb0c1 100%);
            box-shadow: 0 7px 0 rgba(26, 71, 81, .85), 0 12px 26px rgba(57, 185, 203, .15), inset 0 1px 0 rgba(255,255,255,.78);
            font-weight: 850;
            letter-spacing: .015em;
            transform: translateY(-2px);
        }
        #app #tab-terminal .account-summary {
            padding: 17px 19px;
            border-color: rgba(105, 204, 218, .32) !important;
            border-radius: 18px;
            background:
                radial-gradient(ellipse at 100% 20%, rgba(59, 176, 197, .14), transparent 42%),
                linear-gradient(145deg, rgba(23, 37, 45, .98), rgba(11, 18, 25, .99) 65%) !important;
            box-shadow: 0 12px 26px rgba(0,0,0,.3), inset 0 1px 0 rgba(223,250,255,.12), inset 0 -3px 0 rgba(0,0,0,.25);
        }
        #app #tab-terminal .account-summary > div:first-child { color: #a6e9ed !important; letter-spacing: .01em; }
        #app #tab-terminal .account-summary #wallet-runtime-status { color: #becbd0 !important; line-height: 1.55; }
        #app #tab-terminal .control-bar {
            display: flex;
            flex-wrap: wrap;
            gap: 10px;
            align-items: stretch;
        }
        #app #tab-terminal .btn-ctl {
            min-height: 46px;
            border-radius: 13px;
            box-shadow: 0 5px 0 rgba(0,0,0,.25), inset 0 1px 0 rgba(255,255,255,.11);
            font-weight: 800;
            letter-spacing: .01em;
        }
        #app #tab-terminal #btn-toggle { flex: 1 1 220px; }
        #app #tab-terminal #btn-refresh-top { flex: 0 0 auto; min-width: 118px; }
        #app #tab-terminal .btn-sell-all {
            min-height: 46px;
            border: 1px solid rgba(219, 139, 122, .48);
            border-radius: 13px;
            color: #f1c9bf;
            background: linear-gradient(180deg, rgba(83, 45, 45, .96), rgba(40, 26, 31, .99));
            box-shadow: 0 5px 0 rgba(15, 10, 9, .58), inset 0 1px 0 rgba(255,255,255,.1);
            font-weight: 800;
        }
        #app #tab-terminal .overview-funds {
            padding: 18px;
            border-color: rgba(99, 194, 211, .28) !important;
            background:
                radial-gradient(ellipse at 86% 0%, rgba(53, 164, 190, .13), transparent 43%),
                linear-gradient(145deg, #192a32 0%, #101b23 55%, #0d151c 100%) !important;
            box-shadow: 0 16px 32px rgba(0,0,0,.34), inset 0 1px 0 rgba(225,250,255,.12), inset 0 -3px 0 rgba(0,0,0,.3);
        }
        #app #tab-terminal .overview-funds > div:first-child { color: #a5e6ec !important; }
        #app #tab-terminal .overview-funds .grid-stats { gap: 10px; }
        #app #tab-terminal .overview-funds .stat-box {
            padding: 13px 14px;
            border: 1px solid rgba(116, 197, 210, .14);
            border-radius: 13px;
            background: linear-gradient(150deg, rgba(7, 13, 20, .78), rgba(24, 40, 48, .67));
            box-shadow: inset 0 1px 0 rgba(255,255,255,.045), 0 5px 12px rgba(0,0,0,.13);
        }
        #app #tab-terminal .overview-funds .stat-label { color: #aebfc2; letter-spacing: .055em; }
        #app #tab-terminal .overview-funds .stat-val {
            margin-top: 6px;
            color: #eff8f8 !important;
            font-variant-numeric: tabular-nums;
            letter-spacing: -.025em;
        }
        #app #tab-terminal .overview-funds #port-withdrawable { color: #9ce5e8 !important; }
        #app #tab-terminal .overview-funds > div:last-child {
            min-height: 42px;
            margin-top: 11px !important;
            padding: 9px 13px !important;
            border: 1px solid rgba(115, 190, 207, .13);
            background: rgba(5, 11, 17, .78) !important;
        }
        #app #tab-terminal .overview-funds #port-pnl { font-variant-numeric: tabular-nums; }
        #app #tab-terminal #meme-sniper-card,
        #app #tab-terminal #core-strategy-card,
        #app #tab-terminal #activity-log-card {
            padding: 19px;
            border-color: var(--overview-edge) !important;
            background:
                radial-gradient(ellipse at 95% 0%, rgba(57, 166, 190, .075), transparent 38%),
                linear-gradient(150deg, rgba(21, 33, 42, .99), rgba(10, 17, 24, .99) 75%) !important;
            box-shadow: 0 13px 27px rgba(0,0,0,.3), inset 0 1px 0 rgba(225,248,255,.08), inset 0 -2px 0 rgba(0,0,0,.34);
        }
        #app #tab-terminal #meme-sniper-card > div:first-child > div:first-child > div:first-child,
        #app #tab-terminal #core-strategy-card > div:first-child > div:first-child > div:first-child { color: var(--overview-ink) !important; }
        #app #tab-terminal .section-note { color: var(--overview-muted); line-height: 1.55; }
        #app #tab-terminal .grid-stats { gap: 9px; }
        #app #tab-terminal .stat-box {
            min-width: 0;
            padding: 11px 12px;
            border: 1px solid rgba(106, 184, 201, .15);
            border-radius: 12px;
            background: linear-gradient(145deg, rgba(6, 12, 18, .78), rgba(18, 31, 39, .78));
            box-shadow: inset 0 1px 0 rgba(255,255,255,.045);
        }
        #app #tab-terminal .stat-label { color: #a8b9bf; }
        #app #tab-terminal .stat-val { color: #e6f0f2; font-variant-numeric: tabular-nums; overflow-wrap: anywhere; }
        #app #tab-terminal #discovery-status,
        #app #tab-terminal #entry-cost-check {
            padding: 10px 12px;
            border-left: 2px solid rgba(116, 212, 222, .63);
            border-radius: 0 9px 9px 0;
            background: rgba(47, 130, 148, .11);
        }
        #app #tab-terminal #scan-candidates {
            min-height: 44px;
            border: 1px solid rgba(105, 204, 219, .49);
            border-radius: 12px;
            color: #e2f6f6;
            background: linear-gradient(180deg, rgba(37, 86, 98, .97), rgba(20, 46, 56, .99));
            box-shadow: 0 5px 0 rgba(8, 15, 10, .62), inset 0 1px 0 rgba(255,255,255,.12);
            font-weight: 800;
        }
        #app #tab-terminal details { border-color: rgba(104,179,197,.16); }
        #app #tab-terminal summary { color: #d5e7e9; }
        #app #tab-terminal #activity-log-card > div:first-child { color: #b8dadd !important; letter-spacing: .1em; }
        #app #tab-terminal .log-console {
            border-color: rgba(104, 179, 197, .16);
            border-radius: 12px;
            background: rgba(4, 9, 15, .82);
        }
        #app #tab-terminal button:focus-visible,
        #app #tab-terminal summary:focus-visible,
        #nav .nav-btn:focus-visible {
            outline: 2px solid #9be9ee;
            outline-offset: 3px;
        }
        #nav.bottom-nav {
            background: linear-gradient(180deg, rgba(14, 23, 30, .99), rgba(8, 14, 20, .995));
            border-color: rgba(105, 183, 199, .19);
            -webkit-backdrop-filter: blur(18px);
            backdrop-filter: blur(18px);
        }
        #nav .nav-btn { transition: transform .18s ease, background-color .18s ease, color .18s ease; }
        #nav .nav-btn.active { box-shadow: inset 0 1px 0 rgba(255,255,255,.2), 0 5px 15px rgba(71,171,191,.12); }
        #app #tab-terminal::after {
            content: "";
            position: absolute;
            z-index: 0;
            top: 0;
            left: -28%;
            width: 156%;
            height: 42%;
            min-height: 220px;
            pointer-events: none;
            opacity: .42;
            background:
                linear-gradient(112deg, transparent 41%, rgba(87, 203, 220, .035) 48%, rgba(122, 224, 231, .075) 50%, rgba(212, 145, 125, .035) 52%, transparent 59%);
            transform: translate3d(-11%, 0, 0);
            animation: overview-ambient-drift 30s ease-in-out infinite alternate;
        }
        #app #tab-terminal .account-summary,
        #app #tab-terminal .overview-funds,
        #app #tab-terminal #meme-sniper-card,
        #app #tab-terminal #core-strategy-card,
        #app #tab-terminal #activity-log-card {
            transition: transform .24s cubic-bezier(.2,.75,.25,1), box-shadow .24s ease;
        }
        #app #tab-terminal button:not(:disabled) {
            transition: transform .16s ease, box-shadow .16s ease, filter .16s ease;
        }
        #app #tab-terminal button:not(:disabled):focus-visible {
            transform: translateY(-1px);
            filter: brightness(1.06);
        }
        #app #tab-terminal .account-summary:focus-within,
        #app #tab-terminal .overview-funds:focus-within,
        #app #tab-terminal #meme-sniper-card:focus-within,
        #app #tab-terminal #core-strategy-card:focus-within,
        #app #tab-terminal #activity-log-card:focus-within {
            transform: translateY(-2px);
            box-shadow: 0 18px 34px rgba(0,0,0,.34), inset 0 1px 0 rgba(225,248,255,.09), inset 0 -2px 0 rgba(0,0,0,.34);
        }
        @media (hover: hover) and (pointer: fine) {
            #app #tab-terminal .account-summary:hover,
            #app #tab-terminal .overview-funds:hover,
            #app #tab-terminal #meme-sniper-card:hover,
            #app #tab-terminal #core-strategy-card:hover,
            #app #tab-terminal #activity-log-card:hover {
                transform: translateY(-3px);
                box-shadow: 0 20px 38px rgba(0,0,0,.38), inset 0 1px 0 rgba(225,248,255,.1), inset 0 -2px 0 rgba(0,0,0,.34);
            }
            #app #tab-terminal button:not(:disabled):hover {
                transform: translateY(-2px);
                filter: brightness(1.08);
            }
            #app #tab-terminal button:not(:disabled):active {
                transform: translateY(1px);
                filter: brightness(.98);
            }
        }
        @media (max-width: 767px) {
            #app #tab-terminal::after { display: none; }
            #app #tab-terminal .account-summary,
            #app #tab-terminal .overview-funds,
            #app #tab-terminal #meme-sniper-card,
            #app #tab-terminal #core-strategy-card,
            #app #tab-terminal #activity-log-card { transition-duration: .16s; }
        }
        @keyframes overview-synthesis-drift {
            from { transform: rotate(0deg); }
            to { transform: rotate(360deg); }
        }
        @keyframes overview-ambient-drift {
            from { transform: translate3d(-11%, 0, 0); }
            to { transform: translate3d(11%, 0, 0); }
        }
        @media (min-width: 1024px) {
            #app #tab-terminal.active { grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 16px; }
            #app #tab-terminal .page-heading,
            #app #tab-terminal #btn-ai-crypto-investors,
            #app #tab-terminal .account-summary,
            #app #tab-terminal .overview-funds,
            #app #tab-terminal .control-bar,
            #app #tab-terminal .btn-sell-all,
            #app #tab-terminal #activity-log-card { grid-column: 1 / -1; }
            #app #tab-terminal .control-bar { max-width: 680px; }
        }
        @media (max-width: 767px) {
            #app #tab-terminal .page-heading { padding-bottom: 9px; }
            #app #tab-terminal .account-summary { padding: 15px 16px; }
            #app #tab-terminal .overview-funds,
            #app #tab-terminal #meme-sniper-card,
            #app #tab-terminal #core-strategy-card,
            #app #tab-terminal #activity-log-card { padding: 15px; }
            #app #tab-terminal .overview-funds .grid-stats { grid-template-columns: repeat(2, minmax(0, 1fr)); }
        }
        @media (max-width: 380px) {
            #app #tab-terminal .overview-funds .grid-stats,
            #app #tab-terminal #meme-sniper-card .grid-stats { grid-template-columns: 1fr; }
            #app #tab-terminal .control-bar { gap: 8px; }
            #app #tab-terminal #btn-toggle { flex-basis: 100%; }
            #app #tab-terminal #btn-refresh-top { flex: 1 1 100%; }
        }
        @media (prefers-reduced-motion: reduce) {
            #app #tab-terminal *, #nav .nav-btn {
                transition-duration: .01ms !important;
                animation: none !important;
            }
        }
        /* Trading command deck — scoped surface treatment; all trading logic remains unchanged. */
        #app #tab-trading {
            --trade-ink: #edf3e8;
            --trade-muted: #a9b8aa;
            --trade-line: rgba(179, 220, 173, .17);
            --trade-lime: #c7f36b;
            --trade-mint: #76e5b2;
            --trade-warn: #e69aab;
            position: relative;
            isolation: isolate;
            background:
                radial-gradient(ellipse at 88% 0%, rgba(80, 151, 91, .11), transparent 38%),
                radial-gradient(ellipse at 4% 42%, rgba(70, 116, 130, .08), transparent 35%);
            color: var(--trade-ink);
        }
        #app #tab-trading::before {
            content: "";
            position: fixed;
            z-index: -1;
            inset: 0 0 0 226px;
            pointer-events: none;
            opacity: .17;
            background-image: linear-gradient(rgba(181, 220, 171, .05) 1px, transparent 1px),
                linear-gradient(90deg, rgba(181, 220, 171, .05) 1px, transparent 1px);
            background-size: 42px 42px;
            mask-image: linear-gradient(110deg, transparent 4%, #000 70%);
        }
        #app #tab-trading .page-heading {
            position: relative;
            padding-bottom: 13px;
            border-bottom: 1px solid var(--trade-line);
        }
        #app #tab-trading .page-heading::after {
            content: "";
            position: absolute;
            bottom: -1px;
            left: 0;
            width: 76px;
            height: 2px;
            background: linear-gradient(90deg, var(--trade-lime), transparent);
        }
        #app #tab-trading .page-heading .eyebrow {
            color: var(--trade-mint);
            font: 700 10px/1.4 ui-monospace, SFMono-Regular, Menlo, monospace;
            letter-spacing: .16em;
            text-transform: uppercase;
        }
        #app #tab-trading .page-heading h1 {
            color: #f0f5e9;
            letter-spacing: -.045em;
            text-shadow: 0 2px 18px rgba(184, 238, 110, .10);
        }
        #app #tab-trading #trading-controls { min-width: 0; }
        #app #tab-trading .control-bar {
            width: 100%;
            max-width: none;
            min-height: 58px;
            margin: 0;
            padding: 8px;
            gap: 10px;
            border: 1px solid rgba(230, 154, 171, .24);
            border-radius: 17px;
            background: linear-gradient(105deg, rgba(77, 34, 48, .78), rgba(27, 24, 30, .92) 63%, rgba(21, 31, 28, .9));
            box-shadow: inset 0 1px 0 rgba(255,255,255,.08), 0 8px 0 rgba(22, 16, 24, .72), 0 17px 32px rgba(0,0,0,.2);
        }
        #app #tab-trading .control-bar .btn-toggle-off {
            min-height: 40px;
            flex: 1 1 auto;
            justify-content: center;
            border: 1px solid rgba(235, 151, 171, .5) !important;
            border-radius: 10px;
            color: #f3dce1 !important;
            background: linear-gradient(180deg, rgba(113, 48, 67, .9), rgba(67, 32, 45, .95)) !important;
            box-shadow: inset 0 1px 0 rgba(255,255,255,.14), 0 3px 0 rgba(43, 19, 29, .9) !important;
        }
        #app #tab-trading .control-bar .btn-toggle-off #toggle-dot { color: #f2a5b6; }
        #app #tab-trading .control-bar .btn-toggle-on {
            min-height: 40px;
            flex: 1 1 auto;
            justify-content: center;
            border: 1px solid rgba(118, 229, 178, .48) !important;
            border-radius: 10px;
            color: #d8f2e4 !important;
            background: linear-gradient(180deg, rgba(35, 96, 70, .92), rgba(20, 57, 43, .96)) !important;
            box-shadow: inset 0 1px 0 rgba(255,255,255,.14), 0 3px 0 rgba(11, 35, 25, .92) !important;
        }
        #app #tab-trading .control-bar .btn-toggle-on #toggle-dot { color: #8af0bf; }
        #app #tab-trading .control-bar .btn-refresh {
            min-height: 40px;
            flex: 0 0 auto;
            border: 1px solid rgba(181, 220, 173, .24);
            border-radius: 10px;
            color: #d8e8d1;
            background: linear-gradient(180deg, #344438, #202c25);
            box-shadow: inset 0 1px 0 rgba(255,255,255,.12), 0 3px 0 #141d18;
        }
        #app #tab-trading .card,
        #app #tab-trading details#trading-activity-panel {
            position: relative;
            min-width: 0;
            overflow: hidden;
            border: 1px solid var(--trade-line) !important;
            border-radius: 18px;
            background:
                linear-gradient(145deg, rgba(30, 39, 34, .96), rgba(14, 20, 18, .98) 65%),
                #111714 !important;
            box-shadow: inset 0 1px 0 rgba(255,255,255,.075), inset 0 -15px 28px rgba(0,0,0,.2),
                0 5px 0 rgba(6, 11, 9, .78), 0 15px 30px rgba(0,0,0,.24);
        }
        #app #tab-trading .card::before,
        #app #tab-trading details#trading-activity-panel::before {
            content: "";
            position: absolute;
            z-index: 0;
            inset: 0 10% auto;
            height: 1px;
            pointer-events: none;
            background: linear-gradient(90deg, transparent, rgba(205, 239, 190, .42), transparent);
        }
        #app #tab-trading .card > *,
        #app #tab-trading details#trading-activity-panel > * { position: relative; }
        #app #tab-trading #trading-ai-activity {
            padding: clamp(17px, 2.2vw, 25px);
            border-color: rgba(147, 202, 151, .26) !important;
            background:
                radial-gradient(ellipse at 94% 0%, rgba(109, 178, 103, .13), transparent 34%),
                linear-gradient(145deg, rgba(28, 39, 34, .98), rgba(13, 19, 18, .98) 72%) !important;
        }
        #app #tab-trading #trading-ai-activity h3 {
            margin-bottom: 16px;
            color: #eaf3e3;
            font-size: 16px;
            letter-spacing: -.015em;
        }
        #app #tab-trading .status-output,
        #app #tab-trading .section-note {
            color: var(--trade-muted);
            overflow-wrap: anywhere;
            line-height: 1.58;
        }
        #app #tab-trading #ai-status,
        #app #tab-trading #ai-decisions,
        #app #tab-trading #ai-risk-review,
        #app #tab-trading #ai-chart-zones,
        #app #tab-trading #execution-policy-result {
            padding: 10px 12px;
            border: 1px solid rgba(176, 203, 176, .13);
            border-left: 2px solid rgba(199, 243, 107, .52);
            border-radius: 9px;
            background: rgba(5, 11, 9, .34);
            color: #d5e3d3;
            font-size: 12px;
            line-height: 1.6;
        }
        #app #tab-trading #ai-latency,
        #app #tab-trading #ai-learning { padding: 0 2px; font-size: 11px; }
        #app #tab-trading #ai-risk-review { border-left-color: rgba(230, 154, 171, .62); }
        #app #tab-trading #trading-ai-activity details {
            margin-top: 14px;
            border-color: rgba(182, 215, 179, .18);
            background: rgba(7, 12, 10, .46);
        }
        #app #tab-trading #trading-ai-activity details p { max-width: 100ch; }
        #app #tab-trading #trading-ai-activity details p:last-child { color: #b3c1b5; }
        #app #tab-trading #trading-ai-activity details.trading-analysis-disclosure {
            margin-top: 7px;
            padding: 0;
            border: 1px solid rgba(182, 215, 179, .15);
            border-radius: 10px;
            background: rgba(5, 11, 9, .3);
        }
        #app #tab-trading #trading-ai-activity details.trading-analysis-disclosure > summary {
            min-height: 36px;
            padding: 9px 11px;
            color: #cbdac9;
            font-size: 11px;
            font-weight: 700;
            cursor: pointer;
        }
        #app #tab-trading #trading-ai-activity details.trading-analysis-disclosure[open] > summary {
            border-bottom: 1px solid rgba(182, 215, 179, .12);
            color: #e1edda;
        }
        #app #tab-trading #trading-ai-activity details.trading-analysis-disclosure > p {
            margin-top: 8px;
            margin-bottom: 8px;
        }
        #app #tab-trading #trading-strategy,
        #app #tab-trading #trading-sniper { min-width: 0; }
        #app #tab-trading #core-strategy-card,
        #app #tab-trading #meme-sniper-card {
            height: 100%;
            padding: clamp(16px, 1.8vw, 21px);
        }
        #app #tab-trading #core-strategy-card > div:first-child,
        #app #tab-trading #meme-sniper-card > div:first-child { gap: 12px; }
        #app #tab-trading #core-strategy-card > div:first-child > div:first-child > div:first-child,
        #app #tab-trading #meme-sniper-card > div:first-child > div:first-child > div:first-child {
            color: #edf5e9 !important;
            letter-spacing: -.02em;
        }
        #app #tab-trading #user-bal-small {
            flex: 0 0 auto;
            padding: 8px 10px;
            border: 1px solid rgba(118, 229, 178, .25);
            border-radius: 9px;
            color: #a8f0c9 !important;
            background: rgba(41, 106, 77, .16);
            font-size: 13px !important;
            font-variant-numeric: tabular-nums;
        }
        #app #tab-trading .grid-stats {
            gap: 8px;
            margin-top: 15px;
        }
        #app #tab-trading .stat-box {
            min-width: 0;
            padding: 11px 12px;
            border: 1px solid rgba(179, 220, 173, .14);
            border-radius: 11px;
            background: linear-gradient(145deg, rgba(29, 42, 35, .88), rgba(12, 19, 16, .9));
            box-shadow: inset 0 1px 0 rgba(255,255,255,.045), 0 3px 0 rgba(5, 10, 8, .48);
        }
        #app #tab-trading .stat-label {
            color: #9cad9e;
            font-size: 10px;
            letter-spacing: .06em;
            text-transform: uppercase;
        }
        #app #tab-trading .stat-val {
            margin-top: 5px;
            color: #eff6e9;
            font-size: clamp(13px, 1.25vw, 17px);
            line-height: 1.25;
            font-variant-numeric: tabular-nums;
            overflow-wrap: anywhere;
        }
        #app #tab-trading #strategy-assets,
        #app #tab-trading #sniper-position-cap,
        #app #tab-trading #entry-cost-check,
        #app #tab-trading #sniper-pnl { color: #b1c0b2 !important; }
        #app #tab-trading #core-strategy-card > p,
        #app #tab-trading #meme-sniper-card > p { font-size: 11px; }
        #app #tab-terminal .trading-compact-description,
        #app #tab-trading .trading-compact-description {
            margin: 8px 2px 0;
            color: #bdcabc;
            font-size: 11px;
            line-height: 1.45;
        }
        #app #tab-terminal details.trading-description-disclosure,
        #app #tab-trading details.trading-description-disclosure {
            margin: 7px 0 0;
            padding: 0;
            border: 1px solid rgba(179, 220, 173, .13);
            border-radius: 10px;
            background: rgba(6, 12, 9, .42);
        }
        #app #tab-terminal details.trading-description-disclosure > summary,
        #app #tab-trading details.trading-description-disclosure > summary {
            min-height: 36px;
            padding: 9px 11px;
            color: #c9d9c5;
            font-size: 11px;
            font-weight: 700;
            cursor: pointer;
        }
        #app #tab-terminal details.trading-description-disclosure[open] > summary,
        #app #tab-trading details.trading-description-disclosure[open] > summary {
            border-bottom: 1px solid rgba(179, 220, 173, .12);
            color: #e1edda;
        }
        #app #tab-terminal details.trading-description-disclosure > .section-note,
        #app #tab-trading details.trading-description-disclosure > .section-note {
            margin: 0;
            padding: 10px 11px 12px;
            color: #aebcae;
            font-size: 11px;
            line-height: 1.55;
        }
        #app #tab-trading .trading-readiness-card {
            padding: 16px;
            border-color: rgba(222, 190, 112, .22) !important;
            background: linear-gradient(145deg, rgba(49, 42, 28, .78), rgba(20, 21, 17, .96)) !important;
        }
        #app #tab-trading .trading-readiness-card #trading-readiness {
            margin-top: 12px;
            padding: 12px 13px;
            border: 1px solid rgba(222, 190, 112, .2);
            border-radius: 10px;
            color: #e7ddbf;
            background: rgba(11, 12, 9, .4);
            line-height: 1.55;
        }
        #app #tab-trading details#trading-activity-panel {
            min-width: 0;
            padding: 0;
            border-color: rgba(177, 202, 178, .2);
        }
        #app #tab-trading details#trading-activity-panel > summary {
            min-height: 54px;
            padding: 17px 20px;
            color: #e2eddf;
            font-size: 12px;
            font-weight: 750;
            letter-spacing: .035em;
            cursor: pointer;
        }
        #app #tab-trading details#trading-activity-panel[open] > summary {
            border-bottom: 1px solid var(--trade-line);
            background: rgba(121, 163, 120, .06);
        }
        #app #tab-trading #trading-activity { padding: 14px; }
        #app #tab-trading #trading-activity #activity-log-card {
            padding: 13px;
            border-radius: 12px;
            background: rgba(7, 13, 10, .76) !important;
            box-shadow: none;
        }
        #app #tab-trading #activity-log-card > div:first-child {
            color: #c8d9c7 !important;
            letter-spacing: .12em;
        }
        #app #tab-trading .log-console {
            max-height: min(42vh, 420px);
            overflow: auto;
            padding: 12px 13px;
            border: 1px solid rgba(166, 198, 166, .13);
            border-radius: 10px;
            background: #080e0b;
            color: #bac9ba;
            font: 11px/1.65 ui-monospace, SFMono-Regular, Menlo, monospace;
            overflow-wrap: anywhere;
            scrollbar-color: rgba(199, 243, 107, .32) rgba(7, 12, 9, .7);
            scrollbar-width: thin;
        }
        #app #tab-trading button,
        #app #tab-trading summary { -webkit-tap-highlight-color: transparent; }
        #app #tab-trading :is(button, summary, a, input, select, textarea):focus-visible {
            outline: 2px solid var(--trade-lime) !important;
            outline-offset: 3px;
            box-shadow: 0 0 0 4px rgba(199, 243, 107, .14);
        }
        #app #tab-trading button:not(:disabled) { transition: transform .16s ease, filter .16s ease; }
        #app #tab-trading button:not(:disabled):hover { filter: brightness(1.08); }
        #app #tab-trading button:not(:disabled):active { transform: translateY(2px); }
        #app #tab-trading button:disabled { cursor: not-allowed; filter: saturate(.68); }
        @media (hover: hover) and (prefers-reduced-motion: no-preference) {
            #app #tab-trading .card,
            #app #tab-trading details#trading-activity-panel {
                transform-style: preserve-3d;
                transition: transform .22s ease, box-shadow .22s ease, border-color .22s ease;
            }
            #app #tab-trading .card:hover,
            #app #tab-trading details#trading-activity-panel:hover {
                transform: perspective(1200px) translateY(-2px) rotateX(.25deg);
                border-color: rgba(199, 243, 107, .3) !important;
                box-shadow: inset 0 1px 0 rgba(255,255,255,.09), 0 7px 0 rgba(6, 11, 9, .78),
                    0 20px 34px rgba(0,0,0,.29);
            }
        }
        @media (min-width: 768px) {
            #app #tab-trading #trading-strategy,
            #app #tab-trading #trading-sniper,
            #app #tab-trading .trading-readiness-card,
            #app #tab-trading #trading-activity-panel { min-width: 0; }
            #app #tab-trading #trading-strategy,
            #app #tab-trading .trading-readiness-card { grid-column: 1; }
            #app #tab-trading #trading-sniper,
            #app #tab-trading #trading-activity-panel { grid-column: 2; }
        }
        @media (max-width: 767px) {
            #app #tab-trading {
                padding-bottom: calc(150px + env(safe-area-inset-bottom, 0px));
                scroll-padding-bottom: calc(150px + env(safe-area-inset-bottom, 0px));
            }
            #app #tab-trading::before { inset: 0; background-size: 34px 34px; }
            #app #tab-trading .page-heading { padding-bottom: 10px; }
            #app #tab-trading .page-heading h1 { font-size: 29px; }
            #app #tab-trading .control-bar { padding: 7px; gap: 8px; }
            #app #tab-trading #trading-ai-activity,
            #app #tab-trading #core-strategy-card,
            #app #tab-trading #meme-sniper-card { padding: 15px; }
            #app #tab-trading .stat-box { padding: 9px; }
            #app #tab-trading .stat-val { font-size: 14px; }
            #app #tab-trading details#trading-activity-panel > summary { padding: 15px; }
            #app #tab-trading #trading-activity { padding: 10px; }
        }
        @media (max-width: 380px) {
            #app #tab-trading .control-bar { flex-wrap: wrap; }
            #app #tab-trading .control-bar .btn-toggle-off,
            #app #tab-trading .control-bar .btn-refresh { flex: 1 1 100%; }
            #app #tab-trading .grid-stats { grid-template-columns: repeat(2, minmax(0, 1fr)); }
            #app #tab-trading #user-bal-small { max-width: 100%; font-size: 11px !important; }
        }
        @media (prefers-reduced-motion: reduce) {
            #app #tab-trading *,
            #app #tab-trading *::before,
            #app #tab-trading *::after {
                scroll-behavior: auto !important;
                animation: none !important;
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
                <button class="support-quick-link" type="button" onclick="openSupport()">Contact technical support</button>
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
        <label class="signup-consent"><input id="account-risk" type="checkbox" required><span>I understand trading can lose money and returns are not guaranteed.</span></label>
        <div id="affiliate-terms" hidden>
            <p class="signup-terms-heading">Required for this referred account</p>
            <label class="signup-consent"><input id="account-partner-terms" type="checkbox" required><span>I understand this account was referred. Up to 1.75% of realized trading profit may be paid to its referral partners: 1% / 0.5% / 0.25% by level, after prior trading losses are recovered. Profit means sale proceeds minus recorded purchase cost; separately paid network fees are not netted here. Deposits and open trades are excluded; USDC and SOL are accounted for separately.</span></label>
        </div>
        <button id="account-create" class="btn-green-main" onclick="createAccount()">Create account</button>
        <p id="account-error" class="status-output" role="alert" aria-live="assertive"></p>
        <button class="support-quick-link" type="button" onclick="openSupport()">Contact technical support</button>
    </section>
    <div id="support-overlay" class="support-overlay" hidden>
        <section class="support-dialog" role="dialog" aria-modal="true" aria-labelledby="support-title">
            <div class="support-dialog-head">
                <h2 id="support-title">Contact technical support</h2>
                <button class="support-close" type="button" aria-label="Close support form" onclick="closeSupport()">×</button>
            </div>
            <p class="section-note">Your message and optional screenshot will be sent privately to the administrator's Telegram chat. Your verified Telegram ID and username are included so the issue can be identified.</p>
            <p class="muted">Do not send passwords, security codes, recovery codes, private keys, or seed phrases.</p>
            <label class="settings-label" for="support-message">Describe the issue · up to 700 characters</label>
            <textarea id="support-message" maxlength="700" placeholder="What happened, and what did you expect?"></textarea>
            <label class="settings-label" for="support-photo">Screenshot · optional</label>
            <input id="support-photo" type="file" accept=".jpg,.jpeg,.png,image/jpeg,image/png">
            <p class="muted">One JPG or PNG image, up to 5 MB. Images are sent directly and are not stored by the app.</p>
            <button id="support-submit" class="btn-green-main" type="button" onclick="sendSupportRequest()">Send to support</button>
            <p id="support-status" class="support-status" role="status" aria-live="polite"></p>
        </section>
    </div>
    <dialog id="trade-share-dialog" aria-modal="true" aria-labelledby="trade-share-title">
        <div class="trade-share-content">
            <div class="trade-share-head">
                <div>
                    <p class="trade-share-kicker">ZER0LIFE · AI TRADER</p>
                    <h2 id="trade-share-title">Share a completed trade</h2>
                </div>
                <button class="trade-share-close" type="button" aria-label="Close share dialog" onclick="closeTradeShareDialog()">×</button>
            </div>
            <canvas id="trade-share-canvas" width="1080" height="1350" aria-label="Share card preview"></canvas>
            <p class="trade-share-note">Only the token symbol and finalized realized result are shared. No wallet address or transaction ID is included.</p>
            <p class="trade-share-targets">Instagram · Telegram · Facebook · X (Twitter)<br>The app never posts automatically. You choose the destination in the device share menu.</p>
            <label class="trade-share-confirm" for="trade-share-confirmation">
                <input id="trade-share-confirmation" type="checkbox">
                <span>I reviewed this card and confirm sharing.</span>
            </label>
            <label class="trade-share-caption-label" for="trade-share-caption">Caption</label>
            <textarea id="trade-share-caption" readonly></textarea>
            <div class="trade-share-actions">
                <button id="trade-share-submit" type="button" disabled>Open device share menu</button>
                <button id="trade-share-copy" type="button" disabled>Copy caption</button>
                <button id="trade-share-download" type="button" disabled>Download card</button>
                <button id="trade-share-cancel" type="button" onclick="closeTradeShareDialog()">Cancel</button>
            </div>
            <p id="trade-share-status" role="status" aria-live="polite"></p>
        </div>
    </dialog>
    <div id="app" data-ui-version="console-ai-v1" style="display: none;">
        <div id="tab-terminal" class="tab-panel active">
            <header class="page-heading"><div><p class="eyebrow">AI Trader · Personal account</p><h1>Overview</h1></div></header>
            <button id="btn-ai-crypto-investors" type="button" class="btn-green-main" onclick="gotoTab('ai-crypto-investors', null)">Ai Crypto Investors</button>
            <div class="banner-limit account-summary" style="border-color:#ef4444; background:#2a1014; margin-bottom:12px;">
                <div style="color:#4ade80; font-size:13px; font-weight:800;">Your account · USDC / SOL</div>
                <div id="wallet-runtime-status" style="color:#cbd5e1; font-size:11px; margin-top:4px;">Personal funds. Trading carries risk.</div>
                <div id="account-pending-operation-details" role="status" style="display:none;color:#cbd5e1;font-size:11px;margin-top:8px;overflow-wrap:anywhere;white-space:pre-wrap;"></div>
            </div>
            <div class="control-bar">
                <button id="btn-toggle" type="button" class="btn-ctl btn-toggle-off">
                    <span id="toggle-dot">●</span>
                    <span id="toggle-text">Autopilot: OFF</span>
                </button>
                <button id="btn-refresh-top" class="btn-ctl btn-refresh" onclick="manualRefresh()">
                    <span id="refresh-icon" aria-hidden="true">↻</span>
                    <span>Refresh</span>
                </button>
            </div>
            <div id="trading-toggle-status" role="status" aria-live="polite" hidden
                 style="margin:8px 2px 12px;padding:10px 12px;border:1px solid rgba(148,163,184,.25);border-radius:10px;color:#cbd5e1;font-size:12px;white-space:pre-wrap;"></div>

            <button class="btn-sell-all" onclick="closeAllPositions()">
                <span>Close all my positions</span>
            </button>

            <div class="card overview-funds" style="border: 1px solid rgba(34, 197, 94, 0.3); background: linear-gradient(135deg, #0e111a 0%, #0a150d 100%);">
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
                <p class="section-note trading-compact-description">Automatic exits: −5% stop; −2% trail after +3.5%. Manual sales available.</p>
                <details class="trading-description-disclosure">
                    <summary>Exit rules &amp; risk</summary>
                    <p class="section-note">Automatic exits only: −5% stop; after +3.5%, exit on a 2% pullback from the peak. AI SELL and resistance exits are disabled. Manual sales remain available. Thresholds are measured in this position's funding currency, not the USD chart; fills and profit are not guaranteed.</p>
                </details>
                <div id="discovery-status" class="section-note" role="status" aria-live="polite">Candidate scan pending.</div>
                <button class="action-btn" id="scan-candidates" onclick="scanCandidates()">Scan candidates</button>
                <details><summary>Candidate shortlist</summary><div id="candidate-list" class="section-note" style="white-space:pre-line">No verified shortlist yet.</div></details>
                <details><summary>Market discovery · DexScreener + charts</summary><div id="whale-signal-status" class="section-note" style="white-space:pre-line">Scheduled market scanning replaces the Helius whale stream. No whale event is required.</div></details>
                <p id="sniper-pnl" class="section-note">Realized SOL PnL: loading...</p>
            </div>

            <div class="card" id="core-strategy-card">
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
                    <div class="stat-box"><div class="stat-label">Profit trail</div><div class="stat-val" style="color: #4ade80;">+3.50% / −2.00%</div></div>
                    <div class="stat-box"><div class="stat-label">Stop loss</div><div class="stat-val" style="color: #f87171;">−5.00%</div></div>
                </div>
                <p class="section-note trading-compact-description">Entry-cost cap: 2.50% of the order; sell exits are not blocked.</p>
                <details class="trading-description-disclosure">
                    <summary>Entry-cost details</summary>
                    <p class="section-note">Core entry cost budget: 2.50% of the order. Includes quoted DEX fees, price impact, slippage, estimated network fees, Jito tips and account rent for entry/exit. Estimates do not guarantee profit or exit costs. SELL exits are not blocked by this filter.</p>
                </details>
                <p id="entry-cost-check" class="section-note" aria-live="polite">No entry cost check yet.</p>
            </div>

            <div class="card" id="activity-log-card">
                <div style="font-size: 12px; font-weight: 700; color: #94a3b8; margin-bottom: 6px;">ACTIVITY LOG</div>
                <div style="font-size:11px;color:#94a3b8;margin-bottom:8px;">AI decisions are not confirmed trades · times in your timezone</div>
                <div id="log-box" class="log-console" data-activity-version="decision-history-v1">Loading activity...</div>
            </div>
        </div>

        <div id="tab-stats" class="tab-panel">
            <div class="card" id="stats-summary">
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
                <div style="display:grid;grid-template-columns:1fr;gap:10px;margin-top:14px;">
                    <section aria-label="USDC closed trade profit and loss" style="padding:12px;border:1px solid #263329;border-radius:14px;background:#090e0b;">
                        <div style="font-size:11px;font-weight:800;letter-spacing:.08em;color:#d8e4d7;margin-bottom:9px;">USDC · CLOSED TRADES</div>
                        <div style="display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;">
                            <div class="stat-box"><div class="stat-label">Profits</div><div id="pnl-usdc-gains" class="stat-val" style="color:#4ade80;">+$0.00</div></div>
                            <div class="stat-box"><div class="stat-label">Losses</div><div id="pnl-usdc-losses" class="stat-val" style="color:#f87171;">−$0.00</div></div>
                            <div class="stat-box"><div class="stat-label">Net</div><div id="pnl-usdc-net" class="stat-val">+$0.00</div></div>
                        </div>
                        <div class="section-note" style="margin-top:8px;">Wins / losses / even: <span id="pnl-usdc-record">0 / 0 / 0</span> · Partner commissions included: <span id="pnl-usdc-commission">$0.000000</span></div>
                    </section>
                    <section aria-label="SOL closed trade profit and loss" style="padding:12px;border:1px solid #263329;border-radius:14px;background:#090e0b;">
                        <div style="font-size:11px;font-weight:800;letter-spacing:.08em;color:#d8e4d7;margin-bottom:9px;">SOL · CLOSED TRADES</div>
                        <div style="display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;">
                            <div class="stat-box"><div class="stat-label">Profits</div><div id="pnl-sol-gains" class="stat-val" style="color:#4ade80;">+0.000000000</div></div>
                            <div class="stat-box"><div class="stat-label">Losses</div><div id="pnl-sol-losses" class="stat-val" style="color:#f87171;">−0.000000000</div></div>
                            <div class="stat-box"><div class="stat-label">Net</div><div id="pnl-sol-net" class="stat-val">+0.000000000</div></div>
                        </div>
                        <div class="section-note" style="margin-top:8px;">Wins / losses / even: <span id="pnl-sol-record">0 / 0 / 0</span> · Partner commissions included: <span id="pnl-sol-commission">0.000000000 SOL</span></div>
                    </section>
                </div>
                <div class="section-note" style="margin-top:12px;">Closed trades only. Profit and loss figures are after partner commissions; separately paid network fees and Jito tips are excluded. SOL is kept separate from USDC.</div>
            </div>

            <div class="card" id="stats-history">
                <div style="font-size: 13px; font-weight: 800; color: #f8fafc; margin-bottom: 12px;">YOUR TRADES</div>
                <div id="history-box" style="color: #64748b; font-size: 11px; text-align: center; padding: 20px;">Loading trades...</div>
            </div>
        </div>

        <div id="tab-exchange" class="tab-panel" aria-labelledby="exchange-title">
            <header class="page-heading"><div><p class="eyebrow">Cross-chain · Personal ledger</p><h1 id="exchange-title">Exchange</h1></div></header>
            <section class="exchange-hero" aria-label="USDC exchange between Solana and Base">
                <div class="exchange-heading">
                    <div><p class="exchange-kicker">USDC · SOLANA ↔ BASE</p><h1 id="exchange-direction-title">Solana → Base</h1></div>
                    <span class="exchange-status" id="exchange-status">QUOTE · CONFIRM TO EXCHANGE</span>
                </div>
                <div class="exchange-direction" role="group" aria-label="Choose quote direction">
                    <button id="exchange-dir-solana-base" type="button" aria-pressed="true" onclick="setExchangeDirection('solana-to-base')">Solana → Base</button>
                    <button id="exchange-dir-base-solana" type="button" aria-pressed="false" onclick="setExchangeDirection('base-to-solana')">Base → Solana</button>
                </div>
            </section>
            <section class="exchange-quote-panel" aria-label="Quote request">
                <div class="exchange-swap-card">
                    <label class="exchange-swap-label" for="exchange-amount">You pay</label>
                    <div class="exchange-swap-main">
                        <input id="exchange-amount" class="exchange-quote-input" type="number" inputmode="decimal"
                            min="0.000001" max="1000000" step="0.000001" value="10" autocomplete="off"
                            oninput="clearExchangeQuote()">
                        <span class="exchange-token-pill"><span class="exchange-token-mark">U</span>USDC</span>
                    </div>
                    <div class="exchange-swap-meta"><span id="exchange-source-chain">Solana · USDC</span><span id="exchange-source-available">Available: —</span></div>
                </div>
                <div class="exchange-swap-divider" aria-hidden="true"><span>⇅</span></div>
                <div class="exchange-swap-card">
                    <span class="exchange-swap-label">You receive · estimate</span>
                    <div class="exchange-swap-main">
                        <strong id="exchange-preview-out" class="exchange-swap-output">—</strong>
                        <span class="exchange-token-pill"><span class="exchange-token-mark">U</span>USDC</span>
                    </div>
                    <div class="exchange-swap-meta"><span id="exchange-destination-chain">Base · account balance</span><span>After finality</span></div>
                </div>
                <div class="exchange-percentages" role="group" aria-label="Choose a percentage of available balance">
                    <button type="button" data-exchange-percent="25" onclick="setExchangePercent(25)" disabled>25%</button>
                    <button type="button" data-exchange-percent="50" onclick="setExchangePercent(50)" disabled>50%</button>
                    <button type="button" data-exchange-percent="100" onclick="setExchangePercent(100)" disabled>100%</button>
                </div>
                <div class="exchange-quote-form">
                    <button id="exchange-quote-button" class="exchange-quote-button" type="button" onclick="requestExchangeQuote()">Get quote</button>
                </div>
                <p id="exchange-quote-status" class="exchange-quote-status" role="status" aria-live="polite"></p>
                <button type="button" class="btn-secondary" onclick="refreshExchangeTransfers()">Check transfer status</button>
                <div id="exchange-transfer-proof" class="exchange-transfer-proof" aria-live="polite" hidden></div>
                <section id="exchange-quote-result" class="exchange-quote-result" aria-label="Read-only quote result" hidden>
                    <div class="exchange-quote-grid">
                        <div class="exchange-quote-value"><span>Send amount</span><strong id="exchange-quote-in">—</strong></div>
                        <div class="exchange-quote-value"><span>Minimum receive</span><strong id="exchange-quote-min">—</strong></div>
                    </div>
                    <p class="exchange-quote-footnote"><span id="exchange-quote-provider"></span> · <span id="exchange-quote-expiry"></span></p>
                    <p class="exchange-fee-total">Estimated costs: <strong id="exchange-quote-total-fees">—</strong></p>
                    <details class="exchange-cost-details">
                        <summary>Fee breakdown</summary>
                        <ul id="exchange-quote-fees" class="exchange-quote-fees"></ul>
                    </details>
                    <details class="exchange-cost-details">
                        <summary>Route details</summary>
                        <p id="exchange-quote-warning" class="exchange-quote-footnote"></p>
                        <p id="exchange-quote-route-details" class="exchange-quote-footnote"></p>
                    </details>
                    <p class="exchange-confirm-note">Review the estimate and minimum receive. Press “Exchange” to confirm this transaction; the bot handles routing and status checks.</p>
                    <div id="exchange-live-actions"></div>
                </section>
                <p class="exchange-disabled-note">Unresolved transfers stay reserved. Never submit an unresolved transfer again.</p>
            </section>
            <aside class="exchange-safety">Each transfer is confirmed separately. Your account is credited only after the destination transaction is verified as final.</aside>
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
                        <h3>Pay SOL with Solana Pay</h3>
                        <div class="solpay-note">Solana Pay is available for native SOL deposits. For USDC, use the standard transfer and transaction verification below.</div>
                        <label id="solpay-amount-label" for="solpay-amount">Amount in SOL</label>
                        <input id="solpay-amount" class="sig-input" type="number" inputmode="decimal"
                            min="0.000000001" step="0.000000001" value="0.1" autocomplete="off">
                        <div id="solpay-presets" class="token-selector solpay-presets" role="group" aria-label="Quick amount choices"></div>
                        <button class="btn-verify" id="solpay-create" type="button" onclick="createSolanaPayInvoice()">Create payment request</button>
                        <p id="solpay-status" class="solpay-status" role="status" aria-live="polite">Enter the SOL amount to create a payment request.</p>
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

                    <details class="solpay-fallback" open>
                        <summary>Deposit USDC by standard transfer and verify the transaction</summary>
                        <div class="solpay-fallback-content">
                            <p class="solpay-fallback-note">For USDC deposits, verify your wallet once, copy the address below, send USDC on Solana mainnet, then submit the transaction signature. The bot credits your balance only after it verifies the finalized transfer. SOL can also be deposited by standard transfer.</p>
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

            <div class="card" id="monthly-trading-fee-card" style="background:#0b0d14;border-color:#243023;">
                <div style="font-size:13px;font-weight:800;color:#fff;margin-bottom:8px;">Monthly trading fee</div>
                <div id="monthly-trading-fee-status" role="status" aria-live="polite"
                    style="font-size:12px;color:#94a3b8;">Loading fee status…</div>
                <div id="monthly-trading-fee-consent" hidden>
                    <div id="monthly-trading-fee-quote" style="font-size:12px;color:#d9f99d;margin-top:10px;">
                        Current SOL amount: checking…
                    </div>
                    <div style="font-size:11px;line-height:1.55;color:#94a3b8;margin-top:9px;">
                        This is an additional $5 USD-equivalent fee, separate from your access package.
                        It is charged in SOL from your in-app balance immediately after you agree if you have
                        already deposited, then every 30 days. If you agree before your first verified deposit,
                        the first charge is taken after that deposit. The SOL amount is calculated from a fresh
                        SOL/USD quote. The fee is an internal transfer to the administrator's in-app SOL balance;
                        no on-chain transfer is sent. Cancel any time to stop future charges. A paid period remains
                        active until its end. If a due fee cannot be paid, new buys pause; sells and withdrawals remain available.
                    </div>
                    <label style="display:flex;gap:9px;align-items:flex-start;font-size:11px;line-height:1.45;color:#e2e8f0;margin-top:11px;">
                        <input id="monthly-trading-fee-agree" type="checkbox" style="margin-top:2px;">
                        <span>I agree to this recurring fee and authorize the in-app SOL balance transfer described above.</span>
                    </label>
                    <button class="btn-verify" id="monthly-trading-fee-accept" type="button"
                        onclick="acceptMonthlyTradingFee()" disabled style="margin-top:10px;">Accept and activate</button>
                </div>
                <button class="btn-copy" id="monthly-trading-fee-cancel" type="button"
                    onclick="cancelMonthlyTradingFee()" hidden style="margin-top:10px;">
                    Cancel future monthly charges
                </button>
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
                <div style="margin-top:14px;padding:12px;border:1px solid #35402d;border-radius:12px;">
                    <h3 style="margin-top:0;">Owner shortfall adjustment</h3>
                    <p class="muted">This permanently reduces only your available bot balance by the exact finalized USDC/SOL shortfall. It sends no transaction and does not debit customer balances. Pending operations or any other asset shortfall block the adjustment.</p>
                    <div id="owner-shortfall-preview" class="status-output" role="status">Run the read-only wallet coverage check first.</div>
                    <label style="display:flex;gap:10px;align-items:flex-start;font-size:12px;"><input id="owner-shortfall-confirm" type="checkbox" style="width:auto;flex:0 0 auto;">I confirm the exact shortfall is being absorbed from my own available balance, not customer funds.</label>
                    <button id="owner-shortfall-apply" class="btn-copy" onclick="applyOwnerShortfall()" disabled>Apply exact shortfall to my balance</button>
                    <div id="owner-shortfall-result" class="status-output" role="status"></div>
                </div>
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
            <div class="card" id="trading-ai-activity"><h3 style="margin-top:0">Trading signals</h3>
                <p id="ai-status" class="status-output">Checking signal status...</p>
                <details class="trading-analysis-disclosure">
                    <summary>Scan timing &amp; costs</summary>
                    <p id="ai-latency" class="section-note">Signal timings: awaiting a fresh market scan.</p>
                </details>
                <button class="btn-copy" onclick="previewAI()">Check rule signals · no trade</button>
                <details class="trading-analysis-disclosure">
                    <summary>AI decisions &amp; risk checks</summary>
                    <p id="ai-decisions" class="status-output"></p>
                    <p id="ai-risk-review" class="status-output">Risk controller: awaiting analysis.</p>
                </details>
                <details class="trading-analysis-disclosure">
                    <summary>Outcome history &amp; chart levels</summary>
                    <p id="ai-learning" class="section-note">Outcome memory: awaiting finalized trade history.</p>
                    <p id="ai-chart-zones" class="status-output"></p>
                </details>
                <details>
                    <summary>Independent execution guard</summary>
                    <p class="section-note">The rules engine proposes BUY / HOLD only. Server-owned checks decide whether an order is allowed. No model can change limits or disable checks.</p>
                    <p id="execution-policy-summary" class="section-note">Policy loading...</p>
                    <p id="execution-policy-result" class="status-output">No execution check recorded yet.</p>
                    <p class="section-note">Liquidity is checked through the executable route and price impact at the requested size, not a promise of market depth. Entry cost estimates are not profit forecasts. Entry-efficiency checks never block owned-position exits or withdrawals. A passed check is not proof of a filled trade.</p>
                </details>
            </div>
            <div id="trading-strategy"></div>
            <div id="trading-sniper"></div>
            <div class="card trading-readiness-card">
                <button class="btn-copy" onclick="checkTradingReadiness()">Check readiness · no trade</button>
                <div id="trading-readiness" class="status-output">Trading involves risk. No guaranteed returns.</div>
            </div>
            <details id="trading-activity-panel"><summary>Logs &amp; position controls</summary><div id="trading-activity"></div></details>
        </div>
        <div id="tab-tariffs" class="tab-panel">
            <header class="page-heading"><div><p class="eyebrow">ACCESS PACKAGES</p><h1>Tariffs</h1></div></header>
            <div class="card">
                <h3 style="margin-top:0">AI Trader access</h3>
                <p class="section-note">Access packages do not deposit funds into your trading balance or change custody admission. Trading modes are not started by a payment.</p>
                <section id="telegram-tariff-view" hidden>
                    <p id="telegram-tariff-status" role="status" aria-live="polite">Checking access…</p>
                    <p id="telegram-tariff-countdown" class="section-note" aria-live="polite"></p>
                    <button id="telegram-trial-start" class="btn-green-main" type="button" hidden>
                        Start one-time 3-day free trial
                    </button>
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
            <div class="card" id="language-settings">
                <h3 style="margin-top:0">Interface language</h3>
                <p class="section-note">English is the default. Your choice is saved to your account and follows you across devices.</p>
                <label class="settings-label" for="ui-language-select">Choose a language</label>
                <select id="ui-language-select" class="sig-input" aria-label="Choose a language">
                    <option value="en">English</option>
                    <option value="ru">Русский</option>
                    <option value="uk">Українська</option>
                </select>
                <button class="btn-green-main" type="button" onclick="saveUiLanguage()">Save language</button>
                <p id="ui-language-status" class="section-note" role="status" aria-live="polite"></p>
            </div>
            <div class="card" id="support-card">
                <h3 style="margin-top:0">Technical support</h3>
                <p class="section-note">Send a private message and an optional screenshot directly to the administrator.</p>
                <button class="btn-copy" type="button" onclick="openSupport()">Contact support</button>
            </div>
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
            <div class="card" id="settings-order-card"><h3 style="margin-top:0">Order sizes</h3>
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
                <p class="section-note">Core / USDC pairs: SOL, JUP, RAY, PYTH, JTO, ORCA, KMNO, NOS, SHDW, PEPE, OTC; up to 5 USDC-funded positions. Sniper / SOL seeds: WIF, BONK, BOME, POPCAT, MEW, PENGU, PEPE, OTC; up to 5 SOL-funded positions, limited by your available SOL and saved order size. OTC / USDC and OTC / SOL use the verified OTC Token-2022 mint; other Token-2022 assets are not admitted. ORCA currently retains mint authority (additional tokens may be issued); no token is risk-free. Every 15 minutes a bounded DexScreener profiles/search scan checks up to 200 Solana mints, verifies up to 40 and sends at most 8 current candidates plus held assets to AI. This is not every project or a profit forecast; profiles alone do not establish meme status.</p>
                <p class="section-note">Core and Sniper BUYs require fresh closed 5-minute/1-hour candles and either a confirmed support bounce or a bounded rising-trend continuation. Continuation requires one closed bullish 5-minute candle closing above the previous candle, a rising hourly trend, aligned EMAs, RSI 45–68, nonnegative MACD, relative volume of at least 0.8× and limited extension with room below known resistance. The previous candle need not be bullish. Breakout chasing is disabled. Resistance is analysis context only; it cannot trigger a sale. AI SELL is blocked in both modes. Automatic exits use only a 5% stop and a 2% peak-pullback trail activated after +3.5%, measured in the position's funding currency (USDC or SOL). Reaching +3.5% alone does not sell. Manual sales remain available. Chart zones are estimates, fills can slip, and this strategy is not backtested or a profit guarantee.</p>
                <p class="section-note">Confirmed buy, sell, take-profit, and stop-loss alerts are sent to each participant's own Telegram chat. Enable Telegram notifications on your phone. Alerts are sent after Solana finalizes the trade; a safely failed stop-loss sale gets a separate alert saying the position remains open.</p>
                <p class="section-note">Sniper entry filters: approved DEX, at least $10k pool liquidity, $100k 24h volume, 200 daily trades, pool age 24h+, extreme-pump exclusion, standard SPL mints with no mint/freeze authority, or the exact approved OTC Token-2022 mint with immutable metadata and disabled mint/freeze authorities. Other Token-2022 assets are rejected. The shortlist rotates every 15 minutes through up to 40 on-chain-verified candidates; AI sees up to 8 at a time. After any completed close, all participants are blocked from re-buying that mint for 24 hours. Fresh eligibility is required for every buy. Scores rank liquidity/activity, not expected profit. Holder concentration, LP locks and every contract/market risk are not fully checked. Mint checks do not guarantee token safety.</p>
                <p class="section-note">AI makes decisions after you enable a mode. Orders require personal funds, permitted assets, at least 5 price samples, a fresh decision and stable pricing. Only one shared-wallet operation can be pending at a time.</p>
                <p class="section-note">Both modes use a 5% stop below entry. After a position reaches +3.5%, a trailing exit triggers after a 2% pullback from its highest tracked price. Exits are checked approximately every 20 seconds independently of AI availability. Model failures stop new AI orders. After a server restart both modes pause and need manual reactivation.</p>
                <p class="section-note">Stops and trailing exits are execution conditions, not guaranteed fill prices. A 5% stop can close a position, but fast price moves, fees, slippage and liquidity can make the realized loss larger. Returns are not guaranteed.</p>
            </details>
            <details><summary>Diagnostics</summary><p id="worker-status" class="status-output">Loading...</p></details>
            <details><summary>AI connection &amp; costs</summary>
                <p class="section-note">Set OPENAI_API_KEY only in Render Environment. Never enter secrets in chat or app fields. OPENAI_MODEL defaults to gpt-4.1; model availability and billing depend on the OpenAI account.</p>
                <p class="section-note">Default: one analysis every 15 minutes, up to 96 attempted API calls per UTC day across the service, including failed calls. OpenAI API billing is separate and paid by the operator. Only market prices are sent to the model, not keys, Telegram identities or personal balances.</p>
            </details>
            <details id="operator-settings" style="display:none;"><summary>Operator tools · fees &amp; migration</summary><div id="settings-admin"></div></details>
        </div>
        <div id="tab-ai-crypto-investors" class="tab-panel">
            <header class="page-heading">
                <div><p class="eyebrow">AI Trader · Independent strategy</p><h1>AI Crypto Investors</h1></div>
            </header>
            <section id="base-live-readiness-panel" class="card base-live-readiness" aria-label="Base live trading readiness" style="display:none">
                <div class="base-live-readiness-head">
                    <h2>Base real trading</h2>
                    <span class="base-live-off-badge">LIVE TRADING · OFF</span>
                </div>
                <p class="base-live-readiness-note">
                    This checks the Base trading wallet. Real trades remain off until all checks pass and you enable them.
                </p>
                <span id="base-live-wallet-address" class="base-live-address"></span>
                <div class="base-paper-metrics">
                    <div class="base-paper-metric">
                        <span>WALLET USDC</span>
                        <strong id="base-live-usdc">—</strong>
                        <small>Base trading wallet</small>
                    </div>
                    <div class="base-paper-metric">
                        <span>GAS RESERVE</span>
                        <strong id="base-live-eth">—</strong>
                        <small>Native ETH on Base</small>
                    </div>
                    <div class="base-paper-metric">
                        <span>NETWORK</span>
                        <strong id="base-live-network">Not checked</strong>
                        <small>Expected chain ID 8453</small>
                    </div>
                </div>
                <div class="base-live-summary" id="base-live-summary" role="status" aria-live="polite">
                    Base live trading is off.
                </div>
                <div class="base-live-controls">
                    <label class="base-live-budget" for="base-live-budget-input">
                        Live budget · USDC
                        <input id="base-live-budget-input" type="number" min="0" max="10000" step="0.01" value="0" inputmode="decimal">
                    </label>
                    <button id="base-live-save-budget" class="btn-copy base-live-save" type="button" onclick="saveBaseLiveBudget()">Save cap</button>
                    <button id="base-live-toggle" class="btn-copy base-live-toggle" type="button" onclick="toggleBaseLive()" disabled>
                        Live trading · OFF
                    </button>
                </div>
                <details class="base-live-fold">
                    <summary>Open real positions <span id="base-live-position-count">0</span></summary>
                    <div id="base-live-positions" class="base-live-items">No open Base positions.</div>
                </details>
                <details class="base-live-fold">
                    <summary>Recent Base transactions</summary>
                    <div id="base-live-operations" class="base-live-items">No Base transactions.</div>
                </details>
                <details class="base-paper-fold">
                    <summary>Setup checks</summary>
                    <ul id="base-live-checks" class="base-live-checks"></ul>
                </details>
                <button id="base-live-check-button" class="btn-copy base-live-check-button" type="button" onclick="checkBaseLiveReadiness()">
                    Check separate wallet
                </button>
            </section>
            <section id="base-paper-panel" class="card base-paper-card" aria-label="Base network paper simulation" hidden style="display:none">
                <div class="base-paper-head">
                    <div class="base-network-identity">
                        <span class="base-network-mark" aria-hidden="true"><span>B</span></span>
                        <div>
                            <p class="base-network-kicker">BASE NETWORK · L2</p>
                            <h2>Paper Investors</h2>
                        </div>
                    </div>
                    <span class="base-paper-badge"><span class="base-paper-dot"></span>SIM ONLY · LIVE OFF</span>
                </div>
                <p class="base-paper-caption">Manual scan · No Base wallet or real transactions</p>
                <div class="base-paper-metrics">
                    <div class="base-paper-metric">
                        <span>CAP / FREE</span>
                        <strong id="base-paper-budget">$100.00</strong>
                        <small><span id="base-paper-free">$100.00</span> available</small>
                    </div>
                    <div class="base-paper-metric">
                        <span>OPEN POSITIONS</span>
                        <strong id="base-paper-open-count">0</strong>
                        <small><span id="base-paper-open-cost">$0.00</span> open cost</small>
                    </div>
                    <div class="base-paper-metric">
                        <span>REALIZED · GROSS</span>
                        <strong id="base-paper-pnl">$0.00</strong>
                        <small><span id="base-paper-closed-count">0</span> closed · fees excluded</small>
                    </div>
                </div>
                <div class="base-paper-controls">
                    <label class="base-paper-budget-field" for="base-paper-budget-input">
                        <span>Virtual cap · USD</span>
                        <input id="base-paper-budget-input" class="sig-input" type="number" min="0" max="100000" step="1" value="100">
                    </label>
                    <button id="base-paper-save-budget" class="btn-copy base-paper-save" type="button" onclick="saveBasePaperBudget()">Save cap</button>
                    <button id="base-paper-scan" class="btn-green-main base-paper-scan" type="button" onclick="runBasePaperScan()">
                        <span aria-hidden="true">↻</span> Scan Base
                    </button>
                </div>
                <div id="base-paper-status" class="base-paper-status section-note" role="status" aria-live="polite">
                    Ready · scan manually to update the simulation.
                </div>
                <div class="base-paper-details">
                    <details class="base-paper-fold">
                        <summary>Open paper positions</summary>
                        <div id="base-paper-open-positions" class="base-paper-list" aria-live="polite">No open paper positions.</div>
                    </details>
                    <details class="base-paper-fold">
                        <summary>Screened candidates <span class="base-paper-fold-count" id="base-paper-candidate-count">0</span></summary>
                        <div id="base-paper-candidates" class="base-paper-list">No scan results.</div>
                    </details>
                    <details class="base-paper-fold">
                        <summary>Closed tranches &amp; results</summary>
                        <div id="base-paper-closed-trades" class="base-paper-list">No simulated exits.</div>
                        <small id="base-paper-pnl-note" class="base-paper-disclaimer">
                            Gross price-only simulation. Gas, DEX fees, routing, price impact and failed fills are excluded;
                            results are not a profit forecast.
                        </small>
                    </details>
                    <details class="base-paper-fold">
                        <summary>Strategy &amp; filters</summary>
                        <p class="base-paper-rules">
                            Shared virtual USD cap across Base USDC and WETH · up to five positions · three equal entry tranches.
                            Entries require fresh closed 5m/1h candles. Profit exits: +3.5%, +7%, then +10% activation with a
                            5% pullback trail. No stop-loss. Pools need at least $10,000 liquidity, $100,000 24h volume,
                            200 daily transactions, and 24h age. Only exact Base USDC/WETH quote contracts are accepted.
                        </p>
                    </details>
                </div>
            </section>
            <section class="card solana-investor-card" aria-label="Solana network AI Crypto Investors">
                <div class="base-paper-head">
                    <div class="base-network-identity">
                        <span class="solana-network-mark" aria-hidden="true"><span><i></i><i></i><i></i></span></span>
                        <div>
                            <p class="solana-network-kicker">SOLANA NETWORK · MAINNET</p>
                            <h2>Solana Investors</h2>
                        </div>
                    </div>
                    <span id="investor-mode-status" class="solana-investor-badge" aria-live="polite">AUTOPILOT · OFF</span>
                </div>
                <p class="solana-investor-caption">USDC and SOL · screened discovery universe · separate real-money strategy</p>
                <div class="solana-investor-metrics">
                    <div class="solana-investor-metric"><span>CANDIDATES SCREENED</span><strong id="investor-candidate-count">Loading…</strong></div>
                    <div class="solana-investor-metric"><span>OPEN POSITIONS</span><strong id="investor-open-count">0</strong></div>
                    <div class="solana-investor-metric"><span>REALIZED · USDC</span><strong id="investor-pnl-usdc">0.00</strong></div>
                    <div class="solana-investor-metric"><span>REALIZED · SOL</span><strong id="investor-pnl-sol">0.000000000</strong></div>
                </div>
                <div class="solana-investor-controls">
                    <button id="investor-toggle" class="btn-ctl btn-toggle-off solana-investor-toggle" type="button" onclick="toggleInvestorTrading()">Autopilot: OFF · Turn on</button>
                </div>
                <p id="investor-toggle-status" class="solana-investor-help" role="status" aria-live="polite" hidden></p>
                <p class="solana-investor-compact-description">Candidates are screened before entry; not every candidate is traded.</p>
                <details class="base-paper-fold solana-investor-description-disclosure">
                    <summary>Candidate screening details</summary>
                    <p class="solana-investor-help">
                    Screened Solana candidates pass mint, liquidity, chart, quote and execution checks. Discovery checks up to 200 new mints and 40 on-chain candidates per scan; not every token is traded. Core and Meme Sniper remain separate.
                    </p>
                </details>
                <div class="base-paper-details">
                    <details class="solana-investor-fold">
                        <summary>Allocated strategy budget</summary>
                        <p class="solana-investor-help">
                            Set one shared total for all AI Crypto Investors positions in each currency. The amount is reserved
                            internally from your available bot balance; no on-chain transfer is made. Core, Meme Sniper and
                            withdrawals cannot spend the reserved amount. Lower the allocation to release unused funds; the
                            cost basis of open Investor positions cannot be released.
                        </p>
                        <p class="solana-investor-help">
                            This is a hard cap. Profits above it return to your available balance. Trading losses reduce the
                            reserved pool. Saving the same amount tops the pool back up from your available balance if funds
                            allow; lowering it releases unused cash.
                        </p>
                        <div class="solana-investor-budget-grid">
                            <div class="solana-investor-budget-cell"><span>Budget limit · USDC</span><strong id="investor-budget-usdc-summary">0.000000</strong></div>
                            <div class="solana-investor-budget-cell"><span>Free in Investor pool · USDC</span><strong id="investor-budget-usdc-free">0.000000</strong></div>
                            <div class="solana-investor-budget-cell"><span>Open-position cost · USDC</span><strong id="investor-budget-usdc-invested">0.000000</strong></div>
                            <div class="solana-investor-budget-cell"><span>Budget limit · SOL</span><strong id="investor-budget-sol-summary">0.000000000</strong></div>
                            <div class="solana-investor-budget-cell"><span>Free in Investor pool · SOL</span><strong id="investor-budget-sol-free">0.000000000</strong></div>
                            <div class="solana-investor-budget-cell"><span>Open-position cost · SOL</span><strong id="investor-budget-sol-invested">0.000000000</strong></div>
                        </div>
                        <div class="solana-investor-budget-form">
                            <div><label class="settings-label" for="investor-budget-usdc">Type total budget · USDC</label><input id="investor-budget-usdc" class="sig-input" type="number" min="0" step="0.000001" value="0"></div>
                            <div><label class="settings-label" for="investor-budget-sol">Type total budget · SOL</label><input id="investor-budget-sol" class="sig-input" type="number" min="0" step="0.000000001" value="0"></div>
                            <button class="btn-green-main solana-investor-budget-save" type="button" onclick="saveInvestorBudget()">Save Investor budget</button>
                        </div>
                        <div id="investor-budget-status" class="solana-investor-help" role="status" aria-live="polite"></div>
                    </details>
                    <details class="solana-investor-fold">
                        <summary>Strategy &amp; filters</summary>
                        <p class="solana-investor-rules">
                            Entries require a fresh closed green 5m candle and a non-falling 1h chart. Later tranches require
                            the price to rise above the prior entry; no averaging down.
                            The next tranche requires continued upward confirmation. Divide each currency's manually entered budget into five token slots per currency, with three equal tranches per slot. Sell tranche one at +3.5% and tranche two at +7%; activate the final tranche's profit trail after +10%, then exit after a 5% pullback from its peak. Automatic exits are blocked unless the quote's minimum return is above entry cost; a sudden price gap can leave the position open, and a manual sale can realize a loss. This strategy has no stop-loss; the allocation may lose most or all of its value. Targets use estimates; fills, returns and a profitable exit are not guaranteed. Manual sales remain available.
                        </p>
                        <p class="solana-investor-rules">
                            The engine starts only after you press ON and pass readiness checks. It is OFF after every server restart. If it is OFF, open positions remain open and can still be sold manually. Results are recorded per funding currency and exclude separately paid operator/network fees.
                        </p>
                    </details>
                    <details class="solana-investor-fold">
                        <summary>Open investor positions <span class="solana-investor-fold-count" id="investor-open-count-fold">0</span></summary>
                        <div id="investor-open-positions" class="solana-investor-list" aria-live="polite">No AI Crypto Investors positions recorded.</div>
                    </details>
                    <details class="solana-investor-fold">
                        <summary>Closed trades and realized results</summary>
                        <div id="investor-closed-trades" class="solana-investor-list" aria-live="polite">No AI Crypto Investors trades recorded.</div>
                    </details>
                </div>
            </section>
        </div>
    </div>

    <div id="nav" class="bottom-nav" style="display: none;">
        <button class="nav-btn active" onclick="gotoTab('terminal', this)"><span>◈</span>Overview</button>
        <button class="nav-btn" onclick="gotoTab('trading', this)"><span>↗</span>Trading</button>
        <button class="nav-btn" onclick="gotoTab('deposit', this)"><span>⇄</span>Wallet</button>
        <button class="nav-btn" onclick="gotoTab('exchange', this)"><span>⇆</span>Exchange</button>
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
        function tradeAuditText(value) {
            return String(value ?? '').replace(/[&<>"']/g, ch =>
                ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
        }
        function renderTradeAudit(trade) {
            const labels = {
                AI_BUY:'AI entry with recorded analysis', MANUAL_BUY:'Manual purchase',
                MANUAL_SELL:'Manual sale', STOP_LOSS:'Protective stop loss',
                TRAILING_STOP:'Activated peak trailing stop',
                LEGACY_AI_SELL:'Legacy AI sale', RESISTANCE_TARGET:'Legacy resistance exit',
                TAKE_PROFIT:'Legacy take-profit exit', UNKNOWN:'Cause not established'
            };
            const reviews = trade.trade_review || {};
            const phases = trade.status === 'CLOSED' ? ['BUY','SELL'] : ['BUY'];
            return `<details style="margin-top:12px;line-height:1.6;overflow-wrap:anywhere">
                <summary>Trade evidence &amp; second-agent review</summary>
                ${phases.map(kind => {
                    const row = reviews[kind];
                    if (!row) return `<p>${kind}: No audit recorded for this historical trade.</p>`;
                    const facts = row.facts || {};
                    const trigger = facts.trigger || {};
                    const snap = trigger.snapshot || {};
                    const report = row.report;
                    const state = row.state === 'REVIEWED' ? 'Review complete'
                        : row.state === 'WAITING_BUDGET' ? 'Waiting for shared AI budget'
                        : row.state === 'UNAVAILABLE' ? 'AI review unavailable; evidence retained'
                        : row.state === 'RUNNING' ? 'AI review in progress' : 'AI review queued';
                    const price = value => Number.isFinite(value) ? Number(value).toPrecision(8) : 'Not recorded';
                    const triggerNumbers = Object.keys(snap).length
                        ? `<div>Trigger price: ${price(snap.observed_price)} ${tradeAuditText(facts.funding_currency)}
                            · Peak: ${price(snap.peak_price)}
                            · Entry: ${price(snap.entry_price)}</div>
                            <div>Observed return: ${Number.isFinite(snap.observed_return_pct) ? snap.observed_return_pct.toFixed(3)+'%' : 'Not recorded'}
                            · Peak return: ${Number.isFinite(snap.peak_return_pct) ? snap.peak_return_pct.toFixed(3)+'%' : 'Not recorded'}
                            · Pullback: ${Number.isFinite(snap.pullback_from_peak_pct) ? snap.pullback_from_peak_pct.toFixed(3)+'%' : 'Not recorded'}</div>
                            <div>Trigger time: ${Number.isFinite(snap.observed_at) ? tradeAuditText(new Date(snap.observed_at*1000).toLocaleString(uiLocale())) : 'Not recorded'}
                            · Policy: ${tradeAuditText(snap.policy_version || 'Not recorded')}</div>` : '';
                    return `<div style="border-top:1px solid #25332a;padding:12px 0">
                        <strong>${kind} · Recorded facts</strong>
                        <div>${tradeAuditText(labels[trigger.source] || labels.UNKNOWN)}</div>
                        ${trigger.reason ? '<div>Entry reason: '+tradeAuditText(trigger.reason)+'</div>' : ''}
                        ${trigger.risk_reason ? '<div>Risk/trend check: '+tradeAuditText(trigger.risk_reason)+'</div>' : ''}
                        ${trigger.source === 'AI_BUY' ? '<div>Bound risk approval: '
                            +(trigger.risk_approved === true ? 'YES' : 'Not verified')
                            +' · Trend: '+tradeAuditText(trigger.risk_trend)+'</div>' : ''}
                        ${triggerNumbers}
                        <div>Executed price: ${price(facts.fill_price)} ${tradeAuditText(facts.funding_currency)}</div>
                        <div>Finalized: ${tradeAuditText(new Date(row.finalized_at*1000).toLocaleString(uiLocale()))}</div>
                        <div>Second agent: ${tradeAuditText(state)}</div>
                        ${row.reviewed_at ? '<div>Reviewed: '+tradeAuditText(new Date(row.reviewed_at*1000).toLocaleString(uiLocale()))+'</div>' : ''}
                        ${row.last_error ? '<div>'+tradeAuditText(row.last_error)+'</div>' : ''}
                        ${report ? '<div><strong>AI interpretation — not execution evidence</strong></div><div>'
                            +tradeAuditText(report.assessment)+'</div><div>Lesson: '
                            +tradeAuditText(report.lesson)+'</div><div>Evidence: '
                            +tradeAuditText(report.evidence_quality)+' · '+tradeAuditText(row.model)+'</div>' : ''}
                        <small>${tradeAuditText(facts.limitations)}</small>
                    </div>`;
                }).join('')}
                <small>Retrospective analysis only. It cannot place trades, delay exits or change risk settings. This is not model training or a profit guarantee.</small>
            </details>`;
        }
        function renderInvestorDashboard(stats) {
            const basePanel=document.getElementById('base-paper-panel');
            if(basePanel){basePanel.hidden=true;basePanel.style.display='none';}
            const baseLivePanel=document.getElementById('base-live-readiness-panel');
            if(baseLivePanel)baseLivePanel.style.display=stats.is_admin?'':'none';
            if(stats.is_admin&&!baseLiveReadinessLoaded)checkBaseLiveReadiness();
            const investorTrades=(stats.trades||[]).filter(trade=>trade.strategy==='investor');
            const open=investorTrades.filter(trade=>trade.status==='OPEN'||trade.status==='RESERVED');
            const closed=investorTrades.filter(trade=>trade.status==='CLOSED')
                .sort((a,b)=>String(b.closed_at||'').localeCompare(String(a.closed_at||'')));
            const solPnl=closed.filter(t=>t.funding_currency==='SOL')
                .reduce((sum,t)=>sum+Number(t.profit_units||0),0);
            const usdcPnl=closed.filter(t=>t.funding_currency==='USDC')
                .reduce((sum,t)=>sum+Number(t.profit_units||0),0);
            const fmt=(value,digits=6)=>Number.isFinite(Number(value))
                ?Number(value).toFixed(digits):'—';
            investorActive=!!stats.investor_active;
            document.getElementById('investor-mode-status').textContent=
                localizeUiText(investorActive?'AUTOPILOT · ON':'AUTOPILOT · OFF');
            const investorToggle=document.getElementById('investor-toggle');
            investorToggle.textContent=localizeUiText(
                investorActive?'Autopilot: ON · Turn off':'Autopilot: OFF · Turn on');
            investorToggle.className='btn-ctl '+(investorActive?'btn-toggle-on':'btn-toggle-off')+' solana-investor-toggle';
            document.getElementById('investor-budget-usdc').disabled=investorActive;
            document.getElementById('investor-budget-sol').disabled=investorActive;
            document.getElementById('investor-open-count').textContent=String(open.length);
            document.getElementById('investor-open-count-fold').textContent=String(open.length);
            document.getElementById('investor-pnl-usdc').textContent=fmt(usdcPnl,2)+' USDC';
            document.getElementById('investor-pnl-sol').textContent=fmt(solPnl,9)+' SOL';
            const budget=stats.investor_budget||{};
            const usdcBudget=budget.usdc||{};
            const solBudget=budget.sol||{};
            document.getElementById('investor-budget-usdc-summary').textContent=
                fmt(usdcBudget.allocated,6)+' USDC';
            document.getElementById('investor-budget-usdc-free').textContent=
                fmt(usdcBudget.free,6)+' USDC';
            document.getElementById('investor-budget-sol-summary').textContent=
                fmt(solBudget.allocated,9)+' SOL';
            document.getElementById('investor-budget-sol-free').textContent=
                fmt(solBudget.free,9)+' SOL';
            document.getElementById('investor-budget-usdc-invested').textContent=
                fmt(usdcBudget.invested_cost,6)+' USDC';
            document.getElementById('investor-budget-sol-invested').textContent=
                fmt(solBudget.invested_cost,9)+' SOL';
            if(!investorBudgetDirty){
                const usdcInput=document.getElementById('investor-budget-usdc');
                const solInput=document.getElementById('investor-budget-sol');
                if(document.activeElement!==usdcInput)usdcInput.value=fmt(usdcBudget.allocated,6);
                if(document.activeElement!==solInput)solInput.value=fmt(solBudget.allocated,9);
            }
            const discovery=(stats.ai_status||{}).discovery||{};
            document.getElementById('investor-candidate-count').textContent=
                (Number(discovery.eligible||0))+' eligible / '+(Number(discovery.scanned||0))+' scanned';
            document.getElementById('investor-open-positions').textContent=open.length
                ?open.map(t=>(t.token_symbol||t.symbol)+' · '+fmt(t.amount_tokens,4)+' tokens · '
                    +t.funding_currency+(t.entry_step?' · entry step '+t.entry_step:'')
                    +' · entry '+fmt(t.buy_price,9)+' · buy reason: '
                    +(t.buy_reason||'no saved AI reason')
                    +(t.buy_review?' · independent review: '+t.buy_review:'')
                    +' · '+(t.timestamp||'time unavailable')).join('\n')
                :'No AI Crypto Investors positions recorded.';
            document.getElementById('investor-closed-trades').textContent=closed.length
                ?closed.slice(0,20).map(t=>(t.token_symbol||t.symbol)+' · '+t.funding_currency+' · '
                    +fmt(t.profit_units,t.funding_currency==='SOL'?9:4)+' '+t.funding_currency
                    +' ('+fmt(t.profit_pct,2)+'%)'
                    +(t.entry_step?' · entry step '+t.entry_step:'')
                    +(t.exit_step?' · exit step '+t.exit_step:'')
                    +(t.buy_reason?' · buy reason: '+t.buy_reason:'')
                    +(t.exit_reason?' · '+t.exit_reason:'')
                    +(t.sell_review?' · post-trade review: '+t.sell_review:'')
                    +' · '+(t.closed_at||'time unavailable')).join('\n')
                :'No AI Crypto Investors sales recorded.';
        }
        let basePaperLoading=false,basePaperBusy=false,latestBasePaperData=null;
        let baseLiveReadinessBusy=false,baseLiveReadinessLoaded=false;
        function basePaperMoney(value){
            const parsed=Number(value);
            const locale=uiLocale();
            return Number.isFinite(parsed)
                ?new Intl.NumberFormat(locale,{style:'currency',currency:'USD',
                    minimumFractionDigits:2,maximumFractionDigits:2}).format(parsed)
                :'—';
        }
        function localizeBasePaperText(value){
            const source=String(value??'');
            const completed=source.match(/^Paper scan complete: (\d+) simulated buys, (\d+) simulated exits\. No live transactions\.$/);
            if(completed&&currentUiLanguage==='uk')
                return `Симуляцію завершено. Віртуальні покупки: ${completed[1]}; віртуальні виходи: ${completed[2]}. Реальних транзакцій не було.`;
            if(completed&&currentUiLanguage==='ru')
                return `Симуляция завершена. Виртуальные покупки: ${completed[1]}; виртуальные выходы: ${completed[2]}. Реальных транзакций не было.`;
            const rateLimit=source.match(/^Base market data is rate-limited; retry in (\d+)s\.$/);
            if(rateLimit&&currentUiLanguage==='uk')
                return `Запити до ринкових даних Base тимчасово обмежені. Спробуйте ще раз через ${rateLimit[1]} с.`;
            if(rateLimit&&currentUiLanguage==='ru')
                return `Запросы рыночных данных Base временно ограничены. Повторите попытку через ${rateLimit[1]} с.`;
            return localizeUiText(source);
        }
        function renderBasePaper(data){
            if(!data||!data.success)return;
            latestBasePaperData=data;
            const budgetInput=document.getElementById('base-paper-budget-input');
            const openPositions=data.open_positions||[];
            const candidates=data.candidates||[];
            const closedTrades=data.closed_trades||[];
            document.getElementById('base-paper-budget').textContent=basePaperMoney(data.budget_usd);
            document.getElementById('base-paper-open-cost').textContent=basePaperMoney(data.open_cost_usd);
            document.getElementById('base-paper-free').textContent=basePaperMoney(data.free_budget_usd);
            document.getElementById('base-paper-pnl').textContent=basePaperMoney(data.realized_gross_pnl_usd);
            document.getElementById('base-paper-open-count').textContent=String(openPositions.length);
            document.getElementById('base-paper-closed-count').textContent=String(closedTrades.length);
            document.getElementById('base-paper-candidate-count').textContent=String(candidates.length);
            if(document.activeElement!==budgetInput)budgetInput.value=String(Number(data.budget_usd||0));
            const locale=uiLocale();
            const scanned=data.scanned_at
                ?new Date(Number(data.scanned_at)*1000).toLocaleString(locale)
                :localizeBasePaperText('Not scanned');
            document.getElementById('base-paper-status').textContent=
                localizeBasePaperText(data.message)+'\n'+localizeBasePaperText('Last scan: ')+scanned+
                '\n'+localizeBasePaperText('Paper only · live transactions: ')+
                localizeBasePaperText(data.live_trading?'unexpectedly enabled':'OFF')+
                '';
            if(data.pnl_note)document.getElementById('base-paper-pnl-note').textContent=localizeBasePaperText(data.pnl_note);
            document.getElementById('base-paper-candidates').textContent=candidates.length
                ?candidates.map(c=>{
                    const change='5m '+Number(c.change_m5_pct||0).toLocaleString(locale,{minimumFractionDigits:2,maximumFractionDigits:2})
                        +'% / 1h '+Number(c.change_h1_pct||0).toLocaleString(locale,{minimumFractionDigits:2,maximumFractionDigits:2})+'%';
                    const reason=c.simulated_action||c.signal_reason||((c.rejection_reasons||[]).join(' ')||'No entry signal');
                    return localizeBasePaperText((c.symbol||'TOKEN')+' / '+(c.quote_asset||'—')+' · '
                        +Number(c.price_usd||0).toLocaleString(locale,{maximumSignificantDigits:6})
                        +' · liquidity '+basePaperMoney(c.liquidity_usd)+' · volume '+basePaperMoney(c.volume_24h_usd)
                        +' · '+change+'\n'+reason);
                }).join('\n\n')
                :localizeBasePaperText('No eligible Base quote pools returned by the last scan.');
            document.getElementById('base-paper-open-positions').textContent=openPositions.length
                ?openPositions.map(p=>{
                    const pnl=p.unrealized_gross_pnl_usd==null?'price unavailable':basePaperMoney(p.unrealized_gross_pnl_usd)+' gross';
                    const stale=p.price_stale_seconds==null?'':p.price_stale_seconds>120?' · mark may be stale':'';
                    return localizeBasePaperText(p.symbol+' · tranche '+p.entry_step+'/3 · '+basePaperMoney(p.amount_usd)+' · '+p.quote_asset
                        +' · entry $'+Number(p.entry_price_usd||0).toLocaleString(locale,{maximumSignificantDigits:6})+' · current '+pnl+stale);
                }).join('\n')
                :localizeBasePaperText('No open paper positions.');
            document.getElementById('base-paper-closed-trades').textContent=closedTrades.length
                ?closedTrades.map(p=>localizeBasePaperText(p.symbol+' · tranche '+p.entry_step+'/3 · '+basePaperMoney(p.profit_usd)
                    +' gross · '+(p.exit_reason||'exit')+' · '+new Date(Number(p.closed_at||0)*1000).toLocaleString(locale)))
                    .join('\n')
                :localizeBasePaperText('No simulated exits.');
        }
        async function loadBasePaper(){
            if(basePaperLoading||!window.latestAccountStats?.is_admin)return;
            basePaperLoading=true;
            try{
                const response=await apiFetch('/api/base-paper',{cache:'no-store'});
                const data=await response.json();
                if(!response.ok||!data.success)throw Error(data.message||'Could not load Base paper simulation.');
                renderBasePaper(data);
            }catch(error){
                document.getElementById('base-paper-status').textContent=localizeBasePaperText(error.message||'Base paper simulation is unavailable.');
            }finally{basePaperLoading=false;}
        }
        async function saveBasePaperBudget(){
            const button=document.getElementById('base-paper-save-budget');
            const amount=Number(document.getElementById('base-paper-budget-input').value);
            if(!Number.isFinite(amount)||amount<0||amount>100000){
                document.getElementById('base-paper-status').textContent=localizeBasePaperText('Enter a virtual budget from $0 to $100,000.');
                return;
            }
            if(amount>0&&amount<15){
                document.getElementById('base-paper-status').textContent=localizeBasePaperText('Use $0 only after positions close, or at least $15 for 15 virtual tranches.');
                return;
            }
            button.disabled=true;
            try{
                const response=await apiFetch('/api/base-paper/budget',{method:'POST',
                    headers:{'Content-Type':'application/json'},body:JSON.stringify({budget_usd:amount})});
                const data=await response.json();
                if(!response.ok||!data.success)throw Error(data.message||'Could not save virtual cap.');
                renderBasePaper(data);
            }catch(error){
                document.getElementById('base-paper-status').textContent=localizeBasePaperText(error.message||'Could not save virtual cap.');
            }finally{button.disabled=false;}
        }
        async function runBasePaperScan(){
            if(basePaperBusy)return;
            const button=document.getElementById('base-paper-scan');
            basePaperBusy=true;button.disabled=true;
            document.getElementById('base-paper-status').textContent=
                localizeBasePaperText('Scanning up to four Base pools and closed candles. This may take up to a minute; no live order is sent.');
            try{
                const response=await apiFetch('/api/base-paper/scan',{method:'POST'});
                const data=await response.json();
                if(!response.ok||!data.success)throw Error(data.message||'Base paper scan failed.');
                renderBasePaper(data);
            }catch(error){
                document.getElementById('base-paper-status').textContent=localizeBasePaperText(error.message||'Base paper scan failed; no simulated action was recorded.');
            }finally{basePaperBusy=false;button.disabled=false;}
        }
        function renderBaseLive(data){
            document.getElementById('base-live-wallet-address').textContent=data.wallet_address||'';
            document.getElementById('base-live-usdc').textContent=
                data.usdc_balance==null?'—':Number(data.usdc_balance).toLocaleString(uiLocale(),{minimumFractionDigits:2,maximumFractionDigits:6})+' USDC';
            document.getElementById('base-live-eth').textContent=
                data.eth_balance==null?'—':Number(data.eth_balance).toLocaleString(uiLocale(),{minimumFractionDigits:6,maximumFractionDigits:8})+' ETH';
            document.getElementById('base-live-network').textContent=
                data.chain_id==null?localizeUiText('Not checked'):('8453'===String(data.chain_id)?'Base · 8453':'Wrong · '+String(data.chain_id));
            const budget=document.getElementById('base-live-budget-input');
            if(budget&&document.activeElement!==budget)budget.value=Number(data.budget_usdc||0).toFixed(2);
            const enabled=Boolean(data.enabled);
            const badge=document.querySelector('#base-live-readiness-panel .base-live-off-badge');
            const toggle=document.getElementById('base-live-toggle');
            if(badge)badge.textContent=localizeUiText(enabled?'LIVE TRADING · ON':'LIVE TRADING · OFF');
            if(toggle){
                toggle.textContent=localizeUiText(enabled?'Disable Base trading':'Enable Base trading');
                toggle.classList.toggle('is-on',enabled);
                // Keep the control reachable when checks fail so a tap can show
                // the exact blocker instead of looking like a dead button.
                toggle.disabled=false;
            }
            const status=data.status&&data.status.state&&!['OFF','READY'].includes(data.status.state)
                ?data.status.message:data.message;
            document.getElementById('base-live-summary').textContent=localizeUiText(status||'Base live trading is off.');
            document.getElementById('base-live-checks').textContent=(data.checks||[])
                .map(check=>(check.ok?'✓ ':'× ')+localizeUiText(check.name))
                .join('\n');
            const positions=Array.isArray(data.open_positions)?data.open_positions:[];
            document.getElementById('base-live-position-count').textContent=String(positions.length);
            document.getElementById('base-live-positions').textContent=positions.length
                ?positions.map(position=>`${position.symbol} · tranche ${position.entry_step} · entry ${Number(position.entry_price_usdc).toPrecision(6)} USDC · ${((Number(position.cost_usdc_raw)||0)/1e6).toFixed(2)} USDC`).join('\n')
                :localizeUiText('No open Base positions.');
            const operations=Array.isArray(data.recent_operations)?data.recent_operations:[];
            const history=document.getElementById('base-live-operations');
            history.replaceChildren();
            if(!operations.length){
                history.textContent=localizeUiText('No Base transactions.');
            }else{
                for(const operation of operations){
                    const row=document.createElement('div');
                    row.className='base-live-operation';
                    const label=document.createElement('span');
                    label.textContent=`${operation.kind} · ${operation.state}`;
                    row.appendChild(label);
                    if(/^0x[0-9a-fA-F]{64}$/.test(operation.tx_hash||'')){
                        const link=document.createElement('a');
                        link.href='https://basescan.org/tx/'+operation.tx_hash;
                        link.target='_blank';
                        link.rel='noopener noreferrer';
                        link.textContent=operation.tx_hash.slice(0,10)+'…';
                        row.appendChild(link);
                    }
                    history.appendChild(row);
                }
            }
        }
        async function checkBaseLiveReadiness(){
            if(baseLiveReadinessBusy||!window.latestAccountStats?.is_admin)return;
            const button=document.getElementById('base-live-check-button');
            const toggle=document.getElementById('base-live-toggle');
            baseLiveReadinessBusy=true;
            if(button)button.disabled=true;
            if(toggle)toggle.disabled=true;
            try{
                const response=await apiFetch('/api/base-live/readiness',{cache:'no-store'});
                const data=await response.json();
                if(!response.ok||!data.success)throw Error(data.message||'Base wallet check failed.');
                renderBaseLive(data);
                return data;
            }catch(error){
                document.getElementById('base-live-summary').textContent=
                    localizeUiText(error.message||'Base wallet check failed; no transaction was sent.');
                return null;
            }finally{
                baseLiveReadinessBusy=false;
                baseLiveReadinessLoaded=true;
                if(button)button.disabled=false;
                if(toggle)toggle.disabled=false;
            }
        }
        async function saveBaseLiveBudget(){
            if(baseLiveReadinessBusy||!window.latestAccountStats?.is_admin)return;
            const button=document.getElementById('base-live-save-budget');
            const amount=Number(document.getElementById('base-live-budget-input').value);
            if(!Number.isFinite(amount)||amount<0||amount>10000){
                document.getElementById('base-live-summary').textContent=localizeUiText('Enter a budget from $0 to $10,000.');
                return;
            }
            baseLiveReadinessBusy=true;
            if(button)button.disabled=true;
            try{
                const response=await apiFetch('/api/base-live/budget',{method:'POST',
                    headers:{'Content-Type':'application/json'},body:JSON.stringify({budget_usdc:amount})});
                const data=await response.json();
                if(!response.ok||!data.success)throw Error(data.message||'Could not save Base live cap.');
                renderBaseLive(data);
            }catch(error){
                document.getElementById('base-live-summary').textContent=localizeUiText(error.message||'Could not save Base live cap.');
            }finally{
                baseLiveReadinessBusy=false;
                if(button)button.disabled=false;
            }
        }
        async function toggleBaseLive(){
            if(baseLiveReadinessBusy||!window.latestAccountStats?.is_admin)return;
            const toggle=document.getElementById('base-live-toggle');
            const enabling=!toggle.classList.contains('is-on');
            if(enabling){
                const readiness=await checkBaseLiveReadiness();
                if(!readiness)return;
                if(!readiness.can_trade){
                    document.getElementById('base-live-summary').textContent=
                        localizeUiText('Base live trading is not ready. See the failed setup checks below. No order was sent.');
                    return;
                }
                if(!window.confirm(localizeUiText('This starts Base trades with real money. Confirm only after reviewing the wallet, cap, and risks.')))return;
            }
            baseLiveReadinessBusy=true;
            toggle.disabled=true;
            let actionError='';
            try{
                const response=await apiFetch('/api/base-live/toggle',{method:'POST',
                    headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:enabling,confirm:enabling})});
                const data=await response.json();
                if(!response.ok||!data.success)throw Error(data.message||'Could not change Base live mode.');
                renderBaseLive(data);
            }catch(error){
                actionError=error.message||'Could not change Base live mode.';
            }finally{
                baseLiveReadinessBusy=false;
                if(actionError){
                    const refreshed=await checkBaseLiveReadiness();
                    document.getElementById('base-live-summary').textContent=
                        localizeUiText(actionError);
                    if(refreshed&&!refreshed.enabled){
                        document.getElementById('base-live-summary').textContent+=' '+
                            localizeUiText('Base remains OFF. Check the setup results before trying again.');
                    }
                }else{
                    toggle.disabled=false;
                }
            }
        }
        const UI_COPY_ROWS = [
            ["Interface language","Язык интерфейса","Мова інтерфейсу"],
            ["Base real trading","Реальная торговля Base","Реальна торгівля Base"],
            ["Base live trading readiness","Готовность к реальной торговле Base","Готовність до реальної торгівлі Base"],
            ["LIVE TRADING · OFF","РЕАЛЬНАЯ ТОРГОВЛЯ · ВЫКЛ","РЕАЛЬНА ТОРГІВЛЯ · ВИМК"],
            ["LIVE TRADING · ON","РЕАЛЬНАЯ ТОРГОВЛЯ · ВКЛ","РЕАЛЬНА ТОРГІВЛЯ · УВІМК"],
            ["Live budget · USDC","Лимит реальной торговли · USDC","Ліміт реальної торгівлі · USDC"],
            ["Live trading · OFF","Реальная торговля · ВЫКЛ","Реальна торгівля · ВИМК"],
            ["Enable Base trading","Включить торговлю Base","Увімкнути торгівлю Base"],
            ["Disable Base trading","Выключить торговлю Base","Вимкнути торгівлю Base"],
            ["This starts Base trades with real money. Confirm only after reviewing the wallet, cap, and risks.","Это запустит сделки Base на реальные деньги. Подтверждай только после проверки кошелька, лимита и рисков.","Це запустить угоди Base за реальні кошти. Підтверджуй лише після перевірки гаманця, ліміту та ризиків."],
            ["Enter a budget from $0 to $10,000.","Введите лимит от $0 до $10 000.","Введіть ліміт від $0 до $10 000."],
            ["Could not save Base live cap.","Не удалось сохранить лимит Base.","Не вдалося зберегти ліміт Base."],
            ["Could not change Base live mode.","Не удалось изменить режим Base.","Не вдалося змінити режим Base."],
            ["Base live trading is off.","Реальная торговля Base выключена.","Реальна торгівля Base вимкнена."],
            ["Base live trading is not ready. See the failed setup checks below. No order was sent.","Реальная торговля Base пока не готова. Смотри проверки настройки ниже. Сделка не отправлена.","Реальна торгівля Base ще не готова. Переглянь перевірки налаштування нижче. Угоду не надіслано."],
            ["Base remains OFF. Check the setup results before trying again.","Торговля Base остаётся выключенной. Проверь результаты настройки перед новой попыткой.","Торгівля Base залишається вимкненою. Перевір результати налаштування перед новою спробою."],
            ["No open Base positions.","Нет открытых позиций Base.","Немає відкритих позицій Base."],
            ["No Base transactions.","Операций Base пока нет.","Операцій Base ще немає."],
            ["Open real positions","Открытые реальные позиции","Відкриті реальні позиції"],
            ["Recent Base transactions","Последние операции Base","Останні операції Base"],
            ["This checks the Base trading wallet. Real trades remain off until all checks pass and you enable them.","Здесь проверяется торговый кошелёк Base. Реальная торговля останется выключенной, пока не пройдут все проверки и ты её не включишь.","Тут перевіряється торговий гаманець Base. Реальна торгівля залишатиметься вимкненою, доки не пройдуть усі перевірки й ти її не ввімкнеш."],
            ["Base trading wallet","Торговый кошелёк Base","Торговий гаманець Base"],
            ["WALLET USDC","USDC НА КОШЕЛЬКЕ","USDC У ГАМАНЦІ"],
            ["GAS RESERVE","РЕЗЕРВ ETH НА ГАЗ","РЕЗЕРВ ETH НА КОМІСІЇ"],
            ["NETWORK","СЕТЬ","МЕРЕЖА"],
            ["Native ETH on Base","Нативный ETH в Base","Нативний ETH у Base"],
            ["Expected chain ID 8453","Ожидаемый ID сети 8453","Очікуваний ID мережі 8453"],
            ["Not checked","Не проверено","Не перевірено"],
            ["No trade can be sent from this panel.","Эта панель не отправляет сделки.","Ця панель не надсилає угоди."],
            ["Setup checks","Проверки настройки","Перевірки налаштування"],
            ["Check separate wallet","Проверить отдельный кошелёк","Перевірити окремий гаманець"],
            ["Base trading wallet address","Адрес торгового кошелька Base","Адреса торгового гаманця Base"],
            ["Wallet role approved","Роль кошелька разрешена","Роль гаманця дозволена"],
            ["Wallet signer matches","Ключ соответствует адресу","Ключ відповідає адресі"],
            ["Base HTTPS RPC","HTTPS RPC Base","HTTPS RPC Base"],
            ["Live executor and recovery","Исполнитель и восстановление транзакций","Виконавець і відновлення транзакцій"],
            ["Persistent Base operation storage","Постоянное хранилище операций Base","Постійне сховище операцій Base"],
            ["Base operation storage is not persistent; trading is paused.","Хранилище операций Base не постоянное; торговля приостановлена.","Сховище операцій Base не є постійним; торгівлю призупинено."],
            ["Base operation storage is not persistent; no transaction was signed or sent.","Хранилище операций Base не постоянное; транзакция не подписана и не отправлена.","Сховище операцій Base не є постійним; транзакцію не підписано й не надіслано."],
            ["Base mainnet (8453)","Base mainnet (8453)","Base mainnet (8453)"],
            ["USDC balance","Баланс USDC","Баланс USDC"],
            ["USDC plus open positions cover cap","USDC и открытые позиции покрывают лимит","USDC і відкриті позиції покривають ліміт"],
            ["Base wallet is a standard address","Кошелёк Base — обычный адрес","Гаманець Base — звичайна адреса"],
            ["ETH gas reserve","Резерв ETH на комиссии","Резерв ETH на комісії"],
            ["Base live executor is ready; set a cap and explicitly enable real trading.","Исполнитель Base готов. Укажи лимит и отдельно включи реальную торговлю.","Виконавець Base готовий. Вкажи ліміт і окремо увімкни реальну торгівлю."],
            ["Base live trading is off; resolve every failed wallet, network, funding, or kill-switch check. No transaction can be sent.","Реальная торговля Base выключена. Исправь проблемы с кошельком, сетью, балансом или серверным переключателем. Сделки не отправляются.","Реальна торгівля Base вимкнена. Виправ проблеми з гаманцем, мережею, балансом або серверним перемикачем. Угоди не надсилаються."],
            ["Base Mainnet is verified. Configure the Base trading wallet and matching signer; live trading remains off.","Сеть Base Mainnet проверена. Настрой торговый кошелёк Base и соответствующий ключ; реальная торговля остаётся выключенной.","Мережу Base Mainnet перевірено. Налаштуй торговий гаманець Base і відповідний ключ; реальна торгівля залишається вимкненою."],
            ["Fund the Base trading wallet or lower its cap to cover the budget.","Пополните торговый кошелёк Base или уменьшите лимит.","Поповни торговий гаманець Base або зменш ліміт."],
            ["Use $0 to pause or at least $15 for 15 equal tranches.","Укажи $0 для паузы или не менее $15 для 15 равных частей.","Вкажи $0 для паузи або не менше $15 для 15 рівних частин."],
            ["Resolve the pending Base operation before changing its cap.","Сначала разреши незавершённую операцию Base, затем меняй лимит.","Спочатку заверши невирішену операцію Base, потім змінюй ліміт."],
            ["A non-zero cap cannot be lower than the open Base position cost.","Ненулевой лимит не может быть ниже стоимости открытых позиций Base.","Ненульовий ліміт не може бути нижчим за вартість відкритих позицій Base."],
            ["Set a Base live budget of at least $15.","Установи лимит реальной торговли Base не менее $15.","Встанови ліміт реальної торгівлі Base не менше $15."],
            ["The Base live budget has no free entry tranche.","В лимите Base не осталось свободной части для входа.","У ліміті Base не залишилося вільної частини для входу."],
            ["The Base trading wallet has insufficient USDC for the next tranche.","В торговом кошельке Base не хватает USDC для следующей части.","У торговому гаманці Base бракує USDC для наступної частини."],
            ["Base market scan is unavailable; no entry was submitted.","Сканирование рынка Base недоступно; вход не отправлен.","Сканування ринку Base недоступне; вхід не надіслано."],
            ["Base market scan returned invalid data.","Сканирование рынка Base вернуло некорректные данные.","Сканування ринку Base повернуло некоректні дані."],
            ["No Base candidate passed every entry check.","Ни один кандидат Base не прошёл все проверки входа.","Жоден кандидат Base не пройшов усі перевірки входу."],
            ["Base exit quote is unavailable; position remains open.","Котировка выхода Base недоступна; позиция остаётся открытой.","Котирування виходу Base недоступне; позиція залишається відкритою."],
            ["A Base exit quote is unavailable; no new entry will be opened.","Котировка выхода Base недоступна; новые позиции не открываются.","Котирування виходу Base недоступне; нові позиції не відкриваються."],
            ["A Base position needs manual review.","Позицию Base нужно проверить вручную.","Позицію Base потрібно перевірити вручну."],
            ["The exact Base allowance is being revoked.","Точное разрешение Base отзывается.","Точний дозвіл Base відкликається."],
            ["Base live trading was turned off.","Реальная торговля Base выключена.","Реальну торгівлю Base вимкнено."],
            ["Base live trading is locked: wallet, funding, executor, or server activation is not ready.","Реальная торговля Base заблокирована: кошелёк, баланс, исполнитель или серверное включение не готовы.","Реальну торгівлю Base заблоковано: гаманець, баланс, виконавець або серверне ввімкнення не готові."],
            ["Base live mode is off; no new buy can be queued.","Режим Base выключен; новая покупка не будет создана.","Режим Base вимкнено; нову купівлю не буде створено."],
            ["The Base buy exceeds the remaining live budget.","Покупка Base превышает оставшийся лимит.","Купівля Base перевищує залишок ліміту."],
            ["Base buy was skipped because the saved cap no longer covers it.","Покупка Base отменена: сохранённого лимита больше недостаточно.","Купівлю Base пропущено: збереженого ліміту більше недостатньо."],
            ["Base buy was skipped because the saved cap no longer covers it; the exact allowance is being revoked.","Покупка Base отменена: лимита не хватает, точное разрешение отзывается.","Купівлю Base пропущено: ліміту недостатньо, точний дозвіл відкликається."],
            ["Base live preflight failed; no new transaction was sent.","Предварительная проверка Base не пройдена; новая транзакция не отправлена.","Попередню перевірку Base не пройдено; нову транзакцію не надіслано."],
            ["Base worker paused after an internal error; no transaction was retried.","Исполнитель Base приостановлен после внутренней ошибки; повторной отправки не было.","Виконавець Base призупинено після внутрішньої помилки; повторного надсилання не було."],
            ["Base entry validation failed; no swap was submitted.","Проверка входа Base не пройдена; обмен не отправлен.","Перевірку входу Base не пройдено; обмін не надіслано."],
            ["The Base trading wallet needs more ETH for gas and reserve.","В торговом кошельке Base нужно больше ETH на комиссии и резерв.","Торговому гаманцю Base потрібно більше ETH на комісії та резерв."],
            ["Base BUY finalized:","Покупка Base подтверждена:","Купівлю Base підтверджено:"],
            ["Base SELL finalized:","Продажа Base подтверждена:","Продаж Base підтверджено:"],
            ["English is the default. Your choice is saved to your account and follows you across devices.","По умолчанию выбран английский. Выбор сохраняется в аккаунте и действует на всех устройствах.","За замовчуванням вибрано англійську. Вибір зберігається в обліковому записі й діє на всіх пристроях."],
            ["Choose a language","Выберите язык","Виберіть мову"],["Save language","Сохранить язык","Зберегти мову"],
            ["Saving language preference…","Сохраняем язык…","Зберігаємо мову…"],
            ["Language preference saved.","Язык сохранён.","Мову збережено."],
            ["Could not save language preference.","Не удалось сохранить язык.","Не вдалося зберегти мову."],
            ["Overview","Обзор","Огляд"],["Trading","Торговля","Торгівля"],["Wallet","Кошелёк","Гаманець"],
            ["Exchange","Обмен","Обмін"],["Settings","Настройки","Налаштування"],["Tariffs","Тарифы","Тарифи"],
            ["Account preferences","Настройки аккаунта","Налаштування облікового запису"],
            ["Your account · USDC / SOL","Ваш аккаунт · USDC / SOL","Ваш обліковий запис · USDC / SOL"],
            ["Personal funds. Trading carries risk.","Личные средства. Торговля связана с риском.","Особисті кошти. Торгівля пов’язана з ризиком."],
            ["Available funds","Доступные средства","Доступні кошти"],["USDC · available","USDC · доступно","USDC · доступно"],
            ["SOL · available","SOL · доступно","SOL · доступно"],["Your realized PnL:","Реализованный PnL:","Реалізований PnL:"],
            ["Realized PnL · USDC","Реализованный PnL · USDC","Реалізований PnL · USDC"],
            ["Realized PnL · SOL","Реализованный PnL · SOL","Реалізований PnL · SOL"],
            ["Closed trades","Закрытые сделки","Закриті угоди"],["Profits","Прибыль","Прибуток"],
            ["Losses","Убытки","Збитки"],["Net","Итого","Разом"],["YOUR TRADES","ВАШИ СДЕЛКИ","ВАШІ УГОДИ"],
            ["Checking scan status…","Проверяем статус сканирования…","Перевіряємо стан сканування…"],
            ["Autopilot: OFF · Turn on","Автопилот: ВЫКЛ · Включить","Автопілот: ВИМК · Увімкнути"],
            ["Autopilot: ON · Turn off","Автопилот: ВКЛ · Выключить","Автопілот: УВІМК · Вимкнути"],
            ["Autopilot: OFF","Автопилот: ВЫКЛ","Автопілот: ВИМК"],["Refresh","Обновить","Оновити"],
            ["Close all my positions","Закрыть все мои позиции","Закрити всі мої позиції"],
            ["Meme Sniper · SOL","Мем-снайпер · SOL","Мем-снайпер · SOL"],
            ["Available SOL","Доступно SOL","Доступно SOL"],["Order size","Размер ордера","Розмір ордера"],
            ["Scan candidates","Сканировать кандидатов","Сканувати кандидатів"],
            ["Candidate shortlist","Список кандидатов","Список кандидатів"],
            ["Candidate checks expired (valid for 30 minutes). The displayed scan counts are historical. Refresh candidates before new buys.","Проверка кандидатов устарела (срок действия — 30 минут). Показанные количества сканирований относятся к прошлым проверкам. Обновите кандидатов перед новыми покупками.","Перевірка кандидатів застаріла (діє 30 хвилин). Показані кількості сканувань стосуються попередніх перевірок. Оновіть кандидатів перед новими купівлями."],
            ["Candidate scan is running. No trade is submitted.","Идёт проверка кандидатов. Сделки не отправляются.","Триває перевірка кандидатів. Угоди не надсилаються."],
            ["A candidate scan is already running.","Проверка кандидатов уже выполняется.","Перевірка кандидатів уже триває."],
            ["A fresh candidate scan has started.","Запущена новая проверка кандидатов.","Розпочато нову перевірку кандидатів."],
            ["A fresh candidate scan could not start.","Не удалось запустить новую проверку кандидатов.","Не вдалося запустити нову перевірку кандидатів."],
            ["Candidate scan is still running. Investors remain OFF; no trade was sent. Check again shortly.","Проверка кандидатов ещё выполняется. Автоторговля AI Crypto Investors остаётся выключенной; сделка не отправлялась. Проверьте ещё раз немного позже.","Перевірка кандидатів ще триває. Автоторгівля AI Crypto Investors залишається вимкненою; угоду не було надіслано. Перевірте ще раз трохи пізніше."],
            ["A fresh candidate scan has started. Investors remain OFF; wait for it to finish, then tap ON again.","Запущена новая проверка кандидатов. Автоторговля AI Crypto Investors остаётся выключенной. Дождитесь завершения проверки и нажмите «Включить» ещё раз.","Розпочато нову перевірку кандидатів. Автоторгівля AI Crypto Investors залишається вимкненою. Дочекайтеся завершення перевірки й натисніть «Увімкнути» ще раз."],
            ["A candidate scan is already running. Investors remain OFF; wait for it to finish, then try again.","Проверка кандидатов уже выполняется. Автоторговля AI Crypto Investors остаётся выключенной. Дождитесь завершения и попробуйте снова.","Перевірка кандидатів уже триває. Автоторгівля AI Crypto Investors залишається вимкненою. Дочекайтеся завершення й спробуйте знову."],
            ["A fresh candidate scan could not start. Investors remain OFF.","Не удалось запустить новую проверку кандидатов. Автоторговля AI Crypto Investors остаётся выключенной.","Не вдалося розпочати нову перевірку кандидатів. Автоторгівля AI Crypto Investors залишається вимкненою."],
            ["No trade was sent.","Сделка не отправлялась.","Угоду не було надіслано."],
            ["No trade was submitted.","Сделка не была отправлена.","Угоду не було надіслано."],
            ["No mode was enabled and no trade was submitted.","Режим не включён, сделка не отправлялась.","Режим не ввімкнено, угоду не було надіслано."],
            ["An unresolved BUY operation is still being checked. Its funds remain locked; do not start another transaction. Investors stayed OFF.","Покупка всё ещё проверяется. Средства остаются заблокированы; не запускайте другую транзакцию. AI Crypto Investors остаётся выключенным.","Купівля ще перевіряється. Кошти залишаються заблокованими; не запускайте іншу транзакцію. AI Crypto Investors залишається вимкненим."],
            ["An unresolved wallet operation is still being checked. Its funds remain locked; do not start another transaction. Investors stayed OFF.","Операция с кошельком ещё проверяется. Средства остаются заблокированы; не запускайте другую транзакцию. AI Crypto Investors остаётся выключенным.","Операція з гаманцем ще перевіряється. Кошти залишаються заблокованими; не запускайте іншу транзакцію. AI Crypto Investors залишається вимкненим."],
            ["Sniper remains OFF; wait for the scan to finish, then try again.","Снайпер остаётся выключенным. Дождитесь завершения сканирования и попробуйте снова.","Снайпер залишається вимкненим. Дочекайтеся завершення сканування й спробуйте знову."],
            ["Candidate checks are current. The next scan is scheduled in ","Проверка кандидатов актуальна. Следующее сканирование запланировано через ","Перевірка кандидатів актуальна. Наступне сканування заплановано через "],
            ["The next scan is scheduled in ","Следующее сканирование запланировано через ","Наступне сканування заплановано через "],
            ["No extra provider request or trade was submitted.","Дополнительный запрос к поставщику не отправлялся, сделка не совершалась.","Додатковий запит до постачальника не надсилався, угоду не було здійснено."],
            ["Candidate checks are unavailable.","Проверка кандидатов недоступна.","Перевірка кандидатів недоступна."],
            ["No candidates currently pass the verified shortlist. Previous scan counts do not guarantee current eligibility.","Сейчас ни один кандидат не прошёл проверку. Количество кандидатов в прошлых сканированиях не подтверждает их текущую пригодность.","Наразі жоден кандидат не пройшов перевірку. Кількість кандидатів у попередніх скануваннях не підтверджує їхню поточну придатність."],
            ["Candidate scan unavailable","Сканирование кандидатов недоступно","Сканування кандидатів недоступне"],
            ["market-data HTTP 429","ограничение источника рыночных данных HTTP 429","обмеження джерела ринкових даних HTTP 429"],
            [". New sniper entries paused; automatic retry scheduled.",". Новые входы Sniper приостановлены; автоматическая повторная попытка запланирована.",". Нові входи Sniper призупинено; автоматичну повторну спробу заплановано."],
            ["DexScreener rate limit (HTTP 429). Shared market-data requests are paused; automatic retry after at least ","Ограничение DexScreener (HTTP 429). Общие запросы рыночных данных приостановлены; автоматическая повторная попытка не ранее чем через ","Обмеження DexScreener (HTTP 429). Спільні запити ринкових даних призупинено; автоматичну повторну спробу буде виконано не раніше ніж через "],
            ["DexScreener rate limit (HTTP 429); shared market-data requests paused for ","Ограничение DexScreener (HTTP 429); общие запросы рыночных данных приостановлены на ","Обмеження DexScreener (HTTP 429); спільні запити ринкових даних призупинено на "],
            ["GeckoTerminal rate limit (HTTP 429). Automatic retry after at least ","Ограничение GeckoTerminal (HTTP 429). Автоматическая повторная попытка не ранее чем через ","Обмеження GeckoTerminal (HTTP 429). Автоматична повторна спроба не раніше ніж через "],
            ["GeckoTerminal market-data HTTP 429","ограничение источника рыночных данных GeckoTerminal HTTP 429","обмеження джерела ринкових даних GeckoTerminal HTTP 429"],
            ["GeckoTerminal backup is active. Fresh pool data, charts, mint checks and quotes remain required; discovery is limited to the provider's current trending pools.","Резервный источник GeckoTerminal активен. Поиск ограничен актуальными трендовыми пулами; для допуска по-прежнему нужны свежие данные пула, графика, mint и котировки.","Резервне джерело GeckoTerminal активне. Пошук обмежений актуальними трендовими пулами; для допуску потрібні свіжі дані пулу, графіка, mint і котирування."],
            ["GeckoTerminal trending pools (DexScreener fallback)","Трендовые пулы GeckoTerminal (резерв вместо DexScreener)","Трендові пули GeckoTerminal (резерв замість DexScreener)"],
            ["DexScreener profiles + meme meta feeds + bounded searches","Профили DexScreener, метаданные мем-токенов и ограниченный поиск","Профілі DexScreener, метадані мем-токенів і обмежений пошук"],
            ["DexScreener profiles + meme/pump search","Профили DexScreener и поиск meme/pump","Профілі DexScreener і пошук meme/pump"],
            ["DexScreener is rate-limited. Fresh GeckoTerminal screening is required before new entries.","Достигнут лимит DexScreener. Перед новыми входами нужна свежая проверка через GeckoTerminal.","Досягнуто ліміт DexScreener. Перед новими входами потрібна свіжа перевірка через GeckoTerminal."],
            ["DexScreener is unavailable. Fresh GeckoTerminal screening is required before new entries.","DexScreener недоступен. Перед новыми входами нужна свежая проверка через GeckoTerminal.","DexScreener недоступний. Перед новими входами потрібна свіжа перевірка через GeckoTerminal."],
            ["Market-data rate limit: retry available in ","Ограничение запросов рыночных данных: повторная попытка будет доступна через ","Обмеження запитів ринкових даних: повторна спроба буде доступна через "],
            ["Market-data rate limit. Retry in ","Ограничение запросов рыночных данных. Повтор через ","Обмеження запитів ринкових даних. Повтор через "],
            [" seconds. No trade was sent."," с. Сделка не отправлялась."," с. Угоду не було надіслано."],
            [" seconds. No mode was enabled and no trade was submitted."," с. Режим не включён, сделка не отправлялась."," с. Режим не ввімкнено, угоду не було надіслано."],
            ["Market-data provider cooldown: ","Пауза поставщика рыночных данных: ","Пауза постачальника ринкових даних: "],
            ["No verified shortlist yet.","Пока нет проверенного списка кандидатов.","Перевіреного списку кандидатів ще немає."],
            ["Market discovery · DexScreener + charts","Поиск рынка · DexScreener и графики","Пошук ринку · DexScreener і графіки"],
            ["Core strategy · USDC","Стратегия Core · USDC","Стратегія Core · USDC"],
            ["AI · BUY / SELL / HOLD","ИИ · ПОКУПКА / ПРОДАЖА / ОЖИДАНИЕ","ШІ · КУПІВЛЯ / ПРОДАЖ / ОЧІКУВАННЯ"],
            ["Slippage","Проскальзывание","Прослизання"],["Profit trail","Трейлинг прибыли","Трейлінг прибутку"],
            ["Stop loss","Стоп-лосс","Стоп-лос"],["ACTIVITY LOG","ЖУРНАЛ АКТИВНОСТИ","ЖУРНАЛ АКТИВНОСТІ"],
            ["Cross-chain · Personal ledger","Межсетевой обмен · личный баланс","Міжмережевий обмін · особистий баланс"],
            ["USDC · MANUAL CROSS-CHAIN TRANSFER","USDC · РУЧНОЙ МЕЖСЕТЕВОЙ ОБМЕН","USDC · РУЧНИЙ МІЖМЕРЕЖЕВИЙ ОБМІН"],
            ["Amount · USDC","Сумма · USDC","Сума · USDC"],["Get live quote","Получить актуальную котировку","Отримати актуальну котировку"],
            ["Quote only · live transfer is not enabled","Только котировка · переводы пока отключены","Лише котировка · перекази поки вимкнено"],
            ["Review a current route and fees below. The server will not sign or broadcast a transaction from this screen yet.","Ниже проверьте маршрут и комиссии. На этом экране сервер пока не подписывает и не отправляет транзакции.","Нижче перевірте маршрут і комісії. На цьому екрані сервер поки не підписує та не надсилає транзакції."],
            ["Use your Solana USDC","Использовать USDC в Solana","Використати USDC у Solana"],
            ["Use your Base USDC","Использовать USDC в Base","Використати USDC у Base"],
            ["Review quote and costs","Проверить котировку и расходы","Перевірити котировку та витрати"],
            ["Credit your Base balance","Зачислить на баланс Base","Зарахувати на баланс Base"],
            ["Credit your Solana balance","Зачислить на баланс Solana","Зарахувати на баланс Solana"],
            ["Pending or unknown?","Операция ожидает подтверждения?","Операція очікує підтвердження?"],
            ["Do not retry a transfer whose status is pending or unknown. Wait for its status to be resolved before taking any action.","Не повторяйте перевод со статусом «ожидает» или «неизвестен». Дождитесь проверки результата.","Не повторюйте переказ зі статусом «очікує» або «невідомо». Дочекайтеся перевірки результату."],
            ["Confirm and send this quote","Подтвердить и отправить обмен","Підтвердити й надіслати обмін"],
            ["Approve exact USDC only","Разрешить списание только указанного USDC","Дозволити списання лише вказаного USDC"],
            ["Deposit","Пополнение","Поповнення"],["Withdraw","Вывод","Виведення"],
            ["Deposit / withdraw","Пополнение / вывод","Поповнення / виведення"],
            ["Pay SOL with Solana Pay","Пополнить SOL через Solana Pay","Поповнити SOL через Solana Pay"],
            ["Create payment request","Создать платёжный запрос","Створити платіжний запит"],
            ["Verify your wallet","Подтвердить кошелёк","Підтвердити гаманець"],
            ["Copy deposit address","Скопировать адрес пополнения","Скопіювати адресу поповнення"],
            ["Verify deposit","Проверить пополнение","Перевірити поповнення"],
            ["Monthly trading fee","Ежемесячная комиссия за торговлю","Щомісячна комісія за торгівлю"],
            ["Cancel future monthly charges","Отменить будущие списания","Скасувати майбутні списання"],
            ["Transactions","Операции","Операції"],["No transactions yet.","Операций пока нет.","Операцій поки немає."],
            ["Access packages","Тарифы доступа","Тарифи доступу"],["AI Trader access","Доступ к AI Trader","Доступ до AI Trader"],
            ["Choose a time-limited package to continue.","Выберите тариф с ограниченным сроком действия.","Виберіть тариф з обмеженим строком дії."],
            ["Technical support","Техническая поддержка","Технічна підтримка"],
            ["Contact support","Связаться с поддержкой","Зв’язатися з підтримкою"],
            ["Account protection","Защита аккаунта","Захист облікового запису"],
            ["Order sizes","Размеры ордеров","Розміри ордерів"],
            ["Saving settings does not start trading.","Сохранение настроек не запускает торговлю.","Збереження налаштувань не запускає торгівлю."],
            ["Partner program","Партнёрская программа","Партнерська програма"],
            ["Your referral link","Ваша реферальная ссылка","Ваше реферальне посилання"],
            ["Copy referral link","Скопировать реферальную ссылку","Скопіювати реферальне посилання"],
            ["Strategy limits & risks","Ограничения и риски стратегии","Обмеження та ризики стратегії"],
            ["Diagnostics","Диагностика","Діагностика"],["AI connection & costs","Подключение ИИ и расходы","Підключення ШІ та витрати"],
             ["AI Trader · Independent strategy","AI Trader · независимая стратегия","AI Trader · незалежна стратегія"],
             ["AI Crypto Investors","AI-криптоинвесторы","AI-криптоінвестори"],
            ["Solana Investors","Solana Investors","Solana Investors"],
            ["SOLANA NETWORK · MAINNET","СЕТЬ SOLANA · MAINNET","МЕРЕЖА SOLANA · MAINNET"],
            ["USDC and SOL · screened discovery universe · separate real-money strategy","USDC и SOL · проверенные рыночные кандидаты · отдельная стратегия с реальными средствами","USDC і SOL · перевірені ринкові кандидати · окрема стратегія з реальними коштами"],
            ["CANDIDATES SCREENED","КАНДИДАТОВ ПРОВЕРЕНО","КАНДИДАТІВ ПЕРЕВІРЕНО"],
            ["REALIZED · USDC","РЕАЛИЗОВАНО · USDC","РЕАЛІЗОВАНО · USDC"],
            ["REALIZED · SOL","РЕАЛИЗОВАНО · SOL","РЕАЛІЗОВАНО · SOL"],
            ["AUTOPILOT · OFF","АВТОПИЛОТ · ВЫКЛ","АВТОПІЛОТ · ВИМК"],
            ["AUTOPILOT · ON","АВТОПИЛОТ · ВКЛ","АВТОПІЛОТ · УВІМК"],
            ["Status check failed:","Не удалось проверить статус:","Не вдалося перевірити статус:"],
            ["Funds remain reserved. Do not submit again.","Средства остаются зарезервированы. Не отправляйте перевод повторно.","Кошти залишаються зарезервованими. Не надсилайте переказ повторно."],
            ["LI.FI has not found this transfer yet. Funds remain reserved. Do not submit again.","LI.FI пока не нашёл этот перевод. Средства остаются зарезервированы. Не отправляйте перевод повторно.","LI.FI поки не знайшов цей переказ. Кошти залишаються зарезервованими. Не надсилайте переказ повторно."],
            ["LI.FI still reports this transfer as pending. Funds remain reserved. Do not submit again.","LI.FI всё ещё показывает перевод в обработке. Средства остаются зарезервированы. Не отправляйте перевод повторно.","LI.FI усе ще показує переказ в обробці. Кошти залишаються зарезервованими. Не надсилайте переказ повторно."],
            ["Bridge status is rate-limited.","Провайдер ограничил частоту проверки статуса.","Провайдер обмежив частоту перевірки статусу."],
            ["Bridge status is unavailable.","Сервис проверки статуса временно недоступен.","Сервіс перевірки статусу тимчасово недоступний."],
            ["Provider status does not match this transfer; funds remain reserved.","Статус провайдера не совпадает с этим переводом; средства остаются зарезервированы.","Статус провайдера не відповідає цьому переказу; кошти залишаються зарезервованими."],
             ["BASE NETWORK · L2","СЕТЬ BASE · L2","МЕРЕЖА BASE · L2"],
             ["Paper Investors","Виртуальный режим инвестирования","Віртуальний трейдинг"],
             ["Manual scan · No Base wallet or real transactions","Ручное сканирование · без Base-кошелька и реальных транзакций","Ручне сканування · без гаманця Base та реальних транзакцій"],
             ["CAP / FREE","ЛИМИТ / ДОСТУПНО","ЛІМІТ / ВІЛЬНО"],
             ["available","доступно","доступно"],["OPEN POSITIONS","ОТКРЫТЫЕ ПОЗИЦИИ","ВІДКРИТІ ПОЗИЦІЇ"],
             ["open cost","стоимость открытых позиций","вкладено у відкриті позиції"],
             ["REALIZED · GROSS","РЕАЛИЗОВАНО · ДО РАСХОДОВ","РЕАЛІЗОВАНО · ДО ВИТРАТ"],
             ["closed · fees excluded","закрыто · комиссии не учтены","закрито · комісії не враховано"],
             ["Virtual cap · USD","Виртуальный лимит · USD","Ліміт симуляції · USD"],
             ["Save cap","Сохранить лимит","Зберегти ліміт"],
             ["Ready · scan manually to update the simulation.","Готово · запустите сканирование вручную, чтобы обновить симуляцию.","Готово · запустіть сканування вручну, щоб оновити симуляцію."],
             ["No scan results.","Результатов сканирования пока нет.","Результатів сканування ще немає."],
             ["Closed tranches & results","Закрытые этапы и результаты","Закриті етапи та результати"],
             ["Gross price-only simulation.","Симуляция учитывает только изменение цены.","Симуляція враховує лише зміну ціни."],
             ["Gas, DEX fees, routing, price impact and failed fills are excluded;","Газ, комиссии DEX и маршрутизации, влияние на цену и неисполненные ордера не учитываются;","Газ, комісії DEX і маршрутизації, вплив на ціну та невиконані ордери не враховано;"],
             ["results are not a profit forecast.","это не прогноз прибыли.","це не прогноз прибутку."],
             ["Strategy & filters","Стратегия и фильтры","Стратегія та фільтри"],
             ["Shared virtual USD cap across Base USDC and WETH · up to five positions · three equal entry tranches.","Общий виртуальный лимит в USD для Base USDC и WETH · до пяти позиций · три равные части для входа.","Спільний віртуальний ліміт у USD для Base USDC і WETH · до п’яти позицій · три рівні частини для входу."],
             ["Entries require fresh closed 5m/1h candles.","Вход возможен только по свежим закрытым свечам 5m/1h.","Вхід можливий лише за свіжими закритими свічками 5m/1h."],
             ["Profit exits: +3.5%, +7%, then +10% activation with a","Фиксация прибыли: +3,5%, +7%, затем активация на +10% с","Фіксація прибутку: +3,5%, +7%, далі активація на +10% із"],
             ["5% pullback trail. No stop-loss.","трейлингом 5% от максимума. Стоп-лосса нет.","трейлінгом 5% від максимуму. Стоп-лосу немає."],
             ["Pools need at least $10,000 liquidity, $100,000 24h volume,","Ликвидность пула должна быть не менее $10 000, объём за 24 часа — $100 000,","Ліквідність пулу має бути не меншою за $10 000, обсяг за 24 години — $100 000,"],
             ["200 daily transactions, and 24h age. Only exact Base USDC/WETH quote contracts are accepted.","не менее 200 транзакций в сутки и возраст пула от 24 часов. Принимаются только точные контракты Base USDC/WETH.","не менш як 200 транзакцій на добу та вік пулу від 24 годин. Дозволені лише точні контракти Base USDC/WETH."],
             ["No entry signal","Нет сигнала на вход","Немає сигналу для входу"],
             ["No eligible Base quote pools returned by the last scan.","Последнее сканирование не нашло подходящих торговых пулов Base.","Під час останнього сканування не знайдено придатних торгових пулів Base."],
             ["No simulated exits.","Виртуальных выходов пока нет.","Віртуальних виходів ще немає."],
             ["Not scanned","Сканирование ещё не запускалось","Сканування ще не запускали"],
             ["Last scan: ","Последнее сканирование: ","Останнє сканування: "],
             ["Paper only · live transactions: ","Только симуляция · реальные транзакции: ","Лише симуляція · реальні транзакції: "],
             ["unexpectedly enabled","НЕОЖИДАННО ВКЛЮЧЕНЫ","НЕОЧІКУВАНО УВІМКНЕНІ"],["OFF","ВЫКЛ","ВИМКНЕНО"],
             ["Base scan completed; no entry or exit trigger passed. No live transactions.","Сканирование Base завершено: условия для входа или выхода не выполнены. Реальных транзакций не было.","Сканування Base завершено: умов для входу чи виходу не виявлено. Реальних транзакцій не було."],
             ["Base scan completed but found no eligible quote pools.","Сканирование Base завершено, но подходящих торговых пулов не найдено.","Сканування Base завершено, але придатних торгових пулів не знайдено."],
             ["Base scan completed","Сканирование Base завершено","Сканування Base завершено"],
             ["simulated buys","виртуальных покупок","віртуальних покупок"],["simulated exits","виртуальных выходов","віртуальних виходів"],
             ["No live transactions.","Реальных транзакций не было.","Реальних транзакцій не було."],
             ["liquidity ","ликвидность ","ліквідність "],["volume ","объём ","обсяг "],
             ["price unavailable","цена недоступна","ціна недоступна"],[" gross"," брутто"," брутто"],
             ["mark may be stale","цена может быть устаревшей","ціна може бути застарілою"],
             ["tranche ","этап входа ","етап входу "],["BUY tranche","ПОКУПКА, этап входа","КУПІВЛЯ, етап входу"],
             ["SELL tranche","ПРОДАЖ, этап входа","ПРОДАЖ, етап входу"],["entry $","вход $","вхід $"],
             ["current ","текущая цена ","поточна ціна "],["No entry signal","Нет сигнала на вход","Немає сигналу для входу"],
             ["Tokenized stocks and ETFs are excluded.","Токенизированные акции и ETF исключены.","Токенізовані акції та ETF не допускаються."],
             ["Valid token or quote price is unavailable.","Нет корректной цены токена или котируемого актива.","Немає коректної ціни токена або котируваного активу."],
             ["Liquidity is below ","Ликвидность ниже ","Ліквідність нижча за "],
             ["24h volume is below ","Объём за 24 часа ниже ","Обсяг за 24 години нижчий за "],
             ["24h transactions are below ","Число транзакций за 24 часа меньше ","Кількість транзакцій за 24 години менша за "],
             ["Pool is newer than 24h or creation time is unavailable.","Пулу меньше 24 часов или время его создания неизвестно.","Пулу менше ніж 24 години або час його створення невідомий."],
             ["Fresh 5m/1h price-change data is unavailable.","Нет свежих данных об изменении цены за 5 минут / 1 час.","Немає свіжих даних про зміну ціни за 5 хвилин / 1 годину."],
             ["Pool data is stale; no paper entry allowed.","Данные пула устарели; виртуальный вход запрещён.","Дані пулу застаріли; віртуальний вхід заборонено."],
             ["Chart data was not requested in this bounded scan.","График не запрашивался в рамках ограниченного сканирования.","Графік не запитували в межах обмеженого сканування."],
             ["Fresh, closed 5m and 1h chart data is unavailable.","Нет свежих данных закрытых свечей 5m и 1h.","Немає свіжих даних закритих свічок 5m та 1h."],
             ["Fresh chart confirmation is required.","Для подтверждения нужен свежий график.","Для підтвердження потрібен свіжий графік."],
             ["All three paper entry tranches have been used.","Все три виртуальных этапа входа использованы.","Усі три віртуальні етапи входу використано."],
             ["Waiting for a new closed 5m candle before another tranche.","Ожидание новой закрытой свечи 5m перед следующим этапом входа.","Очікування нової закритої свічки 5m перед наступним етапом входу."],
             ["Base paper slots are occupied.","Все места для виртуальных позиций Base заняты.","Усі місця для віртуальних позицій Base зайняті."],
             ["Token is in the 24h paper re-entry cooldown.","Для токена действует 24-часовая пауза перед повторным виртуальным входом.","Для токена діє 24-годинна пауза перед повторним віртуальним входом."],
             ["The shared virtual budget has no free tranche.","В общем виртуальном бюджете не осталось свободного этапа входа.","У спільному віртуальному бюджеті не залишилося вільного етапу входу."],
             ["Paper quantity could not be calculated.","Не удалось рассчитать виртуальный объём сделки.","Не вдалося розрахувати віртуальний обсяг угоди."],
             ["No entry or exit trigger passed","Ни одно условие входа или выхода не выполнено","Жодна умова входу чи виходу не виконана"],
             ["Base market data is unavailable.","Рыночные данные Base недоступны.","Ринкові дані Base недоступні."],
             ["Base market data is rate-limited; retry in ","Запросы рыночных данных Base ограничены; повторите через ","Запити ринкових даних Base обмежені; повторіть через "],
             ["Enter a virtual budget from $0 to $100,000.","Введите виртуальный лимит от $0 до $100 000.","Введіть віртуальний ліміт від $0 до $100 000."],
             ["Use $0 only after positions close, or at least $15 for 15 virtual tranches.","Установите $0 только после закрытия позиций или не менее $15 для 15 виртуальных этапов входа.","Встановлюйте $0 лише після закриття позицій або задайте щонайменше $15 для 15 віртуальних етапів входу."],
             ["Could not save virtual cap.","Не удалось сохранить виртуальный лимит.","Не вдалося зберегти віртуальний ліміт."],
             ["Scanning up to four Base pools and closed candles. This may take up to a minute; no live order is sent.","Проверяем до четырёх пулов Base и закрытые свечи. Это может занять до минуты; реальные ордера не отправляются.","Перевіряємо до чотирьох пулів Base і закриті свічки. Це може тривати до хвилини; реальні ордери не надсилаються."],
             ["Base paper scan failed; no simulated action was recorded.","Не удалось просканировать Base; виртуальные операции не записаны.","Не вдалося просканувати Base; віртуальні операції не записано."],
             ["Base paper simulation is unavailable.","Виртуальная симуляция Base недоступна.","Віртуальна симуляція Base недоступна."],
            ["SIM ONLY · LIVE OFF","ТОЛЬКО СИМУЛЯЦИЯ · РЕАЛЬНАЯ ТОРГОВЛЯ ВЫКЛ","ЛИШЕ СИМУЛЯЦІЯ · РЕАЛЬНА ТОРГІВЛЯ ВИМК"],
            ["Manual scan · No Base wallet or real transactions","Ручное сканирование · без кошелька Base и реальных транзакций","Ручне сканування · без гаманця Base та реальних транзакцій"],
            ["Scan Base","Сканировать Base","Сканувати Base"],["Open paper positions","Открытые виртуальные позиции","Відкриті віртуальні позиції"],
            ["No open paper positions.","Открытых виртуальных позиций нет.","Відкритих віртуальних позицій немає."],
            ["Screened candidates","Проверенные кандидаты","Перевірені кандидати"],
            ["Open positions","Открытые позиции","Відкриті позиції"],
            ["Entry and exit rules","Правила входа и выхода","Правила входу та виходу"],
            ["Allocated strategy budget","Выделенный бюджет стратегии","Виділений бюджет стратегії"],
            ["Save Investor budget","Сохранить бюджет Investors","Зберегти бюджет Investors"],
            ["Closed trades and realized results","Закрытые сделки и реализованные результаты","Закриті угоди та реалізовані результати"],
            ["Launch Terminal","Запустить терминал","Запустити термінал"],
            ["Create your account","Создать аккаунт","Створити обліковий запис"],
            ["Loading...","Загрузка…","Завантаження…"],["Loading…","Загрузка…","Завантаження…"],
            ["Pending","Ожидает подтверждения","Очікує підтвердження"],
            ["COMPLETED","ЗАВЕРШЕНО","ЗАВЕРШЕНО"],["FAILED","ОШИБКА","ПОМИЛКА"],
            ["Transfer submitted; destination funds will appear only after verified finality.","Обмен отправлен; средства появятся после подтверждения перевода в сети.","Обмін надіслано; кошти з’являться після підтвердження переказу в мережі."],
            ["Only your available Solana USDC balance can fund this direction. No external Base deposit is needed.","Для этого направления используются только доступные USDC в Solana. Внешнее пополнение Base не требуется.","Для цього напрямку використовуються лише доступні USDC у Solana. Зовнішнє поповнення Base не потрібне."],
            ["Only your available Base USDC balance can fund this direction. No external Solana deposit is needed.","Для этого направления используются только доступные USDC в Base. Внешнее пополнение Solana не требуется.","Для цього напрямку використовуються лише доступні USDC у Base. Зовнішнє поповнення Solana не потрібне."],
            ["A quote, estimated receive amount, provider charge and network costs will be shown before you manually confirm. Nothing is submitted automatically.","Перед ручным подтверждением будут показаны котировка, ожидаемая сумма получения, комиссия провайдера и сетевые расходы. Ничего не отправляется автоматически.","Перед ручним підтвердженням буде показано котировку, очікувану суму отримання, комісію провайдера та мережеві витрати. Нічого не надсилається автоматично."],
            ["I agree to this recurring fee and authorize the in-app SOL balance transfer described above.","Я соглашаюсь с регулярной комиссией и разрешаю описанный выше внутренний перевод SOL.","Я погоджуюся з регулярною комісією та дозволяю описаний вище внутрішній переказ SOL."],
            ["Your device verifies you using Face ID, Touch ID or another system verification method. We verify the passkey signature, not a face photo. Face-only verification cannot be enforced on every device.","Устройство подтверждает вход через Face ID, Touch ID или другой системный способ. Проверяется подпись ключа доступа, а не фотография лица. Не на каждом устройстве можно принудительно включить распознавание только лица.","Пристрій підтверджує вхід через Face ID, Touch ID або інший системний спосіб. Перевіряється підпис ключа доступу, а не фото обличчя. Не на кожному пристрої можна примусово ввімкнути розпізнавання лише обличчя."],
            ["Both modes use a 5% stop below entry. After a position reaches +3.5%, a trailing exit triggers after a 2% pullback from its highest tracked price. Exits are checked approximately every 20 seconds independently of AI availability. Model failures stop new AI orders. After a server restart both modes pause and need manual reactivation.","В обоих режимах стоп-лосс установлен на 5% ниже входа. После роста позиции на 3,5% трейлинг-выход срабатывает при откате на 2% от максимальной отслеживаемой цены. Выходы проверяются примерно каждые 20 секунд независимо от доступности ИИ. Сбои модели приостанавливают новые ордера ИИ. После перезапуска сервера оба режима остановятся и потребуют ручного включения.","В обох режимах стоп-лос установлено на 5% нижче входу. Після зростання позиції на 3,5% трейлінг-вихід спрацьовує при відкаті на 2% від найвищої відстеженої ціни. Виходи перевіряються приблизно кожні 20 секунд незалежно від доступності ШІ. Збої моделі зупиняють нові ордери ШІ. Після перезапуску сервера обидва режими зупиняються й потребують ручного ввімкнення."],
            ["Stops and trailing exits are execution conditions, not guaranteed fill prices. A 5% stop can close a position, but fast price moves, fees, slippage and liquidity can make the realized loss larger. Returns are not guaranteed.","Стоп-лосс и трейлинг задают условия исполнения, но не гарантируют цену сделки. Стоп на 5% может закрыть позицию, однако резкое движение цены, комиссии, проскальзывание и ликвидность могут увеличить фактический убыток. Доходность не гарантируется.","Стоп-лос і трейлінг визначають умови виконання, але не гарантують ціну угоди. Стоп на 5% може закрити позицію, однак різкий рух ціни, комісії, прослизання та ліквідність можуть збільшити фактичний збиток. Дохідність не гарантована."],
            ["QUOTE FIRST · MANUAL CONFIRM","СНАЧАЛА КОТИРОВКА · РУЧНОЕ ПОДТВЕРЖДЕНИЕ","СПОЧАТКУ КОТИРУВАННЯ · РУЧНЕ ПІДТВЕРДЖЕННЯ"],
            ["LIVE · MANUAL CONFIRMATION","РЕАЛЬНЫЙ ПЕРЕВОД · РУЧНОЕ ПОДТВЕРЖДЕНИЕ","РЕАЛЬНИЙ ПЕРЕКАЗ · РУЧНЕ ПІДТВЕРДЖЕННЯ"],
            ["QUOTE ONLY · SEND OFF","ТОЛЬКО КОТИРОВКА · ОТПРАВКА ВЫКЛ","ЛИШЕ КОТИРУВАННЯ · НАДСИЛАННЯ ВИМК"],
            ["Request a LI.FI estimate first. Nothing moves automatically; a separate confirmation is required for every live transfer.","Сначала запросите оценку LI.FI. Ничего не перемещается автоматически; для каждого реального перевода нужно отдельное подтверждение.","Спочатку запросіть оцінку LI.FI. Нічого не переказується автоматично; для кожного реального переказу потрібне окреме підтвердження."],
            ["Quote first · no automatic sending","Сначала котировка · автоматической отправки нет","Спочатку котирування · автоматичного надсилання немає"],
            ["Live bridge · no automatic sending","Реальный обмен · без автоматической отправки","Реальний обмін · без автоматичного надсилання"],
            ["Quote only · live sending is not configured","Только котировка · реальные переводы не настроены","Лише котирування · реальні перекази не налаштовані"],
            ["A quote does not reserve or move funds. If live sending is configured, each transfer still requires your separate confirmation.","Котировка не резервирует и не перемещает средства. Если реальные переводы настроены, каждый всё равно требует отдельного подтверждения.","Котирування не резервує й не переказує кошти. Якщо реальні перекази налаштовані, кожен усе одно потребує окремого підтвердження."],
            ["A quote does not move funds. The bot sends only after you confirm this exact transfer.","Котировка не перемещает средства. Бот отправит перевод только после твоего подтверждения именно этой операции.","Котирування не переказує кошти. Бот надішле переказ лише після твого підтвердження саме цієї операції."],
            ["Live transfer · your confirmation required. After you confirm, the bot signs, sends, and tracks this transaction.","Реальный перевод · требуется твоё подтверждение. После него бот подписывает, отправляет транзакцию и отслеживает её.","Реальний переказ · потрібне твоє підтвердження. Після нього бот підписує, надсилає транзакцію та відстежує її."],
            ["Get a read-only quote for your Solana USDC. No funds are reserved or moved.","Получите котировку для своих USDC в Solana. Средства не резервируются и не перемещаются.","Отримайте котирування для своїх USDC у Solana. Кошти не резервуються й не переміщуються."],
            ["Get a read-only quote for your Base USDC. No funds are reserved or moved.","Получите котировку для своих USDC в Base. Средства не резервируются и не перемещаются.","Отримайте котирування для своїх USDC у Base. Кошти не резервуються й не переміщуються."],
            ["Request a quote and compare the estimated receive amount. Nothing is submitted automatically.","Запросите котировку и сравните ожидаемую сумму получения. Ничего не отправляется автоматически.","Запитайте котирування та порівняйте очікувану суму отримання. Нічого не надсилається автоматично."],
            ["Review Base estimate","Проверьте оценку для Base","Перевірте оцінку для Base"],
            ["Review Solana estimate","Проверьте оценку для Solana","Перевірте оцінку для Solana"],
            ["Compare the provider's estimate and minimum receive amount. No transfer is available yet.","Сравните оценку провайдера и минимальную сумму получения. Перевод пока недоступен.","Порівняйте оцінку провайдера та мінімальну суму отримання. Переказ поки недоступний."],
            ["You pay","Вы отправляете","Ви надсилаєте"],
            ["You receive · estimate","Вы получите · оценка","Ви отримаєте · оцінка"],
            ["USDC exchange between Solana and Base","Обмен USDC между Solana и Base","Обмін USDC між Solana та Base"],
            ["Available: ","Доступно: ","Доступно: "],
            ["Solana · account balance","Solana · баланс аккаунта","Solana · баланс рахунку"],
            ["Base · account balance","Base · баланс аккаунта","Base · баланс рахунку"],
            ["After finality","После подтверждения сети","Після підтвердження мережі"],
            ["Fee breakdown","Комиссии","Комісії"],["Route details","Маршрут","Маршрут"],
            ["Estimated costs:","Расчётные расходы:","Орієнтовні витрати:"],
            ["Review the estimate and minimum receive. Press “Exchange” to confirm this transaction; the bot handles routing and status checks.","Проверьте котировку и минимум получения. Нажатие «Обменять» подтверждает перевод; бот сам выполнит маршрут и проверит статус.","Перевірте котирування та мінімум отримання. Натискання «Обміняти» підтверджує переказ; бот сам виконає маршрут і перевірить статус."],
            ["Each transfer is confirmed separately. Your account is credited only after the destination transaction is verified as final.","Каждый перевод подтверждается отдельно. Баланс аккаунта пополняется только после проверки окончательного подтверждения в сети получателя.","Кожен переказ підтверджується окремо. Баланс облікового запису зараховується лише після перевірки остаточного підтвердження в мережі отримувача."],
            ["The button confirms this transfer. The bot signs, sends, and tracks it automatically.","Нажатие подтверждает этот перевод. Бот сам подписывает и отправляет транзакцию, затем отслеживает её.","Натискання підтверджує цей переказ. Бот сам підписує й надсилає транзакцію, а потім відстежує її."],
            ["Available balance unavailable.","Баланс недоступен.","Баланс недоступний."],
            ["Exchange","Обменять","Обміняти"],
            ["QUOTE · CONFIRM TO EXCHANGE","КОТИРОВКА · ПОДТВЕРДИТЕ ОБМЕН","КОТИРУВАННЯ · ПІДТВЕРДЬТЕ ОБМІН"],
            ["LIVE · PRESS TO EXCHANGE","РЕАЛЬНЫЙ ОБМЕН · НАЖМИТЕ ДЛЯ ЗАПУСКА","РЕАЛЬНИЙ ОБМІН · НАТИСНІТЬ ДЛЯ ЗАПУСКУ"],
            ["Amount (USDC)","Сумма (USDC)","Сума (USDC)"],["Get quote","Запросить котировку","Запросити котирування"],
            ["Send amount","Отправить","Надіслати"],["Estimated receive","Ожидается получить","Очікується отримати"],
            ["Minimum receive","Минимум к получению","Мінімум до отримання"],
            ["Available source balance","Доступно на исходном балансе","Доступно на балансі джерела"],
            ["Estimated costs","Оценочные расходы","Орієнтовні витрати"],
            ["Quote only. No transfer submitted or funds reserved.","Только котировка. Перевод не отправлен, средства не зарезервированы.","Лише котирування. Переказ не надіслано, кошти не зарезервовано."],
            ["Quote provider","Провайдер котировки","Провайдер котирування"],
            ["LI.FI temporary Solana deposit","Временный депозитный адрес LI.FI в Solana","Тимчасова депозитна адреса LI.FI у Solana"],
            ["Destination wallet","Кошелёк-получатель","Гаманець-отримувач"],
            ["LI.FI Solana fee","Комиссия LI.FI в Solana","Комісія LI.FI у Solana"],
            ["Send","Отправить","Надіслати"],["to","на","до"],["for at least","не менее","щонайменше"],
            ["USDC? This is a real transaction. Review the displayed fees.","USDC? Это реальная транзакция. Проверь показанные комиссии.","USDC? Це реальна транзакція. Перевір показані комісії."],
            ["LI.FI receives USDC at a temporary Solana address, then routes it to the displayed Base wallet. The Solana transaction does not prove LI.FI will complete delivery. Confirm only if you accept this provider risk.","LI.FI получает USDC на временный адрес в Solana, а затем переводит их на указанный Base-кошелёк. Транзакция Solana сама по себе не доказывает, что LI.FI завершит перевод. Подтверждай, только если принимаешь этот риск провайдера.","LI.FI отримує USDC на тимчасову адресу в Solana, а потім переказує їх на вказаний Base-гаманець. Транзакція Solana сама по собі не доводить, що LI.FI завершить переказ. Підтверджуй, лише якщо приймаєш цей ризик провайдера."],
            ["Quote expires","Котировка истекает","Котирування спливає"],
            ["Provider fee","Комиссия провайдера","Комісія провайдера"],
            ["Network fee","Сетевая комиссия","Мережевая комісія"],
            ["No fee breakdown was returned.","Провайдер не вернул разбивку расходов.","Провайдер не повернув розбивку витрат."],
            ["The amount is above your available in-app balance. This is only an estimate; no transfer can be sent.","Сумма превышает доступный баланс в приложении. Это только оценка; отправить перевод нельзя.","Сума перевищує доступний баланс у застосунку. Це лише оцінка; надіслати переказ неможливо."],
            ["This Base quote would require USDC approval before a transfer. No approval was sent.","Для этого перевода Base потребовалось бы разрешение USDC. Разрешение не отправлялось.","Для цього переказу Base знадобився б дозвіл USDC. Дозвіл не надсилався."],
            ["Requesting a live quote…","Запрашиваем актуальную котировку…","Запитуємо актуальне котирування…"],
            ["Could not get a quote. No transaction was sent.","Не удалось получить котировку. Транзакция не отправлялась.","Не вдалося отримати котирування. Транзакцію не надсилали."],
            ["Enter a valid USDC amount.","Введите корректную сумму USDC.","Введіть коректну суму USDC."],
            ["Enter a valid USDC amount with at most six decimals.","Введите сумму USDC не более чем с шестью знаками после запятой.","Введіть суму USDC не більш ніж із шістьма знаками після коми."],
            ["A quote is limited to 10,000 USDC.","Котировка ограничена суммой 10 000 USDC.","Котирування обмежене сумою 10 000 USDC."],
            ["LI.FI API key is not configured on the server.","Ключ LI.FI API не настроен на сервере.","Ключ LI.FI API не налаштований на сервері."],
            ["The Base hot-wallet address is not configured correctly.","Адрес Base hot wallet настроен неверно.","Адреса Base hot wallet налаштована неправильно."],
            ["The Solana wallet is not configured correctly.","Адрес Solana-кошелька настроен неверно.","Адреса Solana-гаманця налаштована неправильно."],
            ["The quote provider is unavailable. No transaction was sent.","Сервис котировок недоступен. Транзакция не отправлялась.","Сервіс котирувань недоступний. Транзакцію не надсилали."],
            ["LI.FI is rate-limiting quotes. Try again shortly.","LI.FI временно ограничил частоту запросов. Попробуйте позже.","LI.FI тимчасово обмежив частоту запитів. Спробуйте пізніше."],
            ["Wait a few seconds before requesting another quote.","Подождите несколько секунд перед следующим запросом котировки.","Зачекайте кілька секунд перед наступним запитом котирування."],
            ["The temporary quote limit has been reached. Try again later.","Достигнут временный лимит котировок. Попробуйте позже.","Досягнуто тимчасового ліміту котирувань. Спробуйте пізніше."],
            ["LI.FI returned a quote for a different route or invalid amount.","LI.FI вернул котировку для другого маршрута или неверной суммы.","LI.FI повернув котирування для іншого маршруту або неправильної суми."],
            ["Estimate only. This screen cannot approve or send a transaction.","Только оценка. На этом экране нельзя подтвердить или отправить транзакцию.","Лише оцінка. На цьому екрані не можна підтвердити чи надіслати транзакцію."],
            ["Quote requests are read-only. Transfers, approvals, and balance changes remain disabled.","Запросы котировок работают только на чтение. Переводы, разрешения и изменения баланса отключены.","Запити котирувань працюють лише для читання. Перекази, дозволи та зміни балансу вимкнені."],
            ["See the estimated output, minimum receive amount, provider fees and network costs. No transfer is submitted.","Смотрите оценку суммы получения, минимальную сумму, комиссию провайдера и сетевые расходы. Перевод не отправляется.","Перегляньте оцінку суми отримання, мінімальну суму, комісію провайдера та мережеві витрати. Переказ не надсилається."],
            ["YOUR SOLANA USDC","ВАШИ USDC В SOLANA","ВАШІ USDC У SOLANA"],
            ["YOUR BASE USDC","ВАШИ USDC В BASE","ВАШІ USDC У BASE"],
            ["YOUR SOLANA BALANCE","ВАШ БАЛАНС SOLANA","ВАШ БАЛАНС SOLANA"],
            ["YOUR BASE BALANCE","ВАШ БАЛАНС BASE","ВАШ БАЛАНС BASE"],
            ["PROVIDER ESTIMATE","ОЦЕНКА ПРОВАЙДЕРА","ОЦІНКА ПРОВАЙДЕРА"],
            ["BASE USDC ESTIMATE","ОЦЕНКА USDC В BASE","ОЦІНКА USDC У BASE"],
            ["SOLANA USDC ESTIMATE","ОЦЕНКА USDC В SOLANA","ОЦІНКА USDC У SOLANA"],
            ["Quote only","Только котировка","Лише котирування"],
            ["Estimates can expire or change. No purchase or transfer is sent from this screen.","Оценки могут устареть или измениться. С этого экрана покупка или перевод не отправляются.","Оцінки можуть застаріти або змінитися. Із цього екрана покупка чи переказ не надсилаються."],
            ["included","включено","включено"],
            ["The quote service is temporarily unavailable.","Сервис котировок временно недоступен.","Сервіс котирувань тимчасово недоступний."],
            ["Could not read your available account balance.","Не удалось прочитать доступный баланс аккаунта.","Не вдалося прочитати доступний баланс облікового запису."],
            ["Specify the direction and USDC amount.","Укажите направление и сумму USDC.","Вкажіть напрямок і суму USDC."],
            ["Choose a valid quote direction.","Выберите корректное направление котировки.","Виберіть коректний напрямок котирування."],
            ["USDC route estimates for Solana and Base","Оценка маршрутов USDC между Solana и Base","Оцінка маршрутів USDC між Solana та Base"],
            ["USDC · READ-ONLY ROUTE QUOTE","USDC · КОТИРОВКА МАРШРУТА","USDC · КОТИРУВАННЯ МАРШРУТУ"],
            ["Choose quote direction","Выберите направление котировки","Виберіть напрямок котирування"],
            ["Read-only quote details","Детали котировки","Деталі котирування"],
            ["Quote request","Запрос котировки","Запит котирування"],
            ["Quote route","Маршрут котировки","Маршрут котирування"]
        ];
        const UI_COPY_ROWS_EXTRA = [
            ["A quote never sends funds. Each approval or transfer requires your separate confirmation.","Котировка не отправляет средства. Каждое разрешение или перевод требуют отдельного подтверждения.","Котирування не переказує кошти. Кожне підтвердження або переказ потребує окремої згоди."],
            ["Solana source transaction","Исходная транзакция Solana","Вихідна транзакція Solana"],
            ["Base source transaction","Исходная транзакция Base","Вихідна транзакція Base"],
            ["Solana destination transaction","Транзакция назначения Solana","Транзакція призначення Solana"],
            ["Base destination transaction","Транзакция назначения Base","Транзакція призначення Base"],
            ["The bot has not recorded a source transaction signature. Funds remain reserved; do not submit again.","Бот не сохранил подпись исходной транзакции. Средства остаются в резерве; не отправляйте перевод повторно.","Бот не зберіг підпис вихідної транзакції. Кошти залишаються в резерві; не надсилайте переказ повторно."],
            ["Solana source signature · broadcast not confirmed","Подпись Solana · отправка не подтверждена","Підпис Solana · надсилання не підтверджено"],
            ["Base source signature · broadcast not confirmed","Подпись Base · отправка не подтверждена","Підпис Base · надсилання не підтверджено"],
            ["Solana source transaction · RPC accepted; confirmation pending","Исходная транзакция Solana · RPC принял, ожидается подтверждение","Вихідна транзакція Solana · RPC прийняв, очікується підтвердження"],
            ["Base source transaction · RPC accepted; confirmation pending","Исходная транзакция Base · RPC принял, ожидается подтверждение","Вихідна транзакція Base · RPC прийняв, очікується підтвердження"],
            ["Submission RPC error:","Ошибка RPC при отправке:","Помилка RPC під час надсилання:"],
            ["The Solana signature expired; two RPCs found no transaction and LI.FI found no transfer. The reserved amount has been returned to your available balance. Request a fresh quote before any new transfer.","Подпись Solana истекла; два RPC не нашли транзакцию, а LI.FI — перевод. Зарезервированная сумма возвращена на доступный баланс. Перед новой отправкой запросите свежую котировку.","Підпис Solana минув; два RPC не знайшли транзакцію, а LI.FI — переказ. Зарезервовану суму повернуто на доступний баланс. Перед новим переказом запросіть свіжу котировку."],
            ["Solana source signature expired · transaction not found","Подпись Solana истекла · транзакция не найдена","Підпис Solana минув · транзакцію не знайдено"],
            ["Solana has seen the source transaction, but it is not finalized yet.","Solana обнаружила исходную транзакцию, но она ещё не подтверждена окончательно.","Solana бачить вихідну транзакцію, але її ще не підтверджено остаточно."],
            ["Base has seen the source transaction, but it is not finalized yet.","Base обнаружила исходную транзакцию, но она ещё не подтверждена окончательно.","Base бачить вихідну транзакцію, але її ще не підтверджено остаточно."],
            ["Solana finalized the source transaction, but LI.FI has not found the bridge transfer. The outcome is unresolved; funds remain reserved. Do not submit again.","Solana окончательно подтвердила исходную транзакцию, но LI.FI не нашёл перевод. Результат не выяснен; средства остаются в резерве. Не отправляйте повторно.","Solana остаточно підтвердила вихідну транзакцію, але LI.FI не знайшов переказ. Результат не з’ясовано; кошти залишаються в резерві. Не надсилайте повторно."],
            ["Base finalized the source transaction, but LI.FI has not found the bridge transfer. The outcome is unresolved; funds remain reserved. Do not submit again.","Base окончательно подтвердила исходную транзакцию, но LI.FI не нашёл перевод. Результат не выяснен; средства остаются в резерве. Не отправляйте повторно.","Base остаточно підтвердила вихідну транзакцію, але LI.FI не знайшов переказ. Результат не з’ясовано; кошти залишаються в резерві. Не надсилайте повторно."],
            ["Solana has no finalized source transaction for this signature, and LI.FI has not found the transfer. The outcome is unresolved; funds remain reserved. Do not submit again.","В Solana нет окончательно подтверждённой исходной транзакции с этой подписью, а LI.FI не нашёл перевод. Результат не выяснен; средства остаются в резерве. Не отправляйте повторно.","У Solana немає остаточно підтвердженої вихідної транзакції з цим підписом, а LI.FI не знайшов переказ. Результат не з’ясовано; кошти залишаються в резерві. Не надсилайте повторно."],
            ["Base has no finalized source transaction for this hash, and LI.FI has not found the transfer. The outcome is unresolved; funds remain reserved. Do not submit again.","В Base нет окончательно подтверждённой исходной транзакции с этим хешем, а LI.FI не нашёл перевод. Результат не выяснен; средства остаются в резерве. Не отправляйте повторно.","У Base немає остаточно підтвердженої вихідної транзакції з цим хешем, а LI.FI не знайшов переказ. Результат не з’ясовано; кошти залишаються в резерві. Не надсилайте повторно."],
            ["ACCESS PACKAGES","ПАКЕТЫ ДОСТУПА","ПАКЕТИ ДОСТУПУ"],
            ["AI SIGNAL / REVIEW","СИГНАЛ ИИ / ПРОВЕРКА","СИГНАЛ ШІ / ПЕРЕВІРКА"],
            ["AI Trader","AI-трейдер","AI-трейдер"],
            ["Ai Crypto Investors","AI-криптоинвесторы","AI-криптоінвестори"],
            ["AI Trader · Personal account","AI-трейдер · личный аккаунт","AI-трейдер · особистий обліковий запис"],
            ["AI Trader · Solana / USDC","AI-трейдер · Solana / USDC","AI-трейдер · Solana / USDC"],
            ["AI decisions are not confirmed trades · times in your timezone","Решения ИИ не являются подтверждёнными сделками · время указано по вашему часовому поясу","Рішення ШІ не є підтвердженими угодами · час указано за вашим часовим поясом"],
            ["AI makes decisions after you enable a mode. Orders require personal funds, permitted assets, at least 5 price samples, a fresh decision and stable pricing. Only one shared-wallet operation can be pending at a time.","ИИ принимает решения только после включения режима. Для ордера нужны личные средства, разрешённый актив, не менее 5 замеров цены, актуальное решение и стабильная цена. Одновременно может ожидать только одна операция общего кошелька.","ШІ ухвалює рішення лише після ввімкнення режиму. Для ордера потрібні особисті кошти, дозволений актив, щонайменше 5 вимірювань ціни, актуальне рішення та стабільна ціна. Одночасно може очікувати лише одна операція спільного гаманця."],
            ["AI-ASSISTED · USER-DIRECTED","С ПОМОЩЬЮ ИИ · ПОД КОНТРОЛЕМ ПОЛЬЗОВАТЕЛЯ","ЗА ДОПОМОГОЮ ШІ · ПІД КОНТРОЛЕМ КОРИСТУВАЧА"],
            ["Accept and activate","Принять и включить","Прийняти й активувати"],
            ["Access package","Пакет доступа","Пакет доступу"],
            ["Accumulated partner commissions","Накопленные партнёрские комиссии","Накопичені партнерські комісії"],
            ["Active referral network","Активная реферальная сеть","Активна реферальна мережа"],
            ["Amount in SOL","Сумма в SOL","Сума в SOL"],
            ["Base → Solana","Base → Solana","Base → Solana"],
            ["Budget limit · SOL","Лимит бюджета · SOL","Ліміт бюджету · SOL"],
            ["Budget limit · USDC","Лимит бюджета · USDC","Ліміт бюджету · USDC"],
            ["Candidate scan pending.","Ожидается сканирование кандидатов.","Очікується сканування кандидатів."],
            ["Check estimated output, minimum receive amount, provider fees and network costs. Confirm each real transaction separately.","Проверьте расчётную сумму получения, минимум, комиссию провайдера и сетевые расходы. Каждый реальный перевод подтверждайте отдельно.","Перевірте розрахункову суму отримання, мінімум, комісію провайдера та мережеві витрати. Кожен реальний переказ підтверджуйте окремо."],
            ["Check legacy operations","Проверить старые операции","Перевірити попередні операції"],
            ["Check readiness · no trade","Проверить готовность · без сделки","Перевірити готовність · без угоди"],
            ["Check rule signals · no trade","Проверить сигналы правил · без сделки","Перевірити сигнали правил · без угоди"],
            ["Check transfer status","Проверить статус перевода","Перевірити статус переказу"],
            ["Check wallet funds against account balances (read-only)","Сверить средства кошелька с балансом аккаунта (только чтение)","Звірити кошти гаманця з балансом облікового запису (лише читання)"],
            ["Checking access…","Проверяем доступ…","Перевіряємо доступ…"],
            ["Checking account access…","Проверяем доступ к аккаунту…","Перевіряємо доступ до облікового запису…"],
            ["Checking signal status...","Проверяем статус сигнала…","Перевіряємо статус сигналу…"],
            ["Close","Закрыть","Закрити"],
            ["Commissions use realized trade profit — sale proceeds minus recorded purchase cost — after prior trading losses are recovered.","Комиссия начисляется с реализованной прибыли: сумма продажи минус учтённая стоимость покупки. Начисление начинается после компенсации предыдущих торговых убытков.","Комісія нараховується з реалізованого прибутку: сума продажу мінус облікована вартість купівлі. Нарахування починається після компенсації попередніх торгових збитків."],
            ["Confirm withdrawals with a one-time Telegram code.","Подтверждайте вывод одноразовым кодом Telegram.","Підтверджуйте виведення одноразовим кодом Telegram."],
            ["Contact technical support","Связаться с технической поддержкой","Зв’язатися з технічною підтримкою"],
            ["Core entry cost budget: 2.50% of the order. Includes quoted DEX fees, price impact, slippage, estimated network fees, Jito tips and account rent for entry/exit. Estimates do not guarantee profit or exit costs. SELL exits are not blocked by this filter.","Лимит расходов на вход Core — 2,50% суммы ордера. Учитываются комиссии DEX, влияние на цену, проскальзывание, сетевые комиссии, чаевые Jito и аренда аккаунта при входе/выходе. Оценка не гарантирует прибыль или стоимость выхода. Фильтр не блокирует продажи.","Ліміт витрат на вхід Core — 2,50% суми ордера. Враховуються комісії DEX, вплив на ціну, прослизання, мережеві комісії, чайові Jito та оренда акаунта під час входу/виходу. Оцінка не гарантує прибуток або вартість виходу. Фільтр не блокує продажі."],
            ["Core trades use your USDC; sniper trades use your SOL. Other users' funds and the operator fee reserve are never your trading capital. Automatic conversion or profit allocation between strategies is not implemented.","Для сделок Core используется ваш USDC, для Sniper — ваш SOL. Средства других пользователей и резерв комиссий оператора не являются вашим торговым капиталом. Автоматическая конвертация и распределение прибыли между стратегиями не поддерживаются.","Для угод Core використовується ваш USDC, для Sniper — ваш SOL. Кошти інших користувачів і резерв комісій оператора не є вашим торговим капіталом. Автоматична конвертація та розподіл прибутку між стратегіями не підтримуються."],
            ["Create account","Создать аккаунт","Створити обліковий запис"],
            ["Credit Base after finality","Зачислить в Base после окончательного подтверждения","Зарахувати в Base після остаточного підтвердження"],
            ["Current SOL amount: checking…","Проверяем текущую сумму SOL…","Перевіряємо поточну суму SOL…"],
            ["DIRECT CONTROL","ПРЯМОЕ УПРАВЛЕНИЕ","БЕЗПОСЕРЕДНЄ КЕРУВАННЯ"],
            ["DIRECT REFERRALS","ПРЯМЫЕ РЕФЕРАЛЫ","ПРЯМІ РЕФЕРАЛИ"],
            ["Direct","Первый уровень","Перший рівень"],
            ["Do not send passwords, security codes, recovery codes, private keys, or seed phrases.","Не отправляйте пароли, коды безопасности и восстановления, закрытые ключи или seed-фразы.","Не надсилайте паролі, коди безпеки й відновлення, приватні ключі або seed-фрази."],
            ["Estimates can expire or change. Review the minimum receive amount and costs before confirming.","Оценка может устареть или измениться. Перед подтверждением проверьте минимальную сумму получения и расходы.","Оцінка може застаріти або змінитися. Перед підтвердженням перевірте мінімальну суму отримання та витрати."],
            ["Fee reserve · SOL","Резерв комиссий · SOL","Резерв комісій · SOL"],
            ["Fees are paid from a separate operator reserve.","Комиссии оплачиваются из отдельного резерва оператора.","Комісії сплачуються з окремого резерву оператора."],
            ["For USDC deposits, verify your wallet once, copy the address below, send USDC on Solana mainnet, then submit the transaction signature. The bot credits your balance only after it verifies the finalized transfer. SOL can also be deposited by standard transfer.","Для пополнения USDC один раз подтвердите кошелёк, скопируйте адрес ниже, отправьте USDC в основной сети Solana и укажите подпись транзакции. Бот зачислит средства только после проверки окончательного подтверждения. SOL можно пополнить обычным переводом.","Щоб поповнити USDC, один раз підтвердьте гаманець, скопіюйте адресу нижче, надішліть USDC в основній мережі Solana й укажіть підпис транзакції. Бот зарахує кошти лише після перевірки остаточного підтвердження. SOL також можна поповнити звичайним переказом."],
            ["Free access has ended","Бесплатный доступ завершён","Безкоштовний доступ завершився"],
            ["Free in Investor pool · SOL","Свободно в пуле Investor · SOL","Вільно в пулі Investor · SOL"],
            ["Free in Investor pool · USDC","Свободно в пуле Investor · USDC","Вільно в пулі Investor · USDC"],
            ["How commissions and payouts work","Как начисляются и выплачиваются комиссии","Як нараховуються та виплачуються комісії"],
            ["I confirm the fee reserve being returned is my SOL, not customer funds.","Подтверждаю, что возвращаемый резерв — мой SOL, а не средства клиентов.","Підтверджую, що резерв, який повертається, — це мій SOL, а не кошти клієнтів."],
            ["I understand trading can lose money and returns are not guaranteed.","Я понимаю, что торговля может привести к убыткам и прибыль не гарантирована.","Я розумію, що торгівля може призвести до збитків і прибуток не гарантований."],
            ["IN-APP","В ПРИЛОЖЕНИИ","У ЗАСТОСУНКУ"],
            ["Import backed old customer balances","Импортировать подтверждённые старые балансы клиентов","Імпортувати підтверджені старі баланси клієнтів"],
            ["Import eligible balances","Импортировать подходящие балансы","Імпортувати придатні баланси"],
            ["Import my legacy balance","Импортировать мой старый баланс","Імпортувати мій попередній баланс"],
            ["Independent execution guard","Независимая проверка исполнения","Незалежна перевірка виконання"],
            ["LEVEL / NETWORK","УРОВЕНЬ / СЕТЬ","РІВЕНЬ / МЕРЕЖА"],
            ["Liquidity is checked through the executable route and price impact at the requested size, not a promise of market depth. Entry cost estimates are not profit forecasts. Entry-efficiency checks never block owned-position exits or withdrawals. A passed check is not proof of a filled trade.","Ликвидность оценивается по исполнимому маршруту и влиянию на цену при заданной сумме, а не по обещанной глубине рынка. Оценка расходов на вход не является прогнозом прибыли. Проверка входа никогда не блокирует выход из своих позиций или вывод средств. Успешная проверка не подтверждает исполнение сделки.","Ліквідність оцінюється за маршрутом виконання та впливом на ціну для вказаної суми, а не за обіцяною глибиною ринку. Оцінка витрат на вхід не є прогнозом прибутку. Перевірка входу ніколи не блокує вихід із власних позицій або виведення коштів. Успішна перевірка не підтверджує виконання угоди."],
            ["Loading activity...","Загружаем историю активности…","Завантажуємо історію активності…"],
            ["Loading assets...","Загружаем активы…","Завантажуємо активи…"],
            ["Loading balance...","Загружаем баланс…","Завантажуємо баланс…"],
            ["Loading fee status…","Загружаем статус резерва комиссий…","Завантажуємо статус резерву комісій…"],
            ["Loading partner balances…","Загружаем балансы партнёров…","Завантажуємо баланси партнерів…"],
            ["Loading trades...","Загружаем сделки…","Завантажуємо угоди…"],
            ["Loading your account-specific link…","Загружаем персональную ссылку аккаунта…","Завантажуємо персональне посилання облікового запису…"],
            ["Logs & position controls","Журналы и управление позициями","Журнали та керування позиціями"],
            ["Manual confirmation","Ручное подтверждение","Ручне підтвердження"],
            ["Manual transfer fallback. No Memo required.","Резервный ручной перевод. Memo не требуется.","Запасний ручний переказ. Memo не потрібен."],
            ["Memecoin trades funded in SOL","Сделки с мемкоинами за SOL","Угоди з мемкоїнами за SOL"],
            ["NOT CONNECTED","НЕ ПОДКЛЮЧЕНО","НЕ ПІДКЛЮЧЕНО"],
            ["Nickname","Псевдоним","Псевдонім"],
            ["No entry cost check yet.","Проверка расходов на вход ещё не проводилась.","Перевірку витрат на вхід ще не виконано."],
            ["No execution check recorded yet.","Проверка исполнения ещё не записана.","Перевірку виконання ще не зафіксовано."],
            ["No legacy balance preview has been run.","Предварительная проверка старого баланса ещё не запускалась.","Попередню перевірку попереднього балансу ще не запускали."],
            ["Not now","Не сейчас","Не зараз"],
            ["One-time USDC ledger migration. Preview checks old balances, new-ledger activity, and unassigned on-chain backing. Accounts with ambiguous new activity are skipped. This updates the internal ledger only; it never sends a Solana transaction.","Одноразовый перенос балансов USDC во внутренний реестр. Предварительная проверка сверяет старые балансы, операции в новом реестре и неподтверждённое покрытие в сети. Аккаунты с неоднозначной активностью пропускаются. Меняется только внутренний реестр; транзакция Solana не отправляется.","Одноразове перенесення балансів USDC до внутрішнього реєстру. Попередня перевірка звіряє старі баланси, операції в новому реєстрі та нерозподілене покриття в мережі. Облікові записи з неоднозначною активністю пропускаються. Змінюється лише внутрішній реєстр; транзакція Solana не надсилається."],
            ["Open in Solana wallet","Открыть в кошельке Solana","Відкрити в гаманці Solana"],
            ["Open investor positions","Открытые позиции Investor","Відкриті позиції Investor"],
            ["Open payment status page","Открыть страницу статуса платежа","Відкрити сторінку стану платежу"],
            ["Open secure checkout","Открыть защищённую оплату","Відкрити захищену оплату"],
            ["Open-position cost · SOL","Стоимость открытых позиций · SOL","Вартість відкритих позицій · SOL"],
            ["Open-position cost · USDC","Стоимость открытых позиций · USDC","Вартість відкритих позицій · USDC"],
            ["Operator tools","Инструменты оператора","Інструменти оператора"],
            ["Operator tools · fees & migration","Инструменты оператора · комиссии и перенос","Інструменти оператора · комісії та перенесення"],
            ["Outcome memory: awaiting finalized trade history.","Память результатов: ожидается окончательная история сделок.","Пам’ять результатів: очікується остаточна історія угод."],
            ["Owner shortfall adjustment","Корректировка дефицита владельца","Коригування нестачі власника"],
            ["PERSONAL EXECUTION LAYER","ПЕРСОНАЛЬНЫЙ ТОРГОВЫЙ УРОВЕНЬ","ОСОБИСТИЙ ТОРГОВИЙ РІВЕНЬ"],
            ["Passkeys & account recovery","Ключи доступа и восстановление аккаунта","Ключі доступу та відновлення облікового запису"],
            ["Pay exactly","Оплатить точную сумму","Сплатити точну суму"],
            ["Payment is separate from your trading balance. It does not enable Core or Sniper.","Оплата не списывается с торгового баланса и не включает Core или Sniper.","Оплата не списується з торгового балансу й не вмикає Core або Sniper."],
            ["Policy loading...","Загружаем правила…","Завантажуємо правила…"],
            ["Preview eligible balances (read-only)","Предварительно проверить подходящие балансы (только чтение)","Попередньо перевірити придатні баланси (лише читання)"],
            ["REFERRAL LINK","РЕФЕРАЛЬНАЯ ССЫЛКА","РЕФЕРАЛЬНЕ ПОСИЛАННЯ"],
            ["REFERRALS","РЕФЕРАЛЫ","РЕФЕРАЛИ"],
            ["Realized SOL PnL: loading...","Реализованный PnL в SOL: загружаем…","Реалізований PnL у SOL: завантажуємо…"],
            ["Recipient","Получатель","Отримувач"],
            ["Recover using a saved code","Восстановить с помощью сохранённого кода","Відновити за збереженим кодом"],
            ["Request expires","Срок действия запроса","Строк дії запиту"],
            ["Required for this referred account","Требуется для этого реферала","Потрібно для цього реферала"],
            ["Reserve not checked yet.","Резерв ещё не проверен.","Резерв ще не перевірено."],
            ["Return excess fee reserve","Вернуть излишек резерва комиссий","Повернути надлишок резерву комісій"],
            ["Risk controller: awaiting analysis.","Контроль рисков: ожидается анализ.","Контроль ризиків: очікується аналіз."],
            ["Run the read-only wallet coverage check first.","Сначала запустите проверку покрытия кошелька без изменений.","Спочатку запустіть перевірку покриття гаманця без змін."],
            ["SECOND CIRCLE","ВТОРОЙ УРОВЕНЬ","ДРУГИЙ РІВЕНЬ"],
            ["RATE","СТАВКА","ЧАСТКА"],
            ["SOL per sniper order","SOL на ордер Sniper","SOL на ордер Sniper"],
            ["SOL · CLOSED TRADES","SOL · ЗАКРЫТЫЕ СДЕЛКИ","SOL · ЗАКРИТІ УГОДИ"],
            ["SOLANA","SOLANA","SOLANA"],
            ["SOLANA / USDC","SOLANA / USDC","SOLANA / USDC"],
            ["SOLANA · USDC","SOLANA · USDC","SOLANA · USDC"],
            ["Save order sizes","Сохранить размер ордеров","Зберегти розмір ордерів"],
            ["Save recovery codes shown in the secure browser. Recovery requires your Telegram account, account security code and an unused recovery code; it replaces old passkeys.","Сохраните коды восстановления в защищённом браузере. Для восстановления нужны аккаунт Telegram, код безопасности и неиспользованный код восстановления; старые ключи доступа будут заменены.","Збережіть коди відновлення в захищеному браузері. Для відновлення потрібні обліковий запис Telegram, код безпеки та невикористаний код відновлення; старі ключі доступу буде замінено."],
            ["Scheduled market scanning replaces the Helius whale stream. No whale event is required.","Плановое сканирование рынка заменяет поток китовых транзакций Helius. Событие от кита не требуется.","Планове сканування ринку замінює потік китових транзакцій Helius. Подія від кита не потрібна."],
            ["Screened Solana candidates · Checking position limit…","Проверенные кандидаты Solana · проверяем лимит позиций…","Перевірені кандидати Solana · перевіряємо ліміт позицій…"],
            ["Screenshot · optional","Снимок экрана · необязательно","Знімок екрана · необов’язково"],
            ["Second level","Второй уровень","Другий рівень"],
            ["See what the AI sees. Stay in control of every move.","Смотрите, что видит ИИ. Контролируйте каждое действие.","Бачте те, що бачить ШІ. Контролюйте кожну дію."],
            ["Set up / add browser passkey","Настроить или добавить ключ доступа в браузере","Налаштувати або додати ключ доступу в браузері"],
            ["Share eligible realized trading profit across three referral levels.","Распределяйте подходящую реализованную прибыль от торговли между тремя уровнями рефералов.","Розподіляйте придатний реалізований прибуток від торгівлі між трьома рівнями рефералів."],
            ["Share your link. Eligible partner activity is tracked automatically.","Поделитесь ссылкой. Подходящая активность партнёров учитывается автоматически.","Поділіться посиланням. Придатна активність партнерів обліковується автоматично."],
            ["Signal timings: awaiting a fresh market scan.","Время сигналов: ожидается новое сканирование рынка.","Час сигналів: очікується нове сканування ринку."],
            ["Solana (SOL)","Solana (SOL)","Solana (SOL)"],
            ["Solana network fees are paid from the operator reserve and are not deducted from the partner’s commission.","Сетевые комиссии Solana оплачиваются из резерва оператора и не вычитаются из комиссии партнёра.","Мережеві комісії Solana сплачуються з резерву оператора й не вираховуються з комісії партнера."],
            ["Solana → Base","Solana → Base","Solana → Base"],
            ["Start one-time 3-day free trial","Начать бесплатный пробный период на 3 дня","Почати безкоштовний пробний період на 3 дні"],
            ["THIRD CIRCLE","ТРЕТИЙ УРОВЕНЬ","ТРЕТІЙ РІВЕНЬ"],
            ["TRADE WITH CLARITY","ТОРГУЙТЕ ОСОЗНАННО","ТОРГУЙТЕ УСВІДОМЛЕНО"],
            ["The rules engine proposes BUY / HOLD only. Server-owned checks decide whether an order is allowed. No model can change limits or disable checks.","Модуль правил предлагает только BUY / HOLD. Разрешение на ордер определяют серверные проверки. Ни одна модель не может менять лимиты или отключать проверки.","Модуль правил пропонує лише BUY / HOLD. Дозвіл на ордер визначають серверні перевірки. Жодна модель не може змінювати ліміти або вимикати перевірки."],
            ["Third level","Третий уровень","Третій рівень"],
            ["Type total budget · SOL","Введите общий бюджет · SOL","Вкажіть загальний бюджет · SOL"],
            ["Type total budget · USDC","Введите общий бюджет · USDC","Вкажіть загальний бюджет · USDC"],
            ["USDC / SOL · Solana mainnet","USDC / SOL · основная сеть Solana","USDC / SOL · основна мережа Solana"],
            ["USDC and SOL are accounted for separately. Open trades and deposits do not count.","USDC и SOL учитываются отдельно. Открытые сделки и пополнения не входят в итог.","USDC і SOL обліковуються окремо. Відкриті угоди та поповнення не враховуються."],
            ["USDC per core order","USDC на ордер Core","USDC на ордер Core"],
            ["USDC · CLOSED TRADES","USDC · ЗАКРЫТЫЕ СДЕЛКИ","USDC · ЗАКРИТІ УГОДИ"],
            ["Unresolved transfers stay reserved. Never submit an unresolved transfer again.","Средства по незавершённым переводам остаются в резерве. Не отправляйте такой перевод повторно.","Кошти за незавершеними переказами залишаються в резерві. Не надсилайте такий переказ повторно."],
            ["View tariffs and pay","Посмотреть тарифы и оплатить","Переглянути тарифи й оплатити"],
            ["Wins / losses / even:","Прибыль / убытки / без изменений:","Прибуток / збитки / без змін:"],
            ["Withdrawals use a one-time Telegram code. A passkey is required for standalone browser sign-in.","Для вывода нужен одноразовый код Telegram. Для входа в браузер без Telegram требуется ключ доступа.","Для виведення потрібен одноразовий код Telegram. Для входу в браузері без Telegram потрібен ключ доступу."],
            ["YOUR BALANCES · MANUAL CONFIRMATION","ВАШИ БАЛАНСЫ · РУЧНОЕ ПОДТВЕРЖДЕНИЕ","ВАШІ БАЛАНСИ · РУЧНЕ ПІДТВЕРДЖЕННЯ"],
            ["YOUR CAPITAL. YOUR CALL.","ВАШ КАПИТАЛ. ВАШЕ РЕШЕНИЕ.","ВАШ КАПІТАЛ. ВАШЕ РІШЕННЯ."],
            ["You may return only the amount above the required minimum. This changes your bot balance only; it does not send a Solana transaction.","Можно вернуть только сумму сверх обязательного минимума. Это меняет только баланс бота и не отправляет транзакцию Solana.","Можна повернути лише суму понад обов’язковий мінімум. Це змінює лише баланс бота й не надсилає транзакцію Solana."],
            ["Your Base balance is credited only after the destination transfer is finalized and verified.","Баланс Base зачисляется только после окончательного подтверждения и проверки перевода в сети назначения.","Баланс Base зараховується лише після остаточного підтвердження та перевірки переказу в мережі призначення."],
            ["Your capital only","Только ваш капитал","Лише ваш капітал"],
            ["Your verified Telegram account is your login. No email or password required.","Вход выполняется через подтверждённый аккаунт Telegram. Электронная почта и пароль не нужны.","Вхід виконується через підтверджений обліковий запис Telegram. Електронна пошта й пароль не потрібні."],
            ["Your wallet needs the full amount in the selected asset and a little SOL for network fees. It shows the fee before approval. The bot credits your balance after Solana finalizes the exact transfer. Do not pay twice.","В кошельке должна быть вся сумма в выбранном активе и немного SOL на комиссию сети. Комиссия показывается до подтверждения. Бот зачислит средства после окончательного подтверждения точного перевода в Solana. Не платите повторно.","У гаманці має бути вся сума у вибраному активі та трохи SOL на комісію мережі. Комісію показано до підтвердження. Бот зарахує кошти після остаточного підтвердження точного переказу в Solana. Не сплачуйте повторно."],
            ["Zer0Life","Zer0Life","Zer0Life"],
            ["partners","партнёров","партнерів"],
            ["Русский","Русский","Російська"],["Українська","Українский","Українська"],
            ["A source transfer does not prove the bridge completed delivery.","Исходный перевод не доказывает, что мост доставил средства.","Вихідний переказ не доводить, що міст доставив кошти."],
            ["Rules v1 · OpenAI OFF · no paid AI requests · ","Правила v1 · OpenAI ВЫКЛ · платные запросы ИИ не отправляются · ","Правила v1 · OpenAI ВИМК · платні запити до ШІ не надсилаються · "],
            ["Core · USDC","Основной режим · USDC","Основний режим · USDC"],
            ["Meme Sniper","Мем-снайпер","Мем-снайпер"],
            ["paid AI reviewer OFF","платный ИИ-аналитик выключен","платний ШІ-аналітик вимкнений"],
            ["Helius WebSocket OFF","Helius WebSocket выключен","Helius WebSocket вимкнено"],
            ["every 300 seconds","каждые 300 секунд","кожні 300 секунд"],
            ["Neither waits for whale events.","Ни один режим не ждёт сигналов от китов.","Жоден режим не чекає на сигнали від китів."],
            [" sec market scan"," с · сканирование рынка"," с · сканування ринку"],
            [" sec interval"," с · интервал"," с · інтервал"],
            ["analyzed","анализ выполнен","аналіз виконано"],
            ["connection unverified","соединение не проверено","з’єднання не перевірено"],
            ["whale-triggered","запущен сигналом кита","запущений сигналом кита"],
            ["Candidate source: scheduled market scanning; Helius WebSocket OFF.","Источник кандидатов: плановое сканирование рынка; Helius WebSocket выключен.","Джерело кандидатів: планове сканування ринку; Helius WebSocket вимкнено."],
            ["Whale-triggered analysis: ","Анализ после сигнала кита: ","Аналіз після сигналу кита: "],
            ["No OpenAI requests. Market data, RPC provider use and on-chain transaction fees may still have costs.","Запросов к OpenAI нет. Рыночные данные, RPC-провайдер и сетевые комиссии могут быть платными.","Запитів до OpenAI немає. Ринкові дані, RPC-провайдер і мережеві комісії можуть бути платними."],
            ["Last analysis timings · ","Время последнего анализа · ","Час останнього аналізу · "],
            ["market data ","рыночные данные ","ринкові дані "],
            ["analyst ","аналитик ","аналітик "],
            ["risk controller ","контролёр рисков ","контролер ризиків "],
            [" · total "," · всего "," · загалом "],
            ["Network confirmation is separate.","Подтверждение в сети происходит отдельно.","Підтвердження в мережі відбувається окремо."],
            ["Awaiting analysis","Ожидается анализ","Очікується аналіз"],
            ["A manual sale is already queued for this position.","Ручная продажа этой позиции уже поставлена в очередь.","Ручний продаж цієї позиції вже додано до черги."],
            ["A payment request is already open. Use the same wallet link; do not create or pay twice.","Запрос на оплату уже открыт. Используйте ту же ссылку на кошелёк; не создавайте новый запрос и не платите повторно.","Запит на оплату вже відкрито. Скористайтеся тим самим посиланням на гаманець; не створюйте новий запит і не сплачуйте повторно."],
            ["A withdrawal is already processing. Do not submit it again.","Вывод уже обрабатывается. Не отправляйте его повторно.","Виведення вже обробляється. Не надсилайте його повторно."],
            ["A withdrawal is already processing. Wait for its status; do not submit it again.","Вывод уже обрабатывается. Дождитесь обновления статуса; не отправляйте его повторно.","Виведення вже обробляється. Дочекайтеся оновлення статусу; не надсилайте його повторно."],
            ["An operation is awaiting Solana confirmation. The server checks it automatically; do not repeat it.","Операция ожидает подтверждения Solana. Сервер проверяет её автоматически; не повторяйте операцию.","Операція очікує підтвердження Solana. Сервер перевіряє її автоматично; не повторюйте операцію."],
            ["Approval result unknown.","Результат подтверждения неизвестен.","Результат підтвердження невідомий."],
            ["Approval submitted. Wait for finality.","Подтверждение отправлено. Дождитесь окончательного завершения.","Підтвердження надіслано. Дочекайтеся остаточного завершення."],
            ["Approval was not submitted.","Подтверждение не отправлено.","Підтвердження не надіслано."],
            ["Allocation not confirmed.","Распределение не подтверждено.","Розподіл не підтверджено."],
            ["Balance check unavailable.","Проверка баланса недоступна.","Перевірка балансу недоступна."],
            ["Base paper scan failed.","Сканирование Base завершилось ошибкой.","Сканування Base завершилося помилкою."],
            ["Could not cancel future charges.","Не удалось отменить будущие списания.","Не вдалося скасувати майбутні списання."],
            ["Could not cancel the queued retry.","Не удалось отменить повторную попытку в очереди.","Не вдалося скасувати повторну спробу в черзі."],
            ["Could not change the trading mode.","Не удалось изменить режим торговли.","Не вдалося змінити режим торгівлі."],
            ["Could not check access.","Не удалось проверить доступ.","Не вдалося перевірити доступ."],
            ["Could not check legacy operations.","Не удалось проверить старые операции.","Не вдалося перевірити старі операції."],
            ["Could not check the withdrawal status. Do not repeat it until history is refreshed.","Не удалось проверить статус вывода. Не повторяйте операцию, пока история не обновится.","Не вдалося перевірити статус виведення. Не повторюйте операцію, доки історія не оновиться."],
            ["Could not check verification.","Не удалось проверить подтверждение.","Не вдалося перевірити підтвердження."],
            ["Could not create a Solana Pay request. Check your account access and try again.","Не удалось создать запрос Solana Pay. Проверьте доступ к аккаунту и попробуйте снова.","Не вдалося створити запит Solana Pay. Перевірте доступ до облікового запису й спробуйте ще раз."],
            ["Could not create account.","Не удалось создать аккаунт.","Не вдалося створити обліковий запис."],
            ["Could not load Base paper simulation.","Не удалось загрузить симуляцию Base.","Не вдалося завантажити симуляцію Base."],
            ["Could not load fee status.","Не удалось загрузить статус комиссии.","Не вдалося завантажити статус комісії."],
            ["Could not load partner earnings.","Не удалось загрузить доход партнёров.","Не вдалося завантажити дохід партнерів."],
            ["Could not load wallet or payout status.","Не удалось загрузить статус кошелька или выплаты.","Не вдалося завантажити статус гаманця або виплати."],
            ["Could not open wallet verification.","Не удалось открыть подтверждение кошелька.","Не вдалося відкрити підтвердження гаманця."],
            ["Could not prepare the withdrawal request.","Не удалось подготовить запрос на вывод.","Не вдалося підготувати запит на виведення."],
            ["Could not preview old balances. No balances were changed.","Не удалось предварительно проверить старые балансы. Балансы не изменены.","Не вдалося попередньо перевірити старі баланси. Баланси не змінено."],
            ["Could not read the support service response. Please try again later.","Не удалось получить ответ службы поддержки. Попробуйте позже.","Не вдалося отримати відповідь служби підтримки. Спробуйте пізніше."],
            ["Could not read wallet coverage.","Не удалось проверить покрытие кошелька.","Не вдалося перевірити забезпечення гаманця."],
            ["Could not request a Telegram code.","Не удалось запросить код Telegram.","Не вдалося запросити код Telegram."],
            ["Could not save the monthly fee agreement.","Не удалось сохранить согласие на ежемесячную комиссию.","Не вдалося зберегти згоду на щомісячну комісію."],
            ["Could not save your agreement.","Не удалось сохранить ваше согласие.","Не вдалося зберегти вашу згоду."],
            ["Could not save your code.","Не удалось сохранить код.","Не вдалося зберегти код."],
            ["Could not send a Telegram code.","Не удалось отправить код Telegram.","Не вдалося надіслати код Telegram."],
            ["Could not send the support request. Please try again later.","Не удалось отправить обращение в поддержку. Попробуйте позже.","Не вдалося надіслати звернення до служби підтримки. Спробуйте пізніше."],
            ["Could not start the free trial.","Не удалось запустить бесплатный пробный период.","Не вдалося розпочати безкоштовний пробний період."],
            ["Could not start verification.","Не удалось начать проверку.","Не вдалося розпочати перевірку."],
            ["Could not submit the sale.","Не удалось отправить продажу.","Не вдалося надіслати продаж."],
            ["Could not verify account.","Не удалось проверить аккаунт.","Не вдалося перевірити обліковий запис."],
            ["Could not verify authorization. Reopen the app through Telegram.","Не удалось проверить авторизацию. Откройте приложение заново через Telegram.","Не вдалося перевірити авторизацію. Відкрийте застосунок знову через Telegram."],
            ["Fee status unavailable. No fee charge is confirmed; refresh before accepting.","Статус комиссии недоступен. Списание не подтверждено; обновите экран перед принятием.","Статус комісії недоступний. Списання не підтверджено; оновіть екран перед прийняттям."],
            ["Import could not be verified.","Не удалось проверить импорт.","Не вдалося перевірити імпорт."],
            ["Import was not confirmed.","Импорт не подтверждён.","Імпорт не підтверджено."],
            ["Independent policy unavailable. Do not enable real trading.","Независимая проверка правил недоступна. Не включайте реальную торговлю.","Незалежна перевірка правил недоступна. Не вмикайте реальну торгівлю."],
            ["No Solana transaction was sent. Run a fresh preview before trying again.","Транзакция Solana не отправлена. Перед новой попыткой создайте свежий предварительный просмотр.","Транзакцію Solana не надіслано. Перед новою спробою створіть новий попередній перегляд."],
            ["No response from server.","Нет ответа от сервера.","Немає відповіді від сервера."],
            ["Outcome unconfirmed. No refund or repeat broadcast.","Результат не подтверждён. Возврат средств и повторная отправка не выполнялись.","Результат не підтверджено. Повернення коштів і повторне надсилання не виконувалися."],
            ["Outcome unknown. Record preserved; no repeat broadcast. Do not delete it manually.","Результат неизвестен. Запись сохранена; повторной отправки не было. Не удаляйте запись вручную.","Результат невідомий. Запис збережено; повторного надсилання не було. Не видаляйте запис вручну."],
            ["Payment status check is temporarily unavailable. Do not pay again.","Проверка статуса оплаты временно недоступна. Не платите повторно.","Перевірка статусу оплати тимчасово недоступна. Не сплачуйте повторно."],
            ["Reserve insufficient.","Недостаточно средств в резерве.","Недостатньо коштів у резерві."],
            ["Reserve release not confirmed.","Возврат средств в резерв не подтверждён.","Повернення коштів до резерву не підтверджено."],
            ["Reserve sufficient for readiness checks.","Резерва достаточно для проверок готовности.","Резерву достатньо для перевірок готовності."],
            ["Sale awaiting confirmation","Продажа ожидает подтверждения","Продаж очікує підтвердження"],
            ["Sale submitted; waiting for Solana confirmation. Do not submit again.","Продажа отправлена; ожидается подтверждение Solana. Не отправляйте её повторно.","Продаж надіслано; очікується підтвердження Solana. Не надсилайте її повторно."],
            ["Server did not respond. This check does not start trading.","Сервер не ответил. Эта проверка не запускает торговлю.","Сервер не відповів. Ця перевірка не запускає торгівлю."],
            ["Server key does not match the wallet address.","Ключ сервера не соответствует адресу кошелька.","Ключ сервера не відповідає адресі гаманця."],
            ["Status check unavailable.","Проверка статуса недоступна.","Перевірка статусу недоступна."],
            ["Status unavailable.","Статус недоступен.","Статус недоступний."],
            ["Submission outcome unknown.","Результат отправки неизвестен.","Результат надсилання невідомий."],
            ["Submitted. Do not retry.","Отправлено. Не повторяйте операцию.","Надіслано. Не повторюйте операцію."],
            ["The SOL/USD quote changed. Review the updated amount before agreeing.","Котировка SOL/USD изменилась. Проверьте обновлённую сумму перед подтверждением.","Котирування SOL/USD змінилося. Перевірте оновлену суму перед підтвердженням."],
            ["The adjustment was not confirmed.","Корректировка не подтверждена.","Коригування не підтверджено."],
            ["Transfer was not submitted.","Перевод не отправлен.","Переказ не надіслано."],
            ["Verification expired or was replaced. Start again.","Срок проверки истёк или её заменили. Начните заново.","Строк перевірки минув або її замінили. Почніть знову."],
            ["Verification timed out. Start again when ready.","Время проверки истекло. Начните заново, когда будете готовы.","Час перевірки минув. Почніть знову, коли будете готові."],
            ["Waiting for payment confirmation. Do not send this payment again.","Ожидается подтверждение оплаты. Не отправляйте платёж повторно.","Очікується підтвердження оплати. Не надсилайте платіж повторно."],
            ["Withdrawal failed; no new transfer was sent.","Вывод не выполнен; новый перевод не отправлялся.","Виведення не виконано; нового переказу не надсилали."],
            ["Withdrawal is awaiting network confirmation. Do not submit it again.","Вывод ожидает подтверждения сети. Не отправляйте его повторно.","Виведення очікує підтвердження мережі. Не надсилайте його повторно."],
            ["Withdrawal status is being checked automatically. Do not submit it again.","Статус вывода проверяется автоматически. Не отправляйте его повторно.","Статус виведення перевіряється автоматично. Не надсилайте його повторно."],
            ["Withdrawal status is unclear. Do not submit it again; the server is checking automatically.","Статус вывода неясен. Не отправляйте его повторно; сервер проверяет операцию автоматически.","Статус виведення незрозумілий. Не надсилайте його повторно; сервер перевіряє операцію автоматично."],
            ["Withdrawal submitted. Status is checked automatically; do not submit it again.","Вывод отправлен. Статус проверяется автоматически; не отправляйте операцию повторно.","Виведення надіслано. Статус перевіряється автоматично; не надсилайте операцію повторно."],
            ["wallet funds do not cover recorded account balances","средств в кошельке недостаточно для зафиксированных балансов аккаунтов","коштів у гаманці недостатньо для зафіксованих балансів облікових записів"],
            ["The Solana transaction failed safe validation","Транзакция Solana не прошла безопасную проверку","Транзакція Solana не пройшла безпечну перевірку"],
            ["TRANSFERS: ","ПЕРЕВОДЫ: ","ПЕРЕКАЗИ: "],
            [" (ATA owner not created in this transaction)"," (аккаунт ATA не создан этой транзакцией)"," (обліковий запис ATA не створено цією транзакцією)"],
            ["; nothing was sent.","; ничего не отправлено.","; нічого не надіслано."],
            ["No pending legacy operations.","Нет ожидающих старых операций.","Немає очікуваних старих операцій."],
            ["No active shared-wallet operation.","Нет активной операции общего кошелька.","Немає активної операції спільного гаманця."],
            ["No bridge transfers yet.","Переводов через мост пока нет.","Переказів через міст поки немає."],
            ["Submission uncertain — check status","Отправка не подтверждена — проверьте статус","Надсилання не підтверджено — перевірте статус"],
            ["No current rule signals yet. A fresh chart scan is required; no AI retry is running.","Актуальных сигналов правил пока нет. Нужно новое сканирование графика; повторный запрос к ИИ не выполняется.","Актуальних сигналів правил поки немає. Потрібне нове сканування графіка; повторний запит до ШІ не виконується."],
            ["No current analysis.","Актуального анализа пока нет.","Актуального аналізу поки немає."],
            ["Server-side chart gate · BUY signals only · paid AI reviewer OFF","Серверная проверка графика · только сигналы BUY · платный ИИ-аналитик выключен","Серверна перевірка графіка · лише сигнали BUY · платний ШІ-аналітик вимкнений"],
            ["Independent risk & trend review · BUY only","Независимая проверка риска и тренда · только BUY","Незалежна перевірка ризику й тренду · лише BUY"],
            ["no BUY review in this analysis.","в этом анализе нет проверки BUY.","у цьому аналізі немає перевірки BUY."],
            ["APPROVED","ОДОБРЕНО","СХВАЛЕНО"],
            ["BLOCKED","ЗАБЛОКИРОВАНО","ЗАБЛОКОВАНО"],
            ["UP","ВВЕРХ","ВГОРУ"],
            ["DOWN","ВНИЗ","ВНИЗ"],
            ["RANGE","БОКОВОЙ ДИАПАЗОН","БОКОВИЙ ДІАПАЗОН"],
            ["Trade outcome history","История результатов сделок","Історія результатів угод"],
            ["Outcome memory","Память результатов","Пам’ять результатів"],
            ["finalized closed trades included in the latest analysis (up to ","окончательно закрытых сделок учтено в последнем анализе (до ","остаточно закритих угод враховано в останньому аналізі (до "],
            ["Fewer than ","Менее ","Менше "],
            [" outcomes per token/mode is limited evidence. "," результатов по токену/режиму — ограниченные данные. "," результатів за токеном/режимом — обмежені дані. "],
            ["Stored history only; no paid model training or profit guarantee. ","Только сохранённая история; платного обучения модели и гарантии прибыли нет. ","Лише збережена історія; платного навчання моделі й гарантії прибутку немає. "],
            ["Automatic exits do not need AI approval.","Для автоматического выхода одобрение ИИ не требуется.","Для автоматичного виходу схвалення ШІ не потрібне."],
            ["The second AI request shares the existing daily budget. Exits do not require its approval.","Второй запрос к ИИ расходует общий дневной лимит. Для выхода его одобрение не требуется.","Другий запит до ШІ використовує спільний денний ліміт. Для виходу його схвалення не потрібне."],
            ["No significant signal.","Значимого сигнала нет.","Значущого сигналу немає."],
            ["6h change ","Изменение за 6 ч ","Зміна за 6 год "],
            [" is above the ","выше ","вище "],
            [" floor.","нижней границы.","нижньої межі."],
            ["No eligible candidates.","Нет подходящих кандидатов.","Немає придатних кандидатів."],
            ["Hourly trend is declining.","Часовой тренд снижается.","Годинний тренд знижується."],
            ["Fresh support bounce confirmed.","Подтверждён отскок от свежего уровня поддержки.","Підтверджено відскок від свіжого рівня підтримки."],
            ["AI entry with recorded analysis","Вход по сигналу ИИ с сохранённым анализом","Вхід за сигналом ШІ зі збереженим аналізом"],
            ["Risk/trend check:","Проверка риска и тренда:","Перевірка ризику й тренду:"],
            ["Bound risk approval:","Подтверждение лимита риска:","Підтвердження ліміту ризику:"],
            [" · risk controller "," · контролёр рисков "," · контролер ризиків "],
            ["Last scan:","Последнее сканирование:","Останнє сканування:"],
            ["previously screened (not current eligibility).","ранее отобрано (это не подтверждает текущую пригодность).","перевірено раніше (це не підтверджує поточну придатність)."],
            ["Provider cooldown: ","Пауза провайдера: ","Пауза провайдера: "],
            [" seconds."," с."," с."],
            ["whale mints screened: ","mint китов проверено: ","mint китів перевірено: "],
            [" eligible)"," подходят)"," придатні)"],
            [" screened · "," проверено · "," перевірено · "],
            [" scanned · refreshed "," просканировано · обновлено "," проскановано · оновлено "],
            ["liquidity $","ликвидность $","ліквідність $"],
            ["held asset","актив уже в позиции","актив уже є в позиції"],
            ["Awaiting analysis","Ожидается анализ","Очікується аналіз"],
            ["01 / SOURCE","01 / ИСТОЧНИК","01 / ДЖЕРЕЛО"],
            ["02 / REVIEW","02 / ПРОВЕРКА","02 / ПЕРЕВІРКА"],
            ["03 / SETTLE","03 / ЗАЧИСЛЕНИЕ","03 / ЗАРАХУВАННЯ"],
            ["03 LEVELS","3 УРОВНЯ","3 РІВНІ"],
            ["30 days · 0.5 SOL","30 дней · 0,5 SOL","30 днів · 0,5 SOL"],
            ["7 days · 100,000 ZRL","7 дней · 100 000 ZRL","7 днів · 100 000 ZRL"],
            ["6-digit Telegram code","6-значный код Telegram","6-значний код Telegram"],
            ["Amount","Сумма","Сума"],
            ["Base network paper simulation","Виртуальная симуляция в сети Base","Віртуальна симуляція в мережі Base"],
            ["Partner commission rates and referrals by level","Ставки партнёрских комиссий и рефералы по уровням","Ставки партнерських комісій і реферали за рівнями"],
            ["Quick amount choices","Быстрый выбор суммы","Швидкий вибір суми"],
            ["Read-only quote result","Результат котировки без отправки","Результат котирування без надсилання"],
            ["SOL closed trade profit and loss","Прибыль и убыток по закрытым сделкам в SOL","Прибуток і збиток за закритими угодами в SOL"],
            ["Transaction signature or Solscan link","Подпись транзакции или ссылка Solscan","Підпис транзакції або посилання на Solscan"],
            ["USDC closed trade profit and loss","Прибыль и убыток по закрытым сделкам в USDC","Прибуток і збиток за закритими угодами в USDC"],
            ["What happened, and what did you expect?","Что произошло и чего вы ожидали?","Що сталося і чого ви очікували?"],
            ["Your nickname","Ваш псевдоним","Ваш псевдонім"],
            ["BUY","ПОКУПКА","КУПІВЛЯ"],
            ["SELL","ПРОДАЖ","ПРОДАЖ"],
            ["HOLD","УДЕРЖИВАТЬ","УТРИМУВАТИ"],
            ["3–24 letters, numbers or underscores. Your balance stays linked to your Telegram account, not this name.","От 3 до 24 латинских букв, цифр или подчёркиваний. Баланс привязан к аккаунту Telegram, а не к этому имени.","Від 3 до 24 латинських літер, цифр або підкреслень. Баланс прив’язаний до облікового запису Telegram, а не до цього імені."],
            ["Allocate my SOL to fees","Выделить мой SOL на сетевые комиссии","Виділити мій SOL на мережеві комісії"],
            ["Apply exact shortfall to my balance","Списать точную недостающую сумму с моего баланса","Списати точну суму нестачі з мого балансу"],
            ["Check current shared-wallet lock (read-only)","Проверить блокировку общего кошелька (только чтение)","Перевірити блокування спільного гаманця (лише читання)"],
            ["Close support form","Закрыть форму обращения в поддержку","Закрити форму звернення до підтримки"],
            ["Confirmed buy, sell, take-profit, and stop-loss alerts are sent to each participant's own Telegram chat. Enable Telegram notifications on your phone. Alerts are sent after Solana finalizes the trade; a safely failed stop-loss sale gets a separate alert saying the position remains open.","Уведомления о подтверждённых покупках, продажах, фиксации прибыли и стоп-лоссе отправляются в личный чат Telegram каждого участника. Включите уведомления Telegram на телефоне. Сообщение о сделке приходит после окончательного подтверждения в Solana; при безопасно отклонённой продаже по стоп-лоссу отдельно сообщается, что позиция остаётся открытой.","Сповіщення про підтверджені купівлі, продажі, фіксацію прибутку та стоп-лос сповіщення надсилаються в особистий чат Telegram кожного учасника. Увімкніть сповіщення Telegram на телефоні. Сповіщення про угоду надходить після остаточного підтвердження в Solana; якщо продаж за стоп-лосом безпечно відхилено, окремо повідомляється, що позиція залишається відкритою."],
            ["Continue to secure checkout","Перейти к защищённой оплате","Перейти до захищеної оплати"],
            ["Default: one analysis every 15 minutes, up to 96 attempted API calls per UTC day across the service, including failed calls. OpenAI API billing is separate and paid by the operator. Only market prices are sent to the model, not keys, Telegram identities or personal balances.","По умолчанию: один анализ каждые 15 минут, до 96 попыток вызова API в сутки по UTC для всего сервиса, включая неудачные запросы. API OpenAI оплачивает оператор отдельно. Модели передаются только рыночные цены, но не ключи, данные Telegram или личные балансы.","За замовчуванням: один аналіз кожні 15 хвилин, до 96 спроб виклику API на добу за UTC для всього сервісу, включно з невдалими запитами. API OpenAI окремо оплачує оператор. Моделі передаються лише ринкові ціни, а не ключі, дані Telegram чи особисті баланси."],
            ["Deposit Crypto (USDC)","Пополнить криптовалютой (USDC)","Поповнити криптовалютою (USDC)"],
            ["Deposit USDC by standard transfer and verify the transaction","Пополнить USDC обычным переводом и подтвердить транзакцию","Поповнити USDC звичайним переказом і підтвердити транзакцію"],
            ["Describe the issue · up to 700 characters","Опишите проблему · до 700 символов","Опишіть проблему · до 700 символів"],
            ["Each withdrawal is approved by a one-time code sent to your Telegram account. The code is tied to the exact amount, token and recipient address. Passkeys are used for standalone browser sign-in and account recovery, not for withdrawals.","Каждый вывод подтверждается одноразовым кодом, отправленным в ваш аккаунт Telegram. Код привязан к точной сумме, токену и адресу получателя. Ключи доступа используются для входа в браузере и восстановления аккаунта, но не для вывода средств.","Кожне виведення підтверджується одноразовим кодом, надісланим у ваш обліковий запис Telegram. Код прив’язаний до точної суми, токена й адреси отримувача. Ключі доступу використовуються для входу в браузері та відновлення облікового запису, але не для виведення коштів."],
            ["Enter the SOL amount to create a payment request.","Введите сумму SOL для создания запроса на оплату.","Введіть суму SOL, щоб створити запит на оплату."],
            ["I confirm these SOL belong to me, not to customers.","Подтверждаю, что этот SOL принадлежит мне, а не клиентам.","Підтверджую, що цей SOL належить мені, а не клієнтам."],
            ["Non-admin withdrawals have a 1.5% fee, credited to the administrator in the same currency.","При выводе для всех, кроме администратора, удерживается комиссия 1,5% и зачисляется администратору в той же валюте.","З виведення для всіх, крім адміністратора, утримується комісія 1,5% і зараховується адміністратору в тій самій валюті."],
            ["One JPG or PNG image, up to 5 MB. Images are sent directly and are not stored by the app.","Одно изображение JPG или PNG размером до 5 МБ. Изображения отправляются напрямую и не сохраняются приложением.","Одне зображення JPG або PNG розміром до 5 МБ. Зображення надсилаються безпосередньо й не зберігаються в застосунку."],
            ["Open verification · hold to copy link","Открыть проверку · удерживайте, чтобы скопировать ссылку","Відкрити перевірку · утримуйте, щоб скопіювати посилання"],
            ["Open wallet verification · hold to copy link","Открыть проверку кошелька · удерживайте, чтобы скопировать ссылку","Відкрити перевірку гаманця · утримуйте, щоб скопіювати посилання"],
            ["Partner earnings credit to your main balance","Партнёрские начисления зачисляются на основной баланс","Партнерські нарахування зараховуються на основний баланс"],
            ["Screened Solana candidates pass mint, liquidity, chart, quote and execution checks. Discovery checks up to 200 new mints and 40 on-chain candidates per scan; not every token is traded. Core and Meme Sniper remain separate.","Кандидаты Solana проходят проверку mint, ликвидности, графика, котировки и исполнения. При поиске проверяется до 200 новых mint и 40 кандидатов в сети за сканирование; торгуются не все токены. Core и Meme Sniper работают отдельно.","Кандидати Solana проходять перевірку mint, ліквідності, графіка, котирування та виконання. Під час пошуку перевіряється до 200 нових mint і 40 кандидатів у мережі за сканування; торгуються не всі токени. Core і Meme Sniper працюють окремо."],
            ["Send a private message and an optional screenshot directly to the administrator.","Отправьте личное сообщение и при необходимости снимок экрана напрямую администратору.","Надішліть особисте повідомлення та, за бажанням, знімок екрана безпосередньо адміністратору."],
            ["Send code in Telegram","Отправить код в Telegram","Надіслати код у Telegram"],
            ["Send to support","Отправить в поддержку","Надіслати до підтримки"],
            ["Set OPENAI_API_KEY only in Render Environment. Never enter secrets in chat or app fields. OPENAI_MODEL defaults to gpt-4.1; model availability and billing depend on the OpenAI account.","Добавляйте OPENAI_API_KEY только в переменные среды Render. Не вводите секреты в чат или поля приложения. По умолчанию OPENAI_MODEL — gpt-4.1; доступность модели и оплата зависят от аккаунта OpenAI.","Додавайте OPENAI_API_KEY лише до змінних середовища Render. Не вводьте секрети в чат або поля застосунку. За замовчуванням OPENAI_MODEL — gpt-4.1; доступність моделі та оплата залежать від облікового запису OpenAI."],
            ["Sniper entry filters: approved DEX, at least $10k pool liquidity, $100k 24h volume, 200 daily trades, pool age 24h+, extreme-pump exclusion, standard SPL mints with no mint/freeze authority, or the exact approved OTC Token-2022 mint with immutable metadata and disabled mint/freeze authorities. Other Token-2022 assets are rejected. The shortlist rotates every 15 minutes through up to 40 on-chain-verified candidates; AI sees up to 8 at a time. After any completed close, all participants are blocked from re-buying that mint for 24 hours. Fresh eligibility is required for every buy. Scores rank liquidity/activity, not expected profit. Holder concentration, LP locks and every contract/market risk are not fully checked. Mint checks do not guarantee token safety.","Фильтры входа Sniper: разрешённая DEX, ликвидность пула от $10 000, объём за 24 часа от $100 000, не менее 200 сделок в сутки, возраст пула от 24 часов и исключение резких пампов. Допускаются стандартные mint SPL без прав mint/freeze либо конкретный разрешённый OTC mint Token-2022 с неизменяемыми метаданными и отключёнными правами mint/freeze. Остальные активы Token-2022 отклоняются. Список обновляется каждые 15 минут: до 40 проверенных в сети кандидатов, из них ИИ анализирует до 8 за раз. После закрытия позиции всем участникам запрещено повторно покупать этот mint в течение 24 часов. Перед каждой покупкой требуется новая проверка. Оценка ранжирует ликвидность и активность, а не ожидаемую прибыль. Концентрация держателей, блокировка LP и все риски контракта/рынка проверяются не полностью. Проверка mint не гарантирует безопасность токена.","Фільтри входу Sniper: дозволена DEX, ліквідність пулу від $10 000, обсяг за 24 години від $100 000, щонайменше 200 угод на добу, вік пулу від 24 годин і виключення різких пампів. Дозволені стандартні mint SPL без прав mint/freeze або конкретний схвалений OTC mint Token-2022 з незмінними метаданими та вимкненими правами mint/freeze. Інші активи Token-2022 відхиляються. Список оновлюється кожні 15 хвилин: до 40 перевірених у мережі кандидатів, з яких ШІ аналізує до 8 за раз. Після закриття позиції всім учасникам заборонено повторно купувати цей mint протягом 24 годин. Перед кожною купівлею потрібна нова перевірка. Оцінка ранжує ліквідність та активність, а не очікуваний прибуток. Концентрація власників, блокування LP та всі ризики контракту/ринку перевіряються не повністю. Перевірка mint не гарантує безпеку токена."],
            ["Tap to view package details.","Нажмите, чтобы посмотреть описание пакета.","Натисніть, щоб переглянути опис пакета."],
            ["The engine starts only after you press ON and pass readiness checks. It is OFF after every server restart. If it is OFF, open positions remain open and can still be sold manually. Results are recorded per funding currency and exclude separately paid operator/network fees.","Модуль запускается только после нажатия ON и успешной проверки готовности. После каждого перезапуска сервера он выключен. Пока он выключен, открытые позиции сохраняются и их можно продать вручную. Результаты учитываются отдельно по валюте финансирования; отдельно оплаченные комиссии оператора и сети не включены.","Модуль запускається лише після натискання ON і успішної перевірки готовності. Після кожного перезапуску сервера його вимкнено. Поки він вимкнений, відкриті позиції зберігаються, і їх можна продати вручну. Результати обліковуються окремо за валютою фінансування; окремо сплачені комісії оператора та мережі не враховано."],
            ["Trading involves risk. No guaranteed returns.","Торговля связана с риском. Прибыль не гарантирована.","Торгівля пов’язана з ризиком. Прибуток не гарантований."],
            ["Trading signals","Торговые сигналы","Торгові сигнали"],
            ["Your SOL to allocate","Ваш SOL для распределения","Ваш SOL для розподілу"],
            ["Automatic exits: −5% stop; −2% trail after +3.5%. Manual sales available.","Автовыходы: стоп −5%; трейлинг −2% после +3,5%. Ручная продажа доступна.","Автовиходи: стоп −5%; трейл −2% після +3,5%. Ручний продаж доступний."],
            ["Exit rules & risk","Правила выхода и риски","Правила виходу та ризики"],
            ["Automatic exits only: −5% stop; after +3.5%, exit on a 2% pullback from the peak. AI SELL and resistance exits are disabled. Manual sales remain available. Thresholds are measured in this position's funding currency, not the USD chart; fills and profit are not guaranteed.","Только автоматические выходы: стоп −5%; после +3,5% — продажа при откате на 2% от пика. Продажи ИИ и выходы по сопротивлению отключены. Ручная продажа доступна. Пороговые значения считаются в валюте позиции, а не по графику в USD; исполнение и прибыль не гарантированы.","Лише автоматичні виходи: стоп −5%; після +3,5% — продаж при відкаті на 2% від піка. Продажі ШІ та виходи за рівнем опору вимкнені. Ручний продаж доступний. Порогові значення вимірюються у валюті позиції, а не за графіком у USD; виконання й прибуток не гарантовані."],
            ["Entry-cost cap: 2.50% of the order; sell exits are not blocked.","Лимит затрат на вход: 2,50% ордера; продажи этим фильтром не блокируются.","Ліміт витрат на вхід: 2,50% ордера; продажі цим фільтром не блокуються."],
            ["Entry-cost details","Расходы на вход","Витрати на вхід"],
            ["Core entry cost budget: 2.50% of the order. Includes quoted DEX fees, price impact, slippage, estimated network fees, Jito tips and account rent for entry/exit. Estimates do not guarantee profit or exit costs. SELL exits are not blocked by this filter.","Лимит затрат на вход Core — 2,50% суммы ордера. Учитываются котировки комиссий DEX, влияние на цену, проскальзывание, сетевые комиссии, чаевые Jito и аренда аккаунта при входе/выходе. Оценка не гарантирует прибыль или стоимость выхода. Фильтр не блокирует продажи.","Ліміт витрат на вхід Core — 2,50% суми ордера. Враховуються котирування комісій DEX, вплив на ціну, прослизання, мережеві комісії, чайові Jito та оренда акаунта під час входу/виходу. Оцінка не гарантує прибуток або вартість виходу."],
            ["Scan timing & costs","Частота сканирования и расходы","Частота сканування та витрати"],
            ["AI decisions & risk checks","Решения ИИ и проверки рисков","Рішення ШІ та перевірки ризиків"],
            ["Outcome history & chart levels","История результатов и уровни графика","Історія результатів і рівні графіка"],
            ["Candidates are screened before entry; not every candidate is traded.","Кандидаты проходят проверку перед входом; покупается не каждый.","Кандидати проходять перевірку перед входом; купується не кожен."],
            ["Candidate screening details","Как проверяются кандидаты","Як перевіряються кандидати"],
            ["Share profit","Поделиться прибылью","Поділитися прибутком"],
            ["Share a completed trade","Поделиться завершённой сделкой","Поділитися завершеною угодою"],
            ["Only the token symbol and finalized realized result are shared. No wallet address or transaction ID is included.","Будут переданы только тикер токена и подтверждённый итог сделки. Адрес кошелька и ID транзакции не включаются.","Буде передано лише тикер токена та підтверджений результат угоди. Адреса гаманця та ID транзакції не додаються."],
            ["Instagram · Telegram · Facebook · X (Twitter)","Instagram · Telegram · Facebook · X (Twitter)","Instagram · Telegram · Facebook · X (Twitter)"],
            ["The app never posts automatically. You choose the destination in the device share menu.","Приложение ничего не публикует автоматически. Вы сами выбираете площадку в меню отправки устройства.","Застосунок нічого не публікує автоматично. Ви самі обираєте платформу в меню поширення пристрою."],
            ["I reviewed this card and confirm sharing.","Я проверил карточку и подтверждаю отправку.","Я переглянув картку й підтверджую поширення."],
            ["Open device share menu","Открыть меню отправки","Відкрити меню поширення"],
            ["Copy caption","Скопировать подпись","Скопіювати текст"],
            ["Download card","Скачать карточку","Завантажити картку"],
            ["Caption","Подпись","Текст"],
            ["Cancel","Отмена","Скасувати"],
            ["Share card preview","Предпросмотр карточки сделки","Попередній перегляд картки угоди"],
            ["Close share dialog","Закрыть окно отправки","Закрити вікно поширення"],
            ["Preparing card…","Подготовка карточки…","Підготовка картки…"],
            ["Share menu closed. The app cannot verify that the post was published.","Меню отправки закрыто. Приложение не может проверить, была ли публикация размещена.","Меню поширення закрито. Застосунок не може перевірити, чи було опубліковано допис."],
            ["Sharing canceled; nothing was posted automatically.","Отправка отменена; приложение ничего не публиковало автоматически.","Поширення скасовано; застосунок нічого не публікував автоматично."],
            ["Sharing is unavailable here. Download the card and copy the caption to post manually.","Отправка здесь недоступна. Скачайте карточку и скопируйте подпись, чтобы опубликовать вручную.","Поширення тут недоступне. Завантажте картку й скопіюйте текст, щоб опублікувати вручну."],
            ["Caption copied.","Подпись скопирована.","Текст скопійовано."],
            ["Card download started.","Загрузка карточки началась.","Завантаження картки розпочалося."],
            ["Could not copy automatically. Select the caption and copy it manually.","Не удалось скопировать автоматически. Выделите подпись и скопируйте её вручную.","Не вдалося скопіювати автоматично. Виділіть текст і скопіюйте його вручну."],
            ["Could not prepare the image. You can still copy the caption.","Не удалось подготовить изображение. Вы всё ещё можете скопировать подпись.","Не вдалося підготувати зображення. Ви все ще можете скопіювати текст."],
            ["ZER0LIFE · AI TRADER","ZER0LIFE · AI TRADER","ZER0LIFE · AI TRADER"],
            ["Finalized trade","Подтверждённая закрытая сделка","Підтверджена закрита угода"],
            ["Realized profit","Реализованная прибыль","Реалізований прибуток"]
        ];
        const UI_COPY = {
            ru:Object.fromEntries([...UI_COPY_ROWS,...UI_COPY_ROWS_EXTRA].map(([english,russian])=>[english,russian])),
            uk:Object.fromEntries([...UI_COPY_ROWS,...UI_COPY_ROWS_EXTRA].map(([english,,ukrainian])=>[english,ukrainian]))
        };
        let currentUiLanguage='en';
        let tradeShareState=null;
        let exchangeQuoteDirection='solana-to-base',exchangeQuoteBusy=false,exchangeSourceAvailable=0;
        function uiLocale(){
            return currentUiLanguage==='uk'?'uk-UA':currentUiLanguage==='ru'?'ru-RU':undefined;
        }
        const uiTextSources=new WeakMap(),uiAttributeSources=new WeakMap();
        const uiTranslationPairs={};
        for(const language of ['ru','uk'])
            uiTranslationPairs[language]=Object.entries(UI_COPY[language]).sort((a,b)=>b[0].length-a[0].length);
        function escapeUiRegExp(value){
            return value.replace(/[.*+?^${}()|[\]\\]/g,'\\$&');
        }
        function localizeUiText(source,language=currentUiLanguage){
            if(language==='en'||!UI_COPY[language])return source;
            let output=String(source??'');
            for(const [english,translated] of uiTranslationPairs[language]){
                if(!output.includes(english))continue;
                if(/^[A-Za-z0-9_]+$/.test(english)){
                    const matcher=new RegExp('(^|[^A-Za-z0-9_])'+escapeUiRegExp(english)+'(?=$|[^A-Za-z0-9_])','g');
                    output=output.replace(matcher,(_match,prefix)=>prefix+translated);
                }else{
                    output=output.split(english).join(translated);
                }
            }
            return output;
        }
        const nativeUiAlert=window.alert.bind(window),nativeUiConfirm=window.confirm.bind(window),nativeUiPrompt=window.prompt.bind(window);
        window.alert=message=>nativeUiAlert(localizeUiText(String(message??'')));
        window.confirm=message=>nativeUiConfirm(localizeUiText(String(message??'')));
        window.prompt=(message,initial)=>nativeUiPrompt(localizeUiText(String(message??'')),
            initial===undefined?undefined:localizeUiText(String(initial)));
        function translateUiTree(root=document.body){
            if(!root)return;
            const walker=document.createTreeWalker(root,NodeFilter.SHOW_TEXT);
            let node;
            while((node=walker.nextNode())){
                if(node.parentElement?.closest('script,style'))continue;
                if(!uiTextSources.has(node))uiTextSources.set(node,node.nodeValue);
                const source=uiTextSources.get(node),leading=source.match(/^\\s*/)?.[0]||'';
                const trailing=source.match(/\\s*$/)?.[0]||'';
                const body=source.slice(leading.length,source.length-trailing.length);
                const next=leading+localizeUiText(body)+trailing;
                if(node.nodeValue!==next)node.nodeValue=next;
            }
            root.querySelectorAll?.('[placeholder],[title],[aria-label],[alt]').forEach(element=>{
                let state=uiAttributeSources.get(element);
                if(!state){state={source:{},applied:{}};uiAttributeSources.set(element,state);}
                for(const name of ['placeholder','title','aria-label','alt']){
                    if(!element.hasAttribute(name))continue;
                    const current=element.getAttribute(name);
                    if(!(name in state.source)||current!==state.applied[name])state.source[name]=current;
                    const next=localizeUiText(state.source[name]);state.applied[name]=next;
                    if(current!==next)element.setAttribute(name,next);
                }
            });
        }
        function applyUiLanguage(language){
            currentUiLanguage=['en','ru','uk'].includes(language)?language:'en';
            document.documentElement.lang=currentUiLanguage;
            document.documentElement.dataset.uiLanguage=currentUiLanguage;
            const select=document.getElementById('ui-language-select');
            if(select)select.value=currentUiLanguage;
            translateUiTree();
            if(latestBasePaperData)renderBasePaper(latestBasePaperData);
            setExchangeDirection(exchangeQuoteDirection);
            if(tradeShareState?.snapshot&&document.getElementById('trade-share-dialog')?.open)
                renderTradeSharePreview(tradeShareState);
        }
        async function saveUiLanguage(){
            const select=document.getElementById('ui-language-select'),status=document.getElementById('ui-language-status');
            const language=select?.value||'en';applyUiLanguage(language);status.textContent='Saving language preference…';
            try{
                const response=await apiFetch('/api/account/language',{method:'POST',headers:{'Content-Type':'application/json'},
                    body:JSON.stringify({language})});
                const data=await response.json();
                if(!response.ok||!data.success)throw new Error(data.message||'Could not save language preference.');
                status.textContent='Language preference saved.';
                if(accountState)accountState.ui_language=language;
            }catch(error){status.textContent=error.message||'Could not save language preference.';}
        }
        let languageMutationScheduled=false;
        const languageObserver=new MutationObserver(()=>{
            if(currentUiLanguage==='en'||languageMutationScheduled)return;
            languageMutationScheduled=true;
            requestAnimationFrame(()=>{languageMutationScheduled=false;translateUiTree();});
        });
        languageObserver.observe(document.body,{subtree:true,childList:true,characterData:true,attributes:true,
            attributeFilter:['placeholder','title','aria-label']});
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
        let investorBudgetDirty=false;
        let investorActive=false;
        let aiConfigured=false;
        let sniperActive=false;
        document.getElementById('settings-order').addEventListener('input',()=>{settingsOrderDirty=true;});
        document.getElementById('settings-sniper-order').addEventListener('input',()=>{settingsOrderDirty=true;});
        document.getElementById('investor-budget-usdc').addEventListener('input',()=>{investorBudgetDirty=true;});
        document.getElementById('investor-budget-sol').addEventListener('input',()=>{investorBudgetDirty=true;});
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
        async function saveInvestorBudget() {
            try {
                const response=await apiFetch('/api/investor_budget',{
                    method:'POST',
                    headers:{'Content-Type':'application/json'},
                    body:JSON.stringify({
                        usdc_total:document.getElementById('investor-budget-usdc').value||'0',
                        sol_total:document.getElementById('investor-budget-sol').value||'0'
                    })
                });
                const result=await response.json();
                if(!response.ok||!result.success)throw new Error(result.message);
                investorBudgetDirty=false;
                document.getElementById('investor-budget-status').textContent=result.message;
                await refresh();
            } catch(e) {
                document.getElementById('investor-budget-status').textContent=e.message;
            }
        }
        async function toggleInvestorTrading() {
            const enabling=!investorActive;
            if(enabling&&!confirm(
                'Enable real-money AI Crypto Investors? It uses only the reserved USDC/SOL budget, has no stop-loss, and can lose most or all of that budget. Autopilot turns OFF after a server restart.'
            ))return;
            const button=document.getElementById('investor-toggle');
            const status=document.getElementById('investor-toggle-status');
            if(button.disabled)return;
            if(status){status.hidden=true;status.textContent='';}
            button.disabled=true;
            button.setAttribute('aria-busy','true');
            button.textContent=localizeUiText('Checking scan status…');
            try {
                const response=await apiFetch('/api/toggle_investor',{method:'POST'});
                const result=await response.json();
                if(!response.ok||!result.success)throw new Error(result.message);
                if(status){status.hidden=true;status.textContent='';}
                await refresh();
            } catch(e) {
                const message=String(e.message||'Could not change AI Crypto Investors mode.');
                if(status){status.hidden=false;status.textContent=localizeUiText(message);}
                if(/candidate checks|candidate scan|market-data rate limit|market-data HTTP 429/i.test(message)){
                    await scanCandidates({waitForCompletion:false,refreshDashboard:false});
                    if(status){status.hidden=false;status.textContent=localizeUiText(message);}
                }else await refresh();
            } finally {
                button.disabled=false;
                button.removeAttribute('aria-busy');
                button.textContent=localizeUiText(
                    investorActive?'Autopilot: ON · Turn off':'Autopilot: OFF · Turn on');
            }
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
                        const referralCount=document.createElement('strong');referralCount.textContent=counts[index].toLocaleString(uiLocale());
                        const referralLabel=document.createElement('small');referralLabel.textContent='partners';
                        referralCell.append(referralCount,referralLabel);row.append(nameCell,rate,referralCell);levelRows.appendChild(row);
                    });
                }
                const totalReferrals=counts.reduce((total,count)=>total+count,0);
                if(networkTotal)networkTotal.textContent=totalReferrals.toLocaleString(uiLocale())+' partners';
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
        function normalizeAnalysisText(value) {
            return String(value||'').replace(/\\r\\n/g,'\n').replace(/\\n/g,'\n').replace(/\\r/g,'\n');
        }
        async function previewAI() {
            const box=document.getElementById('ai-decisions');box.textContent='Waiting for AI… No trades are sent.';
            try {
                const response=await apiFetch('/api/ai_preview',{method:'POST'});
                const result=await response.json();
                if(!response.ok||!result.success)throw new Error(result.message);
                box.textContent=Object.entries(result.decisions).map(([mode,d])=>(mode==='main'?'USDC':'SOL')+': '+d.action+' '+(d.symbol||'')+' — '+normalizeAnalysisText(d.reason)).join('\n');
                await refresh();
            } catch(e) {box.textContent=e.message;}
        }
        function renderCoreChartZones(context, labels) {
            const output=document.getElementById('ai-chart-zones');
            if(!output)return;
            const rows=Object.entries(context||{});
            if(!rows.length) {
                output.textContent='Support/resistance · 5m/1h: waiting for fresh OHLCV candles. Both modes require a confirmed support bounce or bounded rising-trend continuation.';
                return;
            }
            const price=value=>Number.isFinite(Number(value))&&Number(value)>0?Number(value).toPrecision(5):'not established';
            output.textContent='Support/resistance · 5m entry / 1h context · USD\n'+rows.map(([symbol,c])=>{
                const label=(labels||{})[symbol]||symbol;
                if(c.chart_ready!==1)
                    return label+': fresh candles unavailable ('+(c.chart_m5_candles||0)+' 5m / '+(c.chart_h1_candles||0)+' 1h); BUY blocked.';
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
                    +(c.chart_trend_continuation===1?' · setup: trend continuation':
                      c.chart_support_bounce===1?' · setup: support bounce':'')
                    +(c.chart_sell_setup===1?' · exit evidence near resistance':'');
            }).join('\n');
        }
        let withdrawSending = false;
        async function scanCandidates({waitForCompletion=false,refreshDashboard=true}={}) {
            const button=document.getElementById('scan-candidates');
            const status=document.getElementById('discovery-status');
            if(button)button.disabled=true;
            try {
                const readStatus=async()=>{
                    const response=await apiFetch('/api/meme_scan',{method:'POST'});
                    const data=await response.json();
                    if(!response.ok||!data.success)throw new Error(data.message||'Scan unavailable.');
                    return data;
                };
                const deadline=Date.now()+110000;
                let data=await readStatus();
                while(waitForCompletion&&data.scan_running&&Date.now()<deadline){
                    status.textContent=localizeUiText(data.message||'Candidate scan is running. No trade is submitted.');
                    await new Promise(resolve=>setTimeout(resolve,5000));
                    data=await readStatus();
                }
                if(data.scan_running){
                    status.textContent=localizeUiText('Candidate scan is still running. Investors remain OFF; no trade was sent. Check again shortly.');
                    return;
                }
                status.textContent=localizeUiText(data.message||'Scan unavailable.');
                if(refreshDashboard)await refresh();
            }catch(e){status.textContent=localizeUiText(e.message||'Scan unavailable.');}
            finally{if(button)button.disabled=false;}
        }

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
            document.getElementById('solpay-amount').disabled = locked;
            document.querySelectorAll('#solpay-presets button').forEach(button => button.disabled = locked);
            document.getElementById('solpay-create').disabled = locked;
        }
        function initializeSolanaPayControls() {
            const input = document.getElementById('solpay-amount');
            input.step = '0.000000001';
            input.min = '0.000000001';
            input.value = '0.1';
            document.getElementById('solpay-amount-label').textContent = 'Amount in SOL';
            const values = ['0.05', '0.1', '0.25', '0.5'];
            const presets = document.getElementById('solpay-presets');
            presets.replaceChildren(...values.map(value => {
                const button = document.createElement('button');
                button.type = 'button';
                button.className = 'token-btn';
                button.textContent = value + ' SOL';
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
            const decimals = 9;
            const validAmount = /^\d+(?:\.\d{1,9})?$/.test(amount);
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
                    body: JSON.stringify({token: 'SOL', amount})
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
            const openWallet = document.getElementById('solpay-open-wallet');
            const statusPage = document.getElementById('solpay-status-page');
            document.getElementById('solpay-checkout-amount').textContent = invoice.amount + ' ' + invoice.token;
            document.getElementById('solpay-recipient').textContent = invoice.deposit_wallet;
            document.getElementById('solpay-expiration').textContent =
                new Date(Number(invoice.expires_at) * 1000).toLocaleString(uiLocale());
            if (invoice.state === 'CREDITED') {
                openWallet.hidden = true;
                statusPage.hidden = true;
                setSolanaPayStatus('Payment confirmed and credited to your account.', 'success');
                lockSolanaPayInputs(false);
                const storageKey = solanaPayStorageKey();
                if (storageKey) { try { sessionStorage.removeItem(storageKey); } catch(e) {} }
                activeSolanaPayInvoice = null;
                if (solanaPayPollTimer) { clearInterval(solanaPayPollTimer); solanaPayPollTimer = null; }
                return;
            }
            if (invoice.token === 'USDC') {
                document.getElementById('solpay-checkout-amount').textContent = 'Use standard transfer';
                document.getElementById('solpay-recipient').textContent = invoice.deposit_wallet;
                document.getElementById('solpay-expiration').textContent = 'Not available for USDC';
                openWallet.hidden = true;
                statusPage.hidden = true;
                setSolanaPayStatus(
                    'USDC deposits use the standard transfer section below. Do not pay this old Solana Pay request.',
                    'error');
                lockSolanaPayInputs(false);
                const storageKey = solanaPayStorageKey();
                if (storageKey) { try { sessionStorage.removeItem(storageKey); } catch(e) {} }
                activeSolanaPayInvoice = null;
                if (solanaPayPollTimer) { clearInterval(solanaPayPollTimer); solanaPayPollTimer = null; }
                return;
            }
            const expired = Number(invoice.expires_at) <= Date.now() / 1000;
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
                if (invoice.token === 'USDC') {
                    await showSolanaPayInvoice(invoice);
                    return;
                }
                if (invoice.token === 'SOL') {
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
                        ? new Date(seconds * 1000).toLocaleString(uiLocale()) : 'unknown';
                    return 'Operation: ' + (op.kind || 'unknown') + ' / ' + (op.state || 'unknown')
                        + '\nCreated: ' + created
                        + '\nSignature: ' + (op.signature || 'not recorded');
                }).join('\n\n') + '\n\nRead-only check: no transaction was sent or changed.';
            } catch(e) {
                box.textContent = e.message || 'Could not read the current shared-wallet lock.';
            }
        }
        let ownerShortfallPreview = null;
        function rawAmountAtLeast(balance, amount) {
            const left=String(balance||'0').replace(/^0+(?=\d)/,'');
            const right=String(amount||'0').replace(/^0+(?=\d)/,'');
            return left.length>right.length || (left.length===right.length && left>=right);
        }
        async function checkBalanceCoverage() {
            const box=document.getElementById('balance-coverage-details');
            const preview=document.getElementById('owner-shortfall-preview');
            const applyButton=document.getElementById('owner-shortfall-apply');
            ownerShortfallPreview=null;
            applyButton.disabled=true;
            document.getElementById('owner-shortfall-confirm').checked=false;
            box.textContent='Reading finalized wallet balances and the ledger…';
            preview.textContent='Checking the exact shortfall and your available balance…';
            try {
                const response=await apiFetch('/api/admin/balance_check');
                const result=await response.json();
                if(!response.ok||!result.success)throw new Error(result.message||'Balance check unavailable.');
                const usdc=result.assets.USDC, sol=result.assets.SOL;
                ownerShortfallPreview={
                    usdc_raw:String(usdc.shortfall_raw),
                    sol_raw:String(sol.shortfall_raw),
                    usdc:usdc.shortfall,
                    sol:sol.shortfall,
                    usdc_available_raw:String(usdc.owner_available_raw),
                    sol_available_raw:String(sol.owner_available_raw),
                    usdc_available:usdc.owner_available,
                    sol_available:sol.owner_available
                };
                const custodyLines=[];
                if(result.split_wallet_enabled){
                    custodyLines.push('Hot wallet: '+usdc.hot_wallet+' USDC · '+sol.hot_wallet+' SOL');
                    custodyLines.push('Reserve wallet: '+usdc.reserve_wallet+' USDC · '+sol.reserve_wallet+' SOL');
                    custodyLines.push('Hot-wallet caps: '+result.hot_wallet_caps.USDC+
                        ' USDC · '+result.hot_wallet_caps.SOL+' SOL');
                }
                custodyLines.push('Combined wallets: USDC '+usdc.wallet+
                    ' · account balances '+usdc.ledger+' · difference '+usdc.difference);
                custodyLines.push('Combined wallets: SOL '+sol.wallet+
                    ' · account balances '+sol.ledger+' · difference '+sol.difference);
                const additionalAssets=(result.all_assets||[]).filter(asset=>
                    asset.label!=='USDC'&&asset.label!=='SOL');
                additionalAssets.forEach(asset=>custodyLines.push(
                    asset.label+': wallet '+asset.wallet+' · account balances '+asset.ledger+
                    ' · difference '+asset.difference));
                custodyLines.push('Read-only: no ledger or on-chain balance was changed.');
                box.textContent=custodyLines.join('\n');
                const otherDeficits=additionalAssets.filter(asset=>String(asset.shortfall_raw)!=='0');
                const hasShortfall=ownerShortfallPreview.usdc_raw!=='0'||ownerShortfallPreview.sol_raw!=='0';
                const sufficient=rawAmountAtLeast(ownerShortfallPreview.usdc_available_raw,ownerShortfallPreview.usdc_raw)&&
                    rawAmountAtLeast(ownerShortfallPreview.sol_available_raw,ownerShortfallPreview.sol_raw);
                preview.textContent=hasShortfall
                    ? 'Exact proposed reduction: '+ownerShortfallPreview.usdc+' USDC and '+ownerShortfallPreview.sol+
                      ' SOL. Your available balances: '+ownerShortfallPreview.usdc_available+' USDC and '+
                      ownerShortfallPreview.sol_available+' SOL.'+
                      (otherDeficits.length
                        ? ' Other asset shortfall blocks this adjustment: '+otherDeficits.map(asset=>
                            asset.label+' '+asset.shortfall).join(', ')+'.'
                        : sufficient?' Confirm below to continue.':' Your available balance is not enough to cover both shortfalls.')
                    : 'No wallet shortfall was found. No adjustment is available.';
                applyButton.disabled=!hasShortfall||!sufficient||otherDeficits.length>0;
            } catch(e) { box.textContent=e.message||'Could not read wallet coverage.'; }
        }
        async function applyOwnerShortfall() {
            const button=document.getElementById('owner-shortfall-apply');
            const resultBox=document.getElementById('owner-shortfall-result');
            const preview=ownerShortfallPreview;
            if(!preview||button.disabled)return;
            if(!document.getElementById('owner-shortfall-confirm').checked){
                resultBox.textContent='Confirm that only your own balance is being reduced.';return;
            }
            if(!confirm('Permanently reduce your available bot balance by '+preview.usdc+
                ' USDC and '+preview.sol+' SOL? This sends no transaction and does not affect customer balances.'))return;
            button.disabled=true;
            resultBox.textContent='Rechecking finalized wallet coverage before recording the owner-only adjustment…';
            try{
                const response=await apiFetch('/api/admin/owner_shortfall',{method:'POST',
                    headers:{'Content-Type':'application/json'},
                    body:JSON.stringify({
                        expected_usdc_shortfall_raw:preview.usdc_raw,
                        expected_sol_shortfall_raw:preview.sol_raw,
                        request_id:crypto.randomUUID(),
                        confirm:true
                    })});
                const result=await response.json();
                if(!response.ok||!result.success)throw new Error(result.message||'The adjustment was not confirmed.');
                document.getElementById('owner-shortfall-confirm').checked=false;
                resultBox.textContent=result.message+'\nUSDC difference: '+result.coverage.USDC.difference+
                    ' · SOL difference: '+result.coverage.SOL.difference+
                    (result.coverage.BASE_USDC?'\nBase USDC difference: '+result.coverage.BASE_USDC.difference:'')+
                    '\nYour remaining balance: '+result.owner_available_usdc+' USDC and '+
                    result.owner_available_sol+' SOL.';
                ownerShortfallPreview=null;
                await checkBalanceCoverage();
                await refresh();
            }catch(e){
                resultBox.textContent=(e.message||'No response from server.')+
                    '\nRefresh the read-only coverage check before trying again; do not repeat an uncertain adjustment.';
            }finally{
                button.disabled=true;
            }
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
            // Telegram identity takes precedence on the /terminal page too:
            // that route also sets ZRL_WEB for standalone-browser support.
            const telegramUser = tg?.initDataUnsafe?.user;
            if (tg?.initData && telegramUser?.id) {
                return { id: Number(telegramUser.id),
                    username: telegramUser.username || telegramUser.first_name || 'User' };
            }
            if(window.ZRL_WEB)return window.webSession?.user||null;
            return null;
        }

        async function apiFetch(url, options = {}) {
            // Never downgrade an in-Telegram request to a separate browser session.
            if (tg?.initData) {
                const headers = Object.assign({}, options.headers || {},
                    { 'X-Telegram-Init-Data': tg.initData });
                return fetch(url, Object.assign({}, options, { headers }));
            }
            if(window.ZRL_WEB){
                if(!window.webSession?.user)throw Error('Sign in to the web terminal.');
                return fetch(url,{...options,credentials:'same-origin',
                    headers:{...(options.headers||{}),'X-ZRL-CSRF':window.webSession.csrf}});
            }
            throw new Error('Open this app through Telegram.');
        }

        function openSupport(){
            const overlay=document.getElementById('support-overlay');
            overlay.hidden=false;
            document.getElementById('support-status').textContent='';
            document.getElementById('support-status').dataset.state='';
            setTimeout(()=>document.getElementById('support-message').focus(),0);
        }
        function closeSupport(){
            document.getElementById('support-overlay').hidden=true;
        }
        async function sendSupportRequest(){
            const message=document.getElementById('support-message').value.trim();
            const photo=document.getElementById('support-photo').files?.[0]||null;
            const button=document.getElementById('support-submit');
            const status=document.getElementById('support-status');
            const showError=text=>{status.textContent=text;status.dataset.state='error';};
            if(!message){showError('Describe the issue before sending.');return;}
            if(message.length>700){showError('Keep your message under 700 characters.');return;}
            if(photo){
                const allowedType=['image/jpeg','image/png'].includes(photo.type);
                const allowedName=/\.(jpe?g|png)$/i.test(photo.name||'');
                if(!allowedType&&!allowedName){showError('Attach a JPG or PNG image.');return;}
                if(photo.size>5*1024*1024){showError('Choose an image under 5 MB.');return;}
            }
            button.disabled=true;
            button.textContent='Sending…';
            status.textContent='Sending privately to the administrator…';
            status.dataset.state='';
            const form=new FormData();
            form.append('message',message);
            if(photo)form.append('photo',photo,photo.name);
            try{
                const response=await apiFetch('/api/support',{method:'POST',body:form});
                let data;
                try{data=await response.json();}
                catch{
                    if(response.status===413)throw Error('Choose an image under 5 MB.');
                    throw Error('Could not read the support service response. Please try again later.');
                }
                if(!response.ok||!data.success)throw Error(data.message||'Could not send the support request.');
                status.textContent=data.message||'Your support request was sent.';
                status.dataset.state='success';
                document.getElementById('support-message').value='';
                document.getElementById('support-photo').value='';
            }catch(error){
                showError(error.message||'Could not send the support request. Please try again later.');
            }finally{
                button.disabled=false;
                button.textContent='Send to support';
            }
        }

        let telegramTariffState=null,telegramTariffClockOffset=0,telegramTariffBusy=false;
        let selectedTelegramPackage=null;
        const telegramPackageDetails={
            zrl_7d:{title:'7-day AI Trader access',description:'100,000 ZRL · verified as a package payment; never added to trading balance.'},
            sol_30d:{title:'30-day AI Trader access',description:'0.5 SOL · verified as a package payment; never added to trading balance.'}
        };
        let telegramTariffDismissed=false,nextTelegramTariffCheck=0;
        async function refreshTelegramTariffs(){
            if((window.ZRL_WEB&&!tg?.initData)||telegramTariffBusy||!getUserData())return;
            telegramTariffBusy=true;
            try{
                const response=await apiFetch('/api/subscription'),data=await response.json();
                if(!response.ok||!data.success)throw Error(data.message||'Could not check access.');
                telegramTariffState=data;
                telegramTariffClockOffset=Number(data.server_time||0)*1000-Date.now();
                const status=document.getElementById('telegram-tariff-status');
                const note=document.getElementById('telegram-tariff-note');
                const checkout=document.getElementById('telegram-tariff-checkout');
                const trialButton=document.getElementById('telegram-trial-start');
                trialButton.hidden=!data.trial_available;
                if(data.admin_free){
                    status.textContent='Administrator access is permanent; package payment is not required.';
                    note.textContent='The public free-access countdown below is for other accounts. A subscription never changes trading admission or starts Core or Sniper.';
                }else if(data.trial_active){
                    status.textContent='Your one-time 3-day free trial is active until '+new Date(Number(data.trial_expires_at)*1000).toLocaleString(uiLocale())+'.';
                    note.textContent='The separate $5 monthly trading fee is waived during the trial. Access does not turn on Core or Sniper.';
                }else if(data.trial_available){
                    status.textContent='This new account qualifies for one 3-day free trial.';
                    note.textContent='Start it once to receive 72 hours of access. The $5 monthly trading fee is waived during the trial; Core and Sniper remain unchanged.';
                }else if(data.free_access){
                    status.textContent='Free AI Trader subscription is active for everyone through 1 November 2026.';
                    note.textContent='No payment is needed before the countdown ends. Payment is not credited to trading balance and never turns on Core or Sniper.';
                }else if(data.active){
                    status.textContent='Paid access is active until '+new Date(Number(data.expires_at)*1000).toLocaleString(uiLocale())+'.';
                    note.textContent='Access renewal does not change trading admission or start trading modes.';
                }else{
                    status.textContent='Free access has ended. Choose a package to request continued access.';
                    note.textContent=data.purchase_available?
                        'Checkout uses a separate browser passkey flow. The exact payment is verified before access is added; it is never credited to trading balance.':
                        'This account is not admitted for paid trading. A subscription does not change custody or trading admission.';
                }
                checkout.hidden=!data.purchase_available||data.admin_free||data.active||data.free_access;
                const modalCheckout=document.getElementById('telegram-tariff-modal-checkout');
                modalCheckout.hidden=!data.purchase_available||data.admin_free||data.free_access
                    ||data.trial_available||data.trial_active;
                if(!data.payment_required)telegramTariffDismissed=false;
                const modal=document.getElementById('telegram-tariff-modal');
                if(data.payment_required&&!data.trial_available&&!telegramTariffDismissed){
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
        async function startTelegramTrial(){
            const button=document.getElementById('telegram-trial-start');
            if(!telegramTariffState?.trial_available||button.disabled)return;
            button.disabled=true;
            button.textContent='Starting your trial…';
            try{
                const response=await apiFetch('/api/subscription/trial',{
                    method:'POST',headers:{'Content-Type':'application/json'},body:'{}'
                });
                const data=await response.json();
                if(!response.ok||!data.success)throw Error(data.message||'Could not start the free trial.');
                await refreshTelegramTariffs();
                refreshMonthlyTradingFee(true);
            }catch(error){
                alert(error.message||'Could not start the free trial.');
                button.disabled=false;
                button.textContent='Start one-time 3-day free trial';
            }
        }
        document.getElementById('telegram-trial-start')?.addEventListener('click',startTelegramTrial);
        function tickTelegramTariffCountdown(){
            if((window.ZRL_WEB&&!tg?.initData)||!telegramTariffState)return;
            const label=document.getElementById('telegram-tariff-countdown');
            if(telegramTariffState.trial_active&&telegramTariffState.trial_expires_at){
                const trialSeconds=Number(telegramTariffState.trial_expires_at)-
                    Math.floor((Date.now()+telegramTariffClockOffset)/1000);
                if(trialSeconds<=0){
                    label.textContent='Your free trial has ended. Checking access…';
                    if(Date.now()>=nextTelegramTariffCheck){
                        nextTelegramTariffCheck=Date.now()+10000;refreshTelegramTariffs();
                    }
                    return;
                }
                const trialDays=Math.floor(trialSeconds/86400);
                const trialHours=Math.floor(trialSeconds%86400/3600);
                const trialMinutes=Math.floor(trialSeconds%3600/60);
                const trialRemainder=trialSeconds%60;
                label.textContent=`Free trial ends in ${trialDays}d ${trialHours}h ${trialMinutes}m ${trialRemainder}s.`;
                return;
            }
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
            if(!telegramTariffState?.purchase_available||telegramTariffState.admin_free
                    ||telegramTariffState.free_access||telegramTariffState.trial_active)return;
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
            }else if(data.trial_active){
                status.textContent='Your 3-day free trial is active until '+new Date(Number(data.trial_expires_at)*1000).toLocaleString(uiLocale())+'. No package payment is due yet.';
            }else if(data.free_access){
                status.textContent='Your free access is still active. Checkout opens after the public free-access period ends.';
            }else if(!data.purchase_available){
                status.textContent='This account is not admitted for paid access. Payment would not change custody or trading admission.';
            }else if(data.active){
                status.textContent='Your access is active until '+new Date(Number(data.expires_at)*1000).toLocaleString(uiLocale())+'. You can renew securely.';
            }else{
                status.textContent='Continue to the secure browser checkout to create an exact payment invoice.';
            }
            checkout.hidden=!data?.purchase_available||data.admin_free||data.free_access||data.trial_active;
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
            applyUiLanguage(data.ui_language||'en');
            document.getElementById('passkey-state').textContent='Telegram code required for withdrawals. '+
                (data.has_passkey?'Browser passkey active · '+data.passkey_count+' key(s) · '+data.recovery_codes_remaining+' recovery codes remaining.':
                    'No browser passkey set up. Telegram withdrawals remain available.');
            return data;
        }
        async function openTerminal() {
            // Telegram Mini App users keep their original Telegram identity and account.
            // Standalone browser sessions are required only outside Telegram.
            if(window.ZRL_WEB&&!window.Telegram?.WebApp?.initData&&!window.webSession?.user){
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
            const button=document.getElementById('account-create');
            const error=document.getElementById('account-error');
            const risk=document.getElementById('account-risk');
            const partnerTerms=document.getElementById('account-partner-terms');
            const partnerTermsSection=document.getElementById('affiliate-terms');
            error.textContent='';
            if(!risk.checked){
                error.textContent='Please acknowledge the trading risk before creating your account.';
                risk.focus();return;
            }
            if(!partnerTermsSection.hidden&&!partnerTerms.checked){
                error.textContent='Please check the required referral commission agreement above to continue.';
                partnerTerms.closest('label')?.scrollIntoView({block:'center',behavior:'smooth'});
                partnerTerms.focus({preventScroll:true});return;
            }
            button.disabled=true;
            try{
                const response=await apiFetch('/api/account/register',{method:'POST',headers:{'Content-Type':'application/json'},
                    body:JSON.stringify({nickname:document.getElementById('account-nickname').value,
                        accept_risk:risk.checked,
                        accept_partner_terms:partnerTerms.checked})});
                const data=await response.json();if(!response.ok||!data.success)throw Error(data.message||'Could not create account.');
                accountState=data;document.getElementById('account-signup').style.display='none';
                await finishAccount();
            }catch(e){error.textContent=e.message;document.getElementById('account-signup').style.display='block';}
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
            if(!window.ZRL_WEB||tg?.initData)refreshTelegramTariffs();
        }

        function gotoTab(id, btn) {
            document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
            document.querySelectorAll('.nav-btn').forEach(b => b.classList.remove('active'));
            const target = document.getElementById('tab-' + id);
            if (target) target.classList.add('active');
            if (btn) btn.classList.add('active');
            if(id==='tariffs'&&(!window.ZRL_WEB||tg?.initData)){
                document.getElementById('telegram-tariff-view').hidden=false;
                refreshTelegramTariffs();
            }
            if(id==='wallet')refreshMonthlyTradingFee(true);
            if(id==='exchange')refreshExchangeBalances();
        }

        function clearExchangeQuote(){
            const result=document.getElementById('exchange-quote-result');
            const status=document.getElementById('exchange-quote-status');
            if(result)result.hidden=true;
            if(status)status.textContent='';
            document.getElementById('exchange-preview-out').textContent='—';
            document.getElementById('exchange-status').textContent=
                localizeUiText('QUOTE · CONFIRM TO EXCHANGE');
        }
        function setExchangeDirection(direction) {
            exchangeQuoteDirection=direction==='base-to-solana'?'base-to-solana':'solana-to-base';
            const reverse = exchangeQuoteDirection === 'base-to-solana';
            const source = reverse ? 'Base' : 'Solana';
            const destination = reverse ? 'Solana' : 'Base';
            clearExchangeQuote();
            document.getElementById('exchange-direction-title').textContent = source + ' → ' + destination;
            document.getElementById('exchange-dir-solana-base').setAttribute('aria-pressed', String(!reverse));
            document.getElementById('exchange-dir-base-solana').setAttribute('aria-pressed', String(reverse));
            document.getElementById('exchange-source-chain').textContent=source+' · USDC';
            document.getElementById('exchange-destination-chain').textContent=
                localizeUiText(destination+' · account balance');
            refreshExchangeBalances();
        }

        function exchangeQuoteNumber(value){
            const amount=Number(value);
            if(!Number.isFinite(amount))return '—';
            return amount.toLocaleString(uiLocale(),{minimumFractionDigits:2,maximumFractionDigits:6});
        }
        function exchangeQuoteAmount(value){
            const formatted=exchangeQuoteNumber(value);
            return formatted==='—'?formatted:formatted+' USDC';
        }
        function updateExchangePercentButtons(){
            document.querySelectorAll('[data-exchange-percent]').forEach(button=>{
                button.disabled=!Number.isFinite(exchangeSourceAvailable)||exchangeSourceAvailable<0.000001;
            });
        }
        function setExchangePercent(percent){
            if(!Number.isFinite(exchangeSourceAvailable)||exchangeSourceAvailable<=0)return;
            const raw=Math.floor(exchangeSourceAvailable*1_000_000*Number(percent)/100);
            if(raw<1)return;
            document.getElementById('exchange-amount').value=(raw/1_000_000).toFixed(6);
            clearExchangeQuote();
        }
        async function refreshExchangeBalances(){
            const balanceNode=document.getElementById('exchange-source-available');
            if(!balanceNode)return;
            try{
                const response=await apiFetch('/api/exchange/balances',{cache:'no-store'});
                const data=await response.json();
                if(!response.ok||data.success!==true)throw Error(data.message||'Balance unavailable.');
                const raw=Number(exchangeQuoteDirection==='base-to-solana'
                    ?data.base_usdc:data.solana_usdc);
                if(!Number.isFinite(raw)||raw<0)throw Error('Balance unavailable.');
                exchangeSourceAvailable=raw;
                balanceNode.textContent=localizeUiText('Available: ')+exchangeQuoteAmount(raw);
                updateExchangePercentButtons();
            }catch(_error){
                exchangeSourceAvailable=0;
                balanceNode.textContent=localizeUiText('Available balance unavailable.');
                updateExchangePercentButtons();
            }
        }
        function renderExchangeQuote(quote){
            if(!quote||quote.quote_only!==true||quote.transfer_enabled!==false
                ||quote.direction!==exchangeQuoteDirection)
                throw Error('Could not get a quote. No transaction was sent.');
            const live=quote.live_submission_enabled===true&&quote.sufficient_source_balance!==false;
            document.getElementById('exchange-status').textContent=localizeUiText(
                live?'LIVE · PRESS TO EXCHANGE':'QUOTE ONLY · SEND OFF');
            document.getElementById('exchange-quote-in').textContent=exchangeQuoteAmount(quote.amount_in);
            document.getElementById('exchange-preview-out').textContent=exchangeQuoteNumber(quote.amount_out);
            document.getElementById('exchange-quote-min').textContent=exchangeQuoteAmount(quote.amount_out_min);
            exchangeSourceAvailable=Number(quote.source_available)||0;
            document.getElementById('exchange-source-available').textContent=
                localizeUiText('Available: ')+exchangeQuoteAmount(exchangeSourceAvailable);
            updateExchangePercentButtons();
            document.getElementById('exchange-quote-provider').textContent=
                localizeUiText('Quote provider')+': '+String(quote.provider||'LI.FI').slice(0,48);
            const expires=new Date(Number(quote.expires_at)*1000);
            document.getElementById('exchange-quote-expiry').textContent=
                localizeUiText('Quote expires')+': '+(Number.isNaN(expires.getTime())?'—':expires.toLocaleTimeString(uiLocale()));

            const list=document.getElementById('exchange-quote-fees');
            list.replaceChildren();
            const fees=Array.isArray(quote.provider_fees)?quote.provider_fees:[];
            const totalFees=fees.reduce((sum,fee)=>{
                const amount=Number(fee?.amount_usd);
                return sum+(Number.isFinite(amount)&&amount>0?amount:0);
            },0);
            document.getElementById('exchange-quote-total-fees').textContent=
                '$'+totalFees.toFixed(4)+' USD';
            if(!fees.length){
                const item=document.createElement('li');
                item.textContent=localizeUiText('No fee breakdown was returned.');
                list.appendChild(item);
            }else{
                for(const fee of fees.slice(0,24)){
                    const item=document.createElement('li');
                    const feeKind=fee.kind==='network'?'Network fee':'Provider fee';
                    const usd=Number(fee.amount_usd);
                    const cost=Number.isFinite(usd)?'$'+usd.toFixed(4):'—';
                    const included=fee.included?' · '+localizeUiText('included'):'';
                    item.textContent=localizeUiText(feeKind)+': '+
                        String(fee.name||feeKind).slice(0,80)+' · '+cost+' '+String(fee.symbol||'').slice(0,16)+included;
                    list.appendChild(item);
                }
            }
            const warnings=[];
            if(quote.sufficient_source_balance===false)
                warnings.push(localizeUiText('The amount is above your available in-app balance. This is only an estimate; no transfer can be sent.'));
            if(quote.requires_approval===true)
                warnings.push(localizeUiText('This Base quote would require USDC approval before a transfer. No approval was sent.'));
            if(quote.direction==='solana-to-base')
                warnings.push(localizeUiText('LI.FI receives USDC at a temporary Solana address, then routes it to the displayed Base wallet. The Solana transaction does not prove LI.FI will complete delivery. Confirm only if you accept this provider risk.'));
            document.getElementById('exchange-quote-warning').textContent=warnings.join(' ');
            const routeDetails=[];
            if(quote.provider_solana_deposit_address)
                routeDetails.push(localizeUiText('LI.FI temporary Solana deposit')+': '+
                    String(quote.provider_solana_deposit_address));
            if(quote.destination_address)
                routeDetails.push(localizeUiText('Destination wallet')+': '+String(quote.destination_address));
            if(quote.solana_provider_fee_usdc)
                routeDetails.push(localizeUiText('LI.FI Solana fee')+': '+
                    exchangeQuoteAmount(quote.solana_provider_fee_usdc));
            document.getElementById('exchange-quote-route-details').textContent=routeDetails.join(' · ');
            const actions=document.getElementById('exchange-live-actions');
            actions.replaceChildren();
            if(live){
                const action=document.createElement('button');
                action.type='button';
                action.className='exchange-quote-button';
                if(quote.direction==='base-to-solana'&&quote.requires_approval===true){
                    action.textContent=localizeUiText('Approve exact USDC amount');
                    action.onclick=()=>confirmExchangeApproval(quote,action);
                }else{
                    action.textContent=localizeUiText('Exchange');
                    action.onclick=()=>confirmExchangeTransfer(quote,action);
                }
                actions.appendChild(action);
                document.getElementById('exchange-quote-status').textContent=
                    localizeUiText('The button confirms this transfer. The bot signs, sends, and tracks it automatically.');
            }else{
                document.getElementById('exchange-quote-status').textContent=
                    localizeUiText('Quote only. Live submission is not configured.');
            }
            document.getElementById('exchange-quote-result').hidden=false;
        }
        async function confirmExchangeTransfer(quote,button){
            button.disabled=true;
            button.textContent=localizeUiText('Submitting…');
            try{
                const response=await apiFetch('/api/exchange/submit',{
                    method:'POST',headers:{'Content-Type':'application/json'},
                    body:JSON.stringify({quote_id:quote.quote_id,confirm:true})
                });
                const result=await response.json();
                if(!response.ok||result.success!==true)throw Error(result.message||'Transfer was not submitted.');
                document.getElementById('exchange-quote-status').textContent=result.message||'Submitted. Do not retry.';
                button.remove();
                await refreshExchangeTransfers();
            }catch(error){
                button.disabled=true;
                button.textContent=localizeUiText('Submission uncertain — check status');
                document.getElementById('exchange-quote-status').textContent=
                    (error.message||'Submission outcome unknown.')+' Do not submit again.';
            }
        }
        async function confirmExchangeApproval(quote,button){
            if(!window.confirm('Approve exactly '+quote.amount_in+
                ' USDC for this Base bridge? This approval alone does not send USDC.'))return;
            button.disabled=true;
            button.textContent=localizeUiText('Submitting approval…');
            try{
                const response=await apiFetch('/api/exchange/approve',{
                    method:'POST',headers:{'Content-Type':'application/json'},
                    body:JSON.stringify({quote_id:quote.quote_id,confirm_approval:true})
                });
                const result=await response.json();
                if(!response.ok||result.success!==true)throw Error(result.message||'Approval was not submitted.');
                document.getElementById('exchange-quote-status').textContent=result.message||'Approval submitted. Wait for finality.';
                button.remove();
                await refreshExchangeTransfers();
            }catch(error){
                button.disabled=true;
                document.getElementById('exchange-quote-status').textContent=
                    (error.message||'Approval result unknown.')+' Do not retry.';
            }
        }
        function renderExchangeTransferProof(transfer){
            const panel=document.getElementById('exchange-transfer-proof');
            panel.replaceChildren();
            if(!transfer){panel.hidden=true;return;}
            const sourceIsSolana=transfer.direction==='solana-to-base';
            const transactions=[
                {hash:transfer.source_tx_hash,chain:sourceIsSolana?'solana':'base',role:'source'},
                {hash:transfer.destination_tx_hash,chain:sourceIsSolana?'base':'solana',role:'destination'}
            ];
            let shown=0;
            for(const transaction of transactions){
                const hash=String(transaction.hash||'');
                const valid=transaction.chain==='solana'
                    ?/^[1-9A-HJ-NP-Za-km-z]{80,90}$/.test(hash)
                    :/^0x[a-fA-F0-9]{64}$/.test(hash);
                if(!valid)continue;
                const row=document.createElement('div');
                const label=document.createElement('strong');
                let labelText=transaction.chain==='solana'
                    ?transaction.role==='source'?'Solana source transaction':'Solana destination transaction'
                    :transaction.role==='source'?'Base source transaction':'Base destination transaction';
                if(transaction.role==='source'&&transfer.state==='UNKNOWN'){
                    labelText=transaction.chain==='solana'
                        ?'Solana source signature · broadcast not confirmed'
                        :'Base source signature · broadcast not confirmed';
                }else if(transaction.role==='source'&&transfer.expired_unbroadcast===true){
                    labelText='Solana source signature expired · transaction not found';
                }else if(transaction.role==='source'&&transfer.state==='SUBMITTED'){
                    labelText=transaction.chain==='solana'
                        ?'Solana source transaction · RPC accepted; confirmation pending'
                        :'Base source transaction · RPC accepted; confirmation pending';
                }
                label.textContent=localizeUiText(labelText);
                const link=document.createElement('a');
                link.href=transaction.chain==='solana'
                    ?'https://solscan.io/tx/'+encodeURIComponent(hash)
                    :'https://basescan.org/tx/'+encodeURIComponent(hash);
                link.target='_blank';
                link.rel='noopener noreferrer';
                link.textContent=hash;
                row.append(label,document.createTextNode(' · '),link);
                panel.append(row);
                shown++;
            }
            if(transfer.submission_error){
                const note=document.createElement('div');
                note.textContent=localizeUiText('Submission RPC error:')+' '+
                    String(transfer.submission_error).slice(0,300);
                panel.append(note);
                shown++;
            }
            if(shown===0&&['SIGNED','SUBMITTED','UNKNOWN','NEEDS_REVIEW'].includes(transfer.state)){
                const note=document.createElement('div');
                note.textContent=localizeUiText(
                    'The bot has not recorded a source transaction signature. Funds remain reserved; do not submit again.');
                panel.append(note);
                shown++;
            }
            panel.hidden=shown===0;
        }
        async function refreshExchangeTransfers(){
            const status=document.getElementById('exchange-quote-status');
            status.textContent=localizeUiText('Checking bridge status…');
            try{
                const response=await apiFetch('/api/exchange/transfers');
                const result=await response.json();
                if(!response.ok||result.success!==true)throw Error(result.message||'Status check failed.');
                const transfers=Array.isArray(result.transfers)?result.transfers:[];
                const approvals=Array.isArray(result.approvals)?result.approvals:[];
                const latest=transfers[0], approval=approvals[0];
                renderExchangeTransferProof(latest);
                status.textContent=latest
                    ?latest.direction.replace(/-/g,' → ')+' · '+latest.state+' · '+
                        (latest.status_message
                            ?localizeUiText(latest.status_message)
                            :latest.status_check_error
                            ?localizeUiText('Status check failed:')+' '+localizeUiText(latest.status_check_error)+
                                ' '+localizeUiText('Funds remain reserved. Do not submit again.')
                            :latest.received_raw==null?'destination pending':
                                (Number(latest.received_raw)/1e6).toFixed(6)+' USDC credited')
                    :approval?'Base approval · '+approval.state:'No bridge transfers yet.';
                await refreshExchangeBalances();
            }catch(error){
                renderExchangeTransferProof(null);
                status.textContent=(error.message||'Status unavailable.')+' Do not retry an unresolved transfer.';
            }
        }
        async function requestExchangeQuote(){
            if(exchangeQuoteBusy)return;
            const button=document.getElementById('exchange-quote-button');
            const status=document.getElementById('exchange-quote-status');
            const amount=document.getElementById('exchange-amount').value.trim();
            exchangeQuoteBusy=true;
            button.disabled=true;
            status.textContent=localizeUiText('Requesting a live quote…');
            clearExchangeQuote();
            status.textContent=localizeUiText('Requesting a live quote…');
            try{
                const response=await apiFetch('/api/exchange/quote',{
                    method:'POST',
                    headers:{'Content-Type':'application/json'},
                    body:JSON.stringify({direction:exchangeQuoteDirection,amount})
                });
                const data=await response.json();
                if(!response.ok||!data.success)
                    throw Error(data.message||'Could not get a quote. No transaction was sent.');
                renderExchangeQuote(data.quote);
                if(data.quote.live_submission_enabled!==true)
                    status.textContent=localizeUiText('Quote only. Live submission is not configured.');
            }catch(error){
                status.textContent=localizeUiText(error.message||'Could not get a quote. No transaction was sent.');
                document.getElementById('exchange-quote-result').hidden=true;
            }finally{
                exchangeQuoteBusy=false;
                button.disabled=false;
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

        let monthlyFeeLastRefresh=0,monthlyFeeLoading=false,monthlyFeeQuoteAvailable=false,monthlyFeeState=null;
        function updateMonthlyFeeAcceptButton(){
            const button=document.getElementById('monthly-trading-fee-accept');
            const checkbox=document.getElementById('monthly-trading-fee-agree');
            if(button&&checkbox)button.disabled=!monthlyFeeQuoteAvailable||!checkbox.checked||monthlyFeeLoading;
        }
        function monthlyFeeDate(value){
            const stamp=Number(value||0);
            return stamp>0?new Date(stamp*1000).toLocaleString(uiLocale()):'Not scheduled';
        }
        async function refreshMonthlyTradingFee(force=false){
            if(monthlyFeeLoading||!getUserData()||(!force&&Date.now()-monthlyFeeLastRefresh<5000))return;
            monthlyFeeLoading=true;
            try{
                const response=await apiFetch('/api/monthly-trading-fee',{cache:'no-store'});
                const data=await response.json();
                if(!response.ok||!data.success)throw Error(data.error||data.message||'Could not load fee status.');
                monthlyFeeState=data;
                monthlyFeeLastRefresh=Date.now();
                monthlyFeeQuoteAvailable=data.quote_available===true&&Number.isSafeInteger(data.quote_raw);
                const statusEl=document.getElementById('monthly-trading-fee-status');
                const quoteEl=document.getElementById('monthly-trading-fee-quote');
                const consentBox=document.getElementById('monthly-trading-fee-consent');
                const cancelButton=document.getElementById('monthly-trading-fee-cancel');
                if(data.is_owner){
                    statusEl.textContent='Administrator account: this recurring trading fee does not apply.';
                    consentBox.hidden=true;cancelButton.hidden=true;
                    return;
                }
                if(data.trial_fee_waived){
                    statusEl.textContent='No $5 monthly trading fee is charged during your 3-day free trial. After the trial, recurring charges apply only if you have explicitly accepted the fee terms.';
                    quoteEl.textContent='Monthly trading fee waived for the duration of the free trial.';
                    consentBox.hidden=true;
                    cancelButton.hidden=data.consent_status!=='ACTIVE';
                    updateMonthlyFeeAcceptButton();
                    return;
                }
                quoteEl.textContent=monthlyFeeQuoteAvailable
                    ?`Current charge: ${data.quote_sol} SOL (SOL/USD $${Number(data.sol_usd).toFixed(2)}).`
                    :'Current SOL/USD price is unavailable. No charge will be made until a fresh quote is available.';
                const now=Math.floor(Date.now()/1000);
                const paidUntil=Number(data.paid_until||0);
                const active=data.consent_status==='ACTIVE';
                const canceled=data.consent_status==='CANCELED';
                consentBox.hidden=active&&paidUntil>now;
                cancelButton.hidden=!active;
                if(active&&paidUntil>now){
                    statusEl.textContent=`Active. Your next $5 USD-equivalent SOL charge is due around ${monthlyFeeDate(data.next_due_at)}.`;
                }else if(canceled&&paidUntil>now){
                    statusEl.textContent=`Canceled. No future charges will be made. Current paid access remains until ${monthlyFeeDate(paidUntil)}.`;
                    consentBox.hidden=true;cancelButton.hidden=true;
                }else if(canceled){
                    statusEl.textContent='Canceled. No future charges will be made. Accept again only if you want to restart the 30-day fee.';
                    consentBox.hidden=false;cancelButton.hidden=true;
                }else if(active&&!data.funded){
                    statusEl.textContent='Accepted. The first fee will be charged after your first verified deposit; no balance has been charged yet.';
                    consentBox.hidden=true;cancelButton.hidden=false;
                }else if(active&&paidUntil<=now&&data.funded){
                    statusEl.textContent=`Payment due. Available balance: ${(Number(data.balance_raw||0)/1e9).toFixed(9)} SOL. Add enough SOL to retry. New buys pause; sells and withdrawals remain available.`;
                    consentBox.hidden=true;cancelButton.hidden=false;
                }else{
                    statusEl.textContent=data.funded
                        ?'Not active. Review the recurring fee details and explicitly agree before using new buys.'
                        :'Not active. You can review and accept now; the first fee is collected after your first verified deposit.';
                    consentBox.hidden=false;cancelButton.hidden=true;
                }
                const checkbox=document.getElementById('monthly-trading-fee-agree');
                if(checkbox)checkbox.onchange=updateMonthlyFeeAcceptButton;
                updateMonthlyFeeAcceptButton();
            }catch(error){
                const statusEl=document.getElementById('monthly-trading-fee-status');
                if(statusEl)statusEl.textContent='Fee status unavailable. No fee charge is confirmed; refresh before accepting.';
                monthlyFeeQuoteAvailable=false;
                updateMonthlyFeeAcceptButton();
            }finally{monthlyFeeLoading=false;updateMonthlyFeeAcceptButton();}
        }
        async function acceptMonthlyTradingFee(){
            if(!monthlyFeeState||!monthlyFeeQuoteAvailable||
                    !document.getElementById('monthly-trading-fee-agree').checked){
                alert('Review the current quote and check the agreement box before continuing.');
                return;
            }
            const button=document.getElementById('monthly-trading-fee-accept');
            monthlyFeeLoading=true;updateMonthlyFeeAcceptButton();
            try{
                const response=await apiFetch('/api/monthly-trading-fee',{
                    method:'POST',headers:{'Content-Type':'application/json'},
                    body:JSON.stringify({action:'accept',consent:true,quote_raw:monthlyFeeState.quote_raw,
                        terms_version:monthlyFeeState.terms_version})
                });
                const data=await response.json();
                if(response.status===409){
                    alert(data.error||'The SOL/USD quote changed. Review the updated amount before agreeing.');
                    monthlyFeeLastRefresh=0;
                    monthlyFeeLoading=false;
                    await refreshMonthlyTradingFee(true);
                    return;
                }
                if(!response.ok||!data.success)throw Error(data.error||'Could not save your agreement.');
                const result=data.charge?.state;
                const feeMessage=result==='charged'
                    ?'Agreement saved and the first fee was transferred from your in-app SOL balance.'
                    :result==='waiting_for_funding'
                        ?'Agreement saved. The first fee will be charged after your first verified deposit.'
                        :result==='insufficient_funds'
                            ?'Agreement saved, but no fee was charged. Add enough SOL to your bot balance; new buys pause until payment. Sells and withdrawals remain available.'
                            :result==='reactivated'
                                ?'Agreement reactivated. Your already-paid period remains active; no immediate charge was made.'
                                :result==='paused_for_coverage_or_price'
                                    ?'Agreement saved, but no charge was made because wallet coverage or a fresh price could not be verified. No buys will run until billing can be confirmed.'
                                    :'Agreement saved. No charge was made yet; it will be retried after the account is funded and verified.';
                alert(feeMessage);
                document.getElementById('monthly-trading-fee-agree').checked=false;
                monthlyFeeLastRefresh=0;
                monthlyFeeLoading=false;
                await refreshMonthlyTradingFee(true);
                refresh();
            }catch(error){alert(error.message||'Could not save the monthly fee agreement.');}
            finally{monthlyFeeLoading=false;updateMonthlyFeeAcceptButton();}
        }
        async function cancelMonthlyTradingFee(){
            if(!confirm('Cancel future monthly trading fee charges? Any already-paid period remains active until its end. New buys will pause after that date; sells and withdrawals remain available.'))return;
            try{
                const response=await apiFetch('/api/monthly-trading-fee',{
                    method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:'cancel'})
                });
                const data=await response.json();
                if(!response.ok||!data.success)throw Error(data.error||'Could not cancel future charges.');
                monthlyFeeLastRefresh=0;
                await refreshMonthlyTradingFee(true);
                alert('Future monthly charges are canceled.');
            }catch(error){alert(error.message||'Could not cancel future charges.');}
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
            const canSellNow=position?.status==='OPEN';
            const canQueueRetry=position?.status==='RESERVED'&&position.pending_sell;
            if (!position || (!canSellNow && !canQueueRetry) || !position.position_id) {
                alert('This position cannot be sold while its current status is unresolved. Refresh and check the status.');
                return;
            }
            if(position.manual_close_queued){
                alert('A manual sale is already queued for this position.');
                return;
            }
            const quantity=Number(position.amount_tokens).toLocaleString(uiLocale(),{maximumFractionDigits:8});
            const symbol=position.symbol||position.token_symbol||'token';
            const funding=position.funding_currency||'account currency';
            const confirmation=canQueueRetry
                ? 'A sale for '+symbol+' is already pending. Queue one manual retry only if that sale is proven failed or expired? If it succeeds, no second sale will be sent.'
                : 'Sell only this position: '+quantity+' '+symbol+' for '+funding+
                    '? If another wallet operation is pending, the request will wait and the price may change. This sends a real Solana transaction; the final amount can vary with price and slippage.';
            if (!confirm(confirmation)) return;
            button.disabled=true;
            button.textContent=canQueueRetry?'Queueing retry…':'Submitting sell…';
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

        async function cancelQueuedManualClose(position, button) {
            if(!position?.position_id||!position.manual_close_queued)return;
            if(!confirm('Cancel the queued manual retry? This will not cancel any sale already submitted to Solana.'))return;
            button.disabled=true;
            try {
                const response=await apiFetch('/api/cancel_manual_close',{
                    method:'POST',headers:{'Content-Type':'application/json'},
                    body:JSON.stringify({position_id:position.position_id})
                });
                const data=await response.json();
                if(!response.ok||!data.success)throw new Error(data.message||'Could not cancel the queued retry.');
                alert(data.message||'Queued manual retry cancelled.');
                await refresh();
            } catch(e) {
                alert(e.message||'Could not cancel the queued retry.');
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
            const button = document.getElementById('btn-toggle');
            const status = document.getElementById('trading-toggle-status');
            const showStatus = message => {
                if (status) { status.textContent = message; status.hidden = !message; }
            };
            if (button && button.disabled) return;
            const size = String(currentTradingSettings.order_usdc || 1);
            if (button) {
                button.disabled = true;
                button.setAttribute('aria-busy', 'true');
            }
            showStatus(tradingActive ? 'Turning autopilot off…' : 'Sending enable request…');
            try {
                const response = await apiFetch('/api/toggle_trading', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({trade_amount_usdc: size})
                });
                const data = await response.json().catch(() => ({}));
                if (!response.ok || !data.success) {
                    throw new Error(data.message || ('Server rejected the request (' + response.status + ').'));
                }
                showStatus(data.trading_active ? 'Autopilot enabled.' : 'Autopilot disabled.');
                await refresh();
            } catch (error) {
                showStatus(String(error && error.message || 'Trading toggle failed. No trade was confirmed.'));
            } finally {
                if (button) {
                    button.disabled = false;
                    button.removeAttribute('aria-busy');
                }
            }
        }

        async function toggleMemeTrading() {
            if(!sniperActive&&!confirm('Start autonomous AI sniper with your SOL? You can lose money. Order size is set in Settings.'))return;
            const button=document.getElementById('btn-meme-toggle');
            const status=document.getElementById('discovery-status');
            if(button.disabled)return;
            button.disabled=true;
            button.setAttribute('aria-busy','true');
            const label=document.getElementById('meme-toggle-text');
            if(label)label.textContent=localizeUiText('Checking scan status…');
            try {
                const response=await apiFetch('/api/toggle_sniper',{method:'POST'});
                const result=await response.json();
                if(!response.ok||!result.success)throw new Error(result.message||'Sniper mode unchanged.');
                await refresh();
            }catch(e){
                const message=String(e.message||'Sniper mode unchanged: no server response.');
                if(status)status.textContent=localizeUiText(message);
                if(/candidate checks|candidate scan|market-data rate limit|market-data HTTP 429/i.test(message)){
                    await scanCandidates({waitForCompletion:false,refreshDashboard:false});
                    if(status)status.textContent=localizeUiText(message);
                }else await refresh();
            }finally{
                button.removeAttribute('aria-busy');
                updateMemeToggleUI(sniperActive);
            }
        }

        function updateToggleUI(isActive) {
            tradingActive = !!isActive;
            const btn = document.getElementById('btn-toggle');
            if (btn) {
                if(window.ZRL_WEB&&!tg?.initData)btn.disabled=!window.webAccessActive;else if(!btn.hasAttribute('aria-busy'))btn.disabled=false;
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
            const breakdown = stats.pnl_breakdown || {};
            const renderCurrency = (key, currency, digits) => {
                const totals = breakdown[key] || {};
                const positive = Number(totals.profit ?? 0);
                const negative = Number(totals.loss ?? 0);
                const net = Number(totals.net ?? 0);
                const amount = value => {
                    const safe = Number.isFinite(value) ? value : 0;
                    const sign = safe > 0 ? '+' : safe < 0 ? '−' : '';
                    const prefix = currency === 'USDC' ? '$' : '';
                    const suffix = currency === 'SOL' ? ' SOL' : '';
                    return sign + prefix + Math.abs(safe).toFixed(digits) + suffix;
                };
                const commission = Number(totals.partner_commission ?? 0);
                const commissionText = currency === 'USDC'
                    ? '$' + (Number.isFinite(commission) ? commission : 0).toFixed(6)
                    : (Number.isFinite(commission) ? commission : 0).toFixed(9) + ' SOL';
                const assign = (id, value, color) => {
                    const node = document.getElementById(id);
                    if (node) {
                        node.textContent = value;
                        if (color) node.style.color = color;
                    }
                };
                assign('pnl-' + key + '-gains', amount(positive), '#4ade80');
                assign('pnl-' + key + '-losses', amount(-Math.abs(negative)), '#f87171');
                assign('pnl-' + key + '-net', amount(net), net < 0 ? '#f87171' : '#4ade80');
                assign('pnl-' + key + '-record',
                    `${Number(totals.wins || 0)} / ${Number(totals.losses || 0)} / ${Number(totals.break_even || 0)}`);
                assign('pnl-' + key + '-commission', commissionText);
            };
            renderCurrency('usdc', 'USDC', 2);
            renderCurrency('sol', 'SOL', 9);
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
                    monthlyFeeLastRefresh=0;
                    refreshMonthlyTradingFee(true);
                    refresh();
                }
            } catch (e) { alert("Connection error."); }
            finally {
                btn.innerText = "Verify deposit";
                btn.disabled = window.ZRL_WEB ? !window.webAccessActive : false;
            }
        }

        function canShareProfitTrade(trade) {
            const profit=Number(trade?.profit_units);
            const closedAt=trade?.closed_at;
            const hasValidCloseTime=typeof closedAt==='number'
                ?Number.isFinite(closedAt)&&closedAt>0
                :typeof closedAt==='string'&&Number.isFinite(Date.parse(closedAt));
            return trade?.status==='CLOSED'
                && hasValidCloseTime
                && ['USDC','SOL'].includes(trade.funding_currency)
                && Number.isFinite(profit)&&profit>0;
        }
        function safeTradeShareSymbol(trade) {
            const raw=String(trade?.token_symbol||trade?.symbol||'').trim();
            if(!raw||/^0x[a-f\d]{40}$/i.test(raw)||/^[1-9A-HJ-NP-Za-km-z]{32,}$/.test(raw))return 'TOKEN';
            return raw.replace(/[^A-Za-z0-9._-]/g,'').slice(0,16)||'TOKEN';
        }
        function tradeShareAmount(value,currency) {
            const options=currency==='SOL'
                ?{minimumFractionDigits:4,maximumFractionDigits:9}
                :{minimumFractionDigits:2,maximumFractionDigits:6};
            return Number(value).toLocaleString(uiLocale(),options);
        }
        function updateTradeShareActions() {
            const confirmed=!!document.getElementById('trade-share-confirmation')?.checked;
            const ready=!!tradeShareState?.ready;
            const imageReady=!!tradeShareState?.imageReady;
            const submit=document.getElementById('trade-share-submit');
            const copy=document.getElementById('trade-share-copy');
            const download=document.getElementById('trade-share-download');
            if(submit)submit.disabled=!confirmed||!ready;
            if(copy)copy.disabled=!confirmed||!ready;
            if(download)download.disabled=!confirmed||!imageReady;
        }
        function drawShareWrappedText(ctx,text,x,y,maxWidth,lineHeight,maxLines=3) {
            const words=String(text).split(/\s+/);let line='',lines=0;
            for(const word of words){
                const next=line?line+' '+word:word;
                if(line&&ctx.measureText(next).width>maxWidth){
                    ctx.fillText(line,x,y+lines*lineHeight);lines++;line=word;
                    if(lines>=maxLines)return y+lines*lineHeight;
                }else line=next;
            }
            if(line&&lines<maxLines){ctx.fillText(line,x,y+lines*lineHeight);lines++;}
            return y+lines*lineHeight;
        }
        function drawTradeShareCard(state) {
            const canvas=document.getElementById('trade-share-canvas');
            const ctx=canvas?.getContext('2d');
            if(!canvas||!ctx)return false;
            const {snapshot}=state;
            const w=canvas.width,h=canvas.height;
            const background=ctx.createLinearGradient(0,0,w,h);
            background.addColorStop(0,'#101b12');background.addColorStop(.55,'#07100b');background.addColorStop(1,'#050907');
            ctx.fillStyle=background;ctx.fillRect(0,0,w,h);
            const glow=ctx.createRadialGradient(900,180,20,900,180,520);
            glow.addColorStop(0,'rgba(192,255,82,.22)');glow.addColorStop(1,'rgba(192,255,82,0)');
            ctx.fillStyle=glow;ctx.fillRect(0,0,w,h);
            ctx.strokeStyle='rgba(191,235,116,.36)';ctx.lineWidth=2;ctx.strokeRect(36,36,w-72,h-72);
            ctx.fillStyle='#c9fa72';ctx.font='800 28px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif';
            ctx.textBaseline='top';ctx.fillText('ZER0LIFE · AI TRADER',78,92);
            ctx.fillStyle='rgba(222,240,221,.62)';ctx.font='700 22px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif';
            ctx.fillText(localizeUiText('Finalized trade').toUpperCase(),78,210);
            ctx.fillStyle='#f0f7ee';ctx.font='800 88px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif';
            ctx.fillText(snapshot.symbol,78,280);
            ctx.fillStyle='rgba(212,228,211,.76)';ctx.font='700 22px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif';
            ctx.fillText(localizeUiText('Realized profit').toUpperCase(),78,470);
            const amount='+'+tradeShareAmount(snapshot.profit,snapshot.currency)+' '+snapshot.currency;
            let amountSize=76;
            ctx.font=`800 ${amountSize}px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif`;
            while(ctx.measureText(amount).width>920&&amountSize>42){
                amountSize-=4;ctx.font=`800 ${amountSize}px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif`;
            }
            ctx.fillStyle='#c9fa72';ctx.fillText(amount,78,530);
            if(Number.isFinite(snapshot.profitPct)){
                ctx.fillStyle='#d2e9d0';ctx.font='650 32px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif';
                ctx.fillText((snapshot.profitPct>0?'+':'')+snapshot.profitPct.toLocaleString(uiLocale(),{
                    minimumFractionDigits:2,maximumFractionDigits:2
                })+'%',78,640);
            }
            ctx.fillStyle='rgba(201,250,114,.8)';ctx.fillRect(78,744,924,2);
            canvas.setAttribute('aria-label',localizeUiText('Share card preview')+': '+snapshot.symbol+', +'
                +tradeShareAmount(snapshot.profit,snapshot.currency)+' '+snapshot.currency);
            return true;
        }
        async function renderTradeSharePreview(state=tradeShareState) {
            if(!state||state!==tradeShareState)return;
            state.renderRevision=(state.renderRevision||0)+1;
            const revision=state.renderRevision;
            state.ready=false;state.imageReady=drawTradeShareCard(state);state.file=null;state.blob=null;
            const amount='+'+tradeShareAmount(state.snapshot.profit,state.snapshot.currency)+' '+state.snapshot.currency;
            const pct=Number.isFinite(state.snapshot.profitPct)
                ?' · '+(state.snapshot.profitPct>0?'+':'')+state.snapshot.profitPct.toLocaleString(uiLocale(),{
                    minimumFractionDigits:2,maximumFractionDigits:2
                })+'%'
                :'';
            state.caption=[
                'ZER0LIFE AI Trader',
                localizeUiText('Finalized trade')+': '+state.snapshot.symbol,
                localizeUiText('Realized profit')+': '+amount+pct
            ].join('\n');
            const caption=document.getElementById('trade-share-caption');
            if(caption)caption.value=state.caption;
            const status=document.getElementById('trade-share-status');
            if(status)status.textContent=localizeUiText('Preparing card…');
            updateTradeShareActions();
            const canvas=document.getElementById('trade-share-canvas');
            if(state.imageReady&&canvas&&typeof canvas.toBlob==='function'){
                try{
                    const blob=await new Promise(resolve=>canvas.toBlob(resolve,'image/png'));
                    if(state!==tradeShareState||revision!==state.renderRevision)return;
                    state.blob=blob;
                    if(blob&&typeof File==='function'){
                        state.file=new File([blob],'zer0life-'+state.snapshot.symbol.toLowerCase()+'-trade.png',{type:'image/png'});
                    }
                    state.ready=true;
                    if(!blob&&status)status.textContent=localizeUiText('Could not prepare the image. You can still copy the caption.');
                }catch(_error){
                    if(state!==tradeShareState||revision!==state.renderRevision)return;
                    state.ready=true;
                    if(status)status.textContent=localizeUiText('Could not prepare the image. You can still copy the caption.');
                }
            }else{
                state.ready=true;
                if(status)status.textContent=localizeUiText(state.imageReady
                    ?'Sharing is unavailable here. Download the card and copy the caption to post manually.'
                    :'Could not prepare the image. You can still copy the caption.');
            }
            updateTradeShareActions();
        }
        function openTradeShareDialog(trade,trigger) {
            if(!canShareProfitTrade(trade))return;
            const dialog=document.getElementById('trade-share-dialog');
            const checkbox=document.getElementById('trade-share-confirmation');
            const status=document.getElementById('trade-share-status');
            if(!dialog||!checkbox)return;
            const profit=Number(trade.profit_units);
            const percent=Number(trade.profit_pct);
            tradeShareState={
                snapshot:{
                    symbol:safeTradeShareSymbol(trade),
                    profit,
                    currency:trade.funding_currency,
                    profitPct:Number.isFinite(percent)?percent:null
                },
                trigger,
                ready:false,
                imageReady:false,
                file:null,
                blob:null
            };
            checkbox.checked=false;
            if(status)status.textContent='';
            updateTradeShareActions();
            if(!dialog.open)dialog.showModal();
            renderTradeSharePreview(tradeShareState);
            checkbox.focus({preventScroll:true});
        }
        function closeTradeShareDialog() {
            const dialog=document.getElementById('trade-share-dialog');
            if(dialog?.open)dialog.close();
        }
        function sharePreparedTrade() {
            const state=tradeShareState;
            if(!state||!state.ready||!document.getElementById('trade-share-confirmation')?.checked)return;
            const status=document.getElementById('trade-share-status');
            if(typeof navigator.share!=='function'){
                if(status)status.textContent=localizeUiText(
                    'Sharing is unavailable here. Download the card and copy the caption to post manually.'
                );
                return;
            }
            const data={title:'ZER0LIFE AI Trader · '+localizeUiText('Finalized trade'),text:state.caption};
            if(state.file&&typeof navigator.canShare==='function'){
                try{if(navigator.canShare({files:[state.file]}))data.files=[state.file];}catch(_error){}
            }
            try{
                Promise.resolve(navigator.share(data)).then(()=>{
                    if(status)status.textContent=localizeUiText(
                        'Share menu closed. The app cannot verify that the post was published.'
                    );
                }).catch(error=>{
                    if(!status)return;
                    status.textContent=error?.name==='AbortError'
                        ?localizeUiText('Sharing canceled; nothing was posted automatically.')
                        :localizeUiText('Sharing is unavailable here. Download the card and copy the caption to post manually.');
                });
            }catch(_error){
                if(status)status.textContent=localizeUiText(
                    'Sharing is unavailable here. Download the card and copy the caption to post manually.'
                );
            }
        }
        async function copyTradeShareCaption() {
            const state=tradeShareState;
            if(!state||!state.ready||!document.getElementById('trade-share-confirmation')?.checked)return;
            const caption=document.getElementById('trade-share-caption');
            const status=document.getElementById('trade-share-status');
            try{
                if(navigator.clipboard?.writeText)await navigator.clipboard.writeText(state.caption);
                else{
                    caption?.select();
                    if(!document.execCommand('copy'))throw new Error('copy unavailable');
                }
                if(status)status.textContent=localizeUiText('Caption copied.');
            }catch(_error){
                caption?.focus();caption?.select();
                if(status)status.textContent=localizeUiText('Could not copy automatically. Select the caption and copy it manually.');
            }
        }
        function downloadTradeShareCard() {
            const state=tradeShareState;
            if(!state||!state.imageReady||!document.getElementById('trade-share-confirmation')?.checked)return;
            const canvas=document.getElementById('trade-share-canvas');
            const status=document.getElementById('trade-share-status');
            try{
                const anchor=document.createElement('a');
                anchor.href=canvas.toDataURL('image/png');
                anchor.download='zer0life-'+state.snapshot.symbol.toLowerCase()+'-trade.png';
                anchor.click();
                if(status)status.textContent=localizeUiText('Card download started.');
            }catch(_error){
                if(status)status.textContent=localizeUiText('Could not prepare the image. You can still copy the caption.');
            }
        }
        document.getElementById('trade-share-confirmation')?.addEventListener('change',updateTradeShareActions);
        document.getElementById('trade-share-submit')?.addEventListener('click',sharePreparedTrade);
        document.getElementById('trade-share-copy')?.addEventListener('click',copyTradeShareCaption);
        document.getElementById('trade-share-download')?.addEventListener('click',downloadTradeShareCard);
        document.getElementById('trade-share-dialog')?.addEventListener('close',()=>{
            const trigger=tradeShareState?.trigger;
            tradeShareState=null;
            const caption=document.getElementById('trade-share-caption');
            const status=document.getElementById('trade-share-status');
            if(caption)caption.value='';
            if(status)status.textContent='';
            updateTradeShareActions();
            if(trigger?.isConnected)trigger.focus();
        });

        async function refresh() {
            try {
                const l = await apiFetch('/api/logs').then(r => r.json()).catch(() => ({success:false}));
                if (l.success) {
                    const logBox = document.getElementById('log-box');
                    if (logBox) {
                        const entries = Array.isArray(l.entries) ? l.entries : [];
                        const lines = entries.map(e => {
                            const stamp = new Date(e.created_at * 1000).toLocaleString(uiLocale());
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
                refreshMonthlyTradingFee();
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
                                    ? new Date(seconds * 1000).toLocaleString(uiLocale()) : 'unknown';
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
                          +new Date(cost.checked_at*1000).toLocaleString(uiLocale())+' · '+cost.reason
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
                          +' / Sniper '+policy.max_positions.sniper
                          +'. Sniper BUY requires positive 5m/1h changes and no 6h decline below -'
                          +policy.sniper_buy_max_6h_decline_pct
                          +'%. Both modes use confirmed chart entries. AI SELL and resistance exits are disabled; automatic sales use only the 5% stop or the 2% peak trail activated after +3.5% in the funding currency. Manual sales remain available. Order sizes come from your saved settings.'
                        : 'Independent policy unavailable. Do not enable real trading.';
                    const gate=s.execution_check;
                    document.getElementById('execution-policy-result').textContent=gate
                        ? new Date(gate.checked_at*1000).toLocaleString(uiLocale())+' · '+gate.status+' · '
                          +gate.stage+' / '+gate.action+(gate.symbol?' '+gate.symbol:'')+'\n'+normalizeAnalysisText(gate.reason)
                        : 'No execution check recorded yet. AI suggestions are not executed trades.';
                    const ai=s.ai_status||{};
                    const rulesOnly=ai.paid_ai_enabled===false&&ai.signal_engine==='rules-v1';
                    aiConfigured=!!ai.configured;
                    updateMemeToggleUI(s.sniper_active);
                    const whaleAI=ai.whale_analysis||{};
                    const whaleAIState=ai.market_discovery
                        ? '\nCandidate source: scheduled market scanning; Helius WebSocket OFF.'
                        : whaleAI.state&&whaleAI.state!=='idle'
                        ? '\nWhale-triggered analysis: '+whaleAI.state
                          +((whaleAI.symbols||[]).length?' · '+whaleAI.symbols.map(code=>(ai.asset_labels||{})[code]||code).join(', '):'')
                          +(whaleAI.message?' · '+whaleAI.message:'')
                        : '';
                    document.getElementById('ai-status').textContent=(rulesOnly?
                        'Rules v1 · OpenAI OFF · no paid AI requests · '+ai.interval_seconds+' sec market scan':
                        aiConfigured?
                        ai.model+' · '+ai.interval_seconds+' sec interval'+(ai.last_analyzed_at?' · analyzed':' · connection unverified')
                          +(ai.last_trigger==='whale_signal'?' · whale-triggered':''):
                        'Paid AI is unavailable; new AI signals are paused.')+whaleAIState;
                    const timings=ai.timings_ms||{};
                    document.getElementById('ai-latency').textContent=
                        rulesOnly
                        ?'No OpenAI requests. Market data, RPC provider use and on-chain transaction fees may still have costs.'
                        :Number.isFinite(timings.total)
                        ?'Last analysis timings · market data '+timings.market_data+' ms'
                          +' · analyst '+timings.analyst+' ms'
                          +' · risk controller '+timings.risk_controller+' ms'
                          +' · total '+timings.total+' ms. Network confirmation is separate.'
                        :'Signal timings: awaiting a fresh market scan.';
                    if(ai.last_decisions)document.getElementById('ai-decisions').textContent=Object.entries(ai.last_decisions).map(([mode,d])=>{
                        const analyzedAt=ai.decision_created_at?ai.decision_created_at[mode]:ai.last_analyzed_at;
                        const modeName=mode==='main'?'Core · USDC':mode==='sniper'?'Meme Sniper · SOL':'AI Crypto Investors';
                        return modeName+' · '+(analyzedAt?new Date(analyzedAt*1000).toLocaleString(uiLocale()):'Awaiting analysis')+'\n'+d.action+' '+((ai.asset_labels||{})[d.symbol]||d.symbol||'')+' — '+normalizeAnalysisText(d.reason);
                    }).join('\n');
                    else document.getElementById('ai-decisions').textContent=
                        rulesOnly?'No current rule signals yet. A fresh chart scan is required; no AI retry is running.':'No current analysis.';
                    const risk=ai.risk_controller||{};
                    document.getElementById('ai-risk-review').textContent=(rulesOnly
                        ?'Server-side chart gate · BUY signals only · paid AI reviewer OFF\n'
                        :'Independent risk & trend review · BUY only\n')
                        +['main','sniper'].map(mode=>{
                            const v=(risk.verdicts||{})[mode];
                            const name=mode==='main'?'Core · USDC':'Meme Sniper · SOL';
                            if(!v)return name+': no BUY review in this analysis.';
                            return name+': '+(v.approved?'APPROVED':'BLOCKED')+' · '+v.trend
                                +' — '+normalizeAnalysisText(v.reason);
                        }).join('\n');
                    const learning=ai.learning||{};
                    const outcomeCount=Number(learning.sample_count||0);
                    document.getElementById('ai-learning').textContent=(rulesOnly?'Trade outcome history · ':'Outcome memory · ')+outcomeCount
                        +' finalized closed trades included in the latest analysis (up to '+(learning.max_outcomes||500)+'). '
                        +'Fewer than '+(learning.minimum_sample||20)+' outcomes per token/mode is limited evidence. '
                        +(rulesOnly?'Stored history only; no paid model training or profit guarantee. '
                          :'Historical feedback, not model training or proof of profit; separate operator fees excluded. ')
                        +(rulesOnly?'Automatic exits do not need AI approval.':
                          'The second AI request shares the existing daily budget. Exits do not require its approval.');
                    renderCoreChartZones(ai.chart_context,ai.asset_labels);
                    const discovery=ai.discovery||{};
                    document.getElementById('discovery-status').textContent=discovery.entry_paused?discovery.message:
                        discovery.checked_at?(discovery.eligible||0)+' screened · '+(discovery.scanned||0)+' scanned · refreshed '+new Date(discovery.checked_at*1000).toLocaleTimeString(uiLocale()):discovery.message||'Candidate scan pending.';
                    if(discovery.entry_paused&&discovery.checked_at)document.getElementById('discovery-status').textContent+=' Last scan: '+new Date(discovery.checked_at*1000).toLocaleTimeString(uiLocale())+' · '+(discovery.eligible||0)+' previously screened (not current eligibility).';
                    if(discovery.retry_after_seconds)document.getElementById('discovery-status').textContent+=' '+localizeUiText('Provider cooldown: ')+discovery.retry_after_seconds+localizeUiText(' seconds.');
                    if(discovery.whale_prioritized)document.getElementById('discovery-status').textContent+=' · whale mints screened: '+discovery.whale_prioritized+' ('+(discovery.whale_candidates||0)+' eligible)';
                    document.getElementById('candidate-list').textContent=(ai.sniper_assets||[]).map(code=>{
                        const row=(discovery.candidates||[]).find(item=>item.code===code);
                        return ((ai.asset_labels||{})[code]||code)+(row?' · liquidity $'+Math.round(row.liquidity_usd).toLocaleString(uiLocale()):' · held asset');
                    }).join('\n')||'No verified shortlist yet.';
                    const marketSource=ai.market_discovery||{};
                    document.getElementById('whale-signal-status').textContent=
                        localizeUiText(marketSource.provider||'DexScreener profiles + meme meta feeds + bounded searches')
                        +' · Helius WebSocket OFF'
                        +'\nCandidate refresh: every '+(marketSource.scan_interval_seconds||300)+' seconds; bounded to 200 discovered / 40 checked.'
                        +'\nLiquidity and activity screening → fresh 5m/1h charts → rules engine → execution checks.'
                        +'\nMeme Sniper uses SOL. Core uses USDC and independent rising-trend analysis. Neither waits for whale events.'
                        +'\nCandidates are not orders. Unavailable or stale data blocks new entries, never position exits.'
                        +'\nNo Helius streaming or OpenAI traffic from this source. Solana RPC providers and on-chain transactions may still cost money.';
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
                    renderInvestorDashboard(s);
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
                            const isFeeIncome = h.type === 'TRADING_FEE_INCOME';
                            const color = isDep || isFeeIncome ? '#4ade80' : '#f87171';
                            const sign = isDep || isFeeIncome ? '+' : '-';
                            const label = h.label || (isDep ? 'Deposit' : h.type === 'WITHDRAW' ? 'Withdrawal' : 'Transaction');
                            const txLink = (h.tx && h.tx !== 'SIMULATED_TX' && h.tx.length > 20) 
                                ? `<a href="https://solscan.io/tx/${encodeURIComponent(h.tx)}" target="_blank" rel="noopener noreferrer" style="color: #60a5fa; text-decoration: none; font-size: 11px;">Tx ↗</a>`
                                : `<span style="color: #64748b; font-size: 10px;">${h.status}</span>`;
                            
                            return `
                                <div style="display: flex; justify-content: space-between; align-items: center; padding: 10px 0; border-bottom: 1px solid #1c2333; text-align: left;">
                                    <div>
                                        <div style="color: ${color}; font-weight: 800; font-size: 13px;">${sign} ${h.amount}</div>
                                        <div style="font-size: 10px; color: #94a3b8; margin-top: 2px;">${label}</div>
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
                        document.getElementById('history-box').innerHTML = s.trades.map((t,tradeIndex) => {
                            const isClosed = t.status === 'CLOSED';
                            const isOpen = t.status === 'OPEN';
                            const isPending = !isClosed && !isOpen;
                            const pnlColor = Number(t.profit_units??t.profit_usdc) >= 0 ? '#4ade80' : '#f87171';
                            const shareProfitEligible = canShareProfitTrade(t);

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
                                        <div>Opened: <span style="color: #fff;">${t.timestamp}</span></div>
                                        ${t.closed_at ? '<div>Closed: <span style="color:#fff">'+tradeAuditText(t.closed_at)+'</span></div>' : ''}
                                    </div>
                                    <div style="display: flex; justify-content: space-between; align-items: center; margin-top: 6px; padding-top: 6px; border-top: 1px solid #141b2a;">
                                        <div style="font-size: 11px; color: #64748b;">Result:</div>
                                        <div style="font-weight: 800; font-size: 13px; color: ${isClosed ? pnlColor : '#60a5fa'};">
                                            ${isClosed ? (Number(t.profit_units??t.profit_usdc).toFixed(t.funding_currency==='SOL'?9:2)+' '+(t.funding_currency||'USDC')+' ('+t.profit_pct.toFixed(2)+'%)') : isPending ? 'Sale awaiting confirmation' : 'Open position'}
                                        </div>
                                    </div>
                                    ${shareProfitEligible ? `<button type="button" class="btn-green-main share-profitable-trade" data-trade-index="${tradeIndex}">Share profit</button>` : ''}
                                    <div class="order-tx-row">
                                        ${t.buy_tx ? '<a class="tx-link" href="https://solscan.io/tx/' + t.buy_tx + '" target="_blank">↗ Buy transaction</a>' : ''}
                                        ${t.sell_tx ? '<a class="tx-link" style="color: #4ade80;" href="https://solscan.io/tx/' + t.sell_tx + '" target="_blank">↗ Sell transaction</a>' : ''}
                                    </div>
                                    ${renderTradeAudit(t)}
                                    ${t.manual_close_queued ? '<div class="section-note" style="margin-top:8px;color:#cbd5e1;">Manual sale queued. It waits for the current transaction; if that sale succeeds, no retry is sent. <button type="button" class="position-close-cancel" data-position-id="' + t.position_id + '">Cancel queued retry</button></div>' : ''}
                                    ${isOpen && t.position_id && !t.manual_close_queued ? '<button type="button" class="position-close" data-position-id="' + t.position_id + '">Sell this position</button>' : ''}
                                    ${t.status==='RESERVED' && t.pending_sell && t.position_id && !t.manual_close_queued ? '<button type="button" class="position-close" data-position-id="' + t.position_id + '">Queue manual retry</button>' : ''}
                                </div>
                            `;
                        }).join('');
                        document.getElementById('history-box').querySelectorAll('.share-profitable-trade').forEach(button => {
                            const trade=s.trades[Number(button.dataset.tradeIndex)];
                            button.addEventListener('click',()=>openTradeShareDialog(trade,button));
                        });
                        document.getElementById('history-box').querySelectorAll('.position-close').forEach(button => {
                            const position=s.trades.find(trade=>trade.position_id===button.dataset.positionId);
                            button.addEventListener('click',()=>closePosition(position,button));
                        });
                        document.getElementById('history-box').querySelectorAll('.position-close-cancel').forEach(button => {
                            const position=s.trades.find(trade=>trade.position_id===button.dataset.positionId);
                            button.addEventListener('click',()=>cancelQueuedManualClose(position,button));
                        });
                    }
                }
            } catch(e) {}
        }
        // Explicit listener for Telegram iOS WebView; avoids inline-handler lookup issues.
        const primaryTradingButton = document.getElementById('btn-toggle');
        if (primaryTradingButton) {
            primaryTradingButton.addEventListener('click', function(event) {
                event.preventDefault();
                event.stopPropagation();
                toggleTrading();
            });
        }
        window.toggleTrading = toggleTrading;
        initializeSolanaPayControls();
        restoreSolanaPayInvoice();
        setInterval(refresh, 5000);
    </script>
</body>
</html>
"""

HTML_CONTENT = RAW_HTML.replace("__SPLASH_URL__", SPLASH_IMAGE_URL).replace("__DEPOSIT_WALLET__", SHARED_DEPOSIT_WALLET)

async def index_handler(request):
    page = HTML_CONTENT
    if request.path.rstrip("/") in ("", "/terminal"):
        panel = Path(__file__).with_name("web_login.html").read_text(encoding="utf-8")
        page = page.replace("</head>", "<script>window.ZRL_WEB=true;</script></head>")
        page = page.replace("</body>", panel + '<script src="/web-terminal.js"></script></body>')
    page = page.replace("</body>", '<script src="/dashboard.js"></script></body>')
    return web.Response(text=page, content_type='text/html', headers={"Cache-Control": "no-store",
        "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "frame-ancestors 'none'; object-src 'none'; base-uri 'none'"})

def register_public_web_routes(app):
    """Register the browser terminal and its static assets in one testable place."""
    async def web_script(request):
        return web.Response(text=Path(__file__).with_name("web_terminal.js").read_text(encoding="utf-8"),
                            content_type="application/javascript", headers={"Cache-Control": "no-store"})

    async def dashboard_script(request):
        return web.Response(text=Path(__file__).with_name("static").joinpath("dashboard.js").read_text(encoding="utf-8"),
                            content_type="application/javascript", headers={"Cache-Control": "no-store"})

    app.router.add_get("/", index_handler)
    app.router.add_get("/terminal", index_handler)
    app.router.add_get("/terminal/", index_handler)
    app.router.add_get("/static/zerolife-ai-trader-core.webp", splash_asset_handler)
    app.router.add_get("/web-terminal.js", web_script)
    app.router.add_get("/dashboard.js", dashboard_script)


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
        "certifi_version": certifi.__version__,
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
        security_headers_middleware, english_json_middleware, telegram_auth_middleware],
        client_max_size=MAX_SUPPORT_REQUEST_BYTES)
    runtime = CustodyRuntime(DB_FILE, SOLANA_RPC, ADMIN_TELEGRAM_ID, SHARED_HOT_WALLET,
        get_signer_keypair, verify_user_pin, get_token_prices, WATCHLIST, add_log,
        base_url=RENDER_URL, notification_token=TELEGRAM_TOKEN,
        reserve_wallet=CUSTODY_RESERVE_WALLET,
        hot_wallet_cap_usdc=os.getenv("HOT_WALLET_CAP_USDC", ""),
        hot_wallet_cap_sol=os.getenv("HOT_WALLET_CAP_SOL", ""),
        market_context_fn=lambda session: fetch_main_market_context(
            session, WATCHLIST, runtime.main_symbols),
        sniper_market_context_fn=lambda session, watchlist, symbols: fetch_sniper_market_context(
            session, watchlist, symbols))
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
    register_public_web_routes(app)
    app.router.add_get('/web-auth/me', runtime.web_access.public)
    app.router.add_post('/web-auth/start', runtime.web_access.public)
    app.router.add_post('/web-auth/finish', runtime.web_access.public)
    app.router.add_post('/web-auth/logout', runtime.web_access.public)
    app.router.add_get('/api/subscription', wallet_handler(runtime.web_access.status))
    app.router.add_post('/api/subscription/trial', wallet_handler(runtime.web_access.start_trial))
    app.router.add_post('/api/subscription/order', wallet_handler(runtime.web_access.order))
    app.router.add_post('/api/subscription/verify', wallet_handler(runtime.web_access.verify))
    app.router.add_get('/health', health_handler)
    app.router.add_get('/api/logs', wallet_handler(runtime.logs))
    app.router.add_get('/api/partners', wallet_handler(runtime.partner_program))
    app.router.add_post('/api/partners/retry', wallet_handler(runtime.retry_partner_payout))
    app.router.add_get('/api/stats', wallet_handler(runtime.stats))
    app.router.add_get('/api/ai-trader/settings', wallet_handler(runtime.get_ai_trader_settings))
    app.router.add_post('/api/ai-trader/settings', wallet_handler(runtime.save_ai_trader_settings))
    app.router.add_post('/api/ai-trader/signals/evaluate', wallet_handler(runtime.evaluate_ai_trader_signals))
    app.router.add_get('/api/ai-trader/signals/history', wallet_handler(runtime.get_ai_trader_signal_history))
    app.router.add_get('/api/ai-trader/paper-journal', wallet_handler(runtime.get_ai_trader_paper_journal))
    app.router.add_post('/api/ai-trader/backtest', wallet_handler(runtime.run_ai_trader_backtest))
    app.router.add_get('/api/ai-trader/backtest/history', wallet_handler(runtime.get_ai_trader_backtest_history))
    app.router.add_get('/api/monthly-trading-fee', wallet_handler(runtime.monthly_trading_fee))
    app.router.add_post('/api/monthly-trading-fee', wallet_handler(runtime.monthly_trading_fee))
    support_inbox = SupportInbox(TELEGRAM_TOKEN, ADMIN_TELEGRAM_ID)
    app.router.add_post('/api/support', support_inbox.handle)
    app.router.add_post('/api/toggle_trading', wallet_handler(runtime.toggle))
    app.router.add_post('/api/trading_settings', wallet_handler(runtime.save_trading_settings))
    app.router.add_post('/api/investor_budget', wallet_handler(runtime.save_investor_budget))
    app.router.add_post('/api/toggle_investor', wallet_handler(runtime.toggle_investor))
    app.router.add_get('/api/base-paper', wallet_handler(runtime.base_paper_report))
    app.router.add_post('/api/base-paper/budget', wallet_handler(runtime.save_base_paper_budget))
    app.router.add_post('/api/base-paper/scan', wallet_handler(runtime.scan_base_paper))
    app.router.add_get('/api/base-live/readiness', wallet_handler(runtime.base_live_readiness_report))
    app.router.add_post('/api/base-live/budget', wallet_handler(runtime.save_base_live_budget))
    app.router.add_post('/api/base-live/toggle', wallet_handler(runtime.toggle_base_live))
    app.router.add_post('/api/exchange/quote', wallet_handler(runtime.exchange_quote))
    app.router.add_get('/api/exchange/balances', wallet_handler(runtime.exchange_balances))
    app.router.add_post('/api/exchange/submit', wallet_handler(runtime.exchange_submit))
    app.router.add_post('/api/exchange/approve', wallet_handler(runtime.exchange_approve))
    app.router.add_get('/api/exchange/transfers', wallet_handler(runtime.exchange_transfers))
    app.router.add_post('/api/trading_readiness', wallet_handler(runtime.trading_readiness))
    app.router.add_post('/api/ai_preview', wallet_handler(runtime.ai_preview))
    app.router.add_post('/api/recover_expired_operation', wallet_handler(runtime.recover_expired_operation))
    app.router.add_post('/api/toggle_sniper', wallet_handler(runtime.toggle_sniper))
    app.router.add_post('/api/meme_scan', wallet_handler(runtime.scan_candidates))
    app.router.add_get('/api/mem-sniper/paper', wallet_handler(runtime.meme_paper_status))
    app.router.add_post('/api/mem-sniper/paper/scan', wallet_handler(runtime.meme_paper_scan))
    app.router.add_post('/api/mem-sniper/paper/start', wallet_handler(runtime.meme_paper_start))
    app.router.add_post('/api/mem-sniper/paper/stop', wallet_handler(runtime.meme_paper_stop))
    app.router.add_get('/api/mem-sniper/paper/history', wallet_handler(runtime.meme_paper_history))
    app.router.add_post('/api/toggle_meme', api_toggle_meme)
    app.router.add_post('/api/verify_deposit', wallet_handler(runtime.deposit))
    app.router.add_post('/api/wallet_link', wallet_handler(runtime.wallet_link))
    app.router.add_post('/api/deposit_invoice', wallet_handler(runtime.create_invoice))
    app.router.add_get('/api/admin/overview', wallet_handler(runtime.admin_overview))
    app.router.add_get('/api/admin/status', wallet_handler(runtime.admin_status))
    app.router.add_post('/api/admin/entry_pause', wallet_handler(runtime.admin_set_entry_pause))
    app.router.add_get('/api/admin/pending_operations', wallet_handler(runtime.admin_pending_operations))
    app.router.add_get('/api/admin/balance_check', wallet_handler(runtime.admin_balance_check))
    app.router.add_post('/api/admin/owner_shortfall', wallet_handler(runtime.admin_absorb_owner_shortfall))
    app.router.add_post('/api/admin/record_external_withdrawal', wallet_handler(runtime.admin_record_external_withdrawal))
    app.router.add_post('/api/admin/retire_legacy', wallet_handler(runtime.retire_legacy))
    app.router.add_post('/api/admin/fee_reserve', wallet_handler(runtime.reserve_fees))
    app.router.add_post('/api/admin/release_fee_reserve', wallet_handler(runtime.release_fee_reserve))
    app.router.add_post('/api/admin/opening_balance', wallet_handler(runtime.opening_balance))
    app.router.add_get('/api/admin/legacy_balances/preview', wallet_handler(runtime.preview_legacy_balances))
    app.router.add_post('/api/admin/legacy_balances/import', wallet_handler(runtime.import_legacy_balances))
    app.router.add_post('/api/close_all', wallet_handler(runtime.close_all))
    app.router.add_post('/api/close_position', wallet_handler(runtime.close_position))
    app.router.add_post('/api/cancel_manual_close', wallet_handler(runtime.cancel_manual_close))
    app.router.add_post('/api/withdraw', wallet_handler(runtime.withdraw))
    app.router.add_post('/api/withdraw/code', wallet_handler(runtime.request_withdrawal_code))
    app.router.add_get('/api/security_status', api_security_status)
    app.router.add_get('/api/account', wallet_handler(runtime.accounts.status))
    app.router.add_post('/api/account/language', wallet_handler(runtime.accounts.set_language))
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
    asyncio.create_task(runtime.meme_paper_worker())
    asyncio.create_task(runtime.trade_review_worker())
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
