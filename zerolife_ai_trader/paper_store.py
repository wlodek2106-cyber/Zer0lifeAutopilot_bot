"""Durable, user-scoped journal for simulated paper fills only.

This store records results supplied by a paper broker. It does not create orders,
connect to exchanges, or access wallets/signers.
"""
import math
import sqlite3
import uuid


class PaperTradeStore:
    def __init__(self, db_path):
        if not isinstance(db_path, str) or not db_path.strip():
            raise ValueError("A database path is required.")
        self.db_path = db_path
        self._initialize()

    def _connect(self):
        db = sqlite3.connect(self.db_path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        return db

    def _initialize(self):
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS ai_trader_paper_fills (
                    id TEXT PRIMARY KEY,
                    telegram_id INTEGER NOT NULL CHECK(telegram_id > 0),
                    symbol TEXT NOT NULL CHECK(length(symbol) BETWEEN 1 AND 32),
                    side TEXT NOT NULL CHECK(side IN ('BUY', 'SELL')),
                    quantity REAL NOT NULL CHECK(quantity > 0),
                    price REAL NOT NULL CHECK(price > 0),
                    fee REAL NOT NULL CHECK(fee >= 0),
                    realized_pnl REAL NOT NULL,
                    cash_after REAL NOT NULL CHECK(cash_after >= 0),
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_ai_trader_paper_fills_user_time
                    ON ai_trader_paper_fills(telegram_id, id);
                CREATE TABLE IF NOT EXISTS ai_trader_paper_accounts (
                    telegram_id INTEGER PRIMARY KEY CHECK(telegram_id > 0),
                    starting_cash REAL NOT NULL CHECK(starting_cash > 0),
                    cash REAL NOT NULL CHECK(cash >= 0),
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS ai_trader_paper_positions (
                    telegram_id INTEGER NOT NULL,
                    symbol TEXT NOT NULL,
                    quantity REAL NOT NULL CHECK(quantity > 0),
                    average_cost REAL NOT NULL CHECK(average_cost > 0),
                    PRIMARY KEY(telegram_id, symbol)
                );
                CREATE TABLE IF NOT EXISTS ai_trader_paper_executions (
                    telegram_id INTEGER NOT NULL,
                    signal_id INTEGER NOT NULL,
                    fill_id TEXT NOT NULL UNIQUE,
                    PRIMARY KEY(telegram_id, signal_id)
                );
                CREATE TRIGGER IF NOT EXISTS ai_trader_paper_fills_no_update
                    BEFORE UPDATE ON ai_trader_paper_fills
                    BEGIN SELECT RAISE(ABORT, 'paper fill journal is immutable'); END;
                CREATE TRIGGER IF NOT EXISTS ai_trader_paper_fills_no_delete
                    BEFORE DELETE ON ai_trader_paper_fills
                    BEGIN SELECT RAISE(ABORT, 'paper fill journal is immutable'); END;
            """)

    @staticmethod
    def _finite(value, field, *, positive=False, nonnegative=False):
        if type(value) not in (int, float) or not math.isfinite(value):
            raise ValueError(field + " must be a finite number.")
        if positive and value <= 0:
            raise ValueError(field + " must be positive.")
        if nonnegative and value < 0:
            raise ValueError(field + " cannot be negative.")

    def record_fill(self, *, telegram_id, symbol, side, quantity, price,
                    fee, realized_pnl=0.0, cash_after, fill_id=None):
        if type(telegram_id) is not int or telegram_id <= 0:
            raise ValueError("Invalid paper account owner.")
        if not isinstance(symbol, str) or not symbol.strip() or len(symbol) > 32:
            raise ValueError("Invalid paper trading symbol.")
        symbol = symbol.strip().upper()
        if side not in ("BUY", "SELL"):
            raise ValueError("Paper fill side must be BUY or SELL.")
        self._finite(quantity, "quantity", positive=True)
        self._finite(price, "price", positive=True)
        self._finite(fee, "fee", nonnegative=True)
        self._finite(realized_pnl, "realized_pnl")
        self._finite(cash_after, "cash_after", nonnegative=True)
        if fill_id is None:
            fill_id = str(uuid.uuid4())
        if not isinstance(fill_id, str) or not fill_id.strip() or len(fill_id) > 64:
            raise ValueError("Invalid paper fill identifier.")
        with self._connect() as db:
            db.execute("""INSERT INTO ai_trader_paper_fills
                (id, telegram_id, symbol, side, quantity, price, fee,
                 realized_pnl, cash_after)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (fill_id, telegram_id, symbol, side, float(quantity),
                 float(price), float(fee), float(realized_pnl), float(cash_after)))
        return fill_id

    def execute_buy(self, *, telegram_id, signal_id, symbol, quantity,
                    market_price, fee_bps=10, slippage_bps=5, starting_cash=1000):
        """Atomically simulate a BUY, persist account/position/fill, and dedupe signal."""
        if type(telegram_id) is not int or telegram_id <= 0:
            raise ValueError("Invalid paper account owner.")
        if type(signal_id) is not int or signal_id <= 0:
            raise ValueError("Invalid signal identifier.")
        if not isinstance(symbol, str) or not symbol.strip() or len(symbol) > 32:
            raise ValueError("Invalid paper trading symbol.")
        symbol = symbol.strip().upper()
        self._finite(quantity, "quantity", positive=True)
        self._finite(market_price, "market_price", positive=True)
        self._finite(starting_cash, "starting_cash", positive=True)
        self._finite(fee_bps, "fee_bps", nonnegative=True)
        self._finite(slippage_bps, "slippage_bps", nonnegative=True)
        if fee_bps > 1000 or slippage_bps > 1000:
            raise ValueError("Paper fee or slippage is outside safe bounds.")
        fill_price = market_price * (1.0 + slippage_bps / 10000.0)
        notional = quantity * fill_price
        fee = notional * fee_bps / 10000.0
        total = notional + fee
        if not all(math.isfinite(x) for x in (fill_price, notional, fee, total)):
            raise ValueError("Paper execution arithmetic overflow.")
        fill_id = "signal-" + str(telegram_id) + "-" + str(signal_id)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""CREATE TABLE IF NOT EXISTS ai_trader_paper_accounts (
                telegram_id INTEGER PRIMARY KEY CHECK(telegram_id > 0),
                starting_cash REAL NOT NULL CHECK(starting_cash > 0),
                cash REAL NOT NULL CHECK(cash >= 0),
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS ai_trader_paper_positions (
                telegram_id INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                quantity REAL NOT NULL CHECK(quantity > 0),
                average_cost REAL NOT NULL CHECK(average_cost > 0),
                PRIMARY KEY(telegram_id, symbol)
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS ai_trader_paper_executions (
                telegram_id INTEGER NOT NULL,
                signal_id INTEGER NOT NULL,
                fill_id TEXT NOT NULL UNIQUE,
                PRIMARY KEY(telegram_id, signal_id)
            )""")
            prior = db.execute("""SELECT fill_id FROM ai_trader_paper_executions
                WHERE telegram_id=? AND signal_id=?""", (telegram_id, signal_id)).fetchone()
            if prior:
                row = db.execute("SELECT * FROM ai_trader_paper_fills WHERE id=?",
                                 (prior["fill_id"],)).fetchone()
                return {"executed": False, "duplicate": True,
                        "fill": dict(row) if row else None}
            db.execute("""INSERT OR IGNORE INTO ai_trader_paper_accounts
                (telegram_id, starting_cash, cash) VALUES (?, ?, ?)""",
                (telegram_id, float(starting_cash), float(starting_cash)))
            account = db.execute("SELECT * FROM ai_trader_paper_accounts WHERE telegram_id=?",
                                 (telegram_id,)).fetchone()
            cash = float(account["cash"])
            if total > cash:
                raise ValueError("Insufficient paper cash after fees and slippage.")
            prior_position = db.execute("""SELECT quantity, average_cost
                FROM ai_trader_paper_positions WHERE telegram_id=? AND symbol=?""",
                (telegram_id, symbol)).fetchone()
            old_qty = float(prior_position["quantity"]) if prior_position else 0.0
            old_cost = float(prior_position["average_cost"]) if prior_position else 0.0
            new_qty = old_qty + quantity
            new_cash = cash - total
            new_average = ((old_qty * old_cost + total) / new_qty)
            if not all(math.isfinite(x) for x in (new_qty, new_cash, new_average)) or new_qty <= old_qty:
                raise ValueError("Paper account arithmetic overflow.")
            db.execute("""INSERT INTO ai_trader_paper_fills
                (id, telegram_id, symbol, side, quantity, price, fee,
                 realized_pnl, cash_after) VALUES (?, ?, ?, 'BUY', ?, ?, ?, 0, ?)""",
                (fill_id, telegram_id, symbol, float(quantity), fill_price, fee, new_cash))
            db.execute("""UPDATE ai_trader_paper_accounts SET cash=?,
                updated_at=CURRENT_TIMESTAMP WHERE telegram_id=?""",
                (new_cash, telegram_id))
            db.execute("""INSERT INTO ai_trader_paper_positions
                (telegram_id, symbol, quantity, average_cost) VALUES (?, ?, ?, ?)
                ON CONFLICT(telegram_id, symbol) DO UPDATE SET
                quantity=excluded.quantity, average_cost=excluded.average_cost""",
                (telegram_id, symbol, new_qty, new_average))
            db.execute("""INSERT INTO ai_trader_paper_executions
                (telegram_id, signal_id, fill_id) VALUES (?, ?, ?)""",
                (telegram_id, signal_id, fill_id))
            return {"executed": True, "duplicate": False, "fill": {
                "id": fill_id, "symbol": symbol, "side": "BUY",
                "quantity": float(quantity), "price": fill_price, "fee": fee,
                "realized_pnl": 0.0, "cash_after": new_cash}}

    def execute_sell(self, *, telegram_id, signal_id, symbol, quantity,
                     market_price, fee_bps=10, slippage_bps=5):
        """Atomically simulate a position-reducing SELL; never contacts an exchange."""
        if type(telegram_id) is not int or telegram_id <= 0:
            raise ValueError("Invalid paper account owner.")
        if type(signal_id) is not int or signal_id <= 0:
            raise ValueError("Invalid signal identifier.")
        if not isinstance(symbol, str) or not symbol.strip() or len(symbol) > 32:
            raise ValueError("Invalid paper trading symbol.")
        symbol = symbol.strip().upper()
        self._finite(quantity, "quantity", positive=True)
        self._finite(market_price, "market_price", positive=True)
        self._finite(fee_bps, "fee_bps", nonnegative=True)
        self._finite(slippage_bps, "slippage_bps", nonnegative=True)
        if fee_bps > 1000 or slippage_bps > 1000:
            raise ValueError("Paper fee or slippage is outside safe bounds.")

        # A SELL incurs adverse slippage, then fees are deducted from proceeds.
        fill_price = market_price * (1.0 - slippage_bps / 10000.0)
        gross = quantity * fill_price
        fee = gross * fee_bps / 10000.0
        proceeds = gross - fee
        if not all(math.isfinite(x) for x in (fill_price, gross, fee, proceeds)) or fill_price <= 0 or proceeds < 0:
            raise ValueError("Paper execution arithmetic overflow.")
        fill_id = "paper-sell-" + str(telegram_id) + "-" + str(signal_id)

        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute("""SELECT fill_id FROM ai_trader_paper_executions
                WHERE telegram_id=? AND signal_id=?""", (telegram_id, signal_id)).fetchone()
            if prior:
                row = db.execute("SELECT * FROM ai_trader_paper_fills WHERE id=?",
                                 (prior["fill_id"],)).fetchone()
                return {"executed": False, "duplicate": True,
                        "fill": dict(row) if row else None}

            position = db.execute("""SELECT quantity, average_cost
                FROM ai_trader_paper_positions WHERE telegram_id=? AND symbol=?""",
                (telegram_id, symbol)).fetchone()
            if position is None:
                raise ValueError("No paper position is available to sell.")
            account = db.execute("""SELECT cash FROM ai_trader_paper_accounts
                WHERE telegram_id=?""", (telegram_id,)).fetchone()
            if account is None:
                raise ValueError("No paper account is available to sell.")
            held = float(position["quantity"])
            average_cost = float(position["average_cost"])
            if quantity > held:
                raise ValueError("Cannot sell more than the paper position.")

            cash_after = float(account["cash"]) + proceeds
            remaining = held - quantity
            realized_pnl = quantity * (fill_price - average_cost) - fee
            if not all(math.isfinite(x) for x in (cash_after, remaining, realized_pnl)) or cash_after < 0:
                raise ValueError("Paper account arithmetic overflow.")

            db.execute("""INSERT INTO ai_trader_paper_fills
                (id, telegram_id, symbol, side, quantity, price, fee,
                 realized_pnl, cash_after) VALUES (?, ?, ?, 'SELL', ?, ?, ?, ?, ?)""",
                (fill_id, telegram_id, symbol, float(quantity), fill_price,
                 fee, realized_pnl, cash_after))
            db.execute("""UPDATE ai_trader_paper_accounts SET cash=?,
                updated_at=CURRENT_TIMESTAMP WHERE telegram_id=?""",
                (cash_after, telegram_id))
            if remaining <= max(1e-12, held * 1e-12):
                db.execute("""DELETE FROM ai_trader_paper_positions
                    WHERE telegram_id=? AND symbol=?""", (telegram_id, symbol))
                remaining = 0.0
            else:
                db.execute("""UPDATE ai_trader_paper_positions SET quantity=?
                    WHERE telegram_id=? AND symbol=?""",
                    (remaining, telegram_id, symbol))
            db.execute("""INSERT INTO ai_trader_paper_executions
                (telegram_id, signal_id, fill_id) VALUES (?, ?, ?)""",
                (telegram_id, signal_id, fill_id))
            return {"executed": True, "duplicate": False, "fill": {
                "id": fill_id, "symbol": symbol, "side": "SELL",
                "quantity": float(quantity), "price": fill_price, "fee": fee,
                "realized_pnl": realized_pnl, "cash_after": cash_after,
                "remaining_quantity": remaining}}

    def account(self, *, telegram_id):
        if type(telegram_id) is not int or telegram_id <= 0:
            raise ValueError("Invalid paper account owner.")
        with self._connect() as db:
            row = db.execute("""SELECT starting_cash, cash FROM ai_trader_paper_accounts
                WHERE telegram_id=?""", (telegram_id,)).fetchone()
            positions = db.execute("""SELECT symbol, quantity, average_cost
                FROM ai_trader_paper_positions WHERE telegram_id=?
                ORDER BY symbol""", (telegram_id,)).fetchall()
        return {"starting_cash": row["starting_cash"] if row else None,
                "cash": row["cash"] if row else None,
                "positions": [dict(x) for x in positions]}

    def history(self, *, telegram_id, limit=50):
        if type(telegram_id) is not int or telegram_id <= 0:
            raise ValueError("Invalid paper account owner.")
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("History limit must be between 1 and 200.")
        with self._connect() as db:
            rows = db.execute("""SELECT id, symbol, side, quantity, price, fee,
                    realized_pnl, cash_after, created_at
                FROM ai_trader_paper_fills WHERE telegram_id=?
                ORDER BY rowid DESC LIMIT ?""", (telegram_id, limit)).fetchall()
        return [dict(row) for row in rows]

    def daily_realized_pnl(self, *, telegram_id):
        """Return today's realized paper P&L using the journal's UTC timestamps."""
        if type(telegram_id) is not int or telegram_id <= 0:
            raise ValueError("Invalid paper account owner.")
        with self._connect() as db:
            row = db.execute("""SELECT COALESCE(SUM(realized_pnl), 0) AS realized_pnl
                FROM ai_trader_paper_fills
                WHERE telegram_id=? AND date(created_at)=date('now')""",
                (telegram_id,)).fetchone()
        return float(row["realized_pnl"])

    def summary(self, *, telegram_id):
        if type(telegram_id) is not int or telegram_id <= 0:
            raise ValueError("Invalid paper account owner.")
        with self._connect() as db:
            row = db.execute("""SELECT COUNT(*) AS fills,
                    COALESCE(SUM(fee), 0) AS fees_paid,
                    COALESCE(SUM(realized_pnl), 0) AS realized_pnl
                FROM ai_trader_paper_fills WHERE telegram_id=?""",
                (telegram_id,)).fetchone()
        return dict(row)
