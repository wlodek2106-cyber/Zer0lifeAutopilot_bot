"""Numeric, public DEX market aggregates used as context for the USDC strategy."""
import math

import aiohttp


MIN_LIQUIDITY_USD = 100_000
MIN_VOLUME_24H_USD = 10_000
CHANGE_FIELDS = {
    "m5": "change_m5_pct",
    "h1": "change_h1_pct",
    "h6": "change_h6_pct",
    "h24": "change_h24_pct",
}
TRANSACTION_WINDOWS = ("m5", "h1")


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


def context_from_pairs(pairs, watchlist, permitted_symbols=None):
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
        return context_from_pairs(await response.json(), watchlist, permitted_symbols)
