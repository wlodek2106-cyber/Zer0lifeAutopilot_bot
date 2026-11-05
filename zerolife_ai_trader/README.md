# Zer0Life AI Trader Foundation

A non-invasive, dependency-free research and paper-trading foundation for the existing Zer0LifeAutopilot_bot repository.

## What is included
- Validated EMA and RSI calculations.
- Explainable deterministic BUY / SELL / HOLD signals.
- Independent risk gate with position-count, confidence, order-size and daily-loss checks.
- In-memory paper broker with configurable fee and slippage.

## Safety boundary
This package is **paper-only**. It does not import or call custody, wallet, signer, RPC-write, bridge, deposit, withdrawal, or live-execution modules. It is not wired into the production trade loop. Existing Solana and Base execution paths are intentionally unchanged.

The signal engine is rules-based, not an LLM. No paid AI API is required. Public market-data ingestion, exchange/DEX adapters, persistent paper ledger integration, and live execution require separate reviewed changes.

## Run tests
From the repository root:

```bash
python -m unittest discover -s tests -p 'test_zerolife_ai_trader.py' -v
```

No real funds or API keys are needed.
