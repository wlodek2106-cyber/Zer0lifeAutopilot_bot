"""Single-owner wallet operations. Chain balances, persistent signatures, no virtual credits."""
import asyncio
import base64
import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation

import aiohttp
from aiohttp import web
from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.message import Message
from solders.pubkey import Pubkey
from solders.system_program import transfer, TransferParams
from solders.transaction import Transaction, VersionedTransaction

USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SOL = "So11111111111111111111111111111111111111112"
TOKEN = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
ATA = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")
SYSTEM = Pubkey.from_string("11111111111111111111111111111111")
MEMO = Pubkey.from_string("MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr")
GENESIS = "5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d"


class WalletError(Exception):
    def __init__(self, message, status=503):
        super().__init__(message)
        self.status = status


def amount_raw(value, decimals):
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount <= 0 or amount * 10**decimals != (amount * 10**decimals).to_integral_value():
            raise ValueError()
        raw = int(amount * 10**decimals)
        if raw > 2**64 - 1:
            raise ValueError()
        return raw
    except (ValueError, InvalidOperation, OverflowError):
        raise WalletError("Некорректная сумма или число знаков после запятой.", 400)


def associated(wallet, mint):
    return Pubkey.find_program_address([bytes(wallet), bytes(TOKEN), bytes(mint)], ATA)[0]


def durable(path):
    mount = "/var/data"
    if not os.path.realpath(path).startswith(mount + "/"):
        return False
    if os.path.ismount(mount):
        return True
    try:
        with open("/proc/self/mountinfo") as stream:
            return any(line.split()[4] == mount for line in stream)
    except OSError:
        return False


class WalletRuntime:
    def __init__(self, db_file, rpc, owner_id, wallet, signer, pin, prices, watchlist, log):
        self.db_file, self.rpc_url = db_file, rpc
        self.owner_id, self.wallet = int(owner_id), wallet
        self.signer_fn, self.pin_fn, self.prices_fn = signer, pin, prices
        self.watchlist, self.log = watchlist, log
        self.lock = asyncio.Lock()
        self.trading = False
        self.close_requested = False
        self.trade_raw = 20_000_000
        self.history = {s: [] for s in watchlist}
        self.last_error = ""

    @contextmanager
    def db(self):
        connection = sqlite3.connect(self.db_file, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS wallet_operations (
                    id TEXT PRIMARY KEY, signature TEXT UNIQUE NOT NULL,
                    kind TEXT NOT NULL, state TEXT NOT NULL, metadata TEXT NOT NULL,
                    effects_applied INTEGER NOT NULL DEFAULT 0,
                    timestamp TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
                CREATE TABLE IF NOT EXISTS wallet_positions (
                    id TEXT PRIMARY KEY, symbol TEXT NOT NULL, mint TEXT NOT NULL,
                    raw_amount INTEGER NOT NULL, cost_usdc REAL NOT NULL,
                    entry_price REAL NOT NULL, state TEXT NOT NULL,
                    buy_tx TEXT NOT NULL, sell_tx TEXT, profit_usdc REAL DEFAULT 0,
                    exit_price REAL DEFAULT 0, timestamp TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
            """)
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def owner(self, request):
        if int(request["telegram_user"]["id"]) != self.owner_id:
            raise WalletError("Это личный бот владельца.", 403)

    def readiness(self):
        signer = self.signer_fn()
        key_ok = signer is not None and str(signer.pubkey()) == self.wallet
        disk_ok = durable(self.db_file)
        return {"wallet_configured": key_ok, "persistent_storage": disk_ok}

    def ready(self):
        flags = self.readiness()
        if not flags["wallet_configured"]:
            raise WalletError("Серверный ключ отсутствует или не соответствует DEPOSIT_WALLET.")
        if not flags["persistent_storage"]:
            raise WalletError("Нужен Persistent Disk /var/data: без него нельзя надёжно защититься от повторной отправки.")
        return self.signer_fn()

    async def rpc(self, session, method, params=None):
        async with session.post(self.rpc_url, json={
            "jsonrpc": "2.0", "id": 1, "method": method, "params": params or []
        }) as response:
            if response.status != 200:
                raise WalletError("Solana RPC недоступен.")
            result = await response.json()
        if "error" in result:
            raise WalletError("Solana RPC: " + str(result["error"].get("message", "error")))
        if "result" not in result:
            raise WalletError("Неполный ответ Solana RPC.")
        return result["result"]

    async def balances(self, session):
        balance = await self.rpc(session, "getBalance", [self.wallet, {"commitment": "confirmed"}])
        tokens = await self.rpc(session, "getTokenAccountsByOwner", [
            self.wallet, {"programId": str(TOKEN)}, {"encoding": "jsonParsed", "commitment": "confirmed"}])
        assets = {}
        for item in tokens["value"]:
            info = item["account"]["data"]["parsed"]["info"]
            if info.get("owner") != self.wallet:
                continue
            value = info["tokenAmount"]
            entry = assets.setdefault(info["mint"], {"raw": 0, "decimals": value["decimals"]})
            if info.get("state") == "initialized":
                entry["raw"] += int(value["amount"])
        assets[SOL] = {"raw": balance["value"], "decimals": 9}
        assets.setdefault(USDC, {"raw": 0, "decimals": 6})
        return assets

    def operation(self, operation_id):
        with self.db() as db:
            row = db.execute("SELECT * FROM wallet_operations WHERE id=?", (operation_id,)).fetchone()
            return dict(row) if row else None

    def pending(self):
        with self.db() as db:
            return [dict(r) for r in db.execute(
                "SELECT * FROM wallet_operations WHERE state IN ('PREPARED','SUBMITTED','UNKNOWN')")]

    def reserve(self, operation_id, signature, kind, metadata):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT id FROM wallet_operations WHERE state IN ('PREPARED','SUBMITTED','UNKNOWN')").fetchone():
                raise WalletError("Предыдущая операция ещё не подтверждена. Новая отправка заблокирована.", 409)
            db.execute("INSERT INTO wallet_operations(id,signature,kind,state,metadata) VALUES(?,?,?,'PREPARED',?)",
                       (operation_id, signature, kind, json.dumps(metadata)))

    @staticmethod
    def delta(tx, wallet, mint):
        meta = tx["meta"]
        if mint == SOL:
            keys = tx["transaction"]["message"]["accountKeys"]
            index = next(i for i, key in enumerate(keys)
                         if (key.get("pubkey") if isinstance(key, dict) else key) == wallet)
            return meta["postBalances"][index] - meta["preBalances"][index] + (meta["fee"] if index == 0 else 0)
        def total(name):
            return sum(int(item["uiTokenAmount"]["amount"]) for item in meta.get(name, []) or []
                       if item.get("owner") == wallet and item.get("mint") == mint)
        return total("postTokenBalances") - total("preTokenBalances")

    def apply_effects(self, row, tx):
        info = json.loads(row["metadata"])
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT effects_applied FROM wallet_operations WHERE id=?", (row["id"],)).fetchone()
            if existing["effects_applied"]:
                return
            if row["kind"] == "BUY":
                raw = self.delta(tx, self.wallet, info["mint"])
                cost = -self.delta(tx, self.wallet, USDC) / 1_000_000
                if raw <= 0 or cost <= 0:
                    raise WalletError("Нельзя установить фактический результат покупки; новые сделки остановлены.")
                quantity = raw / 10**info["decimals"]
                db.execute("""INSERT INTO wallet_positions(id,symbol,mint,raw_amount,cost_usdc,entry_price,state,buy_tx)
                    VALUES(?,?,?,?,?,?,'OPEN',?)""",
                    (row["id"], info["symbol"], info["mint"], raw, cost, cost / quantity, row["signature"]))
            elif row["kind"] == "SELL":
                proceeds = self.delta(tx, self.wallet, USDC) / 1_000_000
                position = db.execute("SELECT * FROM wallet_positions WHERE id=?", (info["position_id"],)).fetchone()
                if proceeds <= 0 or position is None:
                    raise WalletError("Нельзя установить фактический результат продажи; новые сделки остановлены.")
                quantity = position["raw_amount"] / 10**info["decimals"]
                db.execute("""UPDATE wallet_positions SET state='CLOSED',sell_tx=?,profit_usdc=?,exit_price=? WHERE id=?""",
                    (row["signature"], proceeds - position["cost_usdc"], proceeds / quantity, position["id"]))
            db.execute("UPDATE wallet_operations SET state='COMPLETED',effects_applied=1 WHERE id=?", (row["id"],))

    async def reconcile(self, session):
        for row in self.pending():
            result = await self.rpc(session, "getSignatureStatuses", [[row["signature"]], {"searchTransactionHistory": True}])
            status = result["value"][0]
            if status is None:
                continue
            if status.get("err") is not None:
                with self.db() as db:
                    db.execute("UPDATE wallet_operations SET state='FAILED' WHERE id=?", (row["id"],))
            elif status.get("confirmationStatus") == "finalized":
                tx = await self.rpc(session, "getTransaction", [row["signature"], {
                    "encoding": "jsonParsed", "commitment": "finalized", "maxSupportedTransactionVersion": 0}])
                if tx and tx.get("meta") and tx["meta"].get("err") is None:
                    self.apply_effects(row, tx)

    async def broadcast(self, session, tx, kind, info, operation_id):
        wire = base64.b64encode(bytes(tx)).decode()
        result = await self.rpc(session, "simulateTransaction", [wire, {
            "encoding": "base64", "sigVerify": True, "commitment": "confirmed"}])
        if result["value"].get("err") is not None:
            raise WalletError("Симуляция отклонена: " + json.dumps(result["value"]["err"]), 409)
        signature = str(tx.signatures[0])
        self.reserve(operation_id, signature, kind, info)
        try:
            returned = await self.rpc(session, "sendTransaction", [wire, {
                "encoding": "base64", "skipPreflight": False, "maxRetries": 0}])
            state = "SUBMITTED" if returned == signature else "UNKNOWN"
        except Exception:
            state = "UNKNOWN"
        with self.db() as db:
            db.execute("UPDATE wallet_operations SET state=? WHERE id=? AND state='PREPARED'", (state, operation_id))
        try:
            await self.reconcile(session)
        except Exception:
            pass
        return self.operation(operation_id)

    async def withdraw_transaction(self, session, signer, token, recipient, raw, operation_id):
        if await self.rpc(session, "getGenesisHash") != GENESIS:
            raise WalletError("Разрешена только сеть Solana mainnet.")
        balances = await self.balances(session)
        mint = SOL if token == "SOL" else USDC
        if raw > balances[mint]["raw"]:
            raise WalletError("Недостаточно реальных средств в кошельке.", 409)
        instructions, rent = [], 0
        instructions.append(Instruction(MEMO, operation_id.encode(),
            [AccountMeta(signer.pubkey(), True, False)]))
        if token == "SOL":
            instructions.append(transfer(TransferParams(
                from_pubkey=signer.pubkey(), to_pubkey=recipient, lamports=raw)))
        else:
            source, destination = associated(signer.pubkey(), Pubkey.from_string(USDC)), associated(recipient, Pubkey.from_string(USDC))
            accounts = (await self.rpc(session, "getMultipleAccounts", [
                [str(source), str(destination)], {"encoding": "jsonParsed", "commitment": "confirmed"}]))["value"]
            for account, owner in ((accounts[0], signer.pubkey()), (accounts[1], recipient)):
                if account is None and owner == recipient:
                    continue
                if not account or account.get("owner") != str(TOKEN):
                    raise WalletError("Не найден подходящий USDC token account.", 409)
                info = account.get("data", {}).get("parsed", {}).get("info", {})
                if info.get("mint") != USDC or info.get("owner") != str(owner) or info.get("state") != "initialized":
                    raise WalletError("Неверный USDC mint, владелец или состояние account.", 409)
            source_info = accounts[0]["data"]["parsed"]["info"]["tokenAmount"]
            if source_info["decimals"] != 6 or int(source_info["amount"]) < raw:
                raise WalletError("Недостаточно USDC на основном ATA.", 409)
            if accounts[1] is None:
                rent = await self.rpc(session, "getMinimumBalanceForRentExemption", [165])
                instructions.append(Instruction(ATA, bytes([1]), [
                    AccountMeta(signer.pubkey(), True, True), AccountMeta(destination, False, True),
                    AccountMeta(recipient, False, False), AccountMeta(Pubkey.from_string(USDC), False, False),
                    AccountMeta(SYSTEM, False, False), AccountMeta(TOKEN, False, False)]))
            instructions.append(Instruction(TOKEN, bytes([12]) + raw.to_bytes(8, "little") + bytes([6]), [
                AccountMeta(source, False, True), AccountMeta(Pubkey.from_string(USDC), False, False),
                AccountMeta(destination, False, True), AccountMeta(signer.pubkey(), True, False)]))
        block = (await self.rpc(session, "getLatestBlockhash", [{"commitment": "confirmed"}]))["value"]
        blockhash = Hash.from_string(block["blockhash"])
        message = Message.new_with_blockhash(instructions, signer.pubkey(), blockhash)
        fee = (await self.rpc(session, "getFeeForMessage", [
            base64.b64encode(bytes(message)).decode(), {"commitment": "confirmed"}]))["value"]
        needed = (raw if token == "SOL" else 0) + rent + (fee or 0) + 5000
        if fee is None or needed > balances[SOL]["raw"]:
            raise WalletError("Оставьте SOL на комиссию и создание USDC account.", 409)
        return Transaction([signer], message, blockhash)

    async def swap(self, session, input_mint, output_mint, raw, kind, info):
        signer = self.ready()
        if await self.rpc(session, "getGenesisHash") != GENESIS:
            raise WalletError("Разрешена только Solana mainnet.")
        if self.pending():
            raise WalletError("Ожидается подтверждение предыдущей операции.", 409)
        balances = await self.balances(session)
        if balances.get(input_mint, {}).get("raw", 0) < raw or balances[SOL]["raw"] < 1_000_000:
            raise WalletError("Недостаточно средств для сделки и комиссии.", 409)
        headers = {"Accept": "application/json"}
        if os.getenv("JUPITER_API_KEY"):
            headers["x-api-key"] = os.environ["JUPITER_API_KEY"]
        async with session.get("https://api.jup.ag/swap/v1/quote", headers=headers, params={
            "inputMint": input_mint, "outputMint": output_mint, "amount": raw, "slippageBps": 40}) as response:
            if response.status != 200:
                raise WalletError("Jupiter quote недоступен: HTTP " + str(response.status))
            quote = await response.json()
        if quote.get("inputMint") != input_mint or quote.get("outputMint") != output_mint or int(quote.get("inAmount", 0)) != raw:
            raise WalletError("Jupiter вернул несовместимую котировку.")
        async with session.post("https://api.jup.ag/swap/v1/swap", headers=headers, json={
            "quoteResponse": quote, "userPublicKey": self.wallet, "wrapAndUnwrapSol": True,
            "prioritizationFeeLamports": 25000}) as response:
            if response.status != 200:
                raise WalletError("Jupiter swap недоступен: HTTP " + str(response.status))
            payload = await response.json()
        unsigned = VersionedTransaction.from_bytes(base64.b64decode(payload["swapTransaction"]))
        if str(unsigned.message.account_keys[0]) != self.wallet or unsigned.message.header.num_required_signatures != 1:
            raise WalletError("Jupiter вернул транзакцию с неверным подписантом.")
        signed = VersionedTransaction(unsigned.message, [signer])
        return await self.broadcast(session, signed, kind, info, str(uuid.uuid4()))

    def reply_operation(self, row):
        complete = row["state"] == "COMPLETED"
        failed = row["state"] == "FAILED"
        return web.json_response({
            "success": not failed, "state": row["state"], "tx_signature": row["signature"],
            "message": "Подтверждено сетью." if complete else
                "Сеть отклонила транзакцию." if failed else
                "Подпись сохранена. Ожидается подтверждение сети; не повторяйте отправку."
        }, status=200 if complete else 409 if failed else 202)

    async def handle(self, request, action):
        try:
            self.owner(request)
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
                return await action(request, session)
        except WalletError as exc:
            return web.json_response({"success": False, "message": str(exc)}, status=exc.status)
        except (ValueError, KeyError, TypeError, json.JSONDecodeError):
            return web.json_response({"success": False, "message": "Неверный запрос или неполный ответ провайдера."}, status=400)
        except Exception as exc:
            self.log("Операция кошелька: " + type(exc).__name__)
            return web.json_response({"success": False, "message": "Ошибка связи. Проверьте историю, не повторяйте перевод."}, status=503)

    async def withdraw(self, request, session):
        body = await request.json()
        token = str(body.get("token", "")).upper()
        if token not in {"SOL", "USDC"}:
            raise WalletError("Поддерживаются USDC и SOL.", 400)
        raw = amount_raw(body.get("amount", 0), 9 if token == "SOL" else 6)
        recipient = Pubkey.from_string(str(body.get("recipient_address", "")))
        if not recipient.is_on_curve() or str(recipient) == self.wallet:
            raise WalletError("Введите адрес отдельного личного кошелька, не token account.", 400)
        operation_id = str(body.get("request_id", ""))
        try:
            uuid.UUID(operation_id)
        except ValueError:
            raise WalletError("Отсутствует идентификатор перевода. Обновите приложение.", 400)
        valid, state = self.pin_fn(self.owner_id, str(body.get("pin", "")))
        if not valid:
            raise WalletError("Неверный PIN или код заблокирован.", 423 if state == "locked" else 401)
        async with self.lock:
            signer = self.ready()
            previous = self.operation(operation_id)
            if previous:
                expected = {"token": token, "raw": raw, "recipient": str(recipient)}
                if previous["kind"] != "WITHDRAW" or json.loads(previous["metadata"]) != expected:
                    raise WalletError("Идентификатор принадлежит другому переводу.", 409)
                return self.reply_operation(previous)
            await self.reconcile(session)
            if self.pending():
                raise WalletError("Предыдущая операция не подтверждена; повтор заблокирован.", 409)
            tx = await self.withdraw_transaction(session, signer, token, recipient, raw, operation_id)
            row = await self.broadcast(session, tx, "WITHDRAW", {
                "token": token, "raw": raw, "recipient": str(recipient)}, operation_id)
            return self.reply_operation(row)

    async def deposit(self, request, session):
        body = await request.json()
        signature = str(body.get("tx_signature", "")).strip()
        if not 64 <= len(signature) <= 88:
            raise WalletError("Некорректная подпись транзакции.", 400)
        tx = await self.rpc(session, "getTransaction", [signature, {
            "encoding": "jsonParsed", "commitment": "finalized", "maxSupportedTransactionVersion": 0}])
        if not tx or not tx.get("meta") or tx["meta"].get("err") is not None:
            raise WalletError("Транзакция не финализирована или отклонена.", 409)
        keys = tx["transaction"]["message"]["accountKeys"]
        if any(isinstance(k, dict) and k.get("pubkey") == self.wallet and k.get("signer") for k in keys):
            raise WalletError("Это собственная исходящая операция, не внешний депозит.", 409)
        incoming = {}
        for token, mint, decimals in (("USDC", USDC, 6), ("SOL", SOL, 9)):
            try:
                delta = self.delta(tx, self.wallet, mint)
            except StopIteration:
                delta = 0
            if delta > 0:
                incoming[token] = delta / 10**decimals
        if not incoming:
            raise WalletError("Нет входящего USDC или SOL на серверный адрес.", 409)
        with self.db() as db:
            db.execute("""INSERT OR IGNORE INTO wallet_operations
                (id,signature,kind,state,metadata,effects_applied) VALUES(?,?,'DEPOSIT','COMPLETED',?,1)""",
                ("deposit:" + signature, signature, json.dumps(incoming)))
        return web.json_response({"success": True, "message": "Депозит подтверждён. Баланс берётся из сети.", "received": incoming})

    async def stats(self, request, session):
        await self.reconcile(session)
        assets = await self.balances(session)
        with self.db() as db:
            positions = [dict(r) for r in db.execute("SELECT * FROM wallet_positions ORDER BY timestamp DESC")]
            operations = [dict(r) for r in db.execute("SELECT * FROM wallet_operations ORDER BY timestamp DESC LIMIT 20")]
        history = []
        for row in operations:
            info = json.loads(row["metadata"])
            if row["kind"] not in {"DEPOSIT", "WITHDRAW"}:
                continue
            amount = (", ".join(f"{v} {k}" for k, v in info.items()) if row["kind"] == "DEPOSIT"
                else f'{info["raw"]/10**(9 if info["token"]=="SOL" else 6):.9f} {info["token"]}')
            history.append({"type": row["kind"], "amount": amount, "tx": row["signature"],
                            "time": row["timestamp"], "status": row["state"]})
        trades = []
        for position in positions:
            decimals = self.watchlist.get(position["symbol"], {}).get("decimals", 6)
            quantity = position["raw_amount"] / 10**decimals
            trades.append({"symbol": position["symbol"], "token_symbol": position["symbol"],
                "token_mint": position["mint"], "amount_tokens": quantity, "buy_price": position["entry_price"],
                "sell_price": position["exit_price"], "profit_usdc": position["profit_usdc"],
                "profit_pct": position["profit_usdc"] / position["cost_usdc"] * 100,
                "timestamp": position["timestamp"], "buy_tx": position["buy_tx"], "sell_tx": position["sell_tx"],
                "status": position["state"]})
        usdc, sol = assets[USDC]["raw"] / 1e6, assets[SOL]["raw"] / 1e9
        profit = sum(p["profit_usdc"] for p in positions if p["state"] == "CLOSED")
        blocked = bool(self.pending())
        return web.json_response({"success": True, "username": request["telegram_user"].get("username", "owner"),
            "user_balance": usdc, "sol_balance": sol, "withdrawable": 0 if blocked else usdc,
            "withdrawable_sol": 0 if blocked else max(0, sol - .001), "total_deposited": 0,
            "total_pnl": profit, "profit_usdc": profit, "total_roi": 0,
            "closed_count": sum(p["state"] == "CLOSED" for p in positions), "trades": trades,
            "wallet_history": history, "wallet_status": self.readiness(), "operation_pending": blocked,
            "balance_reconciled": True, "deposit_wallet": self.wallet, "trading_active": self.trading})

    async def toggle(self, request, session):
        async with self.lock:
            self.close_requested = False
            if self.trading:
                self.trading = False
            else:
                self.ready()
                body = await request.json()
                self.trade_raw = amount_raw(body.get("trade_amount_usdc", "20"), 6)
                assets = await self.balances(session)
                if self.trade_raw > assets[USDC]["raw"]:
                    raise WalletError("Размер покупки превышает реальный USDC-баланс.", 409)
                self.trading = True
            self.log("Автопилот: " + ("ВКЛ" if self.trading else "ВЫКЛ"))
        return web.json_response({"success": True, "trading_active": self.trading})

    async def close_all(self, request, session):
        body = await request.json()
        if body.get("confirm") is not True:
            raise WalletError("Нужно подтвердить продажу позиций.", 400)
        async with self.lock:
            self.trading = False
            await self.reconcile(session)
            if self.pending():
                raise WalletError("Предыдущая операция ещё не подтверждена.", 409)
            self.close_requested = True
            with self.db() as db:
                position = db.execute("SELECT * FROM wallet_positions WHERE state='OPEN' LIMIT 1").fetchone()
            if not position:
                return web.json_response({"success": True, "message": "Нет подтверждённых позиций этого режима."})
            row = await self.sell(session, dict(position))
            return self.reply_operation(row)

    async def sell(self, session, position):
        decimals = self.watchlist[position["symbol"]]["decimals"]
        assets = await self.balances(session)
        if position["raw_amount"] > assets.get(position["mint"], {}).get("raw", 0):
            raise WalletError("Остаток позиции изменён вне бота. Автоматическая продажа остановлена.", 409)
        return await self.swap(session, position["mint"], USDC, position["raw_amount"], "SELL",
            {"position_id": position["id"], "decimals": decimals})

    async def worker(self):
        # OFF at every restart; only the authenticated owner's toggle enables orders.
        while True:
            try:
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
                    await self.reconcile(session)
                    if self.trading or self.close_requested:
                        prices = await self.prices_fn(session)
                        for symbol, price in prices.items():
                            if price > 0 and symbol in self.history:
                                self.history[symbol] = (self.history[symbol] + [price])[-10:]
                        async with self.lock:
                            if (self.trading or self.close_requested) and not self.pending():
                                with self.db() as db:
                                    positions = [dict(r) for r in db.execute("SELECT * FROM wallet_positions WHERE state='OPEN'")]
                                target = positions[0] if self.close_requested and positions else next((p for p in positions if prices.get(p["symbol"], 0) > 0
                                    and ((prices[p["symbol"]] / p["entry_price"] - 1) >= .035
                                    or (prices[p["symbol"]] / p["entry_price"] - 1) <= -.020)), None)
                                if target:
                                    await self.sell(session, target)
                                elif self.close_requested:
                                    self.close_requested = False
                                elif self.trading and len(positions) < 5:
                                    owned = {p["symbol"] for p in positions}
                                    dips = [(1 - values[-1] / (sum(values)/len(values)), symbol)
                                            for symbol, values in self.history.items()
                                            if len(values) >= 5 and symbol not in owned]
                                    if dips and max(dips)[0] >= .003:
                                        symbol = max(dips)[1]
                                        asset = self.watchlist[symbol]
                                        await self.swap(session, USDC, asset["mint"], self.trade_raw, "BUY",
                                            {"symbol": symbol, "mint": asset["mint"], "decimals": asset["decimals"]})
            except Exception as exc:
                message = str(exc) if isinstance(exc, WalletError) else type(exc).__name__
                if message != self.last_error:
                    self.log("Автопилот: " + message)
                    self.last_error = message
                if self.pending():
                    self.trading = False
            await asyncio.sleep(20)

    async def logs(self, request, session):
        return web.json_response({"success": True, "logs": getattr(self, "recent_logs", lambda: [])(),
                                 "trading_active": self.trading, "meme_active": False})
