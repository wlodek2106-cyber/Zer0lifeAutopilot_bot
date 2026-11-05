# Zer0Life AI Trader Foundation

A non-invasive, dependency-free research and paper-trading foundation for the existing Zer0LifeAutopilot_bot repository.

## Modules
- `indicators.py`: validated EMA and RSI calculations.
- `strategy.py`: explainable deterministic BUY / SELL / HOLD signals.
- `risk.py`: independent position-count, confidence, order-size and daily-loss gate.
- `paper.py`: in-memory paper broker with fee and slippage modelling.
- `portfolio.py`: validated mark-to-market paper portfolio valuation, PnL, and drawdown metrics.
- `paper_store.py`: immutable, per-user SQLite journal plus atomic, idempotent simulated BUY execution and persistent paper balances/positions.

Authenticated dashboard endpoint:

- `GET /api/ai-trader/paper-journal?limit=50` — returns the signed-in Telegram user's paper-fill history and summary only. The endpoint is read-only and does not place orders.
- `shadow.py`: read-only adapter for the existing `market_context` output. It mirrors conservative chart-entry checks and produces shadow-only `PAPER_BUY` / `HOLD` recommendations.

## Per-user configuration

The dashboard exposes a validated configuration for strategy selection, read-only market-data source, allowlisted symbols, timeframe, paper capital, risk limits, simulated fees/slippage, notification preferences, and an explicit opt-in for automatic paper BUY execution. The opt-in defaults to off; older saved settings are migrated with it off.

Settings are stored per Telegram account in the existing application ledger database through authenticated endpoints:

- `GET /api/ai-trader/settings`
- `POST /api/ai-trader/settings`
- `GET /api/ai-trader/paper-journal?limit=50` — authenticated read-only paper account, positions, and fill history.

The server validates the complete schema and rejects unknown fields, unsupported symbols, invalid numeric ranges, and non-boolean notification flags. API secrets are intentionally not accepted by this configuration endpoint.

**Important boundary:** these settings are currently saved and displayed as account configuration. When `paperAutoExecute` is enabled, fresh BUY signals from public Binance candles are simulated once per candle using configured paper capital, fee and slippage. Repeated evaluations of the same candle are deduplicated. Manual/CSV signals are never auto-executed. Paper/shadow remains the only approved execution scope; live trading is not enabled.

## Run focused tests
From the repository root:

```bash
python -m unittest discover -s tests -p 'test_zerolife_ai_trader*.py' -v
python -m unittest discover -s tests -p 'test_ai_trader_portfolio.py' -v
```

## Safety boundary
This package is paper-only. It does not import or call custody, wallet, signer, RPC-write, bridge, deposit, withdrawal, or live-execution modules. The shadow adapter accepts data already fetched by the existing market-context layer; it does not make network calls and is not wired into the production trade loop. Existing Solana and Base execution paths remain unchanged.

The signal engine is deterministic rules, not an LLM. No paid AI API is required. Public market-data ingestion, exchange/DEX adapters, persistent paper-ledger integration, and any live execution require separate reviewed changes. Nothing here enables real-money trading or guarantees fills or returns.

Paper fill journal tests:

```bash
python -m unittest discover -s tests -p 'test_ai_trader_paper_store.py' -v
```
