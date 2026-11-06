"""Mint-keyed, isolated paper execution for screened Solana meme candidates.

No wallet, RPC, signer, transaction, swap, or live-sniper dependency is used.
"""
import hashlib
import math
import re
import sqlite3
import time

from zerolife_ai_trader.paper_store import PaperTradeStore


class MemePaperEngine:
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
            raise ValueError("Invalid paper fee.")
        if type(slippage_bps) is not int or not 0 <= slippage_bps <= 1000:
            raise ValueError("Invalid paper slippage.")
        self.db_path = db_path
        self.store = PaperTradeStore(db_path)
        self.fee_bps, self.slippage_bps = fee_bps, slippage_bps
        with sqlite3.connect(db_path, timeout=10) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS meme_paper_assets (
                telegram_id INTEGER NOT NULL CHECK(telegram_id > 0),
                asset_key TEXT NOT NULL, mint TEXT NOT NULL, symbol TEXT NOT NULL,
                discovery_code TEXT, updated_at INTEGER NOT NULL,
                PRIMARY KEY(telegram_id, asset_key), UNIQUE(telegram_id, mint))""")
            db.execute("""CREATE TABLE IF NOT EXISTS meme_paper_peaks (
                telegram_id INTEGER NOT NULL CHECK(telegram_id > 0),
                asset_key TEXT NOT NULL, peak_price REAL NOT NULL CHECK(peak_price > 0),
                updated_at INTEGER NOT NULL, PRIMARY KEY(telegram_id, asset_key))""")
            db.execute("""CREATE TABLE IF NOT EXISTS meme_paper_capital (
                telegram_id INTEGER PRIMARY KEY CHECK(telegram_id > 0),
                starting_cash REAL NOT NULL CHECK(starting_cash > 0),
                created_at INTEGER NOT NULL)""")

    @staticmethod
    def _number(v):
        return type(v) in (int, float) and math.isfinite(v)

    @staticmethod
    def valid_mint(mint):
        if not isinstance(mint, str) or re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{32,44}", mint) is None:
            return False
        alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
        value = 0
        for char in mint:
            value = value * 58 + alphabet.index(char)
        decoded_bytes = (value.bit_length() + 7) // 8
        decoded_bytes += len(mint) - len(mint.lstrip("1"))
        return decoded_bytes == 32

    @classmethod
    def asset_key(cls, mint):
        if not cls.valid_mint(mint):
            raise ValueError("A valid Solana mint is required.")
        # PaperTradeStore's legacy symbol column is capped at 32 characters.
        return "M" + hashlib.sha256(mint.encode("ascii")).hexdigest()[:31].upper()

    @classmethod
    def candidate_is_eligible(cls, item):
        if not isinstance(item, dict) or item.get("eligible") is not True:
            return False
        fields = ("price", "liquidity_usd", "volume_24h_usd", "transactions_24h",
                  "age_hours", "change_m5_pct", "change_h1_pct")
        if any(not cls._number(item.get(k)) for k in fields) or not cls.valid_mint(item.get("mint")):
            return False
        return (isinstance(item.get("symbol"), str) and bool(item["symbol"].strip())
            and item["price"] > 0 and item["liquidity_usd"] >= cls.MIN_LIQUIDITY_USD
            and item["volume_24h_usd"] >= cls.MIN_VOLUME_24H_USD
            and item["transactions_24h"] >= cls.MIN_TRADES_24H
            and item["age_hours"] >= cls.MIN_POOL_AGE_HOURS
            and 0 < item["change_m5_pct"] <= 3 and 0 <= item["change_h1_pct"] <= 10)

    @staticmethod
    def _signal_id(uid, mint, side, snapshot):
        raw = f"mem-paper:{uid}:{mint}:{side}:{snapshot}".encode()
        return int.from_bytes(hashlib.sha256(raw).digest()[:8], "big") & 0x7FFFFFFFFFFFFFFF or 1

    def _capital(self, uid, requested):
        with sqlite3.connect(self.db_path, timeout=10) as db:
            # Mem-Sniper starts with its own default/explicit paper capital.
            # Never inherit the AI Trader paper account's balance or settings.
            initial = float(requested)
            db.execute("INSERT OR IGNORE INTO meme_paper_capital VALUES(?,?,?)",
                       (uid, initial, int(time.time())))
            row = db.execute("SELECT starting_cash FROM meme_paper_capital WHERE telegram_id=?", (uid,)).fetchone()
            return float(row[0])

    def status_report(self, *, telegram_id, market_prices=None):
        if type(telegram_id) is not int or telegram_id <= 0:
            raise ValueError("Invalid paper account owner.")
        market_prices = market_prices or {}
        if not isinstance(market_prices, dict):
            raise ValueError("Market prices must be a mapping.")
        account = self.store.account(telegram_id=telegram_id)
        meta = self._metadata(telegram_id)
        capital = self._capital(telegram_id, 1000.0)
        positions, marked = [], 0.0
        for pos in account["positions"]:
            asset = meta.get(pos["symbol"])
            mint = asset["mint"] if asset else None
            price = market_prices.get(mint) if mint else None
            fresh = self._number(price) and price > 0
            if not fresh:
                price = pos["average_cost"]
            marked += pos["quantity"] * price
            positions.append({**pos, "mint": mint,
                "symbol_label": asset["symbol"] if asset else pos["symbol"],
                "market_price": price, "price_fresh": fresh})
        cash = float(account["cash"]) if account["cash"] is not None else capital
        daily = self.store.daily_realized_pnl(telegram_id=telegram_id)
        return {"execution":"solana_meme_paper_only","live_transactions_sent":0,
            "wallet_accessed":False,"starting_cash":capital,"cash":cash,
            "positions":positions,"marked_positions_value":marked,
            "equity_estimate":cash+marked,"daily_realized_pnl":daily,
            "daily_loss_limit_hit":daily <= -capital*self.MAX_DAILY_LOSS_FRACTION,
            "summary":self.store.summary(telegram_id=telegram_id),"actions":[],
            "stale_positions":[{"mint":p["mint"],"symbol":p["symbol_label"],
                "reason":"fresh_price_unavailable"} for p in positions if not p["price_fresh"]]}

    def _metadata(self, uid):
        with sqlite3.connect(self.db_path, timeout=10) as db:
            rows = db.execute("SELECT asset_key,mint,symbol,discovery_code FROM meme_paper_assets WHERE telegram_id=?", (uid,)).fetchall()
        return {r[0]: {"asset_key":r[0],"mint":r[1],"symbol":r[2],"code":r[3]} for r in rows}

    def _save_asset(self, uid, item, now):
        key, mint = self.asset_key(item["mint"]), item["mint"]
        with sqlite3.connect(self.db_path, timeout=10) as db:
            existing = db.execute("SELECT asset_key FROM meme_paper_assets WHERE telegram_id=? AND mint=?", (uid,mint)).fetchone()
            if existing and existing[0] != key:
                raise ValueError("Paper mint identity conflict.")
            db.execute("""INSERT INTO meme_paper_assets
                (telegram_id,asset_key,mint,symbol,discovery_code,updated_at) VALUES(?,?,?,?,?,?)
                ON CONFLICT(telegram_id,asset_key) DO UPDATE SET
                symbol=excluded.symbol,discovery_code=excluded.discovery_code,updated_at=excluded.updated_at""",
                (uid,key,mint,item["symbol"].strip(),item.get("code"),now))
        return key

    def _peak(self, uid, key, price, now):
        with sqlite3.connect(self.db_path, timeout=10) as db:
            row=db.execute("SELECT peak_price FROM meme_paper_peaks WHERE telegram_id=? AND asset_key=?",(uid,key)).fetchone()
            peak=max(float(row[0]) if row else price,price)
            db.execute("""INSERT INTO meme_paper_peaks VALUES(?,?,?,?)
                ON CONFLICT(telegram_id,asset_key) DO UPDATE SET
                peak_price=MAX(meme_paper_peaks.peak_price,excluded.peak_price),updated_at=excluded.updated_at""",
                (uid,key,peak,now))
            return peak

    def _clear_peak(self, uid, key):
        with sqlite3.connect(self.db_path, timeout=10) as db:
            db.execute("DELETE FROM meme_paper_peaks WHERE telegram_id=? AND asset_key=?",(uid,key))

    def open_position_assets(self, telegram_id):
        account=self.store.account(telegram_id=telegram_id)
        meta=self._metadata(telegram_id)
        return [meta[p["symbol"]] for p in account["positions"] if p["symbol"] in meta]

    def process_snapshot(self, *, telegram_id, candidates, starting_cash=1000.0,
                         market_prices=None, snapshot_id=None, now=None):
        if type(telegram_id) is not int or telegram_id <= 0: raise ValueError("Invalid paper account owner.")
        if not self._number(starting_cash) or starting_cash <= 0: raise ValueError("Invalid starting cash.")
        if not isinstance(candidates,list): raise ValueError("Candidates must be a list.")
        if market_prices is None: market_prices={}
        if not isinstance(market_prices,dict): raise ValueError("Market prices must be a mapping.")
        now=int(time.time()) if now is None else int(now)
        snapshot_id=now if snapshot_id is None else snapshot_id
        if type(snapshot_id) is not int or snapshot_id <= 0: raise ValueError("Invalid snapshot identifier.")
        by_mint={}
        for item in candidates:
            if isinstance(item,dict) and self.valid_mint(item.get("mint")) and self._number(item.get("price")) and item["price"]>0:
                old=by_mint.get(item["mint"])
                if old is None or item.get("liquidity_usd",0)>old.get("liquidity_usd",0): by_mint[item["mint"]]=item

        account=self.store.account(telegram_id=telegram_id)
        capital=self._capital(telegram_id, float(starting_cash))
        meta=self._metadata(telegram_id)
        actions=[]; stale=[]; closed=set()

        # Monitor all persisted open mints, regardless of current shortlist membership.
        for pos in list(account["positions"]):
            key=pos["symbol"]; asset=meta.get(key)
            if not asset:
                stale.append({"asset_key":key,"reason":"legacy_position_without_mint"})
                continue
            mint=asset["mint"]; item=by_mint.get(mint)
            price=market_prices.get(mint)
            if not self._number(price) or price<=0: price=item.get("price") if item else None
            if not self._number(price) or price<=0:
                stale.append({"mint":mint,"symbol":asset["symbol"],"reason":"fresh_price_unavailable"}); continue
            peak=self._peak(telegram_id,key,float(price),now); entry=float(pos["average_cost"])
            reason=("STOP_LOSS" if price<=entry*(1-self.STOP_LOSS_FRACTION) else
                    "TRAILING_STOP" if peak>=entry*(1+self.TRAIL_ACTIVATION_FRACTION)
                    and price<=peak*(1-self.TRAIL_DRAWDOWN_FRACTION) else None)
            if reason:
                result=self.store.execute_sell(telegram_id=telegram_id,
                    signal_id=self._signal_id(telegram_id,mint,"SELL",snapshot_id),
                    symbol=key,quantity=pos["quantity"],market_price=float(price),
                    fee_bps=self.fee_bps,slippage_bps=self.slippage_bps)
                actions.append({"mint":mint,"symbol":asset["symbol"],"action":"PAPER_SELL","reason":reason,"execution":result})
                if result["executed"]: self._clear_peak(telegram_id,key); closed.add(mint)

        account=self.store.account(telegram_id=telegram_id)
        positions={p["symbol"]:p for p in account["positions"]}
        daily=self.store.daily_realized_pnl(telegram_id=telegram_id)
        loss_hit=daily <= -capital*self.MAX_DAILY_LOSS_FRACTION
        if not loss_hit:
            for mint,item in by_mint.items():
                if not self.candidate_is_eligible(item) or mint in closed: continue
                key=self.asset_key(mint)
                if key in positions or len(positions)>=self.MAX_OPEN_POSITIONS: continue
                cash=float(account["cash"]) if account["cash"] is not None else capital
                budget=min(cash,capital*self.MAX_ORDER_FRACTION)
                if budget<=0: break
                qty=budget/(item["price"]*(1+self.slippage_bps/10000)*(1+self.fee_bps/10000))
                if not self._number(qty) or qty<=0: continue
                self._save_asset(telegram_id,item,now)
                result=self.store.execute_buy(telegram_id=telegram_id,
                    signal_id=self._signal_id(telegram_id,mint,"BUY",snapshot_id),
                    symbol=key,quantity=qty,market_price=item["price"],
                    fee_bps=self.fee_bps,slippage_bps=self.slippage_bps,starting_cash=capital)
                actions.append({"mint":mint,"symbol":item["symbol"],"action":"PAPER_BUY",
                    "reason":"Screened mint passed paper entry gates.","execution":result})
                account=self.store.account(telegram_id=telegram_id)
                positions={p["symbol"]:p for p in account["positions"]}

        account=self.store.account(telegram_id=telegram_id); meta=self._metadata(telegram_id)
        enriched=[]; marked=0.0
        for p in account["positions"]:
            a=meta.get(p["symbol"]); mint=a["mint"] if a else None
            item=by_mint.get(mint) if mint else None
            price=market_prices.get(mint) if mint else None
            fresh=self._number(price) and price>0
            if not fresh: price=item.get("price") if item else p["average_cost"]; fresh=bool(item)
            marked+=p["quantity"]*price
            enriched.append({**p,"mint":mint,"symbol_label":a["symbol"] if a else p["symbol"],
                "market_price":price,"price_fresh":fresh})
        cash=float(account["cash"]) if account["cash"] is not None else capital
        return {"execution":"solana_meme_paper_only","live_transactions_sent":0,"wallet_accessed":False,
            "starting_cash":capital,"cash":cash,"positions":enriched,"marked_positions_value":marked,
            "equity_estimate":cash+marked,"daily_realized_pnl":daily,"daily_loss_limit_hit":loss_hit,
            "summary":self.store.summary(telegram_id=telegram_id),"actions":actions,
            "stale_positions":stale,"snapshot_at":now}
