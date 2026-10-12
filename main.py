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

IS_TRADING_ACTIVE = True  # Глобальный флаг автопилота (ВКЛ / ВЫКЛ)
RECENT_LOGS = ["⚜️ Multi-Asset Engine v3.6 инициализирован."]

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

    c.execute("PRAGMA table_info(trades)")
    t_cols = [col[1] for col in c.fetchall()]
    if t_cols and "token_symbol" not in t_cols:
        c.execute("DROP TABLE trades")

    c.execute('''CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT, token_symbol TEXT, token_mint TEXT,
        buy_price REAL, amount_tokens REAL, sell_price REAL DEFAULT 0,
        profit_usdc REAL DEFAULT 0.0, buy_tx TEXT, sell_tx TEXT, timestamp TEXT)''')

    c.execute('''CREATE TABLE IF NOT EXISTS deposits (
        id INTEGER PRIMARY KEY AUTOINCREMENT, telegram_id INTEGER, tx_signature TEXT UNIQUE,
        amount_usdc REAL, status TEXT, timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')

    c.execute("PRAGMA table_info(deposits)")
    d_cols = [col[1] for col in c.fetchall()]
    if "amount_usdc" not in d_cols:
        try:
            c.execute("ALTER TABLE deposits ADD COLUMN amount_usdc REAL DEFAULT 0.0")
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
    url = "https://api.coingecko.com/api/v3/simple/price?ids=solana,jupiter-exchange-solana,raydium,wif,pyth-network&vs_currencies=usd"
    try:
        headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            if resp.status == 200:
                data = await resp.json()
                if "solana" in data: prices["SOL"] = float(data["solana"]["usd"])
                if "jupiter-exchange-solana" in data: prices["JUP"] = float(data["jupiter-exchange-solana"]["usd"])
                if "raydium" in data: prices["RAY"] = float(data["raydium"]["usd"])
                if "wif" in data: prices["WIF"] = float(data["wif"]["usd"])
                if "pyth-network" in data: prices["PYTH"] = float(data["pyth-network"]["usd"])
            else:
                add_log(f"⚠️ Ошибка цен CoinGecko, статус: {resp.status}")
    except Exception as e:
        add_log(f"⚠️ Ошибка запроса цен: {str(e)[:40]}")
    return prices

async def execute_jupiter_swap(session: aiohttp.ClientSession, input_mint: str, output_mint: str, amount_raw: int, signer: Keypair) -> str:
    try:
        user_pubkey = str(signer.pubkey())
        headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
        quote_url = f"https://quote-api.jup.ag/v6/quote?inputMint={input_mint}&outputMint={output_mint}&amount={amount_raw}&slippageBps={MAX_SLIPPAGE_BPS}"
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
            "prioritizationFeeLamports": 25000
        }
        async with session.post("https://quote-api.jup.ag/v6/swap", headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=7)) as resp:
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
            "params": [signed_b64, {"skipPreflight": True, "encoding": "base64"}]
        }
        async with session.post(SOLANA_RPC, json=rpc_payload, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            if resp.status == 200:
                data = await resp.json()
                sig = data.get("result")
                if sig:
                    return str(sig)
                else:
                    add_log(f"⚠️ Ошибка отправки Tx: {data.get('error')}")
    except Exception as e:
        add_log(f"⚠️ Исключение в execute_jupiter_swap: {e}")
    return ""

async def real_trading_worker():
    global IS_TRADING_ACTIVE
    add_log("🤖 Сканер Топ-5 активов запущен.")
    signer = get_signer_keypair()
    if not signer:
        add_log("❌ Торговый воркер остановлен: отсутствует приватный ключ.")
        return

    history = {sym: [] for sym in WATCHLIST}
    cooldown_until = 0
    forced_test_done = False

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                now_ts = datetime.now().timestamp()
                prices = await get_token_prices(session)
                
                if prices:
                    add_log(f"📊 Цены: " + ", ".join([f"{s}: ${p:.2f}" for s, p in prices.items()]))
                else:
                    add_log("⚠️ Не удалось получить цены активов...")
                    await asyncio.sleep(10)
                    continue

                for sym, p in prices.items():
                    history[sym].append(p)
                    if len(history[sym]) > 10:
                        history[sym].pop(0)

                conn = sqlite3.connect(DB_FILE)
                c = conn.cursor()
                c.execute("SELECT id, token_symbol, token_mint, buy_price, amount_tokens FROM trades WHERE sell_price = 0 LIMIT 1")
                active_trade = c.fetchone()

                # Управление открытой позицией
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
                            profit_usd = round(TRADE_AMOUNT_USDC * pnl_pct, 2)
                            c.execute("UPDATE trades SET sell_price = ?, profit_usdc = ?, sell_tx = ? WHERE id = ?", 
                                      (cur_px, profit_usd, sell_sig, trade_id))
                            conn.commit()
                            add_log(f"✅ Ордер закрыт! PnL: {profit_usd:+.2f} USDC | Tx: {sell_sig[:10]}...")
                            cooldown_until = now_ts + 30
                        else:
                            add_log(f"⚠️ Ошибка продажи {sym}, повторим позже...")
                    conn.close()
                    await asyncio.sleep(15)
                    continue

                conn.close()

                if not IS_TRADING_ACTIVE:
                    await asyncio.sleep(15)
                    continue

                if now_ts < cooldown_until:
                    await asyncio.sleep(15)
                    continue

                # ГАРАНТИРОВАННЫЙ ТЕСТОВЫЙ ВХОД СРАЗУ ПРИ СТАРТЕ
                if not forced_test_done and "SOL" in prices:
                    forced_test_done = True
                    sym = "SOL"
                    cur_p = prices[sym]
                    mint = WATCHLIST[sym]["mint"]
                    add_log(f"🚀 ТЕСТОВЫЙ ВХОД: Покупка {sym} на ${TRADE_AMOUNT_USDC} USDC...")
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
                        add_log(f"💎 Успешно куплен {sym}! Tx: {buy_sig[:12]}...")
                    else:
                        add_log(f"❌ Ошибка тестового свопа SOL. Проверьте баланс SOL на газе и приватный ключ.")
                    await asyncio.sleep(15)
                    continue

                # Обычное сканирование просадок
                best_candidate = None
                max_dip = 0.0
                for sym, p_list in history.items():
                    if len(p_list) >= 5:
                        avg_p = sum(p_list) / len(p_list)
                        cur_p = p_list[-1]
                        dip = (avg_p - cur_p) / avg_p
                        if dip >= 0.003:
                            if dip > max_dip:
                                max_dip = dip
                                best_candidate = (sym, cur_p)

                if best_candidate:
                    sym, cur_p = best_candidate
                    mint = WATCHLIST[sym]["mint"]
                    add_log(f"🚀 Сигнал входа: {sym} просел на {max_dip*100:.2f}%. Покупка на ${TRADE_AMOUNT_USDC} USDC...")
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
                        add_log(f"💎 Успешно куплен {sym}! Tx: {buy_sig[:12]}...")
                        cooldown_until = now_ts + 30
                    else:
                        add_log(f"⚠️ Ошибка покупки {sym} через Jupiter.")

            except Exception as e:
                add_log(f"⚠️ Ошибка сканера: {e}")

            await asyncio.sleep(15)

# --- ИНТЕРФЕЙС ---
RAW_HTML = r"""<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no, viewport-fit=cover">
    <title>Zer0Life Multi-Asset</title>
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
        .log-console { background: #05070a; border: 1px solid #141926; border-radius: 16px; padding: 12px; font-family: monospace; font-size: 11px; color: #4ade80; height: 260px; overflow-y: auto; white-space: pre-line; line-height: 1.5; }

        .control-bar { display: flex; gap: 10px; margin-bottom: 12px; }
        .btn-ctl {
            flex: 1; padding: 12px 14px; border-radius: 14px; font-weight: 800; font-size: 12px;
            cursor: pointer; display: flex; align-items: center; justify-content: center; gap: 6px;
            border: none; transition: transform 0.15s ease;
        }
        .btn-ctl:active { transform: scale(0.97); }
        .btn-toggle-on { background: rgba(34, 197, 94, 0.15); border: 1px solid rgba(34, 197, 94, 0.4); color: #4ade80; }
        .btn-toggle-off { background: rgba(239, 68, 68, 0.15); border: 1px solid rgba(239, 68, 68, 0.4); color: #f87171; }
        .btn-refresh { background: #151a27; border: 1px solid #232c42; color: #94a3b8; }

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

        .bottom-nav { position: fixed; bottom: 0; left: 0; right: 0; background: rgba(10, 12, 18, 0.95); backdrop-filter: blur(20px); border-top: 1px solid #192033; padding: 12px 16px; display: flex; justify-content: space-around; z-index: 900; }
        .nav-btn { background: transparent; border: none; color: #64748b; font-size: 11px; font-weight: 600; cursor: pointer; }
        .nav-btn.active { color: #22c55e; }
        .tab-panel { display: none; height: 100vh; overflow-y: auto; padding: 16px; padding-bottom: 95px; }
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
                <div style="font-size: 12px; font-weight: 700; color: #94a3b8; margin-bottom: 6px;">ЖУРНАЛ СКАНИРОВАНИЯ</div>
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

        <!-- TAB 3: DEPOSIT -->
        <div id="tab-deposit" class="tab-panel">
            <div class="deposit-modal">
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 14px;">
                    <div style="font-size: 17px; font-weight: 800; color: #fff;">Deposit Crypto (USDC Top-Up)</div>
                    <span style="color: #64748b; cursor: pointer; font-size: 18px;" onclick="gotoTab('terminal', document.querySelectorAll('.nav-btn')[0])">✕</span>
                </div>

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
        </div>
    </div>

    <!-- МЕНЮ -->
    <div id="nav" class="bottom-nav" style="display: none;">
        <button class="nav-btn active" onclick="gotoTab('terminal', this)">⚡ Terminal</button>
        <button class="nav-btn" onclick="gotoTab('stats', this)">📊 Stats</button>
        <button class="nav-btn" onclick="gotoTab('deposit', this)">💼 Deposit</button>
    </div>

    <script>
        const tg = window.Telegram?.WebApp;
        if(tg) tg.expand();

        let isBotRunning = true;

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

        async function toggleTrading() {
            try {
                const res = await fetch('/api/toggle_trading', { method: 'POST' }).then(r => r.json());
                if(res.success) {
                    updateToggleUI(res.trading_active);
                }
            } catch(e) {
                alert("Ошибка смены статуса");
            }
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
            navigator.clipboard.writeText(addr).then(() => {
                alert("Адрес скопирован!");
            });
        }

        async function verifyOnchainDeposit() {
            const sig = document.getElementById('dep-signature').value.trim();
            if(!sig) {
                alert("Вставьте Signature транзакции.");
                return;
            }

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
                    if(l.trading_active !== undefined) {
                        updateToggleUI(l.trading_active);
                    }
                }

                const tgId = tg?.initDataUnsafe?.user?.id || 42882165;
                const s = await fetch('/api/stats?telegram_id=' + tgId).then(r => r.json());
                if(s.success) {
                    const p = s.profit_usdc;
                    document.getElementById('pnl-val').innerText = (p >= 0 ? "+$" : "-$") + Math.abs(p).toFixed(2) + " USDC";
                    document.getElementById('closed-count').innerText = s.closed_count;

                    if(s.user_balance !== undefined) {
                        document.getElementById('user-bal').innerText = "$" + s.user_balance.toFixed(2) + " USDC";
                    }

                    if(s.trades && s.trades.length > 0) {
                        document.getElementById('history-box').innerHTML = s.trades.map(t => {
                            const isClosed = t.status === 'CLOSED';
                            const pnlColor = t.profit_usdc >= 0 ? '#4ade80' : '#f87171';
                            const pnlSign = t.profit_usdc >= 0 ? '+' : '';

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
                                        <div>Объем: <span style="color: #fff;">${t.amount_tokens.toFixed(3)} ${t.token_symbol}</span></div>
                                        <div>Время: <span style="color: #fff;">${t.timestamp}</span></div>
                                    </div>
                                    <div style="display: flex; justify-content: space-between; align-items: center; margin-top: 6px; padding-top: 6px; border-top: 1px solid #141b2a;">
                                        <div style="font-size: 11px; color: #64748b;">Результат:</div>
                                        <div style="font-weight: 800; font-size: 13px; color: ${isClosed ? pnlColor : '#60a5fa'};">
                                            ${isClosed ? `${pnlSign}$${t.profit_usdc.toFixed(2)} (${pnlSign}${t.profit_pct.toFixed(2)}%)` : 'Открытая позиция'}
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
    return web.json_response({"success": True, "logs": RECENT_LOGS, "trading_active": IS_TRADING_ACTIVE})

async def api_toggle_trading(request):
    global IS_TRADING_ACTIVE
    IS_TRADING_ACTIVE = not IS_TRADING_ACTIVE
    status_str = "ВКЛЮЧЕН" if IS_TRADING_ACTIVE else "ВЫКЛЮЧЕН"
    add_log(f"⚙️ Автопилот торговли вручную {status_str}.")
    return web.json_response({"success": True, "trading_active": IS_TRADING_ACTIVE})

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
            "id": r[0],
            "token_symbol": r[1],
            "buy_price": buy_px,
            "amount_tokens": r[3],
            "sell_price": sell_px,
            "profit_usdc": r[5],
            "profit_pct": pnl_pct,
            "buy_tx": r[6],
            "sell_tx": r[7],
            "timestamp": r[8],
            "status": "CLOSED" if is_closed else "OPEN"
        })

    c.execute("SELECT SUM(profit_usdc) FROM trades WHERE sell_price > 0")
    tot_profit = c.fetchone()[0] or 0.0

    c.execute("SELECT COUNT(*) FROM trades WHERE sell_price > 0")
    closed_count = c.fetchone()[0] or 0

    c.execute("SELECT balance_usdc FROM users WHERE telegram_id = ?", (tg_id,))
    u_row = c.fetchone()
    user_bal = u_row[0] if u_row else 0.0
    conn.close()

    return web.json_response({
        "success": True,
        "profit_usdc": tot_profit,
        "closed_count": closed_count,
        "trades": trades,
        "user_balance": user_bal
    })

async def api_verify_deposit(request):
    try:
        body = await request.json()
        tx = body.get("tx_signature", "").strip()
        tg_id = int(body.get("telegram_id", 42882165))

        if not tx or len(tx) < 60:
            return web.json_response({"success": False, "message": "Некорректная сигнатура транзакции Solana."})

        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT id FROM deposits WHERE tx_signature = ?", (tx,))
        if c.fetchone():
            conn.close()
            return web.json_response({"success": False, "message": "Эта транзакция уже была зачислена ранее!"})

        async with aiohttp.ClientSession() as session:
            valid, amount_usdc, msg = await verify_solana_usdc_tx(session, tx, SHARED_DEPOSIT_WALLET)

        if not valid:
            conn.close()
            return web.json_response({"success": False, "message": f"Отказ: {msg}"})

        c.execute("INSERT INTO deposits (telegram_id, tx_signature, amount_usdc, status) VALUES (?, ?, ?, 'VERIFIED')", 
                  (tg_id, tx, amount_usdc))
        c.execute("UPDATE users SET balance_usdc = balance_usdc + ? WHERE telegram_id = ?", (amount_usdc, tg_id))
        conn.commit()
        conn.close()

        add_log(f"📥 Зачислен ончейн-депозит: +${amount_usdc} USDC ({tx[:10]}...)")
        return web.json_response({"success": True, "message": f"✅ Успешно! Зачислено ${amount_usdc} USDC на ваш баланс."})

    except Exception as e:
        return web.json_response({"success": False, "message": f"Ошибка сервера: {str(e)}"})

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
                             ON CONFLICT(telegram_id) DO UPDATE SET username=excluded.username""", 
                          (chat_id, username))
                conn.commit()
                conn.close()

                keyboard = {
                    "inline_keyboard": [[{"text": "🚀 Launch Terminal", "web_app": {"url": RENDER_URL}}]]
                }
                send_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
                payload = {
                    "chat_id": chat_id,
                    "text": "👋 Терминал **ZERØLIFE Multi-Asset** готов к работе.\nДепозит USDC от $50 до $10 000, аналитика ордеров и мониторинг рынка.",
                    "parse_mode": "Markdown",
                    "reply_markup": keyboard
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
    app.router.add_post('/api/verify_deposit', api_verify_deposit)
    app.router.add_post('/webhook', telegram_webhook_handler)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()

    await set_telegram_webhook()
    asyncio.create_task(real_trading_worker())
    add_log("⚜️ Ядро запущено на порту " + str(PORT))
    while True:
        await asyncio.sleep(3600)

if __name__ == "__main__":
    asyncio.run(main())
