import asyncio
import aiohttp
from aiohttp import web
import logging
import os
from datetime import datetime

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
PORT = int(os.getenv("PORT", 10000))
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL", "https://zer0lifeautopilot-bot.onrender.com")

# Официальные mint-адреса токенов в Solana
TOKENS = {
    "SOL": "So11111111111111111111111111111111111111112",
    "USDC": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    "ZRL": "TokenZRLMintAddressPlaceholderHere" # Замените на реальный mint ZRL при деплое
}

AI_MEMORY_LOGS = [
    f"[{datetime.now().strftime('%H:%M:%S')}] Jupiter DEX Core: Модуль агрегации ликвидности подключен."
]

HTML_CONTENT = """<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Zer0life DEX Terminal</title>
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
                <h3 id="user-nickname" style="margin: 0; color: #38bdf8;">DEX Master</h3>
                <p style="margin: 4px 0 0 0; font-size: 11px; color: #94a3b8;">Web4 ID: <span id="user-id">---</span> <span class="badge">SOLANA DEX</span></p>
            </div>
        </div>
    </div>

    <div class="card">
        <h2>⚡ Jupiter DEX Aggregator</h2>
        <p>Статус: <span style="color: #34d399; font-weight: bold;">🟢 Маршрутизация активна</span></p>
        <p style="color: #94a3, font-size: 12px; margin-top: 8px; color: #94a3b8;">Запрос реальных котировок и ликвидности через официальный шлюз Jupiter.</p>
        
        <button class="btn btn-green" onclick="fetchJupiterQuote()">Запросить котировку SOL -> USDC</button>

        <div class="log-box" id="ai-logs">
            Инициализация DEX терминала...
        </div>
    </div>

    <script>
        let tg = window.Telegram.WebApp;
        tg.expand();

        const userId = tg.initDataUnsafe?.user?.id || "999999999";
        const firstName = tg.initDataUnsafe?.user?.first_name || "DEX Master";
        const photoUrl = tg.initDataUnsafe?.user?.photo_url || "https://i.imgur.com/6VBx3io.png";

        document.getElementById('user-id').innerText = userId;
        document.getElementById('user-nickname').innerText = firstName;
        document.getElementById('user-avatar').src = photoUrl;

        async function fetchJupiterQuote() {
            const box = document.getElementById('ai-logs');
            box.innerHTML += `<br>[${new Date().toLocaleTimeString()}] Запрос котировки у Jupiter API...`;
            try {
                const res = await fetch('/api/jupiter_quote?inputMint=So11111111111111111111111111111111111111112&outputMint=EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v&amount=1000000000');
                const data = await res.json();
                if (data.success) {
                    box.innerHTML += `<br><span style="color: #38bdf8;">[OK] Лучший маршрут найден. Выходное количество: ${data.outAmount} микро-USDC</span>`;
                } else {
                    box.innerHTML += `<br><span style="color: #ef4444;">[Error] ${data.error}</span>`;
                }
            } catch(e) {
                box.innerHTML += `<br><span style="color: #ef4444;">[Error] Сбой сети</span>`;
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

        setInterval(fetchLogs, 7000);
    </script>
</body>
</html>
"""

async def get_jupiter_quote_handler(request):
    input_mint = request.query.get("inputMint", TOKENS["SOL"])
    output_mint = request.query.get("outputMint", TOKENS["USDC"])
    amount = request.query.get("amount", "1000000000") # 1 SOL в лампортах
    
    url = f"https://quote-api.jup.ag/v6/quote?inputMint={input_mint}&outputMint={output_mint}&amount={amount}&slippageBps=50"
    
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    out_amount = data.get("outAmount", "0")
                    return web.json_response({"success": True, "outAmount": out_amount, "raw": data})
                else:
                    text = await resp.text()
                    return web.json_response({"success": False, "error": text}, status=400)
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=500)

async def logs_handler(request):
    return web.json_response({"logs": AI_MEMORY_LOGS})

async def index_handler(request):
    return web.Response(text=HTML_CONTENT, content_type='text/html')

async def main():
    app = web.Application()
    app.router.add_get('/', index_handler)
    app.router.add_get('/api/jupiter_quote', get_jupiter_quote_handler)
    app.router.add_get('/api/logs', logs_handler)
    
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()

    while True:
        await asyncio.sleep(300)

if __name__ == "__main__":
    asyncio.run(main())
