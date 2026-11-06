"""Multi-market technical-analysis agent. Read-only: produces research scenarios, never orders."""
from math import isfinite, sqrt
from .indicators import ema, rsi

SUPPORTED_MARKETS = {"solana", "binance"}
SUPPORTED_TIMEFRAMES = {"5m", "15m", "1h", "4h", "1d"}
TIMEFRAME_SECONDS = {"5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}


def _validate_candles(candles, timeframe, now, max_age_seconds):
    if timeframe not in SUPPORTED_TIMEFRAMES:
        raise ValueError("Unsupported technical-analysis timeframe.")
    if not isinstance(candles, (list, tuple)) or len(candles) < 35:
        raise ValueError("At least 35 OHLCV candles are required per timeframe.")
    clean = []
    previous = None
    for candle in candles:
        if not isinstance(candle, dict):
            raise ValueError("Candles must be OHLCV objects.")
        values = [candle.get(k) for k in ("open", "high", "low", "close", "volume")]
        if any(type(v) not in (int, float) or not isfinite(v) for v in values):
            raise ValueError("OHLCV values must be finite numbers.")
        o, h, l, c, v = map(float, values)
        if min(o, h, l, c) <= 0 or v < 0 or h < max(o, c, l) or l > min(o, c, h):
            raise ValueError("Invalid OHLCV candle bounds.")
        ts = candle.get("timestamp")
        if ts is None:
            ts = candle.get("timestamp_ms")
            ts = ts / 1000 if type(ts) in (int, float) else None
        if type(ts) not in (int, float) or not isfinite(ts):
            raise ValueError("Every candle needs a finite timestamp in seconds or timestamp_ms.")
        ts = float(ts)
        if previous is not None and ts <= previous:
            raise ValueError("Candle timestamps must be strictly increasing.")
        previous = ts
        clean.append((ts, o, h, l, c, v))
    if now - clean[-1][0] > max_age_seconds:
        raise ValueError("Latest candle is stale; analysis is blocked.")
    if clean[-1][0] > now + 60:
        raise ValueError("Latest candle timestamp is in the future.")
    return clean


def _macd(closes):
    fast, slow = ema(closes, 12), ema(closes, 26)
    line = fast - slow
    # Signal EMA over the MACD series, calculated from a rolling 26-period seed.
    alpha = 2 / 10
    values = []
    for end in range(26, len(closes) + 1):
        values.append(ema(closes[:end], 12) - ema(closes[:end], 26))
    signal = sum(values[:9]) / 9
    for value in values[9:]:
        signal = alpha * value + (1 - alpha) * signal
    return line, signal, line - signal


def _analyze_timeframe(candles):
    closes = [c[4] for c in candles]
    highs = [c[2] for c in candles]
    lows = [c[3] for c in candles]
    volumes = [c[5] for c in candles]
    last = closes[-1]
    ema20, ema50 = ema(closes, 20), ema(closes,  min(50, len(closes)))
    momentum = rsi(closes, 14)
    macd, macd_signal, macd_hist = _macd(closes)
    window = closes[-20:]
    mean = sum(window) / len(window)
    variance = sum((x - mean) ** 2 for x in window) / len(window)
    std = sqrt(variance)
    tr = [max(highs[i] - lows[i], abs(highs[i] - closes[i-1]),
              abs(lows[i] - closes[i-1])) for i in range(1, len(candles))]
    atr = sum(tr[-14:]) / min(14, len(tr))
    avg_volume = sum(volumes[-21:-1]) / 20
    volume_ratio = volumes[-1] / avg_volume if avg_volume > 0 else None
    trend = "UP" if last > ema20 > ema50 else "DOWN" if last < ema20 < ema50 else "RANGE"
    support = min(lows[-30:])
    resistance = max(highs[-30:])
    return {
        "close": round(last, 10), "trend": trend,
        "ema20": round(ema20, 10), "ema50": round(ema50, 10),
        "rsi14": round(momentum, 2), "macd": round(macd, 10),
        "macd_signal": round(macd_signal, 10), "macd_histogram": round(macd_hist, 10),
        "bollinger_upper": round(mean + 2 * std, 10),
        "bollinger_middle": round(mean, 10),
        "bollinger_lower": round(mean - 2 * std, 10),
        "atr14": round(atr, 10), "atr_pct": round(atr / last * 100, 4),
        "volume_ratio": round(volume_ratio, 3) if volume_ratio is not None else None,
        "support_30": round(support, 10), "resistance_30": round(resistance, 10),
        "last_candle_at": candles[-1][0],
    }


def analyze_market(market, symbol, candles_by_timeframe, *, now, max_age_seconds=None):
    """Analyze Solana DEX or Binance public OHLCV across supplied timeframes."""
    if market not in SUPPORTED_MARKETS:
        raise ValueError("Market must be solana or binance.")
    if not isinstance(symbol, str) or not symbol.strip() or len(symbol) > 40:
        raise ValueError("A valid market symbol is required.")
    if type(now) not in (int, float) or not isfinite(now):
        raise ValueError("A finite analysis timestamp is required.")
    if not isinstance(candles_by_timeframe, dict) or not candles_by_timeframe:
        raise ValueError("At least one timeframe is required.")
    results = {}
    for timeframe, candles in candles_by_timeframe.items():
        limit = max_age_seconds or TIMEFRAME_SECONDS.get(timeframe, 0) * 2
        if timeframe not in SUPPORTED_TIMEFRAMES or limit <= 0:
            raise ValueError("Unsupported timeframe.")
        clean = _validate_candles(candles, timeframe, float(now), limit)
        results[timeframe] = _analyze_timeframe(clean)
    ordered = [results[t] for t in ("5m", "15m", "1h", "4h", "1d") if t in results]
    bullish = sum(item["trend"] == "UP" for item in ordered)
    bearish = sum(item["trend"] == "DOWN" for item in ordered)
    score = bullish - bearish
    aligned = len(ordered) >= 2 and (bullish == len(ordered) or bearish == len(ordered))
    if aligned and bullish >= 2 and score >= 2:
        action, confidence = "BUY_WATCH", min(0.9, 0.55 + 0.07 * bullish)
    elif aligned and bearish >= 2 and score <= -2:
        action, confidence = "SELL_WATCH", min(0.9, 0.55 + 0.07 * bearish)
    else:
        action, confidence = "HOLD", 0.5
    return {
        "agent": "Zer0Life Technical Analyst", "version": "1.0",
        "market": market, "symbol": symbol.strip().upper(),
        "action": action, "confidence": round(confidence, 2),
        "timeframes_aligned": aligned, "timeframe_score": score,
        "timeframes": results,
        "summary": ("Multi-timeframe trend alignment detected; independent risk review required."
                    if action != "HOLD" else
                    "No complete multi-timeframe alignment; wait for clearer evidence."),
        "execution_authority": "none",
        "data_policy": "Stale or incomplete candles fail closed; analysis only, no orders.",
    }
