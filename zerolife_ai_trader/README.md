# Zer0Life AI Trader Foundation

A non-invasive, dependency-free research and paper-trading foundation for the existing Zer0LifeAutopilot_bot repository.

## Modules
- `indicators.py`: validated EMA and RSI calculations.
- `strategy.py`: explainable deterministic BUY / SELL / HOLD signals.
- `risk.py`: independent position-count, confidence, order-size and daily-loss gate.
- `paper.py`: in-memory paper broker with fee and slippage modelling.
- `shadow.py`: read-only adapter for the existing `market_context` output. It mirrors conservative chart-entry checks and produces shadow-only `PAPER_BUY` / `HOLD` recommendations.

## Per-user configuration

The dashboard exposes a validated configuration for strategy selection, read-only market-data source, allowlisted symbols, timeframe, paper capital, risk limits, simulated fees/slippage, and notification preferences.

Settings are stored per Telegram account in the existing application ledger database through authenticated endpoints:

- `GET /api/ai-trader/settings`
- `POST /api/ai-trader/settings`

The server validates the complete schema and rejects unknown fields, unsupported symbols, invalid numeric ranges, and non-boolean notification flags. API secrets are intentionally not accepted by this configuration endpoint.

**Important boundary:** these settings are currently saved and displayed as account configuration. They are not yet wired into a running strategy scheduler or live order executor. Paper/shadow remains the only approved execution scope; live trading is not enabled.

## Run focused tests
From the repository root:

```bash
python -m unittest discover -s tests -p 'test_zerolife_ai_trader*.py' -v
```

## Safety boundary
This package is paper-only. It does not import or call custody, wallet, signer, RPC-write, bridge, deposit, withdrawal, or live-execution modules. The shadow adapter accepts data already fetched by the existing market-context layer; it does not make network calls and is not wired into the production trade loop. Existing Solana and Base execution paths remain unchanged.

The signal engine is deterministic rules, not an LLM. No paid AI API is required. Public market-data ingestion, exchange/DEX adapters, persistent paper-ledger integration, and any live execution require separate reviewed changes. Nothing here enables real-money trading or guarantees fills or returns.
