# Zer0Life AI Trader Foundation

A non-invasive, dependency-free research and paper-trading foundation for the existing Zer0LifeAutopilot_bot repository.

## Modules
- indicators.py: validated EMA and RSI calculations.
- strategy.py: explainable deterministic BUY / SELL / HOLD signals.
- risk.py: independent position-count, confidence, order-size and daily-loss gate.
- paper.py: in-memory paper broker with fee and slippage modelling.
- shadow.py: read-only adapter for the existing market_context output. It mirrors conservative chart-entry checks and produces shadow-only PAPER_BUY/HOLD recommendations.

## Run focused tests
From the repository root:

\`\`\`bash
python -m unittest discover -s tests -p 'test_zerolife_ai_trader*.py' -v
\`\`\`

## Safety boundary
This package is paper-only. It does not import or call custody, wallet, signer, RPC-write, bridge, deposit, withdrawal, or live-execution modules. The shadow adapter accepts data already fetched by the existing market-context layer; it does not make network calls and is not wired into the production trade loop. Existing Solana and Base execution paths remain unchanged.

The signal engine is deterministic rules, not an LLM. No paid AI API is required. Public market-data ingestion, exchange/DEX adapters, persistent paper-ledger integration, and any live execution require separate reviewed changes. Nothing here enables real-money trading or guarantees fills or returns.
