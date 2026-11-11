"""Portable, versioned JSON reports for offline AI Trader research."""
import json
from math import isfinite

_REPORT_FIELDS = (
    "starting_cash", "final_equity", "net_pnl", "return_pct",
    "buy_hold_final_equity", "buy_hold_return_pct",
    "excess_return_vs_buy_hold_pct", "max_drawdown_pct",
    "closed_trades", "open_trades", "wins", "losses", "win_rate_pct",
    "gross_profit", "gross_loss", "profit_factor", "expectancy_per_trade",
    "average_win", "average_loss", "fees_paid",
)


def build_backtest_report(result, *, symbol, interval, data_source,
                          period_start_ms, period_end_ms):
    if not isinstance(result, dict):
        raise ValueError("Backtest result must be an object.")
    for name, value in (("symbol", symbol), ("interval", interval),
                        ("data_source", data_source)):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must be non-empty text.")
    if (type(period_start_ms) is not int or type(period_end_ms) is not int
            or period_start_ms < 0 or period_end_ms <= period_start_ms):
        raise ValueError("Invalid report period.")
    metrics = {}
    for field in _REPORT_FIELDS:
        if field not in result:
            raise ValueError(f"Backtest result is missing {field}.")
        value = result[field]
        if value is not None and (type(value) not in (int, float)
                                  or not isfinite(value)):
            raise ValueError(f"Backtest metric {field} is not finite numeric data.")
        metrics[field] = value
    quality = result.get("data_quality")
    if not isinstance(quality, dict) or quality.get("quality") not in (
        "complete", "gaps_detected"
    ):
        raise ValueError("Validated data-quality metadata is required.")
    if quality.get("quality") != "complete" or quality.get("gap_count") != 0:
        raise ValueError("Reports require complete gap-free historical data.")
    return {
        "schema_version": 1,
        "report_type": "zerolife_ai_trader_backtest",
        "symbol": symbol.strip().upper(),
        "interval": interval.strip(),
        "data_source": data_source.strip(),
        "period": {
            "start_timestamp_ms": period_start_ms,
            "end_timestamp_ms": period_end_ms,
        },
        "execution_model": result.get("execution_model"),
        "metrics": metrics,
        "data_quality": {
            "quality": quality["quality"],
            "candle_count": quality.get("candle_count"),
            "interval_ms": quality.get("interval_ms"),
            "gap_count": quality.get("gap_count"),
        },
        "limitations": [
            "Historical simulation is not a guarantee of future performance.",
            "No live orders or wallet operations were performed.",
            "Results depend on the supplied market data and cost assumptions.",
        ],
    }


def report_to_json(report):
    if not isinstance(report, dict) or report.get("schema_version") != 1:
        raise ValueError("Unsupported or invalid report schema.")
    return json.dumps(report, sort_keys=True, indent=2, allow_nan=False)


def report_to_csv(report):
    """Export one summary row; detailed trade/equity series remain in JSON."""
    import csv
    from io import StringIO

    if not isinstance(report, dict) or report.get("schema_version") != 1:
        raise ValueError("Unsupported or invalid report schema.")
    metrics = report.get("metrics")
    period = report.get("period")
    quality = report.get("data_quality")
    if not all(isinstance(item, dict) for item in (metrics, period, quality)):
        raise ValueError("Report is missing required sections.")
    fields = (
        "schema_version", "symbol", "interval", "data_source",
        "period_start_ms", "period_end_ms", "data_quality",
        "candle_count", "gap_count", *_REPORT_FIELDS,
    )
    row = {
        "schema_version": report["schema_version"],
        "symbol": report.get("symbol"),
        "interval": report.get("interval"),
        "data_source": report.get("data_source"),
        "period_start_ms": period.get("start_timestamp_ms"),
        "period_end_ms": period.get("end_timestamp_ms"),
        "data_quality": quality.get("quality"),
        "candle_count": quality.get("candle_count"),
        "gap_count": quality.get("gap_count"),
        **metrics,
    }
    output = StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fields, extrasaction="raise")
    writer.writeheader()
    writer.writerow(row)
    return output.getvalue()
