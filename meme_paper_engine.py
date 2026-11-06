"""Isolated paper executor for Solana meme-sniper candidates.

This module consumes already-screened market snapshots and writes only to
PaperTradeStore. It has no RPC, signer, wallet, transaction, or swap imports.
"""
import hashlib
import math
import time

from execution_policy import sniper_exit_reason
from zerolife_ai_trader.paper_store import PaperTradeStore


class MemePaperEngine:
    """Deterministic, user-scoped simulation for screened Solana meme candidates."""

    MIN_LIQUIDITY_USD = 10_000.0
    MIN_VOLUME_24H_USD = 100_000.0
    MIN_TRADES_24H = 200
    MIN_POOL_AGE_HOURS = 24.0
    MAX_ORDER_FRACTION = 0.02
    MAX_OPEN_POSITIONS = 5
    MAX_DAILY_LOSS_FRACTION = 0.03
    STOP_LOSS_FRACTION = 0.05
    TRAIL_ACTIVATION_FRACTION = 0.035
    TRAIL_DRAWDOWN_FRACTION = 0.02

    def __init__(self, db_path, *, fee_bps=10, slippage_bps=40):
        if type(fee_bps) is not int or not 0 <= fee_bps <= 1000:
            raise ValueError("Paper fee must be an integer from 0 to 1000 bps.")
        if type(slippage_bps) is not int or not 0 <= slippage_bps <= 1000:
            raise ValueError("Paper slippage must be an integer from 0 to 1000 bps.")
        self.db_path = db_path
        self.store = PaperTradeStore(db_path)
        self.fee_bps = fee_bps
        self.slippage_bps = slippage_bps
        import sqlite3
        with sqlite3.connect(self.db_path, timeout=10) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS meme_paper_peaks (
                telegram_id INTEGER NOT NULL CHECK(telegram_id > 0),
                symbol TEXT NOT NULL,
                peak_price REAL NOT NULL CHECK(peak_price > 0),
                updated_at INTEGER NOT NULL,
                PRIMARY KEY(telegram_id, symbol)
            )""")

    def _record_peak(self, telegram_id, symbol, price, now):
        import sqlite3
        with sqlite3.connect(self.db_path, timeout=10) as db:
            row = db.execute(
                "SELECT peak_price FROM meme_paper_peaks WHERE telegram_id=? AND symbol=?",
                (telegram_id, symbol),
            ).fetchone()
            peak = max(float(row[0]) if row else price, price)
            db.execute("""INSERT INTO meme_paper_peaks
                (telegram_id, symbol, peak_price, updated_at) VALUES (?, ?, ?, ?)
                ON CONFLICT(telegram_id, symbol) DO UPDATE SET
                peak_price=MAX(meme_paper_peaks.peak_price, excluded.peak_price),
                updated_at=excluded.updated_at""", (telegram_id, symbol, peak, now))
            return peak

    def _clear_peak(self, telegram_id, symbol):
        import sqlite3
        with sqlite3.connect(self.db_path, timeout=10) as db:
            db.execute("DELETE FROM meme_paper_peaks WHERE telegram_id=? AND symbol=?",
                       (telegram_id, symbol))

    @staticmethod
    def _number(value):
        return (type(value) in (int, float) and math.isfinite(value))

    @classmethod
    def candidate_is_eligible(cls, item):
        if not isinstance(item, dict) or item.get("eligible") is not True:
            return False
        required = ("price", "liquidity_usd", "volume_24h_usd",
                    "transactions_24h", "age_hours", "change_m5_pct",
                    "change_h1_pct")
        if any(not cls._number(item.get(key)) for key in required):
            return False
        if not isinstance(item.get("symbol"), str) or not item["symbol"].strip():
            return False
        return (
            item["price"] > 0
            and item["liquidity_usd"] >= cls.MIN_LIQUIDITY_USD
            and item["volume_24h_usd"] >= cls.MIN_VOLUME_24H_USD
            and item["transactions_24h"] >= cls.MIN_TRADES_24H
            and item["age_hours"] >= cls.MIN_POOL_AGE_HOURS
            and 0 < item["change_m5_pct"] <= 3
            and 0 <= item["change_h1_pct"] <= 10
        )

    @staticmethod
    def _signal_id(uid, symbol, side, candle_id):
        raw = f"mem-paper:{uid}:{symbol}:{side}:{candle_id}".encode()
        return int.from_bytes(hashlib.sha256(raw).digest()[:8], "big") & 0x7FFFFFFFFFFFFFFF or 1

    def process_snapshot(self, *, telegram_id, candidates, starting_cash=1000.0,
                         candle_id=None, now=None):
        """Process one fresh snapshot. All fills are virtual and persisted locally."""
        if type(telegram_id) is not int or telegram_id <= 0:
            raise ValueError("Invalid paper account owner.")
        if not self._number(starting_cash) or starting_cash <= 0:
            raise ValueError("Starting paper cash must be positive and finite.")
        if not isinstance(candidates, list):
            raise ValueError("Candidates must be a list.")
        now = int(time.time()) if now is None else int(now)
        candle_id = now if candle_id is None else candle_id
        if type(candle_id) is not int or candle_id <= 0:
            raise ValueError("A positive integer snapshot identifier is required.")

        by_symbol = {}
        for item in candidates:
            if not isinstance(item, dict):
                continue
            symbol = item.get("symbol")
            price = item.get("price")
            if isinstance(symbol, str) and symbol.strip() and self._number(price) and price > 0:
                by_symbol[symbol.strip().upper()] = item

        account = self.store.account(telegram_id=telegram_id)
        cash = account["cash"] if account["cash"] is not None else float(starting_cash)
        positions = {p["symbol"]: p for p in account["positions"]}
        actions = []
        equity = cash + sum(
            p["quantity"] * by_symbol.get(p["symbol"], {}).get("price", p["average_cost"])
            for p in positions.values()
        )

        # Protective exits are evaluated before any new entries.
        for symbol, position in list(positions.items()):
            item = by_symbol.get(symbol)
            if item is None:
                continue
            price = item["price"]
            entry = position["average_cost"]
            peak = self._record_peak(telegram_id, symbol, price, now)
            reason = sniper_exit_reason(price, entry, peak)
            # Use the same 5% hard stop and +3.5% activation / 2% peak trail.
            if price <= entry * (1 - self.STOP_LOSS_FRACTION):
                reason = "STOP_LOSS"
            elif peak >= entry * (1 + self.TRAIL_ACTIVATION_FRACTION) and price <= peak * (1 - self.TRAIL_DRAWDOWN_FRACTION):
                reason = "TRAILING_STOP"
            if reason:
                result = self.store.execute_sell(
                    telegram_id=telegram_id,
                    signal_id=self._signal_id(telegram_id, symbol, "SELL", candle_id),
                    symbol=symbol, quantity=position["quantity"], market_price=price,
                    fee_bps=self.fee_bps, slippage_bps=self.slippage_bps,
                )
                actions.append({"symbol": symbol, "action": "PAPER_SELL",
                                "reason": reason, "execution": result})
                positions.pop(symbol, None)
                self._clear_peak(telegram_id, symbol)

        account = self.store.account(telegram_id=telegram_id)
        cash = account["cash"] if account["cash"] is not None else float(starting_cash)
        positions = {p["symbol"]: p for p in account["positions"]}
        daily_pnl = self.store.daily_realized_pnl(telegram_id=telegram_id)
        loss_limit_hit = daily_pnl <= -float(starting_cash) * self.MAX_DAILY_LOSS_FRACTION

        if not loss_limit_hit:
            for symbol, item in by_symbol.items():
                if symbol in positions or len(positions) >= self.MAX_OPEN_POSITIONS:
                    continue
                if not self.candidate_is_eligible(item):
                    continue
                budget = min(cash, float(starting_cash) * self.MAX_ORDER_FRACTION)
                if budget <= 0:
                    break
                quantity = budget / (item["price"] * (1 + self.slippage_bps / 10000)
                                     * (1 + self.fee_bps / 10000))
                if quantity <= 0 or not math.isfinite(quantity):
                    continue
                result = self.store.execute_buy(
                    telegram_id=telegram_id,
                    signal_id=self._signal_id(telegram_id, symbol, "BUY", candle_id),
                    symbol=symbol, quantity=quantity, market_price=item["price"],
                    fee_bps=self.fee_bps, slippage_bps=self.slippage_bps,
                    starting_cash=float(starting_cash),
                )
                actions.append({"symbol": symbol, "action": "PAPER_BUY",
                                "reason": "Screened candidate passed paper entry gates.",
                                "execution": result})
                if result["executed"]:
                    cash = result["fill"]["cash_after"]
                    positions[symbol] = {"symbol": symbol, "quantity": quantity,
                                          "average_cost": result["fill"]["price"]}

        account = self.store.account(telegram_id=telegram_id)
        summary = self.store.summary(telegram_id=telegram_id)
        marked_value = sum(
            p["quantity"] * by_symbol.get(p["symbol"], {}).get("price", p["average_cost"])
            for p in account["positions"]
        )
        return {
            "execution": "solana_meme_paper_only",
            "live_transactions_sent": 0,
            "wallet_accessed": False,
            "starting_cash": account["starting_cash"] or float(starting_cash),
            "cash": account["cash"] if account["cash"] is not None else float(starting_cash),
            "positions": account["positions"],
            "marked_positions_value": marked_value,
            "equity_estimate": (account["cash"] if account["cash"] is not None else float(starting_cash)) + marked_value,
            "daily_realized_pnl": daily_pnl,
            "daily_loss_limit_hit": loss_limit_hit,
            "summary": summary,
            "actions": actions,
            "snapshot_at": now,
        }
