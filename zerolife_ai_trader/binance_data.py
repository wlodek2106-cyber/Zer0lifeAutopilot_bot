"""Offline parser for public Binance Spot kline payloads.

This module performs no HTTP requests and needs no API key. Fetching is kept
outside the research core; callers must pass the raw public response payload.
"""
from .market_data import validate_candles

_BINANCE_INTERVALS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000,
    "30m": 1_800_000, "1h": 3_600_000, "2h": 7_200_000,
    "4h": 14_400_000, "6h": 21_600_000, "8h": 28_800_000,
    "12h": 43_200_000, "1d": 86_400_000,
}


def parse_binance_klines(payload, *, interval, anchor_ms=0,
                         reject_gaps=True):
    """Convert Binance Spot /api/v3/klines rows to validated OHLCV candles."""
    if interval not in _BINANCE_INTERVALS:
        raise ValueError("Unsupported Binance interval.")
    if not isinstance(payload, list) or not payload:
        raise ValueError("Binance kline payload must be a non-empty list.")

    candles = []
    for index, row in enumerate(payload):
        if not isinstance(row, (list, tuple)) or len(row) != 12:
            raise ValueError(f"Binance kline row {index} must contain 12 fields.")
        try:
            timestamp = row[0]
            if type(timestamp) is not int or timestamp < 0:
                raise ValueError("Open time must be a non-negative integer.")
            candle = {
                "timestamp_ms": timestamp,
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
                "volume": float(row[5]),
            }
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"Invalid Binance kline row {index}: {exc}") from exc
        candles.append(candle)

    report = validate_candles(
        candles, interval_ms=_BINANCE_INTERVALS[interval],
        anchor_ms=anchor_ms, reject_gaps=reject_gaps,
    )
    report["source"] = "binance_spot_public_klines"
    report["interval"] = interval
    return report
