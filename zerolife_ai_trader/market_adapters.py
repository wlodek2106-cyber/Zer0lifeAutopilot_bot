"""Read-only market-data adapters for the multi-market technical agent."""
import re
import time

import aiohttp

from chart_analysis import parse_ohlcv
from .technical_agent import analyze_market
from .binance_public import fetch_public_klines, INTERVALS as BINANCE_INTERVALS
import geckoterminal_client as gecko

SOLANA_POOL = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
SOLANA_FRAMES = {
    "5m": ("minute", 5, 300),
    "15m": ("minute", 15, 900),
    "1h": ("hour", 1, 3600),
    "4h": ("hour", 4, 14400),
    "1d": ("day", 1, 86400),
}


async def analyze_binance(symbol, timeframes, *, session=None, now=None):
    now = time.time() if now is None else float(now)
    if not isinstance(timeframes, (list, tuple)) or not timeframes:
        raise ValueError("Select at least one timeframe.")
    if len(set(timeframes)) != len(timeframes) or any(t not in BINANCE_INTERVALS for t in timeframes):
        raise ValueError("Unsupported or duplicate Binance timeframe.")
    data = {}
    for timeframe in timeframes:
        result = await fetch_public_klines(symbol, timeframe, limit=100, session=session)
        candles = []
        for item in result["candles"]:
            ts = item["timestamp_ms"] / 1000
            if ts + BINANCE_INTERVALS[timeframe] / 1000 <= now - 1:
                candles.append({
                    "timestamp": ts, "open": item["open"], "high": item["high"],
                    "low": item["low"], "close": item["close"], "volume": item["volume"],
                })
        data[timeframe] = candles
    return analyze_market("binance", symbol, data, now=now)


async def analyze_solana_pool(pool_address, timeframes, *, session=None, now=None):
    now = time.time() if now is None else float(now)
    if not isinstance(pool_address, str) or not SOLANA_POOL.fullmatch(pool_address):
        raise ValueError("Enter a valid Solana pool address (not a token ticker).")
    if not isinstance(timeframes, (list, tuple)) or not timeframes:
        raise ValueError("Select at least one timeframe.")
    if len(set(timeframes)) != len(timeframes) or any(t not in SOLANA_FRAMES for t in timeframes):
        raise ValueError("Unsupported or duplicate Solana timeframe.")
    owned = session is None
    client = session or aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=12),
        headers={"Accept": "application/json", "User-Agent": "Zer0Life-Technical-Agent/1.0"},
    )
    try:
        data = {}
        for timeframe in timeframes:
            unit, aggregate, seconds = SOLANA_FRAMES[timeframe]
            path = f"/networks/solana/pools/{pool_address}/ohlcv/{unit}"
            async with gecko.request(
                client, path, priority=gecko.CONTEXT,
                params={"aggregate": aggregate, "limit": 100, "currency": "usd"},
                timeout=aiohttp.ClientTimeout(total=12),
            ) as response:
                if response.status != 200:
                    raise RuntimeError("GeckoTerminal OHLCV request failed.")
                payload = await response.json()
            candles = parse_ohlcv(payload, seconds, now=now)
            data[timeframe] = candles
        return analyze_market("solana", pool_address, data, now=now)
    finally:
        if owned:
            await client.close()
