"""Public multi-user custody routes. All user ownership comes from verified Telegram."""
import asyncio
import base64
import json
import math
import os
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import aiohttp
from aiohttp import web
from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.message import Message, to_bytes_versioned
from solders.pubkey import Pubkey
from solders.system_program import transfer, TransferParams
from solders.transaction import Transaction, VersionedTransaction

from wallet_runtime import WalletRuntime, WalletError, amount_raw, associated, TOKEN, ATA, SYSTEM, MEMO, GENESIS
from custody_ledger import CustodyLedger, LedgerError, USDC, SOL, FEE_CAP, available, position_account
from legacy_retirement import prove_retirement, RECOVERY_RPC
from tx_deposits import incoming_amounts
from solders.signature import Signature
import ai_decisions
import activity_log
from meme_discovery import MemeDiscovery

WRAPPED_SOL = "So11111111111111111111111111111111111111112"


def asset_for_mint(mint):
    return SOL if mint == WRAPPED_SOL else mint


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
    def __init__(self, *args, base_url, **kwargs):
        super().__init__(*args, **kwargs)
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
        try:
            async with aiohttp.ClientSession() as session:
                await self.discovery.scan(session)
            self.restore_catalog()
        except Exception:
            self.discovery.fail()
        finally:
            self.discovery_due = time.time() + 900

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

    async def current_prices(self, session):
        prices = await self.prices_fn(session)
        if getattr(self, "discovery", None):
            try:
                prices.update(await self.discovery.prices(session, self.held_sniper_symbols()))
            except Exception:
                self.discovery.fail()
        return prices

    def ai_universes(self):
        discovery = getattr(self, "discovery", None)
        return {"main": getattr(self, "main_symbols", [s for s in self.watchlist if s not in self.sniper_symbols]),
                "sniper": list(dict.fromkeys(
                    [row["code"] for row in discovery.candidates()] + sorted(self.held_sniper_symbols())[:32]
                )) if discovery else self.sniper_symbols}

    async def ai_snapshot(self, session, prices):
        config = ai_decisions.configuration()
        if not config["configured"]:
            raise WalletError("Добавьте OPENAI_API_KEY в Environment на Render. AI-торговля пока не запущена.", 503)
        async with self.ai_lock:
            latest = self.ledger.ai_latest()
            if latest and time.time() - latest["created_at"] < config["interval_seconds"]:
                record = json.loads(latest["decisions"])
                ai_decisions.validate_decisions(record["decisions"], record.get("universes", self.ai_universes()))
                return {**latest, **record}
            universes = self.ai_universes()
            wanted = set(universes["main"] + universes["sniper"]) | {"SOL"}
            market = {symbol: ((self.history.get(symbol) if
                      time.time()-getattr(self, "history_seen", {}).get(symbol, 0) <= 90 else None)
                      or [prices[symbol]])[-30:]
                      for symbol in wanted if prices.get(symbol, 0) > 0}
            screening = {row["code"]: {key: row[key] for key in (
                "liquidity_usd", "volume_24h_usd", "transactions_24h", "age_hours",
                "change_m5_pct", "change_h1_pct")} for row in self.discovery.candidates()
                } if getattr(self, "discovery", None) else {}
            try:
                aid = self.ledger.ai_allow_call(config["interval_seconds"], config["max_calls_per_day"])
                decisions = await ai_decisions.analyze(session, market, universes, screening)
            except (ai_decisions.AIError, LedgerError) as exc:
                raise WalletError(str(exc), 503) from None
            except (aiohttp.ClientError, asyncio.TimeoutError):
                raise WalletError("Нет безопасного ответа AI API. Новые входы остановлены.", 503) from None
            record = {"decisions": decisions, "universes": universes, "prices": {s: h[-1] for s, h in market.items()},
                      "samples": {s: len(h) for s, h in market.items()}}
            self.ledger.ai_record(aid, record)
            return {"id": aid, "created_at": int(time.time()), **record}

    def ai_status(self):
        latest = self.ledger.ai_latest()
        return {**ai_decisions.configuration(), "last_analyzed_at": latest["created_at"] if latest else None,
                "last_decisions": json.loads(latest["decisions"])["decisions"] if latest else None,
                "sniper_assets": self.ai_universes()["sniper"],
                "asset_labels": {s: info.get("label", s) + (" · "+info["mint"][:4]+"…"+info["mint"][-4:]
                                 if s.startswith("M_") else "") for s, info in self.watchlist.items()},
                "discovery": self.discovery.report if getattr(self, "discovery", None) else None}

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
                raise WalletError("Операции приостановлены: остатки кошелька не покрывают реестр.")
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

    def operator_fees(self, uid):
        if uid != self.owner_id:
            return None
        with self.ledger.db() as db:
            row = db.execute("SELECT raw FROM custody_accounts WHERE account='treasury' AND asset=?", (SOL,)).fetchone()
        raw = row["raw"] if row else 0
        return {"reserve_sol": display_raw(raw, 9), "minimum_sol": display_raw(FEE_CAP, 9),
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

    async def recovery_rpc(self, session, method, params):
        # Fixed, independently verified mainnet endpoint; keys/credentials never sent.
        async with session.post(RECOVERY_RPC, json={"jsonrpc": "2.0", "id": 1,
                "method": method, "params": params}, timeout=aiohttp.ClientTimeout(total=8)) as response:
            if response.status != 200:
                raise WalletError("RPC восстановления недоступен; запись не изменена.")
            result = await response.json()
        if "error" in result or "result" not in result:
            raise WalletError("RPC не предоставил доказательства для восстановления; запись не изменена.")
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
                continue
            if status.get("err") is not None:
                # Failed transactions still pay a fee. Fetch it, charge only operator treasury.
                tx = await self.rpc(session, "getTransaction", [row["signature"], {
                    "encoding": "jsonParsed", "commitment": "finalized", "maxSupportedTransactionVersion": 0}])
                if status.get("confirmationStatus") != "finalized" or not tx:
                    continue
                fee = int(tx["meta"]["fee"])
                if fee > row["fee_cap"]:
                    raise WalletError("Комиссия ошибочной транзакции превышает резерв.")
                self.ledger.fail(row["id"], network_proven=True, fee=fee)
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
            if row["kind"] == "WITHDRAW":
                spent = row["raw"]
                asset_delta = self.effects(tx, self.wallet, row["asset"])
                if row["asset"] == USDC and asset_delta != -spent:
                    raise WalletError("Результат вывода USDC не соответствует резерву.")
                fee = -native_delta - (spent if row["asset"] == SOL else 0)
                if fee < int(tx["meta"]["fee"]):
                    raise WalletError("Не удалось установить фактическую комиссию вывода.")
                received = 0
            else:
                spent = -self.effects(tx, self.wallet, row["asset"])
                received = self.effects(tx, self.wallet, info["output_asset"])
                fee = int(tx["meta"]["fee"])
                # Economic SOL delta is actual native+owned-account change excluding network fee.
                economic_sol = self.effects(tx, self.wallet, SOL)
                trade_sol = (received if info["output_asset"] == SOL else 0) - (spent if row["asset"] == SOL else 0)
                if economic_sol != trade_sol:
                    raise WalletError("Неожиданное изменение SOL при swap.")
                fee = trade_sol - native_delta - self.wrapped_delta(tx, self.wallet)
            self.ledger.finalize(row["id"], spent, fee, received)

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
        """Constrain all custodian-owned SPL accounts, not just quote amounts."""
        tokens = (await self.rpc(session, "getTokenAccountsByOwner", [
            self.wallet, {"programId": str(TOKEN)}, {"encoding": "base64", "commitment": "confirmed"}]))["value"]
        addresses = [self.wallet] + [item["pubkey"] for item in tokens]
        for asset in {row["asset"], info.get("output_asset")}:
            if asset and asset != SOL:
                address = str(associated(Pubkey.from_string(self.wallet), Pubkey.from_string(asset)))
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
        if row["kind"] == "WITHDRAW":
            expected = -row["raw"]
            if row["asset"] == USDC and deltas.get(USDC, 0) != expected:
                raise WalletError("Симуляция вывода USDC не соответствует сумме.")
            expense = -native_delta - (row["raw"] if row["asset"] == SOL else 0)
            if expense < fee or expense > row["fee_cap"]:
                raise WalletError("Расход SOL на вывод выходит за резерв комиссий.")
            permitted = {row["asset"], SOL}
        else:
            spent, acquired = -deltas.get(row["asset"], 0), deltas.get(info["output_asset"], 0)
            if spent <= 0 or spent > row["raw"] or acquired <= 0 or acquired < int(info["minimum_out"]):
                raise WalletError("Симуляция swap выходит за сумму пользователя или slippage.")
            if row["kind"] == "SELL" and spent != row["raw"]:
                raise WalletError("Частичная продажа не соответствует запросу.")
            trade_sol = (acquired if info["output_asset"] == SOL else 0) - (spent if row["asset"] == SOL else 0)
            expense = trade_sol - native_delta - wrapped_delta
            if expense > row["fee_cap"]:
                raise WalletError("Swap расходует слишком большой резерв комиссий.")
            permitted = {row["asset"], info["output_asset"], SOL}
        if any(delta for asset, delta in deltas.items() if asset not in permitted):
            raise WalletError("Симуляция затрагивает посторонние пользовательские активы.")
        return wire

    async def send_reserved(self, session, tx, row, info):
        try:
            wire = await self.safe_simulation(session, tx, row, info)
            if getattr(self, "web_access", None) and row["kind"] in ("BUY", "SELL"):
                self.web_access.require(row["uid"], row["kind"], row.get("position_id"),
                                        manual=info.get("manual_close") is True)
            signature = str(tx.signatures[0])
            self.ledger.prepared(row["id"], signature, wire)
        except Exception:
            # Still RESERVED: no possible broadcast; safe to return its own reservation.
            self.ledger.fail(row["id"])
            raise
        try:
            returned = await self.rpc(session, "sendTransaction", [wire, {
                "encoding": "base64", "skipPreflight": False, "maxRetries": 0}])
            self.ledger.submitted(row["id"], returned == signature)
        except Exception:
            self.ledger.submitted(row["id"], False)
        try:
            await self.reconcile(session)
        except Exception:
            pass
        return self.ledger.operation(row["id"])

    def result(self, row):
        state = row["state"]
        return web.json_response({"success": state != "FAILED", "state": state,
            "operation_id": row["id"], "tx_signature": row["signature"],
            "message": "Подтверждено сетью." if state == "COMPLETED" else
                "Операция не выполнена. Ваш резерв возвращён." if state == "FAILED" else
                "Сумма зарезервирована на вашем счёте. Ожидается подтверждение; не повторяйте перевод."
        }, status=200 if state == "COMPLETED" else 409 if state == "FAILED" else 202)

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
        valid, state = self.pin_fn(uid, str(body.get("pin", "")))
        if not valid:
            raise WalletError("Неверный PIN или код заблокирован.", 423 if state == "locked" else 401)
        async with self.lock:
            previous = self.ledger.find_request(uid, request_id)
            if previous:
                if (previous["kind"], previous["asset"], previous["raw"], previous["recipient"]) != (
                        "WITHDRAW", asset, raw, str(recipient)):
                    raise WalletError("Идентификатор относится к другому переводу.", 409)
                return self.result(previous)
            await self.reconcile(session)
            if self.pending():
                raise WalletError("Кошелёк ожидает подтверждения. Ваши средства не списаны.", 409)
            await self.solvent(session)
            if getattr(self, "accounts", None):
                self.accounts.consume(uid, body)
            row = self.ledger.reserve(uid, request_id, "WITHDRAW", asset, raw, {}, str(recipient))
            try:
                tx = await super().withdraw_transaction(session, self.ready(), token, recipient, raw, row["id"])
            except Exception:
                self.ledger.fail(row["id"])
                raise
            return self.result(await self.send_reserved(session, tx, row, {}))

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
        row = self.ledger.reserve(uid, str(uuid.uuid4()), kind, asset, raw, metadata, position_id=position_id)
        try:
            if await self.rpc(session, "getGenesisHash") != GENESIS:
                raise WalletError("Только Solana mainnet.")
            headers = {"Accept": "application/json"}
            if os.getenv("JUPITER_API_KEY"):
                headers["x-api-key"] = os.environ["JUPITER_API_KEY"]
            mint_in = WRAPPED_SOL if asset == SOL else asset
            mint_out = WRAPPED_SOL if output_asset == SOL else output_asset
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
            info = {**metadata, "minimum_out": int(quote["otherAmountThreshold"]), "manual_close": manual}
            async with session.post("https://api.jup.ag/swap/v1/swap", headers=headers, json={
                "quoteResponse": quote, "userPublicKey": self.wallet, "wrapAndUnwrapSol": True,
                "prioritizationFeeLamports": 25000}) as response:
                if response.status != 200:
                    raise WalletError("Jupiter swap недоступен.")
                data = await response.json()
            unsigned = VersionedTransaction.from_bytes(base64.b64decode(data["swapTransaction"]))
            if str(unsigned.message.account_keys[0]) != self.wallet or unsigned.message.header.num_required_signatures != 1:
                raise WalletError("Неверный подписант Jupiter.")
            if getattr(self, "web_access", None):
                self.web_access.require(uid, kind, position_id, manual)
            transaction = VersionedTransaction(unsigned.message, [self.ready()])
        except Exception:
            self.ledger.fail(row["id"])
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
                "SELECT * FROM custody_operations WHERE uid=? ORDER BY created_at DESC LIMIT 20", (uid,))]
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
            profit = ((p["proceeds_raw"] or 0) - p["cost_raw"]) / scale if p["state"] == "CLOSED" else 0
            trades.append({"token_symbol": self.watchlist.get(p["symbol"], {}).get("label", p["symbol"]), "symbol": p["symbol"],
                "token_mint": WRAPPED_SOL if p["asset"] == SOL else p["asset"],
                "amount_tokens": qty, "buy_price": cost / qty,
                "sell_price": (p["proceeds_raw"] or 0) / scale / qty if p["state"] == "CLOSED" else 0,
                "profit_usdc": profit if not native else 0, "profit_units": profit,
                "funding_currency": "SOL" if native else "USDC", "profit_pct": profit / cost * 100,
                "timestamp": datetime.fromtimestamp(p["created_at"], timezone.utc).isoformat(),
                "buy_tx": p["buy_tx"], "sell_tx": p["sell_tx"], "status": p["state"]})
        for row in operations:
            if row["kind"] == "WITHDRAW":
                history.append({"type": "WITHDRAW", "amount": display_raw(row["raw"], 6 if row["asset"] == USDC else 9) +
                    (" USDC" if row["asset"] == USDC else " SOL"), "tx": row["signature"],
                    "time": datetime.fromtimestamp(row["created_at"], timezone.utc).isoformat(), "status": row["state"]})
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
        profit = sum(t["profit_usdc"] for t in trades)
        settings = self.ledger.settings(uid)
        own_pending = [r for r in operations if r["state"] in ("RESERVED", "PREPARED", "SUBMITTED", "UNKNOWN")]
        return web.json_response({"success": True, "username": request["telegram_user"].get("username", "user"),
            "user_balance": user_usdc, "sol_balance": user_sol, "withdrawable": user_usdc,
            "withdrawable_sol": user_sol, "total_deposited": (sum(i["raw"] for i in invoices if i["asset"] == USDC)
                + sum(json.loads(r["amounts"]).get(USDC, 0) for r in tx_deposits))/1e6,
            "total_pnl": profit, "profit_usdc": profit, "total_roi": 0,
            "closed_count": sum(p["state"] == "CLOSED" for p in positions), "trades": trades,
            "wallet_history": history[:20], "wallet_status": {**self.readiness(),
                "finance_allowed": self.public_enabled or uid in self.beta_users},
            "operation_pending": bool(own_pending), "ledger_audited": True,
            "trading_active": bool(settings["trading"]), "is_admin": uid == self.owner_id,
            "operator_fees": self.operator_fees(uid),
            "sniper_active": bool(settings["sniper_trading"]),
            "sniper_settings": {"order_sol": settings["sniper_order_raw"]/1e9, "max_positions": 1},
            "profit_sol": sum(t["profit_units"] for t in trades if t["funding_currency"] == "SOL"),
            "ai_status": self.ai_status(),
            "trading_settings": {"order_usdc": settings["order_raw"]/1e6,
                "max_positions": 5, "take_profit_pct": 3.5, "stop_loss_pct": 2,
                "slippage_pct": .4, "entry_dip_pct": .3, "poll_seconds": 20,
                "assets": self.ai_universes()["main"], "warmup_samples": 5},
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
        self.ledger.settings(uid, enabled=False, sniper_enabled=False, close=True)
        return web.json_response({"success": True, "message": "Закрытие ваших подтверждённых позиций поставлено в очередь."})

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

    async def worker(self):
        # Settings are paused on restart. No agent-triggered trades or blind retries.
        while True:
            try:
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=35)) as session:
                    if getattr(self, "web_access", None):
                        self.web_access.pause_expired()
                    await self.reconcile(session)
                    await self.scan_deposits(session)
                    users = self.ledger.active_users()
                    if users:
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
                                target = next((p for p in positions if current["close_requested"] or (
                                    (current["sniper_trading"] if p["funding_asset"] == SOL else current["trading"])
                                    and self.exit_triggered(p, prices))), None)
                                if target:
                                    await self.trade(session, uid, target["asset"], target["funding_asset"], target["raw"], "SELL",
                                        {"output_asset": target["funding_asset"]}, target["id"],
                                        manual=bool(current["close_requested"]))
                                elif current["close_requested"]:
                                    self.ledger.settings(uid, close=False)
                                elif snapshot:
                                    await self.apply_ai_decision(session, uid, current, positions, prices, snapshot)
            except Exception as exc:
                message = str(exc) if isinstance(exc, (WalletError, LedgerError)) else type(exc).__name__
                if message != self.last_error:
                    self.log("Custody worker: " + message)
                    self.last_error = message
            await asyncio.sleep(20)

    def exit_triggered(self, position, prices):
        price = prices.get(position["symbol"], 0)
        if not math.isfinite(price) or price <= 0:
            return False
        native = position["funding_asset"] == SOL
        if native:
            if not math.isfinite(prices.get("SOL", 0)) or prices.get("SOL", 0) <= 0:
                return False
            price /= prices["SOL"]
            if not math.isfinite(price) or price <= 0:
                return False
        entry = position["cost_raw"] / (1e9 if native else 1e6) / (position["raw"]/10**position["decimals"])
        change = price / entry - 1
        return change >= .035 or change <= -.020

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
        if time.time() - snapshot["created_at"] > 120:
            for mode, flag in (("main", "trading"), ("sniper", "sniper_trading")):
                if settings[flag]:
                    activity_log.observe(self, uid, snapshot, mode)
                    activity_log.record(self, uid, snapshot, mode, "stale",
                        "SKIPPED: AI decision is older than 120 seconds. Waiting for a fresh analysis.")
            return
        for mode, funding, flag, size_key, cap in [
                ("main", USDC, "trading", "order_raw", 5),
                ("sniper", SOL, "sniper_trading", "sniper_order_raw", 1)]:
            if not settings[flag]:
                continue
            activity_log.observe(self, uid, snapshot, mode)
            def note(code, message):
                activity_log.record(self, uid, snapshot, mode, code, message)
            decision = snapshot["decisions"][mode]
            symbol, action = decision["symbol"], decision["action"]
            held = [p for p in positions if p["funding_asset"] == funding]
            if action == "HOLD":
                note("hold", "WAIT: AI chose HOLD. No transaction requested.")
                continue
            original = snapshot["prices"].get(symbol, 0)
            price = prices.get(symbol, 0)
            if (not math.isfinite(original) or not math.isfinite(price)
                    or original <= 0 or price <= 0 or abs(price/original-1) > .01):
                note("price", "SKIPPED: price data is missing/invalid or moved more than 1% since analysis.")
                continue
            if funding == SOL:
                old_sol, now_sol = snapshot["prices"].get("SOL", 0), prices.get("SOL", 0)
                if (not math.isfinite(old_sol) or not math.isfinite(now_sol)
                        or old_sol <= 0 or now_sol <= 0):
                    note("sol_price", "SKIPPED: valid SOL reference prices are unavailable.")
                    continue
                relative_now, relative_old = price/now_sol, original/old_sol
                if (not math.isfinite(relative_now) or not math.isfinite(relative_old)
                        or relative_now <= 0 or relative_old <= 0
                        or abs(relative_now/relative_old-1) > .01):
                    note("relative_price", "SKIPPED: token/SOL price drift exceeds the 1% safety limit.")
                    continue
            if action == "SELL":
                position = next((p for p in held if p["symbol"] == symbol), None)
                if position and self.ledger.ai_claim(snapshot["id"], uid, mode):
                    await self.observed_ai_trade(uid, snapshot, mode, session, uid, position["asset"],
                        funding, position["raw"], "SELL",
                        {"output_asset": funding, "ai_analysis": snapshot["id"]}, position["id"])
                    return
                note("sell_skipped", "SKIPPED: no owned open position in this asset." if not position else
                     "SKIPPED: this analysis was already claimed; no automatic retry.")
            elif (snapshot.get("samples", {}).get(symbol, 0) >= 5
                    and (funding != SOL or snapshot.get("samples", {}).get("SOL", 0) >= 5) and len(held) < cap
                    and not any(p["symbol"] == symbol for p in held)
                    and self.ledger.balance(uid, funding) >= settings[size_key]):
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
                         "decimals": watch["decimals"], "ai_analysis": snapshot["id"]})
                    return
                note("already_claimed", "SKIPPED: this analysis was already claimed; no automatic retry.")
            else:
                if snapshot.get("samples", {}).get(symbol, 0) < 5 or (
                        funding == SOL and snapshot.get("samples", {}).get("SOL", 0) < 5):
                    note("warmup", "SKIPPED: fewer than five price samples; waiting for market-data warm-up.")
                elif len(held) >= cap:
                    note("position_limit", "SKIPPED: this mode's open-position limit has been reached.")
                elif any(p["symbol"] == symbol for p in held):
                    note("already_open", "SKIPPED: you already hold an open position in this asset.")
                else:
                    note("balance", "SKIPPED: your available balance is below the saved purchase size.")

    async def observed_ai_trade(self, uid, snapshot, mode, *args):
        activity_log.record(self, uid, snapshot, mode, "execution_check",
            "EXECUTION CHECK: evaluating an AI order. This is not confirmation of a broadcast or completed trade.")
        try:
            return await self.trade(*args)
        except Exception:
            activity_log.record(self, uid, snapshot, mode, "execution_error",
                "EXECUTION INTERRUPTED: a safety check or execution step failed. Check readiness and transaction "
                "status below; do not resend an unknown transaction.")
            raise
