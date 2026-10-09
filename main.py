import asyncio
import aiohttp
from aiohttp import web
import logging
import os
from solders.keypair import Keypair
import base58

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
PORT = int(os.getenv("PORT", 10000))
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL", "https://zer0lifeautopilot-bot.onrender.com")

# База данных кошельков пользователей в памяти (в продакшене переносится в БД)
USER_WALLETS = {}

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
        .btn { background: #7c3aed; color: white; border: none; width: 100%; padding: 12px; border-radius: 10px; font-size: 14px; font-weight: bold; cursor: pointer; margin-top: 10px; }
        .btn-green { background: #10b981; }
        .coin { display: flex; justify-content: space-between; padding: 8px 0; border-bottom: 1px solid #1e293b; font-size: 14px; }
        .balance-val { color: #10b981; font-weight: bold; }
        .profile-header { display: flex; align-items: center; gap: 12px; margin-bottom: 10px; }
        .avatar { width: 48px; height: 48px; border-radius: 50%; background: #334155; object-fit: cover; }
        .address-box { background: #0b0f19; padding: 8px; border-radius: 8px; font-size: 11px; color: #38bdf8; word-break: break-all; margin-top: 6px; }
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
        <p>Статус: <span style="color: #10b981; font-weight: bold;">🟢 Торговый агент на Solana</span></p>
    </div>
    
    <div class="card">
        <h3>👛 Ваш персональный DEX-кошелек</h3>
        <p style="color: #94a3b8; font-size: 12px;">Пополните этот адрес в сети Solana (SOL), чтобы AI-бот начал автономную торговлю.</p>
        
        <div class="address-box" id="wallet-address">Генерация кошелька...</div>
        
        <div style="margin-top: 12px;" class="coin">
            <span>Баланс SOL:</span>
            <span class="balance-val" id="wallet-balance">0.00 SOL</span>
        </div>

        <button class="btn" onclick="refreshBalance()">Обновить баланс</button>
        <button class="btn btn-green" onclick="toggleAutopilot()" id="autopilot-btn">Запустить AI Автопилот</button>
    </div>

    <div class="card">
        <h3>📊 DEX Пул ликвидности</h3>
        <div class="coin"><span>ZRL / SOL (Raydium)</span><span style="color: #10b981;">Сканирование</span></div>
        <div class="coin"><span>SOL / USDC (Jupiter)</span><span style="color: #10b981;">Активен</span></div>
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

        let userPubKey = "";

        async function initWallet() {
            try {
                const response = await fetch('/api/get_wallet', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ user_id: userId })
                });
                const data = await response.json();
                if (data.status === "success") {
                    userPubKey = data.pubkey;
                    document.getElementById('wallet-address').innerText = userPubKey;
                    document.getElementById('wallet-balance').innerText = data.balance + " SOL";
                }
            } catch (e) {
                console.error(e);
            }
        }

        async function refreshBalance() {
            tg.HapticFeedback.impactOccurred('light');
            await initWallet();
            tg.showAlert("Баланс обновлен из блокчейна Solana!");
        }

        function toggleAutopilot() {
            tg.HapticFeedback.notificationOccurred('success');
            const btn = document.getElementById('autopilot-btn');
            if (btn.innerText.includes("Запустить")) {
                btn.innerText = "Остановить Автопилот";
                btn.style.background = "#ef4444";
                tg.showAlert("AI Автопилот успешно запущен на вашем DEX-кошельке!");
            } else {
                btn.innerText = "Запустить AI Автопилот";
                btn.style.background = "#10b981";
                tg.showAlert("AI Автопилот остановлен.");
            }
        }

        initWallet();
    </script>
</body>
</html>
"""

async def get_solana_balance(pubkey_str):
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://api.mainnet-beta.solana.com",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "getBalance",
                    "params": [pubkey_str]
                },
                timeout=10
            ) as resp:
                data = await resp.json()
                if "result" in data and "value" in data["result"]:
                    lamports = data["result"]["value"]
                    return f"{lamports / 1e9:.4f}"
        return "0.0000"
    except Exception:
        return "0.0000"

async def get_wallet_handler(request):
    try:
        data = await request.json()
        user_id = str(data.get("user_id"))
        
        if user_id not in USER_WALLETS:
            # Генерируем новый кошелек Solana для пользователя
            kp = Keypair()
            pubkey = str(kp.pubkey())
            secret = base58.b58encode(bytes(kp)).decode('utf-8')
            USER_WALLETS[user_id] = {
                "pubkey": pubkey,
                "secret": secret
            }
            logging.info(f"Создан новый DEX-кошелек для юзера {user_id}: {pubkey}")
        
        pubkey = USER_WALLETS[user_id]["pubkey"]
        balance = await get_solana_balance(pubkey)
        
        return web.json_response({
            "status": "success",
            "pubkey": pubkey,
            "balance": balance
        })
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
        "text": f"🤖 **Zer0life DEX**\n\n{text}",
        "parse_mode": "Markdown",
        "reply_markup": {
            "inline_keyboard": [[
                {"text": "🚀 Открыть DEX Терминал", "web_app": {"url": RENDER_URL}}
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
            await send_telegram_message(chat_id, "Ваш персональный DEX-терминал на Solana инициализирован. Нажмите кнопку ниже:")
        return web.Response(text="OK", status=200)
    except Exception:
        return web.Response(text="Error", status=500)

async def main():
    app = web.Application()
    app.router.add_get('/', index_handler)
    app.router.add_post('/webhook', webhook_handler)
    app.router.add_post('/api/get_wallet', get_wallet_handler)
    
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
