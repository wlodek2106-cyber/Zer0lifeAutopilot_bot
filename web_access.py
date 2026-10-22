"""Standalone browser sessions and non-custodial access-package receipts.

No transaction construction, signing, custody debit, or public-opening switch.
"""
import hmac
import json
import secrets
import time
from urllib.parse import urlparse

from aiohttp import web
from solders.signature import Signature
from solders.pubkey import Pubkey

from account_security import digest
from custody_ledger import SOL
from tx_deposits import incoming_amounts
from wallet_runtime import WalletError, GENESIS, TOKEN, WalletRuntime, associated

RECEIVER = "57S7TryAMhxRq5sMSyZxTkyCmzT8tPFKfXjzZwvmv5db"  # ZRL package receiver
SOL_RECEIVER = "CXKWBCBBGcL4hmxoMSPKB3CzGqeZYFT2Ke4hgCJ3wi4Y"
PRICE = 500_000_000
PERIOD = 30 * 86400
ZRL_MINT = "AyfWjjJ9FPYfsgKCUaZ7PwMpoHi8ujf4m3pYxy7MmDiq"
ZRL_DECIMALS = 6
ZRL_PRICE = 100_000
ZRL_PRICE_RAW = ZRL_PRICE * 10**ZRL_DECIMALS
ZRL_PERIOD = 7 * 86400
# 2026-11-01 00:00:00 Europe/Warsaw (the end of October's free-access window).
FREE_ACCESS_UNTIL = 1_793_487_600
SESSION_COOKIE = "__Host-zrl-session"
LOGIN_COOKIE = "__Host-zrl-login"

PACKAGES = {
    "zrl_7d": {
        "asset": "ZRL", "price": ZRL_PRICE, "base_raw": ZRL_PRICE_RAW,
        "decimals": ZRL_DECIMALS, "mint": ZRL_MINT, "period_seconds": ZRL_PERIOD,
        "label": "7 days", "recipient": RECEIVER,
    },
    "sol_30d": {
        "asset": "SOL", "price": "0.5", "base_raw": PRICE,
        "decimals": 9, "mint": "", "period_seconds": PERIOD,
        "label": "30 days", "recipient": SOL_RECEIVER,
    },
}


def display_amount(raw, decimals):
    whole, fraction = divmod(int(raw), 10**decimals)
    if not fraction:
        return str(whole)
    return f"{whole}.{fraction:0{decimals}d}".rstrip("0")


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
                    created INTEGER NOT NULL, deadline INTEGER NOT NULL,
                    package_id TEXT NOT NULL DEFAULT 'sol_30d',
                    asset TEXT NOT NULL DEFAULT 'SOL',
                    raw INTEGER NOT NULL DEFAULT 500000000,
                    period_seconds INTEGER NOT NULL DEFAULT 2592000,
                    mint TEXT NOT NULL DEFAULT '',
                    recipient TEXT NOT NULL DEFAULT '57S7TryAMhxRq5sMSyZxTkyCmzT8tPFKfXjzZwvmv5db');
                CREATE TABLE IF NOT EXISTS access_receipts (
                    signature TEXT PRIMARY KEY, order_id TEXT NOT NULL UNIQUE,
                    uid INTEGER NOT NULL, sender TEXT NOT NULL, raw INTEGER NOT NULL,
                    block_time INTEGER NOT NULL, granted_at INTEGER NOT NULL,
                    asset TEXT NOT NULL DEFAULT 'SOL', mint TEXT NOT NULL DEFAULT '',
                    recipient TEXT NOT NULL DEFAULT '57S7TryAMhxRq5sMSyZxTkyCmzT8tPFKfXjzZwvmv5db');
                CREATE TABLE IF NOT EXISTS access_memberships (
                    uid INTEGER PRIMARY KEY, expires INTEGER NOT NULL);
            """)
            for table, columns in (
                ("access_orders", (
                    ("package_id", "TEXT NOT NULL DEFAULT 'sol_30d'"),
                    ("asset", "TEXT NOT NULL DEFAULT 'SOL'"),
                    ("raw", "INTEGER NOT NULL DEFAULT 500000000"),
                    ("period_seconds", "INTEGER NOT NULL DEFAULT 2592000"),
                    ("mint", "TEXT NOT NULL DEFAULT ''"),
                    ("recipient", "TEXT NOT NULL DEFAULT '57S7TryAMhxRq5sMSyZxTkyCmzT8tPFKfXjzZwvmv5db'"),
                )),
                ("access_receipts", (
                    ("asset", "TEXT NOT NULL DEFAULT 'SOL'"),
                    ("mint", "TEXT NOT NULL DEFAULT ''"),
                    ("recipient", "TEXT NOT NULL DEFAULT '57S7TryAMhxRq5sMSyZxTkyCmzT8tPFKfXjzZwvmv5db'"),
                )),
            ):
                existing = {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}
                for name, definition in columns:
                    if name not in existing:
                        db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
            db.execute("CREATE INDEX IF NOT EXISTS access_orders_open_asset_raw "
                       "ON access_orders(asset, raw, deadline)")
        self.free_access_until = FREE_ACCESS_UNTIL

    def free_access_active(self, now=None):
        return int(time.time() if now is None else now) < self.free_access_until

    def trading_admitted(self, uid, now=None):
        uid = int(uid)
        return bool(
            self.owner(uid)
            or getattr(self.runtime, "public_enabled", False)
            or uid in getattr(self.runtime, "beta_users", set())
            or self.free_access_active(now)
        )

    @staticmethod
    def public_order(row):
        order = dict(row)
        package = PACKAGES.get(order["package_id"], PACKAGES["sol_30d"])
        order.update({
            "asset": package["asset"],
            "mint": package["mint"],
            "recipient": order.get("recipient") or package["recipient"],
            "decimals": package["decimals"],
            "amount": display_amount(order["raw"], package["decimals"]),
            "label": package["label"],
            "days": package["period_seconds"] // 86400,
        })
        return order

    def owner(self, uid):
        # Never trust usernames, client flags, account nicknames or payment recipients.
        return int(uid) == int(self.runtime.owner_id)

    def state(self, uid):
        now = int(time.time())
        with self.ledger.db() as db:
            row = db.execute("SELECT expires FROM access_memberships WHERE uid=?", (uid,)).fetchone()
        owner_free = self.owner(uid)
        promo_free = not owner_free and self.free_access_active(now)
        expiry = row["expires"] if row else 0
        paid_active = expiry > now
        admitted = self.trading_admitted(uid, now)
        return {"success": True, "active": owner_free or promo_free or paid_active,
                "admin_free": owner_free, "free_access": promo_free,
                "free_access_until": self.free_access_until,
                "payment_required": not owner_free and not promo_free and not paid_active,
                "trading_admitted": admitted,
                "expires_at": None if owner_free else expiry, "server_time": now,
                "price_sol": "0.5", "price_lamports": str(PRICE), "days": 30,
                "packages": [{"id": key, "asset": value["asset"], "price": str(value["price"]),
                              "recipient": value["recipient"],
                              "days": value["period_seconds"] // 86400}
                             for key, value in PACKAGES.items()],
                "purchase_available": admitted,
                "sender_wallet": self.ledger.wallet_address(uid)}

    def pause_expired(self, uid=None):
        now = int(time.time())
        if self.free_access_active(now):
            return
        with self.ledger.db() as db:
            db.execute("""UPDATE custody_users SET trading=0,sniper_trading=0
                WHERE uid<>? AND NOT EXISTS(
                    SELECT 1 FROM access_memberships m WHERE m.uid=custody_users.uid AND m.expires>?)
                """ + (" AND uid=?" if uid is not None else ""),
                (int(self.runtime.owner_id), now, uid) if uid is not None else (int(self.runtime.owner_id), now))
        # close_requested is deliberately preserved: explicit user-requested exits.

    def require(self, uid, kind="BUY", position_id=None, manual=False):
        if manual and kind == "SELL":
            if any(p["id"] == position_id and p["state"] in ("OPEN", "RESERVED")
                   for p in self.ledger.positions(uid)):
                return
        if not self.state(uid)["active"]:
            self.pause_expired(uid)
            raise WalletError("Trading subscription expired or not activated. Automatic trading is stopped.", 402)

    @staticmethod
    def incoming_zrl_amount(tx, sender):
        try:
            keys = tx["transaction"]["message"]["accountKeys"]
            addresses = [key["pubkey"] if isinstance(key, dict) else key for key in keys]
            signers = {key["pubkey"] for key in keys if isinstance(key, dict) and key.get("signer")}
            meta = tx["meta"]
        except (KeyError, TypeError):
            raise WalletError("Transaction lacks complete ZRL payment evidence.", 409)
        if RECEIVER in signers or sender not in signers or sender == RECEIVER:
            raise WalletError("ZRL payment must come from your confirmed wallet.", 403)
        if meta.get("innerInstructions") is None:
            raise WalletError("RPC did not provide complete ZRL transfer instructions.", 409)

        try:
            recipient_ata = str(associated(Pubkey.from_string(RECEIVER), Pubkey.from_string(ZRL_MINT)))
            pre = {addresses[item["accountIndex"]]: item
                   for item in (meta.get("preTokenBalances") or [])}
            post = {addresses[item["accountIndex"]]: item
                    for item in (meta.get("postTokenBalances") or [])}
        except (IndexError, KeyError, TypeError, ValueError):
            raise WalletError("Transaction lacks complete ZRL token-account balances.", 409)

        instructions = tx["transaction"]["message"].get("instructions", []) + [
            instruction for group in (meta.get("innerInstructions") or [])
            for instruction in group.get("instructions", [])
        ]
        received = 0
        for instruction in instructions:
            if instruction.get("programId") != str(TOKEN):
                continue
            parsed = instruction.get("parsed") or {}
            if not isinstance(parsed, dict) or parsed.get("type") not in ("transfer", "transferChecked"):
                continue
            info = parsed.get("info") or {}
            destination = info.get("destination", "")
            target = post.get(destination, {})
            if destination != recipient_ata and target.get("owner") != RECEIVER:
                continue
            if (destination != recipient_ata or target.get("owner") != RECEIVER
                    or target.get("mint") != ZRL_MINT):
                raise WalletError("ZRL must be sent to the configured receiver's standard token account.", 409)
            source = pre.get(info.get("source", ""), {})
            source_amount = (source.get("uiTokenAmount") or {}) if source else {}
            target_amount = (target.get("uiTokenAmount") or {})
            authority = info.get("authority")
            if (authority is None and info.get("multisigAuthority") == sender
                    and info.get("signers") == [sender]):
                authority = sender
            if (source.get("owner") != sender or source.get("mint") != ZRL_MINT
                    or source_amount.get("decimals") != ZRL_DECIMALS or authority != sender):
                raise WalletError("ZRL was not sent from your confirmed wallet.", 403)
            if target_amount.get("decimals") != ZRL_DECIMALS:
                raise WalletError("Receiver ZRL token balance has unexpected decimals.", 409)
            if parsed["type"] == "transferChecked":
                amount_info = info.get("tokenAmount") or {}
                if info.get("mint") != ZRL_MINT or amount_info.get("decimals") != ZRL_DECIMALS:
                    raise WalletError("Wrong ZRL mint or token decimals.", 409)
                raw = amount_info.get("amount")
            else:
                raw = info.get("amount")
            try:
                raw = int(raw)
            except (TypeError, ValueError):
                raise WalletError("Invalid ZRL transfer amount.", 409)
            if raw <= 0 or raw > 2**63 - 1:
                raise WalletError("Invalid ZRL transfer amount.", 409)
            received += raw

        if received <= 0:
            raise WalletError("Transaction contains no incoming ZRL transfer.", 409)
        try:
            wallet_delta = WalletRuntime.delta(tx, RECEIVER, ZRL_MINT)
        except (KeyError, IndexError, TypeError, ValueError, StopIteration):
            raise WalletError("RPC did not provide complete ZRL balance changes.", 409)
        if wallet_delta != received:
            raise WalletError("The receiver's wallet-wide ZRL balance change does not match the transfer.", 409)
        return received

    def trading_request(self, request):
        uid = self.runtime.uid(request)
        if request.get("auth_method") not in ("telegram", "web"):
            raise WalletError("Sign in with Telegram or the standalone web terminal to trade.", 401)
        if not self.trading_admitted(uid):
            raise WalletError("Trading is not enabled for this account.", 403)
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
            pending = db.execute("""SELECT * FROM access_orders o WHERE uid=? AND deadline>?
                AND NOT EXISTS(SELECT 1 FROM access_receipts r WHERE r.order_id=o.id)
                ORDER BY created DESC LIMIT 1""", (uid, int(time.time()))).fetchone()
        state = self.state(uid)
        if state["free_access"] or state["admin_free"]:
            pending = None
        public_pending = self.public_order(pending) if pending else None
        return web.json_response({**state, "pending_order": public_pending,
            "recipient": public_pending["recipient"] if public_pending else None},
            headers={"Cache-Control": "no-store"})

    async def order(self, request, session):
        uid = self.runtime.uid(request)
        if self.owner(uid):
            raise WalletError("Administrator access is permanently free. No payment needed.", 409)
        if request.get("auth_method") != "web":
            raise WalletError("Subscription payments are available only on the standalone website.", 403)
        self.accounts.require(uid)
        self.runtime.admit(uid)  # Do not charge customers while public custody is closed.
        body = await request.json()
        package_id = str(body.get("package_id", "sol_30d"))
        package = PACKAGES.get(package_id)
        if not package:
            raise WalletError("Choose one of the available access packages.", 400)
        if self.free_access_active():
            raise WalletError("Free AI Trader access is still active. No payment is due yet.", 409)
        sender = self.ledger.wallet_address(uid)
        if not sender or sender in (RECEIVER, SOL_RECEIVER, self.runtime.wallet):
            raise WalletError("Confirm a separate paying wallet first. The payment receiver cannot pay itself.", 409)
        now = int(time.time())
        with self.ledger.db() as db:
            row = db.execute("""SELECT * FROM access_orders o WHERE uid=? AND deadline>?
                AND NOT EXISTS(SELECT 1 FROM access_receipts r WHERE r.order_id=o.id)
                ORDER BY created DESC LIMIT 1""", (uid, now)).fetchone()
            if row and row["package_id"] != package_id:
                raise WalletError("A payment request is already open. Complete it or wait for it to expire.", 409)
            if not row:
                used_amounts = {item["raw"] for item in db.execute("""SELECT raw FROM access_orders o
                    WHERE asset=? AND deadline>? AND NOT EXISTS(
                        SELECT 1 FROM access_receipts r WHERE r.order_id=o.id)""",
                    (package["asset"], now))}
                raw = next((package["base_raw"] + suffix for suffix in range(1, 10_001)
                            if package["base_raw"] + suffix not in used_amounts), None)
                if raw is None:
                    raise WalletError("Could not reserve a unique payment amount. Try again later.", 503)
                oid = secrets.token_urlsafe(24)
                db.execute("""INSERT INTO access_orders
                    (id,uid,sender,created,deadline,package_id,asset,raw,period_seconds,mint,recipient)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (oid, uid, sender, now, now + 3600, package_id, package["asset"],
                     raw, package["period_seconds"], package["mint"], package["recipient"]))
                row = db.execute("SELECT * FROM access_orders WHERE id=?", (oid,)).fetchone()
        return web.json_response({"success": True, "order": self.public_order(row),
                                  **self.state(uid), "recipient": row["recipient"]})

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
            raise WalletError("Create a payment request before sending payment.", 404)
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
        package = PACKAGES.get(order["package_id"])
        if (not package or order["asset"] != package["asset"] or order["mint"] != package["mint"]
                or order["period_seconds"] != package["period_seconds"]):
            raise WalletError("Payment request package data is invalid.", 409)
        valid_recipients = {package["recipient"]}
        # Older SOL invoices were issued to the original receiver; keep them payable
        # until their one-hour invoice window expires after this receiver change.
        if order["package_id"] == "sol_30d":
            valid_recipients.add(RECEIVER)
        if order["recipient"] not in valid_recipients:
            raise WalletError("Payment request receiver is invalid.", 409)
        if package["asset"] == "SOL":
            amounts = incoming_amounts(tx, order["recipient"], order["sender"])
            if amounts.get(SOL) != order["raw"]:
                raise WalletError("Send the exact native SOL amount shown for this invoice, not wrapped SOL or another token.", 409)
        elif package["asset"] == "ZRL":
            if order["recipient"] != RECEIVER or self.incoming_zrl_amount(tx, order["sender"]) != order["raw"]:
                raise WalletError("Send the exact ZRL amount shown for this invoice.", 409)
        else:
            raise WalletError("Payment asset is not supported.", 409)
        now = int(time.time())
        with self.ledger.db() as db:
            old = db.execute("SELECT * FROM access_receipts WHERE signature=? OR order_id=?",
                             (signature, order["id"])).fetchone()
            if old:
                if old["signature"] != signature or old["uid"] != uid or old["order_id"] != order["id"]:
                    raise WalletError("Payment or request has already been used.", 409)
            else:
                current = db.execute("SELECT expires FROM access_memberships WHERE uid=?", (uid,)).fetchone()
                starts = max(now, current["expires"] if current else 0)
                # A pre-existing invoice may finish during the free window; do not consume its paid days.
                if self.free_access_active(now):
                    starts = max(starts, self.free_access_until)
                expires = starts + order["period_seconds"]
                db.execute("""INSERT INTO access_receipts
                    (signature,order_id,uid,sender,raw,block_time,granted_at,asset,mint,recipient)
                    VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (signature, order["id"], uid, order["sender"], order["raw"],
                     stamp, now, order["asset"], order["mint"], order["recipient"]))
                db.execute("""INSERT INTO access_memberships VALUES(?,?)
                    ON CONFLICT(uid) DO UPDATE SET expires=excluded.expires""", (uid, expires))
        # No custody credit, no trade activation and no debit: this is an access receipt only.
        return web.json_response({**self.state(uid),
            "message": f"{order['period_seconds'] // 86400} days added. Trading modes remain unchanged."})
