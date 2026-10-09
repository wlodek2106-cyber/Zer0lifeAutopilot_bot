import asyncio
import aiohttp
from aiohttp import web
import sqlite3
import os
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s - [%(levelname)s] - %(message)s')

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
PORT = int(os.getenv("PORT", 10000))
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL", "https://zer0lifeautopilot-bot.onrender.com")
DB_FILE = "zer0life_users.db"

def init_db():
    """Создаем чистую базу данных для реальных персональных аккаунтов"""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            telegram_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            solana_wallet TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()

def get_or_create_user(telegram_id: int, username: str, first_name: str):
    """Достаем или создаем персональный аккаунт пользователя по его ID"""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT telegram_id, username, first_name, solana_wallet FROM users WHERE telegram_id = ?", (telegram_id,))
    row = cursor.fetchone()
    
    if not row:
        cursor.execute("INSERT INTO users (telegram_id, username, first_name, solana_wallet) VALUES (?, ?, ?, ?)", 
                       (telegram_id, username, first_name, ""))
        conn.commit()
        user = {"telegram_id": telegram_id, "username": username, "first_name": first_name, "solana_wallet": ""}
    else:
        user = {"telegram_id": row[0], "username": row[1], "first_name": row[2], "solana_wallet": row[3]}
    
    conn.close()
    return user

# Чистый HTML интерфейс профиля
HTML_CONTENT = """<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Zer0Life Profile</title>
    <script src="https://telegram.org/js/telegram-web-app.js"></script>
    <style>
        body { background-color: #06080f; color: #f8fafc; font-family: -apple-system, sans-serif; margin: 0; padding: 16px; }
        .card { background: #0f172a; border-radius: 16px; padding: 18px; margin-bottom: 16px; border: 1px solid #1e293b; }
        .input-field { width: 100%; background: #030712; border: 1px solid #1e293b; color: white; padding: 12px; border-radius: 8px; box-sizing: border-box; margin-top: 8px; font-family: monospace; }
        .btn { background: #7c3aed; color: white; border: none; width: 100%; padding: 14px; border-radius: 12px; font-weight: bold; cursor: pointer; margin-top: 12px; }
    </style>
</head>
<body>
    <div class="card">
        <h2>👤 Персональный профиль</h2>
        <p>ID: <span id="uid">---</span></p>
        <p>Имя: <span id="uname">---</span></p>
        
        <label style="font-size: 12px; color: #94a3b8;">Ваш Solana кошелек:</label>
        <input type="text" id="wallet-input" class="input-field" placeholder="Введите адрес кошелька...">
        <button class="btn" onclick="saveWallet()">Сохранить кошелек</button>
    </div>

    <script>
        let tg = window.Telegram.WebApp;
        tg.expand();

        const user = tg.initDataUnsafe?.user || { id: 12345678, username: "test", first_name: "Test" };
        document.getElementById('uid').innerText = user.id;
        document.getElementById('uname').innerText = user.first_name;

        async function loadProfile() {
            const res = await fetch('/api/profile', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ telegram_id: user.id, username: user.username || "", first_name: user.first_name || "" })
            });
            const data = await res.json();
            if (data.success && data.profile.solana_wallet) {
                document.getElementById('wallet-input').value = data.profile.solana_wallet;
            }
        }

        async function saveWallet() {
            const wallet = document.getElementById('wallet-input').value;
            const res = await fetch('/api/wallet/update', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ telegram_id: user.id, solana_wallet: wallet })
            });
            const data = await res.json();
            if (data.success) {
                tg.showAlert("Кошелек успешно сохранен в базе данных!");
            }
        }

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

async def api_update_wallet(request):
    try:
        data = await request.json()
        telegram_id = int(data.get("telegram_id"))
        wallet = data.get("solana_wallet", "")
        
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET solana_wallet = ? WHERE telegram_id = ?", (wallet, telegram_id))
        conn.commit()
        conn.close()
        
        return web.json_response({"success": True})
    except Exception as e:
        return web.json_response({"success": False, "error": str(e)}, status=500)

async def send_telegram_message(chat_id):
    if not TELEGRAM_TOKEN:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": "⚡ **Zer0Life Профиль**\n\nОткройте ваш персональный кабинет:",
        "parse_mode": "Markdown",
        "reply_markup": {
            "inline_keyboard": [[
                {"text": "🚀 Открыть кабинет", "web_app": {"url": RENDER_URL}}
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
    app.router.add_post('/api/wallet/update', api_update_wallet)
    
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    
    if TELEGRAM_TOKEN:
        webhook_url = f"{RENDER_URL}/webhook"
        async with aiohttp.ClientSession() as session:
            await session.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setWebhook?url={webhook_url}")

    logging.info("Бот и сервер профилей запущены и работают.")
    while True:
        await asyncio.sleep(3600)

if __name__ == "__main__":
    asyncio.run(main())
