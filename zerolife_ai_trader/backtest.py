"""Deterministic, long-only historical backtest for supplied close-price candles.

Signals are computed only from prices available at bar close and fills occur
at the following bar close, avoiding same-bar look-ahead. This is research
software, not a forecast or a live execution adapter.
"""
from math import isfinite
from .strategy import analyze


def run_backtest(closes, *, starting_cash=1000.0, fee_bps=10.0,
                 slippage_bps=5.0, order_fraction=0.02,
                 min_confidence=0.60):
    values = (starting_cash, fee_bps, slippage_bps, order_fraction,
              min_confidence)
    if any(type(v) not in (int, float) or not isfinite(v) for v in values):
        raise ValueError("Backtest settings must be finite numeric values.")
    if (starting_cash <= 0 or not 0 <= fee_bps <= 1000
            or not 0 <= slippage_bps <= 1000
            or not 0 < order_fraction <= 1
            or not 0 <= min_confidence <= 1):
        raise ValueError("Backtest settings are outside safe bounds.")
    if not isinstance(closes, (list, tuple)) or len(closes) < 31:
        raise ValueError("At least 31 closing prices are required.")
    if any(type(p) not in (int, float) or not isfinite(p) or p <= 0
           for p in closes):
        raise ValueError("Closes must be finite positive numeric values.")

    cash = float(starting_cash)
    quantity = 0.0
    entry_cost = 0.0
    trades = []
    equity_curve = [cash]
    pending = None
    fee_rate = fee_bps / 10000.0
    slip_rate = slippage_bps / 10000.0

    for index, close in enumerate(closes):
        # Execute the previous bar's signal at this bar's close.
        if pending == "BUY" and quantity == 0:
            budget = min(cash, starting_cash * order_fraction)
            fill = close * (1 + slip_rate)
            qty = budget / (fill * (1 + fee_rate))
            cost = qty * fill
            fee = cost * fee_rate
            total = cost + fee
            if qty > 0 and total <= cash and all(isfinite(v) for v in (qty, total)):
                cash -= total
                quantity = qty
                entry_cost = total
                trades.append({"entry_index": index, "entry_price": fill,
                               "entry_fee": fee, "exit_index": None,
                               "exit_price": None, "exit_fee": None,
                               "pnl": None})
        elif pending == "SELL" and quantity > 0:
            fill = close * (1 - slip_rate)
            gross = quantity * fill
            fee = gross * fee_rate
            proceeds = gross - fee
            pnl = proceeds - entry_cost
            cash += proceeds
            trades[-1].update({"exit_index": index, "exit_price": fill,
                               "exit_fee": fee, "pnl": pnl})
            quantity = 0.0
            entry_cost = 0.0
        pending = None

        equity_curve.append(cash + quantity * close)
        # Do not generate a signal on the final bar: use it only to liquidate.
        if index < len(closes) - 1 and index >= 29:
            signal = analyze(closes[:index + 1])
            if signal.confidence >= min_confidence:
                if signal.action == "BUY" and quantity == 0:
                    pending = "BUY"
                elif signal.action == "SELL" and quantity > 0:
                    pending = "SELL"

    # Close any remaining position at the last available close.
    if quantity > 0:
        close = closes[-1]
        fill = close * (1 - slip_rate)
        gross = quantity * fill
        fee = gross * fee_rate
        proceeds = gross - fee
        pnl = proceeds - entry_cost
        cash += proceeds
        trades[-1].update({"exit_index": len(closes) - 1, "exit_price": fill,
                           "exit_fee": fee, "pnl": pnl, "forced_exit": True})
        quantity = 0.0
        equity_curve.append(cash)

    final_equity = cash
    peak = equity_curve[0]
    max_drawdown = 0.0
    for equity in equity_curve:
        peak = max(peak, equity)
        if peak:
            max_drawdown = max(max_drawdown, (peak - equity) / peak)

    closed = [t for t in trades if t["pnl"] is not None]
    wins = sum(t["pnl"] > 0 for t in closed)
    losses = sum(t["pnl"] < 0 for t in closed)
    return {
        "starting_cash": starting_cash,
        "final_equity": final_equity,
        "net_pnl": final_equity - starting_cash,
        "return_pct": (final_equity / starting_cash - 1) * 100,
        "max_drawdown_pct": max_drawdown * 100,
        "closed_trades": len(closed),
        "open_trades": len(trades) - len(closed),
        "wins": wins,
        "losses": losses,
        "win_rate_pct": (wins / len(closed) * 100) if closed else 0.0,
        "fees_paid": sum(t["entry_fee"] + (t["exit_fee"] or 0)
                         for t in trades),
        "trades": trades,
        "equity_curve": equity_curve,
        "execution_model": "next_bar_close_with_fee_and_slippage",
    }
