import asyncio
import aiohttp
from aiohttp import web
import logging
import os

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
PORT = int(os.getenv("PORT", 10000))
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL", "https://zer0lifeautopilot-bot.onrender.com")

HTML_CONTENT = """<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Zer0life DEX Terminal</title>
    <script src="https://telegram.org/js/telegram-web-app.js"></script>
    <style>
        body { background-color: #06080f; color: #f8fafc; font-family: sans-serif; margin: 0; padding: 16px; }
        .card { background: #0f172a; border-radius: 16px; padding: 18px; margin-bottom: 16px; border: 1px solid #1e293b; }
        .btn { background: #7c3aed; color: white; border: none; width: 100%; padding: 14px; border-radius: 12px; font-size: 14px; font-weight: bold; cursor: pointer; margin-top: 10px; }
        .btn-green { background: #10b981; }
        .coin { display: flex; justify-content: space-between; padding: 10px 0; border-bottom: 1px solid #1e293b; font-size: 13px; }
        .log-box { background: #030712; padding: 12px; border-radius: 10px; font-family: monospace; font-size: 11px; color: #34d399; max-height: 150px; overflow-y: auto; margin-top: 12px; border: 1px solid #1e293b; }
    </style>
</head>
<body>
    <div class="card">
        <h2>⚡ Zer0life DEX Core</h2>
        <p>Статус: <span style="color: #10b981; font-weight: bold;">🟢 Подключено к Jupiter API</span></p>
        
        <button class="btn btn-green" onclick="checkJupiter()">Проверить ликвидность SOL/USDC</button>

        <div class="log-box" id="logs">
            Система инициализирована. Ожидание запроса...
        </div>
    </div>

    <div class="card">
        <h3>📊 Целевые пары</h3>
        <div class="coin"><span>SOL / USDC (Jupiter DEX)</span><span style="color: #10b981;">Активно</span></div>
        <div class="coin"><span>AVAX / SOL (Raydium)</span><span style="color: #38bdf8;">Сканирование</span></div>
        <div class="coin"><span>INJ / SOL (Raydium)</span><span style="color: #38bdf8;">Сканирование</span></div>
    </div>

    <script>
        let tg = window.Telegram.WebApp;
        tg.expand();

        async function checkJupiter() {
            const box = document.getElementById('logs');
            box.innerHTML += `<br>[${new Date().toLocaleTimeString()}] Запрос котировки у Jupiter...`;
            try {
                // SOL -> USDC
                const res = await fetch('/api/quote?input=So11111111111111111111111111111111111111112&output=EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v&amount=1000000000');
                const data = await res.json();
                if (data.success) {
                    box.innerHTML += `<br><span style="color: #38bdf8;">[OK] Маршрут найден. OutAmount: ${data.outAmount}</span>`;
                } else {
                    box.innerHTML += `<br><span style="color: #ef4444;">[Ошибка] ${data.error}</span>`;
                }
            } catch(e) {
                box.innerHTML += `<br><span style="color: #ef4444;">[Ошибка сети]</span>`;
            }
            box.scrollTop = box.scrollHeight;
        }
    </script>
</body>
</html>
"""

async def quote_handler(request):
    input_mint = request.query.get("input")
    output_mint = request.query.get("output")
    amount = request.query.get("amount")
    
    url = f"https://quote-api.jup.ag/v6/quote?inputMint={input_mint}&outputMint={output_mint}&amount={amount}&slippageBps=50"
    
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
            await send_telegram_message(chat_id, "DEX-терминал готов к работе. Нажмите кнопку ниже:")
        return web.Response(text="OK", status=200)
    except Exception:
        return web.Response(text="Error", status=500)

async def main():
    app = web.Application()
    app.router.add_get('/', index_handler)
    app.router.add_post('/webhook', webhook_handler)
    app.router.add_get('/api/quote', quote_handler)
    
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
