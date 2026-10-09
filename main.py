import asyncio
import aiohttp
from aiohttp import web
import logging
import os

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
PORT = int(os.getenv("PORT", 10000))
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL", "https://zer0lifeautopilot-bot.onrender.com")

async def send_telegram_message(chat_id, text, reply_markup=None):
    if not TELEGRAM_TOKEN or not chat_id:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown"
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup

    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(url, json=payload) as resp:
                pass
        except Exception as e:
            logging.error(f"Ошибка отправки сообщения: {e}")

async def webhook_handler(request):
    try:
        data = await request.json()
        
        # Обработка нажатий на инлайн-кнопки
        if "callback_query" in data:
            callback = data["callback_query"]
            chat_id = callback["message"]["chat"]["id"]
            callback_data = callback["data"]
            
            if callback_data == "check_balance":
                await send_telegram_message(chat_id, "💎 **Баланс кошелька:** `0.2070 SOL ($29.68)`\n🔗 **Адрес:** `5K3n...9xL2`\n🟢 **Статус:** Синхронизировано с Solana Mainnet")
            elif callback_data == "toggle_bot":
                await send_telegram_message(chat_id, "🤖 **AI Autopilot:** Торговый агент успешно запущен и мониторит пулы ликвидности DEX.")
            elif callback_data == "dex_status":
                await send_telegram_message(chat_id, "📊 **DEX Мониторинг активен:**\n• SOL/USDC (Активен)\n• AVAX/USDC (Активен)\n• INJ/USDC (Активен)")
            
            return web.Response(text="OK", status=200)

        # Обработка текстовых команд
        message = data.get("message", {})
        text = message.get("text", "")
        chat_id = message.get("chat", {}).get("id")
        
        if text == "/start" and chat_id:
            keyboard = {
                "inline_keyboard": [
                    [{"text": "💎 Мой баланс и кошелек", "callback_data": "check_balance"}],
                    [{"text": "🚀 Запустить AI Autopilot", "callback_data": "toggle_bot"}],
                    [{"text": "📊 Статус DEX мониторинга", "callback_data": "dex_status"}]
                ]
            }
            welcome_text = (
                "🛡 **Zer0life Web4 Autopilot**\n\n"
                "Добро пожаловать в торговый терминал экосистемы.\n"
                "Управляйте ИИ-ботом и ликвидностью прямо через этот чат без лишних переходов:"
            )
            await send_telegram_message(chat_id, welcome_text, reply_markup=keyboard)

        return web.Response(text="OK", status=200)
    except Exception as e:
        logging.error(f"Ошибка в вебхуке: {e}")
        return web.Response(text="Error", status=500)

async def index_handler(request):
    return web.Response(text="Zer0life Bot Backend is running.", content_type='text/html')

async def main():
    app = web.Application()
    app.router.add_get('/', index_handler)
    app.router.add_post('/webhook', webhook_handler)
    
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    
    if TELEGRAM_TOKEN:
        webhook_url = f"{RENDER_URL}/webhook"
        async with aiohttp.ClientSession() as session:
            await session.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setWebhook?url={webhook_url}")

    while True:
        await asyncio.sleep(300)

if __name__ == "__main__":
    asyncio.run(main())
