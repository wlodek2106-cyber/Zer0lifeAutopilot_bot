# Standalone trading access — SOL only

## Rules

- Standalone terminal: `/terminal` on the existing HTTPS Render hostname.
- Administrator: the server-configured `ADMIN_TELEGRAM_ID` is permanently free,
  in both the existing Telegram account and the standalone terminal. No nickname,
  client-side role flag, payment-recipient address or request-body UID grants this.
- Other accounts: exactly **0.5 native SOL** for **30 × 24 hours**. Renewal adds
  30 days to `max(current UTC time, existing expiry)`. No automatic billing.
- Payment receiver: `57S7TryAMhxRq5sMSyZxTkyCmzT8tPFKfXjzZwvmv5db`.
- No ZRL, trial package, seven-day offer, wrapped SOL, USDC or Memo requirement.
- Subscription payments are separate from custody funds and never credit the
  personal trading ledger or operator fee reserve. Customers must fund trading
  separately. Buying/renewing access never starts either mode.
- Non-admin payments are not offered while public custody is closed unless the
  account has explicitly been added to the operator's existing beta allowlist.
  This update does not enable public custody or expand that allowlist.

## Identity and browser sessions

Existing profiles, withdrawal codes, passkeys and custody balances keep the same
server-verified Telegram UID. No account merges, money migration or new email/DNS
provider. New users still create the account profile and first withdrawal passkey
in Telegram; `/web-auth/me` provides a registration link from server `getMe`.
Telegram is an identity/bootstrap interface, not a cryptocurrency subscription
checkout. Public trading and subscription purchase/verification require the
standalone authenticated browser; the operator's free account still works in
Telegram. Non-admin Telegram clients cannot enable modes or call paid trading
tools; its Trading navigation is hidden after its own stats load.

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

The customer first creates a one-hour payment request bound to UID and the
cryptographically confirmed sender wallet. Then sends exactly 0.5 native SOL and
pastes Tx/signature or Solscan/Solana Explorer transaction link. No funds are
constructed, signed or sent by the subscription feature.

Server checks mainnet genesis; finalized JSON-parsed transaction; successful
metadata; canonical first transaction signature; integer block time within the
request window (30-second clock tolerance at its start); linked sender signer;
native system transfer destination and amount; and actual recipient SOL delta.
Receiver/self-payments, unlinked/foreign senders, old transfers, wrong amount,
failed/missing transaction, wrapped SOL and wrong network cannot activate access.
Finalized receipts can be checked after their send window without resending.
The latest unpaid request is restored after reload.

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

Synthetic receipt tests, actual signed P256 WebAuthn tests, HTTP middleware tests
and mobile Chromium DOM tests cover identity, role/CSRF/cookie rules, renewal,
replay prevention, pre-broadcast expiry refund and owned manual closure. No live
SOL payment, real withdrawal, real iPhone Face ID or live trade was performed.

Draft PR only until explicit deployment consent. Pause both modes and ensure no
open/unmanaged positions before rollout; normal startup pauses modes. Existing
private keys, persistent storage and operator reserve configuration are unchanged.
No new secrets are needed. Do not change the Render hostname: existing passkeys
are bound to that RP ID. A custom-domain migration requires separate planning.

Before public opening, verify the full real-device browser login, wallet proof,
subscription receipt and SOL/USDC withdrawal paths with a controlled beta account.
Operator's own account is free and must never be asked to make a test payment.
Public custody remains off; this update is not a custody certification.
