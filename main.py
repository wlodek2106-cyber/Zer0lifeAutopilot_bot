import asyncio
import aiohttp
from aiohttp import web
import logging
import os

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
PORT = int(os.getenv("PORT", 10000))
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL", "https://zer0lifeautopilot-bot.onrender.com")
DEX_LAUNCHPAD_URL = "https://jup.ag/swap/SOL-ZRL" # Или ваша ссылка на Raydium / Launchpad

HTML_CONTENT = """<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Zer0life DEX Terminal</title>
    <script src="https://telegram.org/js/telegram-web-app.js"></script>
    <style>
        body { background-color: #0b0f19; color: #f8fafc; font-family: sans-serif; margin: 0; padding: 16px; }
        .card { background: #131c2e; border-radius: 14px; padding: 16px; margin-bottom: 16px; border: 1px solid #1e293b; }
        .btn { background: #7c3aed; color: white; border: none; width: 100%; padding: 14px; border-radius: 10px; font-size: 14px; font-weight: bold; cursor: pointer; margin-top: 10px; text-decoration: none; display: block; text-align: center; box-sizing: border-box; }
        .btn-green { background: #10b981; }
        .coin { display: flex; justify-content: space-between; padding: 10px 0; border-bottom: 1px solid #1e293b; font-size: 14px; }
        .profile-header { display: flex; align-items: center; gap: 12px; margin-bottom: 10px; }
        .avatar { width: 48px; height: 48px; border-radius: 50%; background: #334155; object-fit: cover; }
    </style>
</head>
<body>
    <div class="card">
        <div class="profile-header">
            <img id="user-avatar" class="avatar" src="https://i.imgur.com/6VBx3io.png" alt="Avatar">
            <div>
                <h3 id="user-nickname" style="margin: 0; color: #38bdf8;">Трейдер</h3>
                <p style="margin: 4px 0 0 0; font-size: 11px; color: #94a3b8;">Telegram ID: <span id="user-id">---</span></p>
            </div>
        </div>
    </div>

    <div class="card">
        <h2>🛡 Zer0life DEX Autopilot</h2>
        <p>Статус: <span style="color: #10b981; font-weight: bold;">🟢 Сеть Solana активна</span></p>
        <p style="color: #94a3b8; font-size: 12px; margin-top: 8px;">Автономный торговый терминал экосистемы. Управление ликвидностью и пулами ZRL.</p>
        
        <a href="https://jup.ag/swap/SOL-ZRL" target="_blank" class="btn btn-green">🚀 Открыть DEX Launchpad (ZRL)</a>
    </div>

    <div class="card">
        <h3>📊 Мониторинг пулов ликвидности</h3>
        <div class="coin"><span>ZRL / SOL (Raydium)</span><span style="color: #10b981; font-weight: bold;">Ликвидность OK</span></div>
        <div class="coin"><span>SOL / USDC (Jupiter)</span><span style="color: #10b981; font-weight: bold;">Активен</span></div>
        <div class="coin"><span>AVAX / USDC (CEX)</span><span style="color: #38bdf8; font-weight: bold;">Мониторинг</span></div>
    </div>

    <script>
        let tg = window.Telegram.WebApp;
        tg.expand();

        const userId = tg.initDataUnsafe?.user?.id || "123456789";
        const firstName = tg.initDataUnsafe?.user?.first_name || "Trader";
        const photoUrl = tg.initDataUnsafe?.user?.photo_url || "https://i.imgur.com/6VBx3io.png";

        document.getElementById('user-id').innerText = userId;
        document.getElementById('user-nickname').innerText = firstName;
        document.getElementById('user-avatar').src = photoUrl;
    </script>
</body>
</html>
"""

async def index_handler(request):
    return web.Response(text=HTML_CONTENT, content_type='text/html')

async def send_telegram_message(chat_id, text):
    if not TELEGRAM_TOKEN or not chat_id:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": f"🛡 **Zer0life DEX Terminal**\n\n{text}",
        "parse_mode": "Markdown",
        "reply_markup": {
            "inline_keyboard": [
                [{"text": "🚀 Запустить DEX Терминал", "web_app": {"url": RENDER_URL}}],
                [{"text": "💎 Открыть DEX Launchpad (ZRL)", "url": DEX_LAUNCHPAD_URL}]
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
            await send_telegram_message(chat_id, "Экосистема инициализирована. Выберите нужный раздел в управлении ниже:")
        return web.Response(text="OK", status=200)
    except Exception:
        return web.Response(text="Error", status=500)

async def main():
    app = web.Application()
    app.router.add_get('/', index_handler)
    app.router.add_post('/webhook', webhook_handler)
    
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    
    if TELEGRAM_TOKEN:
        webhook_url = f"{RENDER_URL}/webhook"
        async with aiohttp.ClientSession() as session:
            await session.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setWebhook?url={webhook_url}")

    while True:
        await asyncio.sleep(300)

if __name__ == "__main__":
    asyncio.run(main())
