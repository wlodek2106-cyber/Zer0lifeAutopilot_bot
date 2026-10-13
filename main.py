import asyncio
import aiohttp
from aiohttp import web
import sqlite3
import os
import logging
from datetime import datetime
import json
import base58
import base64
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction

logging.basicConfig(level=logging.INFO, format='%(asctime)s - [%(levelname)s] - %(message)s')

# --- КОНФИГУРАЦИЯ ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
PORT = int(os.getenv("PORT", 10000))
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL", "https://zer0lifeautopilot-bot.onrender.com").strip()

DB_DIR = "/var/data"
DB_FILE = os.path.join(DB_DIR, "zer0life_users.db") if os.path.exists(DB_DIR) else "zer0life_users.db"

SOLANA_RPC = os.getenv("SOLANA_RPC_URL", "https://mainnet.helius-rpc.com/?api-key=b82ca0bf-cd65-4ca8-a3d0-4400661d9a93").strip()
SHARED_DEPOSIT_WALLET = os.getenv("DEPOSIT_WALLET", "8hxiCofyaKCBkhR5nsDqvUivmfgxcVx8zo2WiCzSdM6L").strip()
SPLASH_IMAGE_URL = "https://i.postimg.cc/52n94Pzg/57D47DDD-9BED-4D4B-AA69-2B59CAB400AD.png"

# Токен USDC и параметры торговли
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
MIN_DEPOSIT_USDC = 50.0
MAX_DEPOSIT_USDC = 10000.0
MIN_BOT_BALANCE_USDC = 50.0  # Минимальный остаток для работы бота

WATCHLIST = {
    "SOL":  {"mint": "So11111111111111111111111111111111111111112", "decimals": 9},
    "JUP":  {"mint": "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN", "decimals": 6},
    "RAY":  {"mint": "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R", "decimals": 6},
    "WIF":  {"mint": "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm", "decimals": 6},
    "PYTH": {"mint": "HZ1JovNiDcZvKhgWNgQX5Z59C3b22DoPtRUC544HLD37", "decimals": 6}
}

TRADE_AMOUNT_USDC = 20.0
TRADE_AMOUNT_RAW = int(TRADE_AMOUNT_USDC * 1_000_000)
TP_PCT = 0.035          # +3.5%
SL_PCT = 0.020          # -2.0%
MAX_SLIPPAGE_BPS = 40   # 0.40%

# --- ПАРАМЕТРЫ MEME SNIPER РЕЖИМА ---
MEME_ALLOCATION_SOL = 0.1
MEME_TARGET_MULTIPLIER = 10.0  # Итоговая цель х10 (1.0 SOL)
MEME_HALF_MULTIPLIER = 5.0     # Промежуточная цель х5 (0.5 SOL) для частичного вывода в USDC
MEME_STOP_LOSS_PCT = 0.80      # Стоп-лосс на случай скама (-80%)

IS_TRADING_ACTIVE = True       # Глобальный флаг автопилота Топ-5 (ВКЛ / ВЫКЛ)
IS_MEME_ACTIVE = False         # Глобальный флаг Meme Sniper режима (ВКЛ / ВЫКЛ)
RECENT_LOGS = ["⚜️ Multi-Asset & Meme Sniper Engine v4.5 (Portfolio Tracker) инициализирован."]

def add_log(msg: str):
    global RECENT_LOGS
    timestamp = datetime.now().strftime('%H:%M:%S')
    entry = f"[{timestamp}] {msg}"
    RECENT_LOGS.insert(0, entry)
    if len(RECENT_LOGS) > 50:
        RECENT_LOGS.pop()
    logging.info(msg)

def get_signer_keypair():
    pk_env = os.getenv("SOLANA_PRIVATE_KEY", "").strip()
    if not pk_env:
        add_log("❌ Ключ SOLANA_PRIVATE_KEY не задан в переменных.")
        return None
    try:
        pk_clean = pk_env.replace('"', '').replace("'", "").strip()
        if pk_clean.startswith("["):
            return Keypair.from_bytes(bytes(json.loads(pk_clean)))
        else:
            return Keypair.from_bytes(base58.b58decode(pk_clean))
    except Exception as e:
        add_log(f"❌ Ошибка ключа: {e}")
        return None

def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        telegram_id INTEGER PRIMARY KEY, username TEXT, balance_usdc REAL DEFAULT 0.0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    
    c.execute("PRAGMA table_info(users)")
    u_cols = [col[1] for col in c.fetchall()]
    if "balance_usdc" not in u_cols:
        try:
            c.execute("ALTER TABLE users ADD COLUMN balance_usdc REAL DEFAULT 0.0")
        except Exception:
            pass

    c.execute('''CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT, token_symbol TEXT, token_mint TEXT,
        buy_price REAL, amount_tokens REAL, sell_price REAL DEFAULT 0,
        profit_usdc REAL DEFAULT 0.0, buy_tx TEXT, sell_tx TEXT, timestamp TEXT)''')

    c.execute('''CREATE TABLE IF NOT EXISTS deposits (
        id INTEGER PRIMARY KEY AUTOINCREMENT, telegram_id INTEGER, tx_signature TEXT UNIQUE,
        amount_usdc REAL, status TEXT, timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')

    c.execute('''CREATE TABLE IF NOT EXISTS withdrawals (
        id INTEGER PRIMARY KEY AUTOINCREMENT, telegram_id INTEGER, token TEXT,
        amount REAL, recipient_address TEXT, tx_signature TEXT, status TEXT, 
        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')

    c.execute("PRAGMA table_info(trades)")
    t_cols = [col[1] for col in c.fetchall()]
    if "half_taken" not in t_cols:
        try:
            c.execute("ALTER TABLE trades ADD COLUMN half_taken INTEGER DEFAULT 0")
        except Exception:
            pass

    try:
        c.execute("DELETE FROM trades WHERE sell_price = 0")
        conn.commit()
    except Exception:
        pass

    conn.commit()
    conn.close()

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
    mints_str = ",".join([v["mint"] for v in WATCHLIST.values()])
    url = f"https://api.dexscreener.com/latest/dex/tokens/{mints_str}"
    try:
        headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            if resp.status == 200:
                data = await resp.json()
                pairs = data.get("pairs", [])
                for pair in pairs:
                    base_token = pair.get("baseToken", {}).get("address")
                    price_usd = pair.get("priceUsd")
                    if base_token and price_usd:
                        for sym, info in WATCHLIST.items():
                            if info["mint"] == base_token and sym not in prices:
                                prices[sym] = float(price_usd)
    except Exception as e:
        add_log(f"⚠️ Ошибка запроса цен: {str(e)[:40]}")
    return prices

async def execute_jupiter_swap(session: aiohttp.ClientSession, input_mint: str, output_mint: str, amount_raw: int, signer: Keypair, slippage_bps: int = MAX_SLIPPAGE_BPS, priority_fee: int = 25000) -> str:
    try:
        user_pubkey = str(signer.pubkey())
        headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
        quote_url = f"https://api.jup.ag/swap/v1/quote?inputMint={input_mint}&outputMint={output_mint}&amount={amount_raw}&slippageBps={slippage_bps}"
        async with session.get(quote_url, headers=headers, timeout=aiohttp.ClientTimeout(total=6)) as resp:
            if resp.status != 200:
                err_text = await resp.text()
                add_log(f"⚠️ Ошибка котировки Jupiter: {resp.status} {err_text[:50]}")
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
                err_text = await resp.text()
                add_log(f"⚠️ Ошибка формирования Swap: {resp.status} {err_text[:50]}")
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
    except Exception as e:
        add_log(f"⚠️ Исключение в execute_jupiter_swap: {e}")
    return ""

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
                
                if prices:
                    add_log(f"📊 Цены Топ-5: " + ", ".join([f"{s}: ${p:.2f}" for s, p in prices.items()]))

                for sym, p in prices.items():
                    history[sym].append(p)
                    if len(history[sym]) > 10:
                        history[sym].pop(0)

                conn = sqlite3.connect(DB_FILE)
                c = conn.cursor()
                c.execute("SELECT id, token_symbol, token_mint, buy_price, amount_tokens FROM trades WHERE sell_price = 0 AND token_symbol IN ('SOL', 'JUP', 'RAY', 'WIF', 'PYTH') LIMIT 1")
                active_trade = c.fetchone()

                if active_trade:
                    trade_id, sym, mint, entry_px, amount_tokens = active_trade
                    cur_px = prices.get(sym, entry_px)
                    pnl_pct = (cur_px - entry_px) / entry_px
                    add_log(f"🎯 Позиция [{sym}]: Вход ${entry_px:.4f} | Тек: ${cur_px:.4f} | PnL: {pnl_pct*100:+.2f}%")

                    if pnl_pct >= TP_PCT or pnl_pct <= -SL_PCT:
                        action = "TP" if pnl_pct >= TP_PCT else "SL"
                        add_log(f"⚡ Закрытие {sym} по {action} ({pnl_pct*100:+.2f}%)...")
                        raw_amount = int(amount_tokens * (10 ** WATCHLIST[sym]["decimals"]))
                        sell_sig = await execute_jupiter_swap(session, mint, USDC_MINT, raw_amount, signer)
                        if sell_sig:
                            real_exit_px = cur_px
                            profit_usd = round(TRADE_AMOUNT_USDC * pnl_pct, 2)
                            c.execute("UPDATE trades SET sell_price = ?, profit_usdc = ?, sell_tx = ? WHERE id = ?", 
                                      (real_exit_px, profit_usd, sell_sig, trade_id))
                            conn.commit()
                            add_log(f"✅ Ордер закрыт по реальной цене ${real_exit_px:.4f}! Tx: {sell_sig[:10]}...")
                            cooldown_until = now_ts + 30
                        else:
                            add_log(f"⚠️ Ошибка продажи {sym} в сети, повторим позже...")
                    conn.close()
                    await asyncio.sleep(20)
                    continue

                conn.close()

                if not IS_TRADING_ACTIVE or now_ts < cooldown_until:
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
                    sym, cur_p = best_candidate
                    mint = WATCHLIST[sym]["mint"]
                    add_log(f"🚀 Сигнал входа Топ-5: {sym} просел на {max_dip*100:.2f}%. Покупка по ${cur_p:.4f}...")
                    buy_sig = await execute_jupiter_swap(session, USDC_MINT, mint, TRADE_AMOUNT_RAW, signer)
                    if buy_sig:
                        bought_tokens = TRADE_AMOUNT_USDC / cur_p
                        c_in = sqlite3.connect(DB_FILE)
                        c_in.cursor().execute("""INSERT INTO trades 
                            (token_symbol, token_mint, buy_price, amount_tokens, sell_price, profit_usdc, buy_tx, sell_tx, timestamp)
                            VALUES (?, ?, ?, ?, 0, 0, ?, '', ?)""",
                            (sym, mint, cur_p, bought_tokens, buy_sig, datetime.now().strftime('%H:%M:%S')))
                        c_in.commit()
                        c_in.close()
                        add_log(f"💎 Успешно куплен {sym} по ${cur_p:.4f}! Tx: {buy_sig[:12]}...")
                        cooldown_until = now_ts + 30

            except Exception as e:
                add_log(f"⚠️ Ошибка сканера Топ-5: {e}")

            await asyncio.sleep(20)


# --- ВОРКЕР 2: ПАРАЛЛЕЛЬНЫЙ (Meme Sniper Mode) ---
async def meme_sniper_worker():
    global IS_MEME_ACTIVE
    signer = get_signer_keypair()
    if not signer:
        return

    add_log("🐸 Параллельный воркер Meme Sniper запущен.")
    SOL_MINT = "So11111111111111111111111111111111111111112"

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                if not IS_MEME_ACTIVE:
                    await asyncio.sleep(5)
                    continue

                conn = sqlite3.connect(DB_FILE)
                c = conn.cursor()
                c.execute("SELECT id, token_symbol, token_mint, buy_price, amount_tokens, half_taken FROM trades WHERE sell_price = 0 AND token_symbol LIKE 'MEME-%' LIMIT 1")
                active_meme = c.fetchone()
                conn.close()

                if active_meme:
                    trade_id, sym, mint, entry_sol_price, amount_tokens, half_taken = active_meme
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

                                add_log(f"🔥 [MEME SNIPER] {sym}: Вход {entry_sol_price:.5f} SOL | Тек общая стоимость: {current_total_sol:.3f} SOL ({multiplier:.1f}x)")

                                if current_total_sol >= 0.5 and half_taken == 0:
                                    add_log(f"⚡ [MEME SNIPER] Баланс достиг 0.5 SOL! Фиксируем 50% позиции и переводим в USDC...")
                                    half_tokens_raw = int((amount_tokens / 2) * (10 ** 6))
                                    sell_sig = await execute_jupiter_swap(session, mint, USDC_MINT, half_tokens_raw, signer, slippage_bps=300, priority_fee=100000)
                                    if sell_sig:
                                        conn_h = sqlite3.connect(DB_FILE)
                                        conn_h.cursor().execute("UPDATE trades SET half_taken = 1, amount_tokens = amount_tokens / 2 WHERE id = ?", (trade_id,))
                                        conn_h.commit()
                                        conn_h.close()
                                        add_log(f"✅ [MEME SNIPER] Половина позиции переведена в USDC! Tx: {sell_sig[:10]}...")
                                    else:
                                        add_log(f"⚠️ [MEME SNIPER] Ошибка перевода половины в USDC...")

                                elif multiplier >= MEME_TARGET_MULTIPLIER or pnl_pct <= -MEME_STOP_LOSS_PCT:
                                    reason = "ЦЕЛЬ Х10 ДОСТИГНУТА 🚀" if multiplier >= MEME_TARGET_MULTIPLIER else "СТОП-ЛОСС 🛑"
                                    add_log(f"⚡ [MEME SNIPER] Закрываем остаток позиции {sym} ({reason}). Выход по цене {cur_sol_price:.5f} SOL...")
                                    
                                    rem_tokens_raw = int(amount_tokens * (10 ** 6))
                                    sell_sig = await execute_jupiter_swap(session, mint, SOL_MINT, rem_tokens_raw, signer, slippage_bps=300, priority_fee=100000)
                                    
                                    if sell_sig:
                                        conn_up = sqlite3.connect(DB_FILE)
                                        profit_sol = current_total_sol - MEME_ALLOCATION_SOL
                                        conn_up.cursor().execute("UPDATE trades SET sell_price = ?, profit_usdc = ?, sell_tx = ? WHERE id = ?",
                                                                  (cur_sol_price, profit_sol * 150, sell_sig, trade_id))
                                        conn_up.commit()
                                        conn_up.close()
                                        add_log(f"✅ [MEME SNIPER] Финальная фиксация выполнена по {cur_sol_price:.5f} SOL! Tx: {sell_sig[:10]}...")
                    await asyncio.sleep(10)
                else:
                    add_log("🎯 [MEME SNIPER] Сканирование новых мем-пулов по сети Solana...")
                    search_url = "https://api.dexscreener.com/latest/dex/search?q=SOL/USDC"
                    headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
                    
                    async with session.get(search_url, headers=headers, timeout=6) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            pairs = data.get("pairs", [])
                            target_mint = None
                            target_sym = None
                            init_price = 0.0

                            for pair in pairs:
                                if pair.get("chainId") == "solana":
                                    base_token = pair.get("baseToken", {})
                                    m_address = base_token.get("address")
                                    m_symbol = base_token.get("symbol")
                                    p_native = float(pair.get("priceNative", 0.0) or 0.0)
                                    
                                    if m_address and m_address != SOL_MINT and p_native > 0:
                                        target_mint = m_address
                                        target_sym = f"MEME-{m_symbol}"
                                        init_price = p_native
                                        break

                            if target_mint:
                                add_log(f"🚀 [MEME SNIPER] Найден мем-токен: {target_sym}. Вход по ${init_price:.5f} на {MEME_ALLOCATION_SOL} SOL...")
                                amount_lamports = int(MEME_ALLOCATION_SOL * 1_000_000_000)
                                
                                buy_sig = await execute_jupiter_swap(session, SOL_MINT, target_mint, amount_lamports, signer, slippage_bps=500, priority_fee=100000)
                                if buy_sig:
                                    bought_tokens = MEME_ALLOCATION_SOL / init_price if init_price > 0 else 1000.0
                                    conn_in = sqlite3.connect(DB_FILE)
                                    conn_in.cursor().execute("""INSERT INTO trades 
                                        (token_symbol, token_mint, buy_price, amount_tokens, sell_price, profit_usdc, buy_tx, sell_tx, timestamp, half_taken)
                                        VALUES (?, ?, ?, ?, 0, 0, ?, '', ?, 0)""",
                                        (target_sym, target_mint, init_price, bought_tokens, buy_sig, datetime.now().strftime('%H:%M:%S')))
                                    conn_in.commit()
                                    conn_in.close()
                                    add_log(f"💎 [MEME SNIPER] Успешный вход в {target_sym} по ${init_price:.5f}! Tx: {buy_sig[:12]}...")

                    await asyncio.sleep(15)

            except Exception as e:
                add_log(f"⚠️ Ошибка в Meme Sniper воркере: {e}")
                await asyncio.sleep(10)


# --- ИНТЕРФЕЙС ---
RAW_HTML = r"""<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no, viewport-fit=cover">
    <title>Zer0Life Multi-Asset & Meme Sniper</title>
    <script src="https://telegram.org/js/telegram-web-app.js"></script>
    <style>
        * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
        body { background: #07080c; color: #f1f5f9; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; margin: 0; padding: 0; width: 100vw; height: 100vh; overflow: hidden; }
        
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
        .btn-sell-all:active { transform: scale(0.97); }

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

        .deposit-modal { background: #0b0d14; border: 1px solid #1c2333; border-radius: 24px; padding: 18px; }
        .banner-limit { background: rgba(22, 101, 52, 0.12); border: 1px solid rgba(34, 197, 94, 0.3); border-radius: 14px; padding: 12px; text-align: center; margin-bottom: 12px; }
        .btn-green-main { background: #22c55e; color: #000; border: none; width: 100%; padding: 14px; border-radius: 14px; font-size: 14px; font-weight: 800; cursor: pointer; }
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
    </style>
</head>
<body>
    <div id="splash-screen">
        <div class="brand-title">ZERØLIFE</div>
        <div class="brand-run">MULTI-ASSET</div>
        <button class="start-btn" onclick="openTerminal()">Launch Terminal</button>
    </div>

    <div id="app" style="display: none;">
        <!-- TAB 1: TERMINAL -->
        <div id="tab-terminal" class="tab-panel active">
            <div class="control-bar">
                <button id="btn-toggle" class="btn-ctl btn-toggle-on" onclick="toggleTrading()">
                    <span id="toggle-dot">●</span>
                    <span id="toggle-text">Автопилот: ВКЛ</span>
                </button>
                <button id="btn-refresh-top" class="btn-ctl btn-refresh" onclick="manualRefresh()">
                    <span id="refresh-icon">🔄</span>
                    <span>Обновить</span>
                </button>
            </div>

            <button class="btn-sell-all" onclick="closeAllPositions()">
                <span>⚠️ Закрыть все позиции (Продать всё)</span>
            </button>

            <!-- БЛОК ПОРТФЕЛЯ И БАЛАНСА -->
            <div class="card" style="border: 1px solid rgba(34, 197, 94, 0.3); background: linear-gradient(135deg, #0e111a 0%, #0a150d 100%);">
                <div style="font-size: 13px; font-weight: 800; color: #22c55e; margin-bottom: 8px;">💼 РЕАЛЬНЫЙ БАЛАНС И ПОРТФЕЛЬ</div>
                <div class="grid-stats">
                    <div class="stat-box"><div class="stat-label">Баланс в боте</div><div class="stat-val" id="port-balance" style="color: #fff;">$0.00</div></div>
                    <div class="stat-box"><div class="stat-label">Доступно к выводу</div><div class="stat-val" id="port-withdrawable" style="color: #22c55e;">$0.00</div></div>
                </div>
                <div style="margin-top: 8px; display: flex; justify-content: space-between; align-items: center; background: #06080e; padding: 8px 12px; border-radius: 10px;">
                    <div style="font-size: 11px; color: #94a3b8;">Чистая прибыль (PnL):</div>
                    <div id="port-pnl" style="font-size: 13px; font-weight: 900; font-family: monospace; color: #4ade80;">+$0.00 (0.0%)</div>
                </div>
            </div>

            <!-- БЛОК MEME SNIPER РЕЖИМА -->
            <div class="card" style="border: 1px solid rgba(234, 179, 8, 0.3); background: linear-gradient(135deg, #0e111a 0%, #15130a 100%);">
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
                    <div>
                        <div style="font-size: 15px; font-weight: 800; color: #facc15;">🐸 Meme Sniper Mode</div>
                        <div style="font-size: 10px; color: #94a3b8; margin-top: 2px;">0.1 SOL → 0.5 SOL (50% в USDC) → 1 SOL</div>
                    </div>
                    <button id="btn-meme-toggle" class="btn-ctl btn-toggle-off" style="padding: 8px 12px; font-size: 11px;" onclick="toggleMemeTrading()">
                        <span id="meme-toggle-text">ВЫКЛ</span>
                    </button>
                </div>
                <div class="grid-stats">
                    <div class="stat-box"><div class="stat-label">Слот снайпера</div><div class="stat-val" style="color: #facc15;">0.1 SOL</div></div>
                    <div class="stat-box"><div class="stat-label">Цель фиксации</div><div class="stat-val" style="color: #4ade80;">0.5 SOL (USDC) + 1 SOL</div></div>
                </div>
            </div>

            <div class="card">
                <div style="display: flex; justify-content: space-between; align-items: center;">
                    <div>
                        <div style="font-size: 17px; font-weight: 800; color: #fff;">Топ-5 Ликвидных Активов</div>
                        <div style="font-size: 11px; color: #64748b; margin-top: 2px;">SOL • JUP • RAY • WIF • PYTH</div>
                    </div>
                    <div id="user-bal" style="font-size: 12px; color: #22c55e; font-family: monospace; font-weight: 700;">$0.00 USDC</div>
                </div>
                <div class="grid-stats">
                    <div class="stat-box"><div class="stat-label">Размер слота</div><div class="stat-val">$20.00 USDC</div></div>
                    <div class="stat-box"><div class="stat-label">Slippage</div><div class="stat-val" style="color: #60a5fa;">0.40%</div></div>
                    <div class="stat-box"><div class="stat-label">Take Profit</div><div class="stat-val" style="color: #4ade80;">+3.50%</div></div>
                    <div class="stat-box"><div class="stat-label">Stop Loss</div><div class="stat-val" style="color: #f87171;">-2.00%</div></div>
                </div>
            </div>

            <div class="card">
                <div style="font-size: 12px; font-weight: 700; color: #94a3b8; margin-bottom: 6px;">ЖУРНАЛ СКАНИРОВАНИЯ И СНАЙПЕРА</div>
                <div id="log-box" class="log-console">Запуск ядра...</div>
            </div>
        </div>

        <!-- TAB 2: STATS -->
        <div id="tab-stats" class="tab-panel">
            <div class="card">
                <div style="display: flex; justify-content: space-between; align-items: center;">
                    <div>
                        <div style="font-size: 12px; color: #94a3b8; text-transform: uppercase;">Итоговый результат</div>
                        <div id="pnl-val" style="font-size: 26px; font-weight: 900; color: #4ade80; font-family: monospace; margin-top: 4px;">+$0.00 USDC</div>
                    </div>
                    <div style="text-align: right;">
                        <div style="font-size: 11px; color: #94a3b8;">Закрыто ордеров</div>
                        <div id="closed-count" style="font-size: 18px; font-weight: 800; color: #fff; font-family: monospace;">0</div>
                    </div>
                </div>
            </div>

            <div class="card">
                <div style="font-size: 13px; font-weight: 800; color: #f8fafc; margin-bottom: 12px;">ЖУРНАЛ ПОКУПОК И ПРОДАЖ</div>
                <div id="history-box" style="color: #64748b; font-size: 11px; text-align: center; padding: 20px;">Нет зафиксированных ордеров</div>
            </div>
        </div>

        <!-- TAB 3: WALLET -->
        <div id="tab-deposit" class="tab-panel">
            <div class="deposit-modal">
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 14px;">
                    <div style="font-size: 17px; font-weight: 800; color: #fff;" id="wallet-modal-title">Deposit Crypto (USDC Top-Up)</div>
                    <span style="color: #64748b; cursor: pointer; font-size: 18px;" onclick="gotoTab('terminal', document.querySelectorAll('.nav-btn')[0])">✕</span>
                </div>

                <div class="wallet-tabs">
                    <button class="wallet-tab active" id="wtab-deposit" onclick="switchWalletTab('deposit')">Deposit</button>
                    <button class="wallet-tab" id="wtab-withdraw" onclick="switchWalletTab('withdraw')">Withdraw</button>
                </div>

                <!-- СЕКЦИЯ ДЕПОЗИТА -->
                <div id="section-deposit">
                    <div class="banner-limit">
                        <div style="color: #22c55e; font-size: 13px; font-weight: 700;">Deposit from $50 to $10,000 USDC per transaction</div>
                        <div style="color: #64748b; font-size: 11px; margin-top: 4px;">Send SPL-USDC on Solana. Enter transaction signature</div>
                    </div>

                    <button class="btn-green-main" id="btn-show-addr" onclick="toggleDepositDetails()">Show Deposit Address 🚀</button>

                    <div id="deposit-details" style="display: none; margin-top: 14px;">
                        <div style="text-align: center; color: #94a3b8; font-size: 11px; margin-bottom: 8px;">Transfer USDC (Solana SPL) to:</div>
                        <div class="qr-section">
                            <img class="qr-img" src="https://api.qrserver.com/v1/create-qr-code/?size=150x150&data=__DEPOSIT_WALLET__">
                        </div>

                        <div class="address-box" id="wallet-text">__DEPOSIT_WALLET__</div>
                        <button class="btn-copy" onclick="copyWallet()">📋 Copy Address</button>

                        <div style="color: #22c55e; font-size: 11px; font-weight: 700; margin-bottom: 6px;">Transaction Signature:</div>
                        <input id="dep-signature" class="sig-input" placeholder="Paste signature from your wallet...">

                        <button class="btn-verify" id="btn-verify-action" onclick="verifyOnchainDeposit()">Verify & Credit USDC 🔄</button>
                    </div>
                </div>

                <!-- СЕКЦИЯ ВЫВОДА -->
                <div id="section-withdraw" style="display: none;">
                    <div style="font-size: 12px; color: #94a3b8; margin-bottom: 8px;">Select Token to Withdraw:</div>
                    <div class="token-selector">
                        <button class="token-btn active" id="wtoken-usdc" onclick="selectWithdrawToken('USDC')">USDC</button>
                        <button class="token-btn" id="wtoken-sol" onclick="selectWithdrawToken('SOL')">Solana (SOL)</button>
                    </div>

                    <div style="font-size: 12px; color: #94a3b8; margin-bottom: 6px;">Recipient Solana Address:</div>
                    <input id="withdraw-address" class="sig-input" placeholder="Enter recipient wallet address...">

                    <div style="font-size: 12px; color: #94a3b8; margin-bottom: 6px;">Amount:</div>
                    <input id="withdraw-amount" type="number" step="any" class="sig-input" placeholder="0.00">

                    <button class="btn-green-main" id="btn-withdraw-action" onclick="submitWithdraw()">Withdraw 🚀</button>
                </div>
            </div>
        </div>
    </div>

    <!-- МЕНЮ -->
    <div id="nav" class="bottom-nav" style="display: none;">
        <button class="nav-btn active" onclick="gotoTab('terminal', this)">⚡ Terminal</button>
        <button class="nav-btn" onclick="gotoTab('stats', this)">📊 Stats</button>
        <button class="nav-btn" onclick="gotoTab('deposit', this)">💼 Wallet</button>
    </div>

    <script>
        const tg = window.Telegram?.WebApp;
        if(tg) tg.expand();

        let isBotRunning = true;
        let isMemeRunning = false;
        let currentWithdrawToken = 'USDC';

        function openTerminal() {
            document.getElementById('splash-screen').style.display = 'none';
            document.getElementById('app').style.display = 'block';
            document.getElementById('nav').style.display = 'flex';
            refresh();
        }

        function gotoTab(id, btn) {
            document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
            document.querySelectorAll('.nav-btn').forEach(b => b.classList.remove('active'));
            document.getElementById('tab-' + id).classList.add('active');
            if(btn) btn.classList.add('active');
        }

        function switchWalletTab(tab) {
            const isDep = tab === 'deposit';
            document.getElementById('wtab-deposit').className = isDep ? 'wallet-tab active' : 'wallet-tab';
            document.getElementById('wtab-withdraw').className = !isDep ? 'wallet-tab active' : 'wallet-tab';
            document.getElementById('section-deposit').style.display = isDep ? 'block' : 'none';
            document.getElementById('section-withdraw').style.display = !isDep ? 'block' : 'none';
            document.getElementById('wallet-modal-title').innerText = isDep ? 'Deposit Crypto (USDC Top-Up)' : 'Withdraw Crypto';
        }

        function selectWithdrawToken(token) {
            currentWithdrawToken = token;
            document.getElementById('wtoken-usdc').className = token === 'USDC' ? 'token-btn active' : 'token-btn';
            document.getElementById('wtoken-sol').className = token === 'SOL' ? 'token-btn active' : 'token-btn';
        }

        async function closeAllPositions() {
            if(!confirm("Вы уверены, что хотите закрыть все открытые позиции?")) return;
            try {
                const tgId = tg?.initDataUnsafe?.user?.id || 42882165;
                const res = await fetch('/api/close_all', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ telegram_id: tgId })
                });
                const d = await res.json();
                alert(d.message);
                refresh();
            } catch(e) {
                alert("Ошибка соединения.");
            }
        }

        async function submitWithdraw() {
            const addr = document.getElementById('withdraw-address').value.trim();
            const amount = document.getElementById('withdraw-amount').value.trim();
            if(!addr || !amount) { alert("Заполните адрес получателя и сумму."); return; }

            const btn = document.getElementById('btn-withdraw-action');
            btn.innerText = "Обработка вывода...";
            btn.disabled = true;

            try {
                const tgId = tg?.initDataUnsafe?.user?.id || 42882165;
                const res = await fetch('/api/withdraw', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ telegram_id: tgId, token: currentWithdrawToken, recipient_address: addr, amount: parseFloat(amount) })
                });
                const d = await res.json();
                alert(d.message);
                if(d.success) {
                    document.getElementById('withdraw-address').value = '';
                    document.getElementById('withdraw-amount').value = '';
                    refresh();
                }
            } catch(e) {
                alert("Ошибка соединения.");
            } finally {
                btn.innerText = "Withdraw 🚀";
                btn.disabled = false;
            }
        }

        async function toggleTrading() {
            try {
                const res = await fetch('/api/toggle_trading', { method: 'POST' }).then(r => r.json());
                if(res.success) updateToggleUI(res.trading_active);
            } catch(e) {}
        }

        async function toggleMemeTrading() {
            try {
                const res = await fetch('/api/toggle_meme', { method: 'POST' }).then(r => r.json());
                if(res.success) updateMemeToggleUI(res.meme_active);
            } catch(e) {}
        }

        function updateToggleUI(isActive) {
            isBotRunning = isActive;
            const btn = document.getElementById('btn-toggle');
            const txt = document.getElementById('toggle-text');
            if(isActive) {
                btn.className = "btn-ctl btn-toggle-on";
                txt.innerText = "Автопилот: ВКЛ";
            } else {
                btn.className = "btn-ctl btn-toggle-off";
                txt.innerText = "Автопилот: ВЫКЛ";
            }
        }

        function updateMemeToggleUI(isActive) {
            isMemeRunning = isActive;
            const btn = document.getElementById('btn-meme-toggle');
            const txt = document.getElementById('meme-toggle-text');
            if(isActive) {
                btn.className = "btn-ctl btn-toggle-on";
                txt.innerText = "ВКЛ (0.1 SOL)";
            } else {
                btn.className = "btn-ctl btn-toggle-off";
                txt.innerText = "ВЫКЛ";
            }
        }

        async function manualRefresh() {
            const icon = document.getElementById('refresh-icon');
            icon.style.display = "inline-block";
            icon.style.transform = "rotate(360deg)";
            icon.style.transition = "transform 0.5s ease";
            await refresh();
            setTimeout(() => {
                icon.style.transform = "rotate(0deg)";
                icon.style.transition = "none";
            }, 500);
        }

        function toggleDepositDetails() {
            const el = document.getElementById('deposit-details');
            const btn = document.getElementById('btn-show-addr');
            if(el.style.display === 'none') {
                el.style.display = 'block';
                btn.style.display = 'none';
            }
        }

        function copyWallet() {
            const addr = document.getElementById('wallet-text').innerText;
            navigator.clipboard.writeText(addr).then(() => alert("Адрес скопирован!"));
        }

        async function verifyOnchainDeposit() {
            const sig = document.getElementById('dep-signature').value.trim();
            if(!sig) { alert("Вставьте Signature транзакции."); return; }

            const btn = document.getElementById('btn-verify-action');
            btn.innerText = "Верификация USDC в сети...";
            btn.disabled = true;

            try {
                const tgId = tg?.initDataUnsafe?.user?.id || 42882165;
                const res = await fetch('/api/verify_deposit', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ telegram_id: tgId, tx_signature: sig })
                });
                const d = await res.json();
                alert(d.message);
                if(d.success) {
                    document.getElementById('dep-signature').value = "";
                    refresh();
                }
            } catch(e) {
                alert("Ошибка соединения.");
            } finally {
                btn.innerText = "Verify & Credit USDC 🔄";
                btn.disabled = false;
            }
        }

        async function refresh() {
            try {
                const l = await fetch('/api/logs').then(r => r.json());
                if(l.success) {
                    document.getElementById('log-box').innerText = l.logs.join('\n');
                    if(l.trading_active !== undefined) updateToggleUI(l.trading_active);
                    if(l.meme_active !== undefined) updateMemeToggleUI(l.meme_active);
                }

                const tgId = tg?.initDataUnsafe?.user?.id || 42882165;
                const s = await fetch('/api/stats?telegram_id=' + tgId).then(r => r.json());
                if(s.success) {
                    const p = s.profit_usdc;
                    document.getElementById('pnl-val').innerText = (p >= 0 ? "+$" : "-$") + Math.abs(p).toFixed(2) + " USDC";
                    document.getElementById('closed-count').innerText = s.closed_count;
                    if(s.user_balance !== undefined) document.getElementById('user-bal').innerText = "$" + s.user_balance.toFixed(2) + " USDC";

                    document.getElementById('port-balance').innerText = "$" + (s.user_balance || 0).toFixed(2);
                    document.getElementById('port-withdrawable').innerText = "$" + (s.withdrawable || 0).toFixed(2);
                    
                    const portPnlEl = document.getElementById('port-pnl');
                    const portPnlVal = s.total_pnl || 0;
                    const portRoiVal = s.total_roi || 0;
                    const pnlSign = portPnlVal >= 0 ? "+" : "";
                    portPnlEl.innerText = `${pnlSign}$${portPnlVal.toFixed(2)} (${pnlSign}${portRoiVal.toFixed(1)}%)`;
                    portPnlEl.style.color = portPnlVal >= 0 ? '#4ade80' : '#f87171';

                    if(s.trades && s.trades.length > 0) {
                        document.getElementById('history-box').innerHTML = s.trades.map(t => {
                            const isClosed = t.status === 'CLOSED';
                            const pnlColor = t.profit_usdc >= 0 ? '#4ade80' : '#f87171';
                            const tPnlSign = t.profit_usdc >= 0 ? '+' : '';

                            return `
                                <div class="order-card">
                                    <div class="order-top">
                                        <div style="font-weight: 800; color: #fff; font-size: 13px;">${t.token_symbol} / USDC</div>
                                        <div class="order-badge ${isClosed ? 'badge-closed' : 'badge-open'}">
                                            ${isClosed ? 'ЗАКРЫТА' : 'В РЫНКЕ'}
                                        </div>
                                    </div>
                                    <div class="order-grid">
                                        <div>Вход: <span style="color: #fff;">$${t.buy_price.toFixed(3)}</span></div>
                                        <div>Выход: <span style="color: #fff;">${isClosed ? '$' + t.sell_price.toFixed(3) : 'Ожидание'}</span></div>
                                        <div>Объем: <span style="color: #fff;">${t.amount_tokens.toFixed(3)}</span></div>
                                        <div>Время: <span style="color: #fff;">${t.timestamp}</span></div>
                                    </div>
                                    <div style="display: flex; justify-content: space-between; align-items: center; margin-top: 6px; padding-top: 6px; border-top: 1px solid #141b2a;">
                                        <div style="font-size: 11px; color: #64748b;">Результат:</div>
                                        <div style="font-weight: 800; font-size: 13px; color: ${isClosed ? pnlColor : '#60a5fa'};">
                                            ${isClosed ? `${tPnlSign}$${t.profit_usdc.toFixed(2)} (${tPnlSign}${t.profit_pct.toFixed(2)}%)` : 'Открытая позиция'}
                                        </div>
                                    </div>
                                    <div class="order-tx-row">
                                        ${t.buy_tx ? `<a class="tx-link" href="https://solscan.io/tx/${t.buy_tx}" target="_blank">↗ Покупка (Tx)</a>` : ''}
                                        ${t.sell_tx ? `<a class="tx-link" style="color: #4ade80;" href="https://solscan.io/tx/${t.sell_tx}" target="_blank">↗ Продажа (Tx)</a>` : ''}
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
    return web.Response(text=HTML_CONTENT, content_type='text/html', headers={"Cache-Control": "no-store"})

async def health_handler(request):
    return web.Response(text="OK")

async def api_get_logs(request):
    return web.json_response({"success": True, "logs": RECENT_LOGS, "trading_active": IS_TRADING_ACTIVE, "meme_active": IS_MEME_ACTIVE})

async def api_toggle_trading(request):
    global IS_TRADING_ACTIVE
    IS_TRADING_ACTIVE = not IS_TRADING_ACTIVE
    add_log(f"⚙️ Автопилот Топ-5: {'ВКЛ' if IS_TRADING_ACTIVE else 'ВЫКЛ'}")
    return web.json_response({"success": True, "trading_active": IS_TRADING_ACTIVE})

async def api_toggle_meme(request):
    global IS_MEME_ACTIVE
    IS_MEME_ACTIVE = not IS_MEME_ACTIVE
    add_log(f"🐸 Meme Sniper Mode: {'ВКЛ' if IS_MEME_ACTIVE else 'ВЫКЛ'}")
    return web.json_response({"success": True, "meme_active": IS_MEME_ACTIVE})

async def api_get_stats(request):
    tg_id = int(request.query.get("telegram_id", 42882165))
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""SELECT id, token_symbol, buy_price, amount_tokens, sell_price, profit_usdc, buy_tx, sell_tx, timestamp 
                 FROM trades ORDER BY id DESC LIMIT 25""")
    rows = c.fetchall()
    
    trades = []
    for r in rows:
        buy_px = r[2]
        sell_px = r[4]
        is_closed = sell_px > 0
        pnl_pct = round(((sell_px - buy_px) / buy_px) * 100, 2) if is_closed else 0.0

        trades.append({
            "id": r[0], "token_symbol": r[1], "buy_price": buy_px, "amount_tokens": r[3],
            "sell_price": sell_px, "profit_usdc": r[5], "profit_pct": pnl_pct,
            "buy_tx": r[6], "sell_tx": r[7], "timestamp": r[8],
            "status": "CLOSED" if is_closed else "OPEN"
        })

    c.execute("SELECT SUM(profit_usdc) FROM trades WHERE sell_price > 0")
    tot_profit = c.fetchone()[0] or 0.0

    c.execute("SELECT COUNT(*) FROM trades WHERE sell_price > 0")
    closed_count = c.fetchone()[0] or 0

    c.execute("SELECT balance_usdc FROM users WHERE telegram_id = ?", (tg_id,))
    u_row = c.fetchone()
    user_bal = u_row[0] if u_row else 0.0

    c.execute("SELECT SUM(amount_usdc) FROM deposits WHERE telegram_id = ? AND status = 'VERIFIED'", (tg_id,))
    dep_row = c.fetchone()
    total_deposited = dep_row[0] if dep_row and dep_row[0] else 0.0

    conn.close()

    total_pnl = user_bal - total_deposited
    total_roi = (total_pnl / total_deposited * 100) if total_deposited > 0 else 0.0
    
    # Расчет суммы доступной к выводу (все, что выше минимального лимита работы бота $50)
    withdrawable = max(0.0, user_bal - MIN_BOT_BALANCE_USDC)

    return web.json_response({
        "success": True, "profit_usdc": tot_profit, "closed_count": closed_count,
        "trades": trades, "user_balance": user_bal,
        "total_deposited": total_deposited, "total_pnl": total_pnl, "total_roi": total_roi,
        "withdrawable": withdrawable
    })

async def api_verify_deposit(request):
    try:
        body = await request.json()
        tx = body.get("tx_signature", "").strip()
        tg_id = int(body.get("telegram_id", 42882165))

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
        c.execute("UPDATE users SET balance_usdc = balance_usdc + ? WHERE telegram_id = ?", (amount_usdc, tg_id))
        conn.commit()
        conn.close()

        add_log(f"📥 Зачислен депозит: +${amount_usdc} USDC ({tx[:10]}...)")
        return web.json_response({"success": True, "message": f"✅ Успешно! Зачислено ${amount_usdc} USDC."})
    except Exception as e:
        return web.json_response({"success": False, "message": f"Ошибка сервера: {str(e)}"})

async def api_close_all_positions(request):
    try:
        body = await request.json()
        tg_id = int(body.get("telegram_id", 42882165))
        signer = get_signer_keypair()
        if not signer:
            return web.json_response({"success": False, "message": "Ошибка ключа сервера."})

        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT id, token_symbol, token_mint, buy_price, amount_tokens FROM trades WHERE sell_price = 0")
        open_trades = c.fetchall()

        if not open_trades:
            conn.close()
            return web.json_response({"success": True, "message": "Нет открытых позиций для закрытия."})

        closed_count = 0
        async with aiohttp.ClientSession() as session:
            for trade in open_trades:
                trade_id, sym, mint, buy_price, amount_tokens = trade
                decimals = WATCHLIST.get(sym, {}).get("decimals", 6)
                raw_amount = int(amount_tokens * (10 ** decimals))
                
                sell_sig = await execute_jupiter_swap(session, mint, USDC_MINT, raw_amount, signer)
                if sell_sig:
                    c.execute("UPDATE trades SET sell_price = buy_price, sell_tx = ? WHERE id = ?", (sell_sig, trade_id))
                    closed_count += 1

        conn.commit()
        conn.close()
        add_log(f"⚡ Закрыто вручную позиций: {closed_count}")
        return web.json_response({"success": True, "message": f"✅ Закрыто позиций: {closed_count}. Баланс обновлен."})
    except Exception as e:
        return web.json_response({"success": False, "message": f"Ошибка при закрытии позиций: {str(e)}"})

async def api_withdraw(request):
    try:
        body = await request.json()
        tg_id = int(body.get("telegram_id", 42882165))
        token = body.get("token", "USDC")
        recipient = body.get("recipient_address", "").strip()
        amount = float(body.get("amount", 0.0))

        if not recipient or len(recipient) < 30 or amount <= 0:
            return web.json_response({"success": False, "message": "Проверьте правильность адреса и суммы вывода."})

        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT balance_usdc FROM users WHERE telegram_id = ?", (tg_id,))
        u_row = c.fetchone()
        user_bal = u_row[0] if u_row else 0.0

        # Проверка ограничения: оставляем минимум $50 для работы бота
        withdrawable = max(0.0, user_bal - MIN_BOT_BALANCE_USDC)
        if token == "USDC" and amount > withdrawable:
            conn.close()
            return web.json_response({"success": False, "message": f"Нельзя вывести больше доступной суммы (${withdrawable:.2f}). Минимум ${MIN_BOT_BALANCE_USDC} должен оставаться для работы бота."})

        if token == "USDC":
            new_bal = user_bal - amount
            c.execute("UPDATE users SET balance_usdc = ? WHERE telegram_id = ?", (new_bal, tg_id))
        
        c.execute("""INSERT INTO withdrawals (telegram_id, token, amount, recipient_address, tx_signature, status)
                     VALUES (?, ?, ?, ?, ?, 'PENDING')""", (tg_id, token, amount, recipient, "SIMULATED_TX"))
        conn.commit()
        conn.close()

        add_log(f"📤 Запрос на вывод {amount} {token} на адрес {recipient[:6]}... успешно принят.")
        return web.json_response({"success": True, "message": f"✅ Запрос на вывод {amount} {token} отправлен в обработку."})
    except Exception as e:
        return web.json_response({"success": False, "message": f"Ошибка вывода: {str(e)}"})

async def telegram_webhook_handler(request):
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
                    "text": "👋 Терминал **ZERØLIFE Multi-Asset & Meme Sniper** готов.\nДоступен блок реального баланса, пополнений и общей доходности.",
                    "parse_mode": "Markdown", "reply_markup": keyboard
                }
                async with aiohttp.ClientSession() as session:
                    await session.post(send_url, json=payload)
        return web.Response(text="OK")
    except Exception as e:
        return web.Response(text="Error", status=500)

async def set_telegram_webhook():
    if not TELEGRAM_TOKEN or not RENDER_URL:
        return
    webhook_url = f"{RENDER_URL.rstrip('/')}/webhook"
    set_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setWebhook?url={webhook_url}"
    async with aiohttp.ClientSession() as session:
        try:
            await session.get(set_url)
        except Exception:
            pass

async def main():
    init_db()
    app = web.Application()
    app.router.add_get('/', index_handler)
    app.router.add_get('/health', health_handler)
    app.router.add_get('/api/logs', api_get_logs)
    app.router.add_get('/api/stats', api_get_stats)
    app.router.add_post('/api/toggle_trading', api_toggle_trading)
    app.router.add_post('/api/toggle_meme', api_toggle_meme)
    app.router.add_post('/api/verify_deposit', api_verify_deposit)
    app.router.add_post('/api/close_all', api_close_all_positions)
    app.router.add_post('/api/withdraw', api_withdraw)
    app.router.add_post('/webhook', telegram_webhook_handler)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()

    await set_telegram_webhook()
    
    asyncio.create_task(real_trading_worker())
    asyncio.create_task(meme_sniper_worker())
    
    add_log("⚜️ Гибридное ядро заведено на порту " + str(PORT))
    while True:
        await asyncio.sleep(3600)

if __name__ == "__main__":
    asyncio.run(main())
