"""Isolated virtual accounting. This file never opens the real custody database."""
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager

from wallet_runtime import WalletError

SCALE = 1_000_000
PARTNER_BPS = 175  # Worst-case three levels: 1% + 0.5% + 0.25%, modeled only.


class PaperStore:
    def __init__(self, path):
        self.path = path
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS runs(
                    id TEXT PRIMARY KEY, uid INTEGER NOT NULL, current INTEGER NOT NULL DEFAULT 1,
                    enabled INTEGER NOT NULL DEFAULT 1, revision INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL, initial_raw INTEGER NOT NULL, order_raw INTEGER NOT NULL,
                    cash_raw INTEGER NOT NULL, realized_pre_raw INTEGER NOT NULL DEFAULT 0,
                    hwm_raw INTEGER NOT NULL DEFAULT 0, partner_raw INTEGER NOT NULL DEFAULT 0,
                    peak_raw INTEGER NOT NULL, max_dd_bps INTEGER NOT NULL DEFAULT 0,
                    last_tick INTEGER, status TEXT NOT NULL DEFAULT 'Waiting for market data');
                CREATE UNIQUE INDEX IF NOT EXISTS current_paper_run ON runs(uid) WHERE current=1;
                CREATE TABLE IF NOT EXISTS positions(
                    id TEXT PRIMARY KEY, run_id TEXT NOT NULL, symbol TEXT NOT NULL,
                    mint TEXT NOT NULL, decimals INTEGER NOT NULL, raw INTEGER NOT NULL,
                    entry_raw INTEGER NOT NULL, entry_expense_raw INTEGER NOT NULL,
                    opened_at INTEGER NOT NULL, entry_price REAL NOT NULL, ai_reason TEXT NOT NULL,
                    mark_net_raw INTEGER, mark_gross_raw INTEGER, mark_expense_raw INTEGER, mark_at INTEGER);
                CREATE UNIQUE INDEX IF NOT EXISTS paper_one_asset ON positions(run_id,symbol);
                CREATE TABLE IF NOT EXISTS trades(
                    id TEXT PRIMARY KEY, run_id TEXT NOT NULL, symbol TEXT NOT NULL,
                    entry_raw INTEGER NOT NULL, proceeds_raw INTEGER NOT NULL,
                    expenses_raw INTEGER NOT NULL, partner_raw INTEGER NOT NULL,
                    gross_raw INTEGER NOT NULL, net_raw INTEGER NOT NULL,
                    opened_at INTEGER NOT NULL, closed_at INTEGER NOT NULL,
                    entry_reason TEXT NOT NULL, exit_reason TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS claims(
                    run_id TEXT NOT NULL, analysis_id TEXT NOT NULL, PRIMARY KEY(run_id,analysis_id));
                CREATE TABLE IF NOT EXISTS sol_benchmarks(
                    run_id TEXT PRIMARY KEY, started_at INTEGER, raw INTEGER,
                    entry_raw INTEGER, entry_expense_raw INTEGER, cash_raw INTEGER,
                    mark_raw INTEGER, mark_expense_raw INTEGER, mark_at INTEGER);
            """)
        os.chmod(path, 0o600)

    @contextmanager
    def db(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def current(self, uid):
        with self.db() as db:
            row = db.execute("SELECT * FROM runs WHERE uid=? AND current=1", (uid,)).fetchone()
            return dict(row) if row else None

    def active(self):
        with self.db() as db:
            return [r[0] for r in db.execute("SELECT uid FROM runs WHERE current=1 AND enabled=1")]

    def start(self, uid, initial_raw, order_raw, confirm=False):
        if (type(initial_raw) is not int or type(order_raw) is not int
                or not SCALE <= order_raw <= initial_raw <= 1_000_000 * SCALE):
            raise WalletError("Use virtual capital from 1 to 1,000,000 USDC and an order from 1 USDC up to that capital.", 400)
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT id FROM runs WHERE uid=? AND current=1", (uid,)).fetchone() and confirm is not True:
                raise WalletError("Confirm archiving the current virtual run. Real funds are never changed.", 409)
            db.execute("UPDATE runs SET current=0,enabled=0,revision=revision+1 WHERE uid=? AND current=1", (uid,))
            run_id = uuid.uuid4().hex
            db.execute("""INSERT INTO runs(id,uid,created_at,initial_raw,order_raw,cash_raw,peak_raw)
                          VALUES(?,?,?,?,?,?,?)""", (run_id, uid, int(time.time()),
                          initial_raw, order_raw, initial_raw, initial_raw))
            db.execute("INSERT INTO sol_benchmarks(run_id) VALUES(?)", (run_id,))

    def pause(self, uid, enabled):
        with self.db() as db:
            changed = db.execute("""UPDATE runs SET enabled=?,revision=revision+1,status=?
                WHERE uid=? AND current=1""", (int(enabled), "Waiting for a fresh tick" if enabled
                else "Paused: virtual positions retained; no fills will be recorded.", uid)).rowcount
            if not changed:
                raise WalletError("Start a virtual run first.", 409)

    def positions(self, run_id):
        with self.db() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM positions WHERE run_id=? ORDER BY opened_at,id", (run_id,))]

    def claimed(self, run_id, analysis_id):
        with self.db() as db:
            return bool(db.execute("SELECT 1 FROM claims WHERE run_id=? AND analysis_id=?",
                                    (run_id, analysis_id)).fetchone())

    def valid(self, db, run):
        return db.execute("""SELECT * FROM runs
            WHERE id=? AND current=1 AND enabled=1 AND revision=?""",
            (run["id"], run["revision"])).fetchone()

    def note(self, run, status):
        with self.db() as db:
            db.execute("""UPDATE runs SET status=?,last_tick=?
                WHERE id=? AND current=1 AND enabled=1 AND revision=?""",
                (status[:650], int(time.time()), run["id"], run["revision"]))

    def buy(self, run, symbol, watch, token_raw, expense_raw, price, aid, reason):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            current = self.valid(db, run)
            if (not current or current["cash_raw"] < current["order_raw"] + expense_raw
                    or current["order_raw"] != run["order_raw"]
                    or db.execute("SELECT 1 FROM claims WHERE run_id=? AND analysis_id=?", (run["id"], aid)).fetchone()
                    or db.execute("SELECT count(*) FROM positions WHERE run_id=?", (run["id"],)).fetchone()[0] >= 5
                    or db.execute("SELECT 1 FROM positions WHERE run_id=? AND symbol=?", (run["id"], symbol)).fetchone()):
                return False
            db.execute("INSERT INTO claims VALUES(?,?)", (run["id"], aid))
            db.execute("""INSERT INTO positions(id,run_id,symbol,mint,decimals,raw,entry_raw,
                entry_expense_raw,opened_at,entry_price,ai_reason) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (uuid.uuid4().hex, run["id"], symbol, watch["mint"], watch["decimals"], token_raw,
                 run["order_raw"], expense_raw, int(time.time()), price, reason))
            db.execute("UPDATE runs SET cash_raw=cash_raw-?-? WHERE id=?",
                       (run["order_raw"], expense_raw, run["id"]))
            return True

    def mark(self, run, position, proceeds_raw, expense_raw, quoted_at=None):
        with self.db() as db:
            if self.valid(db, run):
                db.execute("""UPDATE positions SET mark_net_raw=?,mark_gross_raw=?,
                    mark_expense_raw=?,mark_at=? WHERE id=? AND run_id=?""",
                    (proceeds_raw-expense_raw, proceeds_raw, expense_raw,
                     int(time.time() if quoted_at is None else quoted_at),
                     position["id"], run["id"]))

    def close(self, run, position, proceeds_raw, expense_raw, reason, aid=None):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            current = self.valid(db, run)
            owned = db.execute("SELECT * FROM positions WHERE id=? AND run_id=?",
                                (position["id"], run["id"])).fetchone()
            if not current or not owned:
                return False
            if aid:
                if db.execute("SELECT 1 FROM claims WHERE run_id=? AND analysis_id=?", (run["id"], aid)).fetchone():
                    return False
                db.execute("INSERT INTO claims VALUES(?,?)", (run["id"], aid))
            gross = proceeds_raw-owned["entry_raw"]
            expenses = owned["entry_expense_raw"]+expense_raw
            cumulative = current["realized_pre_raw"]+gross-expenses
            basis = max(0, cumulative-current["hwm_raw"])
            partner = (basis*PARTNER_BPS+9999)//10000
            db.execute("""INSERT INTO trades VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (uuid.uuid4().hex, run["id"], owned["symbol"], owned["entry_raw"], proceeds_raw,
                 expenses, partner, gross, gross-expenses-partner, owned["opened_at"],
                 int(time.time()), owned["ai_reason"], reason))
            db.execute("DELETE FROM positions WHERE id=? AND run_id=?", (owned["id"], run["id"]))
            db.execute("""UPDATE runs SET cash_raw=cash_raw+?-?-?,realized_pre_raw=?,
                hwm_raw=max(hwm_raw,?),partner_raw=partner_raw+? WHERE id=?""",
                (proceeds_raw, expense_raw, partner, cumulative, cumulative, partner, run["id"]))
            return True

    def equity(self, run, positions, now):
        if any(p["mark_at"] is None or not 0 <= now-p["mark_at"] <= 60 for p in positions):
            return None
        unrealized_pre = sum(p["mark_net_raw"]-p["entry_raw"]-p["entry_expense_raw"] for p in positions)
        basis = max(0, run["realized_pre_raw"]+unrealized_pre-run["hwm_raw"])
        projected_partner = (basis*PARTNER_BPS+9999)//10000
        return run["cash_raw"]+sum(p["mark_net_raw"] for p in positions)-projected_partner

    def checkpoint(self, run):
        with self.db() as db:
            current = self.valid(db, run)
            if not current:
                return
            current = dict(current)
            positions = [dict(p) for p in db.execute("SELECT * FROM positions WHERE run_id=?", (run["id"],))]
            equity = self.equity(current, positions, int(time.time()))
            if equity is not None:
                peak = max(current["peak_raw"], equity)
                dd = max(0, (peak-equity)*10000//peak)
                db.execute("UPDATE runs SET peak_raw=?,max_dd_bps=max(max_dd_bps,?) WHERE id=?",
                           (peak, dd, run["id"]))

    def report(self, uid):
        run = self.current(uid)
        if not run:
            return {"mode": "paper_only", "run": None}
        positions = self.positions(run["id"])
        with self.db() as db:
            trades = [dict(r) for r in db.execute(
                "SELECT * FROM trades WHERE run_id=? ORDER BY closed_at,rowid", (run["id"],))]
            archives = [dict(r) for r in db.execute("""SELECT r.created_at,r.initial_raw,
                (SELECT count(*) FROM trades t WHERE t.run_id=r.id) AS closed_count,
                (SELECT coalesce(sum(net_raw),0) FROM trades t WHERE t.run_id=r.id) AS net_raw,
                (SELECT count(*) FROM positions p WHERE p.run_id=r.id) AS open_count
                FROM runs r WHERE r.uid=? AND r.current=0 ORDER BY r.created_at DESC,rowid DESC LIMIT 5""", (uid,))]
        equity = self.equity(run, positions, int(time.time()))
        net = sum(t["net_raw"] for t in trades)
        streak = longest = 0
        for trade in trades:
            streak = streak+1 if trade["net_raw"] < 0 else 0
            longest = max(longest, streak)
        return {"mode": "paper_only", "run": {
            "enabled": bool(run["enabled"]), "created_at": run["created_at"],
            "last_tick": run["last_tick"], "status": run["status"],
            "initial_usdc": run["initial_raw"]/SCALE, "order_usdc": run["order_raw"]/SCALE,
            "cash_usdc": run["cash_raw"]/SCALE, "equity_usdc": equity/SCALE if equity is not None else None,
            "equity_fresh": equity is not None, "realized_net_usdc": net/SCALE,
            "sol_comparison": self.sol_comparison(run, positions, equity),
            "gross_realized_usdc": sum(t["gross_raw"] for t in trades)/SCALE,
            "realized_expenses_usdc": sum(t["expenses_raw"] for t in trades)/SCALE,
            "modeled_partner_usdc": run["partner_raw"]/SCALE, "closed_count": len(trades),
            "win_rate_pct": sum(t["net_raw"] > 0 for t in trades)*100/len(trades) if trades else None,
            "max_sampled_drawdown_pct": run["max_dd_bps"]/100, "longest_loss_streak": longest,
            "positions": [{"symbol": p["symbol"], "quantity": p["raw"]/10**p["decimals"],
                           "entry_usdc": p["entry_raw"]/SCALE, "mark_at": p["mark_at"]} for p in positions],
            "trades": [{"symbol": t["symbol"], "closed_at": t["closed_at"],
                        "gross_usdc": t["gross_raw"]/SCALE, "expenses_usdc": t["expenses_raw"]/SCALE,
                        "partner_usdc": t["partner_raw"]/SCALE, "net_usdc": t["net_raw"]/SCALE,
                        "entry_reason": t["entry_reason"], "exit_reason": t["exit_reason"]}
                       for t in reversed(trades[-20:])],
            "archives": [{"created_at": a["created_at"], "initial_usdc": a["initial_raw"]/SCALE,
                          "net_usdc": a["net_raw"]/SCALE, "closed_count": a["closed_count"],
                          "open_count": a["open_count"]} for a in archives]}}

    def benchmark(self, run_id):
        with self.db() as db:
            row = db.execute("SELECT * FROM sol_benchmarks WHERE run_id=?", (run_id,)).fetchone()
            return dict(row) if row else None

    def benchmark_save(self, run, values):
        allowed = {"started_at", "raw", "entry_raw", "entry_expense_raw", "cash_raw",
                   "mark_raw", "mark_expense_raw", "mark_at"}
        if not values or set(values)-allowed:
            raise ValueError("Invalid benchmark fields")
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            if not self.valid(db, run):
                return False
            db.execute("UPDATE sol_benchmarks SET "+",".join(k+"=?" for k in values)+" WHERE run_id=?",
                       (*values.values(), run["id"]))
            return True

    def sol_comparison(self, run, positions, equity):
        b = self.benchmark(run["id"])
        result = {"status": "unavailable", "reason": "", "started_at": None}
        if b is None:
            result["reason"] = "This run predates the benchmark. Start a new run; no historical price is invented."
            return result
        result["started_at"] = b["started_at"]
        if b["raw"] is None:
            result["reason"] = "Waiting for executable SOL entry quotes; both strategies remain in virtual cash."
            return result
        now = int(time.time())
        if (b["mark_at"] is None or not 0 <= now-b["mark_at"] <= 60 or equity is None
                or any(abs(p["mark_at"]-b["mark_at"]) > 30 for p in positions)):
            result["reason"] = "Fresh, time-aligned exit quotes are unavailable. No comparison result is shown."
            return result
        pre = b["mark_raw"]-b["mark_expense_raw"]-b["entry_raw"]-b["entry_expense_raw"]
        partner = (max(0, pre)*PARTNER_BPS+9999)//10000
        hold = b["cash_raw"]+b["mark_raw"]-b["mark_expense_raw"]-partner
        initial = run["initial_raw"]
        return {**result, "status": "ready", "reason": "", "as_of": b["mark_at"],
                "sol_quantity": b["raw"]/1_000_000_000,
                "hold_equity_usdc": hold/SCALE, "hold_net_usdc": (hold-initial)/SCALE,
                "hold_return_pct": (hold-initial)*100/initial,
                "core_equity_usdc": equity/SCALE, "core_net_usdc": (equity-initial)/SCALE,
                "core_return_pct": (equity-initial)*100/initial,
                "advantage_usdc": (equity-hold)/SCALE,
                "advantage_percentage_points": (equity-hold)*100/initial,
                "hold_expenses_usdc": (b["entry_expense_raw"]+b["mark_expense_raw"])/SCALE,
                "hold_modeled_partner_usdc": partner/SCALE}
