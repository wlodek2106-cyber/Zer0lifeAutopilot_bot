"""Read-only shadow adapter for the repository's existing Solana chart context.

This module evaluates a secondary signal beside the current bot. It never
changes ai_decisions, execution_policy, custody, balances, or order execution.
"""
from dataclasses import dataclass
from math import isfinite


@dataclass(frozen=True)
class ShadowAssessment:
    action: str
    symbol: str
    price: float | None
    reasons: tuple


def _number(metrics, key):
    value = metrics.get(key)
    if type(value) not in (int, float) or not isfinite(value):
        return None
    return float(value)


def assess_market_context(symbol, metrics):
    """Return BUY_CANDIDATE or HOLD using fields already produced by market_context."""
    if not isinstance(symbol, str) or not symbol or not isinstance(metrics, dict):
        return ShadowAssessment("HOLD", str(symbol or ""), None,
                                ("Missing or invalid market context.",))

    price = _number(metrics, "chart_price_usd")
    if metrics.get("chart_ready") != 1 or metrics.get("chart_buy_setup") != 1:
        return ShadowAssessment("HOLD", symbol, price,
                                ("Existing chart feed is not ready with a confirmed closed green 5m candle.",))

    trend = _number(metrics, "chart_trend_h1")
    change_m5 = _number(metrics, "change_m5_pct")
    change_h1 = _number(metrics, "change_h1_pct")
    ema20_m5 = _number(metrics, "chart_ema20_5m_usd")
    ema50_m5 = _number(metrics, "chart_ema50_5m_usd")
    ema20_h1 = _number(metrics, "chart_ema20_h1_usd")
    ema50_h1 = _number(metrics, "chart_ema50_h1_usd")
    rsi5 = _number(metrics, "chart_rsi14_5m")

    required = (price, trend, change_m5, change_h1, ema20_m5, ema50_m5,
                ema20_h1, ema50_h1, rsi5)
    if any(value is None for value in required) or price <= 0:
        return ShadowAssessment("HOLD", symbol, price,
                                ("Required fresh numeric chart fields are missing or invalid.",))

    if trend < 0 or change_m5 <= 0 or change_h1 < 0:
        return ShadowAssessment("HOLD", symbol, price,
                                ("Hourly trend or short-term momentum is not positive.",))
    if change_m5 > 3 or change_h1 > 10:
        return ShadowAssessment("HOLD", symbol, price,
                                ("Entry is overextended under the existing strategy limits.",))
    if not (ema20_m5 > ema50_m5 and ema20_h1 >= ema50_h1):
        return ShadowAssessment("HOLD", symbol, price,
                                ("EMA20/EMA50 alignment does not confirm the trend.",))
    if not 45 <= rsi5 <= 68:
        return ShadowAssessment("HOLD", symbol, price,
                                ("RSI is outside the shadow entry band.",))

    return ShadowAssessment(
        "BUY_CANDIDATE", symbol, price,
        ("Existing chart feed confirmed a closed green 5m candle.",
         "5m and 1h momentum are positive and within extension limits.",
         "EMA20/EMA50 alignment and RSI confirm the shadow setup.",
         "Shadow-only result: not an order or execution authorization."),
    )
