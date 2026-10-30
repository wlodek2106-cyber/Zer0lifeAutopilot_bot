# Threat Model

## Project Overview

AI Trader is an aiohttp service for Telegram users. It maintains account and custody records in SQLite, reads Solana state through RPC providers, and can sign trades or withdrawals with a server-side Solana key. Telegram, browser passkeys, and transaction-bound Telegram codes are used for identity and sensitive actions.

## Assets

- **Solana signing key and treasury funds** — compromise can authorize irreversible on-chain transfers.
- **Custody ledger and SQLite database** — balances, deposits, open operations, positions, and recovery evidence must remain accurate and private.
- **Telegram bot token and webhook secret** — compromise can impersonate the bot or intercept account workflows.
- **Account sessions, passkeys, PIN hashes, and withdrawal codes** — compromise can expose personal account access or enable unauthorized actions.
- **Trading and withdrawal authorization records** — client input must not override server-side limits, ownership, or transaction status.
- **Uploaded support images and user reports** — validate size/type and send only to the configured administrator.

## Trust Boundaries

- **Telegram/browser to API** — Telegram init data, passkey sessions, CSRF tokens, request bodies, and uploaded files are untrusted until verified server-side.
- **Authenticated user to administrator** — admin endpoints and operator-only balance actions must be owner-authorized on the server, regardless of client UI.
- **Application to SQLite** — ledger updates, pending-operation recovery, and audit evidence must be transactionally consistent.
- **Application to Solana RPC, Jupiter, Jito, market-data, and Telegram APIs** — external responses can be delayed, incomplete, malformed, or malicious; transaction outcomes must be verified before ledger changes.
- **Production host to secrets and signer** — environment secrets and the hot signing key are a high-impact boundary. A compromised host or CI credential can bypass application-level controls.
- **Development to production** — only reviewed source and explicitly approved releases should reach the live service; a restart can affect trading state.

## Scan Anchors

- Production entry points: `main.py`, `custody_runtime.py`, `account_security.py`, `web_access.py`.
- Highest-risk areas: key loading and transaction signing in `main.py` / `custody_runtime.py`; withdrawals and operation recovery in `custody_runtime.py`; Telegram/passkey authorization in `account_security.py` and `web_access.py`.
- Public: health, payment-status and narrowly scoped payment pages; authenticated: `/api/*` and browser-session routes; administrator: `/api/admin/*`.
- Tests under `tests/` exercise auth, custody, transaction recovery, and security headers. The mockup sandbox and design artifacts are not production service code.

## Threat Categories

### Spoofing

An attacker may replay or forge Telegram/browser identity or impersonate the Telegram webhook. Every protected request must verify Telegram's signature or the signed browser session; browser writes must validate origin and CSRF; the webhook must verify Telegram's secret-token header with constant-time comparison.

### Tampering

An attacker may submit a different user ID, recipient, amount, mint, or transaction result than the UI displays. Ownership, asset identity, amount arithmetic, route constraints, transaction signatures, and ledger effects must be derived and checked server-side. Unknown on-chain outcomes must remain reserved and must never be blindly resent.

### Repudiation

Users or operators may dispute a withdrawal, trade, or recovery. Sensitive actions must record the authenticated actor, operation ID, signature, state transitions, and verification evidence without logging credentials or private keys.

### Information Disclosure

Account balances, session material, support submissions, PINs, recovery codes, and application secrets must not appear in public responses or logs. Responses containing account data must be non-cacheable and scoped to the authenticated Telegram identity.

### Denial of Service

Unauthenticated or authenticated callers may exhaust API, RPC, Telegram, or market-data capacity. Request bodies and uploads must be bounded; authentication and support actions must be rate-limited; external calls need timeouts; expensive scans must remain bounded.

### Elevation of Privilege

A normal user may try to reach operator tools or act on another account. The entire `/api/admin/` namespace must require the configured administrator identity, with independent ownership checks in sensitive runtime methods. UI hiding is not an authorization control.

## Required Guarantees

- The Solana signer must never be exposed to browser code, logs, or source control.
- High-value custody must not rely on a single long-lived application hot key as its only security boundary; use an isolated signer or multisig and keep the online wallet's funds limited.
- A new or future administrator route must fail closed unless the authenticated Telegram user is the configured owner.
- Dependency updates must reject known vulnerable HTTP-parser versions, and production dependency scans must target the exact release source rather than stale workspace copies.
- Deployments, secret rotation, and signer migration must be reviewed separately from code changes; changing them can affect live trades or custody.
