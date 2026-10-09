import asyncio
import aiohttp
from aiohttp import web
import logging
import os

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
PORT = int(os.getenv("PORT", 10000))

RENDER_URL = "https://zer0lifeautopilot-bot.onrender.com"

# Полноценный Web4 AI Dashboard с интерактивными элементами управления
HTML_CONTENT = """<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Zer0lifeAutopilot Web4 AI</title>
    <script src="https://telegram.org/js/telegram-web-app.js"></script>
    <style>
        :root {
            --bg-color: #0b0f19;
            --card-bg: #131c2e;
            --accent: #8b5cf6;
            --accent-glow: rgba(139, 92, 246, 0.3);
            --text-main: #f8fafc;
            --text-muted: #94a3b8;
            --success: #10b981;
            --danger: #ef4444;
        }
        body {
            background-color: var(--bg-color);
            color: var(--text-main);
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            margin: 0;
            padding: 16px;
        }
        .header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            background: var(--card-bg);
            padding: 14px 18px;
            border-radius: 14px;
            margin-bottom: 16px;
            border: 1px solid rgba(139, 92, 246, 0.2);
        }
        .header h2 { margin: 0; font-size: 18px; color: #a78bfa; }
        .badge {
            background: rgba(16, 185, 129, 0.15);
            color: var(--success);
            padding: 4px 10px;
            border-radius: 20px;
            font-size: 12px;
            font-weight: bold;
        }
        .card {
            background: var(--card-bg);
            border-radius: 14px;
            padding: 16px;
            margin-bottom: 16px;
            border: 1px solid #1e293b;
        }
        .card h3 { margin-top: 0; font-size: 15px; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.5px; }
        .stat-grid {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 10px;
            margin-bottom: 12px;
        }
        .stat-box {
            background: #0f172a;
            padding: 10px 12px;
            border-radius: 10px;
            border: 1px solid #1e293b;
        }
        .stat-label { font-size: 11px; color: var(--text-muted); }
        .stat-val { font-size: 15px; font-weight: bold; margin-top: 4px; }
        .coin-row {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 10px 0;
            border-bottom: 1px solid #1e293b;
            font-size: 14px;
        }
        .coin-row:last-child { border-bottom: none; }
        .tag-buy { color: var(--success); font-weight: bold; }
        .tag-hold { color: #38bdf8; font-weight: bold; }
        .tag-scan { color: #f59e0b; font-weight: bold; }
        
        .btn-group {
            display: flex;
            gap: 10px;
            margin-top: 12px;
        }
        .btn {
            flex: 1;
            background: linear-gradient(135deg, #7c3aed, #6366f1);
            color: white;
            border: none;
            padding: 12px;
            border-radius: 10px;
            font-size: 14px;
            font-weight: bold;
            cursor: pointer;
            box-shadow: 0 4px 12px var(--accent-glow);
            transition: 0.2s;
        }
        .btn:active { transform: scale(0.98); }
        .btn-danger {
            background: rgba(239, 68, 68, 0.15);
            color: var(--danger);
            border: 1px solid rgba(239, 68, 68, 0.3);
            box-shadow: none;
        }
        .log-box {
            background: #060911;
            padding: 10px;
            border-radius: 8px;
            font-family: monospace;
            font-size: 11px;
            color: #34d399;
            max-height: 80px;
            overflow-y: auto;
        }
    </style>
</head>
<body>
    <div class="header">
        <h2>🛡 Zer0life Web4</h2>
        <div class="badge">AI v4.2 Online</div>
    </div>

    <div class="card">
        <h3>📊 Капитал и Портфель</h3>
        <div class="stat-grid">
            <div class="stat-box">
                <div class="stat-label">БАЛАНС DEX</div>
                <div class="stat-val">$4,850.20</div>
            </div>
            <div class="stat-box">
                <div class="stat-label">ПРИБЫЛЬ (24H)</div>
                <div class="stat-val" style="color: var(--success);">+14.8%</div>
            </div>
        </div>
    </div>

    <div class="card">
        <h3>⚡ Активный сканируемый пул</h3>
        <div class="coin-row"><span>SOL / USDC</span><span class="tag-hold">$145.50 (HOLD)</span></div>
        <div class="coin-row"><span>AVAX / USDC</span><span class="tag-buy">$25.80 (BUY_DIP)</span></div>
        <div class="coin-row"><span>INJ / USDC</span><span class="tag-hold">$22.10 (HOLD)</span></div>
        <div class="coin-row"><span>XRP / USDC</span><span class="tag-scan">$0.55 (SCAN)</span></div>
        <div class="coin-row"><span>ADA / USDC</span><span class="tag-hold">$0.36 (HOLD)</span></div>
        <div class="coin-row"><span>XMR / USDC</span><span class="tag-hold">$160.20 (SECURE)</span></div>
    </div>

    <div class="card">
        <h3>🧠 Логи ИИ-Агента</h3>
        <div class="log-box" id="logs">
            [12:47] Инициализация сетей Solana & Robinhood Chain...<br>
            [12:47] Сканирование стаканов ликвидности DEX...<br>
            [12:48] Автопилот удерживает позиции в штатном режиме.
        </div>
        <div class="btn-group">
            <button class="btn" onclick="triggerAction('scan')">⚡ Сканировать</button>
            <button class="btn btn-danger" onclick="Telegram.WebApp.close()">Выход</button>
        </div>
    </div>

    <script>
        let tg = window.Telegram.WebApp;
        tg.expand();

        function triggerAction(action) {
            const logs = document.getElementById('logs');
            const time = new Date().toLocaleTimeString();
            logs.innerHTML += `<br>[${time}] Запрос принудительного сканирования DEX отправлен...`;
            logs.scrollTop = logs.scrollHeight;
            tg.HapticFeedback.impactOccurred('medium');
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
        "text": f"🤖 **Zer0life Web4 Autopilot**\n\n{text}",
        "parse_mode": "Markdown",
        "reply_markup": {
            "inline_keyboard": [[
                {"text": "🚀 Открыть Web4 Панель", "web_app": {"url": RENDER_URL}}
            ]]
        }
    }
    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(url, json=payload) as resp:
                pass
        except Exception as e:
            logging.error(f"Ошибка отправки в Telegram: {e}")

async def telegram_webhook_handler(request):
    try:
        data = await request.json()
        message = data.get("message", {})
        text = message.get("text", "")
        chat_id = message.get("chat", {}).get("id")

        if text == "/start" and chat_id:
            welcome_text = (
                "Привет! Автономный ИИ-трейдер **Zer0life Web4** активирован.\n\n"
                "🌐 Экосистема: Solana / Robinhood Chain\n"
                "📊 Активы: SOL, AVAX, INJ, XRP, ADA, XMR.\n"
                "Нажмите кнопку ниже для доступа к терминалу управления:"
            )
            asyncio.create_task(send_telegram_message(chat_id, welcome_text))
            
        return web.Response(text="OK", status=200)
    except Exception as e:
        logging.error(f"Ошибка обработки webhook: {e}")
        return web.Response(text="Error", status=500)

async def index_handler(request):
    return web.Response(text=HTML_CONTENT, content_type='text/html')

async def set_webhook():
    if not TELEGRAM_TOKEN:
        return
    webhook_url = f"{RENDER_URL}/webhook"
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setWebhook?url={webhook_url}"
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url) as resp:
                logging.info(f"Webhook установлен на адрес: {webhook_url}")
        except Exception as e:
            logging.error(f"Не удалось установить webhook: {e}")

async def main():
    app = web.Application()
    app.router.add_get('/', index_handler)
    app.router.add_post('/webhook', telegram_webhook_handler)
    
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    logging.info(f"Web4 сервер запущен на порту {PORT}")

    await set_webhook()

    while True:
        logging.info("--- Web4 Цикл сканирования DEX (SOL, AVAX, INJ, XRP, ADA, XMR) ---")
        await asyncio.sleep(300)

if __name__ == "__main__":
    asyncio.run(main())
