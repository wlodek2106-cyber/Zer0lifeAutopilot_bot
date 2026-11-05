"""Deterministic, long-only historical backtest for supplied close-price candles.

Signals are computed only from prices available at bar close and fills occur
at the following bar close, avoiding same-bar look-ahead. This is research
software, not a forecast or a live execution adapter.
"""
from math import isfinite
from .signal_engine import analyze_with_strategy


def run_backtest(closes, *, starting_cash=1000.0, fee_bps=10.0,
                 slippage_bps=5.0, order_fraction=0.02,
                 min_confidence=0.60, strategy="ema_rsi"):
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

    if (strategy not in {"ema_rsi", "trend_following", "mean_reversion", "manual"}:
        raise ValueError("Unsupported backtest strategy.")
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
            cash_after = cash + proceeds
            if (not all(isfinite(v) for v in (fill, gross, fee, proceeds, pnl, cash_after))
                    or fill <= 0 or gross <= 0 or proceeds < 0 or cash_after < 0):
                raise ValueError("Backtest sell arithmetic is outside safe numeric bounds.")
            cash = cash_after
            trades[-1].update({"exit_index": index, "exit_price": fill,
                               "exit_fee": fee, "pnl": pnl})
            quantity = 0.0
            entry_cost = 0.0
        pending = None

        equity_curve.append(cash + quantity * close)
        # Do not generate a signal on the final bar: use it only to liquidate.
        if index < len(closes) - 1 and index >= 29:
            signal = analyze_with_strategy(closes[:index + 1], strategy)
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
        cash_after = cash + proceeds
        if (not all(isfinite(v) for v in (fill, gross, fee, proceeds, pnl, cash_after))
                or fill <= 0 or gross <= 0 or proceeds < 0 or cash_after < 0):
            raise ValueError("Backtest forced-exit arithmetic is outside safe numeric bounds.")
        cash = cash_after
        trades[-1].update({"exit_index": len(closes) - 1, "exit_price": fill,
                           "exit_fee": fee, "pnl": pnl, "forced_exit": True})
        quantity = 0.0
        equity_curve.append(cash)

    final_equity = cash
    # Fully invested buy-and-hold benchmark with the same fee and slippage model.
    benchmark_entry = closes[0] * (1 + slip_rate)
    benchmark_quantity = starting_cash / (benchmark_entry * (1 + fee_rate))
    benchmark_entry_cost = benchmark_quantity * benchmark_entry * (1 + fee_rate)
    benchmark_exit = closes[-1] * (1 - slip_rate)
    benchmark_gross = benchmark_quantity * benchmark_exit
    benchmark_exit_fee = benchmark_gross * fee_rate
    benchmark_final_equity = benchmark_gross - benchmark_exit_fee
    benchmark_fees = (benchmark_quantity * benchmark_entry * fee_rate
                      + benchmark_exit_fee)
    if not all(isfinite(v) for v in (
            benchmark_entry, benchmark_quantity, benchmark_entry_cost,
            benchmark_exit, benchmark_gross, benchmark_exit_fee,
            benchmark_final_equity, benchmark_fees)):
        raise ValueError("Backtest benchmark arithmetic is outside safe numeric bounds.")
    benchmark_return_pct = (benchmark_final_equity / starting_cash - 1) * 100
    strategy_vs_buy_hold_pct = (
        (final_equity / starting_cash - 1) * 100 - benchmark_return_pct
    )
    peak = equity_curve[0]
    max_drawdown = 0.0
    for equity in equity_curve:
        peak = max(peak, equity)
        if peak:
            max_drawdown = max(max_drawdown, (peak - equity) / peak)

    if not all(isfinite(v) for v in (final_equity, max_drawdown)):
        raise ValueError("Backtest result is outside safe numeric bounds.")
    closed = [t for t in trades if t["pnl"] is not None]
    winning_trades = [t["pnl"] for t in closed if t["pnl"] > 0]
    losing_trades = [t["pnl"] for t in closed if t["pnl"] < 0]
    wins = len(winning_trades)
    losses = len(losing_trades)
    gross_profit = sum(winning_trades)
    gross_loss = -sum(losing_trades)
    profit_factor = (gross_profit / gross_loss) if gross_loss else (
        None if not gross_profit else None
    )
    expectancy = (sum(t["pnl"] for t in closed) / len(closed)) if closed else 0.0
    average_win = (gross_profit / wins) if wins else 0.0
    average_loss = (-gross_loss / losses) if losses else 0.0
    return {
        "starting_cash": starting_cash,
        "final_equity": final_equity,
        "net_pnl": final_equity - starting_cash,
        "return_pct": (final_equity / starting_cash - 1) * 100,
        "buy_hold_final_equity": benchmark_final_equity,
        "buy_hold_return_pct": benchmark_return_pct,
        "buy_hold_fees_paid": benchmark_fees,
        "excess_return_vs_buy_hold_pct": strategy_vs_buy_hold_pct,
        "max_drawdown_pct": max_drawdown * 100,
        "closed_trades": len(closed),
        "open_trades": len(trades) - len(closed),
        "wins": wins,
        "losses": losses,
        "win_rate_pct": (wins / len(closed) * 100) if closed else 0.0,
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "profit_factor": profit_factor,
        "expectancy_per_trade": expectancy,
        "average_win": average_win,
        "average_loss": average_loss,
        "fees_paid": sum(t["entry_fee"] + (t["exit_fee"] or 0)
                         for t in trades),
        "trades": trades,
        "equity_curve": equity_curve,
        "execution_model": "next_bar_close_with_fee_and_slippage",
        "strategy": strategy,
    }


def run_segmented_evaluation(closes, *, segments=3, **backtest_settings):
    """Evaluate consecutive, non-overlapping chronological slices independently.

    Each slice starts with fresh paper capital. This is a robustness diagnostic,
    not model training or a substitute for a properly sourced out-of-sample set.
    """
    if type(segments) is not int or segments < 2:
        raise ValueError("At least two evaluation segments are required.")
    if not isinstance(closes, (list, tuple)) or len(closes) < segments * 31:
        raise ValueError("Each evaluation segment must contain at least 31 closes.")
    if any(type(value) not in (int, float) or not isfinite(value) or value <= 0
           for value in closes):
        raise ValueError("Closes must be finite positive numeric values.")

    base, remainder = divmod(len(closes), segments)
    results = []
    start = 0
    for index in range(segments):
        size = base + (1 if index < remainder else 0)
        end = start + size
        result = run_backtest(closes[start:end], **backtest_settings)
        results.append({
            "segment": index + 1,
            "start_index": start,
            "end_index_exclusive": end,
            "bars": size,
            "return_pct": result["return_pct"],
            "buy_hold_return_pct": result["buy_hold_return_pct"],
            "excess_return_vs_buy_hold_pct": result["excess_return_vs_buy_hold_pct"],
            "max_drawdown_pct": result["max_drawdown_pct"],
            "closed_trades": result["closed_trades"],
            "fees_paid": result["fees_paid"],
        })
        start = end

    returns = [item["return_pct"] for item in results]
    return {
        "segments": results,
        "segment_count": segments,
        "mean_segment_return_pct": sum(returns) / len(returns),
        "worst_segment_return_pct": min(returns),
        "positive_segments": sum(value > 0 for value in returns),
        "all_segments_positive": all(value > 0 for value in returns),
        "evaluation_method": "chronological_non_overlapping_segments",
    }


def run_candle_backtest(candles, *, interval_ms, anchor_ms=0,
                        reject_gaps=True, **backtest_settings):
    """Run the close-price backtest only after strict OHLCV validation."""
    from .market_data import validate_candles

    report = validate_candles(
        candles, interval_ms=interval_ms, anchor_ms=anchor_ms,
        reject_gaps=reject_gaps,
    )
    closes = [candle["close"] for candle in report["candles"]]
    result = run_backtest(closes, **backtest_settings)
    result["data_quality"] = {
        "quality": report["quality"],
        "candle_count": report["count"],
        "interval_ms": report["interval_ms"],
        "anchor_ms": report["anchor_ms"],
        "gap_count": report["gap_count"],
        "first_timestamp_ms": report["first_timestamp_ms"],
        "last_timestamp_ms": report["last_timestamp_ms"],
    }
    return result
