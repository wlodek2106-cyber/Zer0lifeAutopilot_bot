import asyncio
import aiohttp

# Конфигурация активов из вашего пула
WATCHLIST = {
    "SOL":  {"mint": "So11111111111111111111111111111111111111112", "decimals": 9},
    "JUP":  {"mint": "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN", "decimals": 6},
    "RAY":  {"mint": "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R", "decimals": 6},
    "WIF":  {"mint": "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm", "decimals": 6},
    "PYTH": {"mint": "HZ1JovNiDcZvKhgWNgQX5Z59C3b22DoPtRUC544HLD37", "decimals": 6}
}

USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SLIPPAGE_BPS = 40  # 0.40% проскальзывание

async def fetch_quote(session, input_mint, output_mint, amount):
    url = f"https://quote-api.jup.ag/v6/quote?inputMint={input_mint}&outputMint={output_mint}&amount={amount}&slippageBps={SLIPPAGE_BPS}"
    async with session.get(url, headers={"User-Agent": "Mozilla/5.0"}) as response:
        if response.status == 200:
            return await response.json()
        return None

async def multi_asset_arbitrage_worker():
    print("🤖 Multi-Asset Arbitrage Worker запущен для SOL, JUP, RAY, WIF, PYTH...")
    
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                for sym, info in WATCHLIST.items():
                    token_mint = info["mint"]
                    decimals = info["decimals"]
                    
                    # Тестовый объем входа (например, эквивалент ~$20 в базовых единицах токена)
                    test_amount_in = int(10 ** decimals) # 1 целая монета для теста
                    
                    # Шаг 1: Покупка/Обмен токена на USDC
                    quote_to_usdc = await fetch_quote(session, token_mint, USDC_MINT, test_amount_in)
                    if not quote_to_usdc:
                        continue
                        
                    usdc_out = int(quote_to_usdc['outAmount'])
                    
                    # Шаг 2: Обратный обмен USDC обратно в исходный токен
                    quote_back = await fetch_quote(session, USDC_MINT, token_mint, usdc_out)
                    if not quote_back:
                        continue
                        
                    final_amount = int(quote_back['outAmount'])
                    profit = final_amount - test_amount_in
                    
                    if profit > 0:
                        print(f"✅ НАЙДЕН СПРЕД [{sym}]: Профит +{profit / 10**decimals:.5f} {sym}")
                    
                    # Паужа между запросами по токенам во избежание лимитов API
                    await asyncio.sleep(1)
                    
            except Exception as e:
                print(f"⚠️ Ошибка в мульти-ассетном воркере: {e}")
            
            await asyncio.sleep(5)
