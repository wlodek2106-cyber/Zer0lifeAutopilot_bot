"""Small dependency-free market indicators with input validation."""
from math import isfinite


def _series(values):
    if not isinstance(values, (list, tuple)) or len(values) < 2:
        raise ValueError("At least two prices are required.")
    if any(type(value) not in (int, float) for value in values):
        raise ValueError("Prices must be numeric values, not booleans or strings.")
    try:
        result = [float(value) for value in values]
    except (OverflowError, TypeError, ValueError) as exc:
        raise ValueError("Prices must be finite positive numbers.") from exc
    if any(not isfinite(value) or value <= 0 for value in result):
        raise ValueError("Prices must be finite positive numbers.")
    return result


def ema(values, period):
    data = _series(values)
    if type(period) is not int or period < 2 or len(data) < period:
        raise ValueError("EMA period must be >= 2 and available in the series.")
    alpha = 2.0 / (period + 1.0)
    current = sum(data[:period]) / period
    for value in data[period:]:
        current = alpha * value + (1.0 - alpha) * current
    if not isfinite(current):
        raise ValueError("EMA calculation is not finite.")
    return current


def rsi(values, period=14):
    data = _series(values)
    if type(period) is not int or period < 2 or len(data) <= period:
        raise ValueError("RSI needs period + 1 prices.")
    gains, losses = [], []
    for previous, current in zip(data[:-1], data[1:]):
        change = current - previous
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for gain, loss in zip(gains[period:], losses[period:]):
        avg_gain = ((period - 1) * avg_gain + gain) / period
        avg_loss = ((period - 1) * avg_loss + loss) / period
    if not isfinite(avg_gain) or not isfinite(avg_loss):
        raise ValueError("RSI calculation is not finite.")
    if avg_loss == 0:
        return 100.0 if avg_gain else 50.0
    ratio = avg_gain / avg_loss
    if not isfinite(ratio):
        return 100.0
    return 100.0 - 100.0 / (1.0 + ratio)
