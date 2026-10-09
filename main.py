import asyncio
import aiohttp
from aiohttp import web
import sqlite3
import os
import logging
from datetime import datetime

# Импорты для реальной работы с криптокошельком и транзакциями Solana
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction
from solana.rpc.async_client import AsyncClient

logging.basicConfig(level=logging.INFO, format='%(asctime)s - [%(levelname)s] - %(message)s')

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
PORT = int(os.getenv("PORT", 10000))
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL", "https://zer0lifeautopilot-bot.onrender.com")
DB_FILE = "zer0life_users.db"
SOLANA_RPC = "https://api.mainnet-beta.solana.com"

# Загружаем приватный ключ торгового суб-кошелька из переменных окружения Render
TRADER_PRIVATE_KEY_ENV = os.getenv("TRADER_PRIVATE_KEY", "")
trader_keypair = None
if TRADER_PRIVATE_KEY_ENV:
    try:
        # Поддерживаем ввод как массив чисел или base58 строку
        if "[" in TRADER_PRIVATE_KEY_ENV:
            import json
            trader_keypair = Keypair.from_bytes(bytes(json.loads(TRADER_PRIVATE_KEY_ENV)))
        else:
            import base58
            trader_keypair = Keypair.from_bytes(base58.b58decode(TRADER_PRIVATE_KEY_ENV))
        logging.info(f"Торговый суб-кошелек успешно загружен: {trader_keypair.pubkey()}")
    except Exception as e:
        logging.error(f"Ошибка загрузки приватного ключа: {e}")

TOKENS = {
    "SOL": "So11111111111111111111111111111111111111112",
    "USDC": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
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
            trade_amount_sol REAL DEFAULT 0.05,
            slippage_bps INTEGER DEFAULT 50,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()

def get_or_create_user(telegram_id: int, username: str, first_name: str):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT telegram_id, username, first_name, solana_wallet, trading_active, trade_amount_sol, slippage_bps FROM users WHERE telegram_id = ?", (telegram_id,))
    row = cursor.fetchone()
    
    # Если суб-кошелек настроен в ENV, автоматически подставляем его адрес пользователю
    default_wallet = str(trader_keypair.pubkey()) if trader_keypair else ""
    
    if not row:
        cursor.execute("INSERT INTO users (telegram_id, username, first_name, solana_wallet, trading_active, trade_amount_sol, slippage_bps) VALUES (?, ?, ?, ?, ?, ?, ?)", 
                       (telegram_id, username, first_name, default_wallet, 0, 0.05, 50))
        conn.commit()
        user = {"telegram_id": telegram_id, "username": username, "first_name": first_name, "solana_wallet": default_wallet, "trading_active": 0, "trade_amount_sol": 0.05, "slippage_bps": 50}
    else:
        user = {"telegram_id": row[0], "username": row[1], "first_name": row[2], "solana_wallet": row[3] or default_wallet, "trading_active": row[4], "trade_amount_sol": row[5], "slippage_bps": row[6]}
    
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
        body { background-color: #06080f; color: #f8fafc; font-family: -apple-system, sans-serif; margin: 0; padding: 16px; }
        .card { background: #0f172a; border-radius: 16px; padding: 18px; margin-bottom: 16px; border: 1px solid #1e293b; }
        .profile-header { display: flex; align-items: center; gap: 14px; margin-bottom: 14px; }
        .avatar { width: 52px; height: 52px; border-radius: 50%; object-fit: cover; border: 2px solid #7c3aed; background: #1e293b; display: none; }
        .input-field { width: 100%; background: #030712; border: 1px solid #1e293b; color: white; padding: 12px; border-radius: 8px; box-sizing: border-box; margin-top: 8px; font-family: monospace; }
        .btn { background: #7c3aed; color: white; border: none; width: 100%; padding: 14px; border-radius: 12px; font-weight: bold; cursor: pointer; margin-top: 12px; }
        .btn-green { background: #10b981; }
        .btn-red { background: #ef4444; }
        .metric { display: flex; justify-content: space-between; margin-top: 10px; font-size: 13px; color: #94a3b8; }
        .val { color: #34d399; font-weight: bold; font-family: monospace; }
        .logs { background: #030712; border: 1px solid #1e293b; border-radius: 8px; padding: 10px; font-family: monospace; font-size: 11px; color: #38bdf8; height: 140px; overflow-y: auto; margin-top: 10px; }
    </style>
</head>
<body>
    <div class="card">
        <div class="profile-header">
            <img id="user-avatar" class="avatar" src="" alt="Avatar">
            <div>
                <h2 style="margin: 0; font-size: 17px;" id="uname">Загрузка...</h2>
                <p style="margin: 4px 0 0 0; font-size: 12px; color: #94a3b8;">ID: <span id="uid" class="val">---</span></p>
            </div>
        </div>
        
        <label style="font-size: 12px; color: #94a3b8;">Торговый суб-кошелек (Автопилот):</label>
        <input type="text" id="wallet-input" class="input-field" readonly>
    </div>

    <div class="card">
        <h3 style="margin-top: 0; font-size: 15px;">🤖 Автономный ИИ-Трейдер (Burner)</h3>
        <div class="metric"><span>Баланс SOL:</span> <span id="wallet-balance" class="val">0.00 SOL</span></div>
        <div class="metric"><span>Статус автопилота:</span> <span id="trade-status" class="val" style="color: #f59e0b;">Остановлен</span></div>
        <button class="btn btn-green" onclick="checkBalance()">Проверить баланс</button>
        <button id="toggle-btn" class="btn btn-green" onclick="toggleTrading()" style="margin-top: 8px;">Включить полный автопилот</button>
    </div>

    <div class="card">
        <h3 style="margin-top: 0; font-size: 15px;">📡 Исполнение сделок в сети (Live)</h3>
        <div id="logs-box" class="logs">Инициализация автономного агента... Готов к торгам.</div>
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
        }

        let isTrading = false;

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
                    updateTradingUI();
                }
            } catch (e) {}
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

        async function toggleTrading() {
            const wallet = document.getElementById('wallet-input').value.trim();
            if (!wallet) {
                tg.showAlert("Суб-кошелек не настроен в системе!");
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
                statusEl.innerText = "Исполнение ордеров 24/7";
                statusEl.style.color = "#10b981";
                btnEl.innerText = "Остановить автопилот";
                btnEl.className = "btn btn-red";
            } else {
                statusEl.innerText = "Остановлен";
                statusEl.style.color = "#f59e0b";
                btnEl.innerText = "Включить полный автопилот";
                btnEl.className = "btn btn-green";
            }
        }

        // Автономный опрос бэкенда для выполнения реальных свапов
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

async def execute_real_swap(amount_lamports: int, slippage: int):
    """Реальное формирование и отправка свапа через Jupiter Swap API и подписанное с помощью solders"""
    if not trader_keypair:
        return "Ошибка: Торговый суб-кошелек не настроен на сервере."
    
    pubkey_str = str(trader_keypair.pubkey())
    
    # 1. Получаем котировку и маршрут от Jupiter
    quote_url = f"https://api.jup.ag/swap/v1/quote?inputMint={TOKENS['SOL']}&outputMint={TOKENS['USDC']}&amount={amount_lamports}&slippageBps={slippage}"
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(quote_url, timeout=5) as resp:
                if resp.status != 200:
                    return "Jupiter API: Ошибка получения котировки."
                quote_data = await resp.json()
        except Exception:
            return "Сбой сети при запросе к Jupiter."

        # 2. Запрашиваем сериализованную транзакцию обмена
        swap_url = "https://api.jup.ag/swap/v1/swap"
        payload = {
            "quoteResponse": quote_data,
            "userPublicKey": pubkey_str,
            "wrapAndUnwrapSol": True
        }
        try:
            async with session.post(swap_url, json=payload, timeout=5) as resp:
                if resp.status != 200:
                    return f"Jupiter Swap Error: {await resp.text()}"
                swap_data = await resp.json()
                swap_transaction_b64 = swap_data.get("swapTransaction")
        except Exception:
            return "Сбой генерации транзакции свапа."

    # 3. Десериализуем, подписываем ключом суб-кошелька и отправляем в сеть Solana
    try:
        import base64
        raw_tx = base64.b64decode(swap_transaction_b64)
        tx = VersionedTransaction.from_bytes(raw_tx)
        
        # Подписываем транзакцию нашим торговым ключом
        tx.sign([trader_keypair])
        
        # Отправляем через официальный RPC асинхронный клиент
        async with AsyncClient(SOLANA_RPC) as client:
            result = await client.send_raw_transaction(bytes(tx))
            tx_sig = result.value
            return f"Сделка исполнена! Tx: {str(tx_sig)[:16]}..."
    except Exception as e:
        return f"Ошибка отправки в блокчейн: {str(e)[:40]}"

async def api_execute_cycle(request):
    telegram_id = int(request.query.get("telegram_id", 0))
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT trading_active, trade_amount_sol, slippage_bps FROM users WHERE telegram_id = ?", (telegram_id,))
    row = cursor.fetchone()
    conn.close()
    
    if not row or row[0] == 0:
        return web.json_response({"success": False})
    
    amount_sol, slippage = row[1], row[2]
    amount_lamports = int(amount_sol * 1_000_000_000)
    
    current_time = datetime.now().strftime('%H:%M:%S')
    
    # Запускаем реальный цикл исполнения сделки
    log_result = await execute_real_swap(amount_lamports, slippage)
    
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

    logging.info("Полноценный автономный торговый движок запущен.")
    while True:
        await asyncio.sleep(3600)

if __name__ == "__main__":
    asyncio.run(main())
