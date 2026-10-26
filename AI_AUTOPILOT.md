# AI autopilot v1 — USDC main mode and SOL companion

## Scope and limitations

This is a callable OpenAI adapter plus independent custodial execution controller,
not a promise of profitable trading. No live model call, live trade, or withdrawal
has been validated by the automated tests.

Core mode selects SOL/JUP/RAY/PYTH/JTO/ORCA/KMNO/NOS/SHDW/PEPE/OTC using personal USDC,
up to five positions. New Core assets still require current liquidity/volume,
quote-impact and closed-candle chart checks before entry. The SOL-funded Sniper
uses WIF/BONK/BOME/POPCAT/MEW/PENGU/PEPE/OTC as screened seeds alongside bounded discovery,
up to five positions. Each 15-minute discovery scan considers at most 200 unique mints,
checks the top 40 through finalized RPC, and supplies at most eight candidates to AI.
A seed is not an eligible trade or a profit forecast.
PEPE and OTC are available to both universes: OTC/USDC in Core and OTC/SOL in Sniper.
OTC uses the exact verified Token-2022 mint with immutable metadata; other Token-2022
assets remain rejected. Each mode uses its own balance and all existing independent
entry checks remain in force.
SOL proceeds return to the same user's SOL balance. There is no automatic
cross-mode profit conversion or allocation.

Both flags are off on restart. The owner/user starts each mode in the authenticated
Telegram application after configuration and readiness checks. The agent must not
activate trading or submit transactions during deployment verification.

## Render configuration

Set `OPENAI_API_KEY` directly in the Render service's Environment screen.
Never paste it into chat, screenshots, GitHub, or this application's input fields.
OpenAI API billing is separate and belongs to the operator.

Optional variables:

- `OPENAI_MODEL`: defaults to `gpt-4.1-mini`; provider access must be checked live.
- AI analysis interval is fixed at 90 seconds to stay below the
  120-second execution-decision freshness limit.
- The service-wide daily AI-call cap is fixed at 960 attempts. At a 90-second
  interval, the service can use about 960 calls per UTC day.

The quota counts attempts (including failed calls), is persisted, applies service-wide,
and resets at UTC midnight. Results are shared across users and both modes.
This is a call-count cap, not a dollar-denominated spending limit; set a budget in
the provider account as well.

Use Trading → “Проверить AI без сделки” to test the actual configured key/model.
It sends only numeric market history and the permitted symbol list. No Telegram
identity, personal balance, wallet address or signing material is sent to OpenAI.
It does not construct, sign or submit a swap.

Then test readiness, allocate operator SOL fee reserve if needed, save personal
purchase sizes, and start modes manually. Defaults: 1 USDC and 0.005 SOL.
First analysis can HOLD while prices warm up; AI refreshes every 90 seconds.
This cadence can increase provider costs relative to the previous 15-minute interval; use the
provider's separate spending limit because this call-count cap is not a dollar budget.

## Execution guards

- Strict structured response and server-side BUY/SELL/HOLD and symbol validation.
- AI cannot choose an owner, destination, amount, API credentials or transaction.
- Finalized BUY/SELL operations enqueue one account-scoped Telegram push containing
  the actual fill amounts and Solscan link; automated exits are labeled stop-loss
  or take-profit. A safely failed stop-loss close alerts that the position remains
  open. Telegram 429 responses are retried; ambiguous delivery outcomes are not
  resent automatically to avoid duplicate pushes.
- Core-only signal guidance compares recent price samples, DEX momentum, relative
  strength versus SOL, and reported buy/sell pressure. It considers one strongest
  USDC candidate when momentum plus relative strength or buyer pressure align, and
  avoids recent/DEX 5-minute moves above 3%, 1-hour moves above 10%, and 6-hour
  declines below 5%. These signal heuristics are not backtested and do not promise
  trades or profits; Sniper's screening, signals, order size, and execution policy
  are unchanged.
- Purchases require at least five samples at analysis time, a decision no more than
  120 seconds old, no more than 1% subsequent price movement (SOL-relative for
  sniper), enough personal available capital, and the position-count limit.
- One attempted action per analysis/user/mode; failure never creates a blind retry.
- One global pending transaction; actual quote bounds, complete account simulation,
  finalized receipts and immutable double-entry accounting remain authoritative.
- Native sniper mint must be standard Tokenkeg, initialized, expected decimals,
  and without mint/freeze authority. These checks do not prove economic safety.
- Market snapshots choose the deepest observed Solana pool with liquidity at least
  USD 100,000 and 24-hour volume at least USD 10,000. These figures are reported by
  DexScreener, not independently audited, and do not guarantee execution liquidity.
- AI HTTP requests run separately from the position-management loop. Failed,
  unavailable or malformed AI output produces no new AI order. Both modes use a
  10% stop-loss; after +3.5% gain, a 2% pullback from the tracked peak triggers
  a trailing exit. These are deterministic conditions, not guaranteed fills.
- Jupiter slippage bound stays 40 bps. Fees/rent come from operator treasury.

## Ledger migration

Adds sniper settings and funding-asset/raw-cost/raw-proceeds columns, and backs up
an existing pre-AI database to `.pre-ai.bak` with mode 0600. Existing USDC costs
and balances are unchanged. All native amounts are lamports, never USDC units.
For SOL positions the required legacy `cost_usdc` column contains a compatibility
sentinel 1 and `proceeds_usdc` is NULL. Neither is used in native accounting, exits,
profit computation or UI; `funding_asset`, `cost_raw`, `proceeds_raw` are authoritative.
Do not use old reporting code that assumes every position is funded in USDC.

The upgrade is additive and restart-paused. Before deployment, stop both modes and
resolve/close positions as appropriate: paused modes do not monitor open positions.
Keep the database backup and immutable records; never delete unknown operations.

## Verification

Tests use temporary databases, mocked HTTP/model responses, synthetic simulation
accounts, and Chromium with mocked Telegram/API. Live public read-only checks of
token metadata, pools, quotes and candles cannot prove a real swap, fill or profit.
Every entry rechecks current eligibility and execution limits. Production model
access and account-specific readiness require checks in the authenticated app
after the operator enters the key.
