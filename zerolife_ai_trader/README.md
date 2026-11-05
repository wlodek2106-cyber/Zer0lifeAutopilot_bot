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

## Phase 2: existing-market-context shadow adapter

`zerolife_ai_trader.shadow.assess_market_context(symbol, metrics)` consumes the
numeric chart fields already emitted by the repository's `market_context`
module. It returns `BUY_CANDIDATE` or `HOLD` with explicit reasons. It is
read-only and does not call `ai_decisions`, `execution_policy`, custody,
wallet, signer, RPC, or any order endpoint.

Example:

```python
from zerolife_ai_trader.shadow import assess_market_context

assessment = assess_market_context("SOL", main_context.get("SOL", {}))
# Log/display assessment for comparison; never pass it to an executor.
```

A GitHub Actions workflow runs the isolated paper and shadow unit-test suites.
The adapter is deliberately not wired into the production trading loop yet:
first compare its decisions against the existing rules and review test results.
