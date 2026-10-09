import asyncio
import aiohttp
from aiohttp import web
import sqlite3
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s - [%(levelname)s] - %(message)s')

DB_FILE = "zer0life_users.db"

def init_db():
    """Создаем чистую реляционную базу данных для реальных аккаунтов пользователей"""
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
    """Достаем или создаем реальный персональный аккаунт пользователя"""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT telegram_id, username, first_name, solana_wallet FROM users WHERE telegram_id = ?", (telegram_id,))
    row = cursor.fetchone()
    
    if not row:
        # Создаем пустой профиль без фейковых кошельков — пользователь сам привяжет свой
        cursor.execute("INSERT INTO users (telegram_id, username, first_name, solana_wallet) VALUES (?, ?, ?, ?)", 
                       (telegram_id, username, first_name, ""))
        conn.commit()
        user = {"telegram_id": telegram_id, "username": username, "first_name": first_name, "solana_wallet": ""}
    else:
        user = {"telegram_id": row[0], "username": row[1], "first_name": row[2], "solana_wallet": row[3]}
    
    conn.close()
    return user

async def api_get_profile(request):
    """Эндпоинт для получения данных конкретного аккаунта по Telegram ID"""
    try:
        data = await request.json()
        telegram_id = int(data.get("telegram_id", 0))
        username = data.get("username", "unknown")
        first_name = data.get("first_name", "Trader")
        
        if not telegram_id:
            return web.json_response({"success": False, "error": "Missing telegram_id"}, status=400)
            
        user = get_or_create_user(telegram_id, username, first_name)
        return web.json_response({"success": True, "profile": user})
    except Exception as e:
        return web.json_response({"success": False, "error": str(e)}, status=500)

async def main():
    init_db()
    app = web.Application()
    app.router.add_post('/api/profile', api_get_profile)
    
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', 10000)
    await site.start()
    logging.info("Сервер персональных аккаунтов запущен.")
    
    while True:
        await asyncio.sleep(3600)

if __name__ == "__main__":
    asyncio.run(main())
