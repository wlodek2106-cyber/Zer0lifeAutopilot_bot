"""Standalone browser sessions and non-custodial SOL subscription receipts.

No transaction construction, signing, custody debit, or public-opening switch.
"""
import hmac
import json
import secrets
import time
from urllib.parse import urlparse

from aiohttp import web
from solders.signature import Signature

from account_security import digest
from custody_ledger import SOL
from tx_deposits import incoming_amounts
from wallet_runtime import WalletError, GENESIS

RECEIVER = "57S7TryAMhxRq5sMSyZxTkyCmzT8tPFKfXjzZwvmv5db"
PRICE = 500_000_000
PERIOD = 30 * 86400
SESSION_COOKIE = "__Host-zrl-session"
LOGIN_COOKIE = "__Host-zrl-login"


class InternalRequest(dict):
    def __init__(self, uid, body):
        super().__init__(telegram_user={"id": uid})
        self.body = body

    async def json(self):
        return self.body


class WebAccess:
    def __init__(self, runtime):
        self.runtime = runtime
        self.ledger = runtime.ledger
        self.accounts = runtime.accounts
        self.origin = self.accounts.origin
        self.bot_username = ""
        with self.ledger.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS web_sessions (
                    hash TEXT PRIMARY KEY, uid INTEGER NOT NULL, credential TEXT NOT NULL,
                    expires INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS web_rate_limits (
                    hash TEXT PRIMARY KEY, started INTEGER NOT NULL, attempts INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS access_orders (
                    id TEXT PRIMARY KEY, uid INTEGER NOT NULL, sender TEXT NOT NULL,
                    created INTEGER NOT NULL, deadline INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS access_receipts (
                    signature TEXT PRIMARY KEY, order_id TEXT NOT NULL UNIQUE,
                    uid INTEGER NOT NULL, sender TEXT NOT NULL, raw INTEGER NOT NULL,
                    block_time INTEGER NOT NULL, granted_at INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS access_memberships (
                    uid INTEGER PRIMARY KEY, expires INTEGER NOT NULL);
            """)

    def owner(self, uid):
        # Never trust usernames, client flags, account nicknames or payment recipients.
        return int(uid) == int(self.runtime.owner_id)

    def state(self, uid):
        now = int(time.time())
        with self.ledger.db() as db:
            row = db.execute("SELECT expires FROM access_memberships WHERE uid=?", (uid,)).fetchone()
        free = self.owner(uid)
        expiry = row["expires"] if row else 0
        return {"success": True, "active": free or expiry > now, "admin_free": free,
                "expires_at": None if free else expiry, "server_time": now,
                "price_sol": "0.5", "price_lamports": str(PRICE), "days": 30,
                "recipient": RECEIVER,
                "purchase_available": self.runtime.public_enabled or uid in self.runtime.beta_users,
                "sender_wallet": self.ledger.wallet_address(uid)}

    def pause_expired(self, uid=None):
        now = int(time.time())
        with self.ledger.db() as db:
            db.execute("""UPDATE custody_users SET trading=0,sniper_trading=0
                WHERE uid<>? AND NOT EXISTS(
                    SELECT 1 FROM access_memberships m WHERE m.uid=custody_users.uid AND m.expires>?)
                """ + (" AND uid=?" if uid is not None else ""),
                (int(self.runtime.owner_id), now, uid) if uid is not None else (int(self.runtime.owner_id), now))
        # close_requested is deliberately preserved: explicit user-requested exits.

    def require(self, uid, kind="BUY", position_id=None, manual=False):
        if manual and kind == "SELL" and self.ledger.settings(uid)["close_requested"]:
            if any(p["id"] == position_id and p["state"] in ("OPEN", "RESERVED")
                   for p in self.ledger.positions(uid)):
                return
        if not self.state(uid)["active"]:
            self.pause_expired(uid)
            raise WalletError("Trading subscription expired or not activated. Automatic trading is stopped.", 402)

    def trading_request(self, request):
        uid = self.runtime.uid(request)
        if not self.owner(uid) and request.get("auth_method") != "web":
            raise WalletError("Trading is available only in the standalone web terminal.", 403)
        self.require(uid)

    def same_origin(self, request):
        if request.headers.get("Origin") != self.origin:
            raise WalletError("Open this page on the configured secure website.", 403)

    async def body(self, request):
        if request.content_length is not None and request.content_length > 8192:
            raise WalletError("Request is too large.", 413)
        raw = await request.content.read(8193)
        if len(raw) > 8192:
            raise WalletError("Request is too large.", 413)
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeError):
            raise WalletError("Invalid JSON.", 400)
        if not isinstance(value, dict):
            raise WalletError("Invalid JSON object.", 400)
        return value

    def authenticate(self, request):
        token = request.cookies.get(SESSION_COOKIE, "")
        if len(token) != 43:
            raise WalletError("Sign in to the web terminal with your passkey.", 401)
        with self.ledger.db() as db:
            row = db.execute("""SELECT s.uid,p.nickname FROM web_sessions s
                JOIN app_passkeys k ON k.id=s.credential AND k.uid=s.uid
                JOIN app_profiles p ON p.uid=s.uid
                WHERE s.hash=? AND s.expires>?""", (digest(token), int(time.time()))).fetchone()
        if not row:
            raise WalletError("Browser session expired. Sign in again.", 401)
        if request.method not in ("GET", "HEAD"):
            self.same_origin(request)
            if not hmac.compare_digest(request.headers.get("X-ZRL-CSRF", ""), digest("csrf:" + token)):
                raise WalletError("Invalid browser request token.", 403)
        return {"id": row["uid"], "username": row["nickname"]}

    def rate_limit(self, request, nickname=""):
        # Use the actual proxy peer, never a client-controlled X-Forwarded-For.
        # Separate account cap avoids a small shared-proxy cap locking out everyone.
        peer = str(request.remote or "unknown")
        keys = [(digest("login-peer:" + peer), 200),
                (digest("login-account:" + peer + ":" + nickname), 20)]
        now = int(time.time())
        with self.ledger.db() as db:
            db.execute("DELETE FROM web_rate_limits WHERE started<?", (now - 600,))
            for key,limit in keys:
                row = db.execute("SELECT attempts FROM web_rate_limits WHERE hash=?", (key,)).fetchone()
                if row and row["attempts"] >= limit:
                    raise WalletError("Too many sign-in attempts. Wait ten minutes.", 429)
            for key,limit in keys:
                db.execute("""INSERT INTO web_rate_limits VALUES(?,?,1)
                    ON CONFLICT(hash) DO UPDATE SET attempts=attempts+1""", (key, now))

    async def public(self, request):
        try:
            result = await self.public_action(request)
            result.headers["Cache-Control"] = "no-store"
            result.headers["Referrer-Policy"] = "no-referrer"
            return result
        except WalletError as exc:
            return web.json_response({"success": False, "message": str(exc)}, status=exc.status,
                                     headers={"Cache-Control": "no-store"})

    async def public_action(self, request):
        if request.path == "/web-auth/me":
            try:
                user = self.authenticate(request)
            except WalletError:
                user = None
            return web.json_response({"success": True, "user": user,
                "csrf": digest("csrf:" + request.cookies.get(SESSION_COOKIE, "")) if user else None,
                "registration_url": "https://t.me/" + self.bot_username if self.bot_username else None})
        self.same_origin(request)
        body = await self.body(request)
        if request.path == "/web-auth/start":
            nick = str(body.get("nickname", "")).strip().lower()
            if len(nick) > 24:
                raise WalletError("Invalid account nickname.", 400)
            self.rate_limit(request, nick)
            with self.ledger.db() as db:
                row = db.execute("SELECT uid FROM app_profiles WHERE nickname_key=?", (nick,)).fetchone()
            if not row:
                raise WalletError("Account or verification code is invalid. Register and set up a passkey in Telegram first.", 401)
            uid = row["uid"]
            self.accounts.pin(uid, body.get("pin"))
            binder = secrets.token_urlsafe(32)
            data = json.loads((await self.accounts.create_ticket(
                InternalRequest(uid, {"purpose": "login", "pin": body.get("pin")}))).text)
            with self.ledger.db() as db:
                db.execute("UPDATE app_passkey_tickets SET payload=? WHERE hash=?",
                           (json.dumps({"browser": digest(binder)}), digest(data["ticket"])))
            response = web.json_response(data)
            response.set_cookie(LOGIN_COOKIE, binder, secure=True, httponly=True, samesite="Strict",
                                path="/", max_age=300)
            return response
        if request.path == "/web-auth/finish":
            token = body.get("ticket")
            session = secrets.token_urlsafe(32)
            with self.ledger.db() as db:
                row = self.accounts.ticket(db, token)
                payload = json.loads(row["payload"])
                if (row["purpose"] != "login" or not hmac.compare_digest(
                        payload.get("browser", ""), digest(request.cookies.get(LOGIN_COOKIE, "")))):
                    raise WalletError("Sign-in belongs to another browser.", 403)
                if row["phase"] != "approved":
                    return web.json_response({"success": True, "pending": True}, status=202)
                key = payload.get("credential", "")
                if not db.execute("SELECT 1 FROM app_passkeys WHERE id=? AND uid=?", (key, row["uid"])).fetchone():
                    raise WalletError("Passkey was revoked.", 401)
                db.execute("UPDATE app_passkey_tickets SET used=1 WHERE hash=?", (row["hash"],))
                db.execute("DELETE FROM web_sessions WHERE expires<?", (int(time.time()),))
                db.execute("INSERT INTO web_sessions VALUES(?,?,?,?)",
                           (digest(session), row["uid"], key, int(time.time()) + 43200))
            response = web.json_response({"success": True})
            response.set_cookie(SESSION_COOKIE, session, secure=True, httponly=True,
                                samesite="Strict", path="/", max_age=43200)
            response.del_cookie(LOGIN_COOKIE, path="/", secure=True, httponly=True, samesite="Strict")
            return response
        if request.path == "/web-auth/logout":
            self.authenticate(request)
            with self.ledger.db() as db:
                db.execute("DELETE FROM web_sessions WHERE hash=?",
                           (digest(request.cookies.get(SESSION_COOKIE, "")),))
            response = web.json_response({"success": True})
            response.del_cookie(SESSION_COOKIE, path="/", secure=True, httponly=True, samesite="Strict")
            return response
        raise WalletError("Not found.", 404)

    async def status(self, request, session):
        uid = self.runtime.uid(request)
        self.pause_expired(uid)
        with self.ledger.db() as db:
            pending = db.execute("""SELECT * FROM access_orders o WHERE uid=?
                AND NOT EXISTS(SELECT 1 FROM access_receipts r WHERE r.order_id=o.id)
                ORDER BY created DESC LIMIT 1""", (uid,)).fetchone()
        return web.json_response({**self.state(uid), "pending_order": dict(pending) if pending else None},
                                 headers={"Cache-Control": "no-store"})

    async def order(self, request, session):
        uid = self.runtime.uid(request)
        if self.owner(uid):
            raise WalletError("Administrator access is permanently free. No payment needed.", 409)
        if request.get("auth_method") != "web":
            raise WalletError("Subscription payments are available only on the standalone website.", 403)
        self.accounts.require(uid)
        self.runtime.admit(uid)  # Do not charge customers while public custody is closed.
        sender = self.ledger.wallet_address(uid)
        if not sender or sender in (RECEIVER, self.runtime.wallet):
            raise WalletError("Confirm a separate paying wallet first. The payment receiver cannot pay itself.", 409)
        now = int(time.time())
        with self.ledger.db() as db:
            row = db.execute("""SELECT * FROM access_orders o WHERE uid=? AND deadline>?
                AND NOT EXISTS(SELECT 1 FROM access_receipts r WHERE r.order_id=o.id)
                ORDER BY created DESC LIMIT 1""", (uid, now)).fetchone()
            if not row:
                oid = secrets.token_urlsafe(24)
                db.execute("INSERT INTO access_orders VALUES(?,?,?,?,?)", (oid, uid, sender, now, now + 3600))
                row = db.execute("SELECT * FROM access_orders WHERE id=?", (oid,)).fetchone()
        return web.json_response({"success": True, "order": dict(row), **self.state(uid)})

    async def verify(self, request, session):
        uid = self.runtime.uid(request)
        if request.get("auth_method") != "web":
            raise WalletError("Use the standalone website to verify subscription payment.", 403)
        body = await request.json()
        signature = str(body.get("tx_signature", "")).strip()
        if signature.startswith(("https://solscan.io/tx/", "https://explorer.solana.com/tx/")):
            signature = urlparse(signature).path.split("/tx/", 1)[1].rstrip("/")
        try:
            Signature.from_string(signature)
        except ValueError:
            raise WalletError("Paste a complete Solana transaction signature or explorer link.", 400)
        with self.ledger.db() as db:
            order = db.execute("SELECT * FROM access_orders WHERE id=? AND uid=?",
                               (str(body.get("order_id", "")), uid)).fetchone()
            receipt = db.execute("SELECT * FROM access_receipts WHERE signature=?", (signature,)).fetchone()
        if not order:
            raise WalletError("Create a payment request before sending SOL.", 404)
        if receipt:
            if receipt["uid"] != uid or receipt["order_id"] != order["id"]:
                raise WalletError("This transaction has already been used.", 409)
            return web.json_response({**self.state(uid), "already_verified": True})
        if await self.runtime.rpc(session, "getGenesisHash") != GENESIS:
            raise WalletError("Solana mainnet is required.", 409)
        tx = await self.runtime.rpc(session, "getTransaction", [signature,
            {"encoding": "jsonParsed", "commitment": "finalized", "maxSupportedTransactionVersion": 0}])
        if (not tx or not tx.get("meta") or tx["meta"].get("err", "missing") is not None
                or signature != (tx.get("transaction", {}).get("signatures") or [None])[0]):
            raise WalletError("Transaction is not finalized or failed. Do not resend; check this Tx again.", 409)
        stamp = tx.get("blockTime")
        if (type(stamp) is not int or not order["created"] - 30 <= stamp <= order["deadline"]
                or stamp > int(time.time()) + 60):
            raise WalletError("Payment must be sent during this payment request's one-hour window.", 409)
        if self.ledger.wallet_address(uid) != order["sender"]:
            raise WalletError("The confirmed paying wallet has changed.", 409)
        amounts = incoming_amounts(tx, RECEIVER, order["sender"])
        if amounts.get(SOL) != PRICE:
            raise WalletError("Subscription requires exactly 0.5 native SOL, not wrapped SOL or another token.", 409)
        now = int(time.time())
        with self.ledger.db() as db:
            old = db.execute("SELECT * FROM access_receipts WHERE signature=? OR order_id=?",
                             (signature, order["id"])).fetchone()
            if old:
                if old["signature"] != signature or old["uid"] != uid or old["order_id"] != order["id"]:
                    raise WalletError("Payment or request has already been used.", 409)
            else:
                current = db.execute("SELECT expires FROM access_memberships WHERE uid=?", (uid,)).fetchone()
                expires = max(now, current["expires"] if current else 0) + PERIOD
                db.execute("INSERT INTO access_receipts VALUES(?,?,?,?,?,?,?)",
                           (signature, order["id"], uid, order["sender"], PRICE, stamp, now))
                db.execute("""INSERT INTO access_memberships VALUES(?,?)
                    ON CONFLICT(uid) DO UPDATE SET expires=excluded.expires""", (uid, expires))
        # No custody credit, no trade activation and no debit: this is an access receipt only.
        return web.json_response({**self.state(uid), "message": "30 days added. Trading modes remain unchanged."})
