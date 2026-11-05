"""Bounded public Binance Spot historical kline downloader.

Uses the public market-data endpoint only. No API key, wallet, or order
permissions are used. Network calls are explicit and can be mocked in tests.
"""
import json
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .binance_data import parse_binance_klines, _BINANCE_INTERVALS

_BASE_URL = "https://api.binance.com/api/v3/klines"


def fetch_binance_klines(symbol, *, interval, start_time_ms, end_time_ms,
                         page_limit=1000, timeout=10, max_pages=100):
    if not isinstance(symbol, str) or not re.fullmatch(r"[A-Za-z0-9]{5,20}", symbol):
        raise ValueError("symbol must be an alphanumeric Spot pair, e.g. SOLUSDC.")
    symbol = symbol.upper()
    if interval not in _BINANCE_INTERVALS:
        raise ValueError("Unsupported Binance interval.")
    if (type(start_time_ms) is not int or type(end_time_ms) is not int
            or start_time_ms < 0 or end_time_ms <= start_time_ms):
        raise ValueError("Invalid historical time range.")
    if type(page_limit) is not int or not 1 <= page_limit <= 1000:
        raise ValueError("page_limit must be between 1 and 1000.")
    if type(max_pages) is not int or not 1 <= max_pages <= 100:
        raise ValueError("max_pages must be between 1 and 100.")
    if type(timeout) not in (int, float) or timeout <= 0 or timeout > 60:
        raise ValueError("timeout must be greater than 0 and at most 60 seconds.")

    interval_ms = _BINANCE_INTERVALS[interval]
    cursor = start_time_ms
    candles_by_time = {}
    pages = 0
    while cursor < end_time_ms and pages < max_pages:
        params = urlencode({
            "symbol": symbol,
            "interval": interval,
            "startTime": cursor,
            "endTime": end_time_ms,
            "limit": page_limit,
        })
        request = Request(
            f"{_BASE_URL}?{params}",
            headers={"User-Agent": "Zer0Life-AI-Trader-Research/1.0",
                     "Accept": "application/json"},
            method="GET",
        )
        try:
            with urlopen(request, timeout=timeout) as response:
                if response.status != 200:
                    raise RuntimeError(f"Binance returned HTTP {response.status}.")
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raise RuntimeError(f"Binance HTTP error: {exc.code}.") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise RuntimeError(f"Binance market-data request failed: {exc}.") from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("Binance returned invalid JSON.") from exc

        if not isinstance(payload, list):
            raise RuntimeError("Binance response was not a kline list.")
        if not payload:
            break
        parsed = parse_binance_klines(
            payload, interval=interval, reject_gaps=False
        )["candles"]
        for candle in parsed:
            timestamp = candle["timestamp_ms"]
            if start_time_ms <= timestamp < end_time_ms:
                candles_by_time[timestamp] = candle
        last_timestamp = parsed[-1]["timestamp_ms"]
        next_cursor = last_timestamp + interval_ms
        if next_cursor <= cursor:
            raise RuntimeError("Binance pagination did not advance.")
        cursor = next_cursor
        pages += 1
        if len(payload) < page_limit:
            break

    candles = [candles_by_time[key] for key in sorted(candles_by_time)]
    if not candles:
        raise ValueError("Binance returned no candles for the requested range.")
    quality = parse_binance_klines(
        [
            [c["timestamp_ms"], str(c["open"]), str(c["high"]),
             str(c["low"]), str(c["close"]), str(c["volume"]),
             c["timestamp_ms"] + interval_ms - 1, "0", 0, "0", "0", "0"]
            for c in candles
        ],
        interval=interval, reject_gaps=True,
    )
    return {
        "symbol": symbol,
        "interval": interval,
        "start_time_ms": start_time_ms,
        "end_time_ms": end_time_ms,
        "pages_fetched": pages,
        "source": "binance_spot_public_klines",
        **quality,
    }
