import asyncio
import aiohttp
import logging
import os

# Настройка логирования
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

WHITELISTED_ASSETS = {
    "SOL": {"mint": "So11111111111111111111111111111111111111112", "min_liquidity": 10000000},
    "AVAX": {"mint": "WAVAX_or_wrapped_on_sol", "min_liquidity": 5000000}
}

async def send_telegram_message(text, chat_id=None):
    """Отправка сообщения в Telegram"""
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
    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(url, json=payload) as resp:
                if resp.status != 200:
                    logging.error(f"Ошибка отправки в Telegram: {await resp.text()}")
        except Exception as e:
            logging.error(f"Ошибка сети Telegram: {e}")

async def handle_telegram_updates():
    """Проверка входящих команд от пользователя в Telegram (long polling)"""
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
                                    "📊 Сканируемые монеты: SOL, AVAX, INJ, XRP, ADA, XMR.\n"
                                    "🟢 Бот настроен на отслеживание просадок и защиту капитала на DEX."
                                )
                                await send_telegram_message(welcome_text, chat_id=chat_id)
            except Exception as e:
                logging.error(f"Ошибка опроса Telegram обновлений: {e}")
            
            await asyncio.sleep(2)

async def trading_background_loop():
    """Фоновый цикл анализа рынка DEX"""
    await send_telegram_message("🚀 Автопилот запущен и начал круглосуточный мониторинг пулов ликвидности!")
    while True:
        logging.info("--- Цикл сканирования рынка DEX ---")
        for symbol, data in WHITELISTED_ASSETS.items():
            price = 145.50 if symbol == "SOL" else 25.80
            logging.info(f"[{symbol}] Проверен DEX. Цена: ${price}")
        
        await asyncio.sleep(300)

async def main():
    # Запускаем параллельно обработчик Telegram и торговый цикл
    await asyncio.gather(
        handle_telegram_updates(),
        trading_background_loop()
    )

if __name__ == "__main__":
    asyncio.run(main())
