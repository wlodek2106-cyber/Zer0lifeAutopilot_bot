import asyncio
import aiohttp
from aiohttp import web
import logging
import os

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
PORT = int(os.getenv("PORT", 10000))

# Функция отправки сообщения с кнопкой Mini App (по умолчанию add_webapp=True)
async def send_telegram_message(text, chat_id=None, add_webapp=True):
    target_chat = chat_id or TELEGRAM_CHAT_ID
    if not TELEGRAM_TOKEN or not target_chat:
        logging.info(f"[TELEGRAM LOG]: {text}")
        return
    
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": target_chat,
        "text": f"🤖 **Zer0lifeAutopilot**\n\n{text}",
        "parse_mode": "Markdown"
    }
    
    if add_webapp:
        render_url = os.getenv("RENDER_EXTERNAL_URL", "https://zer0life-autopilot.onrender.com")
        payload["reply_markup"] = {
            "inline_keyboard": [[
                {"text": "🚀 Открыть Панель Управления", "web_app": {"url": render_url}}
            ]]
        }

    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(url, json=payload) as resp:
                if resp.status != 200:
                    logging.error(f"Ошибка отправки в Telegram: {await resp.text()}")
        except Exception as e:
            logging.error(f"Ошибка сети Telegram: {e}")

async def handle_telegram_updates():
    if not TELEGRAM_TOKEN:
        return
    
    offset = 0
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
    
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                async with session.get(url, params={"offset": offset, "timeout": 30}) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        for result in data.get("result", []):
                            offset = result["update_id"] + 1
                            message = result.get("message", {})
                            text = message.get("text", "")
                            chat_id = message.get("chat", {}).get("id")
                            
                            if text == "/start" and chat_id:
                                welcome_text = (
                                    "Привет! Автономный ИИ-трейдер **Zer0lifeAutopilot** успешно работает.\n\n"
                                    "Нажмите кнопку ниже, чтобы открыть графический интерфейс управления (Mini App):"
                                )
                                await send_telegram_message(welcome_text, chat_id=chat_id, add_webapp=True)
            except Exception as e:
                logging.error(f"Ошибка опроса Telegram: {e}")
            
            await asyncio.sleep(2)

async def trading_background_loop():
    while True:
        logging.info("--- Цикл сканирования рынка DEX ---")
        await asyncio.sleep(300)

async def index_handler(request):
    return web.FileResponse('index.html')

async def main():
    app = web.Application()
    app.router.add_get('/', index_handler)
    
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    logging.info(f"Веб-сервер Mini App запущен на порту {PORT}")

    await asyncio.gather(
        handle_telegram_updates(),
        trading_background_loop()
    )

if __name__ == "__main__":
    asyncio.run(main())
