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
DEX_LAUNCHPAD_URL = "https://jup.ag/swap/SOL-ZRL"

HTML_CONTENT = """<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Zer0life Web4 Divine Terminal</title>
    <script src="https://telegram.org/js/telegram-web-app.js"></script>
    <style>
        body { background-color: #06080f; color: #f8fafc; font-family: 'SF Pro Display', -apple-system, sans-serif; margin: 0; padding: 16px; }
        .card { background: #0f172a; border-radius: 16px; padding: 18px; margin-bottom: 16px; border: 1px solid #1e293b; box-shadow: 0 8px 32px rgba(0,0,0,0.4); }
        .btn { background: linear-gradient(135deg, #7c3aed 0%, #4f46e5 100%); color: white; border: none; width: 100%; padding: 14px; border-radius: 12px; font-size: 14px; font-weight: bold; cursor: pointer; margin-top: 10px; text-decoration: none; display: block; text-align: center; box-sizing: border-box; box-shadow: 0 4px 15px rgba(124,58,237,0.4); }
        .btn-green { background: linear-gradient(135deg, #10b981 0%, #059669 100%); box-shadow: 0 4px 15px rgba(16,185,129,0.4); }
        .metric { display: flex; justify-content: space-between; padding: 10px 0; border-bottom: 1px solid #1e293b; font-size: 13px; }
        .profile-header { display: flex; align-items: center; gap: 14px; margin-bottom: 6px; }
        .avatar { width: 52px; height: 52px; border-radius: 50%; background: #334155; object-fit: cover; border: 2px solid #7c3aed; }
        .log-box { background: #030712; padding: 12px; border-radius: 10px; font-family: 'Courier New', monospace; font-size: 11px; color: #34d399; max-height: 140px; overflow-y: auto; margin-top: 12px; border: 1px solid #1e293b; }
        .badge { background: rgba(16,185,129,0.15); color: #34d399; padding: 4px 8px; border-radius: 6px; font-size: 10px; font-weight: bold; }
    </style>
</head>
<body>
    <div class="card">
        <div class="profile-header">
            <img id="user-avatar" class="avatar" src="https://i.imgur.com/6VBx3io.png" alt="Avatar">
            <div>
                <h3 id="user-nickname" style="margin: 0; color: #38bdf8;">Supreme Trader</h3>
                <p style="margin: 4px 0 0 0; font-size: 11px; color: #94a3b8;">Web4 ID: <span id="user-id">---</span> <span class="badge">GOD MODE</span></p>
            </div>
        </div>
    </div>

    <div class="card">
        <h2>⚡ Zer0life Web4 Supreme AI</h2>
        <p>Интеллектуальный статус: <span style="color: #34d399; font-weight: bold;">🟢 Абсолютный контроль ликвидности</span></p>
        <p style="color: #94a3b8; font-size: 12px; margin-top: 8px;">Автономный нейросетевой кластер высшего порядка анализирует мультичейн-пулы и исполняет ордера без проскальзывания.</p>
        
        <div class="log-box" id="ai-logs">
            [SYS] Web4 Neural Cluster initialized...<br>
            [GOD-AI] Synced with Solana & Jupiter Liquidity Matrix.<br>
            [GOD-AI] Monitoring ZRL tokenomics & momentum...
        </div>

        <a href="https://jup.ag/swap/SOL-ZRL" target="_blank" class="btn btn-green">🚀 Войти в DEX Launchpad (ZRL)</a>
    </div>

    <div class="card">
        <h3>📊 Метрики высшего разума</h3>
        <div class="metric"><span>ZRL / SOL (Raydium Matrix)</span><span style="color: #34d399; font-weight: bold;">Оптимизация 99.8%</span></div>
        <div class="metric"><span>SOL / USDC (Jupiter Router)</span><span style="color: #34d399; font-weight: bold;">Арбитраж активен</span></div>
        <div class="metric"><span>Макро-тренд рынка</span><span style="color: #38bdf8; font-weight: bold;">Бычий импульс</span></div>
    </div>

    <script>
        let tg = window.Telegram.WebApp;
        tg.expand();

        const userId = tg.initDataUnsafe?.user?.id || "999999999";
        const firstName = tg.initDataUnsafe?.user?.first_name || "Supreme Trader";
        const photoUrl = tg.initDataUnsafe?.user?.photo_url || "https://i.imgur.com/6VBx3io.png";

        document.getElementById('user-id').innerText = userId;
        document.getElementById('user-nickname').innerText = firstName;
        document.getElementById('user-avatar').src = photoUrl;

        // Генерация высокоинтеллектуальных логов ИИ Бога-Трейдера
        setInterval(() => {
            const divineLogs = [
                "GOD-AI: Анализ глубины стакана Raydium (ZRL/SOL)... Спреды минимальны.",
                "GOD-AI: К
