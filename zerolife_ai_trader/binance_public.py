"""Read-only Binance Spot public candle client for research and paper mode."""
import asyncio
import aiohttp
import math
from datetime import datetime, timezone

from .market_data import validate_candles

BASE_URL = "https://api.binance.com"
ALLOWED_SYMBOLS = {"SOLUSDC", "SOLUSDT", "JUPUSDC", "RAYUSDC"}
INTERVALS = {
    "1m": 60_000, "5m": 300_000, "15m": 900_000,
    "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000,
}


async def fetch_public_klines(symbol, interval, *, limit=500, session=None):
    """Fetch and validate public OHLCV candles. No credentials or trading endpoints."""
    if symbol not in ALLOWED_SYMBOLS:
        raise ValueError("Unsupported market symbol.")
    if interval not in INTERVALS:
        raise ValueError("Unsupported candle interval.")
    if type(limit) is not int or not 30 <= limit <= 1000:
        raise ValueError("Candle limit must be an integer from 30 to 1000.")
    owned_session = session is None
    client = session or aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=12),
        headers={"Accept": "application/json", "User-Agent": "Zer0Life-AI-Trader-Research/1.0"},
    )
    try:
        async with client.get(
            BASE_URL + "/api/v3/klines",
            params={"symbol": symbol, "interval": interval, "limit": limit},
            allow_redirects=False,
        ) as response:
            if response.status != 200:
                raise RuntimeError("Binance public market-data request failed.")
            payload = await response.json()
        if not isinstance(payload, list) or not 30 <= len(payload) <= limit:
            raise ValueError("Binance returned an incomplete candle series.")
        candles = []
        for row in payload:
            if not isinstance(row, list) or len(row) < 6:
                raise ValueError("Binance returned a malformed candle.")
            try:
                candles.append({
                    "timestamp_ms": int(row[0]),
                    "open": float(row[1]),
                    "high": float(row[2]),
                    "low": float(row[3]),
                    "close": float(row[4]),
                    "volume": float(row[5]),
                })
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError("Binance returned invalid candle values.") from exc
        return validate_candles(candles, interval_ms=INTERVALS[interval])
    except asyncio.TimeoutError as exc:
        raise RuntimeError("Binance public market-data request timed out.") from exc
    finally:
        if owned_session:
            await client.close()



async def fetch_public_price(symbol, *, session=None):
    """Fetch one validated public Spot price for paper valuation only."""
    if symbol not in ALLOWED_SYMBOLS:
        raise ValueError("Unsupported market symbol.")
    owned_session = session is None
    client = session or aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=12),
        headers={"Accept": "application/json", "User-Agent": "Zer0Life-AI-Trader-Research/1.0"},
    )
    try:
        async with client.get(
            BASE_URL + "/api/v3/ticker/price",
            params={"symbol": symbol},
            allow_redirects=False,
        ) as response:
            if response.status != 200:
                raise RuntimeError("Binance public price request failed.")
            payload = await response.json()
        if not isinstance(payload, dict) or payload.get("symbol") != symbol:
            raise ValueError("Binance returned a mismatched price response.")
        raw_price = payload.get("price")
        try:
            price = float(raw_price)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("Binance returned an invalid market price.") from exc
        if not math.isfinite(price) or price <= 0:
            raise ValueError("Binance returned an invalid market price.")
        return {
            "symbol": symbol,
            "price": price,
            "source": "binance_spot_public",
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }
    except asyncio.TimeoutError as exc:
        raise RuntimeError("Binance public price request timed out.") from exc
    finally:
        if owned_session:
            await client.close()
