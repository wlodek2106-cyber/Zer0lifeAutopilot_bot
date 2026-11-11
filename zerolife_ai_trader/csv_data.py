"""Bounded CSV import helpers for offline historical OHLCV research data."""
import csv
from io import StringIO
from .market_data import validate_candles


def load_ohlcv_csv(source, *, interval_ms, reject_gaps=True,
                   max_rows=1_000_000, max_source_chars=50_000_000):
    if not isinstance(source, str) or not source.strip():
        raise ValueError("CSV source must be non-empty text.")
    if type(max_rows) is not int or not 1 <= max_rows <= 5_000_000:
        raise ValueError("max_rows must be an integer between 1 and 5,000,000.")
    if (type(max_source_chars) is not int
            or not 1 <= max_source_chars <= 250_000_000):
        raise ValueError("max_source_chars is outside safe bounds.")
    if len(source) > max_source_chars:
        raise ValueError("CSV source exceeds the configured size limit.")

    required = ("timestamp_ms", "open", "high", "low", "close", "volume")
    try:
        reader = csv.DictReader(StringIO(source), restkey="_extra_fields",
                                restval=None, strict=True)
        if not reader.fieldnames:
            raise ValueError("CSV header is missing.")
        headers = [name.strip().lstrip("\ufeff") if name else ""
                   for name in reader.fieldnames]
        if len(headers) != len(set(headers)) or tuple(headers) != required:
            raise ValueError("CSV must contain each required OHLCV column exactly once.")
        reader.fieldnames = headers
        candles = []
        for line_number, row in enumerate(reader, start=2):
            if line_number - 1 > max_rows:
                raise ValueError("CSV exceeds the configured row limit.")
            if "_extra_fields" in row:
                raise ValueError(f"CSV row {line_number} has extra columns.")
            if any(row.get(key) is None for key in required):
                raise ValueError(f"CSV row {line_number} has missing columns.")
            try:
                timestamp_raw = row["timestamp_ms"].strip()
                if not timestamp_raw or not timestamp_raw.isdecimal():
                    raise ValueError("timestamp_ms must be a non-negative integer.")
                candle = {"timestamp_ms": int(timestamp_raw)}
                for key in ("open", "high", "low", "close", "volume"):
                    raw = row[key].strip()
                    if not raw:
                        raise ValueError(f"{key} is required.")
                    candle[key] = float(raw)
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError(f"Invalid CSV row {line_number}: {exc}") from exc
            candles.append(candle)
    except csv.Error as exc:
        raise ValueError(f"Malformed CSV: {exc}") from exc

    return validate_candles(candles, interval_ms=interval_ms,
                             reject_gaps=reject_gaps)
