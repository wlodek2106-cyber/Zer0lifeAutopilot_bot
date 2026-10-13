"""Integer, double-entry custody ledger. No legacy virtual balance migration."""
import json
import os
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager

USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SOL = "SOL"
FEE_CAP = 5_000_000
ACTIVE = ("RESERVED", "PREPARED", "SUBMITTED", "UNKNOWN")


class LedgerError(Exception):
    pass


def available(uid):
    return "user:" + str(int(uid))


def reserve_account(oid):
    return "operation:" + oid


def position_account(pid):
    return "position:" + pid


class CustodyLedger:
    def __init__(self, path):
        self.path = path
        if os.path.isfile(path) and os.path.getsize(path):
            source = sqlite3.connect(path, timeout=15)
            try:
                migrated = source.execute("SELECT name FROM sqlite_master WHERE name='custody_accounts'").fetchone()
                backup_path = path + ".pre-custody.bak"
                if not migrated and not os.path.exists(backup_path):
                    target = sqlite3.connect(backup_path)
                    try:
                        source.backup(target)
                    finally:
                        target.close()
                    os.chmod(backup_path, 0o600)
            finally:
                source.close()
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS custody_users (
                    uid INTEGER PRIMARY KEY CHECK(uid>0), username TEXT NOT NULL,
                    trading INTEGER NOT NULL DEFAULT 0, close_requested INTEGER NOT NULL DEFAULT 0,
                    order_raw INTEGER NOT NULL DEFAULT 1000000,
                    created_at INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS custody_accounts (
                    account TEXT NOT NULL, asset TEXT NOT NULL, raw INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(account,asset), CHECK(raw>=0 OR account='external'));
                CREATE TABLE IF NOT EXISTS custody_journals (
                    id TEXT PRIMARY KEY, note TEXT NOT NULL, created_at INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS custody_entries (
                    id INTEGER PRIMARY KEY, journal TEXT NOT NULL REFERENCES custody_journals(id),
                    account TEXT NOT NULL, asset TEXT NOT NULL, delta INTEGER NOT NULL);
                CREATE TRIGGER IF NOT EXISTS custody_entries_immutable_update
                    BEFORE UPDATE ON custody_entries BEGIN SELECT RAISE(ABORT,'immutable ledger'); END;
                CREATE TRIGGER IF NOT EXISTS custody_entries_immutable_delete
                    BEFORE DELETE ON custody_entries BEGIN SELECT RAISE(ABORT,'immutable ledger'); END;
                CREATE TRIGGER IF NOT EXISTS custody_journals_immutable_update
                    BEFORE UPDATE ON custody_journals BEGIN SELECT RAISE(ABORT,'immutable ledger'); END;
                CREATE TRIGGER IF NOT EXISTS custody_journals_immutable_delete
                    BEFORE DELETE ON custody_journals BEGIN SELECT RAISE(ABORT,'immutable ledger'); END;
                CREATE TABLE IF NOT EXISTS custody_invoices (
                    id TEXT PRIMARY KEY, uid INTEGER NOT NULL REFERENCES custody_users(uid),
                    asset TEXT NOT NULL, raw INTEGER NOT NULL CHECK(raw>0),
                    created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL,
                    signature TEXT UNIQUE, state TEXT NOT NULL DEFAULT 'OPEN');
                CREATE TABLE IF NOT EXISTS custody_operations (
                    id TEXT PRIMARY KEY, request_key TEXT NOT NULL UNIQUE,
                    uid INTEGER NOT NULL REFERENCES custody_users(uid), kind TEXT NOT NULL,
                    asset TEXT NOT NULL, raw INTEGER NOT NULL CHECK(raw>0),
                    fee_cap INTEGER NOT NULL, recipient TEXT NOT NULL DEFAULT '',
                    position_id TEXT, metadata TEXT NOT NULL,
                    signature TEXT UNIQUE, wire TEXT, state TEXT NOT NULL,
                    created_at INTEGER NOT NULL);
                CREATE UNIQUE INDEX IF NOT EXISTS custody_one_active_operation
                    ON custody_operations((1)) WHERE state IN ('RESERVED','PREPARED','SUBMITTED','UNKNOWN');
                CREATE TABLE IF NOT EXISTS custody_positions (
                    id TEXT PRIMARY KEY, uid INTEGER NOT NULL REFERENCES custody_users(uid),
                    asset TEXT NOT NULL, symbol TEXT NOT NULL, decimals INTEGER NOT NULL,
                    raw INTEGER NOT NULL CHECK(raw>0), cost_usdc INTEGER NOT NULL CHECK(cost_usdc>0),
                    state TEXT NOT NULL, buy_tx TEXT NOT NULL, sell_tx TEXT,
                    proceeds_usdc INTEGER, created_at INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS custody_scan_cursor (
                    address TEXT PRIMARY KEY, signature TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS custody_seen_signatures (
                    signature TEXT PRIMARY KEY, created_at INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS custody_scan_progress (
                    address TEXT PRIMARY KEY, newest_signature TEXT NOT NULL,
                    boundary_signature TEXT, before_signature TEXT NOT NULL,
                    cutoff INTEGER NOT NULL);
            """)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA synchronous=FULL")
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def post(db, jid, note, lines):
        if db.execute("SELECT id FROM custody_journals WHERE id=?", (jid,)).fetchone():
            return False
        totals = {}
        for account, asset, delta in lines:
            if not isinstance(delta, int) or abs(delta) > 2**63 - 1:
                raise LedgerError("Сумма вне диапазона целочисленного реестра.")
            totals[asset] = totals.get(asset, 0) + delta
        if not lines or any(totals.values()):
            raise LedgerError("Несбалансированная проводка.")
        changes = {}
        for account, asset, delta in lines:
            changes[(account, asset)] = changes.get((account, asset), 0) + delta
        for (account, asset), delta in changes.items():
            old = db.execute("SELECT raw FROM custody_accounts WHERE account=? AND asset=?", (account, asset)).fetchone()
            new = (old["raw"] if old else 0) + delta
            if account != "external" and new < 0:
                raise LedgerError("Недостаточно собственных средств или резерва комиссий.")
            db.execute("INSERT INTO custody_accounts(account,asset,raw) VALUES(?,?,?) "
                       "ON CONFLICT(account,asset) DO UPDATE SET raw=excluded.raw", (account, asset, new))
        db.execute("INSERT INTO custody_journals VALUES(?,?,?)", (jid, note, int(time.time())))
        db.executemany("INSERT INTO custody_entries(journal,account,asset,delta) VALUES(?,?,?,?)",
                       [(jid, account, asset, delta) for account, asset, delta in lines])
        return True

    def user(self, uid, username=""):
        uid = int(uid)
        if uid <= 0:
            raise LedgerError("Неверный пользователь.")
        with self.db() as db:
            db.execute("""INSERT INTO custody_users(uid,username,created_at) VALUES(?,?,?)
                ON CONFLICT(uid) DO UPDATE SET username=excluded.username""", (uid, str(username)[:128], int(time.time())))

    def balance(self, uid, asset):
        with self.db() as db:
            row = db.execute("SELECT raw FROM custody_accounts WHERE account=? AND asset=?",
                             (available(uid), asset)).fetchone()
            return row["raw"] if row else 0

    def liabilities(self):
        with self.db() as db:
            return {r["asset"]: r["raw"] for r in db.execute(
                "SELECT asset,SUM(raw) raw FROM custody_accounts WHERE account!='external' GROUP BY asset")}

    def invoice(self, uid, asset, raw):
        now = int(time.time())
        iid = secrets.token_urlsafe(24)
        with self.db() as db:
            count = db.execute("SELECT COUNT(*) FROM custody_invoices WHERE uid=? AND state='OPEN' AND expires_at>?",
                               (uid, now)).fetchone()[0]
            if count >= 10:
                raise LedgerError("Слишком много открытых запросов пополнения.")
            db.execute("INSERT INTO custody_invoices(id,uid,asset,raw,created_at,expires_at) VALUES(?,?,?,?,?,?)",
                       (iid, uid, asset, raw, now, now + 3600))
        return self.get_invoice(iid)

    def get_invoice(self, iid):
        with self.db() as db:
            row = db.execute("SELECT * FROM custody_invoices WHERE id=?", (iid,)).fetchone()
            return dict(row) if row else None

    def credit_deposit(self, iid, signature, asset, raw, block_time):
        with self.db() as db:
            invoice = db.execute("SELECT * FROM custody_invoices WHERE id=?", (iid,)).fetchone()
            if not invoice or invoice["asset"] != asset or invoice["raw"] != raw:
                raise LedgerError("Депозит не соответствует платёжному запросу.")
            if not invoice["created_at"] - 60 <= block_time <= invoice["expires_at"]:
                raise LedgerError("Платёж совершён вне срока действия запроса.")
            if invoice["signature"]:
                if invoice["signature"] == signature:
                    return invoice["uid"]
                raise LedgerError("Этот платёжный запрос уже оплачен.")
            if db.execute("SELECT id FROM custody_invoices WHERE signature=?", (signature,)).fetchone():
                raise LedgerError("Транзакция уже использована.")
            self.post(db, "deposit:" + signature, "confirmed deposit", [
                ("external", asset, -raw), (available(invoice["uid"]), asset, raw)])
            db.execute("UPDATE custody_invoices SET signature=?,state='CREDITED' WHERE id=?", (signature, iid))
            return invoice["uid"]

    def seed_fees(self, raw, backing, owner_uid=None):
        if raw <= 0:
            raise LedgerError("Неверная сумма резерва.")
        with self.db() as db:
            if db.execute("SELECT id FROM custody_operations WHERE state IN ('RESERVED','PREPARED','SUBMITTED','UNKNOWN')").fetchone():
                raise LedgerError("Сначала нужно подтвердить предыдущую операцию.")
            liabilities = db.execute("SELECT COALESCE(SUM(raw),0) FROM custody_accounts WHERE asset=? AND account!='external'", (SOL,)).fetchone()[0]
            own = db.execute("SELECT raw FROM custody_accounts WHERE account=? AND asset=?",
                             (available(owner_uid), SOL)).fetchone() if owner_uid is not None else None
            own_raw = min(raw, own["raw"] if own else 0)
            unassigned = raw - own_raw
            if backing < liabilities + unassigned:
                raise LedgerError("Нельзя выделить комиссию из пользовательских средств.")
            lines = [("treasury", SOL, raw)]
            if own_raw:
                lines.append((available(owner_uid), SOL, -own_raw))
            if unassigned:
                lines.append(("external", SOL, -unassigned))
            self.post(db, "fees:" + str(uuid.uuid4()), "operator allocates only own/unassigned SOL", lines)

    def opening(self, uid, asset, raw, backing):
        with self.db() as db:
            if db.execute("SELECT id FROM custody_operations WHERE state IN ('RESERVED','PREPARED','SUBMITTED','UNKNOWN')").fetchone():
                raise LedgerError("Есть неопределённая операция.")
            if raw <= 0 or asset not in (USDC, SOL):
                raise LedgerError("Неверный актив или сумма.")
            jid = "opening:" + asset
            if db.execute("SELECT id FROM custody_journals WHERE id=?", (jid,)).fetchone():
                raise LedgerError("Открывающий баланс этого актива уже перенесён.")
            claimed = db.execute("SELECT COALESCE(SUM(raw),0) FROM custody_accounts WHERE asset=? AND account!='external'", (asset,)).fetchone()[0]
            if backing < claimed + raw:
                raise LedgerError("Нет нераспределённого остатка для переноса.")
            self.post(db, jid, "operator confirms old unassigned own funds", [
                ("external", asset, -raw), (available(uid), asset, raw)])

    def find_request(self, uid, request_id):
        with self.db() as db:
            row = db.execute("SELECT * FROM custody_operations WHERE request_key=?", (str(uid) + ":" + request_id,)).fetchone()
            return dict(row) if row else None

    def operation(self, oid):
        with self.db() as db:
            row = db.execute("SELECT * FROM custody_operations WHERE id=?", (oid,)).fetchone()
            return dict(row) if row else None

    def pending(self):
        with self.db() as db:
            return [dict(r) for r in db.execute(
                "SELECT * FROM custody_operations WHERE state IN ('RESERVED','PREPARED','SUBMITTED','UNKNOWN')")]

    def reserve(self, uid, request_id, kind, asset, raw, metadata, recipient="", position_id=None):
        oid = str(uuid.uuid4())
        with self.db() as db:
            previous = db.execute("SELECT * FROM custody_operations WHERE request_key=?",
                                  (str(uid) + ":" + request_id,)).fetchone()
            if previous:
                return dict(previous)
            if db.execute("SELECT id FROM custody_operations WHERE state IN ('RESERVED','PREPARED','SUBMITTED','UNKNOWN')").fetchone():
                raise LedgerError("Кошелёк ожидает подтверждения предыдущей операции.")
            source = available(uid)
            if kind == "SELL":
                position = db.execute("SELECT * FROM custody_positions WHERE id=? AND uid=? AND state='OPEN'",
                                      (position_id, uid)).fetchone()
                if not position or position["asset"] != asset or position["raw"] != raw:
                    raise LedgerError("Позиция не принадлежит пользователю или недоступна.")
                source = position_account(position_id)
                db.execute("UPDATE custody_positions SET state='RESERVED' WHERE id=?", (position_id,))
            self.post(db, "reserve:" + oid, "reserve owner funds and operator fees", [
                (source, asset, -raw), (reserve_account(oid), asset, raw),
                ("treasury", SOL, -FEE_CAP), (reserve_account(oid), SOL, FEE_CAP)])
            db.execute("""INSERT INTO custody_operations
                (id,request_key,uid,kind,asset,raw,fee_cap,recipient,position_id,metadata,state,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,'RESERVED',?)""",
                (oid, str(uid) + ":" + request_id, uid, kind, asset, raw, FEE_CAP,
                 recipient, position_id, json.dumps(metadata), int(time.time())))
        return self.operation(oid)

    def prepared(self, oid, signature, wire):
        with self.db() as db:
            changed = db.execute("""UPDATE custody_operations SET signature=?,wire=?,state='PREPARED'
                WHERE id=? AND state='RESERVED'""", (signature, wire, oid)).rowcount
            if changed != 1:
                raise LedgerError("Операция уже обработана.")

    def submitted(self, oid, known):
        with self.db() as db:
            db.execute("UPDATE custody_operations SET state=? WHERE id=? AND state='PREPARED'",
                       ("SUBMITTED" if known else "UNKNOWN", oid))

    def fail(self, oid, network_proven=False, fee=0):
        with self.db() as db:
            row = db.execute("SELECT * FROM custody_operations WHERE id=?", (oid,)).fetchone()
            if not row or row["state"] in ("FAILED", "COMPLETED"):
                return
            if row["state"] != "RESERVED" and not network_proven:
                raise LedgerError("Нельзя возвращать резерв: исход транзакции неизвестен.")
            if not isinstance(fee, int) or fee < 0 or fee > row["fee_cap"]:
                raise LedgerError("Комиссия не соответствует резерву.")
            source = position_account(row["position_id"]) if row["kind"] == "SELL" else available(row["uid"])
            self.post(db, "failed:" + oid, "release only proven unsent/failed", [
                (reserve_account(oid), row["asset"], -row["raw"]), (source, row["asset"], row["raw"]),
                (reserve_account(oid), SOL, -row["fee_cap"]), ("treasury", SOL, row["fee_cap"] - fee),
                ("external", SOL, fee)])
            if row["kind"] == "SELL":
                db.execute("UPDATE custody_positions SET state='OPEN' WHERE id=?", (row["position_id"],))
            db.execute("UPDATE custody_operations SET state='FAILED' WHERE id=?", (oid,))

    def finalize(self, oid, spent, fee, received=0):
        with self.db() as db:
            row = db.execute("SELECT * FROM custody_operations WHERE id=?", (oid,)).fetchone()
            if not row or row["state"] == "COMPLETED":
                return
            if row["state"] not in ("PREPARED", "SUBMITTED", "UNKNOWN"):
                raise LedgerError("Неверное состояние финализации.")
            if not isinstance(fee, int) or fee > row["fee_cap"] or spent <= 0 or spent > row["raw"]:
                raise LedgerError("Фактическая сумма выходит за зарезервированные пределы.")
            info = json.loads(row["metadata"])
            account = reserve_account(oid)
            source = position_account(row["position_id"]) if row["kind"] == "SELL" else available(row["uid"])
            lines = [(account, row["asset"], -spent), ("external", row["asset"], spent),
                (account, SOL, -fee), ("external", SOL, fee),
                (account, SOL, -(row["fee_cap"] - fee)), ("treasury", SOL, row["fee_cap"] - fee)]
            if row["raw"] > spent:
                lines += [(account, row["asset"], -(row["raw"] - spent)), (source, row["asset"], row["raw"] - spent)]
            if row["kind"] in ("BUY", "SELL"):
                if received <= 0:
                    raise LedgerError("Нет подтверждённого результата swap.")
                output = info["output_asset"]
                target = position_account(oid) if row["kind"] == "BUY" else available(row["uid"])
                lines += [("external", output, -received), (target, output, received)]
            self.post(db, "complete:" + oid, "finalized blockchain effects", lines)
            if row["kind"] == "BUY":
                db.execute("""INSERT INTO custody_positions
                    (id,uid,asset,symbol,decimals,raw,cost_usdc,state,buy_tx,created_at)
                    VALUES(?,?,?,?,?,?,?,'OPEN',?,?)""",
                    (oid, row["uid"], info["output_asset"], info["symbol"], info["decimals"],
                     received, spent, row["signature"], int(time.time())))
            elif row["kind"] == "SELL":
                if spent != row["raw"]:
                    raise LedgerError("Частичная продажа требует отдельного разбора.")
                db.execute("""UPDATE custody_positions SET state='CLOSED',sell_tx=?,proceeds_usdc=?
                    WHERE id=? AND uid=?""", (row["signature"], received, row["position_id"], row["uid"]))
            db.execute("UPDATE custody_operations SET state='COMPLETED' WHERE id=?", (oid,))

    def positions(self, uid, state=None):
        with self.db() as db:
            rows = db.execute("SELECT * FROM custody_positions WHERE uid=?" +
                (" AND state=?" if state else "") + " ORDER BY created_at DESC",
                (uid, state) if state else (uid,))
            return [dict(r) for r in rows]

    def settings(self, uid, enabled=None, raw=None, close=None):
        with self.db() as db:
            if enabled is not None:
                db.execute("UPDATE custody_users SET trading=?,close_requested=0 WHERE uid=?", (int(enabled), uid))
            if raw is not None:
                db.execute("UPDATE custody_users SET order_raw=? WHERE uid=?", (raw, uid))
            if close is not None:
                db.execute("UPDATE custody_users SET close_requested=? WHERE uid=?", (int(close), uid))
            row = db.execute("SELECT * FROM custody_users WHERE uid=?", (uid,)).fetchone()
            return dict(row) if row else None

    def active_users(self):
        with self.db() as db:
            return [dict(r) for r in db.execute("SELECT * FROM custody_users WHERE trading=1 OR close_requested=1")]

    def restart_pause(self):
        with self.db() as db:
            db.execute("UPDATE custody_users SET trading=0,close_requested=0")
            # RESERVED has never been broadcast, by contract; PREPARED is ambiguous.
            unsent = [r[0] for r in db.execute("SELECT id FROM custody_operations WHERE state='RESERVED'")]
        for oid in unsent:
            self.fail(oid)

    def audit(self):
        with self.db() as db:
            unbalanced = list(db.execute(
                "SELECT journal,asset,SUM(delta) total FROM custody_entries GROUP BY journal,asset HAVING total!=0"))
            mismatch = list(db.execute("""SELECT a.account,a.asset,a.raw,COALESCE(e.total,0) total
                FROM custody_accounts a LEFT JOIN
                (SELECT account,asset,SUM(delta) total FROM custody_entries GROUP BY account,asset) e
                ON a.account=e.account AND a.asset=e.asset WHERE a.raw!=COALESCE(e.total,0)"""))
            if unbalanced or mismatch:
                raise LedgerError("Проверка целостности реестра не пройдена.")
            return True
