import asyncio
import aiohttp
import logging
from datetime import datetime

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Официальные mint-адреса токенов в сети Solana
TOKENS = {
    "SOL": "So11111111111111111111111111111111111111112",
    "USDC": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
}

class Web4AITraderCore:
    def __init__(self):
        self.session = None

    async def init_session(self):
        if not self.session or self.session.closed:
            self.session = aiohttp.ClientSession()

    async def get_dex_quote(self, input_mint: str, output_mint: str, amount_lamports: int, slippage_bps: int = 50):
        """
        Запрос реальной котировки и маршрута через Jupiter Swap API v6
        """
        await self.init_session()
        url = f"https://api.jup.ag/swap/v1/quote?inputMint={input_mint}&outputMint={output_mint}&amount={amount_lamports}&slippageBps={slippage_bps}"
        
        try:
            async with self.session.get(url, timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return {
                        "success": True,
                        "inAmount": data.get("inAmount"),
                        "outAmount": data.get("outAmount"),
                        "priceImpactPct": data.get("priceImpactPct"),
                        "routePlan": len(data.get("routePlan", []))
                    }
                else:
                    error_text = await resp.text()
                    return {"success": False, "error": error_text}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def scan_market_loop(self):
        """
        Фоновый цикл автономного сканирования рыночной ликвидности 24/7/365
        """
        logging.info("⚡ Web4 AI-Trader: Цикл сканирования рынка запущен.")
        while True:
            # Проверяем ликвидность 1 SOL -> USDC через Jupiter
            result = await self.get_dex_quote(
                input_mint=TOKENS["SOL"],
                output_mint=TOKENS["USDC"],
                amount_lamports=1000000000 # 1 SOL
            )
            
            timestamp = datetime.now().strftime('%H:%M:%S')
            if result["success"]:
                logging.info(f"[{timestamp}] [MARKET SCAN] SOL/USDC | Выход: {result['outAmount']} микро-USDC | Маршрутов: {result['routePlan']}")
            else:
                logging.warning(f"[{timestamp}] [MARKET SCAN ERROR] {result['error']}")
                
            # Интервал между сканированиями для соблюдения лимитов API и анализа
            await asyncio.sleep(15)

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()

if __name__ == "__main__":
    trader = Web4AITraderCore()
    try:
        asyncio.run(trader.scan_market_loop())
    except KeyboardInterrupt:
        logging.info("⏹️ Агент остановлен пользователем.")
