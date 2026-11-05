"""Validated per-user configuration for the isolated AI Trader foundation."""
from copy import deepcopy
from math import isfinite

DEFAULT_SETTINGS = {
    "strategy": "ema_rsi", "source": "binance_public",
    "symbols": "SOLUSDC,BTCUSDT,ETHUSDT", "timeframe": "15m",
    "capital": 1000.0, "orderRisk": 2.0, "dailyLoss": 3.0,
    "maxPositions": 5, "confidence": 60.0, "fee": 10, "slippage": 5,
    "notifySignals": True, "notifyRisk": True, "notifyReports": False,
    "paperAutoExecute": False,
}
STRATEGIES = {"ema_rsi", "trend_following", "mean_reversion", "manual"}
SOURCES = {"binance_public", "dexscreener_readonly", "csv"}
TIMEFRAMES = {"1m", "5m", "15m", "1h", "4h", "1d"}
ALLOWED_SYMBOLS = {"SOLUSDC", "BTCUSDT", "ETHUSDT", "SOLUSDT", "JUPUSDC", "RAYUSDC"}


def validate_settings(payload):
    if not isinstance(payload, dict):
        raise ValueError("Settings payload has missing or unsupported fields.")
    legacy_fields = set(DEFAULT_SETTINGS) - {"paperAutoExecute"}
    if set(payload) == legacy_fields:
        payload = {**payload, "paperAutoExecute": False}
    if set(payload) != set(DEFAULT_SETTINGS):
        raise ValueError("Settings payload has missing or unsupported fields.")
    result = deepcopy(payload)
    if result["strategy"] not in STRATEGIES:
        raise ValueError("Unsupported strategy.")
    if result["source"] not in SOURCES:
        raise ValueError("Unsupported market data source.")
    if result["timeframe"] not in TIMEFRAMES:
        raise ValueError("Unsupported timeframe.")
    symbols_value = result["symbols"]
    if not isinstance(symbols_value, str) or not 1 <= len(symbols_value) <= 200:
        raise ValueError("Invalid trading-pair list.")
    symbols = [item.strip().upper() for item in symbols_value.split(",")]
    if not symbols or len(symbols) > 10 or any(item not in ALLOWED_SYMBOLS for item in symbols):
        raise ValueError("Trading-pair list contains an unsupported symbol.")
    result["symbols"] = ",".join(dict.fromkeys(symbols))
    numeric = {
        "capital": (10, 10_000_000), "orderRisk": (0.1, 100),
        "dailyLoss": (0.1, 100), "maxPositions": (1, 100),
        "confidence": (0, 100), "fee": (0, 1000), "slippage": (0, 1000),
    }
    for key, (minimum, maximum) in numeric.items():
        value = result[key]
        if type(value) not in (int, float) or not isfinite(value):
            raise ValueError("Invalid numeric setting: " + key)
        if not minimum <= value <= maximum:
            raise ValueError("Numeric setting is outside allowed bounds: " + key)
    if type(result["maxPositions"]) is not int:
        raise ValueError("maxPositions must be an integer.")
    for key in ("fee", "slippage"):
        if type(result[key]) is not int:
            raise ValueError(key + " must be an integer.")
    for key in ("notifySignals", "notifyRisk", "notifyReports", "paperAutoExecute"):
        if type(result[key]) is not bool:
            raise ValueError("Invalid notification setting: " + key)
    return result
