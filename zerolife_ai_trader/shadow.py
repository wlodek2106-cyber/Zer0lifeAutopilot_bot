"""Read-only shadow evaluator for existing Zer0Life market-context feeds.

Consumes already-fetched numeric context. Never fetches data, creates orders,
touches custody, or calls a wallet/signer. Not wired into production execution.
"""
from dataclasses import asdict
from math import isfinite

from .risk import RiskLimits, assess_order


def evaluate_context(context, *, equity=1000.0, cash=1000.0,
                     open_positions=0, daily_pnl=0.0, limits=None):
    """Return shadow-only PAPER_BUY/HOLD results from market-context records."""
    if not isinstance(context, dict):
        return []
    results = []
    for symbol, metrics in context.items():
        if not isinstance(symbol, str) or not isinstance(metrics, dict):
            continue
        price = metrics.get("chart_price_usd")
        change_m5 = metrics.get("change_m5_pct")
        change_h1 = metrics.get("change_h1_pct")
        trend = metrics.get("chart_trend_h1")
        chart_ready = metrics.get("chart_ready")
        buy_setup = metrics.get("chart_buy_setup")
        candle_age = metrics.get("chart_m5_age_seconds")
        valid = (
            type(price) in (int, float) and isfinite(price) and price > 0
            and type(change_m5) in (int, float) and isfinite(change_m5)
            and type(change_h1) in (int, float) and isfinite(change_h1)
            and trend in (0, 1) and chart_ready == 1 and buy_setup == 1
            and type(candle_age) in (int, float) and isfinite(candle_age)
            and 0 <= candle_age <= 600
            and change_m5 > 0 and change_h1 >= 0
        )
        if not valid:
            results.append({
                "symbol": symbol,
                "action": "HOLD",
                "price": price if type(price) in (int, float) else None,
                "reason": "Missing, stale, or non-confirming chart evidence; fail closed.",
                "risk": {"allowed": False, "quantity": 0.0,
                         "reason": "No validated entry signal."},
            })
            continue
        confidence = min(0.9, 0.6 + min(change_m5, 3.0) / 30.0
                         + min(change_h1, 5.0) / 100.0)
        risk = assess_order(
            equity=equity, cash=cash, price=float(price), confidence=confidence,
            open_positions=open_positions, daily_pnl=daily_pnl,
            limits=limits or RiskLimits(),
        )
        results.append({
            "symbol": symbol,
            "action": "PAPER_BUY" if risk.allowed else "HOLD",
            "price": float(price),
            "reason": "Existing market-context chart gates passed; shadow evaluation only.",
            "risk": asdict(risk),
        })
    return results
