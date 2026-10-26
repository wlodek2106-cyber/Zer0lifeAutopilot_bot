"""Bounded outcome memory, not model training or an optimized strategy."""
import math

MIN_SAMPLE = 20
MAX_OUTCOMES = 500


def summarize(rows, permitted_mints):
    """Only finalized closed outcomes; never include identities or raw balances."""
    buckets = {}
    for row in rows[:MAX_OUTCOMES]:
        mode = row.get("mode")
        mint = row.get("mint")
        if mode not in ("main", "sniper") or mint not in permitted_mints.get(mode, {}):
            continue
        cost, proceeds = row.get("cost_raw"), row.get("proceeds_raw")
        if type(cost) is not int or cost <= 0 or type(proceeds) is not int or proceeds < 0:
            continue
        result = (proceeds - cost) / cost * 100
        if not math.isfinite(result):
            continue
        symbol = permitted_mints[mode][mint]
        buckets.setdefault((mode, symbol), []).append(result)
    stats = {}
    for (mode, symbol), values in buckets.items():
        n = len(values)
        average = sum(values) / n
        stats.setdefault(mode, {})[symbol] = {
            "closed_trades": n, "wins": sum(value > 0 for value in values),
            "mean_return_pct": round(average, 4),
            "shrunk_mean_return_pct": round(average * n / (n + MIN_SAMPLE), 4),
            "worst_return_pct": round(min(values), 4),
            "evidence": "limited" if n < MIN_SAMPLE else "observed_not_validated",
        }
    return {
        "method": "rolling_finalized_outcomes_v1",
        "max_outcomes": MAX_OUTCOMES, "minimum_sample": MIN_SAMPLE,
        "sample_count": sum(v["closed_trades"] for m in stats.values() for v in m.values()),
        "by_mode": stats,
        "limitations": (
            "Historical observations, not predictions or independent samples. "
            "Returns use actual swap proceeds minus entry cost; exclude separately paid "
            "operator/network fees and partner commissions. Not net strategy profitability. "
            "No model fine-tuning, no parameter changes, no guaranteed profit."),
    }
