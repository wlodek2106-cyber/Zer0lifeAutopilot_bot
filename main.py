import asyncio
import aiohttp
from aiohttp import web
import logging
import os

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
PORT = int(os.getenv("PORT", 10000))
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL", "https://zer0lifeautopilot-bot.onrender.com")

HTML_CONTENT = """<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Zer0life Web4 Terminal</title>
    <script src="https://telegram.org/js/telegram-web-app.js"></script>
    <script src="https://unpkg.com/@solana/web3.js@latest/lib/index.iife.min.js"></script>
    <style>
        body { background-color: #0b0f19; color: #f8fafc; font-family: sans-serif; margin: 0; padding: 16px; }
        .card { background: #131c2e; border-radius: 14px; padding: 16px; margin-bottom: 16px; border: 1px solid #1e293b; }
        .btn { background: #7c3aed; color: white; border: none; width: 100%; padding: 12px; border-radius: 10px; font-size: 14px; font-weight: bold; cursor: pointer; margin-top: 10px; }
        .coin { display: flex; justify-content: space-between; padding: 8px 0; border-bottom: 1px solid #1e293b; font-size: 14px; }
        .balance-val { color: #10b981; font-weight: bold; }
    </style>
</head>
<body>
    <div class="card">
        <h2>🛡 Zer0life Web4 Autopilot</h2>
        <p>Статус: <span style="color: #10b981; font-weight: bold;">🟢 Онлайн</span></p>
    </div>
    
    <div class="card">
        <h3>🔗 Привязка кошелька</h3>
        <p id="wallet-status" style="color: #94a3b8; font-size: 13px;">Введите ваш реальный Solana кошелек для мониторинга DEX</p>
        
        <div style="margin-top: 12px;">
            <input type="text" id="wallet-input" placeholder="Введите Solana Address (например, 7xKX...)" style="width: 100%; padding: 10px; border-radius: 8px; border: 1px solid #334155; background: #0b0f19; color: #fff; font-size: 12px; box-sizing: border-box;">
            <button class="btn" onclick="saveAndFetchWallet()">Подключить и запросить баланс</button>
        </div>

        <div id="balances-container" style="display:none; margin-top: 15px;">
            <div class="coin"><span>Адрес</span><span id="wallet-addr" style="color: #38bdf8; font-size: 11px;">---</span></div>
            <div class="coin"><span>Solana (SOL)</span><span class="balance-val" id="bal-sol">Запрос в сеть...</span></div>
        </div>
    </div>

    <div class="card">
        <h3>📊 DEX Мониторинг</h3>
        <div class="coin"><span>SOL / USDC</span><span><b>Активен</b></span></div>
        <div class="coin"><span>AVAX / USDC</span><span><b>Активен</b></span></div>
        <div class="coin"><span>INJ / USDC</span><span><b>Активен</b></span></div>
        <div class="coin"><span>XRP / USDC</span><span><b>Активен</b></span></div>
        <div class="coin"><span>ADA / USDC</span><span><b>Активен</b></span></div>
        <div class="coin"><span>XMR / USDC</span><span><b>Активен</b></span></div>
    </div>

    <script>
        let tg = window.Telegram.WebApp;
        tg.expand();

        // Сохраняем и запрашиваем реальный баланс через RPC ноду по введенному адресу
        async function saveAndFetchWallet() {
            const pubKey = document.getElementById('wallet-input').value.trim();
            if (!pubKey || pubKey.length < 30) {
                alert("Введите корректный адрес кошелька Solana!");
                return;
            }

            tg.HapticFeedback.impactOccurred('medium');
            
            document.getElementById('wallet-status').innerText = "Статус: Синхронизировано";
            document.getElementById('wallet-addr').innerText = pubKey.slice(0, 4) + '...' + pubKey.slice(-4);
            document.getElementById('balances-container').style.display = 'block';
            document.getElementById('bal-sol').innerText = "Загрузка из сети...";

            try {
                // Прямой запрос к публичной ноде Solana за реальными данными
                const res = await fetch('https://api.mainnet-beta.solana.com', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        jsonrpc: "2.0",
                        id: 1,
                        method: "getBalance",
                        params: [pubKey]
                    })
                });
                const data = await res.json();
                if (data.result && data.result.value !== undefined) {
                    const realSol = (data.result.value / 1e9).toFixed(4);
                    document.getElementById('bal-sol').innerText = realSol + " SOL";
                } else {
                    document.getElementById('bal-sol').innerText = "0.0000 SOL (Кошелек пуст)";
                }
            } catch (err) {
                document.getElementById('bal-sol').innerText = "Ошибка RPC сети";
            }
        }
    </script>
</body>
</html>
"""

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
                {"text": "🚀 Открыть Терминал", "web_app": {"url": RENDER_URL}}
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
            await send_telegram_message(chat_id, "Автономный терминал инициализирован. Нажмите кнопку ниже:")
        return web.Response(text="OK", status=200)
    except Exception:
        return web.Response(text="Error", status=500)

async def index_handler(request):
    return web.Response(text=HTML_CONTENT, content_type='text/html')

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
