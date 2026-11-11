"""Selectable, deterministic signal strategies for research and paper mode."""
from dataclasses import asdict
from math import isfinite

from .indicators import ema, rsi
from .strategy import Signal


def analyze_with_strategy(closes, strategy="ema_rsi"):
    if strategy not in {"ema_rsi", "trend_following", "mean_reversion", "manual"}:
        raise ValueError("Unsupported strategy.")
    if not isinstance(closes, (list, tuple)) or len(closes) < 30:
        raise ValueError("At least 30 closing prices are required.")
    if any(type(value) not in (int, float) or not isfinite(value) or value <= 0
           for value in closes):
        raise ValueError("Closing prices must be finite positive numbers.")
    price = float(closes[-1])
    fast = ema(closes, 9)
    slow = ema(closes, 21)
    momentum = rsi(closes, 14)

    if strategy == "manual":
        return Signal("HOLD", 0.0, price, ("Manual-only mode; no automatic signal.",))

    if strategy in {"ema_rsi", "trend_following"}:
        if fast > slow and (strategy == "trend_following" or 45 <= momentum <= 68):
            confidence = min(0.85, 0.55 + (fast / slow - 1.0) * 8.0)
            return Signal("BUY", round(confidence, 3), price,
                          ("EMA9 above EMA21", "Trend confirmation"))
        if fast < slow and (strategy == "trend_following" or momentum < 45):
            return Signal("SELL", 0.65, price,
                          ("EMA9 below EMA21", "Downtrend confirmation"))
        return Signal("HOLD", 0.5, price, ("No confirmed trend entry.",))

    # Mean-reversion research signal: only act at stretched RSI extremes.
    if momentum <= 30:
        return Signal("BUY", 0.65, price, ("RSI oversold (<=30)", "Mean-reversion watch"))
    if momentum >= 70:
        return Signal("SELL", 0.65, price, ("RSI overbought (>=70)", "Mean-reversion watch"))
    return Signal("HOLD", 0.5, price, ("RSI is inside the neutral band.",))


def evaluate_watchlist(price_series, settings, *, equity=None, cash=None,
                       open_positions=0, daily_pnl=0.0):
    """Return signals plus risk decisions. Never submits orders or accesses wallets."""
    from .configuration import build_risk_limits
    from .risk import assess_order

    if not isinstance(price_series, dict):
        raise ValueError("Price series must be a symbol-to-closes mapping.")
    limits = build_risk_limits(settings)
    capital = settings["capital"] if equity is None else equity
    available_cash = settings["capital"] if cash is None else cash
    output = []
    for symbol in settings["symbols"].split(","):
        closes = price_series.get(symbol)
        if closes is None:
            output.append({"symbol": symbol, "action": "HOLD", "confidence": 0.0,
                           "price": None, "reasons": ["No price series available."],
                           "risk": {"allowed": False, "quantity": 0.0,
                                    "reason": "Market data unavailable."}})
            continue
        signal = analyze_with_strategy(closes, settings["strategy"])
        if signal.action == "BUY":
            risk = assess_order(
                equity=capital, cash=available_cash, price=signal.price,
                confidence=signal.confidence, open_positions=open_positions,
                daily_pnl=daily_pnl, limits=limits,
            )
            action = "PAPER_BUY" if risk.allowed else "HOLD"
            reason = risk.reason
            quantity = risk.quantity
        else:
            risk = None
            action = signal.action
            reason = "Research signal only; no execution."
            quantity = 0.0
        output.append({
            "symbol": symbol, "action": action, "confidence": signal.confidence,
            "price": signal.price, "reasons": list(signal.reasons),
            "risk": ({"allowed": risk.allowed, "quantity": risk.quantity,
                      "reason": risk.reason} if risk else
                     {"allowed": False, "quantity": 0.0, "reason": reason}),
        })
    return output
