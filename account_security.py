"""Telegram-owned profiles and transaction-bound, server-verified WebAuthn.

No facial images, client biometric booleans, email auth, or PIN-only withdrawal bypass.
External browser capabilities are hashed at rest and never placed in URL paths.
"""
import hashlib
import json
import os
import re
import secrets
import sqlite3
import time
import uuid
from urllib.parse import urlsplit

from aiohttp import web
from solders.pubkey import Pubkey
from webauthn import (generate_registration_options, generate_authentication_options,
    verify_registration_response, verify_authentication_response, options_to_json,
    base64url_to_bytes)
from webauthn.helpers import bytes_to_base64url
from webauthn.helpers.structs import (AuthenticatorSelectionCriteria, AuthenticatorAttachment,
    ResidentKeyRequirement, UserVerificationRequirement, PublicKeyCredentialDescriptor)
from wallet_runtime import WalletError, amount_raw


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def user_handle(uid):
    return hashlib.sha256(("zer0life-user:" + str(uid)).encode()).digest()

def backup_once(path):
    """Consistent, private same-volume backup; never overwrite an earlier backup."""
    final = path + ".pre-account-security-v1.sqlite3"
    if os.path.exists(final):
        return
    temporary = final + "." + secrets.token_hex(8) + ".tmp"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    try:
        source = sqlite3.connect(path, timeout=15)
        target = sqlite3.connect(temporary, timeout=15)
        try:
            source.backup(target)
            if target.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise ValueError("Account migration backup integrity check failed")
        finally:
            target.close()
            source.close()
        with open(temporary,"rb") as saved:
            os.fsync(saved.fileno())
        try:
            os.link(temporary,final)
        except FileExistsError:
            pass
    finally:
        os.unlink(temporary)


def intent(uid, body, vault=""):
    token = str(body.get("token", "")).upper()
    if token not in ("SOL", "USDC"):
        raise WalletError("Choose USDC or SOL.", 400)
    raw = amount_raw(body.get("amount", "0"), 9 if token == "SOL" else 6)
    try:
        recipient = Pubkey.from_string(str(body.get("recipient_address", "")))
        request_id = str(uuid.UUID(str(body.get("request_id", ""))))
    except Exception:
        raise WalletError("Invalid recipient or withdrawal request ID.", 400)
    if not recipient.is_on_curve() or str(recipient) == vault:
        raise WalletError("Use a separate recipient wallet.", 400)
    data = {"uid": uid, "token": token, "raw": str(raw),
            "recipient_address": str(recipient), "request_id": request_id}
    return data, digest(json.dumps(data, sort_keys=True, separators=(",", ":")))


class AccountSecurity:
    def __init__(self, ledger, base_url, pin_fn, vault=""):
        self.ledger, self.pin_fn, self.vault = ledger, pin_fn, vault
        parsed = urlsplit(base_url.rstrip("/"))
        if parsed.scheme != "https" or not parsed.hostname or parsed.path or parsed.query or parsed.fragment or parsed.username:
            raise ValueError("Passkeys require a fixed HTTPS public origin.")
        self.origin = parsed.scheme + "://" + parsed.netloc
        self.rp_id = parsed.hostname
        backup_once(ledger.path)
        with ledger.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS app_profiles (
                    uid INTEGER PRIMARY KEY, nickname TEXT NOT NULL,
                    nickname_key TEXT NOT NULL UNIQUE, accepted_at INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS app_passkeys (
                    id TEXT PRIMARY KEY, uid INTEGER NOT NULL, public_key BLOB NOT NULL,
                    counter INTEGER NOT NULL, created_at INTEGER NOT NULL);
                CREATE INDEX IF NOT EXISTS app_passkeys_owner ON app_passkeys(uid);
                CREATE TABLE IF NOT EXISTS app_recovery (
                    uid INTEGER NOT NULL, hash TEXT NOT NULL, PRIMARY KEY(uid,hash));
                CREATE TABLE IF NOT EXISTS app_passkey_tickets (
                    hash TEXT PRIMARY KEY, uid INTEGER NOT NULL, purpose TEXT NOT NULL,
                    phase TEXT NOT NULL, challenge BLOB NOT NULL, payload TEXT NOT NULL,
                    intent_hash TEXT NOT NULL, recovery_hash TEXT NOT NULL,
                    expires INTEGER NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                    used INTEGER NOT NULL DEFAULT 0);
            """)

    def state(self, uid):
        with self.ledger.db() as db:
            row = db.execute("SELECT nickname FROM app_profiles WHERE uid=?", (uid,)).fetchone()
            keys = db.execute("SELECT COUNT(*) FROM app_passkeys WHERE uid=?", (uid,)).fetchone()[0]
            recovery = db.execute("SELECT COUNT(*) FROM app_recovery WHERE uid=?", (uid,)).fetchone()[0]
        return {"success": True, "registered": bool(row), "nickname": row["nickname"] if row else None,
                "has_passkey": keys > 0, "passkey_count": keys, "recovery_codes_remaining": recovery,
                "withdrawal_requires_passkey": True}

    def require(self, uid):
        if not self.state(uid)["registered"]:
            raise WalletError("Create your Telegram account profile first.", 409)

    async def status(self, request, session=None):
        return web.json_response(self.state(int(request["telegram_user"]["id"])), headers={"Cache-Control":"no-store"})

    async def register(self, request, session=None):
        uid = int(request["telegram_user"]["id"])
        body = await request.json()
        nick = str(body.get("nickname", "")).strip()
        if not re.fullmatch(r"[A-Za-z0-9_]{3,24}", nick) or nick.lower() in {"admin","administrator","support","system","zer0life"}:
            raise WalletError("Use 3–24 letters, numbers or underscores; this nickname may be reserved.", 400)
        if body.get("accept_risk") is not True:
            raise WalletError("Acknowledge that trading can lose money.", 400)
        with self.ledger.db() as db:
            old = db.execute("SELECT nickname FROM app_profiles WHERE uid=?", (uid,)).fetchone()
            if not old:
                if db.execute("SELECT uid FROM app_profiles WHERE nickname_key=?", (nick.lower(),)).fetchone():
                    raise WalletError("This nickname is already taken.", 409)
                db.execute("INSERT INTO app_profiles VALUES(?,?,?,?)", (uid,nick,nick.lower(),int(time.time())))
        # Never create/reassign financial ownership based on nickname or request body UID.
        return await self.status(request)

    def pin(self, uid, supplied):
        valid, state = self.pin_fn(uid, str(supplied or ""))
        if not valid:
            raise WalletError("Invalid withdrawal code or code locked.", 423 if state == "locked" else 401)

    def ticket(self, db, nonce, uid=None, active=True, attempts_limit=8):
        if not isinstance(nonce, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", nonce):
            raise WalletError("Invalid passkey link.", 404)
        row = db.execute("SELECT * FROM app_passkey_tickets WHERE hash=?", (digest(nonce),)).fetchone()
        if not row or (uid is not None and row["uid"] != uid):
            raise WalletError("Passkey link not found.", 404)
        if active and (row["used"] or row["expires"] <= int(time.time()) or row["attempts"] >= attempts_limit):
            raise WalletError("Passkey link expired or already used. Start again in Telegram.", 410)
        return dict(row)

    async def create_ticket(self, request, session=None):
        uid = int(request["telegram_user"]["id"])
        self.require(uid)
        body = await request.json()
        self.pin(uid, body.get("pin"))
        purpose = body.get("purpose")
        if purpose not in ("enroll","recover","withdraw"):
            raise WalletError("Invalid passkey action.", 400)
        payload, binding, recovery = {}, "", ""
        if purpose == "withdraw":
            payload, binding = intent(uid, body, self.vault)
        if purpose == "recover":
            recovery = digest(str(uid) + ":" + str(body.get("recovery_code","")).strip())
        now = int(time.time())
        nonce = secrets.token_urlsafe(32)
        with self.ledger.db() as db:
            db.execute("DELETE FROM app_passkey_tickets WHERE expires<?", (now-86400,))
            keys = db.execute("SELECT COUNT(*) FROM app_passkeys WHERE uid=?", (uid,)).fetchone()[0]
            if purpose == "withdraw" and not keys:
                raise WalletError("Set up your passkey before withdrawing.", 428)
            if purpose == "enroll" and keys >= 5:
                raise WalletError("Five passkeys are already registered.", 409)
            if purpose == "recover" and not db.execute(
                    "SELECT 1 FROM app_recovery WHERE uid=? AND hash=?", (uid,recovery)).fetchone():
                raise WalletError("Invalid or used recovery code.", 401)
            if db.execute("SELECT COUNT(*) FROM app_passkey_tickets WHERE uid=? AND used=0 AND expires>?",
                          (uid,now)).fetchone()[0] >= 3:
                raise WalletError("Three verification links are active. Wait five minutes.", 429)
            actual = ("add" if keys else "first") if purpose == "enroll" else purpose
            phase = "auth" if actual in ("add","withdraw") else "register"
            db.execute("INSERT INTO app_passkey_tickets(hash,uid,purpose,phase,challenge,payload,intent_hash,recovery_hash,expires) VALUES(?,?,?,?,?,?,?,?,?)",
                (digest(nonce),uid,actual,phase,secrets.token_bytes(32),json.dumps(payload),binding,recovery,now+300))
        return web.json_response({"success":True,"ticket":nonce,"url":self.origin+"/passkey#ticket="+nonce,
                                  "expires_in":300}, headers={"Cache-Control":"no-store"})

    async def ticket_status(self, request, session=None):
        with self.ledger.db() as db:
            row = self.ticket(db, request.query.get("ticket"), int(request["telegram_user"]["id"]), active=False)
        return web.json_response({"success":True,"phase":row["phase"],"used":bool(row["used"]),
                                 "expired":row["expires"]<=int(time.time())},headers={"Cache-Control":"no-store"})

    def browser(self, request):
        if request.headers.get("Origin") != self.origin:
            raise WalletError("This verification must run on the official app origin.", 403)

    def options(self, db, row):
        uid = row["uid"]
        keys = [PublicKeyCredentialDescriptor(id=base64url_to_bytes(r["id"]))
                for r in db.execute("SELECT id FROM app_passkeys WHERE uid=?", (uid,))]
        if row["phase"] == "auth":
            opts = generate_authentication_options(rp_id=self.rp_id, challenge=row["challenge"],
                allow_credentials=keys, user_verification=UserVerificationRequirement.REQUIRED, timeout=60000)
        elif row["phase"] == "register":
            profile = db.execute("SELECT nickname FROM app_profiles WHERE uid=?", (uid,)).fetchone()
            opts = generate_registration_options(rp_id=self.rp_id,rp_name="Zer0Life",
                user_id=user_handle(uid),user_name=profile["nickname"],challenge=row["challenge"],
                exclude_credentials=keys, timeout=60000,
                authenticator_selection=AuthenticatorSelectionCriteria(
                    authenticator_attachment=AuthenticatorAttachment.PLATFORM,
                    resident_key=ResidentKeyRequirement.REQUIRED,
                    user_verification=UserVerificationRequirement.REQUIRED))
        else:
            raise WalletError("Verification already completed.", 409)
        return {"success":True,"phase":row["phase"],"purpose":row["purpose"],
                "options":json.loads(options_to_json(opts)),"intent":json.loads(row["payload"])}

    async def begin(self, request, session=None):
        self.browser(request)
        body = await request.json()
        with self.ledger.db() as db:
            row = self.ticket(db, body.get("ticket"))
        self.pin(row["uid"], body.get("pin"))
        with self.ledger.db() as db:
            row = self.ticket(db, body.get("ticket"))
            result = self.options(db,row)
        return web.json_response(result,headers={"Cache-Control":"no-store"})

    async def complete(self, request, session=None):
        self.browser(request)
        body = await request.json()
        with self.ledger.db() as db:
            initial = self.ticket(db,body.get("ticket"))
        self.pin(initial["uid"],body.get("pin"))
        # Attempt count commits even when cryptographic verification fails.
        with self.ledger.db() as db:
            row = self.ticket(db,body.get("ticket"))
            db.execute("UPDATE app_passkey_tickets SET attempts=attempts+1 WHERE hash=?", (row["hash"],))
        recovery_codes = []
        try:
            with self.ledger.db() as db:
                row = self.ticket(db,body.get("ticket"),attempts_limit=9)
                uid = row["uid"]
                credential = body.get("credential")
                if not isinstance(credential,dict):
                    raise ValueError("Credential required")
                if row["phase"] == "auth":
                    key = db.execute("SELECT * FROM app_passkeys WHERE id=? AND uid=?",
                        (bytes_to_base64url(base64url_to_bytes(credential.get("id",""))),uid)).fetchone()
                    if not key:
                        raise ValueError("Wrong account credential")
                    handle = credential.get("response",{}).get("userHandle")
                    if handle is not None and base64url_to_bytes(handle) != user_handle(uid):
                        raise ValueError("Wrong account handle")
                    verified = verify_authentication_response(credential=credential,
                        expected_challenge=row["challenge"],expected_rp_id=self.rp_id,expected_origin=self.origin,
                        credential_public_key=key["public_key"],credential_current_sign_count=key["counter"],
                        require_user_verification=True)
                    db.execute("UPDATE app_passkeys SET counter=? WHERE id=?", (verified.new_sign_count,key["id"]))
                    if row["purpose"] == "withdraw":
                        db.execute("UPDATE app_passkey_tickets SET phase='approved' WHERE hash=?", (row["hash"],))
                        result = {"success":True,"phase":"approved"}
                    else:
                        db.execute("UPDATE app_passkey_tickets SET phase='register',challenge=? WHERE hash=?",
                                   (secrets.token_bytes(32),row["hash"]))
                        result = self.options(db,self.ticket(db,body["ticket"]))
                elif row["phase"] == "register":
                    count = db.execute("SELECT COUNT(*) FROM app_passkeys WHERE uid=?", (uid,)).fetchone()[0]
                    if row["purpose"] == "first" and count:
                        raise ValueError("An existing passkey must authorize additional enrollment")
                    if row["purpose"] == "add" and count >= 5:
                        raise ValueError("Passkey limit")
                    if row["purpose"] == "recover" and not db.execute(
                            "SELECT 1 FROM app_recovery WHERE uid=? AND hash=?", (uid,row["recovery_hash"])).fetchone():
                        raise ValueError("Recovery code already consumed")
                    verified = verify_registration_response(credential=credential,
                        expected_challenge=row["challenge"],expected_rp_id=self.rp_id,
                        expected_origin=self.origin,require_user_verification=True)
                    if row["purpose"] == "recover":
                        db.execute("DELETE FROM app_passkeys WHERE uid=?", (uid,))
                        db.execute("DELETE FROM app_recovery WHERE uid=?", (uid,))
                        db.execute("UPDATE app_passkey_tickets SET used=1 WHERE uid=? AND hash<>?", (uid,row["hash"]))
                    db.execute("INSERT INTO app_passkeys VALUES(?,?,?,?,?)",
                        (bytes_to_base64url(verified.credential_id),uid,verified.credential_public_key,
                         verified.sign_count,int(time.time())))
                    if row["purpose"] in ("first","recover"):
                        recovery_codes = [secrets.token_urlsafe(24) for _ in range(8)]
                        db.executemany("INSERT INTO app_recovery VALUES(?,?)",
                            [(uid,digest(str(uid)+":"+code)) for code in recovery_codes])
                    db.execute("UPDATE app_passkey_tickets SET phase='enrolled',used=1 WHERE hash=?", (row["hash"],))
                    result = {"success":True,"phase":"enrolled","recovery_codes":recovery_codes}
                else:
                    raise ValueError("Already completed")
        except WalletError:
            raise
        except Exception:
            raise WalletError("Passkey verification failed. Use the correct account and device; no funds were sent.", 401)
        return web.json_response(result,headers={"Cache-Control":"no-store"})

    def consume(self, uid, body):
        self.require(uid)
        _, binding = intent(uid,body,self.vault)
        with self.ledger.db() as db:
            row = self.ticket(db,body.get("passkey_ticket"),uid)
            if row["purpose"] != "withdraw" or row["phase"] != "approved" or row["intent_hash"] != binding:
                raise WalletError("Passkey approval does not match this exact withdrawal.", 403)
            db.execute("UPDATE app_passkey_tickets SET used=1 WHERE hash=?", (row["hash"],))

    async def browser_handle(self, request):
        try:
            if (request.content_length is not None and request.content_length > 65536) or len(await request.read()) > 65536:
                raise WalletError("Invalid verification body size.", 413)
            action = self.begin if request.path.endswith("/options") else self.complete
            return await action(request)
        except WalletError as exc:
            return web.json_response({"success":False,"message":str(exc)},status=exc.status,
                                     headers={"Cache-Control":"no-store"})
        except Exception:
            return web.json_response({"success":False,"message":"Verification unavailable. No funds were sent."},
                                     status=400,headers={"Cache-Control":"no-store"})
