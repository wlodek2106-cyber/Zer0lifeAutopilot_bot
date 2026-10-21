"""Numeric, public DEX market aggregates used as context for the USDC strategy."""
import asyncio
import math
import re
import time

import aiohttp

import chart_analysis


MIN_LIQUIDITY_USD = 100_000
MIN_VOLUME_24H_USD = 10_000
CHANGE_FIELDS = {
    "m5": "change_m5_pct",
    "h1": "change_h1_pct",
    "h6": "change_h6_pct",
    "h24": "change_h24_pct",
}
TRANSACTION_WINDOWS = ("m5", "h1")
_POOL_ADDRESS = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
_CANDLE_CACHE = {}
_CANDLE_LOCKS = {}
_CANDLE_SEMAPHORES = {}
_CANDLE_TTL = {"m5": 180, "h1": 900}
_CANDLE_CONFIG = {
    "m5": ("minute", 5, 300),
    "h1": ("hour", 1, 3600),
}


def _finite_number(value):
    if type(value) not in (int, float):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _finite_price(value):
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def context_from_pairs(pairs, watchlist, permitted_symbols=None, *, include_pool_address=False):
    """Pick the deepest eligible pool for each permitted token and keep numeric metrics only."""
    if not isinstance(pairs, list):
        return {}
    allowed = set(permitted_symbols if permitted_symbols is not None else watchlist)
    symbols_by_mint = {
        details.get("mint"): symbol
        for symbol, details in watchlist.items()
        if symbol in allowed and isinstance(details, dict) and isinstance(details.get("mint"), str)
    }
    selected = {}
    for pair in pairs:
        if not isinstance(pair, dict) or pair.get("chainId") != "solana":
            continue
        base = pair.get("baseToken")
        symbol = symbols_by_mint.get(base.get("address")) if isinstance(base, dict) else None
        if not symbol:
            continue
        price = _finite_price(pair.get("priceUsd"))
        liquidity_data = pair.get("liquidity")
        volume_data = pair.get("volume")
        liquidity = _finite_number(liquidity_data.get("usd")) if isinstance(liquidity_data, dict) else None
        volume = _finite_number(volume_data.get("h24")) if isinstance(volume_data, dict) else None
        if (price is None or price <= 0 or liquidity is None or liquidity < MIN_LIQUIDITY_USD
                or volume is None or volume < MIN_VOLUME_24H_USD
                or liquidity <= selected.get(symbol, {}).get("_liquidity", -1)):
            continue

        metrics = {"liquidity_usd": liquidity, "volume_24h_usd": volume, "_liquidity": liquidity}
        pool_address = pair.get("pairAddress")
        if include_pool_address and isinstance(pool_address, str):
            metrics["_pool_address"] = pool_address
        changes = pair.get("priceChange")
        if isinstance(changes, dict):
            for window, field in CHANGE_FIELDS.items():
                value = _finite_number(changes.get(window))
                if value is not None:
                    metrics[field] = value

        transactions = pair.get("txns")
        if isinstance(transactions, dict):
            for window in TRANSACTION_WINDOWS:
                counts = transactions.get(window)
                if not isinstance(counts, dict):
                    continue
                for direction in ("buys", "sells"):
                    value = counts.get(direction)
                    if type(value) is int and 0 <= value <= 1_000_000_000:
                        metrics[f"{direction}_{window}"] = value
        selected[symbol] = metrics

    return {
        symbol: {key: value for key, value in metrics.items() if key != "_liquidity"}
        for symbol, metrics in selected.items()
    }


async def _get_candles(session, pool_address, timeframe):
    if not isinstance(pool_address, str) or not _POOL_ADDRESS.fullmatch(pool_address):
        return [], True
    timeframe_name, aggregate, interval = _CANDLE_CONFIG[timeframe]
    key = (pool_address, timeframe)
    now = time.time()
    cached = _CANDLE_CACHE.get(key)
    if cached and now - cached["fetched_at"] <= _CANDLE_TTL[timeframe]:
        return cached["candles"], False

    loop_id = id(asyncio.get_running_loop())
    lock = _CANDLE_LOCKS.setdefault((loop_id, key), asyncio.Lock())
    async with lock:
        now = time.time()
        cached = _CANDLE_CACHE.get(key)
        if cached and now - cached["fetched_at"] <= _CANDLE_TTL[timeframe]:
            return cached["candles"], False
        url = (f"https://api.geckoterminal.com/api/v2/networks/solana/pools/"
               f"{pool_address}/ohlcv/{timeframe_name}")
        try:
            semaphore = _CANDLE_SEMAPHORES.setdefault(loop_id, asyncio.Semaphore(2))
            async with semaphore:
                async with session.get(
                    url,
                    params={"aggregate": aggregate, "limit": 100, "currency": "usd"},
                    headers={"Accept": "application/json", "User-Agent": "Zer0Life-AI-Trader"},
                    timeout=aiohttp.ClientTimeout(total=8),
                ) as response:
                    if response.status != 200:
                        raise ValueError("OHLCV provider returned a non-success status")
                    payload = await response.json()
            candles = chart_analysis.parse_ohlcv(payload, interval, now=now)
            if not candles:
                raise ValueError("OHLCV provider returned no valid closed candles")
            _CANDLE_CACHE[key] = {"candles": candles, "fetched_at": now}
            if len(_CANDLE_CACHE) > 400:
                cutoff = now - 7200
                for cache_key, entry in list(_CANDLE_CACHE.items()):
                    if entry["fetched_at"] < cutoff:
                        _CANDLE_CACHE.pop(cache_key, None)
            return candles, False
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, TypeError):
            if cached:
                return cached["candles"], True
            return [], True


async def _add_chart_context(session, context):
    async def enrich(metrics):
        pool_address = metrics.get("_pool_address")
        m5, h1 = await asyncio.gather(
            _get_candles(session, pool_address, "m5"),
            _get_candles(session, pool_address, "h1"),
        )
        metrics.update(chart_analysis.analyze_charts(
            m5[0], h1[0], now=time.time(), m5_stale=m5[1], h1_stale=h1[1]))
        metrics.pop("_pool_address", None)

    await asyncio.gather(*(enrich(metrics) for metrics in context.values()))


async def fetch_main_market_context(session, watchlist, permitted_symbols):
    mints = [
        watchlist[symbol]["mint"]
        for symbol in permitted_symbols
        if symbol in watchlist and isinstance(watchlist[symbol].get("mint"), str)
    ]
    if not mints:
        return {}
    url = "https://api.dexscreener.com/tokens/v1/solana/" + ",".join(mints)
    async with session.get(
        url,
        headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
        timeout=aiohttp.ClientTimeout(total=8),
    ) as response:
        if response.status != 200:
            return {}
        context = context_from_pairs(
            await response.json(), watchlist, permitted_symbols, include_pool_address=True)
    await _add_chart_context(session, context)
    return context
