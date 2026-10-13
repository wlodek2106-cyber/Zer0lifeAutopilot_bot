# Activity history v1

The account-scoped `/api/logs` response now includes live mode/waiting status,
timestamped AI diagnostics, and the current states and signatures of the user's
financial operations. The UI uses the device's local timezone, safe textContent
and mobile wrapping. AI proposals are explicitly not executed transactions.

The worker records HOLD, SELL without an owned position, stale decisions, invalid
or drifted prices, missing SOL references, warm-up, purchase size/balance mismatch,
position limits, already-held assets, ineligible candidates, failed mint checks,
claimed analyses, and interrupted execution checks. Logging adds no AI requests,
quotes, trades, broadcasts or retries. Existing conditions, action claiming,
amounts and all financial checks are unchanged.

An additive diagnostic table retains 200 events per user across restarts, with
one event per user/analysis/mode/reason; repeated worker ticks do not spam duplicates.
It is not the immutable financial ledger. No historical skip reasons are invented
for analyses that predate this update. The latest shared model result can still be
displayed as a result only, including when that account's modes are paused.

Financial state labels come from custody_operations, not model output.
PREPARED/UNKNOWN explicitly do not mean successful broadcast or completion.
COMPLETED means finalized network outcome. Operation timestamps are initiation
timestamps; the displayed financial state is its current state at refresh time.
Only the authenticated user's rows/signatures are shown. Shared-vault waiting
status does not disclose other users' operation IDs, addresses, amounts or receipts.
Raw exception/provider responses and signing material are not logged.

No AI proposal, readiness check, empty-state message or execution-check event
proves an actual swap. No real-money or device biometric verification is performed
by the synthetic tests. The live diagnostic has no secret/administrative bypass.

Deployment requires separate approval. Stop both automatic modes and resolve
open/unmanaged positions first: normal startup pauses both modes. This change
does not enable public custody or alter subscriptions, PIN/passkey requirements,
fee reserves, trading signals or risk thresholds.
