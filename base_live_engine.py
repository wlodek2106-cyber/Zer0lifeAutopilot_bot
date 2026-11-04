"""Persistent, owner-scoped Base live-trading state.

The engine starts with live trading off. It never shares the Solana custody
ledger, and mode activation requires the global kill switch plus a tested
executor, a dedicated Base signer, and funded USDC/ETH reserves.
"""
import asyncio
import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation

import aiohttp
from eth_abi import decode

import chart_analysis
import execution_policy
from base_live_trading import (
    BASE_CHAIN_ID,
    BASE_USDC_ADDRESS,
    BASE_UNISWAP_V3_SWAP_ROUTER_02,
    BaseLiveTradingError,
    BaseLiveReadiness,
    BaseV3Market,
    encode_deadline_multicall,
    encode_exact_approval,
    encode_exact_input_single,
    encode_revoke_approval,
    live_environment_status,
    minimum_output,
    normalize_address,
)
from base_paper import BasePaperError, BasePaperEngine
from cross_chain_bridge import BridgeError, base_signer_address, sign_base_transaction


BASE_USDC_DECIMALS = 6
MIN_BASE_BUDGET_RAW = 15 * 10**BASE_USDC_DECIMALS
MAX_BASE_BUDGET_RAW = 10_000 * 10**BASE_USDC_DECIMALS
MIN_BASE_GAS_RESERVE_WEI = 5 * 10**15
MAX_BASE_TX_FEE_WEI = 10**15
BASE_ENTRY_SLOTS = 5
BASE_ENTRY_TRANCHES = 3
BASE_PENDING_RECHECK_SECONDS = 5
BASE_WORKER_IDLE_SECONDS = 20
BASE_STOP_LOSS_PCT = 5.0
BASE_REENTRY_COOLDOWN_SECONDS = 24 * 60 * 60
BASE_SLIPPAGE_BPS = 100
BASE_REENTRY_ADD_PULLBACK_PCT = 2.0
BASE_MIN_ENTRY_INTERVAL_SECONDS = 5 * 60
BASE_STOP_LOSS_RATIO = 1 - BASE_STOP_LOSS_PCT / 100


class BaseLiveEngineError(ValueError):
    def __init__(self, message, status=409):
        self.status = status
        super().__init__(message)


def usdc_to_raw(value):
    if isinstance(value, bool):
        raise BaseLiveEngineError("Enter a valid Base live budget.", 400)
    try:
        amount = Decimal(str(value))
        raw = amount * 10**BASE_USDC_DECIMALS
        if (not amount.is_finite() or amount < 0
                or raw != raw.to_integral_value()
                or raw > MAX_BASE_BUDGET_RAW):
            raise ValueError()
        return int(raw)
    except (InvalidOperation, ValueError, TypeError, OverflowError):
        raise BaseLiveEngineError("Base live budget must be between $0 and $10,000.", 400) from None


def base_live_exit_reason(price, entry, peak, entry_step):
    if not all(isinstance(value, (int, float)) and math_is_finite_positive(value)
               for value in (price, entry, peak)) or type(entry_step) is not int:
        return None
    if price / entry <= BASE_STOP_LOSS_RATIO:
        return "STOP_LOSS_5_PERCENT"
    if entry_step == 1 and price / entry >= 1.035:
        return "TAKE_PROFIT_STEP_1"
    if entry_step == 2 and price / entry >= 1.07:
        return "TAKE_PROFIT_STEP_2"
    if (entry_step == 3 and peak / entry >= 1.10
            and price / peak <= 0.95 and price > entry):
        return "TRAILING_TAKE_PROFIT_STEP_3"
    return None


def math_is_finite_positive(value):
    try:
        return value > 0 and value < float("inf")
    except (TypeError, ValueError, OverflowError):
        return False


def _json_object(value):
    try:
        result = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    return result if isinstance(result, dict) else {}


class BaseLiveEngine:
    """Separate Base trading ledger; only the configured owner may use it."""

    def __init__(self, db_path, owner_id, readiness=None, paper_engine=None):
        self.db_path = str(db_path) + ".base-live"
        self.owner_id = int(owner_id)
        self.readiness = readiness or BaseLiveReadiness()
        self._cycle_lock = asyncio.Lock()
        self.paper_engine = paper_engine or BasePaperEngine(db_path, owner_id)
        self._last_entry_scan_at = 0
        self._initialize()
        self.pause_on_restart()

    @contextmanager
    def db(self):
        connection = sqlite3.connect(self.db_path, timeout=20)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self):
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS base_live_settings (
                    uid INTEGER PRIMARY KEY,
                    budget_usdc_raw INTEGER NOT NULL DEFAULT 0 CHECK(budget_usdc_raw>=0),
                    enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0,1)),
                    updated_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS base_live_positions (
                    id TEXT PRIMARY KEY,
                    uid INTEGER NOT NULL,
                    cycle_id TEXT NOT NULL,
                    token_address TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    pool_address TEXT NOT NULL,
                    fee_tier INTEGER NOT NULL,
                    token_decimals INTEGER NOT NULL CHECK(token_decimals BETWEEN 0 AND 18),
                    entry_step INTEGER NOT NULL CHECK(entry_step BETWEEN 1 AND 3),
                    entry_price_usdc REAL NOT NULL CHECK(entry_price_usdc>0),
                    peak_price_usdc REAL NOT NULL CHECK(peak_price_usdc>0),
                    token_raw TEXT NOT NULL CHECK(length(token_raw)>0),
                    cost_usdc_raw INTEGER NOT NULL CHECK(cost_usdc_raw>0),
                    state TEXT NOT NULL CHECK(state IN ('OPEN','CLOSED','REVIEW')),
                    buy_tx_hash TEXT NOT NULL,
                    sell_tx_hash TEXT NOT NULL DEFAULT '',
                    exit_reason TEXT NOT NULL DEFAULT '',
                    realized_usdc_raw INTEGER NOT NULL DEFAULT 0,
                    opened_at INTEGER NOT NULL,
                    closed_at INTEGER
                );
                CREATE INDEX IF NOT EXISTS base_live_positions_by_user
                    ON base_live_positions(uid,state,opened_at DESC);
                CREATE UNIQUE INDEX IF NOT EXISTS base_live_open_tranche_unique
                    ON base_live_positions(uid,cycle_id,entry_step) WHERE state='OPEN';
                CREATE TABLE IF NOT EXISTS base_live_cycles (
                    id TEXT PRIMARY KEY,
                    uid INTEGER NOT NULL,
                    token_address TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    pool_address TEXT NOT NULL,
                    fee_tier INTEGER NOT NULL,
                    current_step INTEGER NOT NULL DEFAULT 0 CHECK(current_step BETWEEN 0 AND 3),
                    last_entry_price_usdc REAL NOT NULL DEFAULT 0,
                    last_entry_candle_at INTEGER NOT NULL DEFAULT 0,
                    state TEXT NOT NULL CHECK(state IN ('OPEN','CLOSED','REVIEW')),
                    opened_at INTEGER NOT NULL,
                    closed_at INTEGER
                );
                CREATE UNIQUE INDEX IF NOT EXISTS base_live_one_open_cycle
                    ON base_live_cycles(uid,token_address) WHERE state='OPEN';
                CREATE TABLE IF NOT EXISTS base_live_operations (
                    id TEXT PRIMARY KEY,
                    uid INTEGER NOT NULL,
                    kind TEXT NOT NULL CHECK(kind IN ('BUY','SELL','APPROVE','REVOKE')),
                    state TEXT NOT NULL CHECK(state IN (
                        'PREPARED','SIGNING','SUBMITTED','UNKNOWN','CONFIRMED','FAILED','NEEDS_REVIEW'
                    )),
                    tx_hash TEXT NOT NULL DEFAULT '',
                    nonce INTEGER,
                    token_address TEXT NOT NULL DEFAULT '',
                    position_id TEXT NOT NULL DEFAULT '',
                    metadata TEXT NOT NULL DEFAULT '{}',
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS base_live_pending_operations
                    ON base_live_operations(uid,state,created_at);
                CREATE UNIQUE INDEX IF NOT EXISTS base_live_single_pending_operation
                    ON base_live_operations(uid)
                    WHERE state IN ('PREPARED','SIGNING','SUBMITTED','UNKNOWN','NEEDS_REVIEW');
                CREATE TABLE IF NOT EXISTS base_live_cooldowns (
                    uid INTEGER NOT NULL,
                    token_address TEXT NOT NULL,
                    blocked_until INTEGER NOT NULL,
                    PRIMARY KEY(uid,token_address)
                );
                CREATE TABLE IF NOT EXISTS base_live_status (
                    uid INTEGER PRIMARY KEY,
                    state TEXT NOT NULL,
                    message TEXT NOT NULL,
                    checked_at INTEGER NOT NULL
                );
                CREATE TRIGGER IF NOT EXISTS base_live_max_five_slots
                BEFORE INSERT ON base_live_cycles
                WHEN NEW.state IN ('OPEN','REVIEW') AND
                    (SELECT COUNT(*) FROM base_live_cycles
                     WHERE uid=NEW.uid AND state IN ('OPEN','REVIEW')) >= 5
                BEGIN
                    SELECT RAISE(ABORT,'Base live slot limit reached');
                END;
            """)
            position_columns = {
                row["name"]: str(row["type"]).upper()
                for row in db.execute("PRAGMA table_info(base_live_positions)")
            }
            if position_columns.get("token_raw") != "TEXT":
                db.executescript("""
                    ALTER TABLE base_live_positions RENAME TO base_live_positions_v1;
                    CREATE TABLE base_live_positions (
                        id TEXT PRIMARY KEY,
                        uid INTEGER NOT NULL,
                        cycle_id TEXT NOT NULL,
                        token_address TEXT NOT NULL,
                        symbol TEXT NOT NULL,
                        pool_address TEXT NOT NULL,
                        fee_tier INTEGER NOT NULL,
                        token_decimals INTEGER NOT NULL CHECK(token_decimals BETWEEN 0 AND 18),
                        entry_step INTEGER NOT NULL CHECK(entry_step BETWEEN 1 AND 3),
                        entry_price_usdc REAL NOT NULL CHECK(entry_price_usdc>0),
                        peak_price_usdc REAL NOT NULL CHECK(peak_price_usdc>0),
                        token_raw TEXT NOT NULL CHECK(length(token_raw)>0),
                        cost_usdc_raw INTEGER NOT NULL CHECK(cost_usdc_raw>0),
                        state TEXT NOT NULL CHECK(state IN ('OPEN','CLOSED','REVIEW')),
                        buy_tx_hash TEXT NOT NULL,
                        sell_tx_hash TEXT NOT NULL DEFAULT '',
                        exit_reason TEXT NOT NULL DEFAULT '',
                        realized_usdc_raw INTEGER NOT NULL DEFAULT 0,
                        opened_at INTEGER NOT NULL,
                        closed_at INTEGER
                    );
                    INSERT INTO base_live_positions
                    SELECT id,uid,cycle_id,token_address,symbol,pool_address,fee_tier,
                           token_decimals,entry_step,entry_price_usdc,peak_price_usdc,
                           CAST(token_raw AS TEXT),cost_usdc_raw,state,buy_tx_hash,
                           sell_tx_hash,exit_reason,realized_usdc_raw,opened_at,closed_at
                    FROM base_live_positions_v1;
                    DROP TABLE base_live_positions_v1;
                    CREATE INDEX base_live_positions_by_user
                        ON base_live_positions(uid,state,opened_at DESC);
                    CREATE UNIQUE INDEX base_live_open_tranche_unique
                        ON base_live_positions(uid,cycle_id,entry_step) WHERE state='OPEN';
                """)

    def _require_owner(self, uid):
        if int(uid) != self.owner_id:
            raise BaseLiveEngineError("Base live trading is currently owner-only.", 403)

    def _ensure_settings(self, db, uid, now=None):
        now = int(time.time()) if now is None else int(now)
        db.execute("""INSERT OR IGNORE INTO base_live_settings(uid,budget_usdc_raw,enabled,updated_at)
            VALUES(?,0,0,?)""", (int(uid), now))
        return db.execute("SELECT * FROM base_live_settings WHERE uid=?",
                          (int(uid),)).fetchone()

    def settings(self, uid):
        self._require_owner(uid)
        with self.db() as db:
            return dict(self._ensure_settings(db, uid))

    def pause_on_restart(self):
        with self.db() as db:
            db.execute("UPDATE base_live_settings SET enabled=0,updated_at=? WHERE enabled=1",
                       (int(time.time()),))

    def save_budget(self, uid, amount):
        self._require_owner(uid)
        raw = usdc_to_raw(amount)
        if 0 < raw < MIN_BASE_BUDGET_RAW:
            raise BaseLiveEngineError("Use $0 to pause or at least $15 for 15 equal tranches.", 400)
        with self.db() as db:
            pending = db.execute("""SELECT COUNT(*) AS n FROM base_live_operations
                WHERE uid=? AND state IN ('PREPARED','SIGNING','SUBMITTED','UNKNOWN','NEEDS_REVIEW')""",
                (int(uid),)).fetchone()["n"]
            if pending:
                raise BaseLiveEngineError("Resolve the pending Base operation before changing its cap.")
            open_cost = db.execute("""SELECT COALESCE(SUM(cost_usdc_raw),0) AS amount
                FROM base_live_positions WHERE uid=? AND state IN ('OPEN','REVIEW')""",
                (int(uid),)).fetchone()["amount"]
            if raw and raw < int(open_cost or 0):
                raise BaseLiveEngineError("A non-zero cap cannot be lower than the open Base position cost.")
            self._ensure_settings(db, uid)
            db.execute("""UPDATE base_live_settings SET budget_usdc_raw=?,updated_at=?
                WHERE uid=?""", (raw, int(time.time()), int(uid)))
        return raw

    def pending_operation(self, uid):
        with self.db() as db:
            row = db.execute("""SELECT * FROM base_live_operations
                WHERE uid=? AND state IN ('PREPARED','SIGNING','SUBMITTED','UNKNOWN','NEEDS_REVIEW')
                ORDER BY created_at LIMIT 1""", (int(uid),)).fetchone()
            return dict(row) if row else None

    def _worker_poll_seconds(self, uid):
        operation = self.pending_operation(uid)
        if operation and operation["state"] in {"PREPARED", "SIGNING", "SUBMITTED", "UNKNOWN"}:
            return BASE_PENDING_RECHECK_SECONDS
        return BASE_WORKER_IDLE_SECONDS

    def set_status(self, uid, state, message, now=None):
        self._require_owner(uid)
        now = int(time.time()) if now is None else int(now)
        with self.db() as db:
            db.execute("""INSERT INTO base_live_status(uid,state,message,checked_at)
                VALUES(?,?,?,?) ON CONFLICT(uid) DO UPDATE SET state=excluded.state,
                message=excluded.message,checked_at=excluded.checked_at""",
                (int(uid), str(state)[:32], str(message)[:300], now))

    def status(self, uid):
        self._require_owner(uid)
        with self.db() as db:
            row = db.execute("SELECT * FROM base_live_status WHERE uid=?",
                             (int(uid),)).fetchone()
            return dict(row) if row else {
                "uid": int(uid), "state": "OFF",
                "message": "Base live trading is off.", "checked_at": None,
            }

    def open_positions(self, uid):
        self._require_owner(uid)
        with self.db() as db:
            return [dict(row) for row in db.execute("""SELECT * FROM base_live_positions
                WHERE uid=? AND state IN ('OPEN','REVIEW') ORDER BY opened_at DESC""",
                (int(uid),))]

    def recent_operations(self, uid, limit=20):
        self._require_owner(uid)
        with self.db() as db:
            return [dict(row) for row in db.execute("""SELECT id,kind,state,tx_hash,token_address,
                created_at,updated_at FROM base_live_operations WHERE uid=?
                ORDER BY created_at DESC LIMIT ?""", (int(uid), int(limit)))]

    def report(self, uid, readiness_report=None):
        self._require_owner(uid)
        readiness_report = readiness_report or {}
        settings = self.settings(uid)
        positions = self.open_positions(uid)
        operations = self.recent_operations(uid)
        pnl_raw = 0
        with self.db() as db:
            pnl_raw = int(db.execute("""SELECT COALESCE(SUM(realized_usdc_raw),0)
                FROM base_live_positions WHERE uid=? AND state='CLOSED'""",
                (int(uid),)).fetchone()[0] or 0)
        budget_usdc = settings["budget_usdc_raw"] / 10**BASE_USDC_DECIMALS
        open_cost_usdc = self._open_cost_raw(uid) / 10**BASE_USDC_DECIMALS
        funded_for_cap = (
            isinstance(readiness_report.get("usdc_balance"), (int, float))
            and float(readiness_report["usdc_balance"]) + open_cost_usdc >= budget_usdc
            and budget_usdc > 0
        )
        ready = bool(readiness_report.get("ready") and funded_for_cap)
        message = readiness_report.get("message", "Base live trading is not ready.")
        if readiness_report.get("ready") and not funded_for_cap:
            message = "Fund the Base trading wallet or lower its cap to cover the budget."
        checks = list(readiness_report.get("checks", []))
        checks = [item for item in checks if item.get("name") != "USDC balance"]
        checks.append({
            "name": "USDC plus open positions cover cap",
            "ok": funded_for_cap,
        })
        return {
            "success": True,
            "enabled": bool(settings["enabled"]),
            "budget_usdc": budget_usdc,
            "wallet_address": readiness_report.get("wallet_address", ""),
            "usdc_balance": readiness_report.get("usdc_balance"),
            "eth_balance": readiness_report.get("eth_balance"),
            "chain_id": readiness_report.get("chain_id"),
            "can_trade": ready,
            "executor_ready": bool(readiness_report.get("executor_ready")),
            "status": self.status(uid),
            "open_positions": positions,
            "recent_operations": operations,
            "realized_usdc": pnl_raw / 10**BASE_USDC_DECIMALS,
            "checks": checks,
            "message": message,
        }

    async def toggle(self, uid, enabled, session, *, confirm=False):
        self._require_owner(uid)
        if type(enabled) is not bool:
            raise BaseLiveEngineError("Specify whether Base live trading should be enabled.", 400)
        if not enabled:
            now = int(time.time())
            with self.db() as db:
                self._ensure_settings(db, uid)
                db.execute("UPDATE base_live_settings SET enabled=0,updated_at=? WHERE uid=?",
                           (now, int(uid)))
                db.execute("""INSERT INTO base_live_status(uid,state,message,checked_at)
                    VALUES(?,'OFF','Base live trading was turned off.',?)
                    ON CONFLICT(uid) DO UPDATE SET state='OFF',
                    message='Base live trading was turned off.',checked_at=excluded.checked_at""",
                    (int(uid), now))
            return await self.report(uid, await self.readiness.report(session))
        if confirm is not True:
            raise BaseLiveEngineError("Confirm that Base orders use real money.", 400)
        env_ready = os.getenv("BASE_LIVE_TRADING_ENABLED", "").lower() == "true"
        readiness = await self.readiness.report(session)
        if not env_ready or not readiness.get("ready"):
            raise BaseLiveEngineError(
                "Base live trading is locked: wallet, funding, executor, or server activation is not ready."
            )
        report = self.report(uid, readiness)
        with self.db() as db:
            settings = self._ensure_settings(db, uid)
            if settings["budget_usdc_raw"] <= 0:
                raise BaseLiveEngineError("Set a non-zero Base live budget before enabling.")
            open_cost_raw = int(db.execute("""SELECT COALESCE(SUM(cost_usdc_raw),0) AS amount
                FROM base_live_positions WHERE uid=? AND state IN ('OPEN','REVIEW')""",
                (int(uid),)).fetchone()["amount"] or 0)
            if (not isinstance(readiness.get("usdc_balance"), (int, float))
                    or float(readiness["usdc_balance"])
                    + open_cost_raw / 10**BASE_USDC_DECIMALS
                    < int(settings["budget_usdc_raw"]) / 10**BASE_USDC_DECIMALS):
                raise BaseLiveEngineError("Fund the Base trading wallet or lower its cap to cover the budget.")
            if (not isinstance(readiness.get("eth_balance"), (int, float))
                    or float(readiness["eth_balance"]) < MIN_BASE_GAS_RESERVE_WEI / 10**18):
                raise BaseLiveEngineError("The Base trading wallet needs at least 0.005 ETH for gas reserve.")
            pending = db.execute("""SELECT COUNT(*) AS n FROM base_live_operations
                WHERE uid=? AND state IN ('PREPARED','SIGNING','SUBMITTED','UNKNOWN','NEEDS_REVIEW')""",
                (int(uid),)).fetchone()["n"]
            if pending:
                raise BaseLiveEngineError("Resolve the pending Base operation before enabling.")
            now = int(time.time())
            db.execute("UPDATE base_live_settings SET enabled=1,updated_at=? WHERE uid=?",
                       (now, int(uid)))
            db.execute("""INSERT INTO base_live_status(uid,state,message,checked_at)
                VALUES(?,'ON','Base live trading is enabled.',?)
                ON CONFLICT(uid) DO UPDATE SET state='ON',
                message='Base live trading is enabled.',checked_at=excluded.checked_at""",
                (int(uid), now))
        report["enabled"] = True
        report["status"] = {
            "uid": int(uid),
            "state": "ON",
            "message": "Base live trading is enabled.",
            "checked_at": now,
        }
        return report

    def _env_wallet(self):
        wallet = normalize_address(os.getenv("BASE_TRADING_WALLET_ADDRESS", ""))
        signer = base_signer_address(os.getenv("BASE_TRADING_PRIVATE_KEY", ""))
        if signer != wallet:
            raise BaseLiveEngineError("The Base trading signer does not match its wallet.", 503)
        bridge = os.getenv("BASE_HOT_WALLET_ADDRESS", "").strip().lower()
        shared_allowed = os.getenv(
            "BASE_SHARED_WALLET_TRADING_ENABLED", ""
        ).lower() == "true"
        if bridge and bridge == wallet and not shared_allowed:
            raise BaseLiveEngineError(
                "Shared Base trading requires explicit server authorization.", 503)
        return wallet

    async def _rpc(self, session, method, params):
        url = (os.getenv("BASE_TRADING_RPC_URL")
               or os.getenv("BASE_RPC_URL") or "").strip()
        if not url.startswith("https://"):
            raise BaseLiveEngineError("A Base HTTPS RPC is not configured.", 503)
        try:
            async with session.post(
                url,
                json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as response:
                payload = await response.json(content_type=None)
        except Exception:
            raise BaseLiveEngineError("Base RPC is temporarily unavailable.", 503) from None
        if (response.status != 200 or not isinstance(payload, dict)
                or "error" in payload or "result" not in payload):
            raise BaseLiveEngineError("Base RPC returned an invalid response.", 503)
        return payload["result"]

    async def _rpc_int(self, session, method, params):
        value = await self._rpc(session, method, params)
        if not isinstance(value, str) or not value.startswith("0x"):
            raise BaseLiveEngineError("Base RPC returned malformed numeric data.", 503)
        try:
            return int(value, 16)
        except ValueError:
            raise BaseLiveEngineError("Base RPC returned malformed numeric data.", 503) from None

    async def _eth_call(self, session, to, data, block="latest"):
        result = await self._rpc(session, "eth_call", [
            {"to": normalize_address(to), "data": data}, block
        ])
        if not isinstance(result, str) or not result.startswith("0x"):
            raise BaseLiveEngineError("Base RPC returned malformed contract data.", 503)
        return result

    async def _token_decimals(self, session, token):
        token = normalize_address(token)
        raw = await self._eth_call(session, token, "0x313ce567")
        try:
            decimals = int(decode(("uint8",), bytes.fromhex(raw[2:]))[0])
        except Exception:
            raise BaseLiveEngineError("The Base token returned invalid decimals.") from None
        if not 0 <= decimals <= 18:
            raise BaseLiveEngineError("The Base token decimal count is unsupported.")
        return decimals

    async def _balance_of(self, session, token, wallet, block="latest"):
        from eth_abi import encode
        call_data = "0x70a08231" + encode(("address",), (normalize_address(wallet),)).hex()
        raw = await self._eth_call(session, token, call_data, block)
        try:
            return int(decode(("uint256",), bytes.fromhex(raw[2:]))[0])
        except Exception:
            raise BaseLiveEngineError("The Base token returned an invalid balance.") from None

    async def _allowance(self, session, token, wallet, spender):
        from eth_abi import encode
        call_data = "0xdd62ed3e" + encode(
            ("address", "address"),
            (normalize_address(wallet), normalize_address(spender)),
        ).hex()
        raw = await self._eth_call(session, token, call_data, "pending")
        try:
            return int(decode(("uint256",), bytes.fromhex(raw[2:]))[0])
        except Exception:
            raise BaseLiveEngineError("The Base token returned an invalid allowance.") from None

    async def _market(self, session):
        async def rpc_call(method, params):
            return await self._rpc(session, method, params)
        return BaseV3Market(rpc_call)

    def _active_operation(self, uid):
        return self.pending_operation(uid)

    def _record_operation(self, uid, kind, token, position_id, metadata):
        operation_id = uuid.uuid4().hex
        now = int(time.time())
        review_message = ""
        with self.db() as db:
            settings = self._ensure_settings(db, uid, now)
            order = metadata.get("order") if isinstance(metadata, dict) else None
            if kind == "BUY":
                self._assert_buy_budget(db, uid, int(metadata.get("amount_in_raw") or 0), settings)
            elif kind == "APPROVE" and isinstance(order, dict) and order.get("side") == "BUY":
                self._assert_buy_budget(db, uid, int(order.get("amount_in_raw") or 0), settings)
            exists = db.execute("""SELECT 1 FROM base_live_operations
                WHERE uid=? AND state IN ('PREPARED','SIGNING','SUBMITTED','UNKNOWN','NEEDS_REVIEW')
                LIMIT 1""", (int(uid),)).fetchone()
            if exists:
                raise BaseLiveEngineError("A Base transaction is still unresolved.")
            db.execute("""INSERT INTO base_live_operations(
                id,uid,kind,state,tx_hash,token_address,position_id,metadata,created_at,updated_at
            ) VALUES(?,?,?,'PREPARED','',?,?,?,?,?)""", (
                operation_id, int(uid), kind, normalize_address(token) if token else "",
                str(position_id or ""), json.dumps(metadata, separators=(",", ":"), sort_keys=True),
                now, now,
            ))
        return operation_id

    def _assert_buy_budget(self, db, uid, amount_raw, settings=None):
        settings = settings or self._ensure_settings(db, uid)
        if not settings["enabled"] or os.getenv("BASE_LIVE_TRADING_ENABLED", "").lower() != "true":
            raise BaseLiveEngineError("Base live mode is off; no new buy can be queued.")
        budget = int(settings["budget_usdc_raw"])
        if budget < MIN_BASE_BUDGET_RAW or int(amount_raw) <= 0:
            raise BaseLiveEngineError("The Base live cap has no valid buy tranche.")
        open_cost = int(db.execute("""SELECT COALESCE(SUM(cost_usdc_raw),0)
            FROM base_live_positions WHERE uid=? AND state IN ('OPEN','REVIEW')""",
            (int(uid),)).fetchone()[0] or 0)
        if budget - open_cost < int(amount_raw):
            raise BaseLiveEngineError("The Base buy exceeds the remaining live budget.")

    def _update_operation(self, operation_id, **fields):
        allowed = {"state", "tx_hash", "nonce", "metadata", "updated_at"}
        if set(fields) - allowed:
            raise ValueError("Unsupported operation field")
        if "metadata" in fields:
            fields["metadata"] = json.dumps(fields["metadata"], separators=(",", ":"), sort_keys=True)
        fields["updated_at"] = int(time.time())
        assignments = ",".join(f"{name}=?" for name in fields)
        with self.db() as db:
            db.execute(f"UPDATE base_live_operations SET {assignments} WHERE id=?",
                       (*fields.values(), operation_id))

    def _claim_signing(self, operation_id):
        with self.db() as db:
            result = db.execute("""UPDATE base_live_operations SET state='SIGNING',updated_at=?
                WHERE id=? AND state='PREPARED' AND tx_hash=''""",
                (int(time.time()), operation_id))
            return result.rowcount == 1

    async def _prepare_and_send(self, session, operation_id):
        """Sign once, persist the deterministic hash, then broadcast once."""
        if os.getenv("BASE_LIVE_TRADING_ENABLED", "").lower() != "true":
            raise BaseLiveEngineError("Base live server kill switch is off.")
        if not self.readiness.persistent_storage_ready():
            with self.db() as db:
                row = db.execute(
                    "SELECT uid FROM base_live_operations WHERE id=?",
                    (operation_id,),
                ).fetchone()
                if row:
                    db.execute(
                        "UPDATE base_live_settings SET enabled=0,updated_at=? WHERE uid=?",
                        (int(time.time()), int(row["uid"])),
                    )
            if row:
                self.set_status(
                    row["uid"], "PAUSED",
                    "Base operation storage is not persistent; trading is paused.",
                )
            raise BaseLiveEngineError(
                "Base operation storage is not persistent; no transaction was signed or sent.",
                503,
            )
        env_status = live_environment_status()
        if not env_status["wallet_configured"] or not env_status["signer_matches_wallet"]:
            raise BaseLiveEngineError("The Base trading wallet signer is not configured.", 503)
        if not env_status["wallet_role_accepted"]:
            raise BaseLiveEngineError("The Base wallet role is not approved for trading.", 503)
        wallet = self._env_wallet()
        with self.db() as db:
            row = db.execute("SELECT * FROM base_live_operations WHERE id=?",
                             (operation_id,)).fetchone()
            if not row:
                raise BaseLiveEngineError("The Base operation was not found.", 404)
            if row["state"] != "PREPARED" or row["tx_hash"]:
                return
            metadata = _json_object(row["metadata"])
            enabled = bool(self._ensure_settings(db, row["uid"])["enabled"])
            kind = row["kind"]
        if kind != "REVOKE" and not enabled:
            self._update_operation(operation_id, state="FAILED")
            return
        chain_id = await self._rpc_int(session, "eth_chainId", [])
        if chain_id != BASE_CHAIN_ID:
            raise BaseLiveEngineError("The configured RPC is not Base mainnet.", 503)
        gas_price = await self._rpc_int(session, "eth_gasPrice", [])
        nonce = await self._rpc_int(session, "eth_getTransactionCount", [wallet, "pending"])
        to = normalize_address(metadata.get("to"))
        data = str(metadata.get("calldata") or "")
        if not (data.startswith("0x") and len(data) >= 10 and len(data) % 2 == 0):
            raise BaseLiveEngineError("The Base transaction calldata is invalid.")
        estimate = await self._rpc_int(session, "eth_estimateGas", [{
            "from": wallet, "to": to, "data": data, "value": "0x0",
        }])
        if estimate <= 0 or estimate > 800_000:
            raise BaseLiveEngineError("Base gas estimate is outside the configured transaction bound.")
        gas_limit = (estimate * 120 + 99) // 100
        if gas_limit > 1_000_000:
            raise BaseLiveEngineError("Base gas limit exceeds the configured transaction bound.")
        estimated_fee = gas_limit * gas_price
        if estimated_fee > MAX_BASE_TX_FEE_WEI:
            raise BaseLiveEngineError("Base gas estimate exceeds the per-transaction safety cap.")
        native_balance = await self._rpc_int(session, "eth_getBalance", [wallet, "latest"])
        if native_balance < MIN_BASE_GAS_RESERVE_WEI + estimated_fee:
            raise BaseLiveEngineError("The Base trading wallet needs more ETH for gas and reserve.")
        transaction = {
            "chainId": BASE_CHAIN_ID,
            "nonce": nonce,
            "gas": gas_limit,
            "gasPrice": gas_price,
            "to": to,
            "value": 0,
            "data": data,
        }
        if not self._claim_signing(operation_id):
            return
        try:
            signed = sign_base_transaction(
                transaction, os.getenv("BASE_TRADING_PRIVATE_KEY", ""), wallet
            )
        except BridgeError:
            self._update_operation(operation_id, state="FAILED")
            raise BaseLiveEngineError("Base transaction signing failed; no transaction was sent.", 503) from None
        tx_hash = signed["transaction_hash"].lower()
        # The known hash is durable before the only broadcast attempt. Any
        # timeout after this point is reconciled by hash and never resent.
        self._update_operation(operation_id, tx_hash=tx_hash, nonce=nonce, state="PREPARED")
        try:
            returned_hash = await self._rpc(
                session, "eth_sendRawTransaction", [signed["raw_transaction"]]
            )
        except BaseLiveEngineError:
            self._update_operation(operation_id, state="UNKNOWN")
            raise BaseLiveEngineError(
                "Broadcast result is unknown. The transaction will be checked by hash; it will not be resent."
            ) from None
        if not isinstance(returned_hash, str) or returned_hash.lower() != tx_hash:
            self._update_operation(operation_id, state="NEEDS_REVIEW")
            raise BaseLiveEngineError(
                "RPC hash did not match the signed transaction. Automatic execution is paused."
            )
        self._update_operation(operation_id, state="SUBMITTED")
        return tx_hash

    @staticmethod
    def _transfer_amount(logs, token, sender, recipient):
        token, sender, recipient = (
            str(token).lower(), str(sender).lower(), str(recipient).lower()
        )
        transfer_topic = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
        total = 0
        if not isinstance(logs, list):
            return 0
        for log in logs:
            if not isinstance(log, dict) or str(log.get("address", "")).lower() != token:
                continue
            topics = log.get("topics")
            if not isinstance(topics, list) or len(topics) < 3:
                continue
            if str(topics[0]).lower() != transfer_topic:
                continue
            from_address = "0x" + str(topics[1])[-40:].lower()
            to_address = "0x" + str(topics[2])[-40:].lower()
            if from_address != sender or to_address != recipient:
                continue
            value = log.get("data")
            if not isinstance(value, str) or not value.startswith("0x"):
                continue
            try:
                total += int(value, 16)
            except ValueError:
                continue
        return total

    def _mark_review(self, operation_id, message, position_id=""):
        with self.db() as db:
            row = db.execute("SELECT uid,kind FROM base_live_operations WHERE id=?",
                             (operation_id,)).fetchone()
            if not row:
                return
            db.execute("""UPDATE base_live_operations SET state='NEEDS_REVIEW',
                updated_at=? WHERE id=?""", (int(time.time()), operation_id))
            if position_id and row["kind"] == "SELL":
                db.execute("""UPDATE base_live_positions SET state='REVIEW'
                    WHERE id=? AND state='OPEN'""", (position_id,))
            db.execute("""UPDATE base_live_settings SET enabled=0,updated_at=?
                WHERE uid=?""", (int(time.time()), row["uid"]))
        self.set_status(row["uid"], "REVIEW", message)

    def _fail_operation(self, operation_id, message):
        revocation_queued = False
        with self.db() as db:
            row = db.execute("SELECT * FROM base_live_operations WHERE id=?",
                             (operation_id,)).fetchone()
            if not row:
                return
            metadata = _json_object(row["metadata"])
            order_meta = metadata.get("order")
            if not isinstance(order_meta, dict):
                order_meta = {}
            db.execute("""UPDATE base_live_operations SET state='FAILED',updated_at=?
                WHERE id=?""", (int(time.time()), operation_id))
            if row["kind"] == "BUY" and metadata.get("cycle_id"):
                remaining = db.execute("""SELECT COUNT(*) AS n FROM base_live_positions
                    WHERE cycle_id=? AND state IN ('OPEN','REVIEW')""",
                    (metadata["cycle_id"],)).fetchone()["n"]
                if not remaining:
                    db.execute("""UPDATE base_live_cycles SET state='CLOSED',closed_at=?
                        WHERE id=? AND current_step=0""",
                        (int(time.time()), metadata["cycle_id"]))
            failed_buy = (
                bool(row["tx_hash"])
                and (
                    row["kind"] == "BUY"
                    or (row["kind"] == "APPROVE"
                        and order_meta.get("side") == "BUY")
                )
            )
            token = normalize_address(
                metadata.get("token_address")
                or order_meta.get("token_address")
                or row["token_address"]
            ) if (metadata.get("token_address")
                  or order_meta.get("token_address")
                  or row["token_address"]) else ""
            if failed_buy and token:
                db.execute("""INSERT INTO base_live_cooldowns(uid,token_address,blocked_until)
                    VALUES(?,?,?) ON CONFLICT(uid,token_address) DO UPDATE SET
                    blocked_until=excluded.blocked_until""",
                    (int(row["uid"]), token,
                     int(time.time()) + BASE_REENTRY_COOLDOWN_SECONDS))
            approval_id = str(metadata.get("approval_operation_id") or "")
            if row["tx_hash"] and approval_id:
                approval = db.execute(
                    "SELECT * FROM base_live_operations WHERE id=? AND kind='APPROVE'",
                    (approval_id,),
                ).fetchone()
                if approval:
                    approval = dict(approval)
                    approved_order = _json_object(approval["metadata"]).get("order")
                else:
                    approved_order = None
            else:
                approval = None
                approved_order = None
        if approval and isinstance(approved_order, dict):
            try:
                self._queue_revocation(row["uid"], approval_id, approved_order)
                revocation_queued = True
            except BaseLiveEngineError:
                pass
        self.set_status(
            row["uid"],
            "REVOCATION_PENDING" if revocation_queued else "FAILED",
            "Failed Base swap finalized; the exact router allowance is queued for revocation."
            if revocation_queued else message,
        )

    async def _reconcile_operation(self, session, operation):
        operation_id = operation["id"]
        tx_hash = str(operation.get("tx_hash") or "").lower()
        if not tx_hash:
            if operation["state"] == "SIGNING":
                if int(time.time()) - int(operation.get("updated_at") or 0) < 60:
                    return
                self._mark_review(
                    operation_id, "Base signing was interrupted before a hash was saved; inspect the wallet before continuing."
                )
                return
            if operation["state"] != "PREPARED":
                self._mark_review(
                    operation_id, "A Base operation has no transaction hash; automatic execution is paused."
                )
                return
            enabled = bool(self.settings(operation["uid"])["enabled"])
            global_enabled = os.getenv("BASE_LIVE_TRADING_ENABLED", "").lower() == "true"
            if operation["kind"] == "REVOKE":
                enabled = True
            if enabled and global_enabled:
                await self._prepare_and_send(session, operation_id)
            else:
                self._fail_operation(
                    operation_id, "An unsent Base operation was cancelled while trading was off."
                )
            return
        try:
            receipt = await self._rpc(
                session, "eth_getTransactionReceipt", [tx_hash]
            )
            if receipt is None:
                return
            if not isinstance(receipt, dict) or not receipt.get("blockNumber"):
                self._mark_review(operation_id, "Base returned an invalid transaction receipt.")
                return
            finalized = await self._rpc(
                session, "eth_getBlockByNumber", ["finalized", False]
            )
            if not isinstance(finalized, dict) or not finalized.get("number"):
                return
            receipt_block = int(receipt["blockNumber"], 16)
            finalized_block = int(finalized["number"], 16)
            if receipt_block > finalized_block:
                return
            success = int(receipt.get("status", "0x0"), 16) == 1
        except BaseLiveEngineError:
            return
        except (TypeError, ValueError, OverflowError):
            self._mark_review(operation_id, "Base receipt could not be verified; automatic trading is paused.")
            return
        if not success:
            self._fail_operation(
                operation_id, "The Base transaction failed on-chain; it will not be resent."
            )
            return
        metadata = _json_object(operation.get("metadata"))
        if operation["kind"] in {"APPROVE", "REVOKE"}:
            order = metadata.get("order") if isinstance(metadata.get("order"), dict) else {}
            token = normalize_address(
                metadata.get("approved_token") or order.get("input_token") or operation.get("token_address")
            )
            amount_expected = 0 if operation["kind"] == "REVOKE" else int(
                metadata.get("amount_in_raw") or order.get("amount_in_raw") or 0
            )
            wallet = self._env_wallet()
            try:
                allowance = await self._allowance(
                    session, token, wallet, BASE_UNISWAP_V3_SWAP_ROUTER_02
                )
            except BaseLiveEngineError:
                return
            if allowance != amount_expected:
                self._mark_review(
                    operation_id, "Final Base approval did not match the exact requested allowance."
                )
                return
            self._update_operation(operation_id, state="CONFIRMED")
            self.set_status(operation["uid"], "READY", "Base approval is finalized and exact.")
            return
        await self._finalize_swap(operation_id, operation, receipt, metadata)

    async def _finalize_swap(self, operation_id, operation, receipt, metadata):
        wallet = self._env_wallet()
        pool = normalize_address(metadata.get("pool_address"))
        input_token = normalize_address(metadata.get("input_token"))
        output_token = normalize_address(metadata.get("output_token"))
        expected_input = int(metadata.get("amount_in_raw") or 0)
        minimum_out = int(metadata.get("minimum_out_raw") or 0)
        actual_input = self._transfer_amount(
            receipt.get("logs"), input_token, wallet, pool
        )
        actual_output = self._transfer_amount(
            receipt.get("logs"), output_token, pool, wallet
        )
        if actual_input != expected_input or actual_output < minimum_out:
            self._mark_review(
                operation_id,
                "Final Base receipt did not match the exact input and minimum output; position is held for review.",
                operation.get("position_id", ""),
            )
            return
        now = int(time.time())
        review_message = ""
        message = ""
        with self.db() as db:
            op = db.execute("SELECT * FROM base_live_operations WHERE id=?",
                            (operation_id,)).fetchone()
            if not op or op["state"] not in {"SUBMITTED", "UNKNOWN", "PREPARED"}:
                return
            if op["kind"] == "BUY":
                cycle_id = str(metadata.get("cycle_id") or "")
                cycle = db.execute("""SELECT * FROM base_live_cycles
                    WHERE id=? AND uid=? AND state='OPEN'""",
                    (cycle_id, int(operation["uid"]))).fetchone()
                if not cycle or int(cycle["current_step"]) + 1 != int(metadata["entry_step"]):
                    db.execute("""UPDATE base_live_operations SET state='NEEDS_REVIEW',
                        updated_at=? WHERE id=?""", (now, operation_id))
                    db.execute("""UPDATE base_live_settings SET enabled=0,updated_at=?
                        WHERE uid=?""", (now, int(operation["uid"])))
                    review_message = "Final Base buy does not match the saved position step."
                else:
                    decimals = int(metadata["token_decimals"])
                    entry_price = (actual_input / 10**BASE_USDC_DECIMALS) / (
                        actual_output / 10**decimals
                    )
                    position_id = str(metadata.get("position_id") or uuid.uuid4().hex)
                    db.execute("""INSERT INTO base_live_positions(
                        id,uid,cycle_id,token_address,symbol,pool_address,fee_tier,
                        token_decimals,entry_step,entry_price_usdc,peak_price_usdc,
                        token_raw,cost_usdc_raw,state,buy_tx_hash,opened_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'OPEN',?,?)""", (
                        position_id, int(operation["uid"]), cycle_id,
                        normalize_address(metadata["token_address"]),
                        str(metadata.get("symbol") or "TOKEN")[:24], pool,
                        int(metadata["fee_tier"]), decimals, int(metadata["entry_step"]),
                        entry_price, entry_price, str(actual_output), actual_input,
                        str(operation["tx_hash"]), now,
                    ))
                    db.execute("""UPDATE base_live_cycles SET current_step=?,
                        last_entry_price_usdc=?,last_entry_candle_at=? WHERE id=?""",
                        (int(metadata["entry_step"]), entry_price,
                         int(metadata.get("entry_candle_at") or now), cycle_id))
                    message = f"Base BUY finalized: {metadata.get('symbol') or 'token'} entry {metadata['entry_step']}."
            else:
                position = db.execute("""SELECT * FROM base_live_positions
                    WHERE id=? AND uid=? AND state='OPEN'""",
                    (str(metadata.get("position_id") or ""), int(operation["uid"]))).fetchone()
                if not position or int(position["token_raw"]) != expected_input:
                    db.execute("""UPDATE base_live_operations SET state='NEEDS_REVIEW',
                        updated_at=? WHERE id=?""", (now, operation_id))
                    db.execute("""UPDATE base_live_settings SET enabled=0,updated_at=?
                        WHERE uid=?""", (now, int(operation["uid"])))
                    if position:
                        db.execute("UPDATE base_live_positions SET state='REVIEW' WHERE id=?",
                                   (position["id"],))
                    review_message = "Final Base sale does not match the saved position amount."
                else:
                    realized = actual_output - int(position["cost_usdc_raw"])
                    db.execute("""UPDATE base_live_positions SET state='CLOSED',
                        sell_tx_hash=?,exit_reason=?,realized_usdc_raw=?,closed_at=?
                        WHERE id=?""", (
                        str(operation["tx_hash"]), str(metadata.get("exit_reason") or "MANUAL")[:80],
                        realized, now, position["id"],
                    ))
                    remaining = db.execute("""SELECT COUNT(*) AS n FROM base_live_positions
                        WHERE cycle_id=? AND state IN ('OPEN','REVIEW')""",
                        (position["cycle_id"],)).fetchone()["n"]
                    if not remaining:
                        db.execute("""UPDATE base_live_cycles SET state='CLOSED',closed_at=?
                            WHERE id=?""", (now, position["cycle_id"]))
                        db.execute("""INSERT INTO base_live_cooldowns(uid,token_address,blocked_until)
                            VALUES(?,?,?) ON CONFLICT(uid,token_address) DO UPDATE SET
                            blocked_until=excluded.blocked_until""", (
                            int(operation["uid"]), position["token_address"],
                            now + BASE_REENTRY_COOLDOWN_SECONDS,
                        ))
                    message = f"Base SELL finalized: {position['symbol']}."
            if review_message:
                db.execute("""UPDATE base_live_operations SET state='NEEDS_REVIEW',
                    updated_at=? WHERE id=?""", (now, operation_id))
            else:
                db.execute("""UPDATE base_live_operations SET state='CONFIRMED',updated_at=?
                    WHERE id=?""", (now, operation_id))
        if review_message:
            self.set_status(operation["uid"], "REVIEW", review_message)
            return
        self.set_status(operation["uid"], "READY", message)

    def _queue_revocation(self, uid, parent_id, order):
        token = normalize_address(order.get("input_token"))
        operation_id = uuid.uuid4().hex
        metadata = {
            "purpose": "REVOKE_APPROVAL",
            "approved_token": token,
            "parent_operation_id": str(parent_id),
            "to": token,
            "calldata": encode_revoke_approval(BASE_UNISWAP_V3_SWAP_ROUTER_02),
        }
        now = int(time.time())
        with self.db() as db:
            parent = db.execute("SELECT * FROM base_live_operations WHERE id=?",
                                (parent_id,)).fetchone()
            if not parent:
                raise BaseLiveEngineError("The Base approval operation was not found.", 404)
            parent_metadata = _json_object(parent["metadata"])
            existing_child = str(parent_metadata.get("child_operation_id") or "")
            if existing_child:
                child = db.execute(
                    "SELECT state FROM base_live_operations WHERE id=?",
                    (existing_child,),
                ).fetchone()
                if child and child["state"] != "FAILED":
                    return existing_child
            self._ensure_settings(db, uid, now)
            db.execute("""INSERT INTO base_live_operations(
                id,uid,kind,state,tx_hash,token_address,position_id,metadata,created_at,updated_at
            ) VALUES(?,?,'REVOKE','PREPARED','',?,'',?,?,?)""", (
                operation_id, int(uid), token,
                json.dumps(metadata, separators=(",", ":"), sort_keys=True),
                now, now,
            ))
            parent_metadata["cancel_requested"] = True
            parent_metadata["child_operation_id"] = operation_id
            db.execute("""UPDATE base_live_operations SET metadata=?,updated_at=?
                WHERE id=?""", (
                json.dumps(parent_metadata, separators=(",", ":"), sort_keys=True),
                now, parent_id,
            ))
        return operation_id

    async def _advance_confirmed_approvals(self, session, uid):
        with self.db() as db:
            rows = [dict(row) for row in db.execute("""SELECT * FROM base_live_operations
                WHERE uid=? AND kind IN ('APPROVE','REVOKE') AND state='CONFIRMED'
                ORDER BY created_at""", (int(uid),))]
        if not rows:
            return
        settings = self.settings(uid)
        global_enabled = os.getenv("BASE_LIVE_TRADING_ENABLED", "").lower() == "true"
        for parent in rows:
            metadata = _json_object(parent.get("metadata"))
            order = metadata.get("order")
            is_reset = (
                parent["kind"] == "REVOKE"
                and metadata.get("purpose") == "RESET_EXACT_ALLOWANCE"
            )
            if (parent["kind"] != "APPROVE" and not is_reset):
                continue
            if not isinstance(order, dict) or metadata.get("child_operation_id"):
                continue
            if not settings["enabled"] or not global_enabled:
                if parent["kind"] == "APPROVE" and global_enabled:
                    self._queue_revocation(uid, parent["id"], order)
                    self.set_status(uid, "OFF", "Unused exact Base allowance is being revoked.")
                elif parent["kind"] == "APPROVE":
                    metadata["cancel_requested"] = True
                    self._update_operation(parent["id"], metadata=metadata)
                    self.set_status(uid, "OFF", "An exact Base allowance is unused; server kill switch is off.")
                continue
            try:
                market = await self._market(session)
                fresh_quote = await market.quote_exact_input(
                    order["pool_address"], order["input_token"],
                    int(order["amount_in_raw"]), token_out=order["output_token"],
                )
            except BaseLiveTradingError:
                if parent["kind"] == "APPROVE":
                    self._queue_revocation(uid, parent["id"], order)
                else:
                    metadata["child_operation_id"] = "SKIPPED"
                    self._update_operation(parent["id"], metadata=metadata)
                self.set_status(uid, "PAUSED", "Base quote could not be refreshed; no swap was sent.")
                continue
            if int(fresh_quote["amount_out_raw"]) < int(order["minimum_out_raw"]):
                if parent["kind"] == "APPROVE":
                    self._queue_revocation(uid, parent["id"], order)
                else:
                    metadata["child_operation_id"] = "SKIPPED"
                    self._update_operation(parent["id"], metadata=metadata)
                self.set_status(uid, "PAUSED", "Base quote moved beyond the saved minimum; no swap was sent.")
                continue
            if is_reset:
                operation_id = uuid.uuid4().hex
                now = int(time.time())
                exact_approval = {
                    "purpose": "EXACT_SWAP_APPROVAL",
                    "order": order,
                    "to": normalize_address(order["input_token"]),
                    "calldata": encode_exact_approval(
                        BASE_UNISWAP_V3_SWAP_ROUTER_02, int(order["amount_in_raw"])
                    ),
                }
                cap_rejected = False
                with self.db() as db:
                    current = db.execute(
                        "SELECT * FROM base_live_operations WHERE id=?",
                        (parent["id"],),
                    ).fetchone()
                    if not current:
                        continue
                    current_meta = _json_object(current["metadata"])
                    if current_meta.get("child_operation_id"):
                        continue
                    try:
                        if order.get("side") == "BUY":
                            self._assert_buy_budget(
                                db, uid, int(order["amount_in_raw"])
                            )
                    except BaseLiveEngineError:
                        current_meta["child_operation_id"] = "SKIPPED"
                        db.execute("""UPDATE base_live_operations SET metadata=?,updated_at=?
                            WHERE id=?""", (
                            json.dumps(current_meta, separators=(",", ":"), sort_keys=True),
                            now, parent["id"],
                        ))
                        cap_rejected = True
                    else:
                        db.execute("""INSERT INTO base_live_operations(
                            id,uid,kind,state,tx_hash,token_address,position_id,metadata,created_at,updated_at
                        ) VALUES(?,?,'APPROVE','PREPARED','',?,?,?,?,?)""", (
                            operation_id, int(uid), normalize_address(order["token_address"]),
                            str(order.get("position_id") or ""),
                            json.dumps(exact_approval, separators=(",", ":"), sort_keys=True),
                            now, now,
                        ))
                        current_meta["child_operation_id"] = operation_id
                        db.execute("""UPDATE base_live_operations SET metadata=?,updated_at=?
                            WHERE id=?""", (
                            json.dumps(current_meta, separators=(",", ":"), sort_keys=True),
                            now, parent["id"],
                        ))
                if cap_rejected:
                    self.set_status(uid, "PAUSED", "Base buy was skipped because the saved cap no longer covers it.")
                    continue
                continue
            inner = order.get("swap_inner_calldata")
            if not isinstance(inner, str):
                self._mark_review(
                    parent["id"], "Saved Base swap calldata is missing; automatic trading is paused."
                )
                continue
            order["calldata"] = encode_deadline_multicall(inner, int(time.time()) + 90)
            order["quote_block"] = int(fresh_quote["block_number"])
            order["expected_out_raw"] = int(fresh_quote["amount_out_raw"])
            order["approval_operation_id"] = parent["id"]
            operation_id = uuid.uuid4().hex
            now = int(time.time())
            try:
                with self.db() as db:
                    current = db.execute("SELECT * FROM base_live_operations WHERE id=?",
                                         (parent["id"],)).fetchone()
                    if not current:
                        continue
                    parent_meta = _json_object(current["metadata"])
                    if parent_meta.get("child_operation_id"):
                        continue
                    if order.get("side") == "BUY":
                        self._assert_buy_budget(
                            db, uid, int(order["amount_in_raw"])
                        )
                    db.execute("""INSERT INTO base_live_operations(
                        id,uid,kind,state,tx_hash,token_address,position_id,metadata,created_at,updated_at
                    ) VALUES(?,?,?,'PREPARED','',?,?,?,?,?)""", (
                        operation_id, int(uid), order["side"],
                        normalize_address(order["token_address"]),
                        str(order.get("position_id") or ""),
                        json.dumps(order, separators=(",", ":"), sort_keys=True),
                        now, now,
                    ))
                    parent_meta["child_operation_id"] = operation_id
                    db.execute("""UPDATE base_live_operations SET metadata=?,updated_at=?
                        WHERE id=?""", (
                        json.dumps(parent_meta, separators=(",", ":"), sort_keys=True),
                        now, parent["id"],
                    ))
            except BaseLiveEngineError:
                self._queue_revocation(uid, parent["id"], order)
                self.set_status(uid, "PAUSED", "Base buy was skipped because the saved cap no longer covers it; the exact allowance is being revoked.")
                continue

    async def _reconcile_pending(self, session, uid):
        with self.db() as db:
            operations = [dict(row) for row in db.execute("""SELECT * FROM base_live_operations
                WHERE uid=? AND state IN ('PREPARED','SIGNING','SUBMITTED','UNKNOWN','NEEDS_REVIEW')
                ORDER BY created_at LIMIT 10""", (int(uid),))]
        for operation in operations:
            if operation["state"] == "NEEDS_REVIEW":
                continue
            await self._reconcile_operation(session, operation)
            refreshed = None
            with self.db() as db:
                row = db.execute("SELECT * FROM base_live_operations WHERE id=?",
                                 (operation["id"],)).fetchone()
                if row:
                    refreshed = dict(row)
            if refreshed and refreshed["state"] == "PREPARED" and not refreshed["tx_hash"]:
                await self._reconcile_operation(session, refreshed)
        await self._advance_confirmed_approvals(session, uid)

    async def _scan_exits(self, session, uid):
        positions = self.open_positions(uid)
        if not positions:
            return False
        market = await self._market(session)
        quote_failed = False
        for position in positions:
            if position["state"] != "OPEN":
                self.set_status(uid, "REVIEW", "A Base position needs manual review.")
                return True
            try:
                quote = await market.quote_exact_input(
                    position["pool_address"],
                    position["token_address"],
                    int(position["token_raw"]),
                    token_out=BASE_USDC_ADDRESS,
                )
                current_price = self._price_from_sell_quote(
                    int(quote["amount_out_raw"]),
                    int(position["token_raw"]),
                    int(position["token_decimals"]),
                )
            except BaseLiveTradingError:
                quote_failed = True
                continue
            peak = max(float(position["peak_price_usdc"]), current_price)
            with self.db() as db:
                db.execute("""UPDATE base_live_positions SET peak_price_usdc=?
                    WHERE id=? AND state='OPEN'""", (peak, position["id"]))
            reason = base_live_exit_reason(
                current_price, float(position["entry_price_usdc"]), peak,
                int(position["entry_step"]),
            )
            if not reason:
                continue
            try:
                operation_id = await self._queue_swap_or_approval(
                    session, uid, side="SELL",
                    token=position["token_address"], symbol=position["symbol"],
                    pool=position["pool_address"], fee=int(position["fee_tier"]),
                    decimals=int(position["token_decimals"]),
                    amount_in_raw=int(position["token_raw"]),
                    entry_step=int(position["entry_step"]),
                    cycle_id=position["cycle_id"], position_id=position["id"],
                    exit_reason=reason,
                )
                self.set_status(uid, "EXIT_PENDING", f"{reason.replace('_', ' ').lower()} sell queued.")
                return True
            except BaseLiveTradingError:
                self.set_status(uid, "EXIT_PAUSED", "Base exit could not be quoted; position remains open.")
                return True
        if quote_failed:
            self.set_status(uid, "EXIT_PAUSED", "A Base exit quote is unavailable; no new entry will be opened.")
            return True
        return False

    def _cycle_for_token(self, uid, token):
        with self.db() as db:
            row = db.execute("""SELECT * FROM base_live_cycles
                WHERE uid=? AND token_address=? AND state='OPEN'""",
                (int(uid), normalize_address(token))).fetchone()
            return dict(row) if row else None

    def _cooldown_active(self, token, now):
        with self.db() as db:
            row = db.execute("""SELECT MAX(blocked_until) AS stamp FROM base_live_cooldowns
                WHERE token_address=?""", (normalize_address(token),)).fetchone()
            return bool(row and row["stamp"] and int(row["stamp"]) > int(now))

    def _open_cost_raw(self, uid):
        with self.db() as db:
            return int(db.execute("""SELECT COALESCE(SUM(cost_usdc_raw),0) AS amount
                FROM base_live_positions WHERE uid=? AND state IN ('OPEN','REVIEW')""",
                (int(uid),)).fetchone()["amount"] or 0)

    def _open_cycle_count(self, uid):
        with self.db() as db:
            return int(db.execute("""SELECT COUNT(*) AS amount FROM base_live_cycles
                WHERE uid=? AND state IN ('OPEN','REVIEW')""",
                (int(uid),)).fetchone()["amount"] or 0)

    async def _scan_entries(self, session, uid):
        settings = self.settings(uid)
        budget_raw = int(settings["budget_usdc_raw"])
        tranche_raw = budget_raw // (BASE_ENTRY_SLOTS * BASE_ENTRY_TRANCHES)
        if budget_raw < MIN_BASE_BUDGET_RAW or tranche_raw <= 0:
            self.set_status(uid, "PAUSED", "Set a Base live budget of at least $15.")
            return False
        if budget_raw - self._open_cost_raw(uid) < tranche_raw:
            self.set_status(uid, "PAUSED", "The Base live budget has no free entry tranche.")
            return False
        wallet = self._env_wallet()
        usdc_balance = await self._balance_of(session, BASE_USDC_ADDRESS, wallet)
        if usdc_balance < tranche_raw:
            self.set_status(uid, "PAUSED", "The Base trading wallet has insufficient USDC for the next tranche.")
            return False
        try:
            candidates = await self.paper_engine.discover(session)
        except BasePaperError:
            self.set_status(uid, "SCAN_PAUSED", "Base market scan is unavailable; no entry was submitted.")
            return False
        if not isinstance(candidates, list):
            self.set_status(uid, "SCAN_PAUSED", "Base market scan returned invalid data.")
            return False
        market = await self._market(session)
        for candidate in candidates[:8]:
            if (not isinstance(candidate, dict) or not candidate.get("entry_eligible")
                    or candidate.get("market_stale")):
                continue
            token = normalize_address(candidate.get("token_address"))
            now = int(time.time())
            cycle = self._cycle_for_token(uid, token)
            if self._cooldown_active(token, now):
                continue
            if not cycle:
                if self._open_cycle_count(uid) >= BASE_ENTRY_SLOTS:
                    continue
                next_step = 1
                previous_entry = None
            else:
                next_step = int(cycle["current_step"]) + 1
                if next_step > BASE_ENTRY_TRANCHES:
                    continue
                previous_entry = float(cycle["last_entry_price_usdc"])
            try:
                quote = await market.find_best_usdc_pool(token, tranche_raw)
                pool = quote["pool_address"]
                decimals = await self._read_token_info(session, token)
                price = await self._price_from_quote(
                    int(quote["amount_out_raw"]), decimals, tranche_raw
                )
                m5, m5_stale = await self.paper_engine._candles(
                    session, pool, "minute", 5, 300, 45
                )
                h1, h1_stale = await self.paper_engine._candles(
                    session, pool, "hour", 1, 3600, 180
                )
                metrics = chart_analysis.analyze_charts(
                    m5, h1, now=time.time(), m5_stale=m5_stale,
                    h1_stale=h1_stale,
                )
            except (BaseLiveTradingError, BasePaperError, ValueError):
                continue
            candle_at = int(metrics.get("chart_m5_closed_at") or 0)
            if metrics.get("chart_ready") != 1 or not candle_at:
                continue
            if cycle and candle_at <= int(cycle["last_entry_candle_at"]):
                continue
            metrics.update({
                "change_m5_pct": candidate.get("change_m5_pct"),
                "change_h1_pct": candidate.get("change_h1_pct"),
                "change_h6_pct": candidate.get("change_h6_pct"),
            })
            verdict = execution_policy.investor_entry_signal(
                token, {token: metrics}, price, previous_entry
            )
            if not verdict.allowed:
                continue
            cycle_id = cycle["id"] if cycle else uuid.uuid4().hex
            if not cycle:
                with self.db() as db:
                    db.execute("""INSERT INTO base_live_cycles(
                        id,uid,token_address,symbol,pool_address,fee_tier,current_step,
                        last_entry_price_usdc,last_entry_candle_at,state,opened_at
                    ) VALUES(?,?,?,?,?,?,0,0,0,'OPEN',?)""", (
                        cycle_id, int(uid), token,
                        str(candidate.get("symbol") or "TOKEN")[:24],
                        pool, int(quote["fee"]), now,
                    ))
            position_id = uuid.uuid4().hex
            try:
                await self._queue_swap_or_approval(
                    session, uid, side="BUY", token=token,
                    symbol=candidate.get("symbol"), pool=pool,
                    fee=int(quote["fee"]), decimals=decimals,
                    amount_in_raw=tranche_raw, entry_step=next_step,
                    cycle_id=cycle_id, position_id=position_id,
                )
                self.set_status(uid, "ENTRY_PENDING", f"Base {candidate.get('symbol') or 'token'} entry {next_step} queued.")
                return True
            except BaseLiveTradingError:
                if not cycle or int(cycle["current_step"]) == 0:
                    with self.db() as db:
                        db.execute("""UPDATE base_live_cycles SET state='CLOSED',closed_at=?
                            WHERE id=? AND current_step=0""", (int(time.time()), cycle_id))
                self.set_status(uid, "ENTRY_PAUSED", "Base entry validation failed; no swap was submitted.")
                return False
        self.set_status(uid, "SCANNED", "No Base candidate passed every entry check.")
        return False

    async def cycle(self, session, uid=None):
        uid = self.owner_id if uid is None else int(uid)
        self._require_owner(uid)
        if self._cycle_lock.locked():
            return
        async with self._cycle_lock:
            try:
                await self._reconcile_pending(session, uid)
                if self.pending_operation(uid):
                    return
                settings = self.settings(uid)
                if not settings["enabled"]:
                    return
                if os.getenv("BASE_LIVE_TRADING_ENABLED", "").lower() != "true":
                    with self.db() as db:
                        db.execute("UPDATE base_live_settings SET enabled=0,updated_at=? WHERE uid=?",
                                   (int(time.time()), uid))
                    self.set_status(uid, "OFF", "Base server kill switch is off.")
                    return
                if not self.readiness.persistent_storage_ready():
                    with self.db() as db:
                        db.execute("UPDATE base_live_settings SET enabled=0,updated_at=? WHERE uid=?",
                                   (int(time.time()), uid))
                    self.set_status(
                        uid, "PAUSED",
                        "Base operation storage is not persistent; trading is paused.",
                    )
                    return
                if await self._scan_exits(session, uid):
                    return
                if time.time() - self._last_entry_scan_at < 90:
                    return
                self._last_entry_scan_at = time.time()
                await self._scan_entries(session, uid)
            except (BaseLiveEngineError, BaseLiveTradingError, BasePaperError):
                self.set_status(uid, "PAUSED", "Base live preflight failed; no new transaction was sent.")

    async def worker(self):
        """Isolated transaction watcher; modes are paused on process restart."""
        while True:
            try:
                async with aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=30)
                ) as session:
                    await self.cycle(session, self.owner_id)
            except Exception:
                self.set_status(
                    self.owner_id, "PAUSED",
                    "Base worker paused after an internal error; no transaction was retried."
                )
            await asyncio.sleep(self._worker_poll_seconds(self.owner_id))

    def _queue_prepared_swap(self, uid, kind, token, position_id, order):
        if kind not in {"BUY", "SELL"}:
            raise BaseLiveEngineError("Unsupported Base swap direction.")
        return self._record_operation(uid, kind, token, position_id, order)

    async def _queue_swap_or_approval(
        self, session, uid, *, side, token, symbol, pool, fee, decimals,
        amount_in_raw, entry_step, cycle_id, position_id, exit_reason="",
    ):
        wallet = self._env_wallet()
        market = await self._market(session)
        token = normalize_address(token)
        if side == "BUY":
            token_in, token_out = BASE_USDC_ADDRESS, token
        elif side == "SELL":
            token_in, token_out = token, BASE_USDC_ADDRESS
        else:
            raise BaseLiveEngineError("Unsupported Base swap direction.")
        quote = await market.quote_exact_input(
            pool, token_in, int(amount_in_raw), token_out=token_out
        )
        if quote["fee"] != int(fee):
            raise BaseLiveEngineError("The verified Uniswap V3 fee changed; rescan before trading.")
        minimum = minimum_output(quote["amount_out_raw"], BASE_SLIPPAGE_BPS)
        deadline = int(time.time()) + 90
        inner = encode_exact_input_single(
            token_in, token_out, int(fee), wallet, int(amount_in_raw), minimum
        )
        calldata = encode_deadline_multicall(inner, deadline)
        order = {
            "side": side,
            "token_address": token,
            "symbol": str(symbol or "TOKEN")[:24],
            "pool_address": normalize_address(pool),
            "fee_tier": int(fee),
            "token_decimals": int(decimals),
            "input_token": normalize_address(token_in),
            "output_token": normalize_address(token_out),
            "amount_in_raw": int(amount_in_raw),
            "expected_out_raw": int(quote["amount_out_raw"]),
            "minimum_out_raw": int(minimum),
            "entry_step": int(entry_step),
            "cycle_id": str(cycle_id or ""),
            "position_id": str(position_id or ""),
            "exit_reason": str(exit_reason)[:80],
            "quote_block": int(quote["block_number"]),
            "to": BASE_UNISWAP_V3_SWAP_ROUTER_02,
            "calldata": calldata,
            "swap_inner_calldata": inner,
        }
        allowance = await self._allowance(
            session, token_in, wallet, BASE_UNISWAP_V3_SWAP_ROUTER_02
        )
        if allowance == int(amount_in_raw):
            return self._queue_prepared_swap(
                uid, side, token, position_id, order
            )
        if allowance > 0:
            reset = {
                "purpose": "RESET_EXACT_ALLOWANCE",
                "approved_token": normalize_address(token_in),
                "order": order,
                "to": normalize_address(token_in),
                "calldata": encode_revoke_approval(BASE_UNISWAP_V3_SWAP_ROUTER_02),
            }
            return self._record_operation(
                uid, "REVOKE", token, position_id, reset
            )
        approval = {
            "purpose": "EXACT_SWAP_APPROVAL",
            "order": order,
            "to": normalize_address(token_in),
            "calldata": encode_exact_approval(
                BASE_UNISWAP_V3_SWAP_ROUTER_02, int(amount_in_raw)
            ),
        }
        return self._record_operation(
            uid, "APPROVE", token, position_id, approval
        )

    async def _read_token_info(self, session, token):
        return await self._token_decimals(session, token)

    async def _price_from_quote(self, output_raw, token_decimals, input_usdc_raw):
        if type(output_raw) is not int or output_raw <= 0:
            raise BaseLiveEngineError("Base returned no token output for the price quote.")
        token_units = output_raw / 10**int(token_decimals)
        usdc_units = int(input_usdc_raw) / 10**BASE_USDC_DECIMALS
        price = usdc_units / token_units if token_units > 0 else 0
        if not isinstance(price, (int, float)) or price <= 0:
            raise BaseLiveEngineError("Base returned an invalid token price.")
        return float(price)

    def _price_from_sell_quote(self, output_usdc_raw, input_token_raw, token_decimals):
        if (type(output_usdc_raw) is not int or output_usdc_raw <= 0
                or type(input_token_raw) is not int or input_token_raw <= 0):
            raise BaseLiveEngineError("Base returned an invalid exit quote.")
        token_units = input_token_raw / 10**int(token_decimals)
        usdc_units = output_usdc_raw / 10**BASE_USDC_DECIMALS
        price = usdc_units / token_units if token_units > 0 else 0
        if price <= 0 or price >= float("inf"):
            raise BaseLiveEngineError("Base returned an invalid exit price.")
        return float(price)
