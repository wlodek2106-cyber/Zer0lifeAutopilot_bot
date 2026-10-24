# Bounded Solana discovery

The SOL-funded sniper is no longer restricted to WIF. This is a bounded feed of
Solana candidates, not an index of all projects or a guarantee of meme classification
or future returns. WIF remains a seeded candidate but must pass current screening.

- Latest and recently updated DexScreener profiles, up to two dynamically selected
  trending meme meta feeds, meme/pump searches and mature BONK/POPCAT/BOME searches.
  Search terms are discovery inputs, not token identity or buy recommendations.
- Up to 200 unique mints, balanced across sources; core assets excluded.
- Raydium/Orca/Meteora/PumpSwap, pool liquidity and 24h volume >= $100k,
  >= 200 daily swaps, pool age >= 24h, no extreme 5-minute/hour pump.
- At most 40 screening leaders checked through finalized RPC mint data.
- Dynamically discovered assets must be standard SPL, with initialized mint,
  integer decimals 0..9, and no mint/freeze authority. Token-2022 candidates are
  rejected except the exact, preapproved OTC seed; OTC requires the verified
  Token-2022 program, six decimals, disabled mint/freeze/update authorities,
  immutable metadata pointer, and matching immutable OTC name/symbol.
- Liquidity/activity score is a screening priority, NOT expected return.
- At most 8 current candidates plus up to 32 held identities in the AI universe.
  At most 30 recent 20-second price samples per symbol in the model input.
- Contract descriptions, links and paid boosts are never model instructions/buy signals.
- Stable mint-derived internal codes prevent duplicate-symbol identity confusion.
  On-chain mint and decimals verified again before an actual entry.
- Registry identities persist for held positions across restarts and shortlist changes.
  A failed scan pauses new entries but does not delete positions/ledger ownership.
  Explicit closes and risk exits remain independent of candidate eligibility.
- Scans every 15 minutes in a separate task, not in the risk loop.
  Entry eligibility expires after 30 minutes and is downgraded by price refreshes.
  Restart requires a new successful scan before new sniper entries.
- Historical dropped symbols are not all requested from DexScreener on every tick;
  only current candidates and held assets are fetched.
- Core price feed uses the documented per-token batch endpoint rather than the
  legacy cross-token pairs response. Missing markets still fail closed.

Existing controls still apply: up to five SOL-funded positions per user, own SOL/order size,
five samples for token and SOL, 120-second AI action window, 1% price/relative-SOL
drift bound, 0.4% slippage, -10% stop and trailing exit after +3.5% / 2% peak pullback,
segregated fee treasury, one pending shared-vault operation, simulation/solvency/
finalization checks, no blind retries, model call quota and no auto-resume after
restart. No funds are converted or redistributed between modes/users by this change.

Limits: holder concentration, LP locks, upgradeability of all protocols, wash volume,
MEV, malicious pools, correlated exposure and every market/contract risk are NOT
fully excluded. Thin or failing market data can prevent timely exits. No promised
profit or fill price. Public custody stays disabled unless the operator separately
enables it; this update is not a public-launch security/compliance certification.

Registration/email/passkey work is a separate change. This update neither creates
new financial identities nor weakens Telegram ownership or withdrawal PIN checks.
