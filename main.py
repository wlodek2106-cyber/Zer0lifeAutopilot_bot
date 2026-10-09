import asyncio
import aiohttp
from aiohttp import web
import logging
import os
import random

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
PORT = int(os.getenv("PORT", 10000))
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL", "https://zer0lifeautopilot-bot.onrender.com")
DEX_LAUNCHPAD_URL = "https://jup.ag/swap/SOL-ZRL"

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
        .btn { background: #7c3aed; color: white; border: none; width: 100%; padding: 14px; border-radius: 10px; font-size: 14px; font-weight: bold; cursor: pointer; margin-top: 10px; text-decoration: none; display: block; text-align: center; box-sizing: border-box; }
        .btn-green { background: #10b981; }
        .coin { display: flex; justify-content: space-between; padding: 10px 0; border-bottom: 1px solid #1e293b; font-size: 14px; }
        .profile-header { display: flex; align-items: center; gap: 12px; margin-bottom: 10px; }
        .avatar { width: 48px; height: 48px; border-radius: 50%; background: #334155; object-fit: cover; }
        .log-box { background: #070a10; padding: 10px; border-radius: 8px; font-family: monospace; font-size: 11px; color: #10b981; max-height: 100px; overflow-y: auto; margin-top: 10px; }
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
        <p>Статус агента: <span style="color: #10b981; font-weight: bold;">🟢 ИИ-модуль активен</span></p>
        <p style="color: #94a3b8; font-size: 12px; margin-top: 8px;">Автономный торговый агент сканирует пулы ликвидности Solana и оптимизирует позиции по ZRL.</p>
        
        <div class="log-box" id="ai-logs">
            [15:40] AI Agent: Инициализация нейросети...<br>
            [15:40] AI Agent: Сканирование пула ZRL/SOL...<br>
            [15:41] AI Agent: Тренд стабильный. Ожидание точек входа.
        </div>

        <a href="https://jup.ag/swap/SOL-ZRL" target="_blank" class="btn btn-green">🚀 Открыть DEX Launchpad (ZRL)</a>
    </div>

    <div class="card">
        <h3>📊 Статус торговых модулей</h3>
        <div class="coin"><span>ZRL / SOL (Raydium)</span><span style="color: #10b981; font-weight: bold;">AI Scalp: Активен</span></div>
        <div class="coin"><span>SOL / USDC (Jupiter)</span><span style="color: #10b981; font-weight: bold;">AI Grid: Активен</span></div>
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

        // Имитация живых логов ИИ-агента в интерфейсе
        setInterval(() => {
            const logs = [
                "AI Agent: Проверка ликвидности стакана...",
                "AI Agent: Анализ волатильности пары ZRL/SOL...",
                "AI Agent: Оптимизация проскальзывания (Slippage 0.5%)...",
                "AI Agent: Сигнал на удержание позиции подтвержден."
            ];
            const randomLog = logs[Math.floor(Math.random() * logs.length)];
            const box = document.getElementById('ai-logs');
            const timeStr = new Date().toLocaleTimeString();
            box.innerHTML += `<br>[${timeStr}] ${randomLog}`;
            box.scrollTop = box.scrollHeight;
        }, 15000);
    </script>
</body>
</html>
"""

# Фоновый AI-агент, который циклически «торгует» и анализирует рынок
async def ai_trading_worker():
    pairs = ["ZRL/SOL", "SOL/USDC", "AVAX/USDT"]
    actions = ["Анализ ордербука", "Проверка ликвидности DEX", "Коррекция сетки ордеров", "Поиск арбитражной возможности"]
    while True:
        await asyncio.sleep(45)
        pair = random.choice(pairs)
        action = random.choice(actions
