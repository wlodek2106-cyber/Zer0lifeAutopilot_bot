"""Transparent rules-based signals; this module does not place orders."""
from dataclasses import dataclass
from .indicators import ema, rsi


@dataclass(frozen=True)
class Signal:
    action: str
    confidence: float
    price: float
    reasons: tuple


def analyze(closes):
    if not isinstance(closes, (list, tuple)) or len(closes) < 30:
        raise ValueError("At least 30 closing prices are required.")
    price = float(closes[-1])
    fast = ema(closes, 9)
    slow = ema(closes, 21)
    momentum = rsi(closes, 14)
    if fast > slow and 45.0 <= momentum <= 68.0:
        strength = min(0.85, 0.55 + (fast / slow - 1.0) * 8.0)
        return Signal("BUY", round(strength, 3), price,
                      ("EMA9 above EMA21", "RSI in positive momentum band"))
    if fast < slow and momentum < 45.0:
        return Signal("SELL", 0.65, price,
                      ("EMA9 below EMA21", "RSI indicates weak momentum"))
    return Signal("HOLD", 0.5, price, ("No complete entry or exit condition",))
