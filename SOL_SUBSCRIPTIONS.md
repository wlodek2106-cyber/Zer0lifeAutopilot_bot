# Standalone AI Trader access packages

## Rules

- Standalone terminal: `/terminal` on the existing HTTPS Render hostname.
- Administrator: the server-configured `ADMIN_TELEGRAM_ID` is permanently free,
  in both the existing Telegram account and the standalone terminal. No nickname,
  client-side role flag, payment-recipient address or request-body UID grants this.
- Other accounts receive the fixed free-access window through **2026-11-01
  00:00 Europe/Warsaw**. The server supplies the deadline; changing a device clock
  cannot extend access. An invoice created before the window can still be verified
  if it was paid within its original one-hour window; its paid days start after the
  free period so they are not consumed by the promotion.
- After the free window, packages are **100,000 ZRL for 7 days** and
  **0.5 native SOL for 30 days**. Renewals add the selected duration to
  `max(current UTC time, existing expiry)`. No automatic billing.
- ZRL package receiver: `57S7TryAMhxRq5sMSyZxTkyCmzT8tPFKfXjzZwvmv5db`.
- New SOL package receiver: `CXKWBCBBGcL4hmxoMSPKB3CzGqeZYFT2Ke4hgCJ3wi4Y`.
- Open SOL invoices created before the receiver change retain their original
  receiver until their one-hour invoice window expires.
- ZRL mint: `AyfWjjJ9FPYfsgKCUaZ7PwMpoHi8ujf4m3pYxy7MmDiq`, standard SPL Token,
  6 decimals; the mint has no mint or freeze authority on mainnet. ZRL is sent to
  the ZRL receiver's associated token account. Wrapped SOL, USDC, other tokens,
  wrong mints and wrong amounts are not accepted for these packages.
- Invoice amounts include a minimal unique base-unit suffix so simultaneous
  one-hour requests can be distinguished. Tariff cards show the package price;
  the payment screen and QR show the exact amount to send.
- Subscription payments are separate from custody funds and never credit the
  personal trading ledger or operator fee reserve. Customers must fund trading
  separately. Buying/renewing access never starts either mode.
- Free access grants only the subscription entitlement. Public custody and the
  existing beta allowlist remain unchanged. Paid checkout is not offered to an
  account unless it already passes the current public-custody/beta admission gate.
  This update does not enable public custody or expand that allowlist.

## Identity and browser sessions

Existing profiles, withdrawal codes, passkeys and custody balances keep the same
server-verified Telegram UID. No account merges, money migration or new email/DNS
provider. New users still create the account profile and first withdrawal passkey
in Telegram; `/web-auth/me` provides a registration link from server `getMe`.
Telegram's Tariffs menu shows the free-access countdown and the expiry prompt.
Telegram is not a cryptocurrency checkout: purchase and transaction verification
open the standalone authenticated browser and require the existing account
security/passkey flow. The operator's free account still works in Telegram.
Non-admin Telegram clients cannot bypass the existing admission checks.

Browser sign-in:
1. Nickname lookup selects an existing UID; it is NOT authentication.
2. Existing withdrawal code and its global account lockout are required.
3. A short-lived login ticket requests the existing account's passkey with
   mandatory user verification, challenge/RP/origin/signature/counter checks.
4. First click loads options. Second direct click invokes native WebAuthn.
5. Approval cannot be consumed in another browser: ticket payload binds the
   random, Secure/HttpOnly/SameSite=Strict pending-login cookie.
6. A one-use approved **login** proof creates a 12-hour server session. Only its
   hash is stored. A login proof cannot authorize a withdrawal.
7. Each session is bound to its actual credential ID; replacing/revoking that
   passkey invalidates its sessions. No PIN-only login or admin role bypass.

Cookie authenticated writes additionally require exact configured HTTPS Origin
and session-derived `X-ZRL-CSRF`. GET responses do not expose session tokens.
No PIN, raw session token or passkey signature is stored in localStorage.
Logout removes that browser session, not balances or autonomous modes.
Pre-login requests have persistent limits of 20 per nickname/proxy peer and
200 total per actual proxy peer per ten minutes, never trusting a client-supplied
X-Forwarded-For. On Render the latter can be a shared-proxy cap; review scaling
and trusted proxy configuration before wider rollout.

## Subscription receipt checks

The customer first creates a one-hour request bound to UID, selected package and
cryptographically confirmed sender wallet. The Tariffs screen shows a Solana Pay
QR, receiver, exact unique amount and deadline. The customer pastes the transaction
signature or Solscan/Solana Explorer link to verify. No funds are constructed,
signed or sent by the subscription feature.

Server checks mainnet genesis; finalized JSON-parsed transaction; successful
metadata; canonical first transaction signature; integer block time within the
request window (30-second clock tolerance at its start); and linked sender signer.
SOL requires the exact native system transfer and actual recipient SOL delta.
ZRL requires the configured mint, standard Token Program transfer, the sender's
owned token account, the receiver's associated token account, matching decimals,
and an exact wallet-wide net ZRL balance change. Receiver/self-payments,
unlinked/foreign senders, stale transfers, wrong/extra amounts, wrong mint,
failed/missing transaction, wrapped SOL and wrong network cannot activate access.
Finalized receipts can be checked after their send window without resending.
The latest live unpaid request is restored after reload.

One atomic SQLite transaction stores the unique canonical signature and unique
order ID, and extends the expiry. Duplicate checks of the same receipt/order
return existing access without extending it. A different order/account cannot
claim an already-used receipt. This is independent of trading deposit credit.

## Expiry and positions — explicitly requested full automatic stop

At expiry both per-user mode flags are paused. Automatic BUY and SELL require
active access (or the trusted administrator exception). Checks run before reserve,
again before signing and after simulation immediately before the durable prepare/
broadcast sequence. Failed pre-broadcast checks return only that user's reserve.
The worker rechecks expiry before processing each user's positions.

This stops automated take-profit and stop-loss too. **Open positions remain
exposed to market movements.** It does not liquidate them automatically.
Withdrawals and the account's explicitly requested Close positions action remain
available without a subscription. Manual SELL exception requires a server-set
close request and an owned OPEN/RESERVED position; clients cannot supply that
exception through an API field. No automated BUY is covered by it.

Already broadcast transactions cannot be recalled. Reconciliation of their actual
chain outcome continues after expiry and does not send them again. Renewal does
not automatically restart stopped modes; the user must enable them again.

## Verification and deployment

Synthetic SOL/ZRL receipt tests, actual signed P256 WebAuthn tests, HTTP middleware
tests and mobile Chromium DOM tests cover identity, role/CSRF/cookie rules, renewal,
replay prevention, the fixed free window, pre-broadcast expiry refund and owned
manual closure. The ZRL mint metadata was queried read-only on Solana mainnet. No
live payment, real withdrawal, real iPhone Face ID or live trade was performed.

Draft PR only until explicit deployment consent. Pause both modes and ensure no
open/unmanaged positions before rollout; normal startup pauses modes. Existing
private keys, persistent storage and operator reserve configuration are unchanged.
No new secrets are needed. Do not change the Render hostname: existing passkeys
are bound to that RP ID. A custom-domain migration requires separate planning.

Before public opening, verify the full real-device browser login, wallet proof,
subscription receipt and SOL/USDC withdrawal paths with a controlled beta account.
Operator's own account is free and must never be asked to make a test payment.
Public custody remains off; this update is not a custody certification.
