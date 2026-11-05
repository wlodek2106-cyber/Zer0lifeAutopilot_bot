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
