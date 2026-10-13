"""Bounded Solana candidate discovery, not an all-project index or profit forecast."""
import asyncio
import hashlib
import json
import math
import re
import time
from urllib.parse import quote

import aiohttp
from solders.pubkey import Pubkey

TOKEN = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
DEXES = {"raydium", "orca", "meteora", "pumpswap"}


def number(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else 0
    except (ValueError, TypeError, OverflowError):
        return 0


def valid_mint(value):
    try:
        return str(Pubkey.from_string(value)) == value
    except (ValueError, TypeError):
        return False


def pool_metrics(pair, now=None):
    now = time.time() if now is None else now
    if not isinstance(pair, dict):
        return None
    if not isinstance(pair.get("baseToken"), dict) or any(
            pair.get(key) is not None and not isinstance(pair[key], dict)
            for key in ("liquidity", "volume", "txns", "priceChange")):
        return None
    if (pair.get("chainId") != "solana" or not isinstance(pair.get("dexId"), str)
            or pair["dexId"] not in DEXES):
        return None
    mint = pair.get("baseToken", {}).get("address")
    if not valid_mint(mint):
        return None
    liquid = number((pair.get("liquidity") or {}).get("usd"))
    vol = number((pair.get("volume") or {}).get("h24"))
    age = (now - number(pair.get("pairCreatedAt")) / 1000) / 3600
    trades = (pair.get("txns") or {}).get("h24") or {}
    if not isinstance(trades, dict):
        return None
    activity = number(trades.get("buys")) + number(trades.get("sells"))
    change = pair.get("priceChange") or {}
    price = number(pair.get("priceUsd"))
    changes_present = all(isinstance(change.get(key), (int, float)) and not isinstance(change[key], bool)
                          and math.isfinite(change[key]) for key in ("m5", "h1"))
    eligible = (price > 0 and liquid >= 100000 and vol >= 100000 and activity >= 200 and changes_present
                and number(pair.get("pairCreatedAt")) > 0 and age >= 24
                and abs(number(change.get("m5"))) <= 35 and abs(number(change.get("h1"))) <= 150)
    # Screening priority is activity/liquidity, never expected return.
    score = math.log1p(max(liquid, 0)) + math.log1p(max(vol, 0)) + math.log1p(max(activity, 0))
    symbol = re.sub(r"[^a-zA-Z0-9_-]", "", str(pair.get("baseToken", {}).get("symbol", "")))[:20] or "TOKEN"
    return {"mint": mint, "symbol": symbol, "price": price, "liquidity_usd": liquid,
            "volume_24h_usd": vol, "transactions_24h": activity, "age_hours": max(age, 0),
            "change_m5_pct": number(change.get("m5")), "change_h1_pct": number(change.get("h1")),
            "score": score, "eligible": eligible}


class MemeDiscovery:
    def __init__(self, ledger, rpc, excluded, seeds=None):
        self.ledger, self.rpc, self.excluded = ledger, rpc, set(excluded)
        self.seeds = seeds or {}
        self.aliases = {info["mint"]: code for code, info in self.seeds.items()}
        self.blocked = True
        self.lock = asyncio.Lock()
        self.report = {"source": "DexScreener profiles + meme/pump search", "complete": False,
                       "message": "Candidate scan has not run yet.", "eligible": 0}
        with ledger.db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS custody_meme_assets(
                code TEXT PRIMARY KEY, mint TEXT NOT NULL UNIQUE, decimals INTEGER NOT NULL,
                metrics TEXT NOT NULL, checked_at INTEGER NOT NULL)""")

    async def fetch(self, session, path):
        async with session.get("https://api.dexscreener.com" + path,
                headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
                timeout=aiohttp.ClientTimeout(total=10)) as response:
            if response.status != 200:
                raise ValueError("DexScreener unavailable: HTTP " + str(response.status))
            return await response.json()

    def assets(self):
        with self.ledger.db() as db:
            return [dict(row) for row in db.execute("SELECT * FROM custody_meme_assets")]

    async def pairs(self, session, mints):
        found = []
        for offset in range(0, len(mints), 30):
            data = await self.fetch(session, "/tokens/v1/solana/" + ",".join(mints[offset:offset+30]))
            if not isinstance(data, list):
                raise ValueError("DexScreener returned an invalid list.")
            found.extend(data)
        return found

    async def scan(self, session):
        async with self.lock:
            meta_paths = []
            try:
                metas = await self.fetch(session, "/metas/trending/v1")
                meta_paths = ["/metas/meta/v1/" + row["slug"] for row in metas
                    if isinstance(row, dict) and isinstance(row.get("slug"), str)
                    and re.fullmatch(r"[a-z0-9-]{1,80}", row["slug"])
                    and "meme" in str(row.get("name", "")).lower()][:2]
            except Exception:
                pass
            results = await asyncio.gather(
                self.fetch(session, "/token-profiles/latest/v1"),
                self.fetch(session, "/token-profiles/recent-updates/v1"),
                self.fetch(session, "/latest/dex/search?q=" + quote("meme")),
                self.fetch(session, "/latest/dex/search?q=" + quote("pump")),
                *[self.fetch(session, "/latest/dex/search?q=" + term) for term in ("BONK", "POPCAT", "BOME")],
                *[self.fetch(session, path) for path in meta_paths],
                return_exceptions=True)
            mints, failures, sources = list(self.aliases), 0, []
            for result in results:
                if isinstance(result, BaseException):
                    failures += 1
                    continue
                if isinstance(result, list):
                    values = [item.get("tokenAddress") for item in result if isinstance(item, dict) and item.get("chainId") == "solana"]
                elif isinstance(result, dict):
                    values = [p.get("baseToken", {}).get("address") for p in result.get("pairs", [])
                              if isinstance(p, dict) and p.get("chainId") == "solana"]
                else:
                    failures += 1
                    continue
                sources.append(values[:30])
            # Round robin prevents a single latest-profile feed crowding out search.
            for values in zip(*[source + [None] * (30-len(source)) for source in sources]):
                for mint in values:
                    if valid_mint(mint) and mint not in self.excluded and mint not in mints:
                        mints.append(mint)
            mints = mints[:90]
            if not mints:
                raise ValueError("Sources returned no Solana candidates.")
            best = {}
            for pair in await self.pairs(session, mints):
                item = pool_metrics(pair)
                if item and item["mint"] in mints and item["eligible"]:
                    if item["liquidity_usd"] > best.get(item["mint"], {}).get("liquidity_usd", 0):
                        best[item["mint"]] = item
            ranked = sorted(best.values(), key=lambda item: item["score"], reverse=True)[:20]
            accepted = []
            if ranked:
                accounts = await self.rpc(session, "getMultipleAccounts", [
                    [item["mint"] for item in ranked], {"encoding": "jsonParsed", "commitment": "finalized"}])
                values = accounts.get("value", [])
                if len(values) != len(ranked):
                    raise ValueError("RPC did not confirm all candidate mint accounts.")
                for item, account in zip(ranked, values):
                    if not account or account.get("owner") != TOKEN or not isinstance(account.get("data"), dict):
                        continue
                    parsed = account["data"].get("parsed") or {}
                    info = parsed.get("info") or {}
                    decimals = info.get("decimals")
                    if (parsed.get("type") != "mint" or not info.get("isInitialized")
                            or not isinstance(decimals, int) or isinstance(decimals, bool) or not 0 <= decimals <= 9
                            or info.get("mintAuthority") is not None or info.get("freezeAuthority") is not None):
                        continue
                    code = self.aliases.get(item["mint"]) or "M_" + hashlib.sha256(item["mint"].encode()).hexdigest()[:16]
                    if code in self.seeds and decimals != self.seeds[code]["decimals"]:
                        continue
                    accepted.append({**item, "code": code, "decimals": decimals})
            now = int(time.time())
            with self.ledger.db() as db:
                # Retain old registry identities for open positions and cached decisions.
                for row in db.execute("SELECT code,metrics FROM custody_meme_assets").fetchall():
                    metrics = json.loads(row["metrics"]); metrics["eligible"] = False
                    db.execute("UPDATE custody_meme_assets SET metrics=? WHERE code=?", (json.dumps(metrics), row["code"]))
                for item in accepted:
                    previous = db.execute("SELECT mint,decimals FROM custody_meme_assets WHERE code=?",
                                          (item["code"],)).fetchone()
                    if previous and (previous["mint"], previous["decimals"]) != (item["mint"], item["decimals"]):
                        raise ValueError("Candidate identity or decimals changed. New entries paused.")
                    db.execute("""INSERT INTO custody_meme_assets VALUES(?,?,?,?,?)
                        ON CONFLICT(code) DO UPDATE SET metrics=excluded.metrics,checked_at=excluded.checked_at""",
                        (item["code"], item["mint"], item["decimals"], json.dumps(item), now))
            self.report = {"source": "DexScreener profiles + meme meta feeds + bounded searches", "complete": False,
                "checked_at": now, "scanned": len(mints), "eligible": len(accepted), "source_failures": failures,
                "message": "Bounded Solana candidate sample, not every project or a profit forecast. Profiles alone do not establish meme status. Paid boosts are not buy signals.",
                "candidates": accepted}
            self.blocked = False
            return self.report

    def candidates(self):
        if self.blocked:
            return []
        now = time.time()
        rows = [json.loads(row["metrics"]) for row in self.assets()
                if 0 <= now-row["checked_at"] < 1800 and json.loads(row["metrics"]).get("eligible")]
        return sorted(rows, key=lambda row: row["score"], reverse=True)[:8]

    def fail(self):
        self.blocked = True
        self.report = {**self.report, "message": "Candidate scan unavailable. New sniper entries paused.",
                       "entry_paused": True}

    async def prices(self, session, held=()):
        wanted = {row["code"] for row in self.candidates()} | set(held)
        assets = [row for row in self.assets() if row["code"] in wanted]
        prices = {}
        by_mint = {row["mint"]: row for row in assets}
        best = {}
        for pair in await self.pairs(session, list(by_mint)):
            item = pool_metrics(pair)
            if item and item["price"] > 0 and item["mint"] in by_mint:
                if item["liquidity_usd"] > best.get(item["mint"], {}).get("liquidity_usd", -1):
                    best[item["mint"]] = item
        with self.ledger.db() as db:
            for mint, item in best.items():
                asset = by_mint[mint]
                prices[asset["code"]] = item["price"]
                old = json.loads(asset["metrics"])
                # Price refresh cannot admit a new candidate; a full scan is required.
                old.update({key: value for key, value in item.items() if key not in ("eligible",)})
                old["eligible"] = bool(old.get("eligible")) and item["eligible"]
                db.execute("UPDATE custody_meme_assets SET metrics=? WHERE code=?", (json.dumps(old), asset["code"]))
        return prices

    def entry_allowed(self, code):
        if self.blocked:
            return False
        now = time.time()
        return any(row["code"] == code and 0 <= now - row["checked_at"] < 1800
                   and json.loads(row["metrics"]).get("eligible") for row in self.assets())
