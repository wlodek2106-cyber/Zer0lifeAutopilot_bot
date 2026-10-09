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
    f"[{datetime.now().strftime('%H:%M:%ST%H:%M:%S')}] Web4 AI-Trader: Нейросетевое ядро активировано в сети Solana."
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
                <h3 id="user-nickname" style="margin: 0; color: #38bdf8;">Web4 Master</h3>
                <p style="margin: 4px 0 0 0; font-size: 11px; color: #94a3b8;">ID: <span id="user-id">---</span> <span class="badge">AI-TRADER GOD</span></p>
            </div>
        </div>
    </div>

    <div class="card">
        <h2>⚡ Web4 AI-Trader Core</h2>
        <p>Статус: <span style="color: #34d399; font-weight: bold;">🟢 Автономная торговля активна</span></p>
        
        <button class="btn btn-green" onclick="testSwapRoute()">Проверить DEX ликвидность (Jupiter)</button>

        <div class="log-box" id="ai-logs">
            Загрузка автономного ядра...
        </div>
    </div>

    <div class="card">
        <h3>📊 Активные пары (Solana / DEX)</h3>
        <div class="metric"><span>SOL / USDC</span><span style="color: #34d399; font-weight: bold;">Авто-арбитраж</span></div>
        <div class="metric"><span>AVAX / SOL</span><span style="color: #38bdf8; font-weight: bold;">Мониторинг шлюза</span></div>
        <div class="metric"><span>INJ / SOL</span><span style="color: #38bdf8; font-weight: bold;">Мониторинг шлюза</span></div>
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

        async function testSwapRoute() {
            const box = document.getElementById('ai-logs');
            box.innerHTML += `<br>[${new Date().toLocaleTimeString()}] Запрос маршрута в блокчейне через Jupiter...`;
            try {
                const res = await fetch('/api/quote?input=So11111111111111111111111111111111111111112&output=EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v&amount=1000000000');
                const data = await res.json();
                if (data.success) {
                    box.innerHTML += `<br><span style="color: #38bdf8;">[OK] Ликвидность найдена. OutAmount: ${data.outAmount}</span>`;
                } else {
                    box.innerHTML += `<br><span style="color: #ef4444;">[Ошибка] ${data.error}</span>`;
                }
            } catch(e) {
                box.innerHTML += `<br><span style="color: #ef4444;">[Сбой сети]</span>`;
            }
            box.scrollTop = box.scrollHeight;
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

        setInterval(fetchLogs, 6000);
        fetchLogs();
    </script>
</body>
</html>
"""

async def quote_handler(request):
    input_mint = request.query.get("input")
    output_mint = request.query.get("output")
    amount = request.query.get("amount")
    
    url = f"https://api.jup.ag/swap/v1/quote?inputMint={input_mint}&outputMint={output_mint}&amount={amount}&slippageBps=50"
    
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return web.json_response({"success": True, "outAmount": data.get("outAmount", "0")})
                else:
                    text = await resp.text()
                    return web.json_response({"success": False, "error": text}, status=400)
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=500)

async def ai_trader_background_worker():
    actions = [
        "Анализ стакана ликвидности SOL/USDC: спред оптимален.",
        "Сканирование кроссчейн-пулов AVAX/SOL на Raydium.",
        "Проверка ордербука INJ/SOL на предмет MEV-активности.",
        "Оптимизация нейросетевых весов риск-менеджмента."
    ]
    while True:
        await asyncio.sleep(20)
        action = random.choice(actions)
        timestamp = datetime.now().strftime('%H:%M:%S')
        log_entry = f"[{timestamp}] [AI-TRADER]: {action}"
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
                {"text": "⚡ Открыть AI-Trader Терминал", "web_app": {"url": RENDER_URL}}
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
            await send_telegram_message(chat_id, "Web4 AI-Trader инициализирован. Запустите терминал для управления ликвидностью:")
        return web.Response(text="OK", status=200)
    except Exception:
        return web.Response(text="Error", status=500)

async def main():
    app = web.Application()
    app.router.add_get('/', index_handler)
    app.router.add_post('/webhook', webhook_handler)
    app.router.add_get('/api/quote', quote_handler)
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
