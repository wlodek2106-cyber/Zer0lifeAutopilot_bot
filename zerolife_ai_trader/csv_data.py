"""CSV import helpers for offline historical OHLCV research datasets.

Expected columns: timestamp_ms, open, high, low, close, volume.
No network requests or exchange credentials are used here.
"""
import csv
from io import StringIO
from .market_data import validate_candles


def load_ohlcv_csv(source, *, interval_ms, reject_gaps=True):
    if not isinstance(source, str) or not source.strip():
        raise ValueError("CSV source must be non-empty text.")
    reader = csv.DictReader(StringIO(source))
    required = {"timestamp_ms", "open", "high", "low", "close", "volume"}
    if not reader.fieldnames or set(reader.fieldnames) != required:
        raise ValueError("CSV must contain exactly the required OHLCV columns.")
    candles = []
    for line_number, row in enumerate(reader, start=2):
        try:
            timestamp_raw = row["timestamp_ms"]
            if not timestamp_raw or not timestamp_raw.isdecimal():
                raise ValueError("timestamp_ms must be an integer.")
            candle = {"timestamp_ms": int(timestamp_raw)}
            for key in ("open", "high", "low", "close", "volume"):
                raw = row[key]
                if raw is None or not raw.strip():
                    raise ValueError(f"{key} is required.")
                candle[key] = float(raw)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"Invalid CSV row {line_number}: {exc}") from exc
        candles.append(candle)
    return validate_candles(candles, interval_ms=interval_ms,
                             reject_gaps=reject_gaps)
