"""Integer, double-entry custody ledger with owner-confirmed backed legacy imports."""
import hashlib
import json
import math
import os
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation

USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SOL = "SOL"
FEE_CAP = 5_000_000
MONTHLY_TRADING_FEE_USD_CENTS = 500
MONTHLY_TRADING_FEE_PERIOD_SECONDS = 30 * 24 * 60 * 60
MONTHLY_TRADING_FEE_TERMS_VERSION = "monthly-trading-fee-v1"
ACTIVE = ("RESERVED", "PREPARED", "SUBMITTED", "UNKNOWN")
PARTNER_LEVEL_BPS = (100, 50, 25)
REENTRY_COOLDOWN_SECONDS = 24 * 60 * 60


class LedgerError(Exception):
    pass


def available(uid):
    return "user:" + str(int(uid))


def reserve_account(oid):
    return "operation:" + oid


def position_account(pid):
    return "position:" + pid


def partner_account(uid):
    return "partner:" + str(int(uid))


def _balance(db, account, asset):
    row = db.execute("SELECT raw FROM custody_accounts WHERE account=? AND asset=?",
                     (account, asset)).fetchone()
    return row["raw"] if row else 0


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
                CREATE TABLE IF NOT EXISTS custody_execution_checks (
                    uid INTEGER PRIMARY KEY, report TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS custody_entry_cost_checks (
                    uid INTEGER PRIMARY KEY, report TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS custody_partner_codes (
                    uid INTEGER PRIMARY KEY, code TEXT NOT NULL UNIQUE);
                CREATE TABLE IF NOT EXISTS custody_referrals (
                    uid INTEGER PRIMARY KEY, parent_uid INTEGER NOT NULL,
                    created_at INTEGER NOT NULL, CHECK(uid<>parent_uid));
                CREATE TABLE IF NOT EXISTS custody_partner_profit_state (
                    trader_uid INTEGER NOT NULL, asset TEXT NOT NULL,
                    net_profit_raw INTEGER NOT NULL DEFAULT 0,
                    high_water_raw INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(trader_uid,asset));
                CREATE TABLE IF NOT EXISTS custody_partner_earnings (
                    id TEXT PRIMARY KEY, position_id TEXT NOT NULL,
                    trader_uid INTEGER NOT NULL, partner_uid INTEGER NOT NULL,
                    level INTEGER NOT NULL CHECK(level BETWEEN 1 AND 3),
                    asset TEXT NOT NULL, profit_basis_raw INTEGER NOT NULL,
                    amount_raw INTEGER NOT NULL CHECK(amount_raw>0),
                    state TEXT NOT NULL DEFAULT 'QUEUED',
                    attempts INTEGER NOT NULL DEFAULT 0, next_attempt INTEGER NOT NULL DEFAULT 0,
                    operation_id TEXT, signature TEXT,
                    created_at INTEGER NOT NULL,
                    UNIQUE(position_id,level));
                CREATE INDEX IF NOT EXISTS custody_partner_earnings_queue
                    ON custody_partner_earnings(state,created_at);
                CREATE TABLE IF NOT EXISTS custody_recovery_notifications (
                    operation_id TEXT PRIMARY KEY, uid INTEGER NOT NULL,
                    payload TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt INTEGER NOT NULL DEFAULT 0, delivered_at INTEGER,
                    last_error TEXT);
                CREATE TABLE IF NOT EXISTS custody_withdrawal_notifications (
                    operation_id TEXT PRIMARY KEY, uid INTEGER NOT NULL,
                    payload TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt INTEGER NOT NULL DEFAULT 0, delivered_at INTEGER,
                    last_error TEXT);
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
                    failure_reason TEXT NOT NULL DEFAULT '',
                    created_at INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS custody_trade_notifications (
                    operation_id TEXT PRIMARY KEY REFERENCES custody_operations(id),
                    uid INTEGER NOT NULL REFERENCES custody_users(uid),
                    payload TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'QUEUED'
                        CHECK(state IN ('QUEUED','SENDING','DELIVERED','FAILED','UNKNOWN')),
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt INTEGER NOT NULL DEFAULT 0,
                    last_attempt_at INTEGER NOT NULL DEFAULT 0,
                    delivered_at INTEGER,
                    last_error TEXT);
                CREATE INDEX IF NOT EXISTS custody_trade_notifications_queue
                    ON custody_trade_notifications(state,next_attempt,operation_id);
                CREATE TABLE IF NOT EXISTS custody_monthly_trading_fee_consents (
                    uid INTEGER PRIMARY KEY REFERENCES custody_users(uid),
                    accepted_at INTEGER NOT NULL,
                    terms_version TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'ACTIVE'
                        CHECK(status IN ('ACTIVE','CANCELED')),
                    next_due_at INTEGER NOT NULL,
                    paid_until INTEGER NOT NULL DEFAULT 0,
                    retry_after INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS custody_monthly_trading_fee_consent_events (
                    id INTEGER PRIMARY KEY,
                    uid INTEGER NOT NULL REFERENCES custody_users(uid),
                    event TEXT NOT NULL CHECK(event IN ('ACCEPTED','CANCELED')),
                    terms_version TEXT NOT NULL,
                    created_at INTEGER NOT NULL);
                CREATE TRIGGER IF NOT EXISTS custody_monthly_fee_consent_events_immutable_update
                    BEFORE UPDATE ON custody_monthly_trading_fee_consent_events
                    BEGIN SELECT RAISE(ABORT,'immutable fee consent audit'); END;
                CREATE TRIGGER IF NOT EXISTS custody_monthly_fee_consent_events_immutable_delete
                    BEFORE DELETE ON custody_monthly_trading_fee_consent_events
                    BEGIN SELECT RAISE(ABORT,'immutable fee consent audit'); END;
                CREATE TABLE IF NOT EXISTS custody_monthly_trading_fee_charges (
                    id TEXT PRIMARY KEY,
                    uid INTEGER NOT NULL REFERENCES custody_users(uid),
                    owner_uid INTEGER NOT NULL REFERENCES custody_users(uid),
                    cycle_due_at INTEGER NOT NULL,
                    amount_raw INTEGER NOT NULL CHECK(amount_raw>0),
                    price_usd TEXT NOT NULL,
                    journal_id TEXT NOT NULL UNIQUE REFERENCES custody_journals(id),
                    created_at INTEGER NOT NULL,
                    UNIQUE(uid,cycle_due_at));
                CREATE TRIGGER IF NOT EXISTS custody_monthly_fee_charges_immutable_update
                    BEFORE UPDATE ON custody_monthly_trading_fee_charges
                    BEGIN SELECT RAISE(ABORT,'immutable fee charge'); END;
                CREATE TRIGGER IF NOT EXISTS custody_monthly_fee_charges_immutable_delete
                    BEFORE DELETE ON custody_monthly_trading_fee_charges
                    BEGIN SELECT RAISE(ABORT,'immutable fee charge'); END;
                CREATE INDEX IF NOT EXISTS custody_monthly_trading_fee_due
                    ON custody_monthly_trading_fee_consents(status,next_due_at,retry_after);
                CREATE TABLE IF NOT EXISTS custody_monthly_fee_notifications (
                    charge_id TEXT PRIMARY KEY REFERENCES custody_monthly_trading_fee_charges(id),
                    uid INTEGER NOT NULL REFERENCES custody_users(uid),
                    payload TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'QUEUED'
                        CHECK(state IN ('QUEUED','SENDING','DELIVERED','FAILED','UNKNOWN')),
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt INTEGER NOT NULL DEFAULT 0,
                    last_attempt_at INTEGER NOT NULL DEFAULT 0,
                    delivered_at INTEGER,
                    last_error TEXT);
                CREATE INDEX IF NOT EXISTS custody_monthly_fee_notifications_queue
                    ON custody_monthly_fee_notifications(state,next_attempt,charge_id);
                CREATE UNIQUE INDEX IF NOT EXISTS custody_one_active_operation
                    ON custody_operations((1)) WHERE state IN ('RESERVED','PREPARED','SUBMITTED','UNKNOWN');
                CREATE TABLE IF NOT EXISTS custody_positions (
                    id TEXT PRIMARY KEY, uid INTEGER NOT NULL REFERENCES custody_users(uid),
                    asset TEXT NOT NULL, symbol TEXT NOT NULL, decimals INTEGER NOT NULL,
                    raw INTEGER NOT NULL CHECK(raw>0), cost_usdc INTEGER NOT NULL CHECK(cost_usdc>0),
                    state TEXT NOT NULL, buy_tx TEXT NOT NULL, sell_tx TEXT,
                    peak_price REAL NOT NULL DEFAULT 0,
                    proceeds_usdc INTEGER, created_at INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS custody_loss_reentry_cooldowns (
                    asset TEXT PRIMARY KEY, blocked_until INTEGER NOT NULL, last_loss_at INTEGER NOT NULL);
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
                    "cost_raw": "INTEGER", "proceeds_raw": "INTEGER", "closed_at": "INTEGER",
                    "peak_price": "REAL NOT NULL DEFAULT 0"},
                "custody_operations": {"failure_reason": "TEXT NOT NULL DEFAULT ''"},
            }.items():
                columns = {row[1] for row in db.execute("PRAGMA table_info(" + table + ")")}
                for name, definition in additions.items():
                    if name not in columns:
                        db.execute("ALTER TABLE " + table + " ADD COLUMN " + name + " " + definition)
            db.execute("UPDATE custody_positions SET cost_raw=cost_usdc,proceeds_raw=proceeds_usdc WHERE cost_raw IS NULL")
            db.execute("""UPDATE custody_positions SET closed_at=(
                    SELECT MAX(o.created_at) FROM custody_operations o
                    WHERE o.position_id=custody_positions.id AND o.kind='SELL' AND o.state='COMPLETED')
                WHERE state='CLOSED' AND closed_at IS NULL""")
            now = int(time.time())
            db.execute("""INSERT INTO custody_loss_reentry_cooldowns(asset,blocked_until,last_loss_at)
                SELECT p.asset, MAX(COALESCE(p.closed_at,o.created_at)) + ?,
                       MAX(COALESCE(p.closed_at,o.created_at))
                FROM custody_positions p JOIN custody_operations o
                    ON o.position_id=p.id AND o.kind='SELL' AND o.state='COMPLETED'
                WHERE p.state='CLOSED'
                    AND COALESCE(p.closed_at,o.created_at) >= ?
                GROUP BY p.asset
                ON CONFLICT(asset) DO UPDATE SET
                    blocked_until=MAX(custody_loss_reentry_cooldowns.blocked_until,excluded.blocked_until),
                    last_loss_at=MAX(custody_loss_reentry_cooldowns.last_loss_at,excluded.last_loss_at)""",
                (REENTRY_COOLDOWN_SECONDS, now - REENTRY_COOLDOWN_SECONDS))
        self.credit_safe_partner_earnings()

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

    def partner_referral_code(self, uid):
        with self.db() as db:
            if not db.execute("SELECT 1 FROM custody_users WHERE uid=?", (uid,)).fetchone():
                raise LedgerError("Create an AI Trader account before sharing a referral link.")
            row = db.execute("SELECT code FROM custody_partner_codes WHERE uid=?", (uid,)).fetchone()
            if row:
                return row["code"]
            for _ in range(8):
                code = secrets.token_urlsafe(9)
                try:
                    db.execute("INSERT INTO custody_partner_codes(uid,code) VALUES(?,?)", (uid, code))
                    return code
                except sqlite3.IntegrityError:
                    continue
            raise LedgerError("Could not create a unique referral link.")

    def bind_referral_code(self, referred_uid, code):
        if type(referred_uid) is not int or referred_uid <= 0 or not isinstance(code, str):
            return False
        with self.db() as db:
            partner = db.execute("SELECT uid FROM custody_partner_codes WHERE code=?", (code,)).fetchone()
            if not partner or partner["uid"] == referred_uid:
                return False
            if db.execute("SELECT 1 FROM custody_users WHERE uid=?", (referred_uid,)).fetchone():
                return False
            db.execute("INSERT OR IGNORE INTO custody_referrals(uid,parent_uid,created_at) VALUES(?,?,?)",
                       (referred_uid, partner["uid"], int(time.time())))
            return db.execute("SELECT parent_uid FROM custody_referrals WHERE uid=?",
                              (referred_uid,)).fetchone()["parent_uid"] == partner["uid"]

    def partner_report(self, uid, bot_username=""):
        code = self.partner_referral_code(uid)
        with self.db() as db:
            referrals = [dict(row) for row in db.execute(
                "SELECT uid,parent_uid FROM custody_referrals WHERE parent_uid=?", (uid,))]
            counts = []
            for level in range(1, 4):
                counts.append(sum(1 for row in referrals if level == 1))
                if level == 2:
                    counts[-1] = db.execute(
                        "SELECT COUNT(*) FROM custody_referrals r JOIN custody_referrals p ON p.uid=r.parent_uid WHERE p.parent_uid=?",
                        (uid,)).fetchone()[0]
                elif level == 3:
                    counts[-1] = db.execute(
                        """SELECT COUNT(*) FROM custody_referrals r
                           JOIN custody_referrals p ON p.uid=r.parent_uid
                           JOIN custody_referrals g ON g.uid=p.parent_uid
                           WHERE g.parent_uid=?""", (uid,)).fetchone()[0]
            earned = []
            for level in range(1, 4):
                row = db.execute("""SELECT asset,COALESCE(SUM(amount_raw),0) total,
                    COALESCE(SUM(CASE WHEN state='CREDITED' THEN amount_raw ELSE 0 END),0) credited,
                    COALESCE(SUM(CASE WHEN state='PAID' THEN amount_raw ELSE 0 END),0) paid,
                    COALESCE(SUM(CASE WHEN state NOT IN ('CREDITED','PAID')
                        THEN amount_raw ELSE 0 END),0) pending
                    FROM custody_partner_earnings WHERE partner_uid=? AND level=? GROUP BY asset""",
                    (uid, level)).fetchall()
                for item in row:
                    earned.append({"level": level, "asset": item["asset"],
                                   "earned_raw": item["total"], "credited_raw": item["credited"],
                                   "paid_raw": item["paid"], "pending_raw": item["pending"]})
            totals_by_asset = {}
            for item in earned:
                totals = totals_by_asset.setdefault(item["asset"], {
                    "earned_raw": 0, "credited_raw": 0, "paid_raw": 0, "pending_raw": 0})
                for field in ("earned_raw", "credited_raw", "paid_raw", "pending_raw"):
                    totals[field] += int(item[field])
            total_assets = [USDC, SOL] + sorted(
                asset for asset in totals_by_asset if asset not in (USDC, SOL))
            earnings_totals = [
                {"asset": asset, **totals_by_asset.get(asset, {
                    "earned_raw": 0, "credited_raw": 0, "paid_raw": 0, "pending_raw": 0})}
                for asset in total_assets
            ]
            earnings_history = [dict(row) for row in db.execute(
                """SELECT id,level,asset,amount_raw,state,signature,created_at
                   FROM custody_partner_earnings WHERE partner_uid=?
                   ORDER BY created_at DESC LIMIT 20""", (uid,))]
            review_required = db.execute(
                "SELECT COUNT(*) FROM custody_partner_earnings WHERE partner_uid=? AND state='NEEDS_REVIEW'",
                (uid,)).fetchone()[0]
            return {"code": code,
                "invite_url": ("https://t.me/" + bot_username + "?start=ref_" + code) if bot_username else "",
                "rates_bps": list(PARTNER_LEVEL_BPS), "referrals_by_level": counts,
                "earnings": earned, "earnings_totals": earnings_totals, "history": earnings_history,
                "automatic_payouts": False, "balance_crediting": True,
                "review_required": review_required,
                "wallet_linked": bool(db.execute(
                    "SELECT 1 FROM custody_wallet_links WHERE uid=?", (uid,)).fetchone())}

    def credit_safe_partner_earnings(self):
        """Move unsubmitted legacy partner earnings into withdrawable user balances."""
        credited = 0
        with self.db() as db:
            groups = db.execute("""SELECT partner_uid,asset,SUM(amount_raw) AS amount
                FROM custody_partner_earnings
                WHERE state IN ('QUEUED','NEEDS_REVIEW') AND operation_id IS NULL
                GROUP BY partner_uid,asset""").fetchall()
            for group in groups:
                uid, asset = group["partner_uid"], group["asset"]
                rows = db.execute("""SELECT id,amount_raw FROM custody_partner_earnings
                    WHERE partner_uid=? AND asset=? AND state IN ('QUEUED','NEEDS_REVIEW')
                    AND operation_id IS NULL ORDER BY created_at,id""", (uid, asset)).fetchall()
                expected = sum(row["amount_raw"] for row in rows)
                source = partner_account(uid)
                # A mismatch may indicate an older payout is still unresolved. Never
                # credit it to the main balance unless the legacy ledger fully backs it.
                if not rows or expected != group["amount"] or _balance(db, source, asset) != expected:
                    continue
                for row in rows:
                    jid = "partner-main-credit:" + row["id"]
                    if not self.post(db, jid, "move verified partner earnings to main balance", [
                            (source, asset, -row["amount_raw"]),
                            (available(uid), asset, row["amount_raw"])]):
                        raise LedgerError("Partner earning was already transferred.")
                    changed = db.execute("""UPDATE custody_partner_earnings SET state='CREDITED'
                        WHERE id=? AND partner_uid=? AND asset=?
                        AND state IN ('QUEUED','NEEDS_REVIEW') AND operation_id IS NULL""",
                        (row["id"], uid, asset)).rowcount
                    if changed != 1:
                        raise LedgerError("Partner earning changed during balance transfer.")
                    credited += 1
        return credited

    def next_partner_earning(self):
        with self.db() as db:
            row = db.execute("""SELECT e.* FROM custody_partner_earnings e
                JOIN custody_wallet_links w ON w.uid=e.partner_uid
                WHERE e.state='QUEUED' AND e.next_attempt<=?
                ORDER BY e.created_at,e.level LIMIT 1""", (int(time.time()),)).fetchone()
            return dict(row) if row else None

    def set_partner_operation(self, earning_id, operation_id):
        with self.db() as db:
            db.execute("""UPDATE custody_partner_earnings SET state='SUBMITTED',operation_id=?
                WHERE id=? AND state='QUEUED'""", (operation_id, earning_id))

    def retry_partner_earning(self, uid, earning_id):
        with self.db() as db:
            changed = db.execute("""UPDATE custody_partner_earnings
                SET state='QUEUED',attempts=0,next_attempt=0
                WHERE id=? AND partner_uid=? AND state='NEEDS_REVIEW'""",
                (earning_id, uid)).rowcount
            return changed == 1

    @staticmethod
    def _record_partner_profit(db, position, profit_raw):
        """Return ledger credits after carrying prior losses and applying three fixed rates."""
        referral = db.execute("SELECT parent_uid FROM custody_referrals WHERE uid=?",
                              (position["uid"],)).fetchone()
        if not referral:
            return {}, 0
        trader_uid, asset = position["uid"], position["funding_asset"]
        db.execute("""INSERT OR IGNORE INTO custody_partner_profit_state
            (trader_uid,asset,net_profit_raw,high_water_raw) VALUES(?,?,0,0)""",
            (trader_uid, asset))
        state = db.execute("""SELECT net_profit_raw,high_water_raw
            FROM custody_partner_profit_state WHERE trader_uid=? AND asset=?""",
            (trader_uid, asset)).fetchone()
        net = state["net_profit_raw"] + profit_raw
        new_high_water = max(state["high_water_raw"], max(0, net))
        eligible_raw = max(0, new_high_water - state["high_water_raw"])
        db.execute("""UPDATE custody_partner_profit_state SET net_profit_raw=?,high_water_raw=?
            WHERE trader_uid=? AND asset=?""", (net, new_high_water, trader_uid, asset))
        credits = {}
        total = 0
        partner_uid = referral["parent_uid"]
        for level, rate_bps in enumerate(PARTNER_LEVEL_BPS, 1):
            if not partner_uid:
                break
            amount = eligible_raw * rate_bps // 10_000
            if amount:
                earning_id = str(uuid.uuid4())
                db.execute("""INSERT INTO custody_partner_earnings
                    (id,position_id,trader_uid,partner_uid,level,asset,profit_basis_raw,
                     amount_raw,created_at,state) VALUES(?,?,?,?,?,?,?,?,?,'CREDITED')""",
                    (earning_id, position["id"], trader_uid, partner_uid, level, asset,
                     eligible_raw, amount, int(time.time())))
                account = available(partner_uid)
                credits[account] = credits.get(account, 0) + amount
                total += amount
            row = db.execute("SELECT parent_uid FROM custody_referrals WHERE uid=?",
                             (partner_uid,)).fetchone()
            partner_uid = row["parent_uid"] if row else None
        return credits, total

    def balance(self, uid, asset):
        with self.db() as db:
            row = db.execute("SELECT raw FROM custody_accounts WHERE account=? AND asset=?",
                             (available(uid), asset)).fetchone()
            return row["raw"] if row else 0

    @staticmethod
    def _has_monthly_fee_funding(db, uid):
        invoice = db.execute("""SELECT 1 FROM custody_invoices
            WHERE uid=? AND state='CREDITED' LIMIT 1""", (uid,)).fetchone()
        if invoice:
            return True
        if db.execute("SELECT 1 FROM custody_tx_deposits WHERE uid=? LIMIT 1", (uid,)).fetchone():
            return True
        return bool(db.execute("""SELECT 1 FROM custody_entries
            WHERE account=? AND delta>0 AND journal IN (
                'opening:SOL','opening:USDC',
                'legacy-opening:SOL:' || ?, 'legacy-opening:USDC:' || ?)
            LIMIT 1""", (available(uid), uid, uid)).fetchone())

    def monthly_fee_status(self, uid, owner_uid, now=None):
        if type(uid) is not int or uid <= 0 or type(owner_uid) is not int or owner_uid <= 0:
            raise LedgerError("Invalid monthly trading fee status request.")
        now = int(time.time()) if now is None else int(now)
        if uid == owner_uid:
            return {
                "is_owner": True, "funded": False, "consent_status": "EXEMPT",
                "accepted_at": None, "next_due_at": None, "paid_until": None,
                "retry_after": None, "balance_raw": self.balance(uid, SOL),
                "last_charge": None,
            }
        with self.db() as db:
            consent = db.execute(
                "SELECT * FROM custody_monthly_trading_fee_consents WHERE uid=?", (uid,)
            ).fetchone()
            funded = self._has_monthly_fee_funding(db, uid)
            balance = db.execute(
                "SELECT raw FROM custody_accounts WHERE account=? AND asset=?",
                (available(uid), SOL)).fetchone()
            last_charge = db.execute("""SELECT amount_raw,price_usd,created_at
                FROM custody_monthly_trading_fee_charges WHERE uid=?
                ORDER BY created_at DESC,id DESC LIMIT 1""", (uid,)).fetchone()
            return {
                "is_owner": False,
                "funded": funded,
                "consent_status": consent["status"] if consent else "NOT_ACCEPTED",
                "accepted_at": consent["accepted_at"] if consent else None,
                "next_due_at": consent["next_due_at"] if consent else None,
                "paid_until": consent["paid_until"] if consent else None,
                "retry_after": consent["retry_after"] if consent else None,
                "balance_raw": balance["raw"] if balance else 0,
                "last_charge": dict(last_charge) if last_charge else None,
                "due": bool(consent and consent["status"] == "ACTIVE"
                            and consent["next_due_at"] <= now),
            }

    def accept_monthly_fee(self, uid, owner_uid, terms_version, now=None):
        if (type(uid) is not int or uid <= 0 or type(owner_uid) is not int or owner_uid <= 0
                or uid == owner_uid or terms_version != MONTHLY_TRADING_FEE_TERMS_VERSION):
            raise LedgerError("Monthly fee consent is invalid.")
        now = int(time.time()) if now is None else int(now)
        with self.db() as db:
            if not db.execute("SELECT 1 FROM custody_users WHERE uid=?", (uid,)).fetchone():
                raise LedgerError("Account not found.")
            old = db.execute(
                "SELECT * FROM custody_monthly_trading_fee_consents WHERE uid=?", (uid,)
            ).fetchone()
            if old and old["status"] == "ACTIVE":
                return {"state": "already_active", "next_due_at": old["next_due_at"]}
            if old and old["paid_until"] > now:
                db.execute("""UPDATE custody_monthly_trading_fee_consents
                    SET accepted_at=?,terms_version=?,status='ACTIVE',
                        next_due_at=paid_until,retry_after=0 WHERE uid=?""",
                    (now, terms_version, uid))
                db.execute("""INSERT INTO custody_monthly_trading_fee_consent_events
                    (uid,event,terms_version,created_at) VALUES(?,'ACCEPTED',?,?)""",
                    (uid, terms_version, now))
                return {"state": "reactivated", "next_due_at": old["paid_until"]}
            db.execute("""INSERT INTO custody_monthly_trading_fee_consents
                (uid,accepted_at,terms_version,status,next_due_at,paid_until,retry_after)
                VALUES(?,?,?,'ACTIVE',?,0,0)
                ON CONFLICT(uid) DO UPDATE SET accepted_at=excluded.accepted_at,
                    terms_version=excluded.terms_version,status='ACTIVE',
                    next_due_at=excluded.next_due_at,paid_until=0,retry_after=0""",
                (uid, now, terms_version, now))
            db.execute("""INSERT INTO custody_monthly_trading_fee_consent_events
                (uid,event,terms_version,created_at) VALUES(?,'ACCEPTED',?,?)""",
                (uid, terms_version, now))
            return {"state": "accepted", "next_due_at": now}

    def cancel_monthly_fee(self, uid, owner_uid, now=None):
        if (type(uid) is not int or uid <= 0 or type(owner_uid) is not int or owner_uid <= 0
                or uid == owner_uid):
            raise LedgerError("Monthly fee cancellation is invalid.")
        now = int(time.time()) if now is None else int(now)
        with self.db() as db:
            consent = db.execute(
                "SELECT * FROM custody_monthly_trading_fee_consents WHERE uid=?", (uid,)
            ).fetchone()
            if not consent or consent["status"] == "CANCELED":
                return {"state": "already_canceled",
                        "paid_until": consent["paid_until"] if consent else 0}
            db.execute("""UPDATE custody_monthly_trading_fee_consents
                SET status='CANCELED',retry_after=0 WHERE uid=?""", (uid,))
            db.execute("""INSERT INTO custody_monthly_trading_fee_consent_events
                (uid,event,terms_version,created_at) VALUES(?,'CANCELED',?,?)""",
                (uid, consent["terms_version"], now))
            return {"state": "canceled", "paid_until": consent["paid_until"]}

    def monthly_fee_due_users(self, now=None):
        now = int(time.time()) if now is None else int(now)
        with self.db() as db:
            return [row["uid"] for row in db.execute("""SELECT c.uid
                FROM custody_monthly_trading_fee_consents c
                WHERE c.status='ACTIVE' AND c.next_due_at<=? AND c.retry_after<=?
                AND (EXISTS (SELECT 1 FROM custody_invoices i
                             WHERE i.uid=c.uid AND i.state='CREDITED')
                     OR EXISTS (SELECT 1 FROM custody_tx_deposits d WHERE d.uid=c.uid)
                     OR EXISTS (SELECT 1 FROM custody_entries e
                                WHERE e.account='user:' || c.uid AND e.delta>0 AND e.journal IN (
                                    'opening:SOL','opening:USDC',
                                    'legacy-opening:SOL:' || c.uid,
                                    'legacy-opening:USDC:' || c.uid)))
                ORDER BY c.next_due_at,c.uid""", (now, now))]

    def charge_monthly_fee(self, uid, owner_uid, amount_raw, price_usd, now=None):
        if (type(uid) is not int or uid <= 0 or type(owner_uid) is not int or owner_uid <= 0
                or uid == owner_uid or type(amount_raw) is not int or amount_raw <= 0):
            raise LedgerError("Monthly fee charge is invalid.")
        try:
            price = Decimal(str(price_usd))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise LedgerError("A valid SOL/USD price is required for monthly billing.") from exc
        if not price.is_finite() or price <= 0:
            raise LedgerError("A valid SOL/USD price is required for monthly billing.")
        now = int(time.time()) if now is None else int(now)
        with self.db() as db:
            consent = db.execute(
                "SELECT * FROM custody_monthly_trading_fee_consents WHERE uid=?", (uid,)
            ).fetchone()
            if not consent or consent["status"] != "ACTIVE":
                return {"state": "not_active"}
            if consent["next_due_at"] > now:
                return {"state": "not_due", "next_due_at": consent["next_due_at"]}
            cycle_due_at = consent["next_due_at"]
            existing = db.execute("""SELECT * FROM custody_monthly_trading_fee_charges
                WHERE uid=? AND cycle_due_at=?""", (uid, cycle_due_at)).fetchone()
            if existing:
                next_due = existing["created_at"] + MONTHLY_TRADING_FEE_PERIOD_SECONDS
                db.execute("""UPDATE custody_monthly_trading_fee_consents
                    SET next_due_at=?,paid_until=?,retry_after=0 WHERE uid=?""",
                    (next_due, next_due, uid))
                return {"state": "already_charged", "charge": dict(existing),
                        "next_due_at": next_due}
            if not self._has_monthly_fee_funding(db, uid):
                return {"state": "waiting_for_funding"}
            balance = db.execute("SELECT raw FROM custody_accounts WHERE account=? AND asset=?",
                                 (available(uid), SOL)).fetchone()
            available_raw = balance["raw"] if balance else 0
            if available_raw < amount_raw:
                retry_after = now + 60 * 60
                db.execute("""UPDATE custody_monthly_trading_fee_consents
                    SET retry_after=? WHERE uid=?""", (retry_after, uid))
                return {"state": "insufficient_funds", "balance_raw": available_raw,
                        "required_raw": amount_raw, "retry_after": retry_after}
            journal_id = f"monthly-trading-fee:{uid}:{cycle_due_at}"
            charge_id = journal_id
            self.post(db, journal_id, "monthly $5 USD-equivalent trading fee", [
                (available(uid), SOL, -amount_raw),
                (available(owner_uid), SOL, amount_raw),
            ])
            db.execute("""INSERT INTO custody_monthly_trading_fee_charges
                (id,uid,owner_uid,cycle_due_at,amount_raw,price_usd,journal_id,created_at)
                VALUES(?,?,?,?,?,?,?,?)""",
                (charge_id, uid, owner_uid, cycle_due_at, amount_raw, str(price), journal_id, now))
            next_due = now + MONTHLY_TRADING_FEE_PERIOD_SECONDS
            db.execute("""UPDATE custody_monthly_trading_fee_consents
                SET next_due_at=?,paid_until=?,retry_after=0 WHERE uid=?""",
                (next_due, next_due, uid))
            db.execute("""INSERT INTO custody_monthly_fee_notifications
                (charge_id,uid,payload) VALUES(?,?,?)""",
                (charge_id, uid, json.dumps({
                    "amount_raw": amount_raw, "price_usd": str(price),
                    "paid_until": next_due,
                }, sort_keys=True, separators=(",", ":"))))
            return {"state": "charged", "charge": {
                "id": charge_id, "uid": uid, "owner_uid": owner_uid,
                "cycle_due_at": cycle_due_at, "amount_raw": amount_raw,
                "price_usd": str(price), "journal_id": journal_id, "created_at": now,
            }, "next_due_at": next_due}

    def monthly_fee_charges(self, uid, owner_uid, limit=20):
        if type(uid) is not int or uid <= 0 or type(owner_uid) is not int or owner_uid <= 0:
            raise LedgerError("Invalid monthly fee history request.")
        limit = max(1, min(int(limit), 100))
        with self.db() as db:
            if uid == owner_uid:
                rows = db.execute("""SELECT amount_raw,price_usd,created_at,uid
                    FROM custody_monthly_trading_fee_charges WHERE owner_uid=?
                    ORDER BY created_at DESC,id DESC LIMIT ?""", (owner_uid, limit)).fetchall()
                return [{"type": "TRADING_FEE_INCOME", "amount_raw": row["amount_raw"],
                         "price_usd": row["price_usd"], "created_at": row["created_at"]}
                        for row in rows]
            rows = db.execute("""SELECT amount_raw,price_usd,created_at
                FROM custody_monthly_trading_fee_charges WHERE uid=?
                ORDER BY created_at DESC,id DESC LIMIT ?""", (uid, limit)).fetchall()
            return [{"type": "TRADING_FEE", "amount_raw": row["amount_raw"],
                     "price_usd": row["price_usd"], "created_at": row["created_at"]}
                    for row in rows]

    def monthly_fee_notifications(self, now=None):
        now = int(time.time()) if now is None else int(now)
        with self.db() as db:
            return [dict(row) for row in db.execute("""SELECT * FROM custody_monthly_fee_notifications
                WHERE state='QUEUED' AND next_attempt<=? ORDER BY next_attempt,charge_id LIMIT 25""",
                (now,)).fetchall()]

    def claim_monthly_fee_notification(self, charge_id, now=None):
        now = int(time.time()) if now is None else int(now)
        with self.db() as db:
            return db.execute("""UPDATE custody_monthly_fee_notifications
                SET state='SENDING',attempts=attempts+1,last_attempt_at=?
                WHERE charge_id=? AND state='QUEUED' AND next_attempt<=?""",
                (now, charge_id, now)).rowcount == 1

    def monthly_fee_notification_result(self, charge_id, outcome, now=None, retry_after=60, error=None):
        now = int(time.time()) if now is None else int(now)
        if outcome not in ("DELIVERED", "FAILED", "UNKNOWN", "RETRY"):
            raise LedgerError("Invalid monthly fee notification outcome.")
        state = "QUEUED" if outcome == "RETRY" else outcome
        with self.db() as db:
            db.execute("""UPDATE custody_monthly_fee_notifications
                SET state=?,next_attempt=?,delivered_at=?,last_error=?
                WHERE charge_id=? AND state='SENDING'""",
                (state, now + max(1, int(retry_after)) if outcome == "RETRY" else 0,
                 now if outcome == "DELIVERED" else None,
                 str(error or "")[:300] or None, charge_id))

    @staticmethod
    def _withdrawal_entitlement(db, uid, asset):
        """Return the user's lifetime withdrawable entitlement for one settlement asset."""
        deposits = db.execute("""SELECT COALESCE(SUM(raw),0) FROM custody_invoices
            WHERE uid=? AND asset=? AND state='CREDITED'""", (uid, asset)).fetchone()[0]
        for row in db.execute(
                "SELECT amounts FROM custody_tx_deposits WHERE uid=?", (uid,)).fetchall():
            try:
                amounts = json.loads(row["amounts"])
                amount = amounts.get(asset, 0) if isinstance(amounts, dict) else None
            except (TypeError, ValueError) as exc:
                raise LedgerError("Could not verify a recorded deposit for the withdrawal limit.") from exc
            if type(amount) is not int or amount < 0:
                raise LedgerError("Could not verify a recorded deposit for the withdrawal limit.")
            deposits += amount

        # Owner-confirmed balances and individually reviewed legacy balances are
        # carry-forward capital; neither is inferred from an unverified old balance.
        opening = db.execute("""SELECT COALESCE(SUM(delta),0) FROM custody_entries
            WHERE account=? AND asset=? AND
                  (journal=? OR journal LIKE ?)""",
            (available(uid), asset, "opening:" + asset, "legacy-opening:" + asset + ":%")).fetchone()[0]
        realized_profit = db.execute("""SELECT COALESCE(SUM(
                COALESCE(p.proceeds_raw,p.proceeds_usdc,0)
                - COALESCE(p.cost_raw,p.cost_usdc,0)
                - COALESCE((SELECT SUM(e.amount_raw) FROM custody_partner_earnings e
                            WHERE e.position_id=p.id),0)),0)
            FROM custody_positions p
            WHERE p.uid=? AND p.funding_asset=? AND p.state='CLOSED'""",
            (uid, asset)).fetchone()[0]
        partner_income = db.execute("""SELECT COALESCE(SUM(amount_raw),0)
            FROM custody_partner_earnings
            WHERE partner_uid=? AND asset=? AND state IN ('CREDITED','PAID')""",
            (uid, asset)).fetchone()[0]
        withdrawal_commissions = db.execute("""SELECT COALESCE(SUM(e.delta),0)
            FROM custody_entries e
            JOIN custody_operations o ON e.journal='complete:' || o.id
            WHERE e.account=? AND e.asset=? AND o.kind='WITHDRAW'
              AND o.asset=? AND o.state='COMPLETED'""",
            (available(uid), asset, asset)).fetchone()[0]
        monthly_fee_income = 0
        if asset == SOL:
            monthly_fee_income = db.execute("""SELECT COALESCE(SUM(amount_raw),0)
                FROM custody_monthly_trading_fee_charges WHERE owner_uid=?""",
                (uid,)).fetchone()[0]
        completed_withdrawals = db.execute("""SELECT COALESCE(SUM(raw),0)
            FROM custody_operations WHERE uid=? AND asset=?
            AND kind IN ('WITHDRAW','PARTNER_PAYOUT') AND state='COMPLETED'""",
            (uid, asset)).fetchone()[0]
        return max(0, deposits + opening + realized_profit + partner_income
                   + withdrawal_commissions + monthly_fee_income - completed_withdrawals)

    def withdrawal_entitlement(self, uid, asset):
        if type(uid) is not int or uid <= 0 or asset not in (USDC, SOL):
            raise LedgerError("Invalid withdrawal entitlement request.")
        with self.db() as db:
            return self._withdrawal_entitlement(db, uid, asset)

    def withdrawal_capacity(self, uid, asset):
        """Limit a withdrawal to both actual available funds and verified lifetime entitlement."""
        if type(uid) is not int or uid <= 0 or asset not in (USDC, SOL):
            raise LedgerError("Invalid withdrawal capacity request.")
        with self.db() as db:
            balance = _balance(db, available(uid), asset)
            entitlement = self._withdrawal_entitlement(db, uid, asset)
            return min(balance, entitlement)

    @staticmethod
    def _legacy_usdc_raw(value):
        try:
            amount = Decimal(str(value))
        except (InvalidOperation, ValueError, TypeError):
            return None
        if not amount.is_finite() or amount < 0:
            return None
        raw = amount * Decimal(1_000_000)
        if raw != raw.to_integral_value() or raw > Decimal(2**63 - 1):
            return None
        return int(raw)

    @staticmethod
    def _legacy_has_custody_activity(db, uid):
        tables = {row["name"] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        checks = (
            ("custody_invoices", "uid"),
            ("custody_tx_deposits", "uid"),
            ("custody_positions", "uid"),
            ("custody_operations", "uid"),
        )
        for table, column in checks:
            if table in tables and db.execute(
                    "SELECT 1 FROM " + table + " WHERE " + column + "=? LIMIT 1", (uid,)).fetchone():
                return True
        if "custody_partner_earnings" in tables and db.execute(
                "SELECT 1 FROM custody_partner_earnings WHERE partner_uid=? OR trader_uid=? LIMIT 1",
                (uid, uid)).fetchone():
            return True
        if "custody_entries" in tables and db.execute(
                "SELECT 1 FROM custody_entries WHERE account=? LIMIT 1", (available(uid),)).fetchone():
            return True
        return False

    @classmethod
    def _legacy_balance_snapshot(cls, db, backing_raw):
        if type(backing_raw) is not int or backing_raw < 0:
            raise LedgerError("Invalid on-chain USDC backing for legacy balance preview.")
        tables = {row["name"] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        source_rows = []
        eligible = []
        skipped = {}

        def skip(reason):
            skipped[reason] = skipped.get(reason, 0) + 1

        if "users" not in tables:
            skip("legacy_table_missing")
        else:
            columns = {row["name"] for row in db.execute("PRAGMA table_info(users)")}
            if not {"telegram_id", "balance_usdc"}.issubset(columns):
                skip("legacy_schema_unrecognized")
            else:
                seen = set()
                duplicate_ids = set()
                rows = db.execute(
                    "SELECT telegram_id,balance_usdc FROM users ORDER BY telegram_id").fetchall()
                for row in rows:
                    source_rows.append([repr(row["telegram_id"]), repr(row["balance_usdc"])])
                    try:
                        uid = int(row["telegram_id"])
                    except (TypeError, ValueError, OverflowError):
                        skip("invalid_user_id")
                        continue
                    if uid <= 0:
                        skip("invalid_user_id")
                        continue
                    if uid in seen:
                        duplicate_ids.add(uid)
                        continue
                    seen.add(uid)
                    raw = cls._legacy_usdc_raw(row["balance_usdc"])
                    if raw is None:
                        skip("invalid_or_excess_precision")
                    elif raw == 0:
                        continue
                    elif db.execute("SELECT 1 FROM custody_journals WHERE id=?",
                                    ("legacy-opening:" + USDC + ":" + str(uid),)).fetchone():
                        skip("already_imported")
                    elif _balance(db, available(uid), USDC) != 0 or cls._legacy_has_custody_activity(db, uid):
                        skip("new_custody_activity")
                    else:
                        eligible.append((uid, raw))
                if duplicate_ids:
                    duplicate_count = 0
                    for uid in sorted(duplicate_ids):
                        eligible = [item for item in eligible if item[0] != uid]
                        duplicate_count += 2
                    skipped["duplicate_user_id"] = duplicate_count

        eligible.sort()
        liabilities = db.execute(
            "SELECT COALESCE(SUM(raw),0) FROM custody_accounts WHERE asset=? AND account!='external'",
            (USDC,)).fetchone()[0]
        free_backing = max(0, backing_raw - liabilities)
        candidate_raw = sum(raw for _, raw in eligible)
        backed = candidate_raw <= free_backing
        snapshot = {
            "source_rows": source_rows,
            "eligible": eligible,
            "skipped": sorted(skipped.items()),
            "backing_raw": backing_raw,
            "liabilities_raw": liabilities,
            "free_backing_raw": free_backing,
        }
        digest = hashlib.sha256(json.dumps(
            snapshot, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        return {
            "digest": digest,
            "candidate_count": len(eligible),
            "candidate_raw": candidate_raw,
            "eligible_count": len(eligible) if backed else 0,
            "amount_raw": candidate_raw if backed else 0,
            "backed": backed,
            "backing_available_raw": free_backing,
            "skipped": skipped,
            "can_import": bool(eligible) and backed,
            "_eligible": eligible,
        }

    def preview_legacy_balances(self, backing_raw):
        """Read-only preview of unambiguous, fully backed legacy USDC balances."""
        with self.db() as db:
            return self._legacy_balance_snapshot(db, backing_raw)

    def import_legacy_balances(self, expected_digest, backing_raw):
        """Atomically import the exact previewed balances without sending a transaction."""
        if not isinstance(expected_digest, str) or len(expected_digest) != 64:
            raise LedgerError("A fresh owner-confirmed preview is required.")
        with self.db() as db:
            preview = self._legacy_balance_snapshot(db, backing_raw)
            if preview["digest"] != expected_digest:
                raise LedgerError("Legacy balances or wallet backing changed. Preview again before importing.")
            if not preview["can_import"]:
                raise LedgerError("No unambiguous, fully backed legacy balances are available to import.")
            imported = 0
            amount = 0
            for uid, raw in preview["_eligible"]:
                journal_id = "legacy-opening:" + USDC + ":" + str(uid)
                if not self.post(db, journal_id, "owner-confirmed backed legacy real balance", [
                        ("external", USDC, -raw), (available(uid), USDC, raw)]):
                    raise LedgerError("A legacy balance was already imported; refresh the preview.")
                db.execute("""UPDATE custody_monthly_trading_fee_consents
                    SET retry_after=0 WHERE uid=? AND status='ACTIVE'""", (uid,))
                imported += 1
                amount += raw
            return {"imported_count": imported, "amount_raw": amount}

    def record_external_withdrawal(self, uid, signature, asset, raw, recipient, fee_lamports, block_time):
        if (type(uid) is not int or uid <= 0 or asset != USDC or type(raw) is not int or raw <= 0
                or type(fee_lamports) is not int or fee_lamports < 0 or not recipient or not signature):
            raise LedgerError("Invalid verified external withdrawal record.")
        request_key = "external-withdrawal:" + signature
        journal_id = "external-withdrawal:" + signature
        operation_id = str(uuid.uuid4())
        metadata = json.dumps({
            "source": "admin_confirmed_finalized_solana_transfer",
            "network_fee_lamports": fee_lamports,
            "block_time": int(block_time),
        }, sort_keys=True, separators=(",", ":"))
        with self.db() as db:
            existing = db.execute("SELECT * FROM custody_operations WHERE signature=?", (signature,)).fetchone()
            if existing:
                if (existing["uid"] == uid and existing["kind"] == "WITHDRAW"
                        and existing["asset"] == asset and existing["raw"] == raw
                        and existing["recipient"] == recipient and existing["state"] == "COMPLETED"):
                    return dict(existing), False
                raise LedgerError("This transaction signature is already used for another ledger entry.")
            if db.execute("SELECT 1 FROM custody_tx_deposits WHERE signature=?", (signature,)).fetchone():
                raise LedgerError("This signature is already recorded as a deposit.")
            if db.execute("SELECT 1 FROM custody_invoices WHERE signature=?", (signature,)).fetchone():
                raise LedgerError("This signature is already attached to a deposit invoice.")
            if db.execute("SELECT 1 FROM custody_operations WHERE state IN "
                          "('RESERVED','PREPARED','SUBMITTED','UNKNOWN') LIMIT 1").fetchone():
                raise LedgerError("Resolve the existing pending operation before recording this transfer.")
            balance = db.execute("SELECT raw FROM custody_accounts WHERE account=? AND asset=?",
                                 (available(uid), asset)).fetchone()
            if not balance or balance["raw"] < raw:
                raise LedgerError("The confirmed transfer exceeds your available USDC balance.")
            fee_reserve = db.execute("SELECT raw FROM custody_accounts WHERE account='treasury' AND asset=?",
                                     (SOL,)).fetchone()
            if fee_lamports and (not fee_reserve or fee_reserve["raw"] < fee_lamports):
                raise LedgerError("The operator fee reserve cannot cover this transaction fee.")
            lines = [(available(uid), asset, -raw), ("external", asset, raw)]
            if fee_lamports:
                lines.extend([("treasury", SOL, -fee_lamports), ("external", SOL, fee_lamports)])
            self.post(db, journal_id, "record already-finalized external withdrawal " + signature, lines)
            db.execute("""INSERT INTO custody_operations
                (id,request_key,uid,kind,asset,raw,fee_cap,recipient,position_id,metadata,
                 signature,wire,state,created_at)
                VALUES(?,?,?,'WITHDRAW',?,?,?,?,NULL,?,?,NULL,'COMPLETED',?)""",
                (operation_id, request_key, uid, asset, raw, fee_lamports, recipient,
                 metadata, signature, int(block_time)))
            row = db.execute("SELECT * FROM custody_operations WHERE id=?", (operation_id,)).fetchone()
            return dict(row), True

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
                raise LedgerError("Too many open deposit requests. Wait for one to expire before creating another.")
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
            db.execute("""UPDATE custody_monthly_trading_fee_consents
                SET retry_after=0 WHERE uid=? AND status='ACTIVE'""", (uid,))
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
            db.execute("""UPDATE custody_monthly_trading_fee_consents
                SET retry_after=0 WHERE uid=? AND status='ACTIVE'""", (invoice["uid"],))
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

    def waive_owner_shortfall(self, owner_uid, usdc_raw, sol_raw, request_id, backing):
        """Record an owner-approved loss absorption from the owner's available balances."""
        if (type(owner_uid) is not int or owner_uid <= 0
                or type(usdc_raw) is not int or type(sol_raw) is not int
                or not 0 <= usdc_raw <= 2**63 - 1
                or not 0 <= sol_raw <= 2**63 - 1
                or usdc_raw + sol_raw == 0 or not isinstance(backing, dict)):
            raise LedgerError("Invalid owner shortfall adjustment.")
        try:
            request_id = str(uuid.UUID(str(request_id)))
        except (ValueError, TypeError, AttributeError) as exc:
            raise LedgerError("A valid adjustment request ID is required.") from exc

        journal_id = "owner-shortfall-waiver:" + request_id
        note = "owner-confirmed shared-wallet shortfall absorption; no on-chain transfer"
        lines = []
        if usdc_raw:
            lines.extend([(available(owner_uid), USDC, -usdc_raw), ("external", USDC, usdc_raw)])
        if sol_raw:
            lines.extend([(available(owner_uid), SOL, -sol_raw), ("external", SOL, sol_raw)])
        expected_lines = sorted(lines)
        with self.db() as db:
            existing = db.execute("SELECT note FROM custody_journals WHERE id=?",
                                  (journal_id,)).fetchone()
            if existing:
                recorded_lines = sorted(tuple(row) for row in db.execute(
                    "SELECT account,asset,delta FROM custody_entries WHERE journal=?",
                    (journal_id,)).fetchall())
                if existing["note"] == note and recorded_lines == expected_lines:
                    return False
                raise LedgerError("This adjustment request ID was already used for a different ledger entry.")

            liabilities = {row["asset"]: int(row["raw"] or 0) for row in db.execute(
                "SELECT asset,SUM(raw) raw FROM custody_accounts WHERE account!='external' GROUP BY asset")}
            deficits = {}
            for asset, owed in liabilities.items():
                actual = int(backing.get(asset, {}).get("raw", 0))
                if actual < 0:
                    raise LedgerError("Invalid finalized wallet balance.")
                deficits[asset] = max(0, owed - actual)
            if (deficits.get(USDC, 0) != usdc_raw or deficits.get(SOL, 0) != sol_raw
                    or any(raw > 0 for asset, raw in deficits.items() if asset not in (USDC, SOL))):
                raise LedgerError("The finalized wallet shortfall changed. No balance was changed.")
            if _balance(db, available(owner_uid), USDC) < usdc_raw:
                raise LedgerError("Your available USDC balance is too low for this adjustment.")
            if _balance(db, available(owner_uid), SOL) < sol_raw:
                raise LedgerError("Your available SOL balance is too low for this adjustment.")
            self.post(db, journal_id, note, lines)
        return True

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
            db.execute("""UPDATE custody_monthly_trading_fee_consents
                SET retry_after=0 WHERE uid=? AND status='ACTIVE'""", (uid,))

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
                cooldown = db.execute("""SELECT blocked_until FROM custody_loss_reentry_cooldowns
                    WHERE asset=?""", (metadata["output_asset"],)).fetchone()
                if cooldown and cooldown["blocked_until"] > int(time.time()):
                    raise LedgerError(
                        "BLOCKED: token is under a global 24-hour re-entry cooldown after a completed trade; "
                        "no funds were reserved.")
            elif kind == "PARTNER_PAYOUT":
                source = partner_account(uid)
                if asset not in (USDC, SOL) or not recipient or raw > _balance(db, source, asset):
                    raise LedgerError("Партнёрская выплата не покрыта начислениями.")
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

    def fail(self, oid, network_proven=False, fee=0, recovery_audit=None, failure_reason=""):
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
            source = (position_account(row["position_id"]) if row["kind"] == "SELL" else
                      partner_account(row["uid"]) if row["kind"] == "PARTNER_PAYOUT" else available(row["uid"]))
            self.post(db, "failed:" + oid, "release only proven unsent/failed", [
                (reserve_account(oid), row["asset"], -row["raw"]), (source, row["asset"], row["raw"]),
                (reserve_account(oid), SOL, -row["fee_cap"]), ("treasury", SOL, row["fee_cap"] - fee),
                ("external", SOL, fee)])
            if row["kind"] == "SELL":
                db.execute("UPDATE custody_positions SET state='OPEN' WHERE id=?", (row["position_id"],))
            elif row["kind"] == "PARTNER_PAYOUT":
                earning = db.execute("""SELECT attempts FROM custody_partner_earnings
                    WHERE operation_id=? AND state='SUBMITTED'""", (oid,)).fetchone()
                if not earning:
                    raise LedgerError("Партнёрское начисление не связано с этой операцией.")
                db.execute("""UPDATE custody_partner_earnings SET
                    state=CASE WHEN attempts+1>=3 THEN 'NEEDS_REVIEW' ELSE 'QUEUED' END,
                    attempts=attempts+1,next_attempt=?,operation_id=NULL
                    WHERE operation_id=? AND state='SUBMITTED'""",
                    (int(time.time()) + 60 * (2 ** min(earning["attempts"], 6)), oid))
            db.execute("UPDATE custody_operations SET state='FAILED',failure_reason=? WHERE id=?",
                       (str(failure_reason or "")[:300], oid))
            if row["kind"] == "SELL" and recovery_audit is None:
                info = json.loads(row["metadata"])
                exit_reason = info.get("exit_reason")
                if exit_reason in ("STOP_LOSS", "TRAILING_STOP"):
                    db.execute("""INSERT OR IGNORE INTO custody_trade_notifications
                        (operation_id,uid,payload) VALUES(?,?,?)""",
                        (oid, row["uid"], json.dumps({
                            "kind": "TRAILING_STOP_FAILED" if exit_reason == "TRAILING_STOP"
                                else "STOP_LOSS_FAILED",
                            "symbol": info.get("symbol", "TOKEN"),
                            "signature": row["signature"],
                        }, separators=(",", ":"))))
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
                # A proven-absent, expired BUY is safe to retire without disabling
                # modes: its original AI analysis remains claimed and cannot resend.
                # Pause modes after other recovered operations; preserve an
                # explicitly requested close-all queue after a SELL.
                db.execute("""UPDATE custody_users SET
                    trading=CASE WHEN ?='BUY' THEN trading ELSE 0 END,
                    sniper_trading=CASE WHEN ?='BUY' THEN sniper_trading ELSE 0 END,
                    close_requested=CASE WHEN ?='SELL' THEN close_requested ELSE 0 END
                    WHERE uid=?""",
                    (row["kind"], row["kind"], row["kind"], row["uid"]))
                db.execute("""INSERT INTO custody_recovery_notifications(operation_id,uid,payload)
                    VALUES(?,?,?)""", (oid, row["uid"], json.dumps({
                        "kind": row["kind"], "asset": row["asset"], "raw": row["raw"],
                        "signature": row["signature"]})))

    def recovery_notifications(self, now):
        with self.db() as db:
            return [dict(row) for row in db.execute("""
                SELECT * FROM custody_recovery_notifications
                WHERE delivered_at IS NULL AND next_attempt<=?
                ORDER BY next_attempt,operation_id LIMIT 5""", (now,))]

    def execution_report(self, uid, report=None):
        with self.db() as db:
            if report is not None:
                db.execute("""INSERT INTO custody_execution_checks(uid,report) VALUES(?,?)
                    ON CONFLICT(uid) DO UPDATE SET report=excluded.report""", (uid, json.dumps(report)))
            row = db.execute("SELECT report FROM custody_execution_checks WHERE uid=?", (uid,)).fetchone()
            return json.loads(row["report"]) if row else None

    def entry_cost_report(self, uid, report=None):
        with self.db() as db:
            if report is not None:
                db.execute("""INSERT INTO custody_entry_cost_checks(uid,report) VALUES(?,?)
                    ON CONFLICT(uid) DO UPDATE SET report=excluded.report""", (uid, json.dumps(report)))
            row = db.execute("SELECT report FROM custody_entry_cost_checks WHERE uid=?", (uid,)).fetchone()
            return json.loads(row["report"]) if row else None

    def recovery_notification_result(self, oid, now, delivered, retry_after=60, error=None):
        with self.db() as db:
            db.execute("""UPDATE custody_recovery_notifications SET attempts=attempts+1,
                delivered_at=?, next_attempt=?, last_error=?
                WHERE operation_id=? AND delivered_at IS NULL""",
                (now if delivered else None, now + retry_after,
                 None if delivered else error, oid))

    def withdrawal_notifications(self, now):
        with self.db() as db:
            return [dict(row) for row in db.execute("""
                SELECT * FROM custody_withdrawal_notifications
                WHERE delivered_at IS NULL AND next_attempt<=?
                ORDER BY next_attempt,operation_id LIMIT 10""", (now,))]

    def withdrawal_notification_result(self, oid, now, delivered, retry_after=60, error=None):
        with self.db() as db:
            db.execute("""UPDATE custody_withdrawal_notifications SET attempts=attempts+1,
                delivered_at=?, next_attempt=?, last_error=?
                WHERE operation_id=? AND delivered_at IS NULL""",
                (now if delivered else None, now + retry_after,
                 None if delivered else error, oid))

    def trade_notifications(self, now):
        with self.db() as db:
            # A process crash during send has an ambiguous Telegram outcome. Do not
            # resend it automatically; that could create a duplicate phone push.
            db.execute("""UPDATE custody_trade_notifications SET state='UNKNOWN',
                last_error='Delivery outcome unknown after interrupted send'
                WHERE state='SENDING' AND last_attempt_at<=?""", (now - 60,))
            return [dict(row) for row in db.execute("""
                SELECT * FROM custody_trade_notifications
                WHERE state='QUEUED' AND next_attempt<=?
                ORDER BY next_attempt,operation_id LIMIT 10""", (now,))]

    def claim_trade_notification(self, oid, now):
        with self.db() as db:
            return db.execute("""UPDATE custody_trade_notifications
                SET state='SENDING',attempts=attempts+1,last_attempt_at=?
                WHERE operation_id=? AND state='QUEUED' AND next_attempt<=?""",
                (now, oid, now)).rowcount == 1

    def trade_notification_result(self, oid, now, outcome, retry_after=60, error=None):
        if outcome not in ("DELIVERED", "RETRY", "FAILED", "UNKNOWN"):
            raise LedgerError("Invalid trade-notification outcome.")
        with self.db() as db:
            row = db.execute("""SELECT attempts FROM custody_trade_notifications
                WHERE operation_id=? AND state='SENDING'""", (oid,)).fetchone()
            if not row:
                return
            if outcome == "DELIVERED":
                db.execute("""UPDATE custody_trade_notifications SET state='DELIVERED',
                    delivered_at=?,last_error=NULL WHERE operation_id=? AND state='SENDING'""",
                    (now, oid))
            elif outcome == "RETRY" and row["attempts"] < 6:
                db.execute("""UPDATE custody_trade_notifications SET state='QUEUED',
                    next_attempt=?,last_error=? WHERE operation_id=? AND state='SENDING'""",
                    (now + max(1, min(int(retry_after), 3600)),
                     str(error or "Telegram temporarily rejected the message.")[:160], oid))
            else:
                state = "FAILED" if outcome in ("RETRY", "FAILED") else "UNKNOWN"
                db.execute("""UPDATE custody_trade_notifications SET state=?,last_error=?
                    WHERE operation_id=? AND state='SENDING'""",
                    (state, str(error or "Delivery outcome is unknown.")[:160], oid))

    def finalize(self, oid, spent, fee, received=0):
        with self.db() as db:
            row = db.execute("SELECT * FROM custody_operations WHERE id=?", (oid,)).fetchone()
            if not row or row["state"] == "COMPLETED":
                return
            if row["state"] not in ("PREPARED", "SUBMITTED", "UNKNOWN"):
                raise LedgerError("Неверное состояние финализации.")
            if not isinstance(fee, int) or fee > row["fee_cap"] or spent <= 0 or spent > row["raw"]:
                raise LedgerError("Фактическая сумма выходит за зарезервированные пределы.")
            if row["kind"] == "PARTNER_PAYOUT" and spent != row["raw"]:
                raise LedgerError("Партнёрская выплата должна совпасть с точной суммой резерва.")
            info = json.loads(row["metadata"])
            commission_raw = 0
            commission_owner_uid = None
            if row["kind"] == "WITHDRAW":
                commission_raw = info.get("withdrawal_fee_raw", 0)
                net_raw = info.get("withdrawal_net_raw", row["raw"])
                commission_owner_uid = info.get("withdrawal_commission_owner_uid")
                if (type(commission_raw) is not int or commission_raw < 0
                        or type(net_raw) is not int or net_raw <= 0
                        or net_raw + commission_raw != row["raw"] or spent != net_raw):
                    raise LedgerError("Withdrawal does not match the reserved amount and fee.")
                if commission_raw and (
                        type(commission_owner_uid) is not int or commission_owner_uid <= 0
                        or commission_owner_uid == row["uid"]):
                    raise LedgerError("Could not verify the withdrawal commission recipient.")
            account = reserve_account(oid)
            source = (position_account(row["position_id"]) if row["kind"] == "SELL" else
                      partner_account(row["uid"]) if row["kind"] == "PARTNER_PAYOUT" else available(row["uid"]))
            lines = [(account, row["asset"], -spent), ("external", row["asset"], spent),
                (account, SOL, -fee), ("external", SOL, fee),
                (account, SOL, -(row["fee_cap"] - fee)), ("treasury", SOL, row["fee_cap"] - fee)]
            if row["raw"] > spent:
                retained = row["raw"] - spent
                if row["kind"] == "WITHDRAW" and commission_raw:
                    lines += [(account, row["asset"], -retained),
                              (available(commission_owner_uid), row["asset"], retained)]
                else:
                    lines += [(account, row["asset"], -retained), (source, row["asset"], retained)]
            commission_total = 0
            if row["kind"] in ("BUY", "SELL"):
                if received <= 0:
                    raise LedgerError("Нет подтверждённого результата swap.")
                output = info["output_asset"]
                target = position_account(oid) if row["kind"] == "BUY" else available(row["uid"])
                if row["kind"] == "SELL":
                    position = db.execute("SELECT * FROM custody_positions WHERE id=? AND uid=?",
                                           (row["position_id"], row["uid"])).fetchone()
                    if not position or position["state"] != "RESERVED" or position["funding_asset"] != output:
                        raise LedgerError("Продажа позиции не соответствует сохранённому состоянию.")
                    commission_accounts, commission_total = self._record_partner_profit(
                        db, position, received - position["cost_raw"])
                    if commission_total > received:
                        raise LedgerError("Партнёрские комиссии превышают полученную прибыль.")
                    lines += [("external", output, -received),
                              (target, output, received - commission_total)]
                    lines.extend((account, output, amount)
                                 for account, amount in commission_accounts.items())
                else:
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
                closed_at = int(time.time())
                db.execute("""UPDATE custody_positions SET state='CLOSED',sell_tx=?,proceeds_usdc=?,
                    proceeds_raw=?,closed_at=? WHERE id=? AND uid=?""",
                    (row["signature"], received if info["output_asset"] == USDC else None,
                     received, closed_at, row["position_id"], row["uid"]))
                blocked_until = closed_at + REENTRY_COOLDOWN_SECONDS
                db.execute("""INSERT INTO custody_loss_reentry_cooldowns
                    (asset,blocked_until,last_loss_at) VALUES(?,?,?)
                    ON CONFLICT(asset) DO UPDATE SET
                        blocked_until=MAX(custody_loss_reentry_cooldowns.blocked_until,excluded.blocked_until),
                        last_loss_at=MAX(custody_loss_reentry_cooldowns.last_loss_at,excluded.last_loss_at)""",
                    (position["asset"], blocked_until, closed_at))
            elif row["kind"] == "PARTNER_PAYOUT":
                db.execute("""UPDATE custody_partner_earnings SET state='PAID',signature=?
                    WHERE operation_id=? AND state='SUBMITTED'""", (row["signature"], oid))
            db.execute("UPDATE custody_operations SET state='COMPLETED' WHERE id=?", (oid,))
            if row["kind"] == "WITHDRAW":
                db.execute("""INSERT OR IGNORE INTO custody_withdrawal_notifications
                    (operation_id,uid,payload) VALUES(?,?,?)""", (oid, row["uid"], json.dumps({
                        "asset": row["asset"], "raw": spent, "requested_raw": row["raw"],
                        "commission_raw": commission_raw, "recipient": row["recipient"],
                        "signature": row["signature"]})))
            elif row["kind"] in ("BUY", "SELL"):
                if row["kind"] == "BUY":
                    notification = {
                        "kind": "BUY", "symbol": info["symbol"],
                        "token_raw": received, "token_decimals": info["decimals"],
                        "funding_asset": row["asset"], "funding_raw": spent,
                        "signature": row["signature"],
                    }
                else:
                    exit_reason = info.get("exit_reason")
                    if exit_reason not in ("STOP_LOSS", "TAKE_PROFIT", "TRAILING_STOP",
                                            "RESISTANCE_TARGET"):
                        exit_reason = None
                    notification = {
                        "kind": "SELL", "symbol": position["symbol"],
                        "token_raw": spent, "token_decimals": position["decimals"],
                        "funding_asset": output, "funding_raw": received - commission_total,
                        "exit_reason": exit_reason, "signature": row["signature"],
                    }
                db.execute("""INSERT OR IGNORE INTO custody_trade_notifications
                    (operation_id,uid,payload) VALUES(?,?,?)""",
                    (oid, row["uid"], json.dumps(notification, separators=(",", ":"))))

    def positions(self, uid, state=None):
        with self.db() as db:
            rows = db.execute("""SELECT p.*,
                COALESCE((SELECT SUM(e.amount_raw) FROM custody_partner_earnings e
                          WHERE e.position_id=p.id),0) AS partner_commission_raw
                FROM custody_positions p WHERE p.uid=?""" +
                (" AND state=?" if state else "") + " ORDER BY created_at DESC",
                (uid, state) if state else (uid,))
            return [dict(r) for r in rows]

    def record_position_peak(self, uid, position_id, price):
        if type(price) not in (int, float) or not math.isfinite(price) or price <= 0:
            return None
        with self.db() as db:
            db.execute("""UPDATE custody_positions SET peak_price=MAX(peak_price,?)
                WHERE id=? AND uid=? AND state='OPEN'""",
                (float(price), position_id, uid))
            row = db.execute("""SELECT peak_price FROM custody_positions
                WHERE id=? AND uid=? AND state='OPEN'""", (position_id, uid)).fetchone()
            return float(row["peak_price"]) if row else None

    def entry_cooldown_until(self, asset, now=None):
        now = int(time.time()) if now is None else int(now)
        with self.db() as db:
            row = db.execute("""SELECT blocked_until FROM custody_loss_reentry_cooldowns
                WHERE asset=? AND blocked_until>?""", (asset, now)).fetchone()
            return int(row["blocked_until"]) if row else None

    # Kept for callers from older releases; the cooldown now applies after every close.
    def loss_entry_cooldown_until(self, asset, now=None):
        return self.entry_cooldown_until(asset, now)

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
            # Automated modes pause on restart. An explicit close-all request is
            # a queued user action and remains until all positions are closed.
            db.execute("UPDATE custody_users SET trading=0,sniper_trading=0")
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
            latest_attempt = db.execute("SELECT MAX(created_at) FROM custody_ai_calls").fetchone()[0]
            latest_success = db.execute("SELECT MAX(created_at) FROM custody_ai_analyses").fetchone()[0]
            count = db.execute("SELECT COUNT(*) FROM custody_ai_calls WHERE created_at>=?", (now - now % 86400,)).fetchone()[0]
            if count >= daily_limit:
                raise LedgerError("Дневной лимит обращений к AI исчерпан. Новые входы остановлены.")
            if latest_success is not None and now - latest_success < interval:
                raise LedgerError("AI ожидает следующего интервала анализа. Новые входы пока остановлены.")
            if (latest_attempt is not None
                    and (latest_success is None or latest_attempt > latest_success)
                    and now - latest_attempt < 60):
                raise LedgerError(
                    "AI предыдущий запрос завершился ошибкой. Повторите через 60 секунд; "
                    "попытка учитывается в дневном лимите.")
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
