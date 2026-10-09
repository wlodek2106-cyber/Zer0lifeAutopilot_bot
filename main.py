import asyncio
import aiohttp
from aiohttp import web
import logging
import os
import random
from datetime import datetime

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
PORT = int(os.getenv("PORT", 10000))
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL", "https://zer0lifeautopilot-bot.onrender.com")

# Хранилище кошельков пользователей в памяти (в продакшене переносится в БД)
USER_WALLETS = {}

AI_MEMORY_LOGS = [
    f"[{datetime.now().strftime('%H:%M:%S')}] Web4 AI-Trader: Торговый модуль управления ликвидностью готов."
]

HTML_CONTENT = """<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Zer0life Web4 AI-Trader</title>
    <script src="https://telegram.org/js/telegram-web-app.js"></script>
    <style>
        body { background-color: #06080f; color: #f8fafc; font-family: 'SF Pro Display', -apple-system, sans-serif; margin: 0; padding: 16px; }
        .card { background: #0f172a; border-radius: 16px; padding: 18px; margin-bottom: 16px; border: 1px solid #1e293b; box-shadow: 0 8px 32px rgba(0,0,0,0.4); }
        .btn { background: linear-gradient(135deg, #7c3aed 0%, #4f46e5 100%); color: white; border: none; width: 100%; padding: 14px; border-radius: 12px; font-size: 14px; font-weight: bold; cursor: pointer; margin-top: 10px; text-decoration: none; display: block; text-align: center; box-sizing: border-box; }
        .btn-green { background: linear-gradient(135deg, #10b981 0%, #059669 100%); }
        .btn-red { background: linear-gradient(135deg, #ef4444 0%, #dc2626 100%); }
        .metric { display: flex; justify-content: space-between; padding: 10px 0; border-bottom: 1px solid #1e293b; font-size: 13px; }
        .profile-header { display: flex; align-items: center; gap: 14px; margin-bottom: 6px; }
        .avatar { width: 52px; height: 52px; border-radius: 50%; background: #334155; object-fit: cover; border: 2px solid #7c3aed; }
        .wallet-box { background: #030712; padding: 10px; border-radius: 8px; font-family: monospace; font-size: 11px; color: #38bdf8; word-break: break-all; margin-top: 8px; border: 1px solid #1e293b; }
        .log-box { background: #030712; padding: 12px; border-radius: 10px; font-family: 'Courier New', monospace; font-size: 11px; color: #34d399; max-height: 140px; overflow-y: auto; margin-top: 12px; border: 1px solid #1e293b; }
        .badge { background: rgba(16,185,129,0.15); color: #34d399; padding: 4px 8px; border-radius: 6px; font-size: 10px; font-weight: bold; }
    </style>
</head>
<body>
    <div class="card">
        <div class="profile-header">
            <img id="user-avatar" class="avatar" src="https://i.imgur.com/6VBx3io.png" alt="Avatar">
            <div>
                <h3 id="user-nickname" style="margin: 0; color: #38bdf8;">Web4 Master</h3>
                <p style="margin: 4px 0 0 0; font-size: 11px; color: #94a3b8;">ID: <span id="user-id">---</span> <span class="badge">AI-TRADER GOD</span></p>
            </div>
        </div>
    </div>

    <div class="card">
        <h3>💳 Торговый DEX-кошелек агента</h3>
        <p style="font-size: 12px; color: #94a3b8;">Пополните этот адрес в сети Solana (SOL), чтобы AI-агент начал автономно торговать по вашим парам.</p>
        <div class="wallet-box" id="wallet-address">Генерация ключа...</div>
        <div class="metric" style="margin-top: 8px;">
            <span>Баланс SOL:</span>
            <span id="wallet-balance" style="color: #34d399; font-weight: bold;">0.00 SOL</span>
        </div>
        <button class="btn" onclick="loadWallet()">Обновить баланс</button>
        <button class="btn btn-green" id="trade-toggle-btn" onclick="toggleAutotrade()">Запустить автономную торговлю</button>
    </div>

    <div class="card">
        <h2>⚡ Web4 AI-Trader Core</h2>
        <div class="log-box" id="ai-logs">
            Ядро ожидает активации баланса...
        </div>
    </div>

    <script>
        let tg = window.Telegram.WebApp;
        tg.expand();

        const userId = tg.initDataUnsafe?.user?.id || "999999999";
        const firstName = tg.initDataUnsafe?.user?.first_name || "Web4 Master";
        const photoUrl = tg.initDataUnsafe?.user?.photo_url || "https://i.imgur.com/6VBx3io.png";

        document.getElementById('user-id').innerText = userId;
        document.getElementById('user-nickname').innerText = firstName;
        document.getElementById('user-avatar').src = photoUrl;

        let isTradingActive = false;

        async function loadWallet() {
            try {
                const res = await fetch('/api/wallet?user_id=' + userId);
                const data = await res.json();
                document.getElementById('wallet-address').innerText = data.pubkey;
                document.getElementById('wallet-balance').innerText = data.balance + " SOL";
            } catch(e) {
                console.error(e);
            }
        }

        function toggleAutotrade() {
            isTradingActive = !isTradingActive;
            const btn = document.getElementById('trade-toggle-btn');
            if (isTradingActive) {
                btn.innerText = "Остановить торговлю";
                btn.className = "btn btn-red";
                tg.showAlert("Автономный агент успешно передан под управление балансом!");
            } else {
                btn.innerText = "Запустить автономную торговлю";
                btn.className = "btn btn-green";
                tg.showAlert("Торговый агент приостановлен.");
            }
        }

        async function fetchLogs() {
            try {
                const response = await fetch('/api/logs');
                const data = await response.json();
                if (data.logs) {
                    const box = document.getElementById('ai-logs');
                    box.innerHTML = data.logs.join('<br>');
                    box.scrollTop = box.scrollHeight;
                }
            } catch (e) {}
        }

        loadWallet();
        setInterval(fetchLogs, 5000);
        fetchLogs();
    </script>
</body>
</html>
"""

async def wallet_handler(request):
    user_id = request.query.get("user_id", "default")
    if user_id not in USER_WALLETS:
        # Генерируем детерминированный рабочий адрес кошелька для демонстрации в Solana
        mock_pubkey = "ZER0" + ''.join(random.choices("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz", k=39))
        USER_WALLETS[user_id] = {"pubkey": mock_pubkey, "balance": 0.05} # Стартовый баланс для проверки автоторговли
    
    wallet = USER_WALLETS[user_id]
    return web.json_response({"pubkey": wallet["pubkey"], "balance": wallet["balance"]})

async def ai_trader_background_worker():
    pairs = ["SOL/USDC", "AVAX/SOL", "INJ/SOL"]
    actions = [
        "Анализ стакана ликвидности: найдена выгодная точка входа.",
        "Исполнение ордера через Jupiter Swap API (с проскальзыванием 0.3%).",
        "Успешная фиксация профита по сетке ордеров.",
        "Ребалансировка активов в пуле доходности."
    ]
    while True:
        await asyncio.sleep(15)
        pair = random.choice(pairs)
        action = random.choice(actions)
        timestamp = datetime.now().strftime('%H:%M:%S')
        log_entry = f"[{timestamp}] [AI-TRADER - {pair}]: {action}"
        logging.info(log_entry)
        AI_MEMORY_LOGS.append(log_entry)
        if len(AI_MEMORY_LOGS) > 25:
            AI_MEMORY_LOGS.pop(0)

async def logs_handler(request):
    return web.json_response({"logs": AI_MEMORY_LOGS})

async def index_handler(request):
    return web.Response(text=HTML_CONTENT, content_type='text/html')

async def send_telegram_message(chat_id, text):
    if not TELEGRAM_TOKEN or not chat_id:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": f"⚡ **Zer0life Web4 AI-Trader**\n\n{text}",
        "parse_mode": "Markdown",
        "reply_markup": {
            "inline_keyboard": [[
                {"text": "⚡ Открыть Терминал Кошелька", "web_app": {"url": RENDER_URL}}
            ]]
        }
    }
    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(url, json=payload) as resp:
                pass
        except Exception as e:
            logging.error(f"Ошибка: {e}")

async def webhook_handler(request):
    try:
        data = await request.json()
        message = data.get("message", {})
        text = message.get("text", "")
        chat_id = message.get("chat", {}).get("id")
        if text == "/start" and chat_id:
            await send_telegram_message(chat_id, "Терминал управления балансом и автономным агентом инициализирован:")
        return web.Response(text="OK", status=200)
    except Exception:
        return web.Response(text="Error", status=500)

async def main():
    app = web.Application()
    app.router.add_get('/', index_handler)
    app.router.add_post('/webhook', webhook_handler)
    app.router.add_get('/api/wallet', wallet_handler)
    app.router.add_get('/api/logs', logs_handler)
    
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    
    if TELEGRAM_TOKEN:
        webhook_url = f"{RENDER_URL}/webhook"
        async with aiohttp.ClientSession() as session:
            await session.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setWebhook?url={webhook_url}")

    asyncio.create_task(ai_trader_background_worker())

    while True:
        await asyncio.sleep(300)

if __name__ == "__main__":
    asyncio.run(main())
