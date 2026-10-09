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

# Реальный HTML с интеграцией Phantom Deep Link для мобильных устройств
HTML_CONTENT = """<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Zer0life Web4 Terminal</title>
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
        .card {
            background: var(--card-bg);
            border-radius: 14px;
            padding: 16px;
            margin-bottom: 16px;
            border: 1px solid #1e293b;
        }
        .card h3 { margin-top: 0; font-size: 15px; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.5px; }
        .wallet-section {
            display: flex;
            justify-content: space-between;
            align-items: center;
            background: #0f172a;
            padding: 12px 14px;
            border-radius: 10px;
            border: 1px solid #1e293b;
            margin-bottom: 12px;
        }
        .wallet-info { font-size: 13px; color: var(--text-muted); }
        .wallet-address { font-size: 13px; font-weight: bold; color: #38bdf8; }
        .coin-row {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 10px 0;
            border-bottom: 1px solid #1e293b;
            font-size: 14px;
        }
        .coin-row:last-child { border-bottom: none; }
        .btn {
            background: linear-gradient(135deg, #7c3aed, #6366f1);
            color: white;
            border: none;
            width: 100%;
            padding: 12px;
            border-radius: 10px;
            font-size: 14px;
            font-weight: bold;
            cursor: pointer;
            box-shadow: 0 4px 12px var(--accent-glow);
            margin-top: 10px;
        }
        .btn:active { transform: scale(0.98); }
        .btn-wallet {
            background: #512da8;
            width: auto;
            margin-top: 0;
            padding: 8px 14px;
            font-size: 12px;
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
        <span style="color: #38bdf8; font-size: 12px; font-weight: bold;">Solana / DEX</span>
    </div>

    <div class="card">
        <h3>🔗 Реальный Web3 Кошелек</h3>
        <div class="wallet-section">
            <div>
                <div class="wallet-info">Статус: <span id="conn-status" style="color: var(--danger);">Не подключен</span></div>
                <div class="wallet-address" id="wallet-addr">Ожидание...</div>
            </div>
            <button class="btn btn-wallet" onclick="connectRealPhantom()">Подключить Phantom</button>
        </div>
    </div>

    <div class="card">
        <h3>📊 DEX Мониторинг</h3>
        <div class="coin-row"><span>SOL / USDC</span><span>Опрос ноды...</span></div>
        <div class="coin-row"><span>AVAX / USDC</span><span>Опрос ноды...</span></div>
        <div class="coin-row"><span>INJ / USDC</span><span>Опрос ноды...</span></div>
        <div class="coin-row"><span>XRP / USDC</span><span>Опрос ноды...</span></div>
        <div class="coin-row"><span>ADA / USDC</span><span>Опрос ноды...</span></div>
        <div class="coin-row"><span>XMR / USDC</span><span>Опрос ноды...</span></div>
    </div>

    <div class="card">
        <h3>🧠 Системные логи</h3>
        <div class="log-box" id="logs">
            [INIT] Терминал инициализирован через Web4 бэкенд.<br>
            [READY] Нажмите кнопку подключения для вызова Phantom.
        </div>
        <button class="btn" onclick="fetchRealData()">Запросить данные DEX</button>
    </div>

    <script>
        let tg = window.Telegram.WebApp;
        tg.expand();

        function connectRealPhantom() {
            const logs = document.getElementById('logs');
            const status = document.getElementById('conn-status');
            const addr = document.getElementById('wallet-addr');

            // Проверка десктопного провайдера
            if (window.solana && window.solana.isPhantom) {
                window.solana.connect().then(response => {
                    const publicKey = response.publicKey.toString();
                    status.innerText = "Подключено";
                    status.style.color = "var(--success)";
                    addr.innerText = publicKey.slice(0, 4) + '...' + publicKey.slice(-4);
                    logs.innerHTML += `<br>[OK] Phantom подключен: ${publicKey}`;
                }).catch(err => {
                    logs.innerHTML += `<br>[ERROR] Отменено пользователем.`;
                });
            } else {
                // Реальный Deep Link для мобильных устройств (перенаправление в приложение Phantom)
                const dAppUrl = encodeURIComponent(window.location.href);
                const cluster = "mainnet-beta";
                const phantomDeepLink = `https://phantom.app/ul/v1/connect?app_url=${dAppUrl}&redirect_link=${dAppUrl}&cluster=${cluster}`;
                
                logs.innerHTML += `<br>[MOBILE] Перенаправление в приложение Phantom...`;
                tg.HapticFeedback.impactOccurred('medium');
                
                // Открываем реальный Deep Link кошелька
                window.location.href = phantomDeepLink;
            }
        }

        function fetchRealData() {
            const logs = document.getElementById('logs');
            logs.innerHTML += `<br>[RPC] Запрос к Solana/DEX контрактам...`;
            logs.scrollTop = logs.scrollHeight;
            tg.HapticFeedback.impactOccurred('light');
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
        "text": f"🤖 **Zer0life Web4 Trader**\n\n{text}",
        "parse_mode": "Markdown",
        "reply_markup": {
            "inline_keyboard": [[
                {"text": "🚀 Открыть Web4 Терминал", "web_app": {"url": RENDER_URL}}
            ]]
        }
    }
    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(url, json=payload) as resp:
                pass
        except Exception as e:
            logging.error(f"Ошибка отправки: {e}")

async def telegram_webhook_handler(request):
    try:
        data = await request.json()
        message = data.get("message", {})
        text = message.get("text", "")
        chat_id = message.get("chat", {}).get("id")

        if text == "/start" and chat_id:
            welcome_text = (
                "Привет! Автономный терминал **Zer0life Web4** запущен.\n\n"
                "🔗 Нажмите кнопку ниже для реального подключения кошелька Phantom:"
            )
            asyncio.create_task(send_telegram_message(chat_id, welcome_text))
            
        return web.Response(text="OK", status=200)
    except Exception as e:
        
