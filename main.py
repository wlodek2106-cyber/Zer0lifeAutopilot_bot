import asyncio
import aiohttp
from aiohttp import web
import sqlite3
import os
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s - [%(levelname)s] - %(message)s')

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
PORT = int(os.getenv("PORT", 10000))
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL", "https://zer0lifeautopilot-bot.onrender.com")
DB_FILE = "zer0life_users.db"
SOLANA_RPC = "https://api.mainnet-beta.solana.com"

def init_db():
    """Создаем чистую базу данных для реальных персональных аккаунтов"""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            telegram_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            solana_wallet TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()

def get_or_create_user(telegram_id: int, username: str, first_name: str):
    """Достаем или создаем персональный аккаунт пользователя по его ID"""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT telegram_id, username, first_name, solana_wallet FROM users WHERE telegram_id = ?", (telegram_id,))
    row = cursor.fetchone()
    
    if not row:
        cursor.execute("INSERT INTO users (telegram_id, username, first_name, solana_wallet) VALUES (?, ?, ?, ?)", 
                       (telegram_id, username, first_name, ""))
        conn.commit()
        user = {"telegram_id": telegram_id, "username": username, "first_name": first_name, "solana_wallet": ""}
    else:
        user = {"telegram_id": row[0], "username": row[1], "first_name": row[2], "solana_wallet": row[3]}
    
    conn.close()
    return user

# Чистый HTML интерфейс профиля с выводом аватарки и баланса из блокчейна
HTML_CONTENT = """<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Zer0Life Profile</title>
    <script src="https://telegram.org/js/telegram-web-app.js"></script>
    <style>
        body { background-color: #06080f; color: #f8fafc; font-family: -apple-system, sans-serif; margin: 0; padding: 16px; }
        .card { background: #0f172a; border-radius: 16px; padding: 18px; margin-bottom: 16px; border: 1px solid #1e293b; }
        .profile-header { display: flex; align-items: center; gap: 14px; margin-bottom: 14px; }
        .avatar { width: 52px; height: 52px; border-radius: 50%; object-fit: cover; border: 2px solid #7c3aed; background: #1e293b; display: none; }
        .input-field { width: 100%; background: #030712; border: 1px solid #1e293b; color: white; padding: 12px; border-radius: 8px; box-sizing: border-box; margin-top: 8px; font-family: monospace; }
        .btn { background: #7c3aed; color: white; border: none; width: 100%; padding: 14px; border-radius: 12px; font-weight: bold; cursor: pointer; margin-top: 12px; }
        .btn-green { background: #10b981; }
        .metric { display: flex; justify-content: space-between; margin-top: 10px; font-size: 13px; color: #94a3b8; }
        .val { color: #34d399; font-weight: bold; font-family: monospace; }
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
        
        <label style="font-size: 12px; color: #94a3b8;">Ваш Solana кошелек:</label>
        <input type="text" id="wallet-input" class="input-field" placeholder="Введите адрес кошелька...">
        <button class="btn" onclick="saveWallet()">Сохранить кошелек</button>
    </div>

    <div class="card">
        <h3 style="margin-top: 0; font-size: 15px;">⚡ Баланс блокчейна</h3>
        <div class="metric"><span>Баланс SOL:</span> <span id="wallet-balance" class="val">0.00 SOL</span></div>
        <button class="btn btn-green" onclick="checkBalance()">Проверить баланс в сети</button>
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

        async function loadProfile() {
            const res = await fetch('/api/profile', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ telegram_id: user.id, username: user.username || "", first_name: user.first_name || "" })
            });
            const data = await res.json();
            if (data.success && data.profile.solana_wallet) {
                document.getElementById('wallet-input').value = data.profile.solana_wallet;
                checkBalance();
            }
        }

        async function saveWallet() {
            const wallet = document.getElementById('wallet-input').value.trim();
            const res = await fetch('/api/wallet/update', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ telegram_id: user.id, solana_wallet: wallet })
            });
            const data = await res.json();
            if (data.success) {
                tg.showAlert("Кошелек успешно сохранен в базе данных!");
                checkBalance();
            }
        }

        async function checkBalance() {
            const wallet = document.getElementById('wallet-input').value.trim();
            if (!wallet) return;
            document.getElementById('wallet-balance').innerText = "Запрос...";
            const res = await fetch('/api/blockchain/balance?wallet=' + wallet);
            const data = await res.json();
            if (data.success) {
                document.getElementById('wallet-balance').innerText = data.balance + " SOL";
            } else {
                document.getElementById('wallet-balance').innerText = "Ошибка";
            }
        }

        loadProfile();
    </script>
</body>
</html>
"""

async def index_handler(request):
    return web.Response(text=HTML_CONTENT, content_type='text/html')

async def api_get_profile(request):
    try:
        data = await request.json()
        user = get_or_create_user(int(data.get("telegram_id")), data.get("username", ""), data.get("first_name", ""))
        return web.json_response({"success": True, "profile": user})
    except Exception as e:
        return web.json_response({"success": False, "error": str(e)}, status=500)

async def api_update_wallet(request
