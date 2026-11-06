# Zer0Life Multi-Market Technical Analyst

The read-only technical-analysis agent accepts normalized OHLCV candle arrays from either:
- Solana DEX market-data adapters (for example, the existing GeckoTerminal/DexScreener path)
- Binance public Spot market-data adapters

Both adapters must normalize candles to `timestamp` (Unix seconds) or `timestamp_ms`, plus `open`, `high`, `low`, `close`, and `volume`. Provide at least 35 candles per timeframe. Supported timeframes: 5m, 15m, 1h, 4h, 1d.

Indicators: EMA20/EMA50, RSI14, MACD(12,26,9), Bollinger Bands(20,2), ATR14, relative volume, and 30-candle support/resistance. The multi-timeframe result is a research watch (BUY_WATCH / SELL_WATCH / HOLD), not an executable order.

Stale, malformed, out-of-order, incomplete, or future-dated candles fail closed. The agent has no wallet, signing, order-placement, or execution authority. Existing server risk gates and Paper Trading remain separate.
