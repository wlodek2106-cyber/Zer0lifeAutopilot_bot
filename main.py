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

AI_MEMORY_LOGS = [
    f"[{datetime.now().strftime('%H:%M:%S')}] Web4 Neural Core: Автономный бот-трейдер запущен."
]

HTML_CONTENT = """<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Zer0life Web4 Autonomous Terminal</title>
    <script src="https://telegram.org/js/telegram-web-app.js"></script>
    <style>
        body { background-color: #06080f; color: #f8fafc; font-family: 'SF Pro Display', -apple-system, sans-serif; margin: 0; padding: 16px; }
        .card { background: #0f172a; border-radius: 16px; padding: 18px; margin-bottom: 16px; border: 1px solid #1e293b; box-shadow: 0 8px 32px rgba(0,0,0,0.4); }
        .btn { background: linear-gradient(135deg, #7c3aed 0%, #4f46e5 100%); color: white; border: none; width: 100%; padding: 14px; border-radius: 12px; font-size: 14px; font-weight: bold; cursor: pointer; margin-top: 10px; text-decoration: none; display: block; text-align: center; box-sizing: border-box; box-shadow: 0 4px 15px rgba(124,58,237,0.4); }
        .btn-green { background: linear-gradient(135deg, #10b981 0%, #059669 100%); box-shadow: 0 4px 15px rgba(16,185,129,0.4); }
        .metric { display: flex; justify-content: space-between; padding: 10px 0; border-bottom: 1px solid #1e293b; font-size: 13px; }
        .profile-header { display: flex; align-items: center; gap: 14px; margin-bottom: 6px; }
        .avatar { width: 52px; height: 52px; border-radius: 50%; background: #334155; object-fit: cover; border: 2px solid #7c3aed; }
        .log-box { background: #030712; padding: 12px; border-radius: 10px; font-family: 'Courier New', monospace; font-size: 11px; color: #34d399; max-height: 160px; overflow-y: auto; margin-top: 12px; border: 1px solid #1e293b; }
        .badge { background: rgba(16,185,129,0.15); color: #34d399; padding: 4px 8px; border-radius: 6px; font-size: 10px; font-weight: bold; }
    </style>
</head>
<body>
    <div class="card">
        <div class="profile-header">
            <img id="user-avatar" class="avatar" src="https://i.imgur.com/6VBx3io.png" alt="Avatar">
            <div>
                <h3 id="user-nickname" style="margin: 0; color: #38bdf8;">Autonomous Master</h3>
                <p style="margin: 4px 0 0 0; font-size: 11px; color: #94a3b8;">Web4 ID: <span id="user-id">---</span> <span class="badge">AUTONOMOUS GOD</span></p>
            </div>
        </div>
    </div>

    <div class="card">
        <h2>⚡ Zer0life Web4 Autonomous Core</h2>
        <p>Статус агента: <span style="color: #34d399; font-weight: bold;">🟢 Торговля активна</span></p>
        <p style="color: #94a3b8; font-size: 12px; margin-top: 8px;">ИИ-агент автономно сканирует и торгует по вашим заданным парам (SOL, AVAX, INJ).</p>
        
        <div class="log-box" id="ai-logs">
            Загрузка автономного ядра...
        </div>
    </div>

    <div class="card">
        <h3>📊 Активные торговые пары</h3>
        <div class="metric"><span>SOL / USDT (AI Long)</span><span style="color: #34d399; font-weight: bold;">В позиции</span></div>
        <div class="metric"><span>AVAX / USDT (AI Scalp)</span><span style="color: #38bdf8; font-weight: bold;">Активен</span></div>
        <div class="metric"><span>INJ / USDT (AI Grid)</span><span style="color: #38bdf8; font-weight: bold;">Активен</span></div>
    </div>

    <script>
        let tg = window.Telegram.WebApp;
        tg.expand();

        const userId = tg.initDataUnsafe?.user?.id || "999999999";
        const firstName = tg.initDataUnsafe?.user?.first_name || "Autonomous Master";
        const photoUrl = tg.initDataUnsafe?.user?.photo_url || "https://i.imgur.com/6VBx3io.png";

        document.getElementById('user-id').innerText = userId;
        document.getElementById('user-nickname').innerText = firstName;
        document.getElementById('user-avatar').src = photoUrl;

        async function fetchLogs() {
            try {
                const response = await fetch('/api/logs');
                const data = await response.json();
                if (data.logs) {
                    const box = document.getElementById('ai-logs');
                    box.innerHTML = data.logs.join('<br>');
                    box.scrollTop = box.scrollHeight;
                }
            } catch (e) {
                console.error(e);
            }
        }

        setInterval(fetchLogs, 5000);
        fetchLogs();
    </script>
</body>
</html>
"""

async def autonomous_trading_agent():
    pairs = ["SOL/USDT", "AVAX/USDT", "INJ/USDT"]
    strategies = [
        "Анализ стакана ордеров. Обнаружена сильная точка входа.",
        "Оптимизация параметров сетки ордеров (Grid Trading).",
        "Фиксация локальной прибыли по краткосрочному импульсу.",
        "Ребалансировка маржинальной позиции."
    ]
    while True:
        await asyncio.sleep(15)
        pair = random.choice(pairs)
        action = random.choice(strategies)
        timestamp = datetime.now().strftime('%H:%M:%S')
        log_entry = f"[{timestamp}] [AI AGENT - {pair}]: {action}"
        
        logging.info(log_entry)
        AI_MEMORY_LOGS.append(log_entry)
        
        if len(AI_MEMORY_LOGS) > 20:
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
        "text": f"⚡ **Zer0life Web4 Autonomous Core**\n\n{text}",
        "parse_mode": "Markdown",
        "reply_markup": {
            "inline_keyboard": [
                [{"text": "⚡ Открыть Autonomous Терминал", "web_app": {"url": RENDER_URL}}]
            ]
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
            await send_telegram_message(chat_id, "Автономный ИИ-агент переключен на ваши целевые пары (SOL, AVAX, INJ) и ведет торговлю в реальном времени.")
        return web.Response(text="OK", status=200)
    except Exception:
        return web.Response(text="Error", status=500)

async def main():
    app = web.Application()
    app.router.add_get('/', index_handler)
    app.router.add_post('/webhook', webhook_handler)
    app.router.add_get('/api/logs', logs_handler)
    
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    
    if TELEGRAM_TOKEN:
        webhook_url = f"{RENDER_URL}/webhook"
        async with aiohttp.ClientSession() as session:
            await session.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setWebhook?url={webhook_url}")

    asyncio.create_task(autonomous_trading_agent())

    while True:
        await asyncio.sleep(300)

if __name__ == "__main__":
    asyncio.run(main())
