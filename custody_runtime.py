"""Public multi-user custody routes. All user ownership comes from verified Telegram."""
import asyncio
import base64
import json
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
        self.base_url = base_url.rstrip("/")
        self.public_enabled = os.getenv("PUBLIC_CUSTODY_ENABLED", "").lower() == "true"
        beta = os.getenv("CUSTODY_BETA_USERS", "").split(",")
        self.beta_users = {self.owner_id} | {int(value.strip()) for value in beta if value.strip()}
        self.ledger.restart_pause()

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
        return {"invoice_id": invoice["id"], "memo": "ZL1:" + invoice["id"],
            "token": token, "amount": display_raw(invoice["raw"], 6 if token == "USDC" else 9),
            "deposit_wallet": self.wallet, "expires_at": invoice["expires_at"],
            "state": invoice["state"], "payment_url": self.base_url + "/deposit/" + invoice["id"]}

    async def create_invoice(self, request, session):
        uid = self.uid(request)
        self.admit(uid)
        if uid != self.owner_id:
            with self.ledger.db() as db:
                fees = db.execute("SELECT raw FROM custody_accounts WHERE account='treasury' AND asset=?", (SOL,)).fetchone()
                if not fees or fees["raw"] < FEE_CAP:
                    raise WalletError("Новые пополнения приостановлены: оператору нужно пополнить резерв комиссий.")
        if await self.rpc(session, "getGenesisHash") != GENESIS:
            raise WalletError("Разрешена только Solana mainnet.")
        body = await request.json()
        token = str(body.get("token", "")).upper()
        if token not in ("USDC", "SOL"):
            raise WalletError("Выберите USDC или SOL.", 400)
        raw = amount_raw(body.get("amount", 0), 6 if token == "USDC" else 9)
        if raw > 2**63 - 1:
            raise WalletError("Сумма вне диапазона реестра.", 400)
        invoice = self.ledger.invoice(uid, USDC if token == "USDC" else SOL, raw)
        return web.json_response({"success": True, **self.public_invoice(invoice)})

    async def deposit(self, request, session):
        self.admit(self.uid(request))
        body = await request.json()
        iid = str(body.get("invoice_id", ""))
        invoice = self.ledger.get_invoice(iid)
        if not invoice or invoice["uid"] != self.uid(request):
            raise WalletError("Это не ваш платёжный запрос.", 403)
        await self.apply_deposit(session, str(body.get("tx_signature", "")), iid)
        return web.json_response({"success": True, "message": "Пополнение вашего счёта подтверждено."})

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
        # SPL Memo requires every listed account to sign. The custodian does NOT sign a deposit.
        instructions.append(Instruction(MEMO, ("ZL1:" + iid).encode(), [
            AccountMeta(sender, True, False)]))
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
        return web.json_response({"success": True, "message": "Ваши SOL выделены на комиссии оператора. Монеты не отправлялись; чужие остатки не менялись."})

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
            row = self.ledger.reserve(uid, request_id, "WITHDRAW", asset, raw, {}, str(recipient))
            try:
                tx = await super().withdraw_transaction(session, self.ready(), token, recipient, raw, row["id"])
            except Exception:
                self.ledger.fail(row["id"])
                raise
            return self.result(await self.send_reserved(session, tx, row, {}))

    async def trade(self, session, uid, asset, output_asset, raw, kind, metadata, position_id=None):
        self.admit(uid)
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
            info = {**metadata, "minimum_out": int(quote["otherAmountThreshold"])}
            async with session.post("https://api.jup.ag/swap/v1/swap", headers=headers, json={
                "quoteResponse": quote, "userPublicKey": self.wallet, "wrapAndUnwrapSol": True,
                "prioritizationFeeLamports": 25000}) as response:
                if response.status != 200:
                    raise WalletError("Jupiter swap недоступен.")
                data = await response.json()
            unsigned = VersionedTransaction.from_bytes(base64.b64decode(data["swapTransaction"]))
            if str(unsigned.message.account_keys[0]) != self.wallet or unsigned.message.header.num_required_signatures != 1:
                raise WalletError("Неверный подписант Jupiter.")
            transaction = VersionedTransaction(unsigned.message, [self.ready()])
        except Exception:
            self.ledger.fail(row["id"])
            raise
        return await self.send_reserved(session, transaction, row, info)

    async def stats(self, request, session):
        uid = self.uid(request)
        await self.reconcile(session)
        self.ledger.audit()
        positions = self.ledger.positions(uid)
        with self.ledger.db() as db:
            operations = [dict(r) for r in db.execute(
                "SELECT * FROM custody_operations WHERE uid=? ORDER BY created_at DESC LIMIT 20", (uid,))]
            invoices = [dict(r) for r in db.execute(
                "SELECT * FROM custody_invoices WHERE uid=? AND state='CREDITED' ORDER BY created_at DESC LIMIT 20", (uid,))]
        trades, history = [], []
        for p in positions:
            qty = p["raw"] / 10**p["decimals"]
            cost = p["cost_usdc"] / 1e6
            profit = ((p["proceeds_usdc"] or 0) - p["cost_usdc"]) / 1e6 if p["state"] == "CLOSED" else 0
            trades.append({"token_symbol": p["symbol"], "symbol": p["symbol"],
                "token_mint": WRAPPED_SOL if p["asset"] == SOL else p["asset"],
                "amount_tokens": qty, "buy_price": cost / qty,
                "sell_price": (p["proceeds_usdc"] or 0) / 1e6 / qty if p["state"] == "CLOSED" else 0,
                "profit_usdc": profit, "profit_pct": profit / cost * 100,
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
        history.sort(key=lambda r: r["time"], reverse=True)
        user_usdc, user_sol = self.ledger.balance(uid, USDC) / 1e6, self.ledger.balance(uid, SOL) / 1e9
        profit = sum(t["profit_usdc"] for t in trades)
        settings = self.ledger.settings(uid)
        own_pending = [r for r in operations if r["state"] in ("RESERVED", "PREPARED", "SUBMITTED", "UNKNOWN")]
        return web.json_response({"success": True, "username": request["telegram_user"].get("username", "user"),
            "user_balance": user_usdc, "sol_balance": user_sol, "withdrawable": user_usdc,
            "withdrawable_sol": user_sol, "total_deposited": sum(i["raw"] for i in invoices if i["asset"] == USDC)/1e6,
            "total_pnl": profit, "profit_usdc": profit, "total_roi": 0,
            "closed_count": sum(p["state"] == "CLOSED" for p in positions), "trades": trades,
            "wallet_history": history[:20], "wallet_status": {**self.readiness(),
                "finance_allowed": self.public_enabled or uid in self.beta_users},
            "operation_pending": bool(own_pending), "ledger_audited": True,
            "trading_active": bool(settings["trading"]), "is_admin": uid == self.owner_id})

    async def logs(self, request, session):
        uid = self.uid(request)
        with self.ledger.db() as db:
            rows = db.execute("SELECT kind,state,created_at FROM custody_operations WHERE uid=? ORDER BY created_at DESC LIMIT 15", (uid,))
            logs = [r["kind"] + ": " + r["state"] for r in rows]
        return web.json_response({"success": True, "logs": logs,
            "trading_active": bool(self.ledger.settings(uid)["trading"]), "meme_active": False})

    async def toggle(self, request, session):
        uid = self.uid(request)
        self.admit(uid)
        settings = self.ledger.settings(uid)
        if settings["trading"]:
            self.ledger.settings(uid, enabled=False)
        else:
            body = await request.json()
            raw = amount_raw(body.get("trade_amount_usdc", "1"), 6)
            if raw > self.ledger.balance(uid, USDC) and not self.ledger.positions(uid, "OPEN"):
                raise WalletError("Размер покупки превышает ваш собственный доступный USDC-баланс.", 409)
            self.ledger.settings(uid, enabled=True, raw=raw)
        return web.json_response({"success": True, "trading_active": bool(self.ledger.settings(uid)["trading"])})

    async def close_all(self, request, session):
        uid = self.uid(request)
        self.admit(uid)
        body = await request.json()
        if body.get("confirm") is not True:
            raise WalletError("Подтвердите закрытие только своих позиций.", 400)
        self.ledger.settings(uid, enabled=False, close=True)
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
                    await self.reconcile(session)
                    await self.scan_deposits(session)
                    users = self.ledger.active_users()
                    if users:
                        prices = await self.prices_fn(session)
                        for symbol, price in prices.items():
                            if symbol in self.history and price > 0:
                                self.history[symbol] = (self.history[symbol] + [price])[-10:]
                        for settings in users:
                            uid = settings["uid"]
                            async with self.lock:
                                if self.pending():
                                    break
                                current = self.ledger.settings(uid)
                                positions = self.ledger.positions(uid, "OPEN")
                                target = next((p for p in positions if current["close_requested"] or (
                                    prices.get(p["symbol"], 0) > 0 and (
                                    prices[p["symbol"]] / (p["cost_usdc"]/1e6/(p["raw"]/10**p["decimals"])) - 1 >= .035
                                    or prices[p["symbol"]] / (p["cost_usdc"]/1e6/(p["raw"]/10**p["decimals"])) - 1 <= -.020))), None)
                                if target and (current["trading"] or current["close_requested"]):
                                    await self.trade(session, uid, target["asset"], USDC, target["raw"], "SELL",
                                        {"output_asset": USDC}, target["id"])
                                elif current["close_requested"]:
                                    self.ledger.settings(uid, close=False)
                                elif current["trading"] and len(positions) < 5:
                                    owned = {p["symbol"] for p in positions}
                                    dips = [(1 - h[-1] / (sum(h)/len(h)), symbol) for symbol, h in self.history.items()
                                            if len(h) >= 5 and symbol not in owned]
                                    if dips and max(dips)[0] >= .003 and self.ledger.balance(uid, USDC) >= current["order_raw"]:
                                        symbol = max(dips)[1]
                                        watch = self.watchlist[symbol]
                                        await self.trade(session, uid, USDC, asset_for_mint(watch["mint"]), current["order_raw"], "BUY",
                                            {"symbol": symbol, "output_asset": asset_for_mint(watch["mint"]), "decimals": watch["decimals"]})
            except Exception as exc:
                message = str(exc) if isinstance(exc, (WalletError, LedgerError)) else type(exc).__name__
                if message != self.last_error:
                    self.log("Custody worker: " + message)
                    self.last_error = message
            await asyncio.sleep(20)
