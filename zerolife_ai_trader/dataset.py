"""Reproducible provenance manifests for validated historical OHLCV datasets."""
import hashlib
import json
from .market_data import validate_candles


def build_dataset_manifest(candles, *, symbol, interval, interval_ms,
                           data_source, source_sha256=None, anchor_ms=0):
    for name, value in (("symbol", symbol), ("interval", interval),
                        ("data_source", data_source)):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must be non-empty text.")
    if source_sha256 is not None:
        if (not isinstance(source_sha256, str) or len(source_sha256) != 64
                or any(char not in "0123456789abcdef" for char in source_sha256.lower())):
            raise ValueError("source_sha256 must be a 64-character hexadecimal digest.")
        source_sha256 = source_sha256.lower()

    quality = validate_candles(candles, interval_ms=interval_ms,
                               anchor_ms=anchor_ms, reject_gaps=True)
    canonical = json.dumps(
        quality["candles"], sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return {
        "schema_version": 1,
        "manifest_type": "zerolife_ohlcv_dataset",
        "symbol": symbol.strip().upper(),
        "interval": interval.strip(),
        "interval_ms": interval_ms,
        "anchor_ms": anchor_ms,
        "data_source": data_source.strip(),
        "source_sha256": source_sha256,
        "dataset_sha256": hashlib.sha256(canonical).hexdigest(),
        "candle_count": quality["count"],
        "first_timestamp_ms": quality["first_timestamp_ms"],
        "last_timestamp_ms": quality["last_timestamp_ms"],
        "data_quality": quality["quality"],
        "gap_count": quality["gap_count"],
    }


def manifest_to_json(manifest):
    if (not isinstance(manifest, dict)
            or manifest.get("schema_version") != 1
            or manifest.get("manifest_type") != "zerolife_ohlcv_dataset"):
        raise ValueError("Unsupported or invalid dataset manifest.")
    return json.dumps(manifest, sort_keys=True, indent=2, allow_nan=False)
