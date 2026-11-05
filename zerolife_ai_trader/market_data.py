"""Strict OHLCV candle validation for historical research data.

Timestamps are UTC Unix milliseconds marking candle open. Missing intervals are
reported and rejected by default so a backtest cannot silently bridge data gaps.
"""
from math import isfinite

_FIELDS = ("timestamp_ms", "open", "high", "low", "close", "volume")


def validate_candles(candles, *, interval_ms, reject_gaps=True):
    if type(interval_ms) is not int or interval_ms <= 0:
        raise ValueError("interval_ms must be a positive integer.")
    if type(reject_gaps) is not bool:
        raise ValueError("reject_gaps must be boolean.")
    if not isinstance(candles, (list, tuple)) or not candles:
        raise ValueError("At least one candle is required.")

    normalized = []
    gaps = []
    previous_timestamp = None
    for index, candle in enumerate(candles):
        if not isinstance(candle, dict) or any(key not in candle for key in _FIELDS):
            raise ValueError(f"Candle {index} is missing required OHLCV fields.")
        timestamp = candle["timestamp_ms"]
        if type(timestamp) is not int or timestamp < 0:
            raise ValueError(f"Candle {index} has an invalid timestamp.")
        if previous_timestamp is not None:
            delta = timestamp - previous_timestamp
            if delta <= 0:
                raise ValueError("Candle timestamps must be strictly increasing.")
            if delta != interval_ms:
                if delta % interval_ms:
                    raise ValueError("Candle timestamps are not aligned to interval.")
                gaps.append({"after_index": index - 1,
                             "missing_intervals": delta // interval_ms - 1})
        values = {}
        for key in ("open", "high", "low", "close", "volume"):
            value = candle[key]
            if type(value) not in (int, float) or not isfinite(value):
                raise ValueError(f"Candle {index} has invalid {key}.")
            value = float(value)
            if key == "volume":
                if value < 0:
                    raise ValueError("Candle volume cannot be negative.")
            elif value <= 0:
                raise ValueError(f"Candle {key} must be positive.")
            values[key] = value
        if values["high"] < max(values["open"], values["close"], values["low"]):
            raise ValueError(f"Candle {index} has an inconsistent high.")
        if values["low"] > min(values["open"], values["close"], values["high"]):
            raise ValueError(f"Candle {index} has an inconsistent low.")
        normalized.append({"timestamp_ms": timestamp, **values})
        previous_timestamp = timestamp

    if gaps and reject_gaps:
        raise ValueError(f"Historical data contains {len(gaps)} timestamp gaps.")
    return {
        "candles": normalized,
        "count": len(normalized),
        "interval_ms": interval_ms,
        "gaps": gaps,
        "gap_count": len(gaps),
        "first_timestamp_ms": normalized[0]["timestamp_ms"],
        "last_timestamp_ms": normalized[-1]["timestamp_ms"],
        "quality": "complete" if not gaps else "gaps_detected",
    }
