"""Public multi-user custody routes. All user ownership comes from verified Telegram."""
import asyncio
import base64
import json
import math
import os
import random
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING

import aiohttp
from aiohttp import web
from solders.address_lookup_table_account import (
    AddressLookupTable, AddressLookupTableAccount, ID as ADDRESS_LOOKUP_TABLE_PROGRAM_ID,
)
from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.message import Message, MessageV0, to_bytes_versioned
from solders.pubkey import Pubkey
from solders.system_program import transfer, TransferParams
from solders.transaction import Transaction, VersionedTransaction

from wallet_runtime import WalletRuntime, WalletError, amount_raw, associated, TOKEN, ATA, SYSTEM, MEMO, GENESIS
from custody_ledger import CustodyLedger, LedgerError, USDC, SOL, FEE_CAP, available, position_account
from legacy_retirement import prove_retirement, RECOVERY_RPC
from tx_deposits import incoming_amounts
from solders.signature import Signature
import ai_decisions
import execution_policy
import activity_log
from entry_costs import EntryCostError, quote_amounts
from meme_discovery import MemeDiscovery
from whale_stream import HeliusWhaleStream

WRAPPED_SOL = "So11111111111111111111111111111111111111112"
JITO_BUNDLE_SEND_URL = "https://mainnet.block-engine.jito.wtf/api/v1/transactions?bundleOnly=true"
JITO_TIP_ACCOUNTS_URL = "https://mainnet.block-engine.jito.wtf/api/v1/bundles"
JITO_TIP_FLOOR_URL = "https://bundles.jito.wtf/api/v1/bundles/tip_floor"
JITO_TIP_MIN_LAMPORTS = 50_000
JITO_TIP_MAX_LAMPORTS = 150_000
JITO_TIP_CACHE_SECONDS = 15
JITO_TIP_MAX_AGE_SECONDS = 120
JUPITER_PRIORITY_FEE_LAMPORTS = 25_000
WHALE_AI_COOLDOWN_SECONDS = 60


def operation_failure_reason(exc):
    if isinstance(exc, (WalletError, LedgerError, EntryCostError)):
        detail = str(exc)
    elif isinstance(exc, asyncio.TimeoutError):
        detail = "Network request timed out."
    elif isinstance(exc, aiohttp.ClientError):
        detail = f"Network request failed ({type(exc).__name__})."
    else:
        detail = f"Execution failed ({type(exc).__name__}); detailed provider response hidden."
    return activity_log.safe_failure_reason(detail)


def asset_for_mint(mint):
    return SOL if mint == WRAPPED_SOL else mint


def jupiter_swap_instructions_payload(quote, wallet):
    return {
        "quoteResponse": quote,
        "userPublicKey": wallet,
        "wrapAndUnwrapSol": True,
        "dynamicComputeUnitLimit": True,
        "prioritizationFeeLamports": {
            "priorityLevelWithMaxLamports": {
                "priorityLevel": "veryHigh",
                "maxLamports": JUPITER_PRIORITY_FEE_LAMPORTS,
                "global": False,
            }
        },
    }


def parse_jupiter_instruction(value):
    if not isinstance(value, dict):
        raise WalletError("Jupiter returned an incomplete swap instruction.")
    program_id = value.get("programId")
    accounts = value.get("accounts", [])
    encoded_data = value.get("data")
    if (not isinstance(program_id, str) or not isinstance(accounts, list)
            or not isinstance(encoded_data, str)):
        raise WalletError("Jupiter returned a malformed swap instruction.")
    if len(accounts) > 256:
        raise WalletError("The Jupiter swap instruction contains too many accounts.")
    try:
        metas = []
        for account in accounts:
            if (not isinstance(account, dict) or not isinstance(account.get("pubkey"), str)
                    or type(account.get("isSigner")) is not bool
                    or type(account.get("isWritable")) is not bool):
                raise ValueError("invalid account metadata")
            metas.append(AccountMeta(
                Pubkey.from_string(account["pubkey"]),
                account["isSigner"],
                account["isWritable"],
            ))
        return Instruction(
            Pubkey.from_string(program_id),
            base64.b64decode(encoded_data, validate=True),
            metas,
        )
    except Exception as exc:
        raise WalletError("Jupiter returned malformed swap instruction data.") from exc


def operator_tip_lamports(info):
    tip = info.get("jito_tip_lamports", 0)
    route = info.get("execution_route")
    if (type(tip) is not int or tip < 0 or tip > JITO_TIP_MAX_LAMPORTS
            or (route == "jito_bundle_only"
                and not JITO_TIP_MIN_LAMPORTS <= tip <= JITO_TIP_MAX_LAMPORTS)
            or (tip and route != "jito_bundle_only")):
        raise WalletError("The recorded Jito tip is outside the approved 50,000–150,000 lamport range.")
    return tip


def adjust_swap_amounts_for_operator_tip(spent, received, asset, output_asset, info):
    tip = operator_tip_lamports(info)
    if asset == SOL:
        spent -= tip
    if output_asset == SOL:
        received += tip
    return spent, received


def display_raw(raw, decimals):
    result = format(Decimal(raw) / Decimal(10**decimals), "f")
    return result.rstrip("0").rstrip(".") if "." in result else result


def memo_invoice(tx):
    found = []
    for instruction in tx["transaction"]["message"].get("instructions", []):
        if instruction.get("programId") == str(MEMO) and isinstance(instruction.get("parsed"), str):
            found.append(instruction["parsed"])
    if len(found) != 1 or not found[0].startswith("ZL1:"):
        raise WalletError("Нужен ровно один Memo платёжного запроса.", 409)
    iid = found[0][4:]
    if len(iid) != 32 or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for c in iid):
        raise WalletError("Некорректный идентификатор Memo.", 409)
    return iid


class CustodyRuntime(WalletRuntime):
    def __init__(self, *args, base_url, notification_token="", market_context_fn=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.notification_token = notification_token
        self.market_context_fn = market_context_fn
        self._notification_lock = asyncio.Lock()
        self.legacy_runtime = WalletRuntime(*args, **kwargs)
        self.ledger = CustodyLedger(self.db_file)
        self.ledger.tx_epoch()
        self.base_url = base_url.rstrip("/")
        self.public_enabled = os.getenv("PUBLIC_CUSTODY_ENABLED", "").lower() == "true"
        beta = os.getenv("CUSTODY_BETA_USERS", "").split(",")
        self.beta_users = {self.owner_id} | {int(value.strip()) for value in beta if value.strip()}
        self.ledger.restart_pause()
        self.ai_lock = asyncio.Lock()
        self.ai_task = None
        self.sniper_symbols = [symbol for symbol in ("WIF",) if symbol in self.watchlist]
        self.watchlist = {symbol: dict(info) for symbol, info in self.watchlist.items()}
        self.main_symbols = [s for s in self.watchlist if s not in self.sniper_symbols]
        self.discovery = MemeDiscovery(self.ledger, self.rpc,
            [self.watchlist[s]["mint"] for s in self.main_symbols],
            {s: self.watchlist[s] for s in self.sniper_symbols})
        self.discovery_task = None
        self.discovery_due = 0
        self.discovery_started = 0
        self.whale_analysis_status = {
            "state": "idle", "checked_at": None, "symbols": [],
            "message": "Waiting for a screened whale candidate.",
        }
        self.whale_stream = HeliusWhaleStream(
            os.getenv("HELIUS_API_KEY", ""), self.on_whale_signal)
        self.whale_stream_task = None
        self._jito_tip_accounts = []
        self._jito_tip_accounts_expiry = 0
        self._close_retry_after = {}
        self.history_seen = {}
        self.restore_catalog()

    def restore_catalog(self):
        for row in self.discovery.assets():
            metrics = json.loads(row["metrics"])
            self.watchlist[row["code"]] = {"mint": row["mint"], "decimals": row["decimals"],
                                           "label": metrics["symbol"]}
            self.history.setdefault(row["code"], [])

    def held_sniper_symbols(self):
        with self.ledger.db() as db:
            return [row[0] for row in db.execute(
                "SELECT DISTINCT symbol FROM custody_positions WHERE state='OPEN' AND funding_asset=?", (SOL,))]

    async def background_discovery(self):
        whale_mints = set()
        try:
            whale_mints = set(self.discovery.pending_whale_mints())
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=75)) as session:
                await self.discovery.scan(session)
                self.restore_catalog()
                accepted = self.discovery.report.get("candidates", [])
                whale_symbols = [row["code"] for row in accepted if
                                 row.get("mint") in whale_mints and isinstance(row.get("code"), str)]
                if whale_symbols:
                    await self.analyze_whale_candidates(session, whale_symbols)
                elif whale_mints:
                    self.whale_analysis_status = {
                        "state": "screened_out", "checked_at": int(time.time()), "symbols": [],
                        "message": "No whale-triggered mint passed the existing candidate screen; no AI call or order was made.",
                    }
        except Exception:
            self.discovery.fail()
            if whale_mints:
                self.whale_analysis_status = {
                    "state": "error", "checked_at": int(time.time()), "symbols": [],
                    "message": "Candidate screening failed; AI analysis was not run and no order was sent.",
                }
        finally:
            self.discovery_due = time.time() + (
                60 if self.discovery.pending_whale_mints() else 900)

    def on_whale_signal(self, event):
        """Queue confirmed whale-buy mints for screening; never place an order."""
        if not self.discovery.queue_whale_mint(event.get("mint")):
            return False
        now = time.time()
        self.whale_analysis_status = {
            "state": "queued", "checked_at": int(now), "symbols": [],
            "message": "Confirmed whale buy queued for candidate screening; no order is sent.",
        }
        next_scan = max(now, self.discovery_started + 60)
        if self.discovery_task and not self.discovery_task.done():
            next_scan = max(next_scan, now + 60)
        self.discovery_due = min(self.discovery_due or next_scan, next_scan)
        return True

    def poll_discovery(self):
        if self.discovery_task and self.discovery_task.done():
            self.discovery_task = None
        if self.discovery_task is None and time.time() >= self.discovery_due:
            self.discovery_started = time.time()
            self.discovery_task = asyncio.create_task(self.background_discovery())

    async def scan_candidates(self, request, session):
        self.admit(self.uid(request))
        if time.time()-self.discovery_started >= 60:
            self.discovery_due = 0
        self.poll_discovery()
        return web.json_response({"success": True, "message":
            "Candidate scan running. Refresh shortly. No trade is submitted." if self.discovery_task else
            "Recent scan available; manual refresh has a 60-second cooldown. No trade is submitted.",
                                  "discovery": self.discovery.report})

    async def current_prices(self, session, extra_symbols=()):
        prices = await self.prices_fn(session)
        if getattr(self, "discovery", None):
            try:
                prices.update(await self.discovery.prices(
                    session, [*self.held_sniper_symbols(), *extra_symbols]))
            except Exception:
                self.discovery.fail()
        return prices

    def ai_universes(self, whale_symbols=None):
        discovery = getattr(self, "discovery", None)
        candidates = discovery.candidates() if discovery else []
        current_codes = {row["code"] for row in candidates}
        preferred = [symbol for symbol in whale_symbols or ()
                     if symbol in current_codes or discovery and discovery.entry_allowed(symbol)]
        sniper_assets = list(dict.fromkeys(preferred + [row["code"] for row in candidates]))[:8]
        sniper_assets += sorted(self.held_sniper_symbols())[:32]
        return {"main": getattr(self, "main_symbols", [s for s in self.watchlist if s not in self.sniper_symbols]),
                "sniper": list(dict.fromkeys(sniper_assets)) if discovery else self.sniper_symbols}

    async def ai_snapshot(self, session, prices, whale_symbols=None):
        config = ai_decisions.configuration()
        if not config["configured"]:
            raise WalletError("Добавьте OPENAI_API_KEY в Environment на Render. AI-торговля пока не запущена.", 503)
        whale_symbols = list(dict.fromkeys(symbol for symbol in (whale_symbols or ())
                                           if isinstance(symbol, str)))[:8]
        whale_triggered = bool(whale_symbols)
        min_interval = WHALE_AI_COOLDOWN_SECONDS if whale_triggered else config["interval_seconds"]
        async with self.ai_lock:
            latest = self.ledger.ai_latest()
            if latest and time.time() - latest["created_at"] < min_interval:
                if whale_triggered:
                    raise WalletError("Whale-triggered AI analysis is waiting for its 60-second request limit.", 429)
                record = json.loads(latest["decisions"])
                ai_decisions.validate_decisions(record["decisions"], record.get("universes", self.ai_universes()))
                return {**latest, **record}
            universes = self.ai_universes(whale_symbols=whale_symbols)
            wanted = set(universes["main"] + universes["sniper"]) | {"SOL"}
            market = {symbol: ((self.history.get(symbol) if
                      time.time()-getattr(self, "history_seen", {}).get(symbol, 0) <= 90 else None)
                      or [prices[symbol]])[-30:]
                      for symbol in wanted if prices.get(symbol, 0) > 0}
            screening_rows = {}
            if getattr(self, "discovery", None):
                for row in [*self.discovery.candidates(),
                            *(self.discovery.report.get("candidates", []) or [])]:
                    if isinstance(row, dict) and row.get("code") in universes["sniper"]:
                        screening_rows[row["code"]] = row
            screening = {symbol: {key: row[key] for key in (
                "liquidity_usd", "volume_24h_usd", "transactions_24h", "age_hours",
                "change_m5_pct", "change_h1_pct") if key in row}
                for symbol, row in screening_rows.items()}
            main_context = {}
            if self.market_context_fn and not whale_triggered:
                try:
                    raw_context = await self.market_context_fn(session)
                    if isinstance(raw_context, dict):
                        main_context = {
                            symbol: metrics for symbol, metrics in raw_context.items()
                            if symbol in universes["main"] and isinstance(metrics, dict)
                        }
                except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
                    main_context = {}
            if whale_triggered and any(symbol not in market for symbol in whale_symbols):
                raise WalletError("Whale candidate has no fresh market price; AI analysis was not sent.", 409)
            try:
                aid = self.ledger.ai_allow_call(min_interval, config["max_calls_per_day"])
                decisions = await ai_decisions.analyze(session, market, universes, screening, main_context)
            except LedgerError as exc:
                raise WalletError(str(exc), 429) from None
            except ai_decisions.AIError as exc:
                raise WalletError(str(exc), 503) from None
            except (aiohttp.ClientError, asyncio.TimeoutError):
                raise WalletError("Нет безопасного ответа AI API. Новые входы остановлены.", 503) from None
            record = {"decisions": decisions, "universes": universes, "prices": {s: h[-1] for s, h in market.items()},
                      "samples": {s: len(h) for s, h in market.items()},
                      "main_market_context": ai_decisions.sanitize_main_context(main_context, universes["main"]),
                      "trigger": "whale_signal" if whale_triggered else "scheduled",
                      "whale_symbols": whale_symbols if whale_triggered else []}
            self.ledger.ai_record(aid, record)
            return {"id": aid, "created_at": int(time.time()), **record}

    async def analyze_whale_candidates(self, session, symbols):
        symbols = list(dict.fromkeys(symbol for symbol in symbols if isinstance(symbol, str)))[:8]
        self.whale_analysis_status = {
            "state": "running", "checked_at": int(time.time()), "symbols": symbols,
            "message": "A confirmed whale buy passed the candidate screen; AI analysis is running. No order is sent by the signal.",
        }
        try:
            prices = await self.current_prices(session, extra_symbols=symbols)
            eligible = [symbol for symbol in symbols
                        if self.discovery.entry_allowed(symbol)
                        and isinstance(prices.get(symbol), (int, float))
                        and math.isfinite(prices[symbol]) and prices[symbol] > 0]
            if not eligible:
                raise WalletError("No whale candidate has a fresh screen and market price; no AI call was made.", 409)
            snapshot = await self.ai_snapshot(session, prices, whale_symbols=eligible)
        except (WalletError, LedgerError) as exc:
            message = str(exc)
            limited = "limit" in message.lower() or "interval" in message.lower()
            self.whale_analysis_status = {
                "state": "limited" if limited else "error",
                "checked_at": int(time.time()), "symbols": symbols, "message": message,
            }
            return None
        except Exception:
            self.whale_analysis_status = {
                "state": "error", "checked_at": int(time.time()), "symbols": symbols,
                "message": "Whale-triggered AI analysis failed safely; no signal submitted an order.",
            }
            return None
        self.whale_analysis_status = {
            "state": "complete", "checked_at": snapshot["created_at"], "symbols": eligible,
            "message": "Fresh AI analysis completed after screening; no order was sent by the signal.",
        }
        return snapshot

    def ai_status(self):
        latest = self.ledger.ai_latest()
        latest_record = json.loads(latest["decisions"]) if latest else {}
        return {**ai_decisions.configuration(), "last_analyzed_at": latest["created_at"] if latest else None,
                "last_decisions": latest_record.get("decisions") if latest else None,
                "chart_context": latest_record.get("main_market_context", {}) if latest else {},
                "last_trigger": latest_record.get("trigger", "scheduled") if latest else None,
                "sniper_assets": self.ai_universes()["sniper"],
                "asset_labels": {s: info.get("label", s) + (" · "+info["mint"][:4]+"…"+info["mint"][-4:]
                                 if s.startswith("M_") else "") for s, info in self.watchlist.items()},
                "discovery": self.discovery.report if getattr(self, "discovery", None) else None,
                "whale_analysis": getattr(self, "whale_analysis_status", {"state": "idle"}),
                "whale_stream": self.whale_stream.snapshot() if getattr(self, "whale_stream", None) else None}

    async def ai_preview(self, request, session):
        uid = self.uid(request)
        self.admit(uid)
        prices = await self.current_prices(session)
        snapshot = await self.ai_snapshot(session, prices)
        return web.json_response({"success": True, "message": "AI ответил. Анализ не подписывает и не отправляет сделки.",
            "decisions": snapshot["decisions"], "created_at": snapshot["created_at"]})

    async def background_ai(self, prices):
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=35)) as session:
            return await self.ai_snapshot(session, prices)

    def poll_ai(self, prices):
        """Never block position risk management on a model HTTP request."""
        config = ai_decisions.configuration()
        if not config["configured"]:
            if self.ai_task:
                self.ai_task.cancel()
                self.ai_task = None
            self.last_error = "AI не подключён: нужен OPENAI_API_KEY на Render."
            self.ai_activity_error = "AI is not configured."
            return None
        if self.ai_task and self.ai_task.done():
            try:
                self.ai_task.result()
                self.ai_activity_error = ""
            except Exception as exc:
                self.last_error = str(exc) if isinstance(exc, (WalletError, ai_decisions.AIError, LedgerError)) else "AI анализ недоступен."
                self.ai_activity_error = ("Last AI refresh failed. Displayed decisions may be from an earlier "
                                          "analysis; inspect their timestamps.")
            self.ai_task = None
        latest = self.ledger.ai_latest()
        if latest and time.time() - latest["created_at"] < config["interval_seconds"]:
            return {**latest, **json.loads(latest["decisions"])}
        if self.ai_task is None:
            self.ai_task = asyncio.create_task(self.background_ai(dict(prices)))
            self.ai_task.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)
        return None

    def owner(self, request):
        user = request["telegram_user"]
        self.ledger.user(int(user["id"]), user.get("username", user.get("first_name", "")))

    def uid(self, request):
        return int(request["telegram_user"]["id"])

    def admit(self, uid):
        if not self.public_enabled and uid not in self.beta_users:
            raise WalletError("Публичный приём средств ещё не открыт.", 503)
        self.ready()

    def readiness(self):
        return {**super().readiness(), "public_custody_enabled": self.public_enabled,
                "legacy_operation_pending": bool(self.legacy_pending())}

    def legacy_pending(self):
        with self.ledger.db() as db:
            exists = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='wallet_operations'").fetchone()
            return [dict(row) for row in db.execute(
                "SELECT * FROM wallet_operations WHERE state IN ('PREPARED','SUBMITTED','UNKNOWN')")] if exists else []

    def ready(self):
        signer = super().ready()
        # Do not ignore an ambiguous legacy payment during a migration.
        with self.ledger.db() as db:
            exists = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='wallet_operations'").fetchone()
            if exists and db.execute("SELECT id FROM wallet_operations WHERE state IN ('PREPARED','SUBMITTED','UNKNOWN') LIMIT 1").fetchone():
                raise WalletError("Старая операция ещё не подтверждена. Wallet → «Проверить старые операции» покажет её подпись и результат проверки. Повторно отправлять деньги не нужно.", 409)
        return signer

    async def handle(self, request, action):
        try:
            self.owner(request)
            if (getattr(self, "web_access", None) and request.method == "POST"
                    and action.__name__ in {"ai_preview", "scan_candidates", "trading_readiness", "save_trading_settings"}):
                self.web_access.trading_request(request)
            if (getattr(self, "accounts", None) and request.method == "POST" and action.__name__ in
                    {"create_invoice","deposit","wallet_link","toggle","toggle_sniper","withdraw"}):
                self.accounts.require(self.uid(request))
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=35)) as session:
                if action.__name__ in {"create_invoice", "deposit", "toggle", "withdraw", "reserve_fees", "opening_balance"} and self.legacy_pending():
                    # Re-check finalized results BEFORE ready()/admit() rejects the request.
                    # Unknown results remain blocked; never resend or guess failure.
                    await self.reconcile(session)
                return await action(request, session)
        except (WalletError, LedgerError) as exc:
            return web.json_response({"success": False, "message": str(exc)},
                                     status=exc.status if isinstance(exc, WalletError) else 409)
        except (ValueError, KeyError, TypeError, json.JSONDecodeError):
            return web.json_response({"success": False, "message": "Неверный запрос или неполный ответ провайдера."}, status=400)
        except Exception as exc:
            self.log("Custody: " + type(exc).__name__)
            return web.json_response({"success": False, "message": "Результат неизвестен. Проверьте свою историю, не повторяйте перевод."}, status=503)

    def pending(self):
        return self.ledger.pending()

    async def balances(self, session):
        native = (await self.rpc(session, "getBalance", [self.wallet, {"commitment": "finalized"}]))["value"]
        # Backing is a finalized snapshot, never a quote or a user-provided number.
        tokens = (await self.rpc(session, "getTokenAccountsByOwner", [
            self.wallet, {"programId": str(TOKEN)}, {"encoding": "jsonParsed", "commitment": "finalized"}]))["value"]
        balances = {SOL: {"raw": native, "decimals": 9}, USDC: {"raw": 0, "decimals": 6}}
        for account in tokens:
            info = account["account"]["data"]["parsed"]["info"]
            if info.get("owner") == self.wallet and info.get("state") == "initialized":
                mint = info["mint"]
                asset = asset_for_mint(mint)
                entry = balances.setdefault(asset, {"raw": 0, "decimals": info["tokenAmount"]["decimals"]})
                entry["raw"] += int(info["tokenAmount"]["amount"])
        # The parent transaction builder expects the wrapped mint key for native SOL.
        balances[WRAPPED_SOL] = balances[SOL]
        return balances

    async def solvent(self, session):
        if await self.rpc(session, "getGenesisHash") != GENESIS:
            raise WalletError("Операции разрешены только в Solana mainnet.")
        self.ledger.audit()
        backing = await self.balances(session)
        for asset, liability in self.ledger.liabilities().items():
            if liability > backing.get(asset, {}).get("raw", 0):
                raise WalletError(
                    "Withdrawals and trading are paused because wallet funds do not cover recorded account balances. "
                    "No transfer was sent.")
        return backing

    @staticmethod
    def wrapped_delta(tx, wallet):
        def total(side):
            return sum(int(entry["uiTokenAmount"]["amount"]) for entry in tx["meta"].get(side, []) or []
                       if entry.get("owner") == wallet and entry.get("mint") == WRAPPED_SOL)
        return total("postTokenBalances") - total("preTokenBalances")

    @staticmethod
    def effects(tx, wallet, asset):
        if asset != SOL:
            return WalletRuntime.delta(tx, wallet, asset)
        meta = tx["meta"]
        keys = tx["transaction"]["message"]["accountKeys"]
        addresses = [key.get("pubkey") if isinstance(key, dict) else key for key in keys]
        if wallet not in addresses:
            return 0
        wallet_index = addresses.index(wallet)
        raw = meta["postBalances"][wallet_index] - meta["preBalances"][wallet_index]
        raw += meta["fee"] if wallet_index == 0 else 0
        token_indices = set()
        for side in ("preTokenBalances", "postTokenBalances"):
            for entry in meta.get(side, []) or []:
                if entry.get("owner") == wallet:
                    token_indices.add(entry["accountIndex"])
        # Include changes to our owned token-account lamports (rent and wrapped SOL).
        # This excludes returned rent/old WSOL from a customer's acquired position.
        for index in token_indices:
            raw += meta["postBalances"][index] - meta["preBalances"][index]
        return raw

    async def apply_deposit(self, session, signature, expected_iid=None):
        from solders.signature import Signature
        Signature.from_string(signature)
        if await self.rpc(session, "getGenesisHash") != GENESIS:
            raise WalletError("Пополнение разрешено только в Solana mainnet.")
        tx = await self.rpc(session, "getTransaction", [signature, {
            "encoding": "jsonParsed", "commitment": "finalized", "maxSupportedTransactionVersion": 0}])
        if not tx or not tx.get("meta") or tx["meta"].get("err") is not None:
            raise WalletError("Платёж ещё не финализирован или отклонён сетью.", 409)
        iid = memo_invoice(tx)
        if expected_iid is not None and iid != expected_iid:
            raise WalletError("Подпись относится к другому платёжному запросу.", 403)
        invoice = self.ledger.get_invoice(iid)
        if not invoice:
            raise WalletError("Неизвестный Memo. Автоматическое зачисление невозможно.", 409)
        if not self.public_enabled and invoice["uid"] not in self.beta_users:
            raise WalletError("Публичный приём средств закрыт.", 503)
        keys = tx["transaction"]["message"]["accountKeys"]
        if any(isinstance(key, dict) and key.get("pubkey") == self.wallet and key.get("signer") for key in keys):
            raise WalletError("Исходящая операция сервера не является депозитом.", 409)
        asset = invoice["asset"]
        if asset == SOL:
            addresses = [k.get("pubkey") if isinstance(k, dict) else k for k in keys]
            if self.wallet not in addresses:
                raise WalletError("Нет перевода SOL на общий адрес.", 409)
            index = addresses.index(self.wallet)
            # Token-account rent is not a native SOL deposit to the shared address.
            delta = tx["meta"]["postBalances"][index] - tx["meta"]["preBalances"][index]
        else:
            delta = self.effects(tx, self.wallet, asset)
        if delta != invoice["raw"]:
            raise WalletError("Сумма или актив не соответствует платёжному запросу.", 409)
        # Only the memo's persisted owner gets credit. The submitting user is irrelevant.
        return self.ledger.credit_deposit(iid, signature, asset, delta, int(tx.get("blockTime") or 0))

    def public_invoice(self, invoice):
        token = "USDC" if invoice["asset"] == USDC else "SOL"
        return {"invoice_id": invoice["id"], "deprecated": True,
            "token": token, "amount": display_raw(invoice["raw"], 6 if token == "USDC" else 9),
            "deposit_wallet": self.wallet, "expires_at": invoice["expires_at"],
            "state": invoice["state"], "payment_url": self.base_url + "/deposit/" + invoice["id"]}

    async def create_invoice(self, request, session):
        uid = self.uid(request)
        self.admit(uid)
        raise WalletError("Платёжные запросы отключены. Используйте обычный перевод на общий адрес и проверку Tx.", 410)

    async def deposit(self, request, session):
        uid = self.uid(request)
        self.admit(uid)
        body = await request.json()
        signature = str(body.get("tx_signature", "")).strip()
        if signature.startswith(("https://solscan.io/tx/", "https://explorer.solana.com/tx/")):
            signature = signature.split("/tx/", 1)[1].split("?", 1)[0].split("#", 1)[0].rstrip("/")
        try:
            Signature.from_string(signature)
        except ValueError:
            raise WalletError("Вставьте полную подпись Tx или ссылку на неё в Solscan — не адрес кошелька.", 400)
        async with self.lock:
            if await self.rpc(session, "getGenesisHash") != GENESIS:
                raise WalletError("Нужна Solana mainnet.", 409)
            tx = await self.rpc(session, "getTransaction", [signature, {
                "encoding": "jsonParsed", "commitment": "finalized", "maxSupportedTransactionVersion": 0}])
            if not tx or not tx.get("meta") or "err" not in tx["meta"] or tx["meta"]["err"] is not None:
                raise WalletError("Tx ещё не финализирована или отклонена. Повторять перевод не нужно.", 409)
            # Old already-issued invoices retain their original owner and deduplication.
            try:
                old_iid = memo_invoice(tx)
            except WalletError:
                old_iid = None
                if any(ix.get("programId") == str(MEMO) and isinstance(ix.get("parsed"), str)
                       and ix["parsed"].startswith("ZL1:")
                       for ix in tx["transaction"]["message"]["instructions"]):
                    raise WalletError("Некорректная старая платёжная метка. Нельзя зачислить её другому счёту.", 409)
            if old_iid:
                old_invoice = self.ledger.get_invoice(old_iid)
                if not old_invoice or old_invoice["uid"] != uid:
                    raise WalletError("Этот старый запрос принадлежит другому аккаунту.", 403)
                await self.apply_deposit(session, signature, old_iid)
            else:
                sender = self.ledger.wallet_address(uid)
                if not sender:
                    raise WalletError("Один раз подтвердите кошелёк кнопкой выше. После этого пополнения проверяются только по Tx.", 403)
                amounts = incoming_amounts(tx, self.wallet, sender)
                balances = await self.solvent(session)
                self.ledger.credit_tx(uid, signature, sender, amounts, int(tx.get("blockTime") or 0),
                                      {asset: data["raw"] for asset, data in balances.items()})
        return web.json_response({"success": True, "message": "Tx проверена. Поступление зачислено только вашему аккаунту."})

    async def wallet_link(self, request, session):
        uid = self.uid(request)
        self.admit(uid)
        if uid != self.owner_id:
            with self.ledger.db() as db:
                fees = db.execute("SELECT raw FROM custody_accounts WHERE account='treasury' AND asset=?", (SOL,)).fetchone()
                if not fees or fees["raw"] < FEE_CAP:
                    raise WalletError("Новые пополнения приостановлены: оператору нужно пополнить резерв комиссий.")
        address = self.ledger.wallet_address(uid)
        nonce = None if address else self.ledger.wallet_challenge(uid, self.base_url)
        return web.json_response({"success": True, "address": address,
            "url": self.base_url + "/wallet-link/" + nonce if nonce else None})

    async def wallet_link_status(self, request, session):
        proof = self.ledger.wallet_proof(request.match_info["nonce"])
        if not proof or proof["expires_at"] < int(time.time()):
            raise WalletError("Ссылка подтверждения истекла. Создайте новую в боте.", 409)
        return web.json_response({"success": True, "message": proof["message"],
            "linked": bool(proof["used"]), "address": self.ledger.wallet_address(proof["uid"])})

    async def wallet_link_confirm(self, request, session):
        proof = self.ledger.wallet_proof(request.match_info["nonce"])
        if not proof or proof["used"] or proof["expires_at"] < int(time.time()):
            raise WalletError("Ссылка уже использована или истекла.", 409)
        self.admit(proof["uid"])
        body = await request.json()
        address = Pubkey.from_string(str(body.get("address", "")))
        if not address.is_on_curve() or str(address) == self.wallet:
            raise WalletError("Подтвердите личный кошелёк, не общий серверный адрес.", 400)
        signature = Signature.from_bytes(base64.b64decode(body.get("signature", ""), validate=True))
        if not signature.verify(address, proof["message"].encode("utf-8")):
            raise WalletError("Подпись кошелька не подтверждена.", 403)
        self.ledger.link_wallet(proof["nonce"], str(address))
        return web.json_response({"success": True, "message": "Кошелёк подтверждён. Вернитесь в Telegram; дальше используйте обычный перевод и Tx."})

    async def deposit_transaction(self, request, session):
        iid = request.match_info["invoice"]
        invoice = self.ledger.get_invoice(iid)
        if not invoice or invoice["state"] != "OPEN" or invoice["expires_at"] < int(time.time()):
            raise WalletError("Платёжный запрос недоступен или истёк.", 409)
        self.admit(invoice["uid"])
        if await self.rpc(session, "getGenesisHash") != GENESIS:
            raise WalletError("Только Solana mainnet.")
        body = await request.json()
        sender = Pubkey.from_string(str(body.get("sender", "")))
        recipient = Pubkey.from_string(self.wallet)
        if not sender.is_on_curve() or sender == recipient:
            raise WalletError("Нужен отдельный кошелёк отправителя.", 400)
        instructions = []
        if invoice["asset"] == SOL:
            instructions.append(transfer(TransferParams(from_pubkey=sender, to_pubkey=recipient, lamports=invoice["raw"])))
        else:
            mint = Pubkey.from_string(USDC)
            source, dest = associated(sender, mint), associated(recipient, mint)
            accounts = (await self.rpc(session, "getMultipleAccounts", [
                [str(source), str(dest)], {"encoding": "jsonParsed", "commitment": "confirmed"}]))["value"]
            source_info = (accounts[0] or {}).get("data", {}).get("parsed", {}).get("info", {})
            value = source_info.get("tokenAmount", {})
            if (source_info.get("owner") != str(sender) or source_info.get("mint") != USDC
                    or source_info.get("state") != "initialized" or value.get("decimals") != 6
                    or int(value.get("amount", 0)) < invoice["raw"]):
                raise WalletError("Недостаточно USDC на ATA кошелька отправителя.", 409)
            if accounts[1] is None:
                instructions.append(Instruction(ATA, bytes([1]), [
                    AccountMeta(sender, True, True), AccountMeta(dest, False, True),
                    AccountMeta(recipient, False, False), AccountMeta(mint, False, False),
                    AccountMeta(SYSTEM, False, False), AccountMeta(TOKEN, False, False)]))
            else:
                dest_info = accounts[1].get("data", {}).get("parsed", {}).get("info", {})
                if dest_info.get("owner") != self.wallet or dest_info.get("mint") != USDC or dest_info.get("state") != "initialized":
                    raise WalletError("Серверный USDC ATA недоступен.")
            instructions.append(Instruction(TOKEN, bytes([12]) + invoice["raw"].to_bytes(8, "little") + bytes([6]), [
                AccountMeta(source, False, True), AccountMeta(mint, False, False),
                AccountMeta(dest, False, True), AccountMeta(sender, True, False)]))
        # Ordinary transfer only. Never add a Memo instruction.
        block = (await self.rpc(session, "getLatestBlockhash", [{"commitment": "confirmed"}]))["value"]
        message = Message.new_with_blockhash(instructions, sender, Hash.from_string(block["blockhash"]))
        transaction = Transaction.new_unsigned(message)
        return web.json_response({"success": True, **self.public_invoice(invoice),
            "transaction": base64.b64encode(bytes(transaction)).decode()})

    async def public_handle(self, request, action):
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
                return await action(request, session)
        except (WalletError, LedgerError) as exc:
            return web.json_response({"success": False, "message": str(exc)},
                status=exc.status if isinstance(exc, WalletError) else 409)
        except Exception:
            return web.json_response({"success": False, "message": "Платёж недоступен. Проверьте статус в боте."}, status=503)

    async def payment_status(self, request, session):
        invoice = self.ledger.get_invoice(request.match_info["invoice"])
        if not invoice:
            raise WalletError("Платёжный запрос не найден.", 404)
        return web.json_response({"success": True, **self.public_invoice(invoice)})

    async def payment_confirm(self, request, session):
        body = await request.json()
        await self.apply_deposit(session, str(body.get("signature", "")), request.match_info["invoice"])
        return await self.payment_status(request, session)

    async def payment_disabled(self, request, session):
        raise WalletError("Старые страницы оплаты отключены. Вернитесь в Wallet: обычный перевод и проверка Tx.", 410)

    async def reserve_fees(self, request, session):
        if self.uid(request) != self.owner_id:
            raise WalletError("Только администратор.", 403)
        self.ready()
        body = await request.json()
        if body.get("confirm_own_unassigned") is not True:
            raise WalletError("Подтвердите, что нераспределённые SOL принадлежат оператору, а не клиентам.", 400)
        raw = amount_raw(body.get("amount_sol", "0"), 9)
        async with self.lock:
            await self.reconcile(session)
            backing = await self.solvent(session)
            self.ledger.seed_fees(raw, backing[SOL]["raw"], self.owner_id)
        return web.json_response({"success": True, "message": "Ваши SOL выделены на комиссии оператора. Монеты не отправлялись; чужие остатки не менялись.",
                                 "operator_fees": self.operator_fees(self.owner_id)})

    async def release_fee_reserve(self, request, session):
        uid = self.uid(request)
        if uid != self.owner_id:
            raise WalletError("Только администратор.", 403)
        self.ready()
        body = await request.json()
        if body.get("confirm_excess_only") is not True or body.get("confirm_own_funds") is not True:
            raise WalletError("Подтвердите возврат только ваших средств сверх минимального резерва.", 400)
        expected_raw = amount_raw(body.get("expected_release_sol", "0"), 9)
        async with self.lock:
            await self.solvent(session)
            released_raw = self.ledger.release_excess_fees(uid, expected_raw)
        released = display_raw(released_raw, 9)
        return web.json_response({
            "success": True,
            "released_sol": released,
            "message": f"{released} SOL возвращены в ваш доступный баланс. Перевод в Solana не отправлялся.",
            "operator_fees": self.operator_fees(uid),
        })

    def operator_fees(self, uid):
        if uid != self.owner_id:
            return None
        with self.ledger.db() as db:
            row = db.execute("SELECT raw FROM custody_accounts WHERE account='treasury' AND asset=?", (SOL,)).fetchone()
        raw = row["raw"] if row else 0
        return {"reserve_sol": display_raw(raw, 9), "minimum_sol": display_raw(FEE_CAP, 9),
                "releasable_sol": display_raw(max(0, raw - FEE_CAP), 9),
                "own_available_sol": display_raw(self.ledger.balance(uid, SOL), 9),
                "sufficient": raw >= FEE_CAP}

    async def opening_balance(self, request, session):
        uid = self.uid(request)
        if uid != self.owner_id or self.public_enabled:
            raise WalletError("Перенос старого личного остатка разрешён только владельцу до публичного открытия.", 403)
        self.ready()
        body = await request.json()
        if body.get("confirm_own_unassigned") is not True:
            raise WalletError("Нужно явно подтвердить отсутствие чужих средств в переносимой сумме.", 400)
        token = str(body.get("token", "")).upper()
        if token not in ("USDC", "SOL"):
            raise WalletError("Выберите USDC или SOL.", 400)
        asset = USDC if token == "USDC" else SOL
        raw = amount_raw(body.get("amount", 0), 6 if token == "USDC" else 9)
        async with self.lock:
            await self.reconcile(session)
            backing = await self.solvent(session)
            self.ledger.opening(uid, asset, raw, backing[asset]["raw"])
        return web.json_response({"success": True, "message": "Подтверждённый личный остаток перенесён в ваш счёт. Старые виртуальные балансы не использовались."})

    async def preview_legacy_balances(self, request, session):
        if self.uid(request) != self.owner_id:
            raise WalletError("Administrator only.", 403)
        self.ready()
        async with self.lock:
            if self.legacy_pending():
                raise WalletError("A legacy wallet operation is still pending; no balances were changed.", 409)
            if self.pending():
                raise WalletError("Finish checking the current operation first; no legacy balances were changed.", 409)
            backing = await self.solvent(session)
            result = self.ledger.preview_legacy_balances(backing[USDC]["raw"])
        result.pop("_eligible", None)
        result["candidate_usdc"] = display_raw(result.pop("candidate_raw"), 6)
        result["eligible_usdc"] = display_raw(result.pop("amount_raw"), 6)
        result["backing_available_usdc"] = display_raw(result.pop("backing_available_raw"), 6)
        return web.json_response({"success": True, **result})

    async def import_legacy_balances(self, request, session):
        if self.uid(request) != self.owner_id:
            raise WalletError("Administrator only.", 403)
        self.ready()
        body = await request.json()
        if body.get("confirm") is not True:
            raise WalletError("Explicitly confirm the exact preview report.", 400)
        digest = body.get("preview_digest")
        async with self.lock:
            if self.legacy_pending():
                raise WalletError("A legacy wallet operation is still pending; no balances were changed.", 409)
            if self.pending():
                raise WalletError("Finish checking the current operation first; no legacy balances were changed.", 409)
            backing = await self.solvent(session)
            result = self.ledger.import_legacy_balances(digest, backing[USDC]["raw"])
        return web.json_response({
            "success": True,
            "message": "Confirmed legacy balances were imported into the ledger. No Solana transactions were sent.",
            "imported_count": result["imported_count"],
            "amount_usdc": display_raw(result["amount_raw"], 6),
        })

    async def admin_status(self, request, session):
        if self.uid(request) != self.owner_id:
            raise WalletError("Только администратор.", 403)
        payload = {"success": True, "legacy_check_error": None, "balance_check_error": None}
        try:
            await self.reconcile(session)
        except Exception as exc:
            payload["legacy_check_error"] = str(exc) if isinstance(exc, WalletError) else "Не удалось проверить старую операцию: " + type(exc).__name__
        payload["legacy_operations"] = [{key: row.get(key) for key in ("id", "signature", "kind", "state", "timestamp")}
                                        for row in self.legacy_pending()]
        with self.ledger.db() as db:
            exists = db.execute("SELECT name FROM sqlite_master WHERE name='wallet_operations'").fetchone()
            payload["retired_legacy_operations"] = [dict(row) for row in db.execute(
                "SELECT signature,kind,state,timestamp FROM wallet_operations WHERE state='EXPIRED_UNCONFIRMED'")] if exists else []
        try:
            backing = await self.solvent(session)
        except Exception as exc:
            payload["balance_check_error"] = str(exc) if isinstance(exc, (WalletError, LedgerError)) else "Не удалось проверить покрытие: " + type(exc).__name__
            return web.json_response(payload)
        liabilities = self.ledger.liabilities()
        with self.ledger.db() as db:
            fee = db.execute("SELECT raw FROM custody_accounts WHERE account='treasury' AND asset=?", (SOL,)).fetchone()
        return web.json_response({**payload,
            "unassigned_usdc": display_raw(backing[USDC]["raw"] - liabilities.get(USDC, 0), 6),
            "unassigned_sol": display_raw(backing[SOL]["raw"] - liabilities.get(SOL, 0), 9),
            "fee_reserve_sol": display_raw(fee["raw"] if fee else 0, 9)})

    async def admin_pending_operations(self, request, session):
        if self.uid(request) != self.owner_id:
            raise WalletError("Только администратор.", 403)
        operations = sorted(self.ledger.pending(),
                            key=lambda row: int(row.get("created_at") or 0),
                            reverse=True)[:5]
        return web.json_response({"success": True, "operations": [
            {key: row.get(key) for key in ("kind", "state", "created_at", "signature")}
            for row in operations
        ]})

    async def admin_balance_check(self, request, session):
        if self.uid(request) != self.owner_id:
            raise WalletError("Только администратор.", 403)
        self.ledger.audit()
        backing = await self.balances(session)
        liabilities = self.ledger.liabilities()
        assets = {}
        for asset, decimals in ((USDC, 6), (SOL, 9)):
            actual = int(backing.get(asset, {}).get("raw", 0))
            owed = int(liabilities.get(asset, 0))
            assets["USDC" if asset == USDC else "SOL"] = {
                "wallet": display_raw(actual, decimals),
                "ledger": display_raw(owed, decimals),
                "difference": display_raw(actual - owed, decimals),
            }
        return web.json_response({"success": True, "assets": assets, "read_only": True})

    async def admin_record_external_withdrawal(self, request, session):
        uid = self.uid(request)
        if uid != self.owner_id:
            raise WalletError("Только администратор.", 403)
        body = await request.json()
        if body.get("confirm") is not True:
            raise WalletError("Confirm this already completed transfer before recording it.", 400)
        signature = str(body.get("signature", "")).strip()
        try:
            Signature.from_string(signature)
            recipient = str(Pubkey.from_string(str(body.get("recipient_address", "")).strip()))
        except Exception as exc:
            raise WalletError("Enter a valid Solana transaction signature and recipient address.", 400) from exc
        if recipient == self.wallet:
            raise WalletError("The recipient must be different from the shared custody wallet.", 400)
        if await self.rpc(session, "getGenesisHash") != GENESIS:
            raise WalletError("External payout verification is available only on Solana mainnet.", 503)
        statuses = await self.rpc(session, "getSignatureStatuses",
                                  [[signature], {"searchTransactionHistory": True}])
        status = (statuses.get("value") or [None])[0]
        if not status or status.get("confirmationStatus") != "finalized" or status.get("err") is not None:
            raise WalletError("The transaction is not finalized successfully. No ledger entry was made.", 409)
        tx = await self.rpc(session, "getTransaction", [signature, {
            "encoding": "jsonParsed", "commitment": "finalized", "maxSupportedTransactionVersion": 0}])
        if not tx or not tx.get("meta") or tx["meta"].get("err") is not None:
            raise WalletError("Could not verify the finalized transaction. No ledger entry was made.", 409)
        meta = tx["meta"]
        message = tx.get("transaction", {}).get("message", {})
        keys = [key.get("pubkey") if isinstance(key, dict) else key
                for key in message.get("accountKeys", [])]
        if not keys or keys[0] != self.wallet:
            raise WalletError("The shared custody wallet was not the transaction fee payer. No ledger entry was made.", 409)
        token_accounts = {}
        for side in ("preTokenBalances", "postTokenBalances"):
            for entry in meta.get(side, []) or []:
                index = entry.get("accountIndex")
                if isinstance(index, int) and 0 <= index < len(keys):
                    token_accounts[(keys[index], side)] = entry
        instructions = list(message.get("instructions", []) or [])
        for group in meta.get("innerInstructions", []) or []:
            instructions.extend(group.get("instructions", []) or [])
        matching = []
        outgoing = []
        for instruction in instructions:
            parsed = instruction.get("parsed") if isinstance(instruction, dict) else None
            info = parsed.get("info", {}) if isinstance(parsed, dict) else {}
            amount_info = info.get("tokenAmount", {}) if isinstance(info, dict) else {}
            if not isinstance(parsed, dict) or parsed.get("type") not in ("transfer", "transferChecked"):
                continue
            source = info.get("source")
            destination = info.get("destination")
            before = token_accounts.get((source, "preTokenBalances")) if source else None
            after = token_accounts.get((source, "postTokenBalances")) if source else None
            if before and before.get("owner") == self.wallet and before.get("mint") == USDC:
                outgoing.append((source, destination))
            if (parsed.get("type") != "transferChecked" or info.get("mint") != USDC
                    or not source or not destination or amount_info.get("decimals") != 6):
                continue
            recipient_before = token_accounts.get((destination, "preTokenBalances"))
            recipient_after = token_accounts.get((destination, "postTokenBalances"))
            source_after = token_accounts.get((source, "postTokenBalances"))
            if not before or not source_after or not recipient_after:
                continue
            if (before.get("owner") != self.wallet or source_after.get("owner") != self.wallet
                    or recipient_after.get("owner") != recipient):
                continue
            if (before.get("mint") != USDC or source_after.get("mint") != USDC
                    or recipient_after.get("mint") != USDC):
                continue
            if recipient_before and (recipient_before.get("owner") != recipient
                                     or recipient_before.get("mint") != USDC):
                continue
            try:
                raw = int(amount_info["amount"])
                source_delta = (int(before["uiTokenAmount"]["amount"])
                                - int(source_after["uiTokenAmount"]["amount"]))
                destination_delta = (int(recipient_after["uiTokenAmount"]["amount"])
                                     - int(recipient_before["uiTokenAmount"]["amount"])
                                     if recipient_before else raw)
            except (KeyError, TypeError, ValueError):
                continue
            if raw > 0 and source_delta == raw and destination_delta == raw:
                matching.append(raw)
        if len(outgoing) != 1 or len(matching) != 1:
            raise WalletError(
                "The transaction must contain exactly one finalized USDC payout from the shared wallet "
                "to the stated recipient, with no other outgoing USDC transfer. No ledger entry was made.", 409)
        try:
            fee_lamports = int(meta.get("fee", 0))
        except (TypeError, ValueError) as exc:
            raise WalletError("The transaction fee could not be verified. No ledger entry was made.", 409) from exc
        if fee_lamports > FEE_CAP:
            raise WalletError("The transaction fee exceeds the operator reserve cap. No ledger entry was made.", 409)
        block_time = tx.get("blockTime")
        if not isinstance(block_time, int):
            raise WalletError("The transaction has no verified block time. No ledger entry was made.", 409)
        async with self.lock:
            self.ledger.audit()
            operation, created = self.ledger.record_external_withdrawal(
                uid, signature, USDC, matching[0], recipient, fee_lamports, block_time)
            self.ledger.audit()
        return web.json_response({
            "success": True, "recorded": created, "state": operation["state"],
            "asset": "USDC", "amount": display_raw(matching[0], 6),
            "recipient_address": recipient, "tx_signature": signature,
            "available_usdc": display_raw(self.ledger.balance(uid, USDC), 6),
            "message": "Recorded an already finalized transfer. No new Solana transaction was sent."
            if created else "This transfer was already recorded. No duplicate ledger entry was added."
        })

    async def recover_expired_operation(self, request, session):
        uid = self.uid(request)
        body = await request.json()
        signature = str(body.get("signature", "")).strip()
        if body.get("confirm_expired") is not True:
            raise WalletError("Confirm the read-only expiry check.", 400)
        if not signature:
            raise WalletError("Pending operation signature is missing.", 400)
        WalletRuntime.ready(self)
        async with self.lock:
            row = next((item for item in self.ledger.pending()
                        if item.get("signature") == signature and int(item["uid"]) == uid), None)
            if not row or row["kind"] not in ("BUY", "SELL", "WITHDRAW", "PARTNER_PAYOUT") or row["state"] not in ("PREPARED", "SUBMITTED", "UNKNOWN"):
                raise WalletError("No matching pending operation belongs to this account.", 409)
            try:
                wire = base64.b64decode(row.get("wire") or "", validate=True)
                signed_tx = VersionedTransaction.from_bytes(wire)
                account_keys = signed_tx.message.account_keys
                if (not signed_tx.signatures or str(signed_tx.signatures[0]) != signature
                        or not account_keys or str(account_keys[0]) != self.wallet
                        or signed_tx.message.header.num_required_signatures != 1):
                    raise ValueError("signature mismatch")
                recent_blockhash = str(signed_tx.message.recent_blockhash)
            except Exception:
                raise WalletError("The signed operation cannot be safely verified; no record was changed.") from None
            proof_row = {
                "signature": signature,
                "timestamp": datetime.fromtimestamp(int(row["created_at"]), timezone.utc).isoformat(),
                "recent_blockhash": recent_blockhash,
                "signed_wire": row["wire"],
            }
            try:
                async with asyncio.timeout(45):
                    async def read_rpc(method, params):
                        return await self.recovery_rpc(session, method, params)
                    evidence = await prove_retirement(read_rpc, self.wallet, proof_row)
            except (asyncio.TimeoutError, aiohttp.ClientError):
                raise WalletError("Recovery check was interrupted; no record was changed.", 503) from None
            evidence = {**evidence, "outcome": "PROVEN_ABSENT_AND_EXPIRED",
                        "ledger_release": True, "transaction_sent": False}
            # The ledger atomically pauses both modes and queues an alert on recovery.
            try:
                self.ledger.fail(row["id"], network_proven=True, fee=0,
                    failure_reason="Recovery verified that the transaction was absent and expired.",
                    recovery_audit={"actor_id": uid, "evidence": evidence})
            except LedgerError as exc:
                raise WalletError(str(exc), 409) from None
            current = self.ledger.operation(row["id"])
            with self.ledger.db() as db:
                audited = db.execute(
                    "SELECT signature FROM custody_operation_recovery_audit WHERE signature=?",
                    (signature,)).fetchone()
            if not current or current["state"] != "FAILED" or not audited:
                raise WalletError("The operation changed during verification; refresh status before trying again.", 409)
            if row["kind"] == "BUY":
                recovery = "the purchase amount is available again"
            elif row["kind"] == "SELL":
                recovery = "the asset was restored to your open position"
            elif row["kind"] == "WITHDRAW":
                recovery = "the withdrawal amount is available again"
            else:
                recovery = "the affiliate commission was returned to its pending payout queue"
        return web.json_response({"success": True, "state": "FAILED",
            "message": f"Solana history proved this {row['kind']} absent and expired; {recovery}. The audit record is retained. No transaction was sent or resent by this recovery check. Both trading modes are OFF; enable them only when ready."})

    async def recovery_rpc(self, session, method, params):
        # Fixed, independently verified mainnet endpoint; keys/credentials never sent.
        async with session.post(RECOVERY_RPC, json={"jsonrpc": "2.0", "id": 1,
                "method": method, "params": params}, timeout=aiohttp.ClientTimeout(total=8)) as response:
            if response.status != 200:
                raise WalletError("Recovery RPC is unavailable; no record was changed.")
            result = await response.json()
        if "error" in result or "result" not in result:
            raise WalletError("RPC did not provide recovery evidence; no record was changed.")
        return result["result"]

    async def retire_legacy(self, request, session):
        if self.uid(request) != self.owner_id or self.public_enabled:
            raise WalletError("Восстановление старой личной записи разрешено только владельцу в закрытом режиме.", 403)
        super().ready()
        body = await request.json()
        if body.get("confirm_archive_unknown") is not True:
            raise WalletError("Нужно подтвердить архивирование без возврата денег и без признания сделки успешной.", 400)
        valid, _ = self.pin_fn(self.owner_id, str(body.get("pin", "")))
        if not valid:
            raise WalletError("Неверный PIN или PIN временно заблокирован.", 403)
        row = next((r for r in self.legacy_pending() if r["signature"] == body.get("signature")), None)
        if not row or row["kind"] not in ("BUY", "SELL", "WITHDRAW"):
            raise WalletError("Эта старая операция не находится в ожидании.", 409)
        async with self.lock:
            async with asyncio.timeout(30):
                async def read_rpc(method, params):
                    return await self.recovery_rpc(session, method, params)
                evidence = await prove_retirement(read_rpc, self.wallet, row)
            # No credits, refunds, position changes or network sends. Preserve signature.
            with self.ledger.db() as db:
                db.execute("""CREATE TABLE IF NOT EXISTS legacy_retirement_audit(
                    signature TEXT PRIMARY KEY, actor_id INTEGER NOT NULL,
                    state_before TEXT NOT NULL, evidence TEXT NOT NULL,
                    timestamp TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
                db.execute("""CREATE TRIGGER IF NOT EXISTS immutable_retirement_update
                    BEFORE UPDATE ON legacy_retirement_audit
                    BEGIN SELECT RAISE(ABORT,'immutable recovery audit'); END""")
                db.execute("""CREATE TRIGGER IF NOT EXISTS immutable_retirement_delete
                    BEFORE DELETE ON legacy_retirement_audit
                    BEGIN SELECT RAISE(ABORT,'immutable recovery audit'); END""")
                changed = db.execute("""UPDATE wallet_operations SET state='EXPIRED_UNCONFIRMED'
                    WHERE id=? AND signature=? AND state IN ('PREPARED','SUBMITTED','UNKNOWN')""",
                    (row["id"], row["signature"])).rowcount
                if not changed:
                    raise WalletError("Статус уже изменился. Обновите проверку.", 409)
                db.execute("INSERT INTO legacy_retirement_audit(signature,actor_id,state_before,evidence) VALUES(?,?,?,?)",
                           (row["signature"], self.owner_id, row["state"], json.dumps(evidence)))
        return web.json_response({"success": True, "state": "EXPIRED_UNCONFIRMED",
            "message": "Старая запись сохранена в архиве как неподтверждённая с истёкшим окном исполнения. Деньги не зачислялись и повторно не отправлялись. Используйте только фактические остатки кошелька."})

    async def reconcile(self, session):
        with self.ledger.db() as db:
            legacy_exists = db.execute("SELECT name FROM sqlite_master WHERE name='wallet_operations'").fetchone()
            legacy_pending = bool(legacy_exists and db.execute(
                "SELECT id FROM wallet_operations WHERE state IN ('PREPARED','SUBMITTED','UNKNOWN') LIMIT 1").fetchone())
        if (self.ledger.pending() or legacy_pending) and await self.rpc(session, "getGenesisHash") != GENESIS:
            raise WalletError("Подтверждение разрешено только в Solana mainnet.")
        if legacy_pending:
            # Reconcile the old signed operation; never send or restart old trading.
            async def finalized_rpc(client, method, params=None):
                answer = await self.rpc(client, method, params)
                if method == "getSignatureStatuses":
                    answer = {**answer, "value": [
                        s if s and s.get("confirmationStatus") == "finalized" else None for s in answer["value"]]}
                return answer
            self.legacy_runtime.rpc = finalized_rpc
            await self.legacy_runtime.reconcile(session)
        for row in self.ledger.pending():
            if row["state"] == "RESERVED":
                continue
            statuses = await self.rpc(session, "getSignatureStatuses", [[row["signature"]], {"searchTransactionHistory": True}])
            status = statuses["value"][0]
            if status is None:
                activity_log.record_jito_stage(self, row, "jito_signature_not_seen",
                    "No Solana signature status is visible yet; this is inconclusive. "
                    "The reservation remains locked; do not resend.")
                # A missing signature can be recovered only with independent
                # rooted-expiry evidence; never resend an unresolved operation.
                if (row["kind"] in ("BUY", "SELL", "WITHDRAW") and row.get("wire")
                        and time.time() - row["created_at"] >= 180):
                    checks = getattr(self, "_expiry_checks", {})
                    last_check = checks.get(row["id"], 0)
                    if time.monotonic() - last_check >= 60:
                        self._expiry_checks = {row["id"]: time.monotonic()}
                        try:
                            async with asyncio.timeout(25):
                                async def independent_rpc(method, params):
                                    return await self.recovery_rpc(session, method, params)
                                evidence = await prove_retirement(independent_rpc, self.wallet, {
                                    "signature": row["signature"], "signed_wire": row["wire"],
                                    "timestamp": datetime.fromtimestamp(
                                        row["created_at"], timezone.utc).isoformat()})
                            self.ledger.fail(row["id"], network_proven=True, fee=0,
                                failure_reason="Solana history verified that the transaction was absent and expired.",
                                recovery_audit={"actor_id": row["uid"], "evidence": {
                                    **evidence, "outcome": "PROVEN_ABSENT_AND_EXPIRED",
                                    "ledger_release": True, "transaction_sent": False,
                                    "automatic": True}})
                            activity_log.record_jito_stage(self, row, "jito_absent_expired",
                                "Independent Solana history verified the signature absent and "
                                "the blockhash expired; the reservation was released safely.")
                        except (WalletError, LedgerError, asyncio.TimeoutError, aiohttp.ClientError):
                            # Missing proof is not a failed transaction. Retain its lock.
                            pass
                continue
            confirmation = status.get("confirmationStatus")
            if confirmation in ("processed", "confirmed", "finalized"):
                activity_log.record_jito_stage(self, row, "jito_chain_" + confirmation,
                    f"Solana reports the swap signature at {confirmation} commitment.")
            if status.get("err") is not None:
                # Failed transactions still pay a fee. Fetch it, charge only operator treasury.
                tx = await self.rpc(session, "getTransaction", [row["signature"], {
                    "encoding": "jsonParsed", "commitment": "finalized", "maxSupportedTransactionVersion": 0}])
                if status.get("confirmationStatus") != "finalized" or not tx:
                    continue
                fee = int(tx["meta"]["fee"])
                if fee > row["fee_cap"]:
                    raise WalletError("Комиссия ошибочной транзакции превышает резерв.")
                chain_error = json.dumps(status.get("err"), ensure_ascii=True,
                                         separators=(",", ":"), sort_keys=True)
                self.ledger.fail(row["id"], network_proven=True, fee=fee,
                    failure_reason=activity_log.safe_failure_reason(
                        "Solana finalized the transaction with an error: " + chain_error))
                activity_log.record_jito_stage(self, row, "jito_finalized_error",
                    "Solana finalized the swap with an error: " + chain_error)
                if row["kind"] == "SELL" and self.ledger.settings(row["uid"])["close_requested"]:
                    symbol = json.loads(row["metadata"]).get("symbol", "")
                    self.schedule_close_retry(row["uid"],
                        "Solana finalized this sale with an error: " + chain_error, symbol)
                continue
            if status.get("confirmationStatus") != "finalized":
                continue
            tx = await self.rpc(session, "getTransaction", [row["signature"], {
                "encoding": "jsonParsed", "commitment": "finalized", "maxSupportedTransactionVersion": 0}])
            if not tx or not tx.get("meta") or tx["meta"].get("err") is not None:
                continue
            info = json.loads(row["metadata"])
            keys = tx["transaction"]["message"]["accountKeys"]
            addresses = [k.get("pubkey") if isinstance(k, dict) else k for k in keys]
            index = addresses.index(self.wallet)
            native_delta = tx["meta"]["postBalances"][index] - tx["meta"]["preBalances"][index]
            if row["kind"] in ("WITHDRAW", "PARTNER_PAYOUT"):
                spent = row["raw"]
                asset_delta = self.effects(tx, self.wallet, row["asset"])
                if asset_delta != -spent:
                    raise WalletError("Результат партнёрского перевода не соответствует резерву.")
                fee = -native_delta - (spent if row["asset"] == SOL else 0)
                if fee < int(tx["meta"]["fee"]):
                    raise WalletError("Не удалось установить фактическую комиссию вывода.")
                received = 0
            else:
                spent = -self.effects(tx, self.wallet, row["asset"])
                received = self.effects(tx, self.wallet, info["output_asset"])
                spent, received = adjust_swap_amounts_for_operator_tip(
                    spent, received, row["asset"], info["output_asset"], info)
                operator_tip = operator_tip_lamports(info)
                # Economic SOL delta is actual native+owned-account change excluding network fee.
                economic_sol = self.effects(tx, self.wallet, SOL)
                trade_sol = (received if info["output_asset"] == SOL else 0) - (spent if row["asset"] == SOL else 0)
                if economic_sol != trade_sol - operator_tip:
                    raise WalletError("Неожиданное изменение SOL при swap.")
                fee = trade_sol - native_delta - self.wrapped_delta(tx, self.wallet)
            self.ledger.finalize(row["id"], spent, fee, received)
            activity_log.record_jito_stage(self, row, "jito_finalized_success",
                "Solana finalized the swap and the ledger reconciled its actual amounts and fees.")
        await self.deliver_withdrawal_notifications(session)
        await self.deliver_trade_notifications(session)

    @staticmethod
    def token_data(account):
        if not account:
            return None
        if account.get("owner") != str(TOKEN) or not isinstance(account.get("data"), list):
            raise WalletError("Симуляция вернула неподдерживаемый token account.")
        data = base64.b64decode(account["data"][0])
        if len(data) != 165:
            raise WalletError("Неподдерживаемый SPL account.")
        return data

    async def safe_simulation(self, session, tx, row, info):
        """Constrain user-owned accounts without exceeding narrow RPC account limits."""
        if row["kind"] in ("WITHDRAW", "PARTNER_PAYOUT"):
            # These locally-built transfers can only debit the native wallet and
            # the source ATA for this asset. Don't request every token account
            # owned by the shared wallet: some RPC providers cap account batches
            # at six, while a custody wallet may hold many unrelated token ATAs.
            addresses = [self.wallet]
            if row["asset"] != SOL:
                addresses.append(str(associated(
                    Pubkey.from_string(self.wallet), Pubkey.from_string(row["asset"]))))
        else:
            tokens = (await self.rpc(session, "getTokenAccountsByOwner", [
                self.wallet, {"programId": str(TOKEN)},
                {"encoding": "base64", "commitment": "confirmed"}]))["value"]
            addresses = [self.wallet] + [item["pubkey"] for item in tokens]
            for asset in {row["asset"], info.get("output_asset")}:
                if asset and asset != SOL:
                    address = str(associated(
                        Pubkey.from_string(self.wallet), Pubkey.from_string(asset)))
                    if address not in addresses:
                        addresses.append(address)
        if len(addresses) > 100:
            raise WalletError("Слишком много token accounts для полной безопасной симуляции.")
        before = (await self.rpc(session, "getMultipleAccounts", [addresses, {
            "encoding": "base64", "commitment": "confirmed"}]))["value"]
        if len(before) != len(addresses):
            raise WalletError("RPC вернул неполный список исходных счетов.")
        message = to_bytes_versioned(tx.message)
        fee = (await self.rpc(session, "getFeeForMessage", [
            base64.b64encode(message).decode(), {"commitment": "confirmed"}]))["value"]
        if fee is None or fee > row["fee_cap"]:
            raise WalletError("Комиссия выходит за резерв.")
        wire = base64.b64encode(bytes(tx)).decode()
        simulated = (await self.rpc(session, "simulateTransaction", [wire, {
            "encoding": "base64", "sigVerify": True, "commitment": "confirmed",
            "accounts": {"encoding": "base64", "addresses": addresses}}]))["value"]
        if simulated.get("err") is not None:
            raise WalletError("Симуляция отклонена: " + json.dumps(simulated["err"]), 409)
        after = simulated.get("accounts")
        if not after or len(after) != len(before) or not before[0] or not after[0]:
            raise WalletError("RPC не вернул полный результат симуляции.")
        if before[0].get("owner") != str(SYSTEM) or after[0].get("owner") != str(SYSTEM) or after[0].get("executable"):
            raise WalletError("Симуляция меняет системного владельца кошелька.")
        for native in (before[0], after[0]):
            if not isinstance(native.get("data"), list) or base64.b64decode(native["data"][0]):
                raise WalletError("Симуляция меняет обычный кошелёк на account с данными/nonce.")
        deltas = {SOL: after[0]["lamports"] - before[0]["lamports"] + fee}
        native_delta = after[0]["lamports"] - before[0]["lamports"]
        wrapped_delta = 0
        owner_bytes = bytes(Pubkey.from_string(self.wallet))
        for old, new in zip(before[1:], after[1:]):
            old_data, new_data = self.token_data(old), self.token_data(new)
            if old_data and old_data[32:64] != owner_bytes:
                raise WalletError("Token account принадлежит другому владельцу.")
            if new_data and new_data[32:64] != owner_bytes:
                raise WalletError("Симуляция меняет владельца token account.")
            if old_data and new_data and (old_data[:64] != new_data[:64] or old_data[72:] != new_data[72:]):
                raise WalletError("Симуляция меняет права, mint или состояние token account.")
            if not old_data and new_data and (any(new_data[72:108]) or any(new_data[129:])):
                raise WalletError("Симуляция создаёт token account с делегатом.")
            if new_data and new_data[108] != 1:
                raise WalletError("Симуляция возвращает неинициализированный или замороженный token account.")
            if not old_data and not new_data:
                continue
            mint = str(Pubkey.from_bytes((new_data or old_data)[:32]))
            old_raw = int.from_bytes(old_data[64:72], "little") if old_data else 0
            new_raw = int.from_bytes(new_data[64:72], "little") if new_data else 0
            if mint != WRAPPED_SOL:
                deltas[mint] = deltas.get(mint, 0) + new_raw - old_raw
            else:
                wrapped_delta += new_raw - old_raw
            # Token account lamports include rent and, for WSOL, wrapped amount.
            deltas[SOL] += (new["lamports"] if new else 0) - (old["lamports"] if old else 0)
        if row["kind"] in ("WITHDRAW", "PARTNER_PAYOUT"):
            expected = -row["raw"]
            if deltas.get(row["asset"], 0) != expected:
                raise WalletError("Симуляция перевода не соответствует зарезервированной сумме.")
            expense = -native_delta - (row["raw"] if row["asset"] == SOL else 0)
            if expense < fee or expense > row["fee_cap"]:
                raise WalletError("Расход SOL на вывод выходит за резерв комиссий.")
            permitted = {row["asset"], SOL}
        else:
            spent, acquired = -deltas.get(row["asset"], 0), deltas.get(info["output_asset"], 0)
            spent, acquired = adjust_swap_amounts_for_operator_tip(
                spent, acquired, row["asset"], info["output_asset"], info)
            if spent <= 0 or spent > row["raw"] or acquired <= 0 or acquired < int(info["minimum_out"]):
                raise WalletError("Симуляция swap выходит за сумму пользователя или slippage.")
            if row["kind"] == "SELL" and spent != row["raw"]:
                raise WalletError("Частичная продажа не соответствует запросу.")
            trade_sol = (acquired if info["output_asset"] == SOL else 0) - (spent if row["asset"] == SOL else 0)
            expense = trade_sol - native_delta - wrapped_delta
            if expense > row["fee_cap"]:
                raise WalletError("Swap расходует слишком большой резерв комиссий.")
            if row["kind"] == "BUY" and row["asset"] == USDC:
                try:
                    context = info.get("entry_cost_context") or {}
                    if context.get("input_raw") != row["raw"]:
                        raise EntryCostError("ENTRY SKIPPED: cost evidence does not match the reserved order.", 409)
                    report = execution_policy.costs(context, max(0, expense))
                    report["symbol"] = info.get("symbol", "")
                    self.ledger.entry_cost_report(row["uid"], report)
                    if report["status"] != "ALLOW":
                        raise EntryCostError(report["reason"], 409)
                except EntryCostError as exc:
                    self.record_entry_cost_error(row["uid"], exc, info.get("symbol", ""))
                    raise
            permitted = {row["asset"], info["output_asset"], SOL}
        if any(delta for asset, delta in deltas.items() if asset not in permitted):
            raise WalletError("Симуляция затрагивает посторонние пользовательские активы.")
        return wire

    async def refresh_swap_blockhash(self, session, tx):
        """Re-sign the already simulated swap with a recent, verified blockhash."""
        message = getattr(tx, "message", None)
        if (not isinstance(message, MessageV0) or not message.account_keys
                or str(message.account_keys[0]) != self.wallet
                or message.header.num_required_signatures != 1):
            raise WalletError("The swap transaction cannot be safely refreshed.")
        try:
            latest = await self.rpc(session, "getLatestBlockhash", [
                {"commitment": "confirmed"}])
            value = latest.get("value") if isinstance(latest, dict) else None
            context = latest.get("context") if isinstance(latest, dict) else None
            if (not isinstance(value, dict) or not isinstance(context, dict)
                    or type(context.get("slot")) is not int
                    or not isinstance(value.get("blockhash"), str)
                    or type(value.get("lastValidBlockHeight")) is not int):
                raise ValueError("incomplete recent blockhash response")
            current_height = await self.rpc(session, "getBlockHeight", [{
                "commitment": "confirmed", "minContextSlot": context["slot"]}])
            if (type(current_height) is not int
                    or value["lastValidBlockHeight"] - current_height < 20):
                raise WalletError(
                    "Recent blockhash is too close to expiry; no transaction was sent.")
            refreshed_message = MessageV0(
                message.header, message.account_keys,
                Hash.from_string(value["blockhash"]), message.instructions,
                message.address_table_lookups)
            if (refreshed_message.header != message.header
                    or refreshed_message.account_keys != message.account_keys
                    or refreshed_message.instructions != message.instructions
                    or refreshed_message.address_table_lookups != message.address_table_lookups):
                raise ValueError("refresh changed swap instructions")
            return VersionedTransaction(refreshed_message, [self.ready()])
        except WalletError:
            raise
        except Exception:
            raise WalletError(
                "Could not verify a fresh blockhash; no transaction was sent.") from None

    async def send_reserved(self, session, tx, row, info):
        try:
            wire = await self.safe_simulation(session, tx, row, info)
            if getattr(self, "web_access", None) and row["kind"] in ("BUY", "SELL"):
                self.web_access.require(row["uid"], row["kind"], row.get("position_id"),
                                        manual=info.get("manual_close") is True)
            if row["kind"] in ("BUY", "SELL"):
                self.authorize_execution_order(
                    row["uid"], row["asset"], info["output_asset"], row["raw"],
                    row["kind"], info, row.get("position_id"), reserved=True)
                await self.authorize_execution_quote(session, row["uid"], row["kind"],
                    row["asset"], info["output_asset"], row["raw"], info)
                tx = await self.refresh_swap_blockhash(session, tx)
                wire = base64.b64encode(bytes(tx)).decode()
            signature = str(tx.signatures[0])
            self.ledger.prepared(row["id"], signature, wire)
        except Exception as exc:
            # Still RESERVED: no possible broadcast; safe to return its own reservation.
            self.ledger.fail(row["id"], failure_reason=operation_failure_reason(exc))
            raise
        try:
            if row["kind"] in ("BUY", "SELL"):
                returned = await self.send_via_jito(session, wire, signature)
                activity_log.record_jito_stage(self, {**row, "signature": signature},
                    "jito_submission_acknowledged",
                    "Jito acknowledged the private submission with a "
                    f"{info.get('jito_tip_lamports', 0)} lamport tip "
                    f"({info.get('jito_tip_source', 'unknown source')}); "
                    "this is not proof of Solana inclusion.")
            else:
                returned = await self.rpc(session, "sendTransaction", [wire, {
                    "encoding": "base64", "skipPreflight": False,
                    "preflightCommitment": "confirmed"}])
            self.ledger.submitted(row["id"], returned == signature)
        except Exception as exc:
            self.ledger.submitted(row["id"], False)
            if row["kind"] in ("BUY", "SELL"):
                detail = activity_log.safe_failure_reason(str(exc)) or type(exc).__name__
                activity_log.record_jito_stage(self, {**row, "signature": signature},
                    "jito_submission_unknown",
                    "Jito did not return a verified acknowledgement (" + detail +
                    "); the transaction outcome is unknown. Do not resend.")
            if row["kind"] == "WITHDRAW":
                self.log("Withdrawal RPC outcome unknown; reserve retained (" + type(exc).__name__ + ").")
        if row["kind"] == "WITHDRAW":
            # Return the signature as soon as the RPC accepts (or ambiguously
            # rejects) the broadcast. Finality is reconciled by the next
            # authenticated stats refresh; never keep the withdrawal UI waiting
            # for finalized commitment before showing the transaction ID.
            return self.ledger.operation(row["id"])
        try:
            await self.reconcile(session)
        except Exception:
            pass
        return self.ledger.operation(row["id"])

    async def get_jito_tip_quote(self, session, *, allow_max_fallback=False):
        now = time.monotonic()
        cached = getattr(self, "_jito_tip_cache", None)
        if cached and now < cached["expires_at"]:
            return {"lamports": cached["lamports"], "source": "recent_95th_percentile"}
        lock = getattr(self, "_jito_tip_lock", None)
        if lock is None:
            lock = asyncio.Lock()
            self._jito_tip_lock = lock
        async with lock:
            now = time.monotonic()
            cached = getattr(self, "_jito_tip_cache", None)
            if cached and now < cached["expires_at"]:
                return {"lamports": cached["lamports"], "source": "recent_95th_percentile"}
            try:
                async with session.get(
                        JITO_TIP_FLOOR_URL,
                        timeout=aiohttp.ClientTimeout(total=5)) as response:
                    if response.status != 200:
                        raise WalletError(
                            "Fresh Jito tip data is unavailable; no swap was sent.")
                    rows = await response.json()
                point = rows[-1] if isinstance(rows, list) and rows else None
                if not isinstance(point, dict):
                    raise ValueError("missing tip-floor row")
                observed_raw = point.get("time")
                if not isinstance(observed_raw, str):
                    raise ValueError("missing tip-floor timestamp")
                observed = datetime.fromisoformat(observed_raw.replace("Z", "+00:00"))
                if observed.tzinfo is None:
                    raise ValueError("tip-floor timestamp is not timezone-aware")
                age = (datetime.now(timezone.utc) - observed.astimezone(timezone.utc)).total_seconds()
                if age < -30 or age > JITO_TIP_MAX_AGE_SECONDS:
                    raise ValueError("tip-floor data is stale")
                p95_sol = Decimal(str(point.get("landed_tips_95th_percentile")))
                if not p95_sol.is_finite() or p95_sol <= 0:
                    raise ValueError("invalid tip-floor percentile")
                p95_lamports = int(
                    (p95_sol * Decimal(1_000_000_000)).to_integral_value(rounding=ROUND_CEILING))
                tip = min(JITO_TIP_MAX_LAMPORTS,
                          max(JITO_TIP_MIN_LAMPORTS, p95_lamports))
            except WalletError:
                if allow_max_fallback:
                    return {"lamports": JITO_TIP_MAX_LAMPORTS,
                            "source": "approved_max_exit_fallback"}
                raise
            except Exception:
                if allow_max_fallback:
                    return {"lamports": JITO_TIP_MAX_LAMPORTS,
                            "source": "approved_max_exit_fallback"}
                raise WalletError(
                    "Fresh Jito tip data is invalid or unavailable; no swap was sent.") from None
            self._jito_tip_cache = {
                "lamports": tip,
                "expires_at": now + JITO_TIP_CACHE_SECONDS,
            }
            return {"lamports": tip, "source": "recent_95th_percentile"}

    async def get_jito_tip_account(self, session):
        now = time.monotonic()
        cached = getattr(self, "_jito_tip_accounts", [])
        if cached and now < getattr(self, "_jito_tip_accounts_expiry", 0):
            return random.choice(cached)
        payload = {"jsonrpc": "2.0", "id": 1, "method": "getTipAccounts", "params": []}
        async with session.post(
                JITO_TIP_ACCOUNTS_URL, json=payload,
                timeout=aiohttp.ClientTimeout(total=6)) as response:
            if response.status != 200:
                raise WalletError("Jito tip accounts are unavailable.")
            answer = await response.json()
        values = answer.get("result") if isinstance(answer, dict) else None
        if not isinstance(values, list) or not values:
            raise WalletError("Jito returned no tip accounts.")
        try:
            accounts = list(dict.fromkeys(str(Pubkey.from_string(value)) for value in values))
        except Exception as exc:
            raise WalletError("Jito returned invalid tip accounts.") from exc
        self._jito_tip_accounts = accounts
        self._jito_tip_accounts_expiry = now + 300
        return random.choice(accounts)

    async def build_swap_transaction(self, session, instructions_data, tip_account,
                                     tip_lamports=JITO_TIP_MIN_LAMPORTS):
        if not isinstance(instructions_data, dict):
            raise WalletError("Jupiter returned an incomplete set of swap instructions.")
        operator_tip_lamports({
            "execution_route": "jito_bundle_only",
            "jito_tip_lamports": tip_lamports,
        })
        instructions = []
        for name in ("computeBudgetInstructions", "setupInstructions"):
            group = instructions_data.get(name, [])
            if not isinstance(group, list):
                raise WalletError("Jupiter returned an invalid set of swap instructions.")
            instructions.extend(parse_jupiter_instruction(item) for item in group)
        token_ledger = instructions_data.get("tokenLedgerInstruction")
        if token_ledger:
            instructions.append(parse_jupiter_instruction(token_ledger))
        swap_instruction = instructions_data.get("swapInstruction")
        if not swap_instruction:
            raise WalletError("Jupiter did not return the primary swap instruction.")
        instructions.append(parse_jupiter_instruction(swap_instruction))
        cleanup = instructions_data.get("cleanupInstruction")
        if cleanup:
            instructions.append(parse_jupiter_instruction(cleanup))
        other = instructions_data.get("otherInstructions", [])
        if not isinstance(other, list):
            raise WalletError("Jupiter returned an invalid set of additional instructions.")
        instructions.extend(parse_jupiter_instruction(item) for item in other)
        try:
            lookup_addresses = instructions_data.get("addressLookupTableAddresses", [])
            if (not isinstance(lookup_addresses, list) or len(lookup_addresses) > 32
                    or any(not isinstance(address, str) for address in lookup_addresses)):
                raise ValueError("invalid lookup table list")
            lookup_addresses = list(dict.fromkeys(lookup_addresses))
            tip_pubkey = Pubkey.from_string(tip_account)
            instructions.append(transfer(TransferParams(
                from_pubkey=Pubkey.from_string(self.wallet),
                to_pubkey=tip_pubkey,
                lamports=tip_lamports,
            )))
        except Exception as exc:
            raise WalletError("Could not validate the Jito tip account.") from exc

        async def load_lookup_tables():
            if not lookup_addresses:
                return []
            result = await self.rpc(session, "getMultipleAccounts", [
                lookup_addresses, {"encoding": "base64", "commitment": "confirmed"}])
            rows = result.get("value")
            if not isinstance(rows, list) or len(rows) != len(lookup_addresses):
                raise WalletError("RPC returned an incomplete Jupiter lookup table list.")
            tables = []
            for address, row in zip(lookup_addresses, rows):
                if (not row or row.get("owner") != str(ADDRESS_LOOKUP_TABLE_PROGRAM_ID)
                        or not isinstance(row.get("data"), list) or not row["data"]):
                    raise WalletError("Jupiter lookup table is unavailable or has an unexpected owner.")
                try:
                    lookup = AddressLookupTable.deserialize(
                        base64.b64decode(row["data"][0], validate=True))
                    tables.append(AddressLookupTableAccount(Pubkey.from_string(address), lookup.addresses))
                except Exception as exc:
                    raise WalletError("Could not decode the Jupiter lookup table.") from exc
            return tables

        async def recent_blockhash():
            response = await self.rpc(session, "getLatestBlockhash", [{"commitment": "confirmed"}])
            blockhash = response.get("value", {}).get("blockhash")
            if not isinstance(blockhash, str):
                raise WalletError("RPC did not return a recent blockhash.")
            try:
                return Hash.from_string(blockhash)
            except Exception as exc:
                raise WalletError("RPC returned an invalid blockhash.") from exc

        lookup_tables, blockhash = await asyncio.gather(load_lookup_tables(), recent_blockhash())
        try:
            message = MessageV0.try_compile(
                Pubkey.from_string(self.wallet), instructions, lookup_tables, blockhash)
            if (not message.account_keys or str(message.account_keys[0]) != self.wallet
                    or message.header.num_required_signatures != 1):
                raise WalletError("The compiled swap transaction contains an unexpected signer.")
            return VersionedTransaction(message, [self.ready()])
        except WalletError:
            raise
        except Exception as exc:
            raise WalletError("Could not safely build the swap transaction.") from exc

    async def send_via_jito(self, session, wire, expected_signature):
        """Privately submit a pre-simulated swap; never fall back to public broadcast."""
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "sendTransaction",
            "params": [wire, {"encoding": "base64"}],
        }
        async with session.post(
                JITO_BUNDLE_SEND_URL, json=payload,
                timeout=aiohttp.ClientTimeout(total=8)) as response:
            if response.status != 200:
                raise WalletError("Jito private submission is unavailable.")
            answer = await response.json()
        if (not isinstance(answer, dict) or answer.get("error")
                or answer.get("result") != expected_signature):
            raise WalletError("Jito did not confirm the swap submission.")
        return answer["result"]

    def result(self, row):
        state = row["state"]
        kind = row.get("kind")
        if kind == "SELL":
            message = ("Sale confirmed on Solana." if state == "COMPLETED" else
                "Sale failed; the position remains open." if state == "FAILED" else
                "Sale submitted to Solana and waiting for confirmation. Do not submit it again."
                if state == "SUBMITTED" else
                "The sale result is unclear. We are checking it; do not submit it again.")
            return web.json_response({"success": state != "FAILED", "state": state,
                "operation_id": row["id"], "tx_signature": row["signature"], "message": message},
                status=200 if state == "COMPLETED" else 409 if state == "FAILED" else 202)
        return web.json_response({"success": state != "FAILED", "state": state,
            "operation_id": row["id"], "tx_signature": row["signature"],
            "message": "Withdrawal confirmed on Solana." if state == "COMPLETED" else
                "Withdrawal failed. The amount is available again." if state == "FAILED" else
                "Withdrawal sent to Solana and waiting for confirmation. Do not submit it again."
                if state == "SUBMITTED" else
                "The network result is unclear. We are checking it; do not submit this withdrawal again."
        }, status=200 if state == "COMPLETED" else 409 if state == "FAILED" else 202)

    async def request_withdrawal_code(self, request, session):
        uid = self.uid(request)
        self.admit(uid)
        if not getattr(self, "accounts", None):
            raise WalletError("Telegram withdrawal confirmation is unavailable.", 503)
        body = await request.json()
        payload, _ = self.accounts.withdrawal_intent(uid, body)
        request_id = payload["request_id"]
        token = payload["token"]
        asset = USDC if token == "USDC" else SOL
        raw = int(payload["raw"])
        async with self.lock:
            previous = self.ledger.find_request(uid, request_id)
            if previous:
                if (previous["kind"], previous["asset"], previous["raw"], previous["recipient"]) != (
                        "WITHDRAW", asset, raw, payload["recipient_address"]):
                    raise WalletError("This request ID belongs to a different transfer.", 409)
                raise WalletError("This withdrawal already exists. Check its status; do not start another.", 409)
            await self.reconcile(session)
            if self.pending():
                raise WalletError(
                    "A previous transfer is still being checked. Do not start another withdrawal.", 409)
            await self.solvent(session)
            withdrawable = self.ledger.withdrawal_capacity(uid, asset)
            if raw > withdrawable:
                decimals = 6 if asset == USDC else 9
                raise WalletError(
                    "The amount exceeds your verified withdrawal limit. Maximum available: "
                    + display_raw(withdrawable, decimals) + " " + token + ".", 409)
            return await self.accounts.send_withdrawal_code(request, session, body)

    async def withdraw(self, request, session):
        uid = self.uid(request)
        self.admit(uid)
        body = await request.json()
        token = str(body.get("token", "")).upper()
        if token not in ("USDC", "SOL"):
            raise WalletError("Выберите USDC или SOL.", 400)
        asset = USDC if token == "USDC" else SOL
        raw = amount_raw(body.get("amount", "0"), 6 if token == "USDC" else 9)
        recipient = Pubkey.from_string(str(body.get("recipient_address", "")))
        if not recipient.is_on_curve() or str(recipient) == self.wallet:
            raise WalletError("Укажите отдельный кошелёк получателя.", 400)
        request_id = str(body.get("request_id", ""))
        uuid.UUID(request_id)
        async with self.lock:
            previous = self.ledger.find_request(uid, request_id)
            if previous:
                if (previous["kind"], previous["asset"], previous["raw"], previous["recipient"]) != (
                        "WITHDRAW", asset, raw, str(recipient)):
                    raise WalletError("Идентификатор относится к другому переводу.", 409)
                return self.result(previous)
            await self.reconcile(session)
            if self.pending():
                raise WalletError(
                    "A previous transfer is still being checked. Do not start another withdrawal.", 409)
            await self.solvent(session)
            withdrawable = self.ledger.withdrawal_capacity(uid, asset)
            if raw > withdrawable:
                decimals = 6 if asset == USDC else 9
                raise WalletError(
                    "The amount exceeds your verified withdrawal limit. Maximum available: "
                    + display_raw(withdrawable, decimals) + " " + token + ".", 409)
            if getattr(self, "accounts", None):
                self.accounts.consume_withdrawal_code(uid, body)
            else:
                raise WalletError("Telegram withdrawal confirmation is unavailable.", 503)
            row = self.ledger.reserve(uid, request_id, "WITHDRAW", asset, raw, {}, str(recipient))
            try:
                tx = await super().withdraw_transaction(session, self.ready(), token, recipient, raw, row["id"])
            except Exception as exc:
                self.ledger.fail(row["id"], failure_reason=operation_failure_reason(exc))
                raise
            return self.result(await self.send_reserved(session, tx, row, {}))

    def record_entry_cost_error(self, uid, exc, symbol=""):
        self.record_execution_check(uid, execution_policy.deny("cost", str(exc)), "cost", "BUY", symbol)
        existing = self.ledger.entry_cost_report(uid) or {}
        if existing.get("status") == "SKIP" and existing.get("reason") == str(exc):
            return
        self.ledger.entry_cost_report(uid, {"status": "SKIP", "checked_at": int(time.time()),
            "symbol": symbol, "max_cost_pct": 2.5, "estimate_only": True, "reason": str(exc)})

    async def core_entry_cost_context(self, session, uid, quote, raw, output, headers, symbol=""):
        quoted_at = time.time()
        try:
            _, minimum_tokens = quote_amounts(quote, USDC, output, raw)
            async with session.get("https://api.jup.ag/swap/v1/quote", headers=headers, params={
                    "inputMint": output, "outputMint": USDC, "amount": minimum_tokens,
                    "slippageBps": 40}, timeout=aiohttp.ClientTimeout(total=10)) as response:
                if response.status != 200:
                    raise EntryCostError("ENTRY SKIPPED: reverse quote is unavailable.", 503)
                reverse = await response.json()
            _, minimum_usdc = quote_amounts(reverse, output, USDC, minimum_tokens)
            if abs(reverse["contextSlot"] - quote["contextSlot"]) > 75:
                raise EntryCostError("ENTRY SKIPPED: entry and exit quotes are not contemporaneous.", 409)
            sol_quote = reverse
            if output != WRAPPED_SOL:
                async with session.get("https://api.jup.ag/swap/v1/quote", headers=headers, params={
                        "inputMint": WRAPPED_SOL, "outputMint": USDC, "amount": 1_000_000_000,
                        "slippageBps": 40}, timeout=aiohttp.ClientTimeout(total=10)) as response:
                    if response.status != 200:
                        raise EntryCostError("ENTRY SKIPPED: SOL/USDC fee conversion is unavailable.", 503)
                    sol_quote = await response.json()
                quote_amounts(sol_quote, WRAPPED_SOL, USDC, 1_000_000_000)
                if abs(sol_quote["contextSlot"] - reverse["contextSlot"]) > 75:
                    raise EntryCostError("ENTRY SKIPPED: SOL/USDC fee conversion is stale.", 409)
            sol_usdc = Decimal(sol_quote["outAmount"]) * 1000 / Decimal(sol_quote["inAmount"])
            rent = await self.rpc(session, "getMinimumBalanceForRentExemption", [165, {"commitment": "confirmed"}])
            return {"quoted_at": quoted_at, "input_raw": raw, "exit_minimum_raw": minimum_usdc,
                    "sol_usdc": str(sol_usdc), "exit_rent_lamports": rent}
        except Exception as exc:
            if not isinstance(exc, EntryCostError):
                exc = EntryCostError("ENTRY SKIPPED: complete entry and exit cost evidence is unavailable.", 503)
            self.record_entry_cost_error(uid, exc, symbol)
            raise exc from None

    def record_execution_check(self, uid, verdict, stage, kind, symbol=""):
        self.ledger.execution_report(uid, {
            "policy_version": execution_policy.VERSION,
            "status": "ALLOW" if verdict.allowed else "BLOCKED",
            "stage": stage, "action": kind, "symbol": symbol,
            "code": verdict.code, "reason": verdict.reason, "checked_at": int(time.time())})
        return verdict

    def authorize_execution_order(self, uid, asset, output_asset, raw, kind, info,
                                  position_id=None, reserved=False):
        positions = (self.ledger.positions(uid) if reserved and kind == "SELL"
                     else self.ledger.positions(uid, "OPEN"))
        verdict = execution_policy.order(uid=uid, kind=kind, asset=asset,
            output_asset=output_asset, raw=raw, metadata=info, position_id=position_id,
            settings=self.ledger.settings(uid), positions=positions,
            watchlist=self.watchlist, universes=self.ai_universes(),
            balance=self.ledger.balance(uid, asset) + (raw if reserved and kind == "BUY" else 0),
            reserved=reserved)
        if verdict.allowed and info.get("ai_analysis") and not execution_policy.fresh(
                info.get("ai_created_at"), time.time(), execution_policy.DECISION_SECONDS):
            verdict = execution_policy.deny("stale", "BLOCKED: AI decision expired before execution.")
        self.record_execution_check(uid, verdict, "pre_send" if reserved else "order",
                                    kind, info.get("symbol", "")).require()

    async def authorize_execution_quote(self, session, uid, kind, asset, output_asset, raw, info):
        try:
            head_slot = await self.rpc(session, "getSlot", [{"commitment": "confirmed"}])
        except Exception:
            self.record_execution_check(uid, execution_policy.deny("quote_evidence",
                "BLOCKED: current Solana slot could not be verified. No transaction was sent."),
                "quote", kind, info.get("symbol", "")).require()
        verdict = execution_policy.quote(kind=kind, value=info.get("risk_quote"),
            input_mint=WRAPPED_SOL if asset == SOL else asset,
            output_mint=WRAPPED_SOL if output_asset == SOL else output_asset, raw=raw,
            received_at=info.get("quote_received_at"), head_slot=head_slot)
        self.record_execution_check(uid, verdict, "quote", kind, info.get("symbol", "")).require()

    async def trade(self, session, uid, asset, output_asset, raw, kind, metadata, position_id=None, manual=False):
        if getattr(self, "web_access", None):
            self.web_access.require(uid, kind, position_id, manual)
        self.admit(uid)
        if kind == "BUY" and getattr(self, "accounts", None):
            self.accounts.require(uid)
        await self.reconcile(session)
        if self.pending():
            raise WalletError("Ожидается подтверждение предыдущей операции.", 409)
        await self.solvent(session)
        self.authorize_execution_order(uid, asset, output_asset, raw, kind, metadata, position_id)
        try:
            tip_quote = await self.get_jito_tip_quote(
                session, allow_max_fallback=kind == "SELL")
        except WalletError:
            reason = ("BLOCKED: fresh Jito tip statistics unavailable; no new BUY was sent."
                      if kind == "BUY" else
                      "BLOCKED: Jito tip statistics unavailable; SELL was not sent.")
            self.record_execution_check(uid, execution_policy.deny(
                "jito_tip_stats", reason), "tip_quote", kind, metadata.get("symbol", ""))
            raise
        tip_lamports = tip_quote["lamports"]
        metadata = {**metadata, "execution_policy_version": execution_policy.VERSION,
                    "execution_route": "jito_bundle_only",
                    "jito_tip_lamports": tip_lamports,
                    "jito_tip_source": tip_quote["source"]}
        row = self.ledger.reserve(uid, str(uuid.uuid4()), kind, asset, raw, metadata, position_id=position_id)
        try:
            if await self.rpc(session, "getGenesisHash") != GENESIS:
                raise WalletError("Только Solana mainnet.")
            headers = {"Accept": "application/json"}
            if os.getenv("JUPITER_API_KEY"):
                headers["x-api-key"] = os.environ["JUPITER_API_KEY"]
            mint_in = WRAPPED_SOL if asset == SOL else asset
            mint_out = WRAPPED_SOL if output_asset == SOL else output_asset
            quote_received_at = time.time()
            async with session.get("https://api.jup.ag/swap/v1/quote", headers=headers, params={
                "inputMint": mint_in, "outputMint": mint_out, "amount": raw, "slippageBps": 40}) as response:
                if response.status != 200:
                    raise WalletError("Jupiter quote недоступен.")
                quote = await response.json()
            if (quote.get("inputMint") != mint_in or quote.get("outputMint") != mint_out
                    or int(quote.get("inAmount", 0)) != raw or int(quote.get("slippageBps", 0)) > 40
                    or quote.get("swapMode") != "ExactIn"
                    or not 0 < int(quote.get("otherAmountThreshold", 0)) <= int(quote.get("outAmount", 0))):
                raise WalletError("Неверная котировка Jupiter.")
            info = {**metadata, "minimum_out": int(quote["otherAmountThreshold"]), "manual_close": manual,
                    "risk_quote": quote, "quote_received_at": quote_received_at}
            await self.authorize_execution_quote(session, uid, kind, asset, output_asset, raw, info)
            if kind == "BUY" and asset == USDC:
                info["entry_cost_context"] = await self.core_entry_cost_context(
                    session, uid, quote, raw, mint_out, headers, metadata.get("symbol", ""))
            async def fetch_swap_instructions():
                async with session.post("https://api.jup.ag/swap/v1/swap-instructions",
                        headers=headers, json=jupiter_swap_instructions_payload(quote, self.wallet),
                        timeout=aiohttp.ClientTimeout(total=8)) as response:
                    if response.status != 200:
                        raise WalletError("Jupiter swap instructions are unavailable.")
                    return await response.json()

            instructions_data, tip_account = await asyncio.gather(
                fetch_swap_instructions(), self.get_jito_tip_account(session))
            transaction = await self.build_swap_transaction(
                session, instructions_data, tip_account, tip_lamports)
            if getattr(self, "web_access", None):
                self.web_access.require(uid, kind, position_id, manual)
        except Exception as exc:
            self.ledger.fail(row["id"], failure_reason=operation_failure_reason(exc))
            raise
        return await self.send_reserved(session, transaction, row, info)

    async def stats(self, request, session):
        uid = self.uid(request)
        if getattr(self, "web_access", None):
            self.web_access.pause_expired(uid)
        await self.reconcile(session)
        self.ledger.audit()
        positions = self.ledger.positions(uid)
        with self.ledger.db() as db:
            operations = [dict(r) for r in db.execute(
                """SELECT o.*, n.delivered_at AS telegram_notice_delivered_at
                   FROM custody_operations o LEFT JOIN custody_withdrawal_notifications n
                   ON n.operation_id=o.id WHERE o.uid=? ORDER BY o.created_at DESC LIMIT 20""", (uid,))]
            invoices = [dict(r) for r in db.execute(
                "SELECT * FROM custody_invoices WHERE uid=? AND state='CREDITED' ORDER BY created_at DESC LIMIT 20", (uid,))]
            tx_deposits = [dict(r) for r in db.execute(
                "SELECT * FROM custody_tx_deposits WHERE uid=? ORDER BY block_time DESC", (uid,))]
        trades, history = [], []
        for p in positions:
            qty = p["raw"] / 10**p["decimals"]
            native = p["funding_asset"] == SOL
            scale = 1e9 if native else 1e6
            cost = p["cost_raw"] / scale
            gross_profit_raw = (p["proceeds_raw"] or 0) - p["cost_raw"]
            net_profit_raw = gross_profit_raw - p["partner_commission_raw"]
            profit = net_profit_raw / scale if p["state"] == "CLOSED" else 0
            trades.append({"token_symbol": self.watchlist.get(p["symbol"], {}).get("label", p["symbol"]), "symbol": p["symbol"],
                "token_mint": WRAPPED_SOL if p["asset"] == SOL else p["asset"],
                "amount_tokens": qty, "buy_price": cost / qty,
                "sell_price": (p["proceeds_raw"] or 0) / scale / qty if p["state"] == "CLOSED" else 0,
                "profit_usdc": profit if not native else 0, "profit_units": profit,
                "partner_commission": p["partner_commission_raw"] / scale,
                "funding_currency": "SOL" if native else "USDC", "profit_pct": profit / cost * 100,
                "timestamp": datetime.fromtimestamp(p["created_at"], timezone.utc).isoformat(),
                "buy_tx": p["buy_tx"], "sell_tx": p["sell_tx"], "status": p["state"],
                "position_id": p["id"]})
        for row in operations:
            if row["kind"] == "WITHDRAW":
                history.append({"type": "WITHDRAW", "amount": display_raw(row["raw"], 6 if row["asset"] == USDC else 9) +
                    (" USDC" if row["asset"] == USDC else " SOL"), "tx": row["signature"],
                    "time": datetime.fromtimestamp(row["created_at"], timezone.utc).isoformat(),
                    "status": row["state"], "telegram_notified": bool(row["telegram_notice_delivered_at"])})
        for inv in invoices:
            history.append({"type": "DEPOSIT", "amount": display_raw(inv["raw"], 6 if inv["asset"] == USDC else 9) +
                (" USDC" if inv["asset"] == USDC else " SOL"), "tx": inv["signature"],
                "time": datetime.fromtimestamp(inv["created_at"], timezone.utc).isoformat(), "status": "VERIFIED"})
        for receipt in tx_deposits:
            for asset, raw in json.loads(receipt["amounts"]).items():
                history.append({"type": "DEPOSIT", "amount": display_raw(raw, 6 if asset == USDC else 9) +
                    (" USDC" if asset == USDC else " SOL"), "tx": receipt["signature"],
                    "time": datetime.fromtimestamp(receipt["block_time"], timezone.utc).isoformat(), "status": "VERIFIED"})
        history.sort(key=lambda row: row["time"], reverse=True)
        history.sort(key=lambda r: r["time"], reverse=True)
        user_usdc, user_sol = self.ledger.balance(uid, USDC) / 1e6, self.ledger.balance(uid, SOL) / 1e9
        withdrawable_usdc = self.ledger.withdrawal_capacity(uid, USDC) / 1e6
        withdrawable_sol = self.ledger.withdrawal_capacity(uid, SOL) / 1e9
        profit = sum(t["profit_usdc"] for t in trades)
        settings = self.ledger.settings(uid)
        own_pending = [r for r in operations if r["state"] in ("RESERVED", "PREPARED", "SUBMITTED", "UNKNOWN")]
        def pending_summary(row):
            if row["asset"] == USDC:
                reserved_amount, reserved_asset = display_raw(row["raw"], 6), "USDC"
            elif row["asset"] == SOL:
                reserved_amount, reserved_asset = display_raw(row["raw"], 9), "SOL"
            else:
                position = next((p for p in positions if p["id"] == row.get("position_id")), None)
                reserved_amount = display_raw(row["raw"], position["decimals"]) if position else str(row["raw"])
                reserved_asset = position["symbol"] if position else "token"
            return {
                "kind": row.get("kind"), "state": row.get("state"),
                "created_at": row.get("created_at"), "signature": row.get("signature"),
                "reserved_amount": reserved_amount, "reserved_asset": reserved_asset,
            }
        return web.json_response({"success": True, "username": request["telegram_user"].get("username", "user"),
            "user_balance": user_usdc, "sol_balance": user_sol, "withdrawable": withdrawable_usdc,
            "withdrawable_sol": withdrawable_sol, "total_deposited": (sum(i["raw"] for i in invoices if i["asset"] == USDC)
                + sum(json.loads(r["amounts"]).get(USDC, 0) for r in tx_deposits))/1e6,
            "total_pnl": profit, "profit_usdc": profit, "total_roi": 0,
            "closed_count": sum(p["state"] == "CLOSED" for p in positions), "trades": trades,
            "wallet_history": history[:20], "wallet_status": {**self.readiness(),
                "finance_allowed": self.public_enabled or uid in self.beta_users},
            "operation_pending": bool(own_pending),
            "pending_operations": [pending_summary(row) for row in own_pending],
            "ledger_audited": True,
            "trading_active": bool(settings["trading"]), "is_admin": uid == self.owner_id,
            "operator_fees": self.operator_fees(uid),
            "sniper_active": bool(settings["sniper_trading"]),
            "sniper_settings": {"order_sol": settings["sniper_order_raw"]/1e9,
                "max_positions": execution_policy.MAX_POSITIONS["sniper"]},
            "profit_sol": sum(t["profit_units"] for t in trades if t["funding_currency"] == "SOL"),
            "ai_status": self.ai_status(),
            "execution_policy": execution_policy.describe(),
            "execution_check": self.ledger.execution_report(uid),
            "trading_settings": {"order_usdc": settings["order_raw"]/1e6,
                "max_positions": 5, "take_profit_pct": 3.5, "stop_loss_pct": 2,
                "slippage_pct": .4, "entry_dip_pct": .3, "poll_seconds": 20,
                "assets": self.ai_universes()["main"], "warmup_samples": 5},
            "entry_cost_check": self.ledger.entry_cost_report(uid),
            "worker_error": self.last_error if uid == self.owner_id else None})

    async def save_trading_settings(self, request, session):
        uid = self.uid(request)
        self.admit(uid)
        body = await request.json()
        raw = amount_raw(body.get("order_usdc", "1"), 6)
        sniper_raw = amount_raw(body["order_sol"], 9) if "order_sol" in body else None
        async with self.lock:
            if self.ledger.settings(uid)["trading"] or self.ledger.settings(uid)["sniper_trading"] or self.pending():
                raise WalletError("Остановите автопилот и дождитесь подтверждения операций перед изменением размера покупки.", 409)
            if raw > self.ledger.balance(uid, USDC):
                raise WalletError("Размер покупки превышает ваш доступный USDC-баланс.", 409)
            if (sniper_raw is not None and sniper_raw != self.ledger.settings(uid)["sniper_order_raw"]
                    and sniper_raw > self.ledger.balance(uid, SOL)):
                raise WalletError("Размер покупки снайпера превышает ваш личный SOL-баланс.", 409)
            self.ledger.settings(uid, raw=raw, sniper_raw=sniper_raw)
        return web.json_response({"success": True, "message": "Размер покупки сохранён. Автопилот не включён.",
                                 "order_usdc": raw/1e6})

    async def trading_preflight(self, uid, session, raw, funding=USDC, output=WRAPPED_SOL):
        self.admit(uid)
        await self.reconcile(session)
        if self.pending():
            raise WalletError("Сначала дождитесь подтверждения предыдущей операции.", 409)
        await self.solvent(session)
        if await self.rpc(session, "getGenesisHash") != GENESIS:
            raise WalletError("Для торговли требуется Solana mainnet.", 409)
        with self.ledger.db() as db:
            fee = db.execute("SELECT raw FROM custody_accounts WHERE account='treasury' AND asset=?", (SOL,)).fetchone()
        if not fee or fee["raw"] < FEE_CAP:
            reserve = display_raw(fee["raw"] if fee else 0, 9)
            raise WalletError("Резерв комиссий: " + reserve + " SOL; требуется не меньше "
                + display_raw(FEE_CAP, 9) + " SOL. В Настройках → Резерв комиссий выделите только свои SOL. Пользовательские средства автоматически не расходуются.", 409)
        prices = await self.prices_fn(session)
        if not any(prices.get(symbol, 0) > 0 for symbol in self.watchlist):
            raise WalletError("Источник котировок недоступен. Автопилот не запущен.", 503)
        await self.ai_snapshot(session, prices)
        headers = {"Accept": "application/json"}
        if os.getenv("JUPITER_API_KEY"):
            headers["x-api-key"] = os.environ["JUPITER_API_KEY"]
        async with session.get("https://api.jup.ag/swap/v1/quote", params={
            "inputMint": WRAPPED_SOL if funding == SOL else funding, "outputMint": output, "amount": raw, "slippageBps": 40},
                headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as response:
            if response.status != 200:
                raise WalletError("Jupiter не вернул котировку. Проверьте доступ к API; автопилот не запущен.", 503)
            quote = await response.json()
        if (quote.get("inputMint") != (WRAPPED_SOL if funding == SOL else funding) or quote.get("outputMint") != output
                or int(quote.get("inAmount", 0)) != raw or int(quote.get("outAmount", 0)) <= 0):
            raise WalletError("Jupiter вернул некорректную котировку. Автопилот не запущен.", 503)

    async def trading_readiness(self, request, session):
        uid = self.uid(request)
        raw = self.ledger.settings(uid)["order_raw"]
        if raw > self.ledger.balance(uid, USDC) and not self.ledger.positions(uid, "OPEN"):
            raise WalletError("Недостаточно личных USDC для выбранного размера покупки.", 409)
        await self.trading_preflight(uid, session, raw)
        return web.json_response({"success": True, "message": "Проверены mainnet, покрытие средств, резерв SOL, цены и котировка Jupiter. Подписи и отправки сделок не было.",
            "order_usdc": raw/1e6, "trading_active": bool(self.ledger.settings(uid)["trading"])})

    async def toggle_sniper(self, request, session):
        uid = self.uid(request)
        self.admit(uid)
        async with self.lock:
            settings = self.ledger.settings(uid)
            if settings["sniper_trading"]:
                self.ledger.settings(uid, sniper_enabled=False)
            else:
                if getattr(self, "web_access", None):
                    self.web_access.trading_request(request)
                symbols = self.ai_universes()["sniper"]
                if not symbols:
                    raise WalletError("No verified shortlist yet. Use Scan candidates, then retry.", 409)
                if settings["sniper_order_raw"] > self.ledger.balance(uid, SOL) and not any(
                        p["funding_asset"] == SOL for p in self.ledger.positions(uid, "OPEN")):
                    raise WalletError("Недостаточно ваших SOL для снайпера.", 409)
                mint = self.watchlist[symbols[0]]["mint"]
                await self.verify_sniper_mint(session, symbols[0])
                await self.trading_preflight(uid, session, settings["sniper_order_raw"], SOL, mint)
                if getattr(self, "web_access", None):
                    self.web_access.trading_request(request)
                self.ledger.settings(uid, sniper_enabled=True)
        return web.json_response({"success": True, "sniper_active": bool(self.ledger.settings(uid)["sniper_trading"])})

    async def logs(self, request, session):
        uid = self.uid(request)
        return web.json_response({"success": True, **activity_log.report(self, uid),
            "trading_active": bool(self.ledger.settings(uid)["trading"]),
            "sniper_active": bool(self.ledger.settings(uid)["sniper_trading"])})

    async def partner_program(self, request, session):
        uid = self.uid(request)
        bot_username = getattr(getattr(self, "web_access", None), "bot_username", "")
        return web.json_response({"success": True, **self.ledger.partner_report(uid, bot_username)})

    async def retry_partner_payout(self, request, session):
        self.uid(request)
        raise WalletError("Partner earnings are credited to your main balance. External payout retries are disabled.", 409)

    async def toggle(self, request, session):
        uid = self.uid(request)
        self.admit(uid)
        settings = self.ledger.settings(uid)
        if settings["trading"]:
            self.ledger.settings(uid, enabled=False)
        else:
            if getattr(self, "web_access", None):
                self.web_access.trading_request(request)
            body = await request.json()
            raw = amount_raw(body.get("trade_amount_usdc", "1"), 6)
            if raw > self.ledger.balance(uid, USDC) and not self.ledger.positions(uid, "OPEN"):
                raise WalletError("Размер покупки превышает ваш собственный доступный USDC-баланс.", 409)
            await self.trading_preflight(uid, session, raw)
            if getattr(self, "web_access", None):
                self.web_access.trading_request(request)
            self.ledger.settings(uid, enabled=True, raw=raw)
        return web.json_response({"success": True, "trading_active": bool(self.ledger.settings(uid)["trading"])})

    async def close_all(self, request, session):
        uid = self.uid(request)
        self.admit(uid)
        body = await request.json()
        if body.get("confirm") is not True:
            raise WalletError("Подтвердите закрытие только своих позиций.", 400)
        was_queued = bool(self.ledger.settings(uid)["close_requested"])
        positions = self.ledger.positions(uid, "OPEN")
        pending_sell = any(op["uid"] == uid and op["kind"] == "SELL"
                           for op in self.ledger.pending())
        should_queue = bool(positions or pending_sell)
        self.ledger.settings(uid, enabled=False, sniper_enabled=False, close=should_queue)
        if not should_queue:
            self._close_retry_after.pop(uid, None)
            activity_log.record_event(self, uid, f"close-empty-{uuid.uuid4()}",
                "close_queue_empty", "Close request checked: no confirmed open positions remain. No transaction was sent.")
            return web.json_response({"success": True,
                "message": "No confirmed open positions remain. No transaction was sent."})
        if not was_queued:
            self._close_retry_after.pop(uid, None)
            waiting = " An existing sell is awaiting confirmation." if pending_sell else ""
            activity_log.record_event(self, uid, f"close-queued-{uuid.uuid4()}",
                "close_queue_queued",
                f"Close-all request queued for {len(positions)} open position(s). "
                f"Core and Sniper are OFF.{waiting} No new transaction was sent by this request.")
        return web.json_response({"success": True, "message": "Закрытие ваших подтверждённых позиций поставлено в очередь."})

    async def close_position(self, request, session):
        uid = self.uid(request)
        self.admit(uid)
        body = await request.json()
        if body.get("confirm") is not True:
            raise WalletError("Confirm the sale of the selected position.", 400)
        position_id = body.get("position_id")
        if not isinstance(position_id, str) or not position_id or len(position_id) > 128:
            raise WalletError("Invalid position.", 400)
        position = next((row for row in self.ledger.positions(uid)
                         if row["id"] == position_id), None)
        if not position:
            raise WalletError("Open position was not found for your account.", 404)
        if position["state"] != "OPEN":
            raise WalletError("A sale for this position is already pending. Do not submit it again.", 409)
        metadata = {
            "output_asset": position["funding_asset"],
            "symbol": position["symbol"],
            "decimals": position["decimals"],
            "manual_close": True,
        }
        return await self.trade(session, uid, position["asset"], position["funding_asset"],
            position["raw"], "SELL", metadata, position_id=position_id, manual=True)

    async def scan_deposits(self, session):
        with self.ledger.db() as db:
            if not db.execute("SELECT id FROM custody_invoices WHERE state='OPEN' AND expires_at>? LIMIT 1",
                              (int(time.time()) - 86400,)).fetchone():
                return
        addresses = [self.wallet, str(associated(Pubkey.from_string(self.wallet), Pubkey.from_string(USDC)))]
        for address in addresses:
            with self.ledger.db() as db:
                saved = db.execute("SELECT signature FROM custody_scan_cursor WHERE address=?", (address,)).fetchone()
                progress = db.execute("SELECT * FROM custody_scan_progress WHERE address=?", (address,)).fetchone()
                oldest = db.execute("SELECT MIN(created_at) FROM custody_invoices WHERE state='OPEN' AND expires_at>?",
                                    (int(time.time()) - 86400,)).fetchone()[0]
            if oldest is None:
                return
            cursor = saved["signature"] if saved else None
            newest, before, done = None, None, False
            cutoff = oldest - 60
            if progress:
                cursor = progress["boundary_signature"]
                newest, before, cutoff = progress["newest_signature"], progress["before_signature"], progress["cutoff"]
            for page in range(5):
                options = {"limit": 100, "commitment": "finalized"}
                if before:
                    options["before"] = before
                signatures = await self.rpc(session, "getSignaturesForAddress", [address, options])
                if not signatures:
                    done = True
                    break
                newest = newest or signatures[0]["signature"]
                for item in signatures:
                    if item["signature"] == cursor or (not cursor and item.get("blockTime") and item["blockTime"] < cutoff):
                        done = True
                        break
                    with self.ledger.db() as db:
                        seen = db.execute("SELECT signature FROM custody_seen_signatures WHERE signature=?", (item["signature"],)).fetchone()
                        known_server = db.execute("SELECT id FROM custody_operations WHERE signature=?", (item["signature"],)).fetchone()
                    if seen or known_server:
                        continue
                    if item.get("err") is not None:
                        permanent = True
                    else:
                        permanent = False
                        try:
                            await self.apply_deposit(session, item["signature"])
                            permanent = True
                        except (WalletError, LedgerError, ValueError) as exc:
                            if isinstance(exc, WalletError) and (exc.status == 503 or "ещё не" in str(exc)):
                                raise
                            permanent = True
                    if permanent:
                        with self.ledger.db() as db:
                            db.execute("INSERT OR IGNORE INTO custody_seen_signatures VALUES(?,?)",
                                       (item["signature"], int(time.time())))
                if done or len(signatures) < 100:
                    done = True
                    break
                before = signatures[-1]["signature"]
            if done and newest:
                with self.ledger.db() as db:
                    db.execute("INSERT INTO custody_scan_cursor VALUES(?,?) ON CONFLICT(address) DO UPDATE SET signature=excluded.signature",
                               (address, newest))
                    db.execute("DELETE FROM custody_scan_progress WHERE address=?", (address,))
            elif newest and before:
                with self.ledger.db() as db:
                    db.execute("""INSERT INTO custody_scan_progress VALUES(?,?,?,?,?)
                        ON CONFLICT(address) DO UPDATE SET newest_signature=excluded.newest_signature,
                        boundary_signature=excluded.boundary_signature,before_signature=excluded.before_signature,
                        cutoff=excluded.cutoff""", (address, newest, cursor, before, cutoff))

    async def deliver_recovery_notifications(self, session):
        async with self._notification_lock:
            for event in self.ledger.recovery_notifications(int(time.time())):
                payload = json.loads(event["payload"])
                restored = "The asset was restored to the open position."
                if payload["kind"] == "BUY":
                    asset = payload["asset"]
                    amount = (display_raw(payload["raw"], 9 if asset == SOL else 6)
                              if asset in (SOL, USDC) else str(payload["raw"]))
                    symbol = "SOL" if asset == SOL else "USDC" if asset == USDC else asset
                    restored = f"Purchase reservation released: {amount} {symbol}."
                    mode_note = (
                        "Core and Sniper settings were left unchanged.\n"
                        "This BUY will not be retried; any later order requires a fresh AI analysis.\n")
                else:
                    mode_note = (
                        "Core: OFF. Sniper: OFF.\n"
                        "This transaction was not retried. Enable a mode manually to resume.\n")
                text = (
                    "AI Trader: a pending operation was safely recovered.\n"
                    "Solana proved expiry; the transaction was not found.\n"
                    f"{restored}\n{mode_note}"
                    f"Transaction: https://solscan.io/tx/{payload['signature']}")
                delivered, error = False, "Telegram delivery unavailable"
                retry_after = min(3600, 60 * 2 ** min(event["attempts"], 6))
                try:
                    if not self.notification_token:
                        raise WalletError("Telegram notification token is not configured.")
                    async with session.post(
                            f"https://api.telegram.org/bot{self.notification_token}/sendMessage",
                            json={"chat_id": event["uid"], "text": text,
                                  "disable_web_page_preview": True},
                            timeout=aiohttp.ClientTimeout(total=8)) as response:
                        result = await response.json()
                        delivered = (response.status == 200 and result.get("ok") is True
                                     and isinstance(result.get("result"), dict)
                                     and type(result["result"].get("message_id")) is int)
                        if not delivered:
                            error = f"Telegram delivery rejected (HTTP {response.status})"
                            delay = result.get("parameters", {}).get("retry_after")
                            if type(delay) is int and delay > 0:
                                retry_after = max(retry_after, delay)
                except Exception:
                    # Never log exception text: HTTP errors can include the bot token.
                    pass
                self.ledger.recovery_notification_result(
                    event["operation_id"], int(time.time()), delivered, retry_after, error)
                if not delivered:
                    self.log("Recovery notification pending; delivery will be retried.")

    async def deliver_withdrawal_notifications(self, session):
        async with self._notification_lock:
            for event in self.ledger.withdrawal_notifications(int(time.time())):
                payload = json.loads(event["payload"])
                asset = payload["asset"]
                symbol = "USDC" if asset == USDC else "SOL" if asset == SOL else ""
                if not symbol:
                    self.ledger.withdrawal_notification_result(
                        event["operation_id"], int(time.time()), False, 3600,
                        "Unsupported withdrawal asset")
                    continue
                amount = display_raw(payload["raw"], 6 if asset == USDC else 9)
                signature = payload["signature"]
                text = (
                    "Zer0Life AI Trader\n"
                    "Withdrawal confirmed on Solana.\n"
                    f"Sent: {amount} {symbol}\n"
                    f"Recipient: {payload['recipient']}\n"
                    f"Transaction: https://solscan.io/tx/{signature}")
                delivered, error = False, "Telegram delivery unavailable"
                retry_after = min(3600, 60 * 2 ** min(event["attempts"], 6))
                try:
                    if not self.notification_token:
                        raise WalletError("Telegram notification token is not configured.")
                    async with session.post(
                            f"https://api.telegram.org/bot{self.notification_token}/sendMessage",
                            json={"chat_id": event["uid"], "text": text,
                                  "disable_web_page_preview": True},
                            timeout=aiohttp.ClientTimeout(total=8)) as response:
                        result = await response.json()
                        delivered = (response.status == 200 and result.get("ok") is True
                                     and isinstance(result.get("result"), dict)
                                     and type(result["result"].get("message_id")) is int)
                        if not delivered:
                            error = f"Telegram delivery rejected (HTTP {response.status})"
                            delay = result.get("parameters", {}).get("retry_after")
                            if type(delay) is int and delay > 0:
                                retry_after = max(retry_after, delay)
                except Exception:
                    # Never log exception text: HTTP errors can include the bot token.
                    pass
                self.ledger.withdrawal_notification_result(
                    event["operation_id"], int(time.time()), delivered, retry_after, error)
                if not delivered:
                    self.log("Withdrawal notification pending; delivery will be retried.")

    @staticmethod
    def trade_notification_text(payload):
        kind = payload.get("kind")
        symbol = "".join(c for c in str(payload.get("symbol", "TOKEN"))
                         if c.isalnum() or c in "-_")[:24] or "TOKEN"
        if kind == "STOP_LOSS_FAILED":
            signature = payload.get("signature")
            text = (f"Zer0Life AI Trader\nStop-loss triggered for {symbol}, "
                    "but the sale was not confirmed. The position remains open.")
            if (isinstance(signature, str) and 64 <= len(signature) <= 100
                    and all(c in "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
                            for c in signature)):
                text += f"\nTransaction: https://solscan.io/tx/{signature}"
            return text
        funding_asset = payload.get("funding_asset")
        funding_name = "USDC" if funding_asset == USDC else "SOL" if funding_asset == SOL else None
        raw, decimals = payload.get("token_raw"), payload.get("token_decimals")
        funding_raw = payload.get("funding_raw")
        signature = payload.get("signature")
        valid_signature = (isinstance(signature, str) and 64 <= len(signature) <= 100
            and all(c in "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
                    for c in signature))
        if (kind not in ("BUY", "SELL") or funding_name is None
                or type(raw) is not int or raw <= 0
                or type(decimals) is not int or not 0 <= decimals <= 18
                or type(funding_raw) is not int or funding_raw <= 0
                or not valid_signature):
            raise WalletError("Trade notification data failed validation.")
        token_amount = display_raw(raw, decimals)
        funding_amount = display_raw(funding_raw, 6 if funding_asset == USDC else 9)
        if kind == "BUY":
            header = "Purchase confirmed on Solana."
            details = f"Bought: {token_amount} {symbol}\nSpent: {funding_amount} {funding_name}"
        else:
            reason = payload.get("exit_reason")
            if reason == "STOP_LOSS":
                header = "Stop-loss triggered — sale confirmed on Solana."
            elif reason == "TAKE_PROFIT":
                header = "Take-profit reached — sale confirmed on Solana."
            else:
                header = "Sale confirmed on Solana."
            details = f"Sold: {token_amount} {symbol}\nCredited: {funding_amount} {funding_name}"
        return ("Zer0Life AI Trader\n" + header + "\n" + details
                + f"\nTransaction: https://solscan.io/tx/{signature}")

    async def deliver_trade_notifications(self, session):
        if not self.notification_token:
            return
        async with self._notification_lock:
            for event in self.ledger.trade_notifications(int(time.time())):
                oid = event["operation_id"]
                now = int(time.time())
                if not self.ledger.claim_trade_notification(oid, now):
                    continue
                outcome, retry_after = "UNKNOWN", 60
                error = "Delivery outcome unknown; not retried to avoid a duplicate push."
                try:
                    text = self.trade_notification_text(json.loads(event["payload"]))
                    async with session.post(
                            f"https://api.telegram.org/bot{self.notification_token}/sendMessage",
                            json={"chat_id": event["uid"], "text": text,
                                  "disable_web_page_preview": True},
                            timeout=aiohttp.ClientTimeout(total=8)) as response:
                        result = await response.json()
                        if (response.status == 200 and result.get("ok") is True
                                and isinstance(result.get("result"), dict)
                                and type(result["result"].get("message_id")) is int):
                            outcome, error = "DELIVERED", None
                        elif response.status == 429 and result.get("ok") is False:
                            outcome = "RETRY"
                            error = "Telegram rate limited the notification."
                            delay = result.get("parameters", {}).get("retry_after")
                            if type(delay) is int and delay > 0:
                                retry_after = min(delay, 3600)
                        elif response.status < 500 and result.get("ok") is False:
                            outcome = "FAILED"
                            error = f"Telegram rejected the notification (HTTP {response.status})."
                except WalletError:
                    outcome, error = "FAILED", "Trade notification data failed validation."
                except Exception:
                    # A lost response is ambiguous: do not send another push.
                    outcome = "UNKNOWN"
                self.ledger.trade_notification_result(
                    oid, int(time.time()), outcome, retry_after=retry_after, error=error)
                if outcome in ("FAILED", "UNKNOWN"):
                    self.log("Trade notification " + outcome.lower()
                             + "; the confirmed trade remains recorded.")

    async def recovery_notification_worker(self):
        while True:
            try:
                async with aiohttp.ClientSession() as session:
                    await self.deliver_recovery_notifications(session)
                    await self.deliver_withdrawal_notifications(session)
            except Exception:
                self.log("Recovery notification worker unavailable; queued alerts are retained.")
            await asyncio.sleep(20)

    async def process_close_queues(self, session):
        queued = [user for user in self.ledger.active_users() if user["close_requested"]]
        if not queued:
            return False
        if self.pending():
            return True
        for queued_user in queued:
            uid = queued_user["uid"]
            retry = self._close_retry_after.get(uid, {})
            if retry.get("until", 0) > time.monotonic():
                continue
            async with self.lock:
                if self.pending():
                    return True
                if getattr(self, "web_access", None):
                    self.web_access.pause_expired(uid)
                current = self.ledger.settings(uid)
                if not current["close_requested"]:
                    continue
                positions = self.ledger.positions(uid, "OPEN")
                if not positions:
                    self.ledger.settings(uid, close=False)
                    self._close_retry_after.pop(uid, None)
                    activity_log.record_event(self, uid, f"close-complete-{uuid.uuid4()}",
                        "close_queue_complete", "Close-all complete: no open position remains.")
                    continue
                target = positions[0]
                try:
                    await self.trade(session, uid, target["asset"], target["funding_asset"],
                        target["raw"], "SELL", {"output_asset": target["funding_asset"]},
                        target["id"], manual=True)
                    self._close_retry_after.pop(uid, None)
                except Exception as exc:
                    self.schedule_close_retry(uid, operation_failure_reason(exc), target["symbol"])
                    self.log("Close queue attempt failed; the request remains queued and will retry automatically.")
                return True
        return True

    def schedule_close_retry(self, uid, reason, symbol=""):
        previous = self._close_retry_after.get(uid, {})
        attempts = previous.get("attempts", 0) + 1
        delay = min(300, 20 * (2 ** min(attempts - 1, 4)))
        self._close_retry_after[uid] = {
            "until": time.monotonic() + delay, "attempts": attempts}
        name = activity_log.label(self, symbol) or "position"
        safe_reason = activity_log.safe_failure_reason(reason)
        activity_log.record_event(self, uid, f"close-error-{uuid.uuid4()}",
            "close_queue_retry",
            f"Close attempt for {name} has no finalized success: {safe_reason}. "
            f"The queue remains active. The server will not send again while a transaction is unresolved; "
            f"next retry is no sooner than {delay} seconds.")
        return delay

    async def worker_cycle(self, session):
        if getattr(self, "web_access", None):
            self.web_access.pause_expired()
        await self.reconcile(session)
        close_queue_active = await self.process_close_queues(session)
        await self.scan_deposits(session)
        self.ledger.credit_safe_partner_earnings()
        if close_queue_active:
            return
        users = self.ledger.active_users()
        if not users:
            return
        self.poll_discovery()
        prices = await self.current_prices(session)
        for symbol in self.history:
            if symbol not in prices:
                self.history[symbol] = []
        for symbol, price in prices.items():
            if symbol in self.history and price > 0:
                if time.time()-self.history_seen.get(symbol, 0) > 90:
                    self.history[symbol] = []
                self.history[symbol] = (self.history[symbol] + [price])[-90:]
                self.history_seen[symbol] = time.time()
        snapshot = self.poll_ai(prices) if any(
            user["trading"] or user["sniper_trading"] for user in users) else None
        for settings in users:
            uid = settings["uid"]
            async with self.lock:
                if self.pending():
                    break
                if getattr(self, "web_access", None):
                    self.web_access.pause_expired(uid)
                current = self.ledger.settings(uid)
                positions = self.ledger.positions(uid, "OPEN")
                target = None
                target_reason = None
                for position in positions:
                    mode_enabled = (current["sniper_trading"] if position["funding_asset"] == SOL
                                    else current["trading"])
                    reason = self.exit_reason(position, prices) if mode_enabled else None
                    if reason:
                        target, target_reason = position, reason
                        break
                if target:
                    await self.trade(session, uid, target["asset"], target["funding_asset"],
                        target["raw"], "SELL", {"output_asset": target["funding_asset"],
                            "exit_reason": target_reason, "symbol": target["symbol"]},
                        target["id"], manual=False)
                elif snapshot:
                    await self.apply_ai_decision(session, uid, current, positions, prices, snapshot)

    async def worker(self):
        # Settings are paused on restart. Explicit close queues run before market scans.
        while True:
            stream = getattr(self, "whale_stream", None)
            stream_task = getattr(self, "whale_stream_task", None)
            if stream and stream.enabled and (stream_task is None or stream_task.done()):
                self.whale_stream_task = asyncio.create_task(stream.run())
            try:
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=35)) as session:
                    await self.worker_cycle(session)
            except Exception as exc:
                message = str(exc) if isinstance(exc, (WalletError, LedgerError)) else type(exc).__name__
                if message != self.last_error:
                    self.log("Custody worker: " + message)
                    self.last_error = message
            else:
                self.last_error = ""
            await asyncio.sleep(20)

    def exit_reason(self, position, prices):
        price = prices.get(position["symbol"], 0)
        if not math.isfinite(price) or price <= 0:
            return None
        native = position["funding_asset"] == SOL
        if native:
            if not math.isfinite(prices.get("SOL", 0)) or prices.get("SOL", 0) <= 0:
                return None
            price /= prices["SOL"]
            if not math.isfinite(price) or price <= 0:
                return None
        entry = position["cost_raw"] / (1e9 if native else 1e6) / (position["raw"]/10**position["decimals"])
        return execution_policy.risk_exit_reason(price, entry)

    def exit_triggered(self, position, prices):
        return self.exit_reason(position, prices) is not None

    async def verify_sniper_mint(self, session, symbol):
        watch = self.watchlist[symbol]
        account = (await self.rpc(session, "getAccountInfo", [
            watch["mint"], {"encoding": "jsonParsed", "commitment": "finalized"}]))["value"]
        if not account or account.get("owner") != str(TOKEN):
            raise WalletError("Снайпер разрешает только стандартный SPL Token mint.", 409)
        if not isinstance(account.get("data"), dict):
            raise WalletError("RPC не подтвердил параметры mint.", 409)
        data = account["data"].get("parsed", {})
        info = data.get("info", {})
        if (data.get("type") != "mint" or info.get("decimals") != watch["decimals"]
                or not info.get("isInitialized") or info.get("mintAuthority") is not None
                or info.get("freezeAuthority") is not None):
            raise WalletError("Mint снайпера не прошёл проверку decimals/authority.", 409)

    async def apply_ai_decision(self, session, uid, settings, positions, prices, snapshot):
        # No provider output can select an amount, wallet, owner, or non-allowlisted mint.
        ai_decisions.validate_decisions(snapshot["decisions"], snapshot.get("universes", self.ai_universes()))
        if not execution_policy.fresh(snapshot["created_at"], time.time(), execution_policy.DECISION_SECONDS):
            for mode, flag in (("main", "trading"), ("sniper", "sniper_trading")):
                if settings[flag]:
                    activity_log.observe(self, uid, snapshot, mode)
                    activity_log.record(self, uid, snapshot, mode, "stale",
                        "SKIPPED: AI decision is older than 120 seconds. Waiting for a fresh analysis.")
                    self.record_execution_check(uid, execution_policy.deny("stale",
                        "BLOCKED: AI decision timestamp is stale or invalid."),
                        "proposal", snapshot["decisions"][mode]["action"],
                        snapshot["decisions"][mode]["symbol"] or "")
            return
        for mode, funding, flag, size_key in [
                ("main", USDC, "trading", "order_raw"),
                ("sniper", SOL, "sniper_trading", "sniper_order_raw")]:
            if not settings[flag]:
                continue
            activity_log.observe(self, uid, snapshot, mode)
            def note(code, message):
                activity_log.record(self, uid, snapshot, mode, code, message)
                self.record_execution_check(uid, execution_policy.deny(code, message),
                                            "proposal", action, symbol or "")
            decision = snapshot["decisions"][mode]
            symbol, action = decision["symbol"], decision["action"]
            held = [p for p in positions if p["funding_asset"] == funding]
            verdict = execution_policy.proposal(mode=mode, decision=decision,
                # Interpret the proposal against its server-captured universe.
                # The mandatory order gate rechecks the LIVE catalog before any send.
                universe=snapshot.get("universes", self.ai_universes())[mode],
                created_at=snapshot["created_at"],
                original=snapshot["prices"], prices=prices, samples=snapshot.get("samples", {}),
                held=held, balance=self.ledger.balance(uid, funding), order_raw=settings[size_key],
                main_context=snapshot.get("main_market_context", {}))
            self.record_execution_check(uid, verdict, "proposal", action, symbol or "")
            if not verdict.allowed:
                note(verdict.code, verdict.reason)
                continue
            if action == "SELL":
                position = next((p for p in held if p["symbol"] == symbol), None)
                if position and self.ledger.ai_claim(snapshot["id"], uid, mode):
                    await self.observed_ai_trade(uid, snapshot, mode, session, uid, position["asset"],
                        funding, position["raw"], "SELL",
                        {"output_asset": funding, "ai_analysis": snapshot["id"],
                         "ai_created_at": snapshot["created_at"]}, position["id"])
                    return
                note("sell_skipped", "SKIPPED: no owned open position in this asset." if not position else
                     "SKIPPED: this analysis was already claimed; no automatic retry.")
            elif action == "BUY":
                if mode == "sniper":
                    if getattr(self, "discovery", None) and not self.discovery.entry_allowed(symbol):
                        note("candidate", "SKIPPED: candidate screening is stale or the asset is no longer eligible.")
                        continue
                    try:
                        await self.verify_sniper_mint(session, symbol)
                    except Exception:
                        note("mint_check", "BLOCKED: on-chain mint verification did not succeed.")
                        raise
                if self.ledger.ai_claim(snapshot["id"], uid, mode):
                    watch = self.watchlist[symbol]
                    await self.observed_ai_trade(uid, snapshot, mode, session, uid, funding,
                        asset_for_mint(watch["mint"]), settings[size_key], "BUY",
                        {"symbol": symbol, "output_asset": asset_for_mint(watch["mint"]),
                         "decimals": watch["decimals"], "ai_analysis": snapshot["id"],
                         "ai_created_at": snapshot["created_at"]})
                    return
                note("already_claimed", "SKIPPED: this analysis was already claimed; no automatic retry.")

    async def observed_ai_trade(self, uid, snapshot, mode, *args):
        activity_log.record(self, uid, snapshot, mode, "execution_check",
            "EXECUTION CHECK: evaluating an AI order. This is not confirmation of a broadcast or completed trade.")
        try:
            return await self.trade(*args)
        except EntryCostError as exc:
            activity_log.record(self, uid, snapshot, mode, "entry_cost_skip", str(exc))
            return None
        except Exception as exc:
            activity_log.record(self, uid, snapshot, mode, "execution_error",
                "EXECUTION INTERRUPTED: " + operation_failure_reason(exc) +
                ". Check the transaction status below; do not resend an unknown transaction.")
            raise
