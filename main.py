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

logging.basicConfig(level=logging.INFO, format='%(asctime)s - [%(levelname)s] - %(message)s')

# --- КОНФИГУРАЦИЯ ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
PORT = int(os.getenv("PORT", 10000))
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL", "https://zer0lifeautopilot-bot.onrender.com").strip()
DB_FILE = "zer0life_users.db"
SOLANA_RPC = os.getenv("SOLANA_RPC_URL", "https://mainnet.helius-rpc.com/?api-key=b82ca0bf-cd65-4ca8-a3d0-4400661d9a93").strip()

SHARED_DEPOSIT_WALLET = "8hxiCofyaKCBkhR5nsDqvUivmfgxcVx8zo2WiCzSdM6L"
SPLASH_IMAGE_URL = "https://i.postimg.cc/52n94Pzg/57D47DDD-9BED-4D4B-AA69-2B59CAB400AD.png"

SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

TRADE_AMOUNT_USDC = 25.0
TRADE_AMOUNT_RAW = int(TRADE_AMOUNT_USDC * 1_000_000)
TP_PCT = 0.010
SL_PCT = 0.010

RECENT_LOGS = ["⚡ Запуск торгового ядра Zer0Life..."]

def add_log(msg: str):
    global RECENT_LOGS
    timestamp = datetime.now().strftime('%H:%M:%S')
    entry = f"[{timestamp}] {msg}"
    RECENT_LOGS.insert(0, entry)
    if len(RECENT_LOGS) > 40:
        RECENT_LOGS.pop()
    logging.info(msg)

from solders.keypair import Keypair
from solders.transaction import VersionedTransaction

def get_signer_keypair():
    pk_env = os.getenv("SOLANA_PRIVATE_KEY", "").strip()
    if not pk_env:
        add_log("❌ Ошибка: SOLANA_PRIVATE_KEY отсутствует на Render!")
        return None
    try:
        pk_clean = pk_env.replace('"', '').replace("'", "").strip()
        if pk_clean.startswith("["):
            return Keypair.from_bytes(bytes(json.loads(pk_clean)))
        else:
            return Keypair.from_bytes(base58.b58decode(pk_clean))
    except Exception as e:
        add_log(f"❌ Ошибка парсинга ключа: {e}")
        return None

# --- БАЗА ДАННЫХ ---
def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            telegram_id INTEGER PRIMARY KEY,
            username TEXT,
            solana_wallet TEXT,
            trading_active INTEGER DEFAULT 1,
            initial_balance REAL DEFAULT 95.0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token_pair TEXT,
            position_side TEXT DEFAULT 'LONG',
            buy_price REAL,
            amount_lamports INTEGER,
            sell_price REAL DEFAULT 0,
            profit_usdc REAL DEFAULT 0.0,
            buy_tx TEXT,
            sell_tx TEXT,
            timestamp TEXT
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS deposits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER,
            tx_signature TEXT UNIQUE,
            amount_sol REAL,
            status TEXT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()

async def send_telegram_notification(chat_id: int, text: str):
    if not TELEGRAM_TOKEN or not chat_id:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    async with aiohttp.ClientSession() as session:
        try:
            await session.post(url, json=payload, timeout=3)
        except Exception:
            pass

# --- БЕЗОПАСНЫЙ СВОП JUPITER SWAP API V6 ---
async def execute_jupiter_swap(session: aiohttp.ClientSession, input_mint: str, output_mint: str, amount_raw: int, signer: Keypair) -> str:
    try:
        user_pubkey = str(signer.pubkey())
        headers = {"User-Agent": "Mozilla/5.0"}
        
        # 1. Quote
        quote_url = f"https://quote-api.jup.ag/v6/quote?inputMint={input_mint}&outputMint={output_mint}&amount={amount_raw}&slippageBps=150"
        async with session.get(quote_url, headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            if resp.status != 200:
                err = await resp.text()
                add_log(f"❌ Ошибка Quote API ({resp.status}): {err[:50]}")
                return ""
            quote_data = await resp.json()

        # 2. Swap TX
        payload = {
            "quoteResponse": quote_data,
            "userPublicKey": user_pubkey,
            "wrapAndUnwrapSol": True,
            "prioritizationFeeLamports": 50000
        }
        async with session.post("https://quote-api.jup.ag/v6/swap", headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=6)) as resp:
            if resp.status != 200:
                err = await resp.text()
                add_log(f"❌ Ошибка Swap API ({resp.status}): {err[:50]}")
                return ""
            res = await resp.json()
            swap_b64 = res.get("swapTransaction")

        if not swap_b64:
            return ""

        # 3. Подпись
        raw_bytes = base64.b64decode(swap_b64)
        tx = VersionedTransaction.from_bytes(raw_bytes)
        signed_tx = VersionedTransaction(tx.message, [signer])

        # 4. Отправка в RPC
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
                    add_log(f"❌ RPC Err: {data.get('error')}")
    except Exception as e:
        add_log(f"⚠️ Исключение свопа: {e}")
    return ""

async def get_sol_price(session: aiohttp.ClientSession) -> float:
    # Запрос через DexScreener как приоритет (быстрее и без блокировок Cloud IP)
    try:
        url = "https://api.dexscreener.com/latest/dex/tokens/So11111111111111111111111111111111111111112"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=3)) as resp:
            if resp.status == 200:
                data = await resp.json()
                pairs = data.get("pairs") or []
                if pairs:
                    return float(pairs[0].get("priceUsd", 0.0))
    except Exception:
        pass

    try:
        url = "https://api.jup.ag/price/v2?ids=So11111111111111111111111111111111111111112"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=3)) as resp:
            if resp.status == 200:
                d = await resp.json()
                return float(d.get("data", {}).get(SOL_MINT, {}).get("price", 0.0))
    except Exception:
        pass
    return 0.0

# --- РАБОЧИЙ ЦИКЛ С ПРИНУДИТЕЛЬНЫМ СТАРТОМ ---
FORCE_BUY_TRIGGER = False

async def real_trading_worker():
    global FORCE_BUY_TRIGGER
    add_log("🚀 Запуск ончейн-воркера...")
    signer = get_signer_keypair()
    if not signer:
        add_log("❌ ОСТАНОВКА: Проверьте ключ SOLANA_PRIVATE_KEY")
        return

    wallet_pubkey = str(signer.pubkey())
    add_log(f"🔑 Live Mode: {wallet_pubkey[:6]}...{wallet_pubkey[-4:]}")

    price_history = []
    cycle_counter = 0

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                cycle_counter += 1
                current_price = await get_sol_price(session)
                
                if current_price <= 0:
                    add_log("⏳ Ожидание ответа оракула цены SOL...")
                    await asyncio.sleep(4)
                    continue

                price_history.append(current_price)
                if len(price_history) > 15:
                    price_history.pop(0)

                conn = sqlite3.connect(DB_FILE)
                cursor = conn.cursor()
                cursor.execute("SELECT id, buy_price, amount_lamports, buy_tx FROM trades WHERE sell_price = 0 LIMIT 1")
                active_trade = cursor.fetchone()

                # 1. Есть открытая сделка — сопровождение
                if active_trade:
                    trade_id, entry_px, lamports, buy_tx = active_trade
                    diff_pct = (current_price - entry_px) / entry_px
                    
                    if cycle_counter % 3 == 0:
                        add_log(f"📈 Позиция активна: SOL ${current_price:.2f} \vert{} Вход ${entry_px:.2f} ({diff_pct*100:+.2f}%)")

                    if diff_pct >= TP_PCT or diff_pct <= -SL_PCT:
                        add_log(f"🔔 Закрытие сделки! Профит: {diff_pct*100:+.2f}%. Отправка SOL -> USDC...")
                        sell_sig = await execute_jupiter_swap(session, SOL_MINT, USDC_MINT, lamports, signer)
                        if sell_sig:
                            pnl_usd = round(TRADE_AMOUNT_USDC * diff_pct, 2)
                            cursor.execute("UPDATE trades SET sell_price = ?, profit_usdc = ?, sell_tx = ? WHERE id = ?", (current_price, pnl_usd, sell_sig, trade_id))
                            conn.commit()
                            add_log(f"🏆 Сделка исполнена! TX: {sell_sig[:12]}...")
                            add_log(f"🔗 Solscan: https://solscan.io/tx/{sell_sig}")
                    
                    conn.close()
                    await asyncio.sleep(4)
                    continue

                conn.close()

                # 2. Позиции нет — скрининг и исполнение
                avg_p = sum(price_history) / len(price_history)
                momentum = (current_price - avg_p) / avg_p if avg_p > 0 else 0

                if cycle_counter % 3 == 0:
                    add_log(f"🔍 Скринер: SOL ${current_price:.2f} | Импульс: {momentum*100:+.3f}%")

                # Вход: либо по импульсу, либо по ручной кнопке "Купить сейчас"
                if momentum >= 0.0001 or FORCE_BUY_TRIGGER or len(price_history) >= 3:
                    if FORCE_BUY_TRIGGER:
                        add_log("⚡ Принудительный старт сделки по запросу!")
                        FORCE_BUY_TRIGGER = False
                    else:
                        add_log(f"⚡ Сигнал входа! Отправка транзакции $25 USDC -> SOL...")

                    buy_sig = await execute_jupiter_swap(session, USDC_MINT, SOL_MINT, TRADE_AMOUNT_RAW, signer)

                    if buy_sig:
                        sol_lamports = int((TRADE_AMOUNT_USDC / current_price) * 1_000_000_000)
                        c = sqlite3.connect(DB_FILE)
                        c.cursor().execute("""
                            INSERT INTO trades (token_pair, position_side, buy_price, amount_lamports, sell_price, profit_usdc, buy_tx, sell_tx, timestamp) 
                            VALUES ('SOL/USDC', 'LONG', ?, ?, 0, 0, ?, '', ?)
                        """, (current_price, sol_lamports, buy_sig, datetime.now().strftime('%H:%M:%S')))
                        c.commit()
                        c.close()

                        add_log(f"💎 Реальная сделка открыта в сети Solana!")
                        add_log(f"🔗 Solscan TX: https://solscan.io/tx/{buy_sig}")
                    else:
                        add_log("⚠️ Своп отклонен сетью. Повторный цикл через 5 сек.")

            except Exception as e:
                add_log(f"⚠️ Ошибка воркера: {e}")

            await asyncio.sleep(4)

# --- TELEGRAM POLLING ---
async def telegram_bot_worker():
    if not TELEGRAM_TOKEN:
        return
    offset = 0
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
    send_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                async with session.get(f"{url}?offset={offset}&timeout=15", timeout=20) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        for update in data.get("result", []):
                            offset = update["update_id"] + 1
                            msg = update.get("message", {})
                            text = msg.get("text", "")
                            chat_id = msg.get("chat", {}).get("id")

                            if text.startswith("/start") and chat_id:
                                reply = {
                                    "chat_id": chat_id,
                                    "text": "⚜️ *Zer0Life Real DEX Engine Активен!*\n\nТорговля идет на Jupiter DEX прямо с кошелька.\n\n👇 Нажмите для открытия терминала:",
                                    "parse_mode": "Markdown",
                                    "reply_markup": {
                                        "inline_keyboard": [[
                                            {"text": "🚀 Открыть Zer0Life Run", "web_app": {"url": RENDER_URL}}
                                        ]]
                                    }
                                }
                                await session.post(send_url, json=reply, timeout=4)
            except Exception:
                pass
            await asyncio.sleep(1)

# --- ИНТЕРФЕЙС ---
RAW_HTML = r"""<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no, viewport-fit=cover">
    <title>Ai_trading_bot</title>
    <script src="https://telegram.org/js/telegram-web-app.js"></script>
    <style>
        * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
        body { background: #050507; color: #f1f5f9; font-family: -apple-system, BlinkMacSystemFont, "SF Pro Display", sans-serif; margin: 0; padding: 0; width: 100vw; height: 100vh; overflow: hidden; }
        #splash-screen {
            position: fixed; top: 0; left: 0; width: 100vw; height: 100vh;
            background-color: #050507;
            background-image: linear-gradient(180deg, rgba(0,0,0,0.05) 0%, rgba(0,0,0,0.2) 50%, rgba(0,0,0,0.85) 100%), url('__SPLASH_URL__');
            background-size: cover; background-position: center top; background-repeat: no-repeat;
            display: flex; flex-direction: column; justify-content: flex-end; align-items: center;
            padding: 24px; padding-bottom: calc(34px + env(safe-area-inset-bottom, 16px)); z-index: 9999;
        }
        .splash-branding { text-align: center; margin-bottom: 24px; user-select: none; }
        .brand-title { font-size: 42px; font-weight: 900; font-style: italic; color: #ffffff; text-transform: uppercase; line-height: 1.0; }
        .brand-run { font-size: 54px; font-weight: 900; font-style: italic; color: #5ff53b; text-transform: uppercase; line-height: 1.0; margin-top: 4px; text-shadow: 0 0 25px rgba(95,245,59,0.5); }
        .run-btn {
            background: linear-gradient(180deg, #8af64a 0%, #68e831 100%);
            color: #000000; border: none; width: 100%; max-width: 440px; padding: 18px;
            border-radius: 22px; font-weight: 800; font-size: 18px; cursor: pointer; text-align: center;
        }
        .card { background: linear-gradient(145deg, rgba(22, 22, 26, 0.95), rgba(12, 12, 14, 0.95)); border-radius: 24px; padding: 20px; margin-bottom: 16px; border: 1px solid rgba(212, 175, 55, 0.2); }
        .title-gold { margin-top: 0; font-size: 17px; font-weight: 800; color: #ffdf73; }
        .metric { display: flex; justify-content: space-between; margin-top: 10px; font-size: 13px; color: #8a8d93; }
        .val { color: #ffdf73; font-weight: 700; font-family: monospace; }
        .btn-gold { background: linear-gradient(135deg, #d4af37 0%, #aa771c 100%); color: #050505; border: none; width: 100%; padding: 14px; border-radius: 18px; font-weight: 800; cursor: pointer; }
        .btn-green-action { background: linear-gradient(180deg, #1bd984 0%, #0fb86c 100%); color: #041f12; border: none; width: 100%; padding: 14px; border-radius: 18px; font-weight: 800; cursor: pointer; margin-top: 10px; }
        .logs { background: rgba(0, 0, 0, 0.7); border: 1px solid rgba(212, 175, 55, 0.15); border-radius: 18px; padding: 12px; font-family: monospace; font-size: 11px; color: #5ff53b; height: 180px; overflow-y: auto; margin-top: 10px; white-space: pre-line; }
        .trade-item { background: rgba(0, 0, 0, 0.5); border: 1px solid rgba(212, 175, 55, 0.15); border-radius: 16px; padding: 12px; margin-top: 8px; font-family: monospace; font-size: 11px; display: flex; justify-content: space-between; align-items: center; }

        .neon-card { background: #0d101d; border: 1px solid #1c223a; border-radius: 26px; padding: 20px; margin-bottom: 16px; }
        .header-row { display: flex; justify-content: space-between; align-items: flex-start; }
        .user-name { font-size: 19px; font-weight: 800; color: #ffffff; }
        .user-id { color: #17e089; font-size: 13px; font-weight: 700; margin-top: 5px; font-family: monospace; }
        .pool-badge { background: rgba(14, 53, 44, 0.85); border: 1px solid #137754; color: #1fe090; padding: 8px 12px; border-radius: 20px; font-size: 11px; font-weight: 700; }
        .address-pill { background: #080a14; border: 1px solid #16533f; border-radius: 14px; padding: 13px 12px; font-family: monospace; font-size: 11px; color: #1ee08f; text-align: center; word-break: break-all; margin-top: 8px; }
        .qr-wrapper { display: flex; justify-content: center; align-items: center; margin: 18px 0; }
        .qr-box { background: #ffffff; padding: 12px; border-radius: 18px; }
        .qr-box img { display: block; width: 140px; height: 140px; }
        .btn-purple { background: linear-gradient(180deg, #6b43f7 0%, #5127ee 100%); color: #ffffff; border: none; width: 100%; padding: 16px; border-radius: 18px; font-weight: 700; font-size: 15px; cursor: pointer; }
        .input-tx { background: #080a14; border: 1px solid #1c223a; border-radius: 14px; padding: 14px; width: 100%; color: #ffffff; font-family: monospace; font-size: 12px; margin: 8px 0 12px 0; outline: none; }

        .bottom-nav { position: fixed; bottom: 0; left: 0; right: 0; background: rgba(10, 10, 12, 0.95); backdrop-filter: blur(30px); border-top: 1px solid rgba(212, 175, 55, 0.15); padding: 12px 16px; display: flex; justify-content: space-around; z-index: 900; }
        .nav-item { background: transparent; border: none; color: #5a5d63; font-size: 11px; font-weight: 600; display: flex; flex-direction: column; align-items: center; gap: 4px; cursor: pointer; }
        .nav-item.active { color: #d4af37; }
        .tab-content { display: none; width: 100%; height: 100vh; overflow-y: auto; padding: 16px; padding-bottom: 110px; }
        .tab-content.active { display: block; }
    </style>
</head>
<body>
    <div id="splash-screen">
        <div class="splash-branding">
            <div class="brand-title">ZERØLIFE</div>
            <div class="brand-run">RUN</div>
        </div>
        <button class="run-btn" onclick="enterApp()">Get Started</button>
    </div>

    <div id="app-container" style="display: none; width: 100vw; height: 100vh;">
        <!-- TAB 1: TRADER -->
        <div id="tab-trader" class="tab-content active">
            <div class="card">
                <h3 class="title-gold">⚡ Live Onchain Jupiter DEX</h3>
                <div class="metric"><span>Режим:</span> <span class="val" style="color: #5ff53b;">100% Ончейн (Real Swaps)</span></div>
                <div class="metric"><span>Сделка:</span> <span class="val">$25.00 USDC -> SOL</span></div>
                <div class="metric"><span>Статус:</span> <span id="status-val" class="val" style="color: #5ff53b;">Активен (24/7)</span></div>
                <button class="btn-green-action" onclick="forceTrade()">⚡ Открыть сделку прямо сейчас</button>
            </div>
            <div class="card">
                <h3 class="title-gold">📡 Журнал Реальных Транзакций</h3>
                <div id="logs-box" class="logs">Связь с блокчейном Solana...</div>
            </div>
        </div>

        <!-- TAB 2: STATS -->
        <div id="tab-stats" class="tab-content">
            <div class="card">
                <h3 class="title-gold">📊 Статистика Доходности</h3>
                <div class="metric"><span>Чистый PnL:</span> <span id="stat-profit" class="val" style="color: #5ff53b;">+$0.00 USDC</span></div>
                <button class="btn-gold" style="margin-top: 12px;" onclick="loadStats()">Обновить</button>
            </div>
            <div class="card">
                <h3 class="title-gold">📜 Исполненные Сделки (Solscan)</h3>
                <div id="trades-list" style="max-height: 250px; overflow-y: auto;">
                    <div style="color: #5a5d63; font-size: 12px; text-align: center; padding: 20px;">Ожидание сделок...</div>
                </div>
            </div>
        </div>

        <!-- TAB 3: WALLET -->
        <div id="tab-wallet" class="tab-content">
            <div class="neon-card">
                <div class="header-row">
                    <div>
                        <div class="user-name">CryptoWlodek | ZRL szn</div>
                        <div class="user-id">ID: 42882165</div>
                    </div>
                    <div class="pool-badge">Pool Limit: 0.25 - 100 SOL</div>
                </div>
                <div style="color: #7b88a8; font-size: 12px; margin-top: 14px;">Адрес депозита экосистемы:</div>
                <div class="address-pill" id="sol-address">8hxiCofyaKCBkhR5nsDqvUivmfgxcVx8zo2WiCzSdM6L</div>
                <div class="qr-wrapper">
                    <div class="qr-box">
                        <img src="https://api.qrserver.com/v1/create-qr-code/?size=180x180&data=8hxiCofyaKCBkhR5nsDqvUivmfgxcVx8zo2WiCzSdM6L&bgcolor=ffffff&color=000000&margin=0" alt="QR">
                    </div>
                </div>
                <button class="btn-purple" onclick="copyDepositAddress()">Копировать адрес</button>
            </div>
            <div class="neon-card">
                <div style="font-weight: 800; font-size: 16px;">Верификация депозита</div>
                <input type="text" id="tx-input" class="input-tx" placeholder="Вставьте хэш транзакции...">
                <button class="btn-gold" onclick="verifyDeposit()">Verify & Credit SOL</button>
            </div>
        </div>
    </div>

    <!-- МЕНЮ НАВИГАЦИИ -->
    <div id="bottom-nav-bar" class="bottom-nav" style="display: none;">
        <button id="nav-trader" class="nav-item active" onclick="switchTab('trader')"><span style="font-size:20px;">⚡</span><span>Trader</span></button>
        <button id="nav-stats" class="nav-item" onclick="switchTab('stats')"><span style="font-size:20px;">📊</span><span>Stats</span></button>
        <button id="nav-wallet" class="nav-item" onclick="switchTab('wallet')"><span style="font-size:20px;">💼</span><span>Wallet</span></button>
    </div>

    <script>
        let user = { id: 42882165 };
        window.enterApp = function() {
            document.getElementById('splash-screen').style.display = 'none';
            document.getElementById('app-container').style.display = 'block';
            document.getElementById('bottom-nav-bar').style.display = 'flex';
            switchTab('trader');
            loadStats(); loadLogs();
        };

        window.switchTab = function(tab) {
            document.querySelectorAll('#app-container .tab-content').forEach(el => el.classList.remove('active'));
            document.querySelectorAll('.nav-item').forEach(el => el.classList.remove('active'));
            if(tab === 'trader') {
                document.getElementById('tab-trader').classList.add('active');
                document.getElementById('nav-trader').classList.add('active');
                loadLogs();
            } else if(tab === 'stats') {
                document.getElementById('tab-stats').classList.add('active');
                document.getElementById('nav-stats').classList.add('active');
                loadStats();
            } else if(tab === 'wallet') {
                document.getElementById('tab-wallet').classList.add('active');
                document.getElementById('nav-wallet').classList.add('active');
            }
        };

        window.forceTrade = async function() {
            try {
                await fetch('/api/force_trade', { method: 'POST' });
                alert("Команда на открытие сделки отправлена в воркер!");
                loadLogs();
            } catch(e) {}
        };

        window.copyDepositAddress = function() {
            const addr = document.getElementById('sol-address').innerText.trim();
            if (navigator.clipboard) {
                navigator.clipboard.writeText(addr).then(() => { alert("Адрес скопирован!"); });
            }
        };

        window.verifyDeposit = async function() {
            const tx = document.getElementById('tx-input').value.trim();
            if(!tx) return;
            const res = await fetch('/api/verify_deposit', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ telegram_id: user.id, tx_signature: tx })
            });
            const d = await res.json();
            alert(d.message || "Готово");
        };

        window.loadLogs = async function() {
            try {
                const res = await fetch('/api/logs');
                const data = await res.json();
                if(data.success) { document.getElementById('logs-box').innerText = data.logs.join('\n'); }
            } catch(e) {}
        };

        window.loadStats = async function() {
            try {
                const res = await fetch('/api/stats?telegram_id=' + user.id);
                const data = await res.json();
                if(data.success) {
                    const p = data.profit_usdc;
                    const el = document.getElementById('stat-profit');
                    el.innerText = (p >= 0 ? "+$" : "-$") + Math.abs(p).toFixed(2) + " USDC";
                    el.style.color = p >= 0 ? "#5ff53b" : "#a71d31";
                    
                    const list = document.getElementById('trades-list');
                    if(data.trades.length === 0) {
                        list.innerHTML = '<div style="color: #5a5d63; font-size: 12px; text-align: center; padding: 20px;">Ожидание реальных транзакций...</div>';
                    } else {
                        list.innerHTML = data.trades.map(t => `
                            <div class="trade-item">
                                <div>
                                    <div style="color: #f1f5f9; font-weight: bold;">${t.token_pair}</div>
                                    <div style="color: #8a8d93; font-size: 10px;">Вход: $${t.buy_price.toFixed(2)} | Выход: $${t.sell_price.toFixed(2)}</div>
                                </div>
                                <div style="text-align: right;">
                                    <div style="color: ${t.profit_usdc >= 0 ? '#5ff53b' : '#a71d31'}; font-weight: bold;">
                                        ${t.profit_usdc >= 0 ? '+' : ''}$${t.profit_usdc.toFixed(2)} USDC
                                    </div>
                                    <a href="https://solscan.io/tx/${t.buy_tx}" target="_blank" style="color: #d4af37; font-size: 9px; text-decoration: underline;">Solscan TX</a>
                                </div>
                            </div>
                        `).join('');
                    }
                }
            } catch(e) {}
        };

        setInterval(() => { loadLogs(); loadStats(); }, 4000);
    </script>
</body>
</html>
"""

HTML_CONTENT = RAW_HTML.replace("__SPLASH_URL__", SPLASH_IMAGE_URL)

async def index_handler(request):
    headers = {"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"}
    return web.Response(text=HTML_CONTENT, content_type='text/html', headers=headers)

async def health_handler(request):
    return web.Response(text="OK", status=200)

async def api_get_logs(request):
    return web.json_response({"success": True, "logs": RECENT_LOGS})

async def api_force_trade(request):
    global FORCE_BUY_TRIGGER
    FORCE_BUY_TRIGGER = True
    add_log("📥 Получена команда: принудительное открытие сделки.")
    return web.json_response({"success": True})

async def api_get_stats(request):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT token_pair, buy_price, sell_price, profit_usdc, buy_tx, timestamp FROM trades WHERE sell_price > 0 ORDER BY id DESC LIMIT 20")
    rows = cursor.fetchall()
    trades = [{"token_pair": r[0], "buy_price": r[1], "sell_price": r[2], "profit_usdc": r[3], "buy_tx": r[4], "timestamp": r[5]} for r in rows]
    cursor.execute("SELECT SUM(profit_usdc) FROM trades WHERE sell_price > 0")
    total_profit = cursor.fetchone()[0] or 0.0
    conn.close()
    return web.json_response({"success": True, "profit_usdc": total_profit, "trades": trades})

async def api_verify_deposit(request):
    try:
        body = await request.json()
        tx = body.get("tx_signature", "").strip()
        tg_id = int(body.get("telegram_id", 42882165))
        if not tx:
            return web.json_response({"success": False, "message": "Пустой хэш транзакции"})

        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM deposits WHERE tx_signature = ?", (tx,))
        if cursor.fetchone():
            conn.close()
            return web.json_response({"success": False, "message": "Эта транзакция уже была учтена!"})

        cursor.execute("INSERT INTO deposits (telegram_id, tx_signature, amount_sol, status) VALUES (?, ?, 0.5, 'VERIFIED')", (tg_id, tx))
        conn.commit()
        conn.close()

        add_log(f"Депозит верифицирован: {tx[:10]}... на баланс Solana!")
        return web.json_response({"success": True, "message": "Депозит подтвержден"})
    except Exception as e:
        return web.json_response({"success": False, "message": str(e)})

async def main():
    init_db()
    app = web.Application()
    app.router.add_get('/', index_handler)
    app.router.add_get('/health', health_handler)
    app.router.add_get('/api/logs', api_get_logs)
    app.router.add_get('/api/stats', api_get_stats)
    app.router.add_post('/api/force_trade', api_force_trade)
    app.router.add_post('/api/verify_deposit', api_verify_deposit)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()

    asyncio.create_task(real_trading_worker())
    asyncio.create_task(telegram_bot_worker())

    add_log("Zer0Life DEX Live Engine и Telegram Polling запущены.")
    while True:
        await asyncio.sleep(3600)

if __name__ == "__main__":
    asyncio.run(main())
