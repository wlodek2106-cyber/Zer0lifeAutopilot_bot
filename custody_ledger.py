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
                if migrated and not any(row[1] == "funding_asset" for row in source.execute("PRAGMA table_info(custody_positions)")):
                    ai_backup = path + ".pre-ai.bak"
                    if not os.path.exists(ai_backup):
                        target = sqlite3.connect(ai_backup)
                        try:
                            source.backup(target)
                        finally:
                            target.close()
                        os.chmod(ai_backup, 0o600)
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
                CREATE TABLE IF NOT EXISTS custody_wallet_links (
                    uid INTEGER PRIMARY KEY, address TEXT NOT NULL UNIQUE, created_at INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS custody_wallet_proofs (
                    nonce TEXT PRIMARY KEY, uid INTEGER NOT NULL, message TEXT NOT NULL,
                    expires_at INTEGER NOT NULL, used INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS custody_tx_deposits (
                    signature TEXT PRIMARY KEY, uid INTEGER NOT NULL, sender TEXT NOT NULL,
                    amounts TEXT NOT NULL, block_time INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS custody_tx_epoch (
                    id INTEGER PRIMARY KEY CHECK(id=1), started_at INTEGER NOT NULL);
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
                CREATE TABLE IF NOT EXISTS custody_ai_calls (
                    id TEXT PRIMARY KEY, created_at INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS custody_ai_analyses (
                    id TEXT PRIMARY KEY, created_at INTEGER NOT NULL, decisions TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS custody_ai_attempts (
                    analysis_id TEXT NOT NULL, uid INTEGER NOT NULL, mode TEXT NOT NULL,
                    PRIMARY KEY(analysis_id,uid,mode));
                CREATE TABLE IF NOT EXISTS custody_activity_events (
                    uid INTEGER NOT NULL, analysis_id TEXT NOT NULL, mode TEXT NOT NULL,
                    code TEXT NOT NULL, message TEXT NOT NULL, created_at INTEGER NOT NULL,
                    PRIMARY KEY(uid,analysis_id,mode,code));
                CREATE INDEX IF NOT EXISTS custody_activity_by_user
                    ON custody_activity_events(uid,created_at DESC);
            """)
            for table, additions in {
                "custody_users": {
                    "sniper_trading": "INTEGER NOT NULL DEFAULT 0",
                    "sniper_order_raw": "INTEGER NOT NULL DEFAULT 5000000"},
                "custody_positions": {
                    "funding_asset": "TEXT NOT NULL DEFAULT '" + USDC + "'",
                    "cost_raw": "INTEGER", "proceeds_raw": "INTEGER"},
            }.items():
                columns = {row[1] for row in db.execute("PRAGMA table_info(" + table + ")")}
                for name, definition in additions.items():
                    if name not in columns:
                        db.execute("ALTER TABLE " + table + " ADD COLUMN " + name + " " + definition)
            db.execute("UPDATE custody_positions SET cost_raw=cost_usdc,proceeds_raw=proceeds_usdc WHERE cost_raw IS NULL")

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

    def wallet_address(self, uid):
        with self.db() as db:
            row = db.execute("SELECT address FROM custody_wallet_links WHERE uid=?", (uid,)).fetchone()
            return row["address"] if row else None

    def tx_epoch(self):
        with self.db() as db:
            earliest = db.execute("SELECT MIN(created_at) FROM custody_users").fetchone()[0]
            db.execute("INSERT OR IGNORE INTO custody_tx_epoch VALUES(1,?)", (earliest or int(time.time()),))
            return db.execute("SELECT started_at FROM custody_tx_epoch WHERE id=1").fetchone()[0]

    def wallet_challenge(self, uid, origin):
        nonce = secrets.token_urlsafe(24)
        message = f"Zer0life wallet ownership only — NO TRANSFER\nOrigin: {origin}\nTelegram account: {uid}\nNonce: {nonce}"
        with self.db() as db:
            db.execute("DELETE FROM custody_wallet_proofs WHERE expires_at<?", (int(time.time()),))
            db.execute("DELETE FROM custody_wallet_proofs WHERE uid=? AND used=0", (uid,))
            db.execute("INSERT INTO custody_wallet_proofs(nonce,uid,message,expires_at) VALUES(?,?,?,?)",
                       (nonce, uid, message, int(time.time()) + 600))
        return nonce

    def wallet_proof(self, nonce):
        with self.db() as db:
            row = db.execute("SELECT * FROM custody_wallet_proofs WHERE nonce=?", (nonce,)).fetchone()
            return dict(row) if row else None

    def link_wallet(self, nonce, address):
        with self.db() as db:
            proof = db.execute("SELECT * FROM custody_wallet_proofs WHERE nonce=?", (nonce,)).fetchone()
            if not proof or proof["used"] or proof["expires_at"] < int(time.time()):
                raise LedgerError("Подтверждение истекло или уже использовано.")
            rows = db.execute("SELECT * FROM custody_wallet_links WHERE uid=? OR address=?",
                              (proof["uid"], address)).fetchall()
            if any(row["uid"] != proof["uid"] or row["address"] != address for row in rows):
                raise LedgerError("Кошелёк уже закреплён за другим аккаунтом либо у вас уже есть подтверждённый кошелёк.")
            db.execute("INSERT OR IGNORE INTO custody_wallet_links VALUES(?,?,?)",
                       (proof["uid"], address, int(time.time())))
            db.execute("UPDATE custody_wallet_proofs SET used=1 WHERE nonce=?", (nonce,))

    def credit_tx(self, uid, signature, sender, amounts, block_time, backing):
        with self.db() as db:
            link = db.execute("SELECT address FROM custody_wallet_links WHERE uid=?", (uid,)).fetchone()
            if not link or link["address"] != sender:
                raise LedgerError("Сначала подтвердите свой кошелёк.")
            previous = db.execute("SELECT * FROM custody_tx_deposits WHERE signature=?", (signature,)).fetchone()
            if previous:
                if previous["uid"] != uid:
                    raise LedgerError("Tx уже зачислена другому аккаунту.")
                return False
            if db.execute("SELECT id FROM custody_journals WHERE id=?", ("deposit:" + signature,)).fetchone():
                raise LedgerError("Tx уже учтена.")
            epoch = db.execute("SELECT started_at FROM custody_tx_epoch WHERE id=1").fetchone()
            if not epoch or block_time < epoch["started_at"]:
                raise LedgerError("Эта Tx предшествует созданию раздельного custody-учёта. Старые деньги повторно не зачисляются; не отправляйте их повторно.")
            lines = []
            for asset, raw in amounts.items():
                if asset not in (SOL, USDC) or type(raw) is not int or not 0 < raw <= 2**63 - 1:
                    raise LedgerError("Неверный актив или сумма.")
                imported = db.execute("""SELECT MAX(j.created_at) FROM custody_journals j
                    JOIN custody_entries e ON e.journal=j.id
                    WHERE e.account='external' AND e.asset=? AND e.delta<0
                    AND (j.id LIKE 'opening:%' OR j.id LIKE 'fees:%')""", (asset,)).fetchone()[0]
                if imported is not None and block_time <= imported:
                    raise LedgerError("Tx предшествует переносу старых нераспределённых средств. Нельзя повторно зачислить уже перенесённые монеты; требуется сверка старого учёта.")
                total = db.execute("SELECT COALESCE(SUM(raw),0) FROM custody_accounts WHERE asset=? AND account!='external'",
                                   (asset,)).fetchone()[0]
                if total + raw > backing.get(asset, 0):
                    raise LedgerError("Недостаточно фактического покрытия для зачисления.")
                lines.extend([("external", asset, -raw), (available(uid), asset, raw)])
            if not lines:
                raise LedgerError("Нет поступления.")
            self.post(db, "deposit:" + signature, "verified sender Tx deposit", lines)
            db.execute("INSERT INTO custody_tx_deposits VALUES(?,?,?,?,?)",
                       (signature, uid, sender, json.dumps(amounts), block_time))
            return True

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
            if db.execute("SELECT signature FROM custody_tx_deposits WHERE signature=?", (signature,)).fetchone():
                raise LedgerError("Tx уже учтена новым способом.")
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

    def release_excess_fees(self, owner_uid, expected_raw):
        if owner_uid is None or expected_raw <= 0:
            raise LedgerError("Неверная сумма возврата резерва.")
        with self.db() as db:
            if db.execute("SELECT id FROM custody_operations WHERE state IN ('RESERVED','PREPARED','SUBMITTED','UNKNOWN') LIMIT 1").fetchone():
                raise LedgerError("Сначала нужно подтвердить предыдущую операцию.")
            row = db.execute("SELECT raw FROM custody_accounts WHERE account='treasury' AND asset=?",
                             (SOL,)).fetchone()
            reserve = row["raw"] if row else 0
            releasable = reserve - FEE_CAP
            if releasable <= 0:
                raise LedgerError("Резерв комиссий уже на обязательном минимуме.")
            if releasable != expected_raw:
                raise LedgerError("Резерв изменился. Обновите экран и подтвердите актуальную сумму.")
            self.post(db, "fee-release:" + str(uuid.uuid4()),
                      "operator returns excess fee reserve to owner", [
                          ("treasury", SOL, -releasable),
                          (available(owner_uid), SOL, releasable)])
            return releasable

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
                if metadata.get("output_asset") != position["funding_asset"]:
                    raise LedgerError("Продажа должна возвращать средства в валюту этой позиции.")
                source = position_account(position_id)
                db.execute("UPDATE custody_positions SET state='RESERVED' WHERE id=?", (position_id,))
            elif kind == "BUY":
                if asset not in (USDC, SOL) or metadata.get("output_asset") in (None, asset):
                    raise LedgerError("Недопустимая валюта покупки.")
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

    def fail(self, oid, network_proven=False, fee=0, recovery_audit=None):
        with self.db() as db:
            row = db.execute("SELECT * FROM custody_operations WHERE id=?", (oid,)).fetchone()
            if not row or row["state"] in ("FAILED", "COMPLETED"):
                return
            if row["state"] != "RESERVED" and not network_proven:
                raise LedgerError("Нельзя возвращать резерв: исход транзакции неизвестен.")
            if recovery_audit is not None and (
                    row["state"] not in ("PREPARED", "SUBMITTED", "UNKNOWN")
                    or not row["signature"]
                    or not isinstance(recovery_audit.get("evidence"), dict)
                    or type(recovery_audit.get("actor_id")) is not int):
                raise LedgerError("Recovery audit evidence is incomplete.")
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
            if recovery_audit is not None:
                db.execute("""CREATE TABLE IF NOT EXISTS custody_operation_recovery_audit(
                    signature TEXT PRIMARY KEY, operation_id TEXT NOT NULL,
                    actor_id INTEGER NOT NULL, state_before TEXT NOT NULL,
                    evidence TEXT NOT NULL, timestamp TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
                db.execute("""CREATE TRIGGER IF NOT EXISTS custody_recovery_audit_immutable_update
                    BEFORE UPDATE ON custody_operation_recovery_audit
                    BEGIN SELECT RAISE(ABORT,'immutable custody recovery audit'); END""")
                db.execute("""CREATE TRIGGER IF NOT EXISTS custody_recovery_audit_immutable_delete
                    BEFORE DELETE ON custody_operation_recovery_audit
                    BEGIN SELECT RAISE(ABORT,'immutable custody recovery audit'); END""")
                db.execute("""INSERT INTO custody_operation_recovery_audit
                    (signature,operation_id,actor_id,state_before,evidence) VALUES(?,?,?,?,?)""",
                    (row["signature"], oid, recovery_audit["actor_id"], row["state"],
                     json.dumps(recovery_audit["evidence"])))

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
                    (id,uid,asset,symbol,decimals,raw,cost_usdc,state,buy_tx,created_at,funding_asset,cost_raw)
                    VALUES(?,?,?,?,?,?,?,'OPEN',?,?,?,?)""",
                    (oid, row["uid"], info["output_asset"], info["symbol"], info["decimals"],
                     received, spent if row["asset"] == USDC else 1,
                     row["signature"], int(time.time()), row["asset"], spent))
            elif row["kind"] == "SELL":
                if spent != row["raw"]:
                    raise LedgerError("Частичная продажа требует отдельного разбора.")
                db.execute("""UPDATE custody_positions SET state='CLOSED',sell_tx=?,proceeds_usdc=?,proceeds_raw=?
                    WHERE id=? AND uid=?""", (row["signature"], received if info["output_asset"] == USDC else None,
                    received, row["position_id"], row["uid"]))
            db.execute("UPDATE custody_operations SET state='COMPLETED' WHERE id=?", (oid,))

    def positions(self, uid, state=None):
        with self.db() as db:
            rows = db.execute("SELECT * FROM custody_positions WHERE uid=?" +
                (" AND state=?" if state else "") + " ORDER BY created_at DESC",
                (uid, state) if state else (uid,))
            return [dict(r) for r in rows]

    def settings(self, uid, enabled=None, raw=None, close=None, sniper_enabled=None, sniper_raw=None):
        with self.db() as db:
            if enabled is not None:
                db.execute("UPDATE custody_users SET trading=?,close_requested=0 WHERE uid=?", (int(enabled), uid))
            if raw is not None:
                db.execute("UPDATE custody_users SET order_raw=? WHERE uid=?", (raw, uid))
            if close is not None:
                db.execute("UPDATE custody_users SET close_requested=? WHERE uid=?", (int(close), uid))
            if sniper_enabled is not None:
                db.execute("UPDATE custody_users SET sniper_trading=? WHERE uid=?", (int(sniper_enabled), uid))
            if sniper_raw is not None:
                db.execute("UPDATE custody_users SET sniper_order_raw=? WHERE uid=?", (sniper_raw, uid))
            row = db.execute("SELECT * FROM custody_users WHERE uid=?", (uid,)).fetchone()
            return dict(row) if row else None

    def active_users(self):
        with self.db() as db:
            return [dict(r) for r in db.execute("SELECT * FROM custody_users WHERE trading=1 OR sniper_trading=1 OR close_requested=1")]

    def restart_pause(self):
        with self.db() as db:
            db.execute("UPDATE custody_users SET trading=0,sniper_trading=0,close_requested=0")
            # RESERVED has never been broadcast, by contract; PREPARED is ambiguous.
            unsent = [r[0] for r in db.execute("SELECT id FROM custody_operations WHERE state='RESERVED'")]
        for oid in unsent:
            self.fail(oid)

    def ai_latest(self):
        with self.db() as db:
            row = db.execute("SELECT * FROM custody_ai_analyses ORDER BY created_at DESC,rowid DESC LIMIT 1").fetchone()
            return dict(row) if row else None

    def ai_allow_call(self, interval, daily_limit):
        now = int(time.time())
        with self.db() as db:
            latest = db.execute("SELECT MAX(created_at) FROM custody_ai_calls").fetchone()[0]
            count = db.execute("SELECT COUNT(*) FROM custody_ai_calls WHERE created_at>=?", (now - now % 86400,)).fetchone()[0]
            if count >= daily_limit:
                raise LedgerError("Дневной лимит обращений к AI исчерпан. Новые входы остановлены.")
            if latest is not None and now - latest < interval:
                raise LedgerError("AI ожидает следующего интервала анализа. Новые входы пока остановлены.")
            aid = str(uuid.uuid4())
            db.execute("INSERT INTO custody_ai_calls VALUES(?,?)", (aid, now))
            return aid

    def ai_record(self, aid, decisions):
        with self.db() as db:
            db.execute("INSERT INTO custody_ai_analyses VALUES(?,?,?)", (aid, int(time.time()), json.dumps(decisions)))

    def ai_claim(self, aid, uid, mode):
        if mode not in ("main", "sniper"):
            raise LedgerError("Неизвестный режим AI.")
        with self.db() as db:
            return bool(db.execute("INSERT OR IGNORE INTO custody_ai_attempts VALUES(?,?,?)", (aid, uid, mode)).rowcount)

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
