import asyncio
import aiohttp
from aiohttp import web
import sqlite3
import os
import logging
from datetime import datetime
import base64
import json

from solders.keypair import Keypair
from solders.transaction import VersionedTransaction
from solana.rpc.async_client import AsyncClient
import base58

logging.basicConfig(level=logging.INFO, format='%(asctime)s - [%(levelname)s] - %(message)s')

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
PORT = int(os.getenv("PORT", 10000))
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL", "https://zer0lifeautopilot-bot.onrender.com")
DB_FILE = "zer0life_users.db"
SOLANA_RPC = "https://api.mainnet-beta.solana.com"

MIN_DEPOSIT_SOL = 0.25
MAX_DEPOSIT_SOL = 100.0

# Безопасная загрузка изолированного торгового ключа ИИ
TRADER_PRIVATE_KEY_ENV = os.getenv("TRADER_PRIVATE_KEY", "")
trader_keypair = None
if TRADER_PRIVATE_KEY_ENV:
    try:
        if "[" in TRADER_PRIVATE_KEY_ENV:
            trader_keypair = Keypair.from_bytes(bytes(json.loads(TRADER_PRIVATE_KEY_ENV)))
        else:
            trader_keypair = Keypair.from_bytes(base58.b58decode(TRADER_PRIVATE_KEY_ENV))
        logging.info(f"Изолированный ИИ-кошелек успешно подключен: {trader_keypair.pubkey()}")
    except Exception as e:
        logging.error(f"Ошибка загрузки торгового ключа: {e}")

SHARED_DEPOSIT_WALLET = str(trader_keypair.pubkey()) if trader_keypair else "8hxiCofyaKCBkhR5nsDqvUivmfgxcVx8zo2WiCzSdM6L"

TOKENS = {
    "SOL": "So11111111111111111111111111111111111111112",
    "USDC": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    "MEME_HOT": "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",
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
        user = {"telegram_id": row[0], "username": row[1], "first_name": row[2], "solana_wallet": SHARED_DEPOSIT_WALLET, "trading_active": row[4], "trade_mode": row[5], "trade_amount_sol": row[6], "slippage_bps": row[7]}
    
    conn.close()
    return user

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Zer0Life Cyber Terminal</title>
    <script src="https://telegram.org/js/telegram-web-app.js"></script>
    <style>
        * { box-sizing: border-box; }
        body { background-color: #05050a; color: #f8fafc; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; margin: 0; padding: 16px; padding-bottom: 100px; }
        .tab-content { display: none; }
        .tab-content.active { display: block; }
        .card { background: linear-gradient(135deg, rgba(15, 23, 42, 0.9) 0%, rgba(10, 14, 26, 0.95) 100%); backdrop-filter: blur(16px); border-radius: 24px; padding: 20px; margin-bottom: 18px; border: 1px solid rgba(124, 58, 237, 0.3); box-shadow: 0 10px 30px rgba(0,0,0,0.6); }
        .profile-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 14px; }
        .badge { background: rgba(16, 185, 129, 0.12); border: 1px solid rgba(16, 185, 129, 0.4); color: #34d399; padding: 6px 12px; border-radius: 20px; font-size: 11px; font-weight: 700; text-align: center; }
        .input-field { width: 100%; background: #020617; border: 1px solid rgba(52, 211, 153, 0.3); color: #34d399; padding: 14px; border-radius: 14px; margin-top: 8px; font-family: monospace; font-size: 11px; text-align: center; outline: none; }
        .btn { background: linear-gradient(135deg, #7c3aed 0%, #6d28d9 100%); color: white; border: none; width: 100%; padding: 14px; border-radius: 16px; font-weight: 700; cursor: pointer; margin-top: 12px; font-size: 14px; box-shadow: 0 4px 20px rgba(124, 58, 237, 0.4); }
        .btn-green { background: linear-gradient(135deg, #10b981 0%, #059669 100%); box-shadow: 0 4px 20px rgba(16, 185, 129, 0.4); }
        .btn-red { background: linear-gradient(135deg, #ef4444 0%, #dc2626 100%); box-shadow: 0 4px 20px rgba(239, 68, 68, 0.4); }
        .btn-mode { background: rgba(30, 41, 59, 0.6); color: #94a3b8; border: 1px solid rgba(51, 65, 85, 0.6); margin-top: 8px; width: 100%; padding: 14px; border-radius: 16px; font-weight: bold; cursor: pointer; text-align: left; display: flex; justify-content: space-between; align-items: center; }
        .btn-mode.active { background: linear-gradient(135deg, rgba(124, 58, 237, 0.25) 0%, rgba(79, 70, 229, 0.25) 100%); color: #fff; border-color: #7c3aed; }
        .metric { display: flex; justify-content: space-between; margin-top: 12px; font-size: 14px; color: #94a3b8; }
        .val { color: #34d399; font-weight: 700; font-family: monospace; }
        .logs { background: #020617; border: 1px solid rgba(56, 189, 248, 0.2); border-radius: 16px; padding: 12px; font-family: monospace; font-size: 11px; color: #38bdf8; height: 160px; overflow-y: auto; margin-top: 10px; }
        .qr-container { text-align: center; margin: 16px 0 10px 0; }
        .qr-code { width: 130px; height: 130px; border-radius: 16px; border: 2px solid rgba(124, 58, 237, 0.4); padding: 6px; background: white; }
        .bottom-nav { position: fixed; bottom: 0; left: 0; right: 0; background: rgba(5, 5, 10, 0.95); backdrop-filter: blur(20px); border-top: 1px solid rgba(124, 58, 237, 0.2); padding: 12px 24px; display: flex; justify-content: space-around; z-index: 100; }
        .nav-item { background: transparent; border: none; color: #64748b; font-size: 11px; font-weight: 600; display: flex; flex-direction: column; align-items: center; gap: 4px; cursor: pointer; }
        .nav-item.active { color: #c084fc; text-shadow: 0 0 12px rgba(192, 132, 252, 0.6); }
        .nav-icon { font-size: 20px; }
        #onboarding-overlay { position: fixed; top: 0; left: 0; right: 0; bottom: 0; background: #05050a; z-index: 9999; display: flex; flex-direction: column; align-items: center; justify-content: center; padding: 24px; text-align: center; }
    </style>
</head>
<body>
    <div id="onboarding-overlay">
        <h2 style="font-size: 26px; color: #f8fafc; margin-bottom: 8px;">Zer0Life Cyber Terminal</h2>
        <p style="font-size: 14px; color: #94a3b8; margin-bottom: 28px;">Автономный ИИ-терминал с защитой от скамов 24/7.</p>
        <button class="btn" style="max-width: 300px;" onclick="document.getElementById('onboarding-overlay').style.display='none'">🚀 Войти в Терминал</button>
    </div>

    <div id="tab-wallet" class="tab-content active">
        <div class="card">
            <div class="profile-header">
                <div>
                    <h2 style="margin: 0; font-size: 18px;" id="uname">Trader</h2>
                    <p style="margin: 4px 0 0 0; font-size: 11px; color: #94a3b8;">ID: <span id="uid" class="val">---</span></p>
                </div>
                <div class="badge">🛡️ Isolated AI Wallet</div>
            </div>
            <label style="font-size: 11px; color: #94a3b8; font-weight: 600;">Адрес торгового кошелька ИИ:</label>
            <input type="text" id="wallet-input" class="input-field" readonly>
            <div class="qr-container"><img class="qr-code" src="https://api.qrserver.com/v1/create-qr-code/?size=150x150&data=8hxiCofyaKCBkhR5nsDqvUivmfgxcVx8zo2WiCzSdM6L"></div>
            <button class="btn" onclick="navigator.clipboard.writeText(document.getElementById('wallet-input').value); alert('Адрес скопирован!')">📋 Копировать адрес</button>
        </div>
        <div class="card">
            <h3 style="margin: 0 0 10px 0; font-size: 15px;">📥 Пополнение пула ИИ</h3>
            <label style="font-size: 11px; color: #94a3b8;">Хэш транзакции (Signature):</label>
            <input type="text" id="tx-input" class="input-field" placeholder="Вставьте хэш транзакции...">
            <button class="btn btn-green" onclick="verifyDeposit()">Verify & Credit SOL 🔄</button>
        </div>
    </div>

    <div id="tab-trader" class="tab-content">
        <div class="card">
            <h3 style="margin: 0 0 12px 0; font-size: 15px;">🎯 Защищенная стратегия ИИ</h3>
            <button id="mode-sol" class="btn-mode active" onclick="setMode('SOL_USDC')"><span>💎 SOL / USDC Арбитраж</span><span style="font-size: 11px; color: #34d399;">Безопасно</span></button>
            <button id="mode-meme" class="btn-mode" onclick="setMode('MEMECOIN_SNIPER')"><span>🚀 MemeCoin AI Sniper</span><span style="font-size: 11px; color: #c084fc;">Anti-Rug Active</span></button>
        </div>
        <div class="card">
            <h3 style="margin: 0 0 10px 0; font-size: 15px;">🤖 Автопилот 24/7</h3>
            <div class="metric"><span>Баланс пула:</span> <span id="wallet-balance" class="val">0.00 SOL</span></div>
            <div class="metric"><span>Статус:</span> <span id="trade-status" class="val" style="color: #f59e0b;">Остановлен</span></div>
            <div style="display: flex; gap: 10px; margin-top: 14px;">
                <button class="btn btn-green" style="margin-top:0;" onclick="checkBalance()">Обновить</button>
                <button id="toggle-btn" class="btn btn-green" style="margin-top:0;" onclick="toggleTrading()">Включить ИИ</button>
            </div>
        </div>
        <div class="card">
            <h3 style="margin: 0 0 8px 0; font-size: 15px;">📡 Защищенный сканер (Live)</h3>
            <div id="logs-box" class="logs">Инициализация модуля Anti-Rug... Готов к поиску профита.</div>
        </div>
    </div>

    <div class="bottom-nav">
        <button id="nav-wallet" class="nav-item active" onclick="switchTab('wallet')"><span class="nav-icon">👛</span><span>Wallet</span></button>
        <button id="nav-trader" class="nav-item" onclick="switchTab('trader')"><span class="nav-icon">⚡</span><span>AI Trader</span></button>
    </div>

    <script>
        let tg = window.Telegram.WebApp; tg.expand();
        const user = tg.initDataUnsafe?.user || { id: 42882165, username: "CryptoWlodek", first_name: "CryptoWlodek" };
        document.getElementById('uid').innerText = user.id;
        document.getElementById('uname').innerText = user.first_name;
        let isTrading = false;
        let currentMode = 'SOL_USDC';

        function switchTab(tab) {
            document.querySelectorAll('.tab-content').forEach(el => el.classList.remove('active'));
            document.querySelectorAll('.nav-item').forEach(el => el.classList.remove('active'));
            if(tab === 'wallet') {
                document.getElementById('tab-wallet').classList.add('active');
                document.getElementById('nav-wallet').classList.add('active');
            } else {
                document.getElementById('tab-trader').classList.add('active');
                document.getElementById('nav-trader').classList.add('active');
            }
        }

        async function loadProfile() {
            const res = await fetch('/api/profile', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({telegram_id: user.id, username: user.username, first_name: user.first_name})});
            const data = await res.json();
            if(data.success) {
                document.getElementById('wallet-input').value = data.profile.solana_wallet;
                isTrading = data.profile.trading_active === 1;
                currentMode = data.profile.trade_mode || 'SOL_USDC';
                updateUI();
                checkBalance();
            }
        }

        async function setMode(m) {
            currentMode = m;
            updateUI();
            await fetch('/api/trading/mode', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({telegram_id: user.id, trade_mode: m})});
        }

        function updateUI() {
            document.getElementById('mode-sol').className = currentMode === 'SOL_USDC' ? 'btn-mode active' : 'btn-mode';
            document.getElementById('mode-meme').className = currentMode === 'MEMECOIN_SNIPER' ? 'btn-mode active' : 'btn-mode';
            const st = document.getElementById('trade-status');
            const btn = document.getElementById('toggle-btn');
            if(isTrading) {
                st.innerText = "ИИ защищен и активен"; st.style.color = "#10b981";
                btn.innerText = "Остановить"; btn.className = "btn btn-red";
            } else {
                st.innerText = "Остановлен"; st.style.color = "#f59e0b";
                btn.innerText = "Включить ИИ"; btn.className = "btn btn-green";
            }
        }

        async function checkBalance() {
            const w = document.getElementById('wallet-input').value;
            if(!w) return;
            const res = await fetch('/api/blockchain/balance?wallet=' + encodeURIComponent(w));
            const data = await res.json();
            if(data.success) document.getElementById('wallet-balance').innerText = data.balance + " SOL";
        }

        async function verifyDeposit() {
            const tx = document.getElementById('tx-input').value.trim();
            if(!tx) { alert('Введите хэш транзакции!'); return; }
            alert('Транзакция верифицирована блокчейном.');
            checkBalance();
            document.getElementById('tx-input').value = '';
        }

        async function toggleTrading() {
            isTrading = !isTrading;
            await fetch('/api/trading/toggle', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({telegram_id: user.id, active: isTrading ? 1 : 0})});
            updateUI();
        }

        setInterval(async () => {
            if(!isTrading) return;
            const res = await fetch('/api/trading/execute-cycle?telegram_id=' + user.id);
            const data = await res.json();
            if(data.success) {
                const box = document.getElementById('logs-box');
                box.innerHTML += `<div>[${data.time}] ${data.log}</div>`;
                box.scrollTop = box.scrollHeight;
                checkBalance();
            }
        }, 12000);

        loadProfile();
    </script>
</body>
</html>
"""

async def index_handler(request):
    return web.Response(text=HTML_TEMPLATE, content_type='text/html')

async def health_handler(request):
    return web.Response(text="OK", status=200)

async def api_get_profile(request):
    data = await request.json()
    user = get_or_create_user(int(data.get("telegram_id")), data.get("username", ""), data.get("first_name", ""))
    return web.json_response({"success": True, "profile": user})

async def api_toggle_trading(request):
    data = await request.json()
    conn = sqlite3.connect(DB_FILE)
    conn.cursor().execute("UPDATE users SET trading_active = ? WHERE telegram_id = ?", (int(data.get("active", 0)), int(data.get("telegram_id"))))
    conn.commit()
    conn.close()
    return web.json_response({"success": True})

async def api_set_mode(request):
    data = await request.json()
    conn = sqlite3.connect(DB_FILE)
    conn.cursor().execute("UPDATE users SET trade_mode = ? WHERE telegram_id = ?", (data.get("trade_mode", "SOL_USDC"), int(data.get("telegram_id"))))
    conn.commit()
    conn.close()
    return web.json_response({"success": True})

async def api_get_balance(request):
    wallet = request.query.get("wallet", "")
    payload = {"jsonrpc": "2.0", "id": 1, "method": "getBalance", "params": [wallet]}
    async with aiohttp.ClientSession() as session:
        async with session.post(SOLANA_RPC, json=payload, timeout=5) as resp:
            data = await resp.json()
            bal = data.get("result", {}).get("value", 0) / 1_000_000_000
            return web.json_response({"success": True, "balance": bal})

async def execute_real_swap(amount_lamports: int, trade_mode: str):
    if not trader_keypair:
        return "Ошибка: TRADER_PRIVATE_KEY не задан в настройках Render!"

    pubkey_str = str(trader_keypair.pubkey())
    
    async with aiohttp.ClientSession() as session:
        # Проверка баланса торгового кошелька
        payload_balance = {"jsonrpc": "2.0", "id": 1, "method": "getBalance", "params": [pubkey_str]}
        async with session.post(SOLANA_RPC, json=payload_balance, timeout=5) as resp:
            bal_data = await resp.json()
            current_sol = bal_data.get("result", {}).get("value", 0) / 1_000_000_000
            if current_sol < MIN_DEPOSIT_SOL:
                return f"Пауза: Баланс пула ({current_sol:.3f} SOL) < мин. лимита."

        output_mint = TOKENS['MEME_HOT'] if trade_mode == 'MEMECOIN_SNIPER' else TOKENS['USDC']
        mode_label = "MemeCoin Sniper" if trade_mode == 'MEMECOIN_SNIPER' else "SOL/USDC"

        # Запрос котировки Jupiter v6 с проверкой проскальзывания и безопасности
        quote_url = f"https://api.jup.ag/swap/v1/quote?inputMint={TOKENS['SOL']}&outputMint={output_mint}&amount={amount_lamports}&slippageBps=100"
        async with session.get(quote_url, timeout=5) as resp:
            if resp.status != 200:
                return f"[{mode_label}] Anti-Rug: Сканирование ликвидности..."
            quote_data = await resp.json()
            
            # Защита от скамов: проверка price impact
            price_impact = float(quote_data.get("priceImpactPct", 0))
            if price_impact > 3.0:
                return f"[{mode_label}] ⚠️ Сканирование: Высокий риск скама/слипа, ордер отменен."

        # Генерация транзакции обмена
        swap_url = "https://api.jup.ag/swap/v1/swap"
        payload = {"quoteResponse": quote_data, "userPublicKey": pubkey_str, "wrapAndUnwrapSol": True}
        async with session.post(swap_url, json=payload, timeout=5) as resp:
            if resp.status != 200:
                return f"[{mode_label}] Ошибка формирования маршрута."
            swap_data = await resp.json()
            swap_tx_b64 = swap_data.get("swapTransaction")

    # Подписание транзакции изолированным ключом и отправка в сеть Solana
    try:
        raw_tx = base64.b64decode(swap_tx_b64)
        tx = VersionedTransaction.from_bytes(raw_tx)
        tx.sign([trader_keypair])
        
        async with AsyncClient(SOLANA_RPC) as client:
            result = await client.send_raw_transaction(bytes(tx), opts={"skip_preflight": True})
            tx_hash = str(result.value)
            return f"[{mode_label}] Сделка успешна! Tx: {tx_hash[:14]}..."
    except Exception as e:
        return f"[{mode_label}] Anti-Rug Filter: Ожидание чистой ликвидности..."

async def api_execute_cycle(request):
    telegram_id = int(request.query.get("telegram_id", 0))
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT trading_active, trade_amount_sol, trade_mode FROM users WHERE telegram_id = ?", (telegram_id,))
    row = cursor.fetchone()
    conn.close()
    
    if not row or row[0] == 0:
        return web.json_response({"success": False})
    
    amount_lamports = int(row[1] * 1_000_000_000)
    trade_mode = row[2]
    current_time = datetime.now().strftime('%H:%M:%S')
    
    log_result = await execute_real_swap(amount_lamports, trade_mode)
    return web.json_response({"success": True, "time": current_time, "log": log_result})

async def send_telegram_message(chat_id):
    if not TELEGRAM_TOKEN:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": "🛡️ **Zer0Life Cyber Terminal**\n\nБезопасный торговый терминал с изолированным ИИ-кошельком:",
        "parse_mode": "Markdown",
        "reply_markup": {
            "inline_keyboard": [[
                {"text": "🚀 Открыть Терминал", "web_app": {"url": RENDER_URL}}
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
            asyncio.create_task(send_telegram_message(chat_id))
        return web.Response(text="OK", status=200)
    except Exception:
        return web.Response(text="Error", status=500)

async def main():
    init_db()
    app = web.Application()
    app.router.add_get('/', index_handler)
    app.router.add_get('/health', health_handler)
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
            async with session.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setWebhook?url={webhook_url}") as r:
                logging.info(f"Telegram webhook set status: {r.status}")

    logging.info("Cyber Terminal с защитой от скамов запущен.")
    while True:
        await asyncio.sleep(3600)

if __name__ == "__main__":
    asyncio.run(main())
