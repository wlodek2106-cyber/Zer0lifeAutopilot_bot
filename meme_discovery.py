"""Bounded Solana candidate discovery, not an all-project index or profit forecast."""
import asyncio
import hashlib
import json
import math
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote

import aiohttp
from solders.pubkey import Pubkey
from spl_token_support import (
    TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID, validate_mint_account,
)

TOKEN = TOKEN_PROGRAM_ID
TOKEN_2022 = TOKEN_2022_PROGRAM_ID
DEXES = {"raydium", "orca", "meteora", "pumpswap"}
MIN_LIQUIDITY_USD = 10_000
MAX_DISCOVERED_MINTS = 200
MAX_VERIFIED_CANDIDATES = 40
AI_SHORTLIST_SIZE = 8
SHORTLIST_ROTATION_SECONDS = 15 * 60
# Discovery hints only. Every result remains subject to the same market and
# on-chain checks below; a matching ticker is not treated as token identity.
TARGETED_SEARCH_TERMS = ("FARTCOIN", "PONKE", "GIGA", "GOAT", "MOTHER", "MYRO", "SLERF")


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
    if pair.get("chainId") != "solana":
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
    reasons = []
    if not isinstance(pair.get("dexId"), str) or pair["dexId"] not in DEXES:
        reasons.append("DEX is not approved")
    if price <= 0:
        reasons.append("price unavailable")
    if liquid < MIN_LIQUIDITY_USD:
        reasons.append(f"liquidity ${liquid:,.0f} below ${MIN_LIQUIDITY_USD:,.0f}")
    if vol < 100000:
        reasons.append(f"24h volume ${vol:,.0f} below $100,000")
    if activity < 200:
        reasons.append(f"24h trades {activity:,.0f} below 200")
    if number(pair.get("pairCreatedAt")) <= 0:
        reasons.append("pool creation time unavailable")
    elif age < 24:
        reasons.append(f"pool age {age:.1f}h below 24h")
    if not changes_present:
        reasons.append("5m/1h price-change data unavailable")
    else:
        if abs(number(change.get("m5"))) > 35:
            reasons.append(f"5m move {number(change.get('m5')):.1f}% exceeds ±35%")
        if abs(number(change.get("h1"))) > 150:
            reasons.append(f"1h move {number(change.get('h1')):.1f}% exceeds ±150%")
    # Screening priority is activity/liquidity, never expected return.
    score = math.log1p(max(liquid, 0)) + math.log1p(max(vol, 0)) + math.log1p(max(activity, 0))
    symbol = re.sub(r"[^a-zA-Z0-9_-]", "", str(pair.get("baseToken", {}).get("symbol", "")))[:20] or "TOKEN"
    return {"mint": mint, "symbol": symbol, "price": price, "liquidity_usd": liquid,
            "volume_24h_usd": vol, "transactions_24h": activity, "age_hours": max(age, 0),
            "change_m5_pct": number(change.get("m5")), "change_h1_pct": number(change.get("h1")),
            "dex": pair.get("dexId"), "score": score, "eligible": not reasons,
            "rejection_reasons": reasons}


class MemeDiscovery:
    def __init__(self, ledger, rpc, excluded, seeds=None):
        self.ledger, self.rpc, self.excluded = ledger, rpc, set(excluded)
        self.seeds = seeds or {}
        self.aliases = {info["mint"]: code for code, info in self.seeds.items()}
        self.whale_priorities = {}
        self.whale_seen = {}
        self.blocked = True
        self.lock = asyncio.Lock()
        # Shared by discovery and price refreshes, not per participant.
        self.request_lock = asyncio.Lock()
        self.next_request_at = 0
        self.provider_cooldown_until = 0
        self.rate_limit_failures = 0
        self.hint_cache = {}
        self.report = {"source": "DexScreener profiles + meme/pump search", "complete": False,
                       "message": "Candidate scan has not run yet.", "eligible": 0}
        with ledger.db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS custody_meme_assets(
                code TEXT PRIMARY KEY, mint TEXT NOT NULL UNIQUE, decimals INTEGER NOT NULL,
                metrics TEXT NOT NULL, checked_at INTEGER NOT NULL)""")

    def queue_whale_mint(self, mint, now=None):
        now = int(time.time() if now is None else now)
        if not valid_mint(mint) or mint in self.excluded or mint in self.aliases:
            return False
        for old_mint, checked_at in list(self.whale_seen.items()):
            if now - checked_at >= 600:
                self.whale_seen.pop(old_mint, None)
        last_seen = self.whale_seen.get(mint)
        if last_seen is not None and now - last_seen < 600:
            return False
        self.whale_seen[mint] = now
        self.whale_priorities[mint] = now
        while len(self.whale_seen) > 5000:
            self.whale_seen.pop(next(iter(self.whale_seen)))
        while len(self.whale_priorities) > 90:
            self.whale_priorities.pop(next(iter(self.whale_priorities)))
        return True

    def pending_whale_mints(self, now=None):
        now = int(time.time() if now is None else now)
        for mint, queued_at in list(self.whale_priorities.items()):
            if now - queued_at >= 1800:
                self.whale_priorities.pop(mint, None)
        return [mint for mint, _ in sorted(
            self.whale_priorities.items(), key=lambda item: item[1])[:30]]

    def cooldown_remaining(self):
        return max(0, math.ceil(self.provider_cooldown_until - time.monotonic()))

    @staticmethod
    def retry_after_seconds(value):
        try:
            seconds = float(value)
            if math.isfinite(seconds):
                return max(0, seconds)
        except (ValueError, TypeError):
            pass
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            return max(0, (date - datetime.now(timezone.utc)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            return 0

    async def fetch(self, session, path):
        # Cache discovery leads only. Prices and eligibility still require fresh
        # pool responses and the existing on-chain/chart/execution checks.
        hint = not path.startswith("/tokens/")
        async with self.request_lock:
            now = time.monotonic()
            if self.cooldown_remaining():
                raise ValueError("DexScreener unavailable: HTTP 429")
            cached = self.hint_cache.get(path) if hint else None
            if cached and now - cached[0] < 300:
                return cached[1]
            await asyncio.sleep(max(0, self.next_request_at - now))
            self.next_request_at = time.monotonic() + 1.0
            async with session.get("https://api.dexscreener.com" + path,
                    headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
                    timeout=aiohttp.ClientTimeout(total=10)) as response:
                if response.status == 429:
                    self.rate_limit_failures = min(6, self.rate_limit_failures + 1)
                    delay = max(60 * 2 ** (self.rate_limit_failures - 1),
                                self.retry_after_seconds(response.headers.get("Retry-After")))
                    self.provider_cooldown_until = time.monotonic() + delay
                if response.status != 200:
                    raise ValueError("DexScreener unavailable: HTTP " + str(response.status))
                data = await response.json()
                self.rate_limit_failures = 0
                if hint:
                    self.hint_cache[path] = (time.monotonic(), data)
                return data

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
            whale_mints = self.pending_whale_mints()
            meta_paths = []
            try:
                metas = await self.fetch(session, "/metas/trending/v1")
                meta_paths = ["/metas/meta/v1/" + row["slug"] for row in metas
                    if isinstance(row, dict) and isinstance(row.get("slug"), str)
                    and re.fullmatch(r"[a-z0-9-]{1,80}", row["slug"])
                    and "meme" in str(row.get("name", "")).lower()][:2]
            except Exception:
                pass
            # Do not queue a burst ahead of protective position-price refreshes.
            paths = [
                "/token-profiles/latest/v1",
                "/token-profiles/recent-updates/v1",
                "/latest/dex/search?q=" + quote("meme"),
                "/latest/dex/search?q=" + quote("pump"),
                *["/latest/dex/search?q=" + term
                  for term in ("BONK", "POPCAT", "BOME", *TARGETED_SEARCH_TERMS)],
                *meta_paths]
            results = []
            for path in paths:
                try:
                    results.append(await self.fetch(session, path))
                except Exception as exc:
                    results.append(exc)
                    if self.cooldown_remaining():
                        raise
            mints, failures, sources = list(dict.fromkeys([*whale_mints, *self.aliases])), 0, []
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
            mints = mints[:MAX_DISCOVERED_MINTS]
            if not mints:
                raise ValueError("Sources returned no Solana candidates.")
            best = {}
            rejected_pools = {}
            for pair in await self.pairs(session, mints):
                item = pool_metrics(pair)
                if not item or item["mint"] not in mints:
                    continue
                if item["eligible"]:
                    if item["liquidity_usd"] > best.get(item["mint"], {}).get("liquidity_usd", 0):
                        best[item["mint"]] = item
                else:
                    previous = rejected_pools.get(item["mint"])
                    approved_dex = item["dex"] in DEXES
                    previous_approved_dex = previous and previous["dex"] in DEXES
                    if (previous is None or (approved_dex and not previous_approved_dex)
                            or (approved_dex == previous_approved_dex
                                and (len(item["rejection_reasons"]) < len(previous["rejection_reasons"])
                                     or (len(item["rejection_reasons"]) == len(previous["rejection_reasons"])
                                         and item["score"] > previous["score"])))):
                        rejected_pools[item["mint"]] = item
            ranked = sorted(best.values(), key=lambda item: item["score"], reverse=True)[
                :MAX_VERIFIED_CANDIDATES]
            accepted = []
            mint_rejections = {}
            for item in sorted(best.values(), key=lambda row: row["score"], reverse=True)[
                    MAX_VERIFIED_CANDIDATES:]:
                mint_rejections[item["mint"]] = [
                    f"outside the top {MAX_VERIFIED_CANDIDATES} candidates checked on-chain"]
            if ranked:
                accounts = await self.rpc(session, "getMultipleAccounts", [
                    [item["mint"] for item in ranked], {"encoding": "jsonParsed", "commitment": "finalized"}])
                values = accounts.get("value", [])
                if len(values) != len(ranked):
                    raise ValueError("RPC did not confirm all candidate mint accounts.")
                for item, account in zip(ranked, values):
                    reasons = []
                    code = self.aliases.get(item["mint"])
                    seed = self.seeds.get(code) if code else None
                    expected_program = (seed or {}).get("token_program", TOKEN)
                    expected_symbol = (seed or {}).get("label") or (seed or {}).get("symbol")
                    try:
                        program = validate_mint_account(
                            account, item["mint"],
                            expected_decimals=(seed or {}).get("decimals"),
                            expected_symbol=expected_symbol,
                            expected_program=expected_program,
                            allow_token_2022=bool(seed and expected_program == TOKEN_2022))
                        decimals = account["data"]["parsed"]["info"]["decimals"]
                    except (ValueError, KeyError, TypeError) as exc:
                        mint_rejections[item["mint"]] = [str(exc)]
                        continue
                    code = code or "M_" + hashlib.sha256(item["mint"].encode()).hexdigest()[:16]
                    if code in self.seeds and decimals != self.seeds[code]["decimals"]:
                        mint_rejections[item["mint"]] = ["mint decimals do not match the saved asset identity"]
                        continue
                    accepted.append({**item, "code": code, "decimals": decimals,
                                     "token_program": program})
            now = int(time.time())
            with self.ledger.db() as db:
                # Retain old registry identities for open positions and cached decisions.
                for row in db.execute("SELECT code,metrics FROM custody_meme_assets").fetchall():
                    metrics = json.loads(row["metrics"]); metrics["eligible"] = False
                    db.execute("UPDATE custody_meme_assets SET metrics=? WHERE code=?", (json.dumps(metrics), row["code"]))
                for item in accepted:
                    existing_mint = db.execute(
                        "SELECT code,decimals FROM custody_meme_assets WHERE mint=?",
                        (item["mint"],)).fetchone()
                    if existing_mint:
                        if existing_mint["decimals"] != item["decimals"]:
                            raise ValueError("Candidate mint decimals changed. New entries paused.")
                        # Keep the persisted identity for held positions and cached decisions.
                        item["code"] = existing_mint["code"]
                    previous = db.execute("SELECT mint,decimals FROM custody_meme_assets WHERE code=?",
                                          (item["code"],)).fetchone()
                    if previous and (previous["mint"], previous["decimals"]) != (item["mint"], item["decimals"]):
                        raise ValueError("Candidate identity or decimals changed. New entries paused.")
                    db.execute("""INSERT INTO custody_meme_assets VALUES(?,?,?,?,?)
                        ON CONFLICT(code) DO UPDATE SET metrics=excluded.metrics,checked_at=excluded.checked_at""",
                        (item["code"], item["mint"], item["decimals"], json.dumps(item), now))
            for mint in whale_mints:
                self.whale_priorities.pop(mint, None)
            accepted_mints = {item["mint"] for item in accepted}
            whale_rejections = []
            for mint in whale_mints:
                if mint in accepted_mints:
                    continue
                if mint in best:
                    reasons = mint_rejections.get(mint, ["candidate was not accepted by the on-chain screen"])
                else:
                    item = rejected_pools.get(mint)
                    reasons = (item["rejection_reasons"] if item else
                               mint_rejections.get(mint, ["no supported Solana pool data found"]))
                whale_rejections.append({
                    "mint": mint[:6] + "…" + mint[-5:],
                    "reasons": reasons[:6],
                })
            self.report = {"source": "DexScreener profiles + meme meta feeds + bounded searches", "complete": False,
                "checked_at": now, "scanned": len(mints), "eligible": len(accepted), "source_failures": failures,
                "whale_prioritized": len([mint for mint in whale_mints if mint in mints]),
                "whale_candidates": sum(1 for item in accepted if item["mint"] in whale_mints),
                "whale_rejections": whale_rejections,
                "message": "Bounded Solana candidate sample, not every project or a profit forecast. Whale signals are leads only; all candidates still pass the existing screen.",
                "candidates": accepted}
            self.blocked = False
            return self.report

    def candidates(self, now=None):
        if self.blocked:
            return []
        now = time.time() if now is None else now
        rows = [json.loads(row["metrics"]) for row in self.assets()
                if 0 <= now-row["checked_at"] < 1800 and json.loads(row["metrics"]).get("eligible")]
        ranked = sorted(rows, key=lambda row: row["score"], reverse=True)[:MAX_VERIFIED_CANDIDATES]
        if len(ranked) <= AI_SHORTLIST_SIZE:
            return ranked
        # Keep each AI shortlist stable for one analysis interval, then rotate through
        # all on-chain-verified candidates rather than showing only the top eight.
        start = (int(now // SHORTLIST_ROTATION_SECONDS) * AI_SHORTLIST_SIZE) % len(ranked)
        return [ranked[(start + offset) % len(ranked)] for offset in range(AI_SHORTLIST_SIZE)]

    @staticmethod
    def failure_category(exc):
        if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
            return "network timeout"
        if isinstance(exc, aiohttp.ClientResponseError):
            return f"provider HTTP {exc.status}"
        if isinstance(exc, aiohttp.ClientError):
            return "network/provider error"
        message = str(exc)
        match = re.fullmatch(r"DexScreener unavailable: HTTP (\d{3})", message)
        if match:
            return f"market-data HTTP {match.group(1)}"
        if "Solana RPC" in message:
            return "Solana RPC error"
        if isinstance(exc, ValueError):
            return "invalid external scan data"
        return f"internal scan error ({type(exc).__name__})"

    def fail(self, reason=None):
        self.blocked = True
        message = "Candidate scan unavailable"
        if reason:
            message += f" ({reason})"
        self.report = {**self.report,
                       "message": message + ". New sniper entries paused; automatic retry scheduled.",
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
