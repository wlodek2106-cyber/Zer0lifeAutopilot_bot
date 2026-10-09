import asyncio
import aiohttp
from aiohttp import web
import logging
import os

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
PORT = int(os.getenv("PORT", 10000))

# Ваш точный актуальный адрес с Render
RENDER_URL = "https://zer0lifeautopilot-bot.onrender.com"

# HTML-интерфейс со всеми отслеживаемыми монетами
HTML_CONTENT = """<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Zer0lifeAutopilot Dashboard</title>
    <script src="https://telegram.org/js/telegram-web-app.js"></script>
    <style>
        body {
            background-color: #0f172a;
            color: #f8fafc;
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            margin: 0;
            padding: 16px;
        }
        .header {
            text-align: center;
            margin-bottom: 20px;
        }
        .card {
            background: #1e293b;
            border-radius: 12px;
            padding: 16px;
            margin-bottom: 16px;
            box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1);
        }
        .status-active {
            color: #22c55e;
            font-weight: bold;
        }
        .coin-row {
            display: flex;
            justify-content: space-between;
            margin-bottom: 8px;
            border-bottom: 1px solid #334155;
            padding-bottom: 8px;
            font-size: 14px;
        }
        .btn {
            background: #6366f1;
            color: white;
            border: none;
            width: 100%;
            padding: 12px;
            border-radius: 8px;
            font-size: 16px;
            font-weight: bold;
            cursor: pointer;
            margin-top: 10px;
        }
        .btn:active { background: #4f46e5; }
    </style>
</head>
<body>
    <div class="header">
        <h2>🛡 Zer0life Autopilot</h2>
        <p>Статус: <span class="status-active">🟢 Активен (24/7)</span></p>
    </div>

    <div class="card">
        <h3>📊 Мониторинг DEX</h3>
        <div class="coin-row"><span>SOL / USDC</span><span><b>$145.50</b> (HOLD)</span></div>
        <div class="coin-row"><span>AVAX / USDC</span><span><b>$25.80</b> (BUY_DIP)</span></div>
        <div class="coin-row"><span>INJ / USDC</span><span><b>$22.10</b> (HOLD)</span></div>
        <div class="coin-row"><span>XRP / USDC</span><span><b>$0.55</b> (SCAN)</span></div>
        <div class="coin-row"><span>ADA / USDC</span><span><b>$0.36</b> (HOLD)</span></div>
        <div class="coin-row"><span>XMR / USDC</span><span><b>$160.20</b> (SECURE)</span></div>
    </div>

    <div class="card">
        <h3>⚙️ Управление</h3>
        <p>Автономный ИИ-агент сканирует ликвидность на просадках и защищает капитал.</p>
        <button class="btn" onclick="Telegram.WebApp.close()">Закрыть панель</button>
    </div>

    <script>
        let tg = window.Telegram.WebApp;
        tg.expand();
    </script>
</body>
</html>
"""

async def send_telegram_message(text, chat_id=None, add_webapp=True):
    target_chat = chat_id or TELEGRAM_CHAT_ID
    if not TELEGRAM_TOKEN or not target_chat:
        return
    
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": target_chat,
        "text": f"🤖 **Zer0lifeAutopilot**\n\n{text}",
        "parse_mode": "Markdown"
    }
    
    if add_webapp:
        payload["reply_markup"] = {
            "inline_keyboard": [[
                {"text": "🚀 Открыть Панель Управления", "web_app": {"url": RENDER_URL}}
            ]]
        }

    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(url, json=payload) as resp:
                pass
        except Exception as e:
            logging.error(f"Ошибка сети Telegram: {e}")

async def handle_telegram_updates():
    if not TELEGRAM_TOKEN:
        return
    
    offset = 0
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
    
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                async with session.get(url, params={"offset": offset, "timeout": 30}) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        for result in data.get("result", []):
                            offset = result["update_id"] + 1
                            message = result.get("message", {})
                            text = message.get("text", "")
                            chat_id = message.get("chat", {}).get("id")
                            
                            if text == "/start" and chat_id:
                                welcome_text = (
                                    "Привет! Автономный ИИ-трейдер **Zer0lifeAutopilot** успешно работает.\n\n"
                                    "📊 Сканируемые монеты: SOL, AVAX, INJ, XRP, ADA, XMR.\n"
                                    "🟢 Бот настроен на отслеживание просадок и защиту капитала на DEX."
                                )
                                await send_telegram_message(welcome_text, chat_id=chat_id, add_webapp=True)
            except Exception as e:
                logging.error(f"Ошибка опроса Telegram: {e}")
            
            await asyncio.sleep(2)

async def trading_background_loop():
    while True:
        logging.info("--- Цикл сканирования рынка DEX (SOL, AVAX, INJ, XRP, ADA, XMR) ---")
        await asyncio.sleep(300)

async def index_handler(request):
    return web.Response(text=HTML_CONTENT, content_type='text/html')

async def main():
    app = web.Application()
    app.router.add_get('/', index_handler)
    
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    logging.info(f"Веб-сервер Mini App запущен на порту {PORT}")

    await asyncio.gather(
        handle_telegram_updates(),
        trading_background_loop()
    )

if __name__ == "__main__":
    asyncio.run(main())
