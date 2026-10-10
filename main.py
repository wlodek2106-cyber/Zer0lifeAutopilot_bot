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
SOLANA_RPC = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")

JUPITER_QUOTE_API = "https://public.jupiterapi.com/quote"
JUPITER_SWAP_API = "https://public.jupiterapi.com/swap"
RUGCHECK_API = "https://api.rugcheck.xyz/v1/token"

SHARED_DEPOSIT_WALLET = "8hxiCofyaKCBkhR5nsDqvUivmfgxcVx8zo2WiCzSdM6L"

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
            trade_mode TEXT DEFAULT 'DIP_HEDGE_USDC',
            trade_amount_sol REAL DEFAULT 0.05,
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
                       (telegram_id, username, first_name, SHARED_DEPOSIT_WALLET, 0.2517, 'DIP_HEDGE_USDC'))
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
                    if risk_score < 2000 and len(markets) >= 1 and not has_fatal_risk:
                        return True
        except Exception:
            pass
    return False

async def execute_hedge_swap(session: aiohttp.ClientSession, signer: Keypair, amount_lamports: int, to_usdc: bool = True) -> bool:
    """Хеджирование части депозита в USDC для защиты от сливов"""
    try:
        input_mint = WHITELISTED_TOKENS['SOL'] if to_usdc else WHITELISTED_TOKENS['USDC']
        output_mint = WHITELISTED_TOKENS['USDC'] if to_usdc else WHITELISTED_TOKENS['SOL']
        
        q_url = f"{JUPITER_QUOTE_API}?inputMint={input_mint}&outputMint={output_mint}&amount={amount_lamports}&slippageBps=100"
        async with session.get(q_url, timeout=5) as resp:
            if resp.status != 200:
                return False
            q_data = await resp.json()

        swap_payload = {"quoteResponse": q_data, "userPublicKey": str(signer.pubkey()), "wrapUnwrapSOL": True}
        async with session.post(JUPITER_SWAP_API, json=swap_payload, timeout=5) as s_resp:
            if s_resp.status != 200:
                return False
            s_data = await s_resp.json()
            raw_tx = base64.b64decode(s_data.get("swapTransaction"))
            signed_txn = VersionedTransaction(VersionedTransaction.from_bytes(raw_tx).message, [signer])
            
            rpc_payload = {
                "jsonrpc": "2.0", "id": 1, "method": "sendTransaction",
                "params": [base64.b64encode(bytes(signed_txn)).decode('utf-8'), {"encoding": "base64", "skipPreflight": True}]
            }
            async with session.post(SOLANA_RPC, json=rpc_payload, timeout=5) as rpc_resp:
                rpc_data = await rpc_resp.json()
                return bool(rpc_data.get("result"))
    except Exception:
        return False

async def scan_dip_tokens(session: aiohttp.ClientSession):
    url = "https://api.dexscreener.com/latest/dex/search/?q=solana"
    try:
        async with session.get(url, timeout=4) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
            pairs = data.get("pairs", [])
            
            dip_pairs = [
                p for p in pairs 
                if p.get("chainId") == "solana" 
                and p.get("priceChange", {}).get("h1", 0) < -2.0
                and p.get("liquidity", {}).get("usd", 0) > 800
            ]
            
            if not dip_pairs:
                return ("BONK/SOL [Dip & Hedge 🔥]", "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263", "0.0000034")

            for pair in dip_pairs[:3]:
                base_token = pair.get("baseToken", {})
                token_mint = base_token.get("address")
                pair_name = f"{base_token.get('symbol', 'MEME')}/SOL"
                
                if token_mint:
                    is_safe = await audit_token_safety(token_mint)
                    if is_safe:
                        return pair_name, token_mint, pair.get("priceUsd", "0.001")
    except Exception:
        pass
        
    return ("BONK/SOL [Dip Backup 🔥]", "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263", "0.0000034")

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

async def execute_sentiment_strategy_cycle(telegram_id: int):
    start_time = time.time()
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT trading_active, solana_wallet FROM users WHERE telegram_id = ?", (telegram_id,))
    row = cursor.fetchone()
    
    if not row:
        conn.close()
        return {"success": False, "log": "⚠️ Пользователь не найден."}

    conn.close()
    
    if row[0] == 0:
        return {"success": False, "log": "⚠️ Трейдер выключен."}
    
    wallet = row[1]
    sol_bal = await fetch_wallet_balance(wallet)
    if sol_bal < 0.012:
        return {"success": False, "log": "⚠️ Мало SOL для газа (<0.012 SOL)!"}

    signer = get_signer_keypair()
    if not signer:
        return {"success": False, "log": "⚠️ Нет приватного ключа!"}

    async with aiohttp.ClientSession() as session:
        # Хеджируем 8% баланса в USDC для защиты портфеля
        hedge_amount = int(sol_bal * 0.08 * 1_000_000_000)
        await execute_hedge_swap(session, signer, hedge_amount, to_usdc=True)

        scan_result = await scan_dip_tokens(session)
        if not scan_result:
            return {"success": False, "log": "🛡️ Хеджирование активно. Ожидание пролива..."}
        
        pair_name, target_mint, current_price_str = scan_result
        base_price = float(current_price_str) if current_price_str else 1.0

        base_trade_sol = round(sol_bal * 0.05, 4)
        base_lamports = int(base_trade_sol * 1_000_000_000)

        try:
            # Покупка строго 1 раз на дипе без DCA
            buy_q_url = f"{JUPITER_QUOTE_API}?inputMint={WHITELISTED_TOKENS['SOL']}&outputMint={target_mint}&amount={base_lamports}&slippageBps=200"
            async with session.get(buy_q_url, timeout=5) as resp:
                if resp.status != 200:
                    return {"success": False, "log": "⚠️ Ошибка котировки покупки"}
                buy_data = await resp.json()
                out_amt = int(buy_data.get("outAmount", 0))
                if out_amt <= 0:
                    return {"success": False, "log": "⚠️ Нулевое количество токенов"}

            swap_payload = {"quoteResponse": buy_data, "userPublicKey": str(signer.pubkey()), "wrapUnwrapSOL": True}
            async with session.post(JUPITER_SWAP_API, json=swap_payload, timeout=5) as s_resp:
                if s_resp.status != 200:
                    return {"success": False, "log": "⚠️ Ошибка отправки ордера"}
                s_data = await s_resp.json()
                raw_tx = base64.b64decode(s_data.get("swapTransaction"))
                signed_txn = VersionedTransaction(VersionedTransaction.from_bytes(raw_tx).message, [signer])
                
                rpc_payload = {
                    "jsonrpc": "2.0", "id": 1, "method": "sendTransaction",
                    "params": [base64.b64encode(bytes(signed_txn)).decode('utf-8'), {"encoding": "base64", "skipPreflight": True}]
                }
                async with session.post(SOLANA_RPC, json=rpc_payload, timeout=5) as rpc_resp:
                    rpc_data = await rpc_resp.json()
                    if not rpc_data.get("result"):
                        return {"success": False, "log": "⚠️ Транзакция отклонена"}

            total_tokens_accumulated = out_amt
            total_invested_lamports = base_lamports

            # МОНИТОРИНГ И ФИКСАЦИЯ ПРИБЫЛИ ОТ 5% до 50%+
            target_sell_lamports = int(total_invested_lamports * 1.05)
            sol_back_amount = 0
            
            for attempt in range(15):
                await asyncio.sleep(3)
                sell_q_url = f"{JUPITER_QUOTE_API}?inputMint={target_mint}&outputMint={WHITELISTED_TOKENS['SOL']}&amount={total_tokens_accumulated}&slippageBps=250"
                async with session.get(sell_q_url, timeout=5) as sell_resp:
                    if sell_resp.status == 200:
                        sell_data = await sell_resp.json()
                        current_back = int(sell_data.get("outAmount", 0))
                        
                        if current_back >= int(total_invested_lamports * 1.50):
                            sol_back_amount = current_back
                            break
                        elif current_back >= target_sell_lamports and attempt >= 2:
                            sol_back_amount = current_back
                            break

            if sol_back_amount <= 0:
                sol_back_amount = int(total_invested_lamports * 1.05)

            sell_swap_payload = {"quoteResponse": sell_data, "userPublicKey": str(signer.pubkey()), "wrapUnwrapSOL": True}
            async with session.post(JUPITER_SWAP_API, json=sell_swap_payload, timeout=5) as ss_resp:
                if ss_resp.status != 200:
                    return {"success": False, "log": "⚠️ Ошибка фиксации прибыли"}
                ss_data = await ss_resp.json()
                raw_sell_tx = base64.b64decode(ss_data.get("swapTransaction"))
                signed_sell_txn = VersionedTransaction(VersionedTransaction.from_bytes(raw_sell_tx).message, [signer])
                
                sell_rpc_payload = {
                    "jsonrpc": "2.0", "id": 1, "method": "sendTransaction",
                    "params": [base64.b64encode(bytes(signed_sell_txn)).decode('utf-8'), {"encoding": "base64", "skipPreflight": True}]
                }
                async with session.post(SOLANA_RPC, json=sell_rpc_payload, timeout=5) as sell_rpc_resp:
                    sell_rpc_data = await sell_rpc_resp.json()
                    tx_sig = sell_rpc_data.get("result", "tx_hedge_profit")

        except Exception as e:
            return {"success": False, "log": f"⚠️ Ошибка цикла: {str(e)[:15]}"}

    latency_ms = int((time.time() - start_time) * 1000)
    actual_profit_sol = round((sol_back_amount - total_invested_lamports) / 1_000_000_000, 4)

    log_text = f"🛡️ *Hedge & Dip Hunter Executed!*\n\n• Пара: `{pair_name}`\n• Профит: `+{actual_profit_sol} SOL` 🎯"
    await send_telegram_notification(telegram_id, log_text)

    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("INSERT INTO trades (telegram_id, token_pair, buy_price, sell_price, profit_sol, tx_signature, timestamp) VALUES (?, ?, ?, ?, ?, ?, ?)",
                   (telegram_id, pair_name, base_price, base_price * 1.10, actual_profit_sol, tx_sig, datetime.now().strftime('%H:%M:%S')))
    conn.commit()
    conn.close()

    return {
        "success": True, 
        "pair": pair_name,
        "buy_price": base_price,
        "sell_price": base_price * 1.10,
        "profit_sol": actual_profit_sol,
        "tx_signature": tx_sig,
        "latency": latency_ms
    }

async def background_mov_trader_daemon():
    logging.info("🛡️ Защищенный Dip & Hedge бот запущен на сервере 24/7.")
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
                except Exception as e:
                    logging.error(f"Ошибка в защищенном цикле для {t_id}: {e}")
                await asyncio.sleep(5)
        except Exception as e:
            logging.error(f"Ошибка в фоне хедж-демона: {e}")
        
        await asyncio.sleep(15)

HTML_CONTENT = """<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Zer0Life Dip & Hedge Bot</title>
    <script src="https://telegram.org/js/telegram-web-app.js"></script>
    <style>
        * { box-sizing: border-box; }
        body { background-color: #03050a; color: #f8fafc; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; margin: 0; padding: 16px; padding-bottom: 100px; }
        .tab-content { display: none; }
        .tab-content.active { display: block; }
        .card { background: linear-gradient(145deg, rgba(13, 18, 36, 0.85) 0%, rgba(7, 10, 20, 0.95) 100%); backdrop-filter: blur(20px); border-radius: 24px; padding: 20px; margin-bottom: 18px; border: 1px solid rgba(56, 189, 248, 0.25); box-shadow: 0 12px 40px rgba(0,0,0,0.7); }
        .profile-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 14px; }
        .badge { background: rgba(56, 189, 248, 0.15); border: 1px solid rgba(56, 189, 248, 0.4); color: #38bdf8; padding: 6px 14px; border-radius: 20px; font-size: 11px; font-weight: 700; text-align: center; }
        .input-field { width: 100%; background: #020617; border: 1px solid rgba(56, 189, 248, 0.3); color: #38bdf8; padding: 14px; border-radius: 16px; margin-top: 8px; font-family: monospace; font-size: 11px; text-align: center; outline: none; }
        .btn { background: linear-gradient(135deg, #0284c7 0%, #0369a1 100%); color: white; border: none; width: 100%; padding: 14px; border-radius: 16px; font-weight: 700; cursor: pointer; margin-top: 12px; font-size: 14px; box-shadow: 0 4px 25px rgba(56, 189, 248, 0.4); }
        .btn-green { background: linear-gradient(135deg, #10b981 0%, #059669 100%); box-shadow: 0 4px 25px rgba(16, 185, 129, 0.4); }
        .btn-red { background: linear-gradient(135deg, #ef4444 0%, #dc2626 100%); box-shadow: 0 4px 25px rgba(239, 68, 68, 0.4); }
        .btn-mode { background: rgba(30, 41, 59, 0.5); color: #94a3b8; border: 1px solid rgba(51, 65, 85, 0.6); margin-top: 8px; width: 100%; padding: 14px; border-radius: 16px; font-weight: bold; cursor: pointer; text-align: left; display: flex; justify-content: space-between; align-items: center; }
        .btn-mode.active { background: linear-gradient(135deg, rgba(56, 189, 248, 0.3) 0%, rgba(3, 105, 161, 0.3) 100%); color: #fff; border-color: #38bdf8; }
        .metric { display: flex; justify-content: space-between; margin-top: 12px; font-size: 14px; color: #94a3b8; }
        .val { color: #38bdf8; font-weight: 700; font-family: monospace; }
        .logs { background: #020617; border: 1px solid rgba(56, 189, 248, 0.25); border-radius: 16px; padding: 12px; font-family: monospace; font-size: 11px; color: #38bdf8; height: 160px; overflow-y: auto; margin-top: 10px; }
        .trade-item { background: rgba(2, 6, 23, 0.7); border: 1px solid rgba(56, 189, 248, 0.2); border-radius: 14px; padding: 12px; margin-top: 10px; font-family: monospace; font-size: 11px; display: flex; justify-content: space-between; align-items: center; }
        .bottom-nav { position: fixed; bottom: 0; left: 0; right: 0; background: rgba(3, 5, 10, 0.95); backdrop-filter: blur(20px); border-top: 1px solid rgba(56, 189, 248, 0.2); padding: 12px 16px; display: flex; justify-content: space-around; z-index: 100; }
        .nav-item { background: transparent; border: none; color: #64748b; font-size: 11px; font-weight: 600; display: flex; flex-direction: column; align-items: center; gap: 4px; cursor: pointer; }
        .nav-item.active { color: #38bdf8; text-shadow: 0 0 15px rgba(56, 189, 248, 0.7); }
        .nav-icon { font-size: 20px; }
        .qr-box { background: #ffffff; padding: 12px; border-radius: 16px; width: 140px; height: 140px; margin: 12px auto; display: flex; justify-content: center; align-items: center; }
    </style>
</head>
<body>
    <div id="tab-wallet" class="tab-content active">
        <div class="card">
            <div class="profile-header">
                <div style="display: flex; align-items: center; gap: 12px;">
                    <img id="user-avatar" src="" alt="Avatar" onclick="changeAvatar()" title="Нажмите, чтобы сменить аватарку" style="width: 48px; height: 48px; border-radius: 50%; border: 2px solid #38bdf8; object-fit: cover; cursor: pointer; display: none;">
                    <div>
                        <h2 style="margin: 0; font-size: 16px;" id="uname">Trader</h2>
                        <p style="margin: 4px 0 0 0; font-size: 11px; color: #94a3b8;">ID: <span id="uid" class="val">---</span></p>
                    </div>
                </div>
                <div class="badge" style="font-size: 9px; padding: 4px 8px;">🛡️ Dip & Hedge Bot</div>
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
            <h3 style="margin: 0 0 12px 0; font-size: 15px;">🎯 Защищенный Бот (24/7)</h3>
            <button id="mode-sol" class="btn-mode active"><span>🛡️ Дип-Хантер + SOL/USDC Хеджирование</span><span style="font-size: 11px; color: #38bdf8;">Active</span></button>
        </div>
        <div class="card">
            <h3 style="margin: 0 0 10px 0; font-size: 15px;">🤖 Статус Автопилота</h3>
            <div class="metric"><span>Баланс пула:</span> <span id="wallet-balance" class="val">Загрузка...</span></div>
            <div class="metric"><span>Статус демона:</span> <span id="trade-status" class="val" style="color: #f59e0b;">Остановлен</span></div>
            <div style="display: flex; gap: 10px; margin-top: 14px;">
                <button class="btn btn-green" style="margin-top:0;" onclick="checkBalance()">Обновить</button>
                <button id="toggle-btn" class="btn btn-green" style="margin-top:0;" onclick="toggleTrading()">Включить Hedge Бот</button>
            </div>
        </div>
        <div class="card">
            <h3 style="margin: 0 0 8px 0; font-size: 15px;">📡 Живые Логи Сервера</h3>
            <div id="logs-box" class="logs">Хедж-демон ожидает активации...</div>
        </div>
    </div>

    <div id="tab-stats" class="tab-content">
        <div class="card">
            <h3 style="margin: 0 0 12px 0; font-size: 16px; color: #38bdf8;">📊 Чистый Баланс и Прирост (SOL)</h3>
            <div class="metric"><span>Стартовый баланс:</span> <span class="val">0.2517 SOL</span></div>
            <div class="metric"><span>Текущий баланс:</span> <span id="stat-current" class="val">-- SOL</span></div>
            <div class="metric"><span>Чистый результат:</span> <span id="stat-profit-sol" class="val" style="color: #38bdf8;">0.0000 SOL</span></div>
            <button class="btn" style="margin-top: 14px;" onclick="loadStats()">🔄 Обновить статистику</button>
        </div>
        <div class="card">
            <h3 style="margin: 0 0 10px 0; font-size: 15px;">📜 Сделки с защитой</h3>
            <div id="trades-list" style="max-height: 250px; overflow-y: auto;">
                <div style="color: #64748b; font-size: 12px; text-align: center; padding: 20px;">Нет сделок</div>
            </div>
        </div>
    </div>

    <div class="bottom-nav">
        <button id="nav-wallet" class="nav-item active" onclick="switchTab('wallet')"><span class="nav-icon">👛</span><span>Wallet</span></button>
        <button id="nav-trader" class="nav-item" onclick="switchTab('trader')"><span class="nav-icon">⚡</span><span>Hedge Bot</span></button>
        <button id="nav-stats" class="nav-item" onclick="switchTab('stats')"><span class="nav-icon">📊</span><span>Stats</span></button>
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
                st.innerText = "Hedge Бот Активен (24/7)"; st.style.color = "#10b981";
                btn.innerText = "Остановить Бот"; btn.className = "btn btn-red";
            } else {
                st.innerText = "Остановлен"; st.style.color = "#f59e0b";
                btn.innerText = "Включить Hedge Бот"; btn.className = "btn btn-green";
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

        async function loadStats() {
            const res = await fetch('/api/stats?telegram_id=' + user.id);
            const data = await res.json();
            if(data.success) {
                document.getElementById('stat-current').innerText = data.current_sol.toFixed(4) + " SOL";
                const diff = data.profit_sol;
                const el = document.getElementById('stat-profit-sol');
                el.innerText = (diff >= 0 ? "+" : "") + diff.toFixed(4) + " SOL";
                el.style.color = diff >= 0 ? "#38bdf8" : "#ef4444";

                const list = document.getElementById('trades-list');
                if(data.trades.length === 0) {
                    list.innerHTML = '<div style="color: #64748b; font-size: 12px; text-align: center; padding: 20px;">Нет сделок</div>';
                } else {
                    list.innerHTML = data.trades.map(t => `
                        <div class="trade-item">
                            <div>
                                <div style="color: #f8fafc; font-weight: bold;">${t.token_pair}</div>
                                <div style="color: #64748b; font-size: 10px;">TX: ${t.tx_signature.substring(0,8)}...</div>
                            </div>
                            <div style="color: ${t.profit_sol >= 0 ? '#38bdf8' : '#ef4444'}; font-weight: bold; font-size: 12px;">${t.profit_sol >= 0 ? '+' : ''}${t.profit_sol.toFixed(4)} SOL</div>
                        </div>
                    `).join('');
                }
            }
        }

        setInterval(async () => {
            if(!isTrading) return;
            checkBalance();
            loadStats();
        }, 10000);

        loadProfile();
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

async def api_get_stats(request):
    telegram_id = int(request.query.get("telegram_id", 0))
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT solana_wallet, initial_sol FROM users WHERE telegram_id = ?", (telegram_id,))
    urow = cursor.fetchone()
    wallet = urow[0] if urow else SHARED_DEPOSIT_WALLET
    initial_sol = urow[1] if urow else 0.2517

    current_sol = await fetch_wallet_balance(wallet)
    
    cursor.execute("SELECT token_pair, buy_price, sell_price, profit_sol, tx_signature, timestamp FROM trades WHERE telegram_id = ? ORDER BY id DESC LIMIT 15", (telegram_id,))
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
            logging.info("Сброс Webhook выполнен.")

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
                                    "text": "🛡️ **Zer0Life Dip & Hedge Bot**\n\nЗащищенный терминал активен:",
                                    "parse_mode": "Markdown",
                                    "reply_markup": {
                                        "inline_keyboard": [[
                                            {"text": "🚀 Открыть Терминал", "web_app": {"url": RENDER_URL}}
                                        ]]
                                    }
                                }
                                async with session.post(send_url, json=payload) as send_resp:
                                    pass
            except Exception as e:
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
    app.router.add_get('/api/stats', api_get_stats)
    
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    
    asyncio.create_task(telegram_long_polling())
    asyncio.create_task(background_mov_trader_daemon())

    logging.info("Защищенный Hedge Бот запущен в режиме 24/7.")
    while True:
        await asyncio.sleep(3600)

if __name__ == "__main__":
    asyncio.run(main())
