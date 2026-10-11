import asyncio
import aiohttp
import time

# Адреса смарт-контрактов токенов в сети Solana
INPUT_MINT = "So11111111111111111111111111111111111111112" # SOL
OUTPUT_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v" # USDC
AMOUNT_IN = 1000000000 # Тестовый объем: 1 SOL (в лампортах, 1 SOL = 10^9 lamports)
SLIPPAGE_BPS = 50 # Допустимое проскальзывание 0.5%

async def fetch_quote(session, input_mint, output_mint, amount):
    """Отправляет запрос к Jupiter API для получения котировки обмена"""
    url = f"https://quote-api.jup.ag/v6/quote?inputMint={input_mint}&outputMint={output_mint}&amount={amount}&slippageBps={SLIPPAGE_BPS}"
    
    async with session.get(url) as response:
        if response.status == 200:
            return await response.json()
        return None

async def arbitrage_worker():
    """Бесконечный цикл сканирования DEX на наличие арбитражных окон"""
    print("🤖 Zer0Life Arbitrage Worker запущен и сканирует пулы ликвидности...")
    
    # Создаем постоянную сессию для быстрых запросов без переподключения
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                # Шаг 1: Узнаем, сколько USDC мы получим за 1 SOL
                quote_forward = await fetch_quote(session, INPUT_MINT, OUTPUT_MINT, AMOUNT_IN)
                
                if quote_forward:
                    usdc_amount = quote_forward['outAmount']
                    
                    # Шаг 2: Узнаем, сколько SOL мы получим обратно за эти USDC
                    quote_backward = await fetch_quote(session, OUTPUT_MINT, INPUT_MINT, usdc_amount)
                    
                    if quote_backward:
                        final_sol = int(quote_backward['outAmount'])
                        
                        # Высчитываем чистую разницу (Спред)
                        profit = final_sol - AMOUNT_IN
                        
                        if profit > 0:
                            print(f"✅ НАЙДЕН АРБИТРАЖ! Вход: 1 SOL | Выход: {final_sol / 10**9:.5f} SOL | Профит: +{profit / 10**9:.5f} SOL")
                            # В будущем здесь мы будем вызывать функцию отправки транзакции
                        else:
                            # Закомментируйте строку ниже, если не хотите, чтобы консоль засорялась при отсутствии профита
                            pass 
                            
            except Exception as e:
                print(f"⚠️ Ошибка сканера DEX: {e}")
            
            # Обязательная пауза (2 секунды), чтобы API Jupiter не заблокировал нашего бота за спам
            await asyncio.sleep(2)
