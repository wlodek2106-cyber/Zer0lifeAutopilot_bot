import asyncio
import aiohttp
from aiohttp import web
import logging
import os
import time
import hmac
import hashlib
import base64

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
                <h3 id="user-nickname" style="margin: 0; color: #38bdf8;">Трейдер</h3>
                <p style="margin: 4px 0 0 0; font-size: 11px; color: #94a3b8;">Telegram ID: <span id="user-id">---</span></p>
            </div>
        </div>
    </div>

    <div class="card">
        <h2>🛡 Zer0life AI Autopilot</h2>
        <p>Статус: <span style="color: #10b981; font-weight: bold;">🟢 Подключение к Bitget API</span></p>
    </div>
    
    <div class="card">
        <h3>🔑 Данные Bitget API</h3>
        <p style="color: #94a3b8; font-size: 12px;">Только права на торговлю (без вывода).</p>
        
        <label style="font-size: 12px; color: #94a3b8;">API Key</label>
        <input type="text" id="api-key" class="input-field" placeholder="Введите API Key">
        
        <label style="font-size: 12px; color: #94a3b8;">API Secret</label>
        <input type="password" id="api-secret" class="input-field" placeholder="Введите API Secret">

        <label style="font-size: 12px; color: #94a3b8;">Passphrase (Пароль API)</label>
        <input type="password" id="api-pass" class="input-field" placeholder="Введите Passphrase">
        
        <button class="btn" onclick="connectExchange()">Подключить и запросить баланс</button>

        <div id="account-stats" style="display:none; margin-top: 15px; border-top: 1px solid #1e293b; padding-top: 10px;">
            <div class="coin"><span>Баланс счета (USDT)</span><span class="balance-val" id="acc-balance">0.00 USDT</span></div>
            <div class="coin"><span>Статус соединения</span><span class="balance-val" style="color: #38bdf8;">Активно</span></div>
        </div>
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

        // Автозагрузка сохраненных ключей из памяти устройства при открытии
        window.addEventListener('load', () => {
            const savedKey = localStorage.getItem('bg_key');
            const savedSecret = localStorage.getItem('bg_secret');
            const savedPass = localStorage.getItem('bg_pass');

            if (savedKey && savedSecret && savedPass) {
                document.getElementById('api-key').value = savedKey;
                document.getElementById('api-secret').value = savedSecret;
                document.getElementById('api-pass').value = savedPass;
                
                // Автоматический запрос баланса при наличии ключей
                connectExchange(true);
            }
        });

        async function connectExchange(isAuto = false) {
            const apiKey = document.getElementById('api-key').value.trim();
            const apiSecret = document.getElementById('api-secret').value.trim();
            const apiPass = document.getElementById('api-pass').value.trim();

            if (!apiKey || !apiSecret || !apiPass) {
                if (!isAuto) alert("Заполните все поля (Key, Secret, Passphrase)!");
                return;
            }

            if (!isAuto) {
                tg.HapticFeedback.impactOccurred('medium');
            }
            
            const response = await fetch('/api/connect_bitget', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ user_id: userId, api_key: apiKey, api_secret: apiSecret, api_pass: apiPass })
            });

            const data = await response.json();
            if (data.status === "success") {
                // Сохраняем в localStorage телефона
                localStorage.setItem('bg_key', apiKey);
                localStorage.setItem('bg_secret', apiSecret);
                localStorage.setItem('bg_pass', apiPass);

                document.getElementById('account-stats').style.display = 'block';
                document.getElementById('acc-balance').innerText = data.balance + " USDT";
                
                if (!isAuto) {
                    tg.showAlert("Успешно! Получен реальный баланс с Bitget.");
                }
            } else {
                if (!isAuto) {
                    alert("Ошибка подключения к Bitget: " + (data.message || "Неверные данные"));
                }
            }
        }
    </script>
</body>
</html>
"""

def generate_bitget_signature(timestamp, method, request_path, body, secret_key):
    message = str(timestamp) + method.upper() + request_path + body
    mac = hmac.new(secret_key.encode('utf-8'), message.encode('utf-8'), digestmod=hashlib.sha256)
    return base64.b64encode(mac.digest()).decode('utf-8')

async def fetch_bitget_balance(api_key, api_secret, api_pass):
    try:
        method = "GET"
        endpoint = "/api/v2/spot/account/assets"
        url = f"https://api.bitget.com{endpoint}"
        timestamp = str(int(time.time() * 1000))
        
        signature = generate_bitget_signature(timestamp, method, endpoint, "", api_secret)
        
        headers = {
            "ACCESS-KEY": api_key,
            "ACCESS-SIGN": signature,
            "ACCESS-TIMESTAMP": timestamp,
            "ACCESS-PASSPHRASE": api_pass,
            "Content-Type": "application/json"
        }

        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=10) as resp:
                data = await resp.json()
                if data.get("code") == "00000":
                    assets = data.get("data", [])
                    total_usdt = 0.0
                    for asset in assets:
                        if asset.get("coin") == "USDT":
                            total_usdt = float(asset.get("available", 0)) + float(asset.get("frozen", 0))
                            break
                    return True, f"{total_usdt:.2f}"
                else:
                    return False, data.get("msg", "API Error")
    except Exception as e:
        return False, str(e)

async def connect_bitget_handler(request):
    try:
        data = await request.json()
        user_id = data.get("user_id")
        api_key = data.get("api_key")
        api_secret = data.get("api_secret")
        api_pass = data.get("api_pass")
        
        success, result = await fetch_bitget_balance(api_key, api_secret, api_pass)
        if success:
            USER_ACCOUNTS[user_id] = {
                "api_key": api_key,
                "api_secret": api_secret,
                "api_pass": api_pass,
                "balance": result
            }
            return web.json_response({"status": "success", "balance": result})
        else:
            return web.json_response({"status": "error", "message": result}, status=200)
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
            await send_telegram_message(chat_id, "Ваш персональный AI-терминал готов. Нажмите кнопку ниже для настройки подключения к бирже:")
        return web.Response(text="OK", status=200)
    except Exception:
        return web.Response(text="Error", status=500)

async def main():
    app = web.Application()
    app.router.add_get('/', index_handler)
    app.router.add_post('/webhook', webhook_handler)
    app.router.add_post('/api/connect_bitget', connect_bitget_handler)
    
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
