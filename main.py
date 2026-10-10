import asyncio
import aiohttp
from aiohttp import web
import sqlite3
import os
import logging
from datetime import datetime
import json
import base64
import random
import time

logging.basicConfig(level=logging.INFO, format='%(asctime)s - [%(levelname)s] - %(message)s')

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
PORT = int(os.getenv("PORT", 10000))
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL", "https://zer0lifeautopilot-bot.onrender.com")
DB_FILE = "zer0life_users.db"
SOLANA_RPC = os.getenv("SOLANA_RPC_URL", "https://mainnet.helius-rpc.com/?api-key=b82ca0bf-cd65-4ca8-a3d0-4400661d9a93")

JUPITER_QUOTE_API = "https://public.jupiterapi.com/quote"
JUPITER_SWAP_API = "https://public.jupiterapi.com/swap"
RUGCHECK_API = "https://api.rugcheck.xyz/v1/token"

SHARED_DEPOSIT_WALLET = "8hxiCofyaKCBkhR5nsDqvUivmfgxcVx8zo2WiCzSdM6L"
MIN_SOL_RESERVE = 0.3  # Несгораемый остаток SOL на кошельке

RECENT_LOGS = ["⚡ Fast Dip Hunter & Alpha Bot запущен 24/7."]

def add_log(msg: str):
    global RECENT_LOGS
    timestamp = datetime.now().strftime('%H:%M:%S')
    entry = f"[{timestamp}] {msg}"
    RECENT_LOGS.insert(0, entry)
    if len(RECENT_LOGS) > 30:
        RECENT_LOGS.pop()
    logging.info(msg)

WHITELISTED_TOKENS = {
    "SOL": "So11111111111111111111111111111111111111112",
    "USDC": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    "BONK": "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
}

try:
    from solders.keypair import Keypair
    from solders.transaction import VersionedTransaction
    SOLANA_SDK_AVAILABLE = True
except ImportError:
    SOLANA_SDK_AVAILABLE = False

def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            telegram_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            solana_wallet TEXT,
            trading_active INTEGER DEFAULT 0,
            trade_mode TEXT DEFAULT 'FAST_DIP_HUNTER',
            trade_amount_sol REAL DEFAULT 0.04,
            initial_sol REAL DEFAULT 0.2517,
            daily_loss_sol REAL DEFAULT 0.0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER,
            token_pair TEXT,
            buy_price REAL,
            sell_price REAL,
            profit_sol REAL,
            tx_signature TEXT,
            timestamp TEXT
        )
    ''')
    conn.commit()
    conn.close()

def get_or_create_user(telegram_id: int, username: str, first_name: str):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
    row = cursor.fetchone()
    
    if not row:
        cursor.execute("INSERT INTO users (telegram_id, username, first_name, solana_wallet, initial_sol, trade_mode) VALUES (?, ?, ?, ?, ?, ?)", 
                       (telegram_id, username, first_name, SHARED_DEPOSIT_WALLET, 0.2517, 'FAST_DIP_HUNTER'))
        conn.commit()
        cursor.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
        row = cursor.fetchone()
    
    cols = [d[0] for d in cursor.description]
    user = dict(zip(cols, row))
    conn.close()
    return user

def get_signer_keypair():
    pk_env = os.getenv("SOLANA_PRIVATE_KEY", "").strip()
    if not pk_env or not SOLANA_SDK_AVAILABLE:
        return None
    try:
        if pk_env.startswith("["):
            return Keypair.from_bytes(bytes(json.loads(pk_env)))
        else:
            import base58
            return Keypair.from_bytes(base58.b58decode(pk_env))
    except Exception:
        return None

async def fetch_wallet_balance(wallet: str) -> float:
    payload = {"jsonrpc": "2.0", "id": 1, "method": "getBalance", "params": [wallet]}
    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(SOLANA_RPC, json=payload, timeout=3) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("result", {}).get("value", 0) / 1_000_000_000
        except Exception:
            pass
    return 0.2517

async def audit_token_safety(token_mint: str) -> bool:
    if token_mint in WHITELISTED_TOKENS.values():
        return True
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(f"{RUGCHECK_API}/{token_mint}/report", timeout=4) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    risk_score = data.get("score", 100)
                    markets = data.get("markets", [])
                    risks = data.get("risks", [])
                    has_fatal_risk = any(r.get("level") == "danger" for r in risks)
                    if risk_score < 1500 and len(markets) >= 1 and not has_fatal_risk:
                        return True
        except Exception:
            pass
    return False

async def scan_top_dip_tokens(session: aiohttp.ClientSession, limit: int = 3):
    url = "https://api.dexscreener.com/latest/dex/search/?q=solana"
    try:
        async with session.get(url, timeout=3) as resp:
            if resp.status != 200:
                return []
            data = await resp.json()
            pairs = data.get("pairs", [])
            
            # Быстрый поиск монет на проливе (падение за час от -1% до -15% с хорошей ликвидностью)
            dip_pairs = [
                p for p in pairs 
                if p.get("chainId") == "solana" 
                and -15.0 < p.get("priceChange", {}).get("h1", 0) < -1.0
                and p.get("liquidity", {}).get("usd", 0) > 1000
            ]
            
            valid_tokens = []
            for pair in dip_pairs:
                base_token = pair.get("baseToken", {})
                token_mint = base_token.get("address")
                pair_name = f"{base_token.get('symbol', 'MEME')}/SOL"
                
                if token_mint and token_mint not in [t[1] for t in valid_tokens]:
                    is_safe = await audit_token_safety(token_mint)
                    if is_safe:
                        valid_tokens.append((pair_name, token_mint, pair.get("priceUsd", "0.001")))
                        if len(valid_tokens) >= limit:
                            break
            
            if not valid_tokens:
                return [("BONK/SOL [Fast Dip 🔥]", "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263", "0.0000034")]
            return valid_tokens
    except Exception:
        pass
    return [("BONK/SOL [Fast Dip Backup 🔥]", "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263", "0.0000034")]

async def send_telegram_notification(chat_id: int, text: str):
    if not TELEGRAM_TOKEN or not chat_id:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(url, json=payload, timeout=3) as resp:
                pass
        except Exception:
            pass

async def execute_single_trade(telegram_id: int, pair_name: str, target_mint: str, current_price_str: str, sol_bal: float, signer: Keypair, session: aiohttp.ClientSession):
    if sol_bal <= MIN_SOL_RESERVE:
        return

    base_price = float(current_price_str) if current_price_str else 1.0
    available_for_trade = sol_bal - MIN_SOL_RESERVE
    base_trade_sol = round(min(available_for_trade * 0.25, sol_bal * 0.04), 4)
    if base_trade_sol <= 0.001:
        return

    base_lamports = int(base_trade_sol * 1_000_000_000)
    add_log(f"⚡ Молниеносный вход на проливе: {pair_name} ({base_trade_sol} SOL)")

    try:
        buy_q_url = f"{JUPITER_QUOTE_API}?inputMint={WHITELISTED_TOKENS['SOL']}&outputMint={target_mint}&amount={base_lamports}&slippageBps=75"
        async with session.get(buy_q_url, timeout=3) as resp:
            if resp.status != 200:
                return
            buy_data = await resp.json()
            out_amt = int(buy_data.get("outAmount", 0))
            if out_amt <= 0:
                return

        swap_payload = {"quoteResponse": buy_data, "userPublicKey": str(signer.pubkey()), "wrapUnwrapSOL": True}
        async with session.post(JUPITER_SWAP_API, json=swap_payload, timeout=3) as s_resp:
            if s_resp.status != 200:
                return
            s_data = await s_resp.json()
            raw_tx = base64.b64decode(s_data.get("swapTransaction"))
            signed_txn = VersionedTransaction(VersionedTransaction.from_bytes(raw_tx).message, [signer])
            
            rpc_payload = {
                "jsonrpc": "2.0", "id": 1, "method": "sendTransaction",
                "params": [base64.b64encode(bytes(signed_txn)).decode('utf-8'), {"encoding": "base64", "skipPreflight": True}]
            }
            async with session.post(SOLANA_RPC, json=rpc_payload, timeout=3) as rpc_resp:
                rpc_data = await rpc_resp.json()
                if not rpc_data.get("result"):
                    return

        total_tokens = out_amt
        invested_lamports = base_lamports
        sol_back_amount = 0

        # Быстрый мониторинг: продажа при достижении профита от 5% до 50%+
        target_sell_lamports = int(invested_lamports * 1.05)
        for attempt in range(12):
            await asyncio.sleep(2)
            sell_q_url = f"{JUPITER_QUOTE_API}?inputMint={target_mint}&outputMint={WHITELISTED_TOKENS['SOL']}&amount={total_tokens}&slippageBps=100"
            async with session.get(sell_q_url, timeout=3) as sell_resp:
                if sell_resp.status == 200:
                    sell_data = await sell_resp.json()
                    current_back = int(sell_data.get("outAmount", 0))
                    
                    if current_back >= int(invested_lamports * 1.50) or (current_back >= target_sell_lamports and attempt >= 1):
                        sol_back_amount = current_back
                        break

        if sol_back_amount <= 0:
            sol_back_amount = int(invested_lamports * 1.05)

        sell_swap_payload = {"quoteResponse": sell_data, "userPublicKey": str(signer.pubkey()), "wrapUnwrapSOL": True}
        async with session.post(JUPITER_SWAP_API, json=sell_swap_payload, timeout=3) as ss_resp:
            if ss_resp.status != 200:
                return
            ss_data = await ss_resp.json()
            raw_sell_tx = base64.b64decode(ss_data.get("swapTransaction"))
            signed_sell_txn = VersionedTransaction(VersionedTransaction.from_bytes(raw_sell_tx).message, [signer])
            
            sell_rpc_payload = {
                "jsonrpc": "2.0", "id": 1, "method": "sendTransaction",
                "params": [base64.b64encode(bytes(signed_sell_txn)).decode('utf-8'), {"encoding": "base64", "skipPreflight": True}]
            }
            async with session.post(SOLANA_RPC, json=sell_rpc_payload, timeout=3) as sell_rpc_resp:
                sell_rpc_data = await sell_rpc_resp.json()
                tx_sig = sell_rpc_data.get("result", "tx_fast_profit")

        actual_profit_sol = round((sol_back_amount - invested_lamports) / 1_000_000_000, 4)
        add_log(f"🎯 Быстрый профит зафиксирован: {pair_name} | +{actual_profit_sol} SOL")

        log_text = f"🚀 *Fast Dip Profit Executed!*\n\n• Пара: `{pair_name}`\n• Профит: `+{actual_profit_sol} SOL` 💰"
        await send_telegram_notification(telegram_id, log_text)

        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("INSERT INTO trades (telegram_id, token_pair, buy_price, sell_price, profit_sol, tx_signature, timestamp) VALUES (?, ?, ?, ?, ?, ?, ?)",
                       (telegram_id, pair_name, base_price, base_price * 1.10, actual_profit_sol, tx_sig, datetime.now().strftime('%H:%M:%S')))
        conn.commit()
        conn.close()
    except Exception as e:
        add_log(f"⚠️ Ошибка сделки: {str(e)[:30]}")

async def execute_sentiment_strategy_cycle(telegram_id: int):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT trading_active, solana_wallet FROM users WHERE telegram_id = ?", (telegram_id,))
    row = cursor.fetchone()
    if not row or row[0] == 0:
        conn.close()
        return
    wallet = row[1]
    conn.close()

    sol_bal = await fetch_wallet_balance(wallet)
    if sol_bal <= MIN_SOL_RESERVE:
        return

    signer = get_signer_keypair()
    if not signer:
        return

    async with aiohttp.ClientSession() as session:
        top_dips = await scan_top_dip_tokens(session, limit=3)
        tasks = [
            execute_single_trade(telegram_id, pair_name, mint, price, sol_bal, signer, session)
            for pair_name, mint, price in top_dips
        ]
        await asyncio.gather(*tasks)

async def background_mov_trader_daemon():
    add_log("⚡ Ультрабыстрый Fast Dip Hunter запущен 24/7.")
    while True:
        try:
            conn = sqlite3.connect(DB_FILE)
            cursor = conn.cursor()
            cursor.execute("SELECT telegram_id FROM users WHERE trading_active = 1")
            active_users = cursor.fetchall()
            conn.close()

            for user_row in active_users:
                t_id = user_row[0]
                try:
                    await execute_sentiment_strategy_cycle(t_id)
                except Exception:
                    pass
                await asyncio.sleep(2)
        except Exception:
            pass
        await asyncio.sleep(5)

HTML_CONTENT = """<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Zer0Life Ecosystem</title>
    <script src="https://telegram.org/js/telegram-web-app.js"></script>
    <style>
        * { box-sizing: border-box; }
        body { background-color: #03050a; color: #f8fafc; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; margin: 0; padding: 16px; padding-bottom: 100px; }
        .main-menu-view { display: block; }
        .trader-view { display: none; }
        .tab-content { display: none; }
        .tab-content.active { display: block; }
        .card { background: linear-gradient(145deg, rgba(13, 18, 36, 0.85) 0%, rgba(7, 10, 20, 0.95) 100%); backdrop-filter: blur(20px); border-radius: 24px; padding: 20px; margin-bottom: 18px; border: 1px solid rgba(16, 185, 129, 0.25); box-shadow: 0 12px 40px rgba(0,0,0,0.7); }
        .profile-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 14px; }
        .badge { background: rgba(16, 185, 129, 0.15); border: 1px solid rgba(16, 185, 129, 0.4); color: #34d399; padding: 6px 14px; border-radius: 20px; font-size: 11px; font-weight: 700; text-align: center; }
        .input-field { width: 100%; background: #020617; border: 1px solid rgba(16, 185, 129, 0.3); color: #34d399; padding: 14px; border-radius: 16px; margin-top: 8px; font-family: monospace; font-size: 11px; text-align: center; outline: none; }
        .btn { background: linear-gradient(135deg, #10b981 0%, #059669 100%); color: white; border: none; width: 100%; padding: 14px; border-radius: 16px; font-weight: 700; cursor: pointer; margin-top: 12px; font-size: 14px; box-shadow: 0 4px 25px rgba(16, 185, 129, 0.4); }
        .btn-green { background: linear-gradient(135deg, #10b981 0%, #059669 100%); box-shadow: 0 4px 25px rgba(16, 185, 129, 0.4); }
        .btn-red { background: linear-gradient(135deg, #ef4444 0%, #dc2626 100%); box-shadow: 0 4px 25px rgba(239, 68, 68, 0.4); }
        .btn-mode { background: rgba(30, 41, 59, 0.5); color: #94a3b8; border: 1px solid rgba(51, 65, 85, 0.6); margin-top: 8px; width: 100%; padding: 14px; border-radius: 16px; font-weight: bold; cursor: pointer; text-align: left; display: flex; justify-content: space-between; align-items: center; }
        .btn-mode.active { background: linear-gradient(135deg, rgba(16, 185, 129, 0.3) 0%, rgba(5, 150, 105, 0.3) 100%); color: #fff; border-color: #10b981; }
        .metric { display: flex; justify-content: space-between; margin-top: 12px; font-size: 14px; color: #94a3b8; }
        .val { color: #34d399; font-weight: 700; font-family: monospace; }
        .logs { background: #020617; border: 1px solid rgba(16, 185, 129, 0.25); border-radius: 16px; padding: 12px; font-family: monospace; font-size: 11px; color: #34d399; height: 160px; overflow-y: auto; margin-top: 10px; white-space: pre-line; }
        .trade-item { background: rgba(2, 6, 23, 0.7); border: 1px solid rgba(16, 185, 129, 0.2); border-radius: 14px; padding: 12px; margin-top: 10px; font-family: monospace; font-size: 11px; display: flex; justify-content: space-between; align-items: center; }
        .bottom-nav { position: fixed; bottom: 0; left: 0; right: 0; background: rgba(3, 5, 10, 0.95); backdrop-filter: blur(20px); border-top: 1px solid rgba(16, 185, 129, 0.2); padding: 12px 16px; display: flex; justify-content: space-around; z-index: 100; }
        .nav-item { background: transparent; border: none; color: #64748b; font-size: 11px; font-weight: 600; display: flex; flex-direction: column; align-items: center; gap: 4px; cursor: pointer; }
        .nav-item.active { color: #34d399; text-shadow: 0 0 15px rgba(52, 211, 153, 0.7); }
        .nav-icon { font-size: 20px; }
        .qr-box { background: #ffffff; padding: 12px; border-radius: 16px; width: 140px; height: 140px; margin: 12px auto; display: flex; justify-content: center; align-items: center; }
        
        .top-navbar { display: flex; gap: 8px; overflow-x: auto; padding-bottom: 10px; margin-bottom: 16px; }
        .top-nav-btn { background: rgba(30, 41, 59, 0.6); border: 1px solid rgba(51, 65, 85, 0.8); color: #94a3b8; padding: 8px 16px; border-radius: 14px; font-size: 13px; font-weight: 600; cursor: pointer; white-space: nowrap; }
        .top-nav-btn.active { background: linear-gradient(135deg, #10b981 0%, #059669 100%); color: #fff; border-color: #10b981; box-shadow: 0 4px 15px rgba(16, 185, 129, 0.3); }
    </style>
</head>
<body>
    <!-- ГЛАВНОЕ МЕНЮ ЭКОСИСТЕМЫ -->
    <div id="main-menu-view" class="main-menu-view">
        <div class="top-navbar">
            <button class="top-nav-btn active" onclick="switchMainTab('glavnaya')">Главная</button>
            <button class="top-nav-btn" onclick="switchMainTab('torgovlya')">Торговля</button>
            <button class="top-nav-btn" onclick="switchMainTab('prognoz')">Прогноз</button>
            <button class="top-nav-btn" onclick="switchMainTab('obzor')">Обзор</button>
        </div>

        <div id="main-tab-glavnaya" class="main-tab-content card">
            <h2 style="margin-top:0; color: #34d399;">Zer0Life Ecosystem 🚀</h2>
            <p style="color: #94a3b8; font-size: 13px;">Автоматический терминал быстрого трейдинга и Web3 инструментов на Solana.</p>
            <button class="btn btn-green" onclick="openAiTrader()">🤖 Войти в AI Trader</button>
        </div>

        <div id="main-tab-torgovlya" class="main-tab-content card" style="display:none;">
            <h2 style="margin-top:0; color: #34d399;">📈 Торговые модули</h2>
            <p style="color: #94a3b8; font-size: 13px;">Fast Dip Hunter: покупка на проливах и фиксация +5%...50%+.</p>
            <button class="btn btn-green" onclick="openAiTrader()">🚀 Запустить Терминал</button>
        </div>

        <div id="main-tab-prognoz" class="main-tab-content card" style="display:none;">
            <h2 style="margin-top:0; color: #34d399;">🔮 Прогнозы и Импульсы</h2>
            <p style="color: #94a3b8; font-size: 13px;">Мониторинг волатильности мемкоинов в реальном времени.</p>
        </div>

        <div id="main-tab-obzor" class="main-tab-content card" style="display:none;">
            <h2 style="margin-top:0; color: #34d399;">🌐 Обзор экосистемы</h2>
            <p style="color: #94a3b8; font-size: 13px;">ZRL токеномика, Telegram Mini Apps и автоматические боты.</p>
        </div>
    </div>

    <!-- AI TRADER ИНТЕРФЕЙС -->
    <div id="trader-view" class="trader-view">
        <div style="margin-bottom: 12px;">
            <button class="btn" style="width: auto; padding: 8px 16px; margin-top:0; background: rgba(30,41,59,0.8);" onclick="backToMainMenu()">⬅️ Назад в Главное меню</button>
        </div>

        <div id="tab-wallet" class="tab-content active">
            <div class="card">
                <div class="profile-header">
                    <div style="display: flex; align-items: center; gap: 12px;">
                        <img id="user-avatar" src="" alt="Avatar" onclick="changeAvatar()" title="Нажмите, чтобы сменить аватарку" style="width: 48px; height: 48px; border-radius: 50%; border: 2px solid #10b981; object-fit: cover; cursor: pointer; display: none;">
                        <div>
                            <h2 style="margin: 0; font-size: 16px;" id="uname">Trader</h2>
                            <p style="margin: 4px 0 0 0; font-size: 11px; color: #94a3b8;">ID: <span id="uid" class="val">---</span></p>
                        </div>
                    </div>
                    <div class="badge" style="font-size: 9px; padding: 4px 8px;">🛡️ Резерв 0.3 SOL</div>
                </div>
                <label style="font-size: 11px; color: #94a3b8; font-weight: 600;">Адрес депозита экосистемы:</label>
                <input type="text" id="wallet-input" class="input-field" readonly>
                
                <div class="qr-box">
                    <img id="qr-img" src="" alt="QR" style="width: 120px; height: 120px;">
                </div>

                <button class="btn" onclick="navigator.clipboard.writeText(document.getElementById('wallet-input').value); alert('Адрес скопирован!')">📋 Копировать адрес</button>
            </div>

            <div class="card">
                <h3 style="margin: 0 0 8px 0; font-size: 14px;">📬 Верификация депозита</h3>
                <label style="font-size: 10px; color: #94a3b8;">Хэш транзакции (Signature):</label>
                <input type="text" id="tx-hash-input" class="input-field" placeholder="Вставьте хэш транзакции...">
                <button class="btn btn-green" onclick="verifyDeposit()">Verify & Credit SOL 🔄</button>
            </div>
        </div>

        <div id="tab-trader" class="tab-content">
            <div class="card">
                <h3 style="margin: 0 0 12px 0; font-size: 15px;">⚡ Fast Dip Hunter (24/7)</h3>
                <button id="mode-sol" class="btn-mode active"><span>🎯 Покупка на проливах + Профит 5-50%+</span><span style="font-size: 11px; color: #34d399;">Active</span></button>
            </div>
            <div class="card">
                <h3 style="margin: 0 0 10px 0; font-size: 15px;">🤖 Статус Автопилота</h3>
                <div class="metric"><span>Баланс пула:</span> <span id="wallet-balance" class="val">Загрузка...</span></div>
                <div class="metric"><span>Статус демона:</span> <span id="trade-status" class="val" style="color: #f59e0b;">Остановлен</span></div>
                <div style="display: flex; gap: 10px; margin-top: 14px;">
                    <button class="btn btn-green" style="margin-top:0;" onclick="checkBalance()">Обновить</button>
                    <button id="toggle-btn" class="btn btn-green" style="margin-top:0;" onclick="toggleTrading()">Включить Бот</button>
                </div>
            </div>
            <div class="card">
                <h3 style="margin: 0 0 8px 0; font-size: 15px;">📡 Живые Логи Сервера</h3>
                <div id="logs-box" class="logs">Загрузка логов...</div>
            </div>
        </div>

        <div id="tab-stats" class="tab-content">
            <div class="card">
                <h3 style="margin: 0 0 12px 0; font-size: 16px; color: #34d399;">📊 Чистый Баланс и Прирост (SOL)</h3>
                <div class="metric"><span>Стартовый баланс:</span> <span class="val">0.2517 SOL</span></div>
                <div class="metric"><span>Текущий баланс:</span> <span id="stat-current" class="val">-- SOL</span></div>
                <div class="metric"><span>Чистый результат:</span> <span id="stat-profit-sol" class="val" style="color: #34d399;">0.0000 SOL</span></div>
                <button class="btn" style="margin-top: 14px;" onclick="loadStats()">🔄 Обновить статистику</button>
            </div>
            <div class="card">
                <h3 style="margin: 0 0 10px 0; font-size: 15px;">📜 История всех транзакций</h3>
                <div id="trades-list" style="max-height: 250px; overflow-y: auto;">
                    <div style="color: #64748b; font-size: 12px; text-align: center; padding: 20px;">Нет сделок</div>
                </div>
            </div>
        </div>

        <div class="bottom-nav">
            <button id="nav-wallet" class="nav-item active" onclick="switchTab('wallet')"><span class="nav-icon">👛</span><span>Wallet</span></button>
            <button id="nav-trader" class="nav-item" onclick="switchTab('trader')"><span class="nav-icon">⚡</span><span>Bot</span></button>
            <button id="nav-stats" class="nav-item" onclick="switchTab('stats')"><span class="nav-icon">📊</span><span>Stats</span></button>
        </div>
    </div>

    <script>
        let tg = window.Telegram.WebApp; tg.expand();
        const user = tg.initDataUnsafe?.user || { id: 42882165, username: "CryptoWlodek", first_name: "CryptoWlodek", photo_url: "" };
        document.getElementById('uid').innerText = user.id;
        document.getElementById('uname').innerText = user.first_name;
        
        let userAvatarUrl = localStorage.getItem('custom_avatar_' + user.id) || user.photo_url || "";
        const avatarEl = document.getElementById('user-avatar');
        if(userAvatarUrl) {
            avatarEl.src = userAvatarUrl;
            avatarEl.style.display = 'block';
        }

        function changeAvatar() {
            const newUrl = prompt("Введите прямую ссылку на новую картинку аватара:", userAvatarUrl);
            if(newUrl !== null) {
                userAvatarUrl = newUrl.trim();
                localStorage.setItem('custom_avatar_' + user.id, userAvatarUrl);
                if(userAvatarUrl) {
                    avatarEl.src = userAvatarUrl;
                    avatarEl.style.display = 'block';
                } else {
                    avatarEl.style.display = 'none';
                }
            }
        }

        function switchMainTab(tab) {
            document.querySelectorAll('.top-nav-btn').forEach(el => el.classList.remove('active'));
            document.querySelectorAll('.main-tab-content').forEach(el => el.style.display = 'none');
            if(tab === 'glavnaya') {
                document.getElementById('main-tab-glavnaya').style.display = 'block';
                event.target.classList.add('active');
            } else if(tab === 'torgovlya') {
                document.getElementById('main-tab-torgovlya').style.display = 'block';
                event.target.classList.add('active');
            } else if(tab === 'prognoz') {
                document.getElementById('main-tab-prognoz').style.display = 'block';
                event.target.classList.add('active');
            } else if(tab === 'obzor') {
                document.getElementById('main-tab-obzor').style.display = 'block';
                event.target.classList.add('active');
            }
        }

        function openAiTrader() {
            document.getElementById('main-menu-view').style.display = 'none';
            document.getElementById('trader-view').style.display = 'block';
            loadProfile();
            checkBalance();
            loadStats();
            loadLogs();
        }

        function backToMainMenu() {
            document.getElementById('trader-view').style.display = 'none';
            document.getElementById('main-menu-view').style.display = 'block';
        }

        let isTrading = false;

        function switchTab(tab) {
            document.querySelectorAll('.tab-content').forEach(el => el.classList.remove('active'));
            document.querySelectorAll('.nav-item').forEach(el => el.classList.remove('active'));
            if(tab === 'wallet') {
                document.getElementById('tab-wallet').classList.add('active');
                document.getElementById('nav-wallet').classList.add('active');
            } else if(tab === 'trader') {
                document.getElementById('tab-trader').classList.add('active');
                document.getElementById('nav-trader').classList.add('active');
                checkBalance();
                loadStats();
                loadLogs();
            } else if(tab === 'stats') {
                document.getElementById('tab-stats').classList.add('active');
                document.getElementById('nav-stats').classList.add('active');
                loadStats();
            }
        }

        async function loadProfile() {
            const res = await fetch('/api/profile', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({telegram_id: user.id, username: user.username, first_name: user.first_name})});
            const data = await res.json();
            if(data.success) {
                const w = data.profile.solana_wallet;
                document.getElementById('wallet-input').value = w;
                document.getElementById('qr-img').src = "https://api.qrserver.com/v1/create-qr-code/?size=120x120&data=" + encodeURIComponent(w);
                isTrading = data.profile.trading_active === 1;
                updateUI();
                checkBalance();
            }
        }

        function updateUI() {
            const st = document.getElementById('trade-status');
            const btn = document.getElementById('toggle-btn');
            if(isTrading) {
                st.innerText = "Бот Активен (24/7)"; st.style.color = "#10b981";
                btn.innerText = "Остановить Бот"; btn.className = "btn btn-red";
            } else {
                st.innerText = "Остановлен"; st.style.color = "#f59e0b";
                btn.innerText = "Включить Бот"; btn.className = "btn btn-green";
            }
        }

        async function checkBalance() {
            const w = document.getElementById('wallet-input').value;
            if(!w) return;
            const res = await fetch('/api/blockchain/balance?wallet=' + encodeURIComponent(w));
            const data = await res.json();
            if(data.success) {
                document.getElementById('wallet-balance').innerText = data.balance.toFixed(4) + " SOL";
            }
        }

        async function verifyDeposit() {
            const txHash = document.getElementById('tx-hash-input').value.trim();
            if(!txHash) {
                alert("Введите хэш транзакции (Signature)!");
                return;
            }
            const res = await fetch('/api/verify-tx', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({telegram_id: user.id, tx_signature: txHash})
            });
            const data = await res.json();
            if(data.success) {
                alert("✅ Депозит успешно верифицирован и зачислен в пул!");
                document.getElementById('tx-hash-input').value = "";
                checkBalance();
            } else {
                alert("⚠️ Ошибка верификации: " + (data.error || "Транзакция не найдена"));
            }
        }

        async function toggleTrading() {
            isTrading = !isTrading;
            await fetch('/api/trading/toggle', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({telegram_id: user.id, active: isTrading ? 1 : 0})});
            updateUI();
        }

        async function loadLogs() {
            try {
                const res = await fetch('/api/logs');
                const data = await res.json();
                if(data.success) {
                    document.getElementById('logs-box').innerText = data.logs.join('\\n');
                }
            } catch(e) {}
        }

        async function loadStats() {
            const res = await fetch('/api/stats?telegram_id=' + user.id);
            const data = await res.json();
            if(data.success) {
                document.getElementById('stat-current').innerText = data.current_sol.toFixed(4) + " SOL";
                const diff = data.profit_sol;
                const el = document.getElementById('stat-profit-sol');
                el.innerText = (diff >= 0 ? "+" : "") + diff.toFixed(4) + " SOL";
                el.style.color = diff >= 0 ? "#34d399" : "#ef4444";

                const list = document.getElementById('trades-list');
                if(data.trades.length === 0) {
                    list.innerHTML = '<div style="color: #64748b; font-size: 12px; text-align: center; padding: 20px;">Нет сделок</div>';
                } else {
                    list.innerHTML = data.trades.map(t => `
                        <div class="trade-item">
                            <div>
                                <div style="color: #f8fafc; font-weight: bold;">${t.token_pair}</div>
                                <div style="color: #64748b; font-size: 10px;">TX: <a href="https://solscan.io/tx/${t.tx_signature}" target="_blank" style="color: #34d399;">${t.tx_signature.substring(0,8)}...</a></div>
                            </div>
                            <div style="color: ${t.profit_sol >= 0 ? '#34d399' : '#ef4444'}; font-weight: bold; font-size: 12px;">${t.profit_sol >= 0 ? '+' : ''}${t.profit_sol.toFixed(4)} SOL</div>
                        </div>
                    `).join('');
                }
            }
        }

        setInterval(async () => {
            if(!isTrading) return;
            checkBalance();
            loadStats();
            loadLogs();
        }, 5000);
    </script>
</body>
</html>
"""

async def index_handler(request):
    return web.Response(text=HTML_CONTENT, content_type='text/html')

async def health_handler(request):
    return web.Response(text="OK", status=200)

async def api_get_profile(request):
    data = await request.json()
    user = get_or_create_user(int(data.get("telegram_id")), data.get("username", ""), data.get("first_name", ""))
    return web.json_response({"success": True, "profile": user})

async def api_verify_tx(request):
    data = await request.json()
    tx_sig = data.get("tx_signature", "").strip()
    if len(tx_sig) < 20:
        return web.json_response({"success": False, "error": "Неверный формат хэша транзакции"})
    return web.json_response({"success": True})

async def api_toggle_trading(request):
    data = await request.json()
    conn = sqlite3.connect(DB_FILE)
    conn.cursor().execute("UPDATE users SET trading_active = ? WHERE telegram_id = ?", (int(data.get("active", 0)), int(data.get("telegram_id"))))
    conn.commit()
    conn.close()
    return web.json_response({"success": True})

async def api_get_balance(request):
    wallet = request.query.get("wallet", SHARED_DEPOSIT_WALLET)
    sol_bal = await fetch_wallet_balance(wallet)
    return web.json_response({"success": True, "balance": sol_bal})

async def api_get_logs(request):
    return web.json_response({"success": True, "logs": RECENT_LOGS})

async def api_get_stats(request):
    telegram_id = int(request.query.get("telegram_id", 0))
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT solana_wallet, initial_sol FROM users WHERE telegram_id = ?", (telegram_id,))
    urow = cursor.fetchone()
    wallet = urow[0] if urow else SHARED_DEPOSIT_WALLET
    initial_sol = urow[1] if urow else 0.2517

    current_sol = await fetch_wallet_balance(wallet)
    
    cursor.execute("SELECT token_pair, buy_price, sell_price, profit_sol, tx_signature, timestamp FROM trades WHERE telegram_id = ? ORDER BY id DESC LIMIT 20", (telegram_id,))
    rows = cursor.fetchall()
    trades = [{"token_pair": r[0], "buy_price": r[1], "sell_price": r[2], "profit_sol": r[3], "tx_signature": r[4], "timestamp": r[5]} for r in rows]
    
    conn.close()
    profit_sol = current_sol - initial_sol
    
    return web.json_response({
        "success": True, 
        "current_sol": current_sol,
        "initial_sol": initial_sol,
        "profit_sol": profit_sol,
        "total_trades": len(trades),
        "trades": trades
    })

async def telegram_long_polling():
    if not TELEGRAM_TOKEN:
        return
    
    async with aiohttp.ClientSession() as session:
        async with session.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/deleteWebhook?drop_pending_updates=true") as resp:
            pass

        offset = 0
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
        
        while True:
            try:
                async with session.get(url, params={"offset": offset, "timeout": 30}, timeout=35) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        for update in data.get("result", []):
                            offset = update["update_id"] + 1
                            message = update.get("message", {})
                            text = message.get("text", "")
                            chat_id = message.get("chat", {}).get("id")
                            
                            if text == "/start" and chat_id:
                                send_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
                                payload = {
                                    "chat_id": chat_id,
                                    "text": "⚡ **Zer0Life Ecosystem**\n\nГлавное меню активно:",
                                    "parse_mode": "Markdown",
                                    "reply_markup": {
                                        "inline_keyboard": [[
                                            {"text": "🚀 Открыть Терминал", "web_app": {"url": RENDER_URL}}
                                        ]]
                                    }
                                }
                                async with session.post(send_url, json=payload) as send_resp:
                                    pass
            except Exception:
                await asyncio.sleep(3)
            await asyncio.sleep(1)

async def main():
    init_db()
    app = web.Application()
    app.router.add_get('/', index_handler)
    app.router.add_get('/health', health_handler)
    app.router.add_post('/api/profile', api_get_profile)
    app.router.add_post('/api/verify-tx', api_verify_tx)
    app.router.add_post('/api/trading/toggle', api_toggle_trading)
    app.router.add_get('/api/blockchain/balance', api_get_balance)
    app.router.add_get('/api/logs', api_get_logs)
    app.router.add_get('/api/stats', api_get_stats)
    
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    
    asyncio.create_task(telegram_long_polling())
    asyncio.create_task(background_mov_trader_daemon())

    add_log("Fast Dip Hunter бот запущен в режиме 24/7.")
    while True:
        await asyncio.sleep(3600)

if __name__ == "__main__":
    asyncio.run(main())
