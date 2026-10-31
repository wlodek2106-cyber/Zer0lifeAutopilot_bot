"""Owner-only, read-only Base market scan and persisted paper-investing ledger.

This module never signs, broadcasts, or reserves a real transaction. A scan is
explicitly requested by the user; paper entries/exits use GeckoTerminal prices
and omit gas, routing fees, price impact, and failed-fill effects.
"""
import asyncio
import json
import math
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime

import aiohttp

import chart_analysis
import dexscreener_client
import execution_policy


BASE_CHAIN_ID = 8453
BASE_NETWORK = "base"
BASE_USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
BASE_WETH = "0x4200000000000000000000000000000000000006"
BASE_QUOTES = {BASE_USDC: "USDC", BASE_WETH: "WETH"}
GECKO_ORIGIN = "https://api.geckoterminal.com/api/v2"
MIN_LIQUIDITY_USD = 10_000
MIN_VOLUME_24H_USD = 100_000
MIN_TXNS_24H = 200
MIN_POOL_AGE_SECONDS = 24 * 60 * 60
MAX_CHART_CANDIDATES = 4
MAX_SLOTS = 5
TRANCHES_PER_SLOT = 3
REENTRY_COOLDOWN_SECONDS = 24 * 60 * 60
PAPER_DEFAULT_BUDGET_USD = 100.0
MAX_PAPER_BUDGET_USD = 100_000.0
M5_CACHE_SECONDS = 180
H1_CACHE_SECONDS = 900
GECKO_MIN_REQUEST_INTERVAL = 2.05
GECKO_DEFAULT_COOLDOWN_SECONDS = 60
ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
SYMBOL_RE = re.compile(r"[^a-zA-Z0-9._-]")
BLOCKED_EQUITY_TERMS = (
    "xstock", "tokenized stock", "tokenised stock", "equity token",
    "stock token", "etf token", "tokenized etf", "tokenised etf",
)
BLOCKED_EQUITY_TICKERS = {
    "AAPL", "AMZN", "AMD", "AVGO", "BABA", "COIN", "GOOG", "GOOGL", "IBM",
    "INTC", "META", "MSTR", "MSFT", "NFLX", "NVDA", "ORCL", "PLTR", "QQQ",
    "SNDK", "SPY", "TSLA", "TSM", "UBER",
}


class BasePaperError(Exception):
    def __init__(self, message, status=503):
        self.status = status
        super().__init__(message)


def number(value):
    if isinstance(value, bool):
        return 0.0
    try:
        parsed = float(value)
        return parsed if math.isfinite(parsed) else 0.0
    except (TypeError, ValueError, OverflowError):
        return 0.0


def finite_numeric(value):
    if value is None or isinstance(value, bool):
        return False
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError, OverflowError):
        return False


def valid_address(value):
    return isinstance(value, str) and ADDRESS_RE.fullmatch(value) is not None


def _address_from_token_id(value):
    if not isinstance(value, str) or not value.startswith("base_"):
        return ""
    candidate = value[5:]
    return candidate.lower() if valid_address(candidate) else ""


def _timestamp(value):
    if not isinstance(value, str) or not value:
        return 0
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    except (TypeError, ValueError, OverflowError):
        return 0


def _symbol(value):
    safe = SYMBOL_RE.sub("", str(value or ""))[:20]
    return safe or "TOKEN"


def _is_excluded_equity(symbol, name):
    value = (str(symbol or "") + " " + str(name or "")).lower()
    normalized_symbol = re.sub(r"[^A-Z0-9]", "", str(symbol or "").upper())
    root_symbol = normalized_symbol[:-1] if normalized_symbol.endswith(("C", "X")) else normalized_symbol
    return (any(term in value for term in BLOCKED_EQUITY_TERMS)
            or normalized_symbol in BLOCKED_EQUITY_TICKERS
            or root_symbol in BLOCKED_EQUITY_TICKERS)


def pool_candidate(pool, included, *, now=None):
    """Validate a GeckoTerminal Base pool and return a display-safe candidate."""
    now = int(time.time()) if now is None else int(now)
    if not isinstance(pool, dict) or not isinstance(pool.get("attributes"), dict):
        return None
    attrs = pool["attributes"]
    relationships = pool.get("relationships")
    if not isinstance(relationships, dict):
        return None
    base_relationship = relationships.get("base_token")
    quote_relationship = relationships.get("quote_token")
    if not isinstance(base_relationship, dict) or not isinstance(quote_relationship, dict):
        return None
    base_relation = base_relationship.get("data")
    quote_relation = quote_relationship.get("data")
    if not isinstance(base_relation, dict) or not isinstance(quote_relation, dict):
        return None
    base_attrs = included.get(base_relation.get("id"), {})
    quote_attrs = included.get(quote_relation.get("id"), {})
    base_address = str(base_attrs.get("address") or
                       _address_from_token_id(base_relation.get("id"))).lower()
    quote_address = str(quote_attrs.get("address") or
                        _address_from_token_id(quote_relation.get("id"))).lower()
    pool_address = str(attrs.get("address") or "").lower()
    if (not valid_address(base_address) or not valid_address(quote_address)
            or not valid_address(pool_address) or quote_address not in BASE_QUOTES
            or base_address in BASE_QUOTES):
        return None

    price = number(attrs.get("base_token_price_usd"))
    quote_price = number(attrs.get("quote_token_price_usd"))
    liquidity = number(attrs.get("reserve_in_usd"))
    volume_data = attrs.get("volume_usd")
    volume = number(volume_data.get("h24")) if isinstance(volume_data, dict) else 0.0
    transactions = attrs.get("transactions")
    daily_transactions = transactions.get("h24", {}) if isinstance(transactions, dict) else {}
    if not isinstance(daily_transactions, dict):
        daily_transactions = {}
    activity = int(number(daily_transactions.get("buys")) + number(daily_transactions.get("sells")))
    created_at = _timestamp(attrs.get("pool_created_at"))
    age_seconds = max(0, now - created_at) if created_at else 0
    symbol_raw = base_attrs.get("symbol") or base_attrs.get("name") or "TOKEN"
    name_raw = base_attrs.get("name") or symbol_raw
    symbol = _symbol(symbol_raw)
    changes = attrs.get("price_change_percentage")
    changes = changes if isinstance(changes, dict) else {}
    m5 = number(changes.get("m5"))
    h1 = number(changes.get("h1"))
    h6 = number(changes.get("h6"))
    metrics_present = all(key in changes and finite_numeric(changes.get(key))
                          for key in ("m5", "h1"))
    reasons = []
    if _is_excluded_equity(symbol_raw, name_raw):
        reasons.append("Tokenized stocks and ETFs are excluded.")
    if price <= 0 or quote_price <= 0:
        reasons.append("Valid token or quote price is unavailable.")
    if liquidity < MIN_LIQUIDITY_USD:
        reasons.append(f"Liquidity is below ${MIN_LIQUIDITY_USD:,.0f}.")
    if volume < MIN_VOLUME_24H_USD:
        reasons.append(f"24h volume is below ${MIN_VOLUME_24H_USD:,.0f}.")
    if activity < MIN_TXNS_24H:
        reasons.append(f"24h transactions are below {MIN_TXNS_24H}.")
    if not created_at or age_seconds < MIN_POOL_AGE_SECONDS:
        reasons.append("Pool is newer than 24h or creation time is unavailable.")
    if not metrics_present:
        reasons.append("Fresh 5m/1h price-change data is unavailable.")

    score = (math.log1p(liquidity) + math.log1p(volume) + math.log1p(activity))
    return {
        "chain_id": BASE_CHAIN_ID,
        "token_address": base_address,
        "pool_address": pool_address,
        "symbol": symbol,
        "name": str(name_raw)[:80],
        "quote_asset": BASE_QUOTES[quote_address],
        "quote_address": quote_address,
        "price_usd": price,
        "quote_price_usd": quote_price,
        "liquidity_usd": liquidity,
        "volume_24h_usd": volume,
        "transactions_24h": activity,
        "age_hours": age_seconds / 3600 if created_at else 0,
        "change_m5_pct": m5,
        "change_h1_pct": h1,
        "change_h6_pct": h6,
        "market_metrics_present": metrics_present,
        "score": score,
        "chart_ready": 0,
        "entry_eligible": False,
        "signal_reason": "",
        "rejection_reasons": reasons,
        "metrics": {
            "change_m5_pct": m5,
            "change_h1_pct": h1,
            "change_h6_pct": h6,
        },
    }


class BasePaperEngine:
    def __init__(self, db_path, owner_id):
        self.db_path = db_path
        self.owner_id = int(owner_id)
        self._scan_lock = asyncio.Lock()
        self._gecko_request_lock = asyncio.Lock()
        self._last_gecko_request = 0.0
        self._gecko_cooldown_until = 0.0
        self._pool_cache = None
        self._pool_cache_at = 0.0
        self._candle_cache = {}
        self._dex_cooldown_message = ""
        self._initialize()

    @contextmanager
    def db(self):
        connection = sqlite3.connect(self.db_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
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
                CREATE TABLE IF NOT EXISTS base_paper_settings (
                    uid INTEGER PRIMARY KEY,
                    budget_usd REAL NOT NULL CHECK(budget_usd>=0),
                    updated_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS base_paper_cycles (
                    id TEXT PRIMARY KEY,
                    uid INTEGER NOT NULL,
                    token_address TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    pool_address TEXT NOT NULL,
                    quote_asset TEXT NOT NULL CHECK(quote_asset IN ('USDC','ETH')),
                    state TEXT NOT NULL CHECK(state IN ('OPEN','CLOSED')),
                    current_step INTEGER NOT NULL DEFAULT 0 CHECK(current_step BETWEEN 0 AND 3),
                    last_entry_price_usd REAL NOT NULL DEFAULT 0,
                    last_entry_candle_at INTEGER NOT NULL DEFAULT 0,
                    last_price_usd REAL NOT NULL DEFAULT 0,
                    last_price_at INTEGER NOT NULL DEFAULT 0,
                    opened_at INTEGER NOT NULL,
                    closed_at INTEGER
                );
                CREATE UNIQUE INDEX IF NOT EXISTS base_paper_one_open_cycle
                    ON base_paper_cycles(uid,token_address) WHERE state='OPEN';
                CREATE INDEX IF NOT EXISTS base_paper_cycles_by_user
                    ON base_paper_cycles(uid,state,opened_at DESC);
                CREATE TABLE IF NOT EXISTS base_paper_lots (
                    id TEXT PRIMARY KEY,
                    cycle_id TEXT NOT NULL REFERENCES base_paper_cycles(id),
                    uid INTEGER NOT NULL,
                    entry_step INTEGER NOT NULL CHECK(entry_step BETWEEN 1 AND 3),
                    amount_usd REAL NOT NULL CHECK(amount_usd>0),
                    entry_price_usd REAL NOT NULL CHECK(entry_price_usd>0),
                    token_amount REAL NOT NULL CHECK(token_amount>0),
                    peak_price_usd REAL NOT NULL CHECK(peak_price_usd>0),
                    state TEXT NOT NULL CHECK(state IN ('OPEN','CLOSED')),
                    exit_price_usd REAL,
                    profit_usd REAL,
                    exit_reason TEXT NOT NULL DEFAULT '',
                    opened_at INTEGER NOT NULL,
                    closed_at INTEGER
                );
                CREATE INDEX IF NOT EXISTS base_paper_lots_by_user
                    ON base_paper_lots(uid,state,opened_at DESC);
                CREATE UNIQUE INDEX IF NOT EXISTS base_paper_one_lot_per_step
                    ON base_paper_lots(cycle_id,entry_step);
                CREATE TABLE IF NOT EXISTS base_paper_scans (
                    uid INTEGER PRIMARY KEY,
                    scanned_at INTEGER NOT NULL,
                    message TEXT NOT NULL,
                    candidates_json TEXT NOT NULL
                );
            """)

    def _ensure_settings(self, db, uid, now=None):
        now = int(time.time()) if now is None else int(now)
        default_budget = PAPER_DEFAULT_BUDGET_USD if int(uid) == self.owner_id else 0.0
        db.execute("""INSERT OR IGNORE INTO base_paper_settings(uid,budget_usd,updated_at)
            VALUES(?,?,?)""", (int(uid), default_budget, now))
        return db.execute("SELECT * FROM base_paper_settings WHERE uid=?",
                          (int(uid),)).fetchone()

    def settings(self, uid):
        with self.db() as db:
            row = self._ensure_settings(db, uid)
            return dict(row)

    def set_budget(self, uid, amount, now=None):
        if isinstance(amount, bool):
            raise ValueError("Enter a valid paper budget.")
        try:
            amount = float(amount)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("Enter a valid paper budget.") from exc
        if not math.isfinite(amount) or amount < 0 or amount > MAX_PAPER_BUDGET_USD:
            raise ValueError(f"Paper budget must be between $0 and ${MAX_PAPER_BUDGET_USD:,.0f}.")
        now = int(time.time()) if now is None else int(now)
        with self.db() as db:
            self._ensure_settings(db, uid, now)
            invested = db.execute("""SELECT COALESCE(SUM(amount_usd),0) AS value
                FROM base_paper_lots WHERE uid=? AND state='OPEN'""",
                (int(uid),)).fetchone()["value"]
            if amount + 1e-9 < float(invested or 0):
                raise ValueError("Budget cannot be below open paper-position cost; scan exits or wait for them to close.")
            if 0 < amount < MAX_SLOTS * TRANCHES_PER_SLOT:
                raise ValueError("Use $0 to pause simulation or at least $15 for the 15 virtual tranches.")
            db.execute("""UPDATE base_paper_settings SET budget_usd=?,updated_at=?
                WHERE uid=?""", (amount, now, int(uid)))
        return self.report(uid, now=now)

    def open_token_addresses(self, uid):
        with self.db() as db:
            return [row["token_address"] for row in db.execute(
                "SELECT token_address FROM base_paper_cycles WHERE uid=? AND state='OPEN'",
                (int(uid),))]

    def report(self, uid, now=None):
        now = int(time.time()) if now is None else int(now)
        with self.db() as db:
            settings = self._ensure_settings(db, uid, now)
            open_cost = float(db.execute("""SELECT COALESCE(SUM(amount_usd),0) AS value
                FROM base_paper_lots WHERE uid=? AND state='OPEN'""",
                (int(uid),)).fetchone()["value"] or 0)
            realized = float(db.execute("""SELECT COALESCE(SUM(profit_usd),0) AS value
                FROM base_paper_lots WHERE uid=? AND state='CLOSED'""",
                (int(uid),)).fetchone()["value"] or 0)
            open_cycles = [dict(row) for row in db.execute("""SELECT * FROM base_paper_cycles
                WHERE uid=? AND state='OPEN' ORDER BY opened_at DESC""", (int(uid),))]
            open_lots = [dict(row) for row in db.execute("""SELECT l.*,c.symbol,
                c.token_address,c.quote_asset,c.last_price_usd,c.last_price_at,c.pool_address
                FROM base_paper_lots l JOIN base_paper_cycles c ON c.id=l.cycle_id
                WHERE l.uid=? AND l.state='OPEN' ORDER BY l.opened_at DESC""", (int(uid),))]
            closed = [dict(row) for row in db.execute("""SELECT l.*,c.symbol,
                c.token_address,c.quote_asset,c.pool_address
                FROM base_paper_lots l JOIN base_paper_cycles c ON c.id=l.cycle_id
                WHERE l.uid=? AND l.state='CLOSED' ORDER BY l.closed_at DESC,l.opened_at DESC
                LIMIT 20""", (int(uid),))]
            scan = db.execute("SELECT * FROM base_paper_scans WHERE uid=?",
                              (int(uid),)).fetchone()

        budget = float(settings["budget_usd"])
        for lot in open_lots:
            price = number(lot.get("last_price_usd"))
            lot["unrealized_gross_pnl_usd"] = (
                lot["amount_usd"] * (price / lot["entry_price_usd"] - 1)
                if price > 0 else None)
            lot["price_stale_seconds"] = (
                max(0, now - int(lot.get("last_price_at") or 0))
                if lot.get("last_price_at") else None)
        candidates = []
        message = "No Base paper scan has been run yet."
        scanned_at = None
        if scan:
            scanned_at = int(scan["scanned_at"])
            message = scan["message"]
            try:
                value = json.loads(scan["candidates_json"])
                candidates = value if isinstance(value, list) else []
            except (json.JSONDecodeError, TypeError):
                candidates = []
                message = "Saved Base paper scan data could not be read."
        return {
            "success": True,
            "network": "Base",
            "chain_id": BASE_CHAIN_ID,
            "paper_only": True,
            "live_trading": False,
            "budget_usd": budget,
            "open_cost_usd": open_cost,
            "free_budget_usd": max(0.0, budget - open_cost),
            "realized_gross_pnl_usd": realized,
            "open_cycle_count": len(open_cycles),
            "open_lot_count": len(open_lots),
            "candidates": candidates,
            "open_positions": open_lots,
            "closed_trades": closed,
            "scanned_at": scanned_at,
            "message": message,
            "pnl_note": ("Gross price-only simulation. Gas, DEX fees, routing, price impact, "
                         "taxes and failed fills are not included; results are not a profit forecast."),
        }

    async def _gecko_json(self, session, path):
        now_mono = time.monotonic()
        if now_mono < self._gecko_cooldown_until:
            retry = max(1, math.ceil(self._gecko_cooldown_until - now_mono))
            raise BasePaperError(f"Base market data is rate-limited; retry in {retry}s.", 429)
        async with self._gecko_request_lock:
            now_mono = time.monotonic()
            if now_mono < self._gecko_cooldown_until:
                retry = max(1, math.ceil(self._gecko_cooldown_until - now_mono))
                raise BasePaperError(f"Base market data is rate-limited; retry in {retry}s.", 429)
            wait = GECKO_MIN_REQUEST_INTERVAL - (now_mono - self._last_gecko_request)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_gecko_request = time.monotonic()
            try:
                async with session.get(
                    GECKO_ORIGIN + path,
                    headers={"Accept": "application/json", "User-Agent": "Zer0Life-Base-Paper"},
                    timeout=aiohttp.ClientTimeout(total=7),
                ) as response:
                    if response.status == 429:
                        try:
                            retry = max(GECKO_DEFAULT_COOLDOWN_SECONDS,
                                        int(response.headers.get("Retry-After", 0)))
                        except (TypeError, ValueError):
                            retry = GECKO_DEFAULT_COOLDOWN_SECONDS
                        self._gecko_cooldown_until = time.monotonic() + min(retry, 1800)
                        raise BasePaperError(
                            f"Base market data is rate-limited; retry in {min(retry, 1800)}s.", 429)
                    if response.status != 200:
                        raise BasePaperError(
                            f"Base market-data provider returned HTTP {response.status}.", 503)
                    try:
                        payload = await response.json()
                    except (aiohttp.ContentTypeError, json.JSONDecodeError, ValueError) as exc:
                        raise BasePaperError("Base market-data provider returned invalid JSON.", 503) from exc
                    if not isinstance(payload, dict):
                        raise BasePaperError("Base market-data provider returned invalid data.", 503)
                    return payload
            except BasePaperError:
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                raise BasePaperError("Base market-data provider is unavailable.", 503) from exc

    async def _pools(self, session):
        now_mono = time.monotonic()
        if self._pool_cache and now_mono - self._pool_cache_at < 60:
            return self._pool_cache, False
        path = ("/networks/base/pools?include=base_token,quote_token"
                "&sort=h24_tx_count_desc&page=1")
        try:
            payload = await self._gecko_json(session, path)
        except BasePaperError:
            if self._pool_cache:
                return self._pool_cache, True
            raise
        if not isinstance(payload.get("data"), list):
            raise BasePaperError("Base pool scan returned an invalid response.", 503)
        self._pool_cache = payload
        self._pool_cache_at = time.monotonic()
        return payload, False

    async def _candles(self, session, pool_address, timeframe, aggregate, interval, ttl):
        key = (pool_address, timeframe)
        now = time.time()
        cached = self._candle_cache.get(key)
        if cached and 0 <= now - cached["fetched_at"] <= ttl:
            return cached["candles"], False
        path = (f"/networks/base/pools/{pool_address}/ohlcv/{timeframe}"
                f"?aggregate={aggregate}&limit=100&currency=usd&token=base")
        try:
            payload = await self._gecko_json(session, path)
            candles = chart_analysis.parse_ohlcv(payload, interval, now=now)
            if not candles:
                raise BasePaperError("Base chart provider returned no valid closed candles.", 503)
            self._candle_cache[key] = {"candles": candles, "fetched_at": now}
            return candles, False
        except BasePaperError:
            if cached:
                return cached["candles"], True
            return [], True

    async def _dex_prices(self, session, addresses):
        addresses = list(dict.fromkeys(
            address.lower() for address in addresses if valid_address(address)))
        if not addresses:
            return {}
        path = "/tokens/v1/base/" + ",".join(addresses[:30])
        try:
            async with dexscreener_client.request(
                session, path, priority=dexscreener_client.PRICE,
                headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
                timeout=aiohttp.ClientTimeout(total=6),
            ) as response:
                if response.status != 200:
                    return {}
                payload = await response.json()
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, TypeError):
            return {}
        if not isinstance(payload, list):
            return {}
        wanted = set(addresses)
        best = {}
        for pair in payload:
            if not isinstance(pair, dict) or pair.get("chainId") != BASE_NETWORK:
                continue
            base = pair.get("baseToken")
            quote = pair.get("quoteToken")
            if not isinstance(base, dict) or not isinstance(quote, dict):
                continue
            token_address = str(base.get("address") or "").lower()
            quote_address = str(quote.get("address") or "").lower()
            price = number(pair.get("priceUsd"))
            if token_address not in wanted or quote_address not in BASE_QUOTES or price <= 0:
                continue
            liquidity = pair.get("liquidity")
            liquidity_usd = number(liquidity.get("usd")) if isinstance(liquidity, dict) else 0
            pair_address = str(pair.get("pairAddress") or "").lower()
            if (liquidity_usd < MIN_LIQUIDITY_USD or not valid_address(pair_address)
                    or liquidity_usd <= best.get(token_address, {}).get("liquidity_usd", -1)):
                continue
            best[token_address] = {
                "price_usd": price,
                "quote_asset": BASE_QUOTES[quote_address],
                "pool_address": pair_address,
                "liquidity_usd": liquidity_usd,
                "fetched_at": int(time.time()),
            }
        return best

    async def discover(self, session):
        pool_payload, pool_stale = await self._pools(session)
        included = {}
        for row in pool_payload.get("included", []):
            if isinstance(row, dict) and row.get("type") == "token":
                included[row.get("id")] = row.get("attributes", {})
        now = int(time.time())
        candidates = []
        for pool in pool_payload["data"]:
            candidate = pool_candidate(pool, included, now=now)
            if candidate:
                candidate["market_stale"] = pool_stale
                if pool_stale:
                    candidate["rejection_reasons"].append("Pool data is stale; no paper entry allowed.")
                candidates.append(candidate)
        candidates.sort(key=lambda row: row["score"], reverse=True)
        unique_candidates = []
        seen_tokens = set()
        for row in candidates:
            if row["token_address"] in seen_tokens:
                continue
            seen_tokens.add(row["token_address"])
            unique_candidates.append(row)
        candidates = unique_candidates
        chartable = [row for row in candidates if not row["rejection_reasons"]][:MAX_CHART_CANDIDATES]
        chartable_addresses = {row["token_address"] for row in chartable}
        for row in candidates:
            if row["token_address"] not in chartable_addresses:
                if not row["rejection_reasons"]:
                    row["rejection_reasons"].append(
                        "Chart data was not requested in this bounded scan.")
                row["signal_reason"] = row["rejection_reasons"][0]
                continue
            m5, m5_stale = await self._candles(
                session, row["pool_address"], "minute", 5, 300, M5_CACHE_SECONDS)
            h1, h1_stale = await self._candles(
                session, row["pool_address"], "hour", 1, 3600, H1_CACHE_SECONDS)
            metrics = chart_analysis.analyze_charts(
                m5, h1, now=time.time(), m5_stale=m5_stale, h1_stale=h1_stale)
            metrics.update({
                "change_m5_pct": row["change_m5_pct"],
                "change_h1_pct": row["change_h1_pct"],
                "change_h6_pct": row["change_h6_pct"],
            })
            row["metrics"] = metrics
            row["chart_ready"] = int(metrics.get("chart_ready") == 1)
            if not row["chart_ready"]:
                row["rejection_reasons"].append(
                    "Fresh, closed 5m and 1h chart data is unavailable.")
                row["signal_reason"] = row["rejection_reasons"][-1]
                continue
            verdict = execution_policy.investor_entry_signal(
                row["token_address"], {row["token_address"]: metrics},
                row["price_usd"])
            row["entry_eligible"] = bool(verdict.allowed and not pool_stale)
            row["signal_reason"] = verdict.reason
            if not verdict.allowed:
                row["rejection_reasons"].append(verdict.reason)
        return candidates

    def apply_snapshot(self, uid, candidates, prices=None, *, now=None):
        """Record one idempotent, user-requested price/chart simulation step."""
        now = int(time.time()) if now is None else int(now)
        prices = prices or {}
        candidates = [dict(item) for item in candidates if isinstance(item, dict)]
        prices = dict(prices)
        for candidate in candidates:
            address = str(candidate.get("token_address") or "").lower()
            price = number(candidate.get("price_usd"))
            if valid_address(address) and price > 0:
                prices.setdefault(address, {
                    "price_usd": price,
                    "quote_asset": candidate.get("quote_asset"),
                    "pool_address": candidate.get("pool_address"),
                })
        actions = []
        with self.db() as db:
            setting = self._ensure_settings(db, uid, now)
            budget = float(setting["budget_usd"])

            # First update open paper marks and deterministic profit exits.
            cycles = [dict(row) for row in db.execute("""SELECT * FROM base_paper_cycles
                WHERE uid=? AND state='OPEN'""", (int(uid),))]
            for cycle in cycles:
                mark = prices.get(cycle["token_address"])
                price = number(mark.get("price_usd")) if isinstance(mark, dict) else 0
                if price <= 0:
                    continue
                pool_address = str(mark.get("pool_address") or cycle["pool_address"]).lower()
                quote_asset = mark.get("quote_asset") if mark.get("quote_asset") in ("USDC", "WETH") else cycle["quote_asset"]
                db.execute("""UPDATE base_paper_cycles SET last_price_usd=?,last_price_at=?,
                    pool_address=?,quote_asset=? WHERE id=?""",
                    (price, now, pool_address, quote_asset, cycle["id"]))
                lots = [dict(row) for row in db.execute("""SELECT * FROM base_paper_lots
                    WHERE cycle_id=? AND state='OPEN'""", (cycle["id"],))]
                for lot in lots:
                    peak = max(number(lot["peak_price_usd"]), price, number(lot["entry_price_usd"]))
                    db.execute("UPDATE base_paper_lots SET peak_price_usd=? WHERE id=?",
                               (peak, lot["id"]))
                    reason = execution_policy.investor_exit_reason(
                        price, number(lot["entry_price_usd"]), peak, int(lot["entry_step"]))
                    if reason:
                        pnl = number(lot["amount_usd"]) * (
                            price / number(lot["entry_price_usd"]) - 1)
                        db.execute("""UPDATE base_paper_lots SET state='CLOSED',
                            exit_price_usd=?,profit_usd=?,exit_reason=?,closed_at=?
                            WHERE id=? AND state='OPEN'""",
                            (price, pnl, reason, now, lot["id"]))
                        actions.append({
                            "kind": "SELL", "symbol": cycle["symbol"],
                            "entry_step": lot["entry_step"], "price_usd": price,
                            "pnl_usd": pnl, "reason": reason,
                        })
                remaining = db.execute("""SELECT COUNT(*) AS n FROM base_paper_lots
                    WHERE cycle_id=? AND state='OPEN'""", (cycle["id"],)).fetchone()["n"]
                current = db.execute("SELECT current_step FROM base_paper_cycles WHERE id=?",
                                      (cycle["id"],)).fetchone()
                if not remaining and current and int(current["current_step"]) >= TRANCHES_PER_SLOT:
                    db.execute("""UPDATE base_paper_cycles SET state='CLOSED',closed_at=?
                        WHERE id=? AND state='OPEN'""", (now, cycle["id"]))

            for candidate in candidates:
                reasons = list(candidate.get("rejection_reasons") or [])
                address = str(candidate.get("token_address") or "").lower()
                price = number(candidate.get("price_usd"))
                metrics = candidate.get("metrics") if isinstance(candidate.get("metrics"), dict) else {}
                candle_at = int(number(metrics.get("chart_m5_closed_at")))
                if not valid_address(address) or price <= 0:
                    candidate["entry_eligible"] = False
                    candidate["signal_reason"] = "Token identity or price is invalid."
                    continue
                if reasons or candidate.get("chart_ready") != 1 or not candle_at:
                    candidate["entry_eligible"] = False
                    if not reasons:
                        reasons.append("Fresh chart confirmation is required.")
                    candidate["signal_reason"] = reasons[0]
                    continue

                cycle = db.execute("""SELECT * FROM base_paper_cycles
                    WHERE uid=? AND token_address=? AND state='OPEN'""",
                    (int(uid), address)).fetchone()
                previous_entry = None
                next_step = 1
                if cycle:
                    cycle = dict(cycle)
                    previous_entry = number(cycle["last_entry_price_usd"])
                    next_step = int(cycle["current_step"]) + 1
                    if next_step > TRANCHES_PER_SLOT:
                        candidate["entry_eligible"] = False
                        candidate["signal_reason"] = "All three paper entry tranches have been used."
                        continue
                    if candle_at <= int(cycle["last_entry_candle_at"]):
                        candidate["entry_eligible"] = False
                        candidate["signal_reason"] = "Waiting for a new closed 5m candle before another tranche."
                        continue
                else:
                    active_slots = int(db.execute("""SELECT COUNT(*) AS n FROM base_paper_cycles
                        WHERE uid=? AND state='OPEN'""", (int(uid),)).fetchone()["n"])
                    if active_slots >= MAX_SLOTS:
                        candidate["entry_eligible"] = False
                        candidate["signal_reason"] = f"All {MAX_SLOTS} Base paper slots are occupied."
                        continue
                    last_closed = db.execute("""SELECT MAX(closed_at) AS stamp
                        FROM base_paper_cycles WHERE uid=? AND token_address=? AND state='CLOSED'""",
                        (int(uid), address)).fetchone()["stamp"]
                    if last_closed and now - int(last_closed) < REENTRY_COOLDOWN_SECONDS:
                        candidate["entry_eligible"] = False
                        candidate["signal_reason"] = "Token is in the 24h paper re-entry cooldown."
                        continue

                verdict = execution_policy.investor_entry_signal(
                    address, {address: metrics}, price, previous_entry)
                if not verdict.allowed:
                    candidate["entry_eligible"] = False
                    candidate["signal_reason"] = verdict.reason
                    continue
                open_cost = float(db.execute("""SELECT COALESCE(SUM(amount_usd),0) AS value
                    FROM base_paper_lots WHERE uid=? AND state='OPEN'""",
                    (int(uid),)).fetchone()["value"] or 0)
                tranche_usd = budget / (MAX_SLOTS * TRANCHES_PER_SLOT)
                if budget <= 0 or tranche_usd < 1 or budget - open_cost + 1e-9 < tranche_usd:
                    candidate["entry_eligible"] = False
                    candidate["signal_reason"] = "The shared virtual budget has no free tranche."
                    continue
                token_amount = tranche_usd / price
                if not math.isfinite(token_amount) or token_amount <= 0:
                    candidate["entry_eligible"] = False
                    candidate["signal_reason"] = "Paper quantity could not be calculated."
                    continue

                if not cycle:
                    cycle_id = uuid.uuid4().hex
                    db.execute("""INSERT INTO base_paper_cycles
                        (id,uid,token_address,symbol,pool_address,quote_asset,state,current_step,
                         last_entry_price_usd,last_entry_candle_at,last_price_usd,last_price_at,opened_at)
                        VALUES(?,?,?,?,?,?,'OPEN',0,0,0,?,?,?)""",
                        (cycle_id, int(uid), address, str(candidate.get("symbol") or "TOKEN")[:20],
                         str(candidate.get("pool_address") or "").lower(),
                         candidate.get("quote_asset") if candidate.get("quote_asset") in ("USDC", "WETH") else "USDC",
                         price, now, now))
                    cycle = db.execute("SELECT * FROM base_paper_cycles WHERE id=?",
                                       (cycle_id,)).fetchone()
                    cycle = dict(cycle)
                else:
                    cycle_id = cycle["id"]
                lot_id = uuid.uuid4().hex
                db.execute("""INSERT INTO base_paper_lots
                    (id,cycle_id,uid,entry_step,amount_usd,entry_price_usd,token_amount,
                     peak_price_usd,state,opened_at)
                    VALUES(?,?,?,?,?,?,?,?, 'OPEN',?)""",
                    (lot_id, cycle_id, int(uid), next_step, tranche_usd, price,
                     token_amount, price, now))
                db.execute("""UPDATE base_paper_cycles SET current_step=?,last_entry_price_usd=?,
                    last_entry_candle_at=?,last_price_usd=?,last_price_at=?,pool_address=?,quote_asset=?
                    WHERE id=?""",
                    (next_step, price, candle_at, price, now,
                     str(candidate.get("pool_address") or "").lower(),
                     candidate.get("quote_asset") if candidate.get("quote_asset") in ("USDC", "WETH") else "USDC",
                     cycle_id))
                candidate["entry_eligible"] = True
                candidate["signal_reason"] = verdict.reason
                candidate["simulated_action"] = f"BUY tranche {next_step}"
                actions.append({
                    "kind": "BUY", "symbol": str(candidate.get("symbol") or "TOKEN")[:20],
                    "entry_step": next_step, "amount_usd": tranche_usd,
                    "price_usd": price,
                })

            public_candidates = [self._public_candidate(row) for row in candidates]
            bought = sum(action["kind"] == "BUY" for action in actions)
            sold = sum(action["kind"] == "SELL" for action in actions)
            if bought or sold:
                message = f"Paper scan complete: {bought} simulated buys, {sold} simulated exits. No live transactions."
            elif not candidates:
                message = "Base scan completed but found no eligible quote pools."
            else:
                message = "Base scan completed; no entry or exit trigger passed. No live transactions."
            db.execute("""INSERT INTO base_paper_scans(uid,scanned_at,message,candidates_json)
                VALUES(?,?,?,?) ON CONFLICT(uid) DO UPDATE SET
                scanned_at=excluded.scanned_at,message=excluded.message,
                candidates_json=excluded.candidates_json""",
                (int(uid), now, message, json.dumps(public_candidates, separators=(",", ":"))))
        return self.report(uid, now=now)

    @staticmethod
    def _public_candidate(row):
        return {
            "token_address": row.get("token_address"),
            "pool_address": row.get("pool_address"),
            "symbol": row.get("symbol"),
            "quote_asset": row.get("quote_asset"),
            "price_usd": number(row.get("price_usd")),
            "liquidity_usd": number(row.get("liquidity_usd")),
            "volume_24h_usd": number(row.get("volume_24h_usd")),
            "transactions_24h": int(number(row.get("transactions_24h"))),
            "age_hours": number(row.get("age_hours")),
            "change_m5_pct": number(row.get("change_m5_pct")),
            "change_h1_pct": number(row.get("change_h1_pct")),
            "chart_ready": int(row.get("chart_ready") == 1),
            "entry_eligible": bool(row.get("entry_eligible")),
            "signal_reason": str(row.get("signal_reason") or "")[:220],
            "simulated_action": str(row.get("simulated_action") or ""),
            "rejection_reasons": [
                str(reason)[:180] for reason in (row.get("rejection_reasons") or [])[:8]
            ],
        }

    async def scan(self, session, uid):
        if int(uid) != self.owner_id:
            raise BasePaperError("Base paper simulation is currently owner-only.", 403)
        async with self._scan_lock:
            candidates = await self.discover(session)
            current_prices = {
                row["token_address"]: {
                    "price_usd": row["price_usd"],
                    "quote_asset": row["quote_asset"],
                    "pool_address": row["pool_address"],
                    "fetched_at": int(time.time()),
                }
                for row in candidates
                if number(row.get("price_usd")) > 0 and not row.get("market_stale")
            }
            open_addresses = self.open_token_addresses(uid)
            if open_addresses:
                current_prices.update(await self._dex_prices(session, open_addresses))
            return self.apply_snapshot(uid, candidates, current_prices)
