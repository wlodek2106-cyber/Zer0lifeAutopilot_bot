# Telegram profiles and withdrawal passkeys

No email, password, Resend, private domain, camera or face template is required.
Resend/DNS configuration remains unused; this does not send test/verification email.
Financial identity remains the server-verified Telegram UID. Nicknames are server
unique, ASCII 3–24 characters, case insensitive and never authorize money access.
Profile creation is idempotent for an existing UID; no financial ledger rewrite.

## Withdrawal and browser flow

- Existing signed Telegram init-data middleware protects account/ticket/status APIs.
- Profile creation and the existing withdrawal confirmation code precede enrollment.
- External browser page uses the existing fixed HTTPS Render origin and RP ID.
  A 32-byte capability is in the URL fragment, not URL paths, proxy query logs,
  referrers or the database; the page removes it from visible history.
- The browser also requires the account's withdrawal code, with the existing
  global-per-account PIN attempt lock. Ticket ownership cannot be supplied by
  the browser. Same-origin POST checks, no-store, frame denial and a restrictive
  standalone page CSP protect browser ceremonies.
- Registration and authentication are verified by pinned py_webauthn 3.0.1.
  Challenge, RP ID, exact origin, UP/UV, credential owner, user handle (if supplied),
  signature and sign counter are checked. Platform/resident credential requested;
  device/user verification is required, not a client `biometric=true` signal.
- The OS may use Face ID, Touch ID or device unlock. Face-only verification is not
  enforceable across devices; no claim of KYC or a face-only hardware attestation.
- New user activation for every credential ceremony avoids Safari async gesture
  loss. If the Telegram WebView is unsupported, open a fresh link in Safari/Chrome.
  Actual iPhone/Telegram support remains a deployment acceptance test.
- Withdraw approvals bind UID, asset, exact integer base-unit amount, full recipient
  and UUID. Amounts are rendered via BigInt, never rounded floats. Server consumes
  an unexpired single-use approval under the existing withdrawal lock, before
  reservation/signing. No approval, wrong owner or changed fields => no new reserve.
- Existing idempotent withdrawal request-state lookups do not resend transactions
  and do not require consuming another approval. PIN remains required. No automatic
  withdrawal is sent by the standalone browser page.
- Three active tickets per account, five-minute lifetime, eight verification
  attempts, five credentials max. Tokens and recovery codes are SHA256-hashed at
  rest; high-entropy random values are not password hashes.

## Recovery

First enrollment displays eight random 192-bit recovery codes once, only in the
secure browser response/page. Save them privately before closing the browser.
There is no email recovery or PIN-only downgrade. Add a backup passkey; ordinary
additional enrollment requires both an existing verified passkey and the PIN.

Recovery requires signed Telegram identity, PIN and an unused recovery code.
Old passkeys, recovery codes and outstanding approvals are revoked atomically
only after the new WebAuthn registration verifies. New codes are returned once.
Parallel first-enrollment tickets cannot register a second key without existing
credential authorization. Recovery does not move funds or change Telegram UID.
Loss of Telegram, PIN, all passkeys and/or recovery factors can prevent withdrawal;
there is no hidden administrative biometric bypass.

## Rollout

Draft, owner-reviewed rollout only. This update does not turn on public custody,
start either mode, change order sizes or execute live transactions. Stop both
modes and confirm no open/unmanaged positions before merging/deploying.
Existing profiles will need a nickname and passkey enrollment; their balances
remain owned by the same Telegram UID. Profile gating blocks new financial API
intents/new buys, but does not block deterministic exits of existing positions.
Existing user PINs remain valid and are never silently reset.

Before adding account tables, startup creates a consistent SQLite backup alongside
the persistent database, named `.pre-account-security-v1.sqlite3` after the DB name.
It verifies integrity, uses 0600 permissions and never overwrites an existing copy.
This is a same-volume migration backup, not offsite disaster recovery.
Back up the persistent SQLite database securely offsite before public rollout; it now includes
profiles, public keys, counters and hashed recovery credentials. Keep the existing
Render public hostname stable: changing RP ID/origin requires a planned credential
migration, not changing RP ID from untrusted request Host headers.

Before public opening: complete real iPhone Face ID enrollment, save recovery
codes, add/test backup passkey, verify cancelled approvals do not debit, test both
SOL/USDC transaction-bound approvals and recovery on a second device. These are
not claimed by local synthetic cryptographic/browser tests. Public custody remains
off, and this update is not a production custody certification.
