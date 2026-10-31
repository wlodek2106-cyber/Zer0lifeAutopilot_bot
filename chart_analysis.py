"""Deterministic OHLCV chart features for Core and Sniper strategies."""
import math


MIN_CANDLES = 50
MAX_M5_AGE_SECONDS = 7 * 60
MAX_H1_AGE_SECONDS = 70 * 60


def _number(value):
    if isinstance(value, bool) or type(value) not in (int, float):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def parse_ohlcv(payload, interval_seconds, now=None):
    """Return sorted, validated, closed OHLCV bars from GeckoTerminal's response."""
    if type(interval_seconds) is not int or interval_seconds <= 0:
        return []
    if now is None:
        import time
        now = time.time()
    try:
        rows = payload["data"]["attributes"]["ohlcv_list"]
    except (TypeError, KeyError):
        return []
    if not isinstance(rows, list):
        return []

    by_timestamp = {}
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 6:
            continue
        timestamp = _number(row[0])
        values = [_number(value) for value in row[1:6]]
        if timestamp is None or any(value is None for value in values):
            continue
        timestamp = int(timestamp / 1000) if timestamp > 100_000_000_000 else int(timestamp)
        opened, high, low, close, volume = values
        if (timestamp <= 0 or timestamp + interval_seconds > now - 2
                or min(opened, high, low, close) <= 0 or volume < 0
                or high < max(opened, close, low) or low > min(opened, close, high)):
            continue
        by_timestamp[timestamp] = {
            "timestamp": timestamp,
            "open": opened,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
        }
    return [by_timestamp[key] for key in sorted(by_timestamp)]


def _ema(values, period):
    if len(values) < period or period <= 0:
        return None
    alpha = 2 / (period + 1)
    result = sum(values[:period]) / period
    for value in values[period:]:
        result = alpha * value + (1 - alpha) * result
    return result


def _rsi(values, period=14):
    if len(values) <= period:
        return None
    changes = [values[index] - values[index - 1] for index in range(1, len(values))]
    recent = changes[-period:]
    gains = sum(max(change, 0) for change in recent) / period
    losses = sum(max(-change, 0) for change in recent) / period
    if losses == 0:
        return 100.0 if gains > 0 else 50.0
    return 100 - 100 / (1 + gains / losses)


def _atr(candles, period=14):
    if len(candles) <= period:
        return None
    ranges = []
    for previous, current in zip(candles[-period - 1:-1], candles[-period:]):
        ranges.append(max(
            current["high"] - current["low"],
            abs(current["high"] - previous["close"]),
            abs(current["low"] - previous["close"]),
        ))
    return sum(ranges) / period if ranges else None


def _macd_histogram_pct(values):
    if len(values) < 35:
        return None
    macd_values = []
    for end in range(26, len(values) + 1):
        prefix = values[:end]
        fast, slow = _ema(prefix, 12), _ema(prefix, 26)
        if fast is not None and slow is not None:
            macd_values.append(fast - slow)
    signal = _ema(macd_values, 9)
    if signal is None or values[-1] <= 0:
        return None
    return (macd_values[-1] - signal) / values[-1] * 100


def _pivot_levels(candles, field, is_low):
    levels = []
    for index in range(2, len(candles) - 2):
        value = candles[index][field]
        neighbours = [candles[offset][field]
                      for offset in range(index - 2, index + 3) if offset != index]
        if ((value <= min(neighbours) and value < max(neighbours)) if is_low
                else (value >= max(neighbours) and value > min(neighbours))):
            levels.append(value)
    return levels


def _nearest_zone(levels, price, tolerance, support, minimum_touches=2):
    if not levels or tolerance <= 0:
        return None
    clusters = []
    for level in sorted(levels):
        if clusters and level - sum(clusters[-1]) / len(clusters[-1]) <= tolerance:
            clusters[-1].append(level)
        else:
            clusters.append([level])
    candidates = []
    for cluster in clusters:
        if len(cluster) < minimum_touches:
            continue
        center = sum(cluster) / len(cluster)
        if (center <= price if support else center >= price):
            candidates.append((abs(price - center), center, len(cluster)))
    if not candidates:
        return None
    _, center, touches = min(candidates, key=lambda item: item[0])
    width = max(tolerance, center * 0.001)
    return {
        "low": max(0.0, center - width),
        "high": center + width,
        "touches": touches,
    }


def _engulfing(previous, current, bullish):
    previous_body_low = min(previous["open"], previous["close"])
    previous_body_high = max(previous["open"], previous["close"])
    current_body_low = min(current["open"], current["close"])
    current_body_high = max(current["open"], current["close"])
    previous_bearish = previous["close"] < previous["open"]
    current_bullish = current["close"] > current["open"]
    previous_bullish = previous["close"] > previous["open"]
    current_bearish = current["close"] < current["open"]
    if bullish:
        return (previous_bearish and current_bullish
                and current_body_low <= previous_body_low
                and current_body_high >= previous_body_high)
    return (previous_bullish and current_bearish
            and current_body_low <= previous_body_low
            and current_body_high >= previous_body_high)


def analyze_charts(m5_candles, h1_candles, *, now, m5_stale=False, h1_stale=False):
    """Calculate chart metrics; entry setup is one fresh closed bullish 5m candle."""
    m5 = m5_candles if isinstance(m5_candles, list) else []
    h1 = h1_candles if isinstance(h1_candles, list) else []
    result = {
        "chart_ready": 0,
        "chart_buy_setup": 0,
        "chart_support_bounce": 0,
        "chart_trend_continuation": 0,
        "chart_sell_setup": 0,
        "chart_m5_candles": len(m5),
        "chart_h1_candles": len(h1),
    }
    if m5:
        result["chart_m5_age_seconds"] = max(0, int(now - m5[-1]["timestamp"] - 300))
    if h1:
        result["chart_h1_age_seconds"] = max(0, int(now - h1[-1]["timestamp"] - 3600))

    if not m5 or not h1:
        return result
    closes_5m = [bar["close"] for bar in m5]
    closes_h1 = [bar["close"] for bar in h1]
    current = m5[-1]
    price = current["close"]
    age_5m = result.get("chart_m5_age_seconds", MAX_M5_AGE_SECONDS + 1)
    age_h1 = result.get("chart_h1_age_seconds", MAX_H1_AGE_SECONDS + 1)
    ready = (
        len(m5) >= MIN_CANDLES and len(h1) >= MIN_CANDLES
        and age_5m <= MAX_M5_AGE_SECONDS and age_h1 <= MAX_H1_AGE_SECONDS
        and not m5_stale and not h1_stale
    )

    atr_5m = _atr(m5)
    atr_h1 = _atr(h1)
    ema20_5m = _ema(closes_5m, 20)
    ema50_5m = _ema(closes_5m, 50)
    ema20_h1 = _ema(closes_h1, 20)
    ema50_h1 = _ema(closes_h1, 50)
    rsi_5m = _rsi(closes_5m)
    macd_pct = _macd_histogram_pct(closes_5m)
    previous_volumes = [bar["volume"] for bar in m5[-21:-1] if bar["volume"] > 0]
    volume_ratio = (current["volume"] / (sum(previous_volumes) / len(previous_volumes))
                    if current["volume"] > 0 and previous_volumes else None)
    atr_pct = (atr_5m / price * 100) if atr_5m is not None and price > 0 else None
    tolerance = max((atr_h1 or 0) * 0.35, price * 0.002)
    support = _nearest_zone(
        _pivot_levels(h1[-100:], "low", True), price, tolerance, support=True)
    resistance = _nearest_zone(
        _pivot_levels(h1[-100:], "high", False), price, tolerance, support=False)

    trend = 0
    if ema20_h1 is not None and ema50_h1 is not None and len(closes_h1) >= 26:
        earlier_ema20 = _ema(closes_h1[:-5], 20)
        slope = ema20_h1 - earlier_ema20 if earlier_ema20 is not None else 0
        if price > ema20_h1 > ema50_h1 and slope > 0:
            trend = 1
        elif price < ema20_h1 < ema50_h1 and slope < 0:
            trend = -1

    if not ready:
        return result

    result.update({
        "chart_ready": 1,
        "chart_trend_h1": trend,
        "chart_rsi14_5m": rsi_5m,
        "chart_macd_hist_pct_5m": macd_pct,
        "chart_volume_ratio_5m": volume_ratio,
        "chart_atr_pct_5m": atr_pct,
        "chart_ema20_5m_usd": ema20_5m,
        "chart_ema50_5m_usd": ema50_5m,
        "chart_ema20_h1_usd": ema20_h1,
        "chart_ema50_h1_usd": ema50_h1,
        "chart_price_usd": price,
        "chart_m5_closed_at": m5[-1]["timestamp"],
        "chart_h1_closed_at": h1[-1]["timestamp"],
    })
    if support:
        result.update({
            "chart_support_low_usd": support["low"],
            "chart_support_high_usd": support["high"],
            "chart_support_touches_h1": support["touches"],
        })
    if resistance:
        result.update({
            "chart_resistance_low_usd": resistance["low"],
            "chart_resistance_high_usd": resistance["high"],
            "chart_resistance_touches_h1": resistance["touches"],
        })

    last = m5[-1]
    volume_ok = volume_ratio is not None and volume_ratio >= 1.0
    bounce_extension = max(tolerance, min((atr_5m or 0) * 2.0, price * 0.01))
    support_hold = bool(
        support and last["low"] <= support["high"] + tolerance
        and last["close"] >= support["low"] and last["close"] > last["open"]
        and last["close"] <= support["high"] + bounce_extension
    )
    momentum_ok = (rsi_5m is not None and 42 <= rsi_5m <= 72
                  and ema20_5m is not None and last["close"] >= ema20_5m
                  and macd_pct is not None and macd_pct >= -0.05)
    support_bounce = bool(trend >= 0 and support_hold and volume_ok and momentum_ok)
    # A separate continuation setup need not revisit a repeated hourly support.
    # Require the latest closed bar to be bullish, aligned EMAs and bounded extension; never
    # turn a wick, falling chart or overbought breakout into an entry signal.
    extension_limit = min((atr_5m or 0) * 2.0, price * 0.01)
    resistance_room = max(atr_5m or 0, price * 0.005)
    trend_continuation = bool(
        trend == 1 and len(m5) >= 2
        and ema20_5m is not None and ema50_5m is not None
        and price >= ema20_5m > ema50_5m
        and extension_limit > 0 and price - ema20_5m <= extension_limit
        and last["close"] > last["open"]
        and last["close"] > m5[-2]["close"]
        and rsi_5m is not None and 45 <= rsi_5m <= 68
        and macd_pct is not None and macd_pct >= 0
        and volume_ratio is not None and volume_ratio >= 0.8
        and (not resistance or price + resistance_room < resistance["low"])
    )
    result["chart_support_bounce"] = int(support_bounce)
    result["chart_trend_continuation"] = int(trend_continuation)
    # Entry confirmation is intentionally simple: the last parsed candle is closed
    # and fresh because chart_ready is true above. Repeated support, RSI, MACD,
    # volume and resistance remain context, not mandatory buy-pattern gates.
    result["chart_buy_setup"] = int(last["close"] > last["open"])

    near_resistance = bool(
        resistance and last["high"] >= resistance["low"] - tolerance
        and last["close"] <= resistance["high"] + tolerance
    )
    bearish_confirmation = last["close"] < last["open"] or (
        ema20_5m is not None and last["close"] < ema20_5m)
    result["chart_sell_setup"] = int(near_resistance and bearish_confirmation)
    result["chart_bullish_engulfing_5m"] = int(
        len(m5) >= 2 and _engulfing(m5[-2], m5[-1], bullish=True))
    result["chart_bearish_engulfing_5m"] = int(
        len(m5) >= 2 and _engulfing(m5[-2], m5[-1], bullish=False))
    return {key: value for key, value in result.items()
            if type(value) in (int, float) and math.isfinite(value)}
