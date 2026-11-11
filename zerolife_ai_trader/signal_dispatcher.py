"""Fail-closed AI signal validation and idempotency boundary.

This module creates execution plans only. It never signs, submits, or broadcasts
transactions and deliberately has no wallet/RPC/exchange dependencies.
"""
from datetime import datetime, timezone
import math
import re
import sqlite3


SUPPORTED_CHAINS = frozenset({"solana", "base"})
SUPPORTED_SIDES = frozenset({"BUY", "SELL"})
_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


class SignalDispatchError(ValueError):
    """A signal is invalid, stale, unsupported, or unsafe to dispatch."""


def _identifier(value, field):
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise SignalDispatchError(field + " is invalid.")
    return value


def prepare_signal(signal, *, token_allowlist, now=None, max_age_seconds=120):
    """Validate a normalized signal and return an inert execution plan.

    Required signal keys: telegram_id, chain, symbol, side, timeframe,
    candle_id, observed_at, confidence. BUY additionally requires amount_usdc.
    SELL additionally requires position_id and quantity. Token address is
    resolved only from the server-owned allowlist, never from signal input.
    """
    if not isinstance(signal, dict):
        raise SignalDispatchError("Signal must be an object.")
    uid = signal.get("telegram_id")
    if type(uid) is not int or uid <= 0:
        raise SignalDispatchError("Invalid signal owner.")
    chain = signal.get("chain")
    if chain not in SUPPORTED_CHAINS:
        raise SignalDispatchError("Unsupported chain.")
    symbol = signal.get("symbol")
    if not isinstance(symbol, str) or not re.fullmatch(r"[A-Z0-9]{2,20}", symbol):
        raise SignalDispatchError("Invalid symbol.")
    side = signal.get("side")
    if side not in SUPPORTED_SIDES:
        raise SignalDispatchError("Unsupported signal side.")
    timeframe = signal.get("timeframe")
    if timeframe not in {"1m", "5m", "15m", "1h", "4h", "1d"}:
        raise SignalDispatchError("Unsupported timeframe.")
    candle_id = _identifier(str(signal.get("candle_id", "")), "candle_id")

    observed_at = signal.get("observed_at")
    if type(observed_at) not in (int, float) or not math.isfinite(observed_at):
        raise SignalDispatchError("Signal timestamp is invalid.")
    current = datetime.now(timezone.utc).timestamp() if now is None else float(now)
    age = current - float(observed_at)
    if age < -5 or age > max_age_seconds:
        raise SignalDispatchError("Signal is stale or timestamp is in the future.")

    confidence = signal.get("confidence")
    if type(confidence) not in (int, float) or not math.isfinite(confidence) or not 0 <= confidence <= 1:
        raise SignalDispatchError("Signal confidence must be between 0 and 1.")

    if not isinstance(token_allowlist, dict):
        raise SignalDispatchError("Server token allowlist is unavailable.")
    chain_tokens = token_allowlist.get(chain)
    if not isinstance(chain_tokens, dict):
        raise SignalDispatchError("No token mapping exists for this chain.")
    token_address = chain_tokens.get(symbol)
    if not isinstance(token_address, str) or not token_address.strip():
        raise SignalDispatchError("Symbol is not allowlisted on this chain.")

    plan = {
        "telegram_id": uid, "chain": chain, "symbol": symbol,
        "token_address": token_address, "side": side,
        "timeframe": timeframe, "candle_id": candle_id,
        "confidence": float(confidence),
        "signal_key": ":".join((str(uid), chain, symbol, timeframe, candle_id, side)),
        "observed_at": float(observed_at),
        "mode": "paper_candidate",
    }
    if side == "BUY":
        amount = signal.get("amount_usdc")
        if type(amount) not in (int, float) or not math.isfinite(amount) or amount <= 0:
            raise SignalDispatchError("BUY requires a positive USDC amount.")
        plan["amount_usdc"] = float(amount)
    else:
        position_id = _identifier(signal.get("position_id"), "position_id")
        quantity = signal.get("quantity")
        if type(quantity) not in (int, float) or not math.isfinite(quantity) or quantity <= 0:
            raise SignalDispatchError("SELL requires a positive position quantity.")
        plan["position_id"] = position_id
        plan["quantity"] = float(quantity)
    return plan


class SignalDispatchStore:
    """SQLite idempotency ledger. A signal key can be claimed only once."""

    def __init__(self, db_path):
        if not isinstance(db_path, str) or not db_path.strip():
            raise ValueError("A database path is required.")
        self.db_path = db_path
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS ai_signal_dispatches (
                telegram_id INTEGER NOT NULL CHECK(telegram_id > 0),
                signal_key TEXT NOT NULL,
                chain TEXT NOT NULL CHECK(chain IN ('solana', 'base')),
                symbol TEXT NOT NULL,
                side TEXT NOT NULL CHECK(side IN ('BUY', 'SELL')),
                status TEXT NOT NULL CHECK(status IN ('CLAIMED', 'PAPER_DONE', 'BLOCKED')),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (telegram_id, signal_key)
            )""")

    def _connect(self):
        db = sqlite3.connect(self.db_path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def claim(self, plan):
        """Atomically reserve a signal key; returns False for duplicates."""
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            cursor = db.execute("""INSERT OR IGNORE INTO ai_signal_dispatches
                (telegram_id, signal_key, chain, symbol, side, status)
                VALUES (?, ?, ?, ?, ?, 'CLAIMED')""",
                (plan["telegram_id"], plan["signal_key"], plan["chain"],
                 plan["symbol"], plan["side"]))
            return cursor.rowcount == 1

    def set_status(self, *, telegram_id, signal_key, status):
        if status not in {"PAPER_DONE", "BLOCKED"}:
            raise ValueError("Unsupported dispatch status.")
        with self._connect() as db:
            cursor = db.execute("""UPDATE ai_signal_dispatches SET status=?
                WHERE telegram_id=? AND signal_key=? AND status='CLAIMED'""",
                (status, telegram_id, signal_key))
            return cursor.rowcount == 1

    def get(self, *, telegram_id, signal_key):
        with self._connect() as db:
            row = db.execute("""SELECT * FROM ai_signal_dispatches
                WHERE telegram_id=? AND signal_key=?""",
                (telegram_id, signal_key)).fetchone()
        return dict(row) if row else None


def dispatch_signal(signal, *, store, token_allowlist, now=None,
                    live_enabled=False, paper_executor=None):
    """Validate and dedupe; paper executor is opt-in, live execution is absent."""
    plan = prepare_signal(signal, token_allowlist=token_allowlist, now=now)
    if not store.claim(plan):
        return {"accepted": False, "duplicate": True, "plan": plan}
    if live_enabled is True:
        store.set_status(telegram_id=plan["telegram_id"],
                         signal_key=plan["signal_key"], status="BLOCKED")
        return {"accepted": False, "duplicate": False,
                "reason": "Live execution adapter is not connected; fail-closed.",
                "plan": plan}
    if paper_executor is None:
        return {"accepted": True, "duplicate": False,
                "status": "CLAIMED", "plan": plan}
    try:
        result = paper_executor(plan)
    except Exception:
        store.set_status(telegram_id=plan["telegram_id"],
                         signal_key=plan["signal_key"], status="BLOCKED")
        raise
    store.set_status(telegram_id=plan["telegram_id"],
                     signal_key=plan["signal_key"], status="PAPER_DONE")
    return {"accepted": True, "duplicate": False,
            "status": "PAPER_DONE", "result": result, "plan": plan}
