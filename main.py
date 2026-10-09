import asyncio
import aiohttp
import logging
from datetime import datetime

logging.basicConfig(level=logging.INFO, format='%(asctime)s - [%(levelname)s] - %(message)s')

# Официальные mint-адреса токенов в сети Solana
TOKENS = {
    "SOL": "So11111111111111111111111111111111111111112",
    "USDC": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
}

async def fetch_real_jupiter_quote():
    url = f"https://api.jup.ag/swap/v1/quote?inputMint={TOKENS['SOL']}&outputMint={TOKENS['USDC']}&amount=1000000000&slippageBps=50"
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("outAmount")
                else:
                    text = await resp.text()
                    logging.error(f"Ошибка API Jupiter: {text}")
                    return None
        except Exception as e:
            logging.error(f"Сбой сети при запросе к Jupiter: {e}")
            return None

async def main():
    logging.info("Запуск чистого движка анализа ликвидности Solana DEX...")
    while True:
        out_amount = await fetch_real_jupiter_quote()
        timestamp = datetime.now().strftime('%H:%M:%S')
        if out_amount:
            logging.info(f"[{timestamp}] Успешный опрос блокчейна. 1 SOL = {int(out_amount) / 1_000_000} USDC")
        else:
            logging.warning(f"[{timestamp}] Не удалось получить данные от шлюза.")
        
        await asyncio.sleep(20)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Остановлено пользователем.")
