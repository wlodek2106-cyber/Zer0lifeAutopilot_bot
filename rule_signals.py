"""Deterministic, no-paid-AI entry signals using the server-owned chart policy."""
import math

import execution_policy


ENGINE_ID = "rules-v1"


def _number(value):
    if type(value) not in (int, float) or not math.isfinite(value):
        return 0.0
    return float(value)


def _entry_check(mode, symbol, contexts, prices, screened_symbols):
    if not execution_policy.positive(prices.get(symbol)):
        return execution_policy.deny(
            "rule_price", "HOLD: no fresh numeric price is available.")
    if mode in ("sniper", "investor") and symbol not in screened_symbols:
        return execution_policy.deny(
            "rule_screen", "HOLD: token is not on the current verified shortlist.")
    chart = contexts["main"] if mode == "main" else contexts["sniper"]
    if mode == "main":
        return execution_policy.core_entry_signal(symbol, chart)
    if mode == "sniper":
        return execution_policy.sniper_entry_signal(symbol, chart)
    return execution_policy.investor_entry_signal(
        symbol, chart, prices.get(symbol))


def _rank(symbol, context, screening):
    metrics = context.get(symbol, {})
    screened = screening.get(symbol, {})
    liquidity = _number(screened.get(
        "liquidity_usd", metrics.get("liquidity_usd")))
    volume = _number(screened.get(
        "volume_24h_usd", metrics.get("volume_24h_usd")))
    momentum = _number(metrics.get("change_m5_pct"))
    # Prefer deeper, more active markets; use lower short-term extension only
    # as a tie-breaker. This is ranking, not a guarantee of execution quality.
    return (liquidity, volume, -momentum, symbol)


def _reason(mode, symbol, context):
    metrics = context.get(symbol, {})
    m5 = _number(metrics.get("change_m5_pct"))
    h1 = _number(metrics.get("change_h1_pct"))
    trend = "rising" if metrics.get("chart_trend_h1") == 1 else "flat"
    if mode == "investor":
        return (f"Rule BUY: fresh closed green 5m candle; 5m {m5:.2f}% and "
                f"1h {h1:.2f}% are positive; 1h trend is rising. No paid AI used.")
    return (f"Rule BUY: fresh closed green 5m candle; 5m {m5:.2f}%; "
            f"1h {h1:.2f}% and {trend}; within the existing extension limits. "
            "No paid AI used.")


def build_decisions(universes, main_context, sniper_context, prices, screening,
                    screened_symbols):
    """Choose at most one rules-qualified candidate per enabled strategy."""
    contexts = {"main": main_context or {}, "sniper": sniper_context or {}}
    decisions = {}
    for mode in universes:
        if mode not in ("main", "sniper", "investor"):
            continue
        symbols = universes.get(mode, [])
        candidates = []
        reasons = []
        for symbol in symbols:
            verdict = _entry_check(
                mode, symbol, contexts, prices, screened_symbols)
            if verdict.allowed:
                chart = contexts["main"] if mode == "main" else contexts["sniper"]
                candidates.append((symbol, chart))
            elif len(reasons) < 3:
                reasons.append(verdict.reason)
        if candidates:
            chart = contexts["main"] if mode == "main" else contexts["sniper"]
            candidates.sort(
                key=lambda item: _rank(item[0], chart, screening),
                reverse=True)
            symbol, _ = candidates[0]
            decisions[mode] = {
                "action": "BUY", "symbol": symbol,
                "reason": _reason(mode, symbol, chart)[:350],
            }
        else:
            detail = (" First checks: " + " | ".join(reasons)) if reasons else ""
            decisions[mode] = {
                "action": "HOLD", "symbol": None,
                "reason": ("Rule HOLD: no current candidate passed the chart and "
                           "screening rules; no paid AI request was made." + detail)[:350],
            }
    return decisions


def review_decisions(analysis_id, decisions, main_context, sniper_context,
                     screening_symbols, prices, checked_at):
    """Bind a rules-based BUY to the same server-owned entry gates."""
    contexts = {"main": main_context or {}, "sniper": sniper_context or {}}
    verdicts = {}
    for mode, decision in decisions.items():
        if decision.get("action") != "BUY":
            continue
        symbol = decision["symbol"]
        allowed = _entry_check(
            mode, symbol, contexts, prices, screening_symbols).allowed
        metrics = contexts["main" if mode == "main" else "sniper"].get(symbol, {})
        trend = "UP" if metrics.get("chart_trend_h1") == 1 else (
            "RANGE" if metrics.get("chart_trend_h1") == 0 else "UNKNOWN")
        verdicts[mode] = {
            "symbol": symbol,
            "approved": allowed,
            "trend": trend,
            "reason": ("Server chart rules passed; live execution checks still apply."
                       if allowed else "Server chart rules no longer pass; BUY blocked."),
        }
    return {
        "analysis_id": analysis_id,
        "checked_at": checked_at,
        "model": ENGINE_ID,
        "source": ENGINE_ID,
        "verdicts": verdicts,
    }
