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
        <h3>🔗 Кошелек Solana</h3>
        <p id="wallet-status" style="color: #94a3b8; font-size: 13px;">Статус: Не подключен</p>
        <div id="balances-container" style="display:none; margin-top: 10px;">
            <div class="coin"><span>Адрес</span><span id="wallet-addr" style="color: #38bdf8; font-size: 11px;">---</span></div>
            <div class="coin"><span>Solana (SOL)</span><span class="balance-val" id="bal-sol">Загрузка...</span></div>
        </div>
        <button class="btn" id="conn-btn" onclick="connectWalletInTG()">Подключить Phantom</button>
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

        async function connectWalletInTG() {
            try {
                tg.HapticFeedback.impactOccurred('medium');

                // Проверка доступности провайдера Phantom в WebApp окружении
                const provider = window.solana || window.phantom?.solana;

                if (provider) {
                    const resp = await provider.connect();
                    const pubKey = resp.publicKey.toString();
                    
                    document.getElementById('wallet-status').innerText = "Подключено";
                    document.getElementById('wallet-addr').innerText = pubKey;
                    document.getElementById('balances-container').style.display = 'block';
                    document.getElementById('conn-btn').innerText = 'Кошелек синхронизирован';
                    document.getElementById('conn-btn').style.background = '#10b981';

                    // Запрос реального баланса через Web3.js
                    const connection = new solanaWeb3.Connection(solanaWeb3.clusterApiUrl('mainnet-beta'), 'confirmed');
                    const balance = await connection.getBalance(new solanaWeb3.PublicKey(pubKey));
                    document.getElementById('bal-sol').innerText = (balance / solanaWeb3.LAMPORTS_PER_SOL).toFixed(4) + " SOL";
                } else {
                    // Если инжект провайдера заблокирован телеграмом на iOS, используем защищенный универсальный диплинк подписи
                    const encodedUrl = encodeURIComponent(window.location.href);
                    window.location.href = `https://phantom.app/ul/v1/connect?app_url=${encodedUrl}&redirect_link=${encodedUrl}&cluster=mainnet-beta`;
                }
            } catch (err) {
                alert("Ошибка подключения: " + err.message);
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
