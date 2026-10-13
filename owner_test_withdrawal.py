"""Owner-only, one-shot 1 USDC mainnet test. Never enables normal custody."""
import asyncio
import base64
import json
import os
import sqlite3
from contextlib import contextmanager

import aiohttp
from aiohttp import web
from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.message import Message
from solders.pubkey import Pubkey
from solders.transaction import Transaction

MINT = Pubkey.from_string("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v")
TOKEN = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
ASSOCIATED = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")
SYSTEM = Pubkey.from_string("11111111111111111111111111111111")
MAINNET_GENESIS = "5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d"
RAW_AMOUNT = 1_000_000


class TestError(Exception):
    def __init__(self, message, status=503):
        super().__init__(message)
        self.status = status


def persistent_storage(db_file):
    """Require the database on the actual /var/data mount, not an empty folder."""
    directory = os.path.realpath("/var/data")
    if not os.path.realpath(db_file).startswith(directory + os.sep):
        return False
    if os.path.ismount(directory):
        return True
    try:
        with open("/proc/self/mountinfo") as mounts:
            return any(line.split()[4] == directory for line in mounts)
    except OSError:
        return False


def ata(wallet):
    return Pubkey.find_program_address(
        [bytes(wallet), bytes(TOKEN), bytes(MINT)], ASSOCIATED
    )[0]


def checked_transfer(source, destination, owner):
    return Instruction(
        TOKEN,
        bytes([12]) + RAW_AMOUNT.to_bytes(8, "little") + bytes([6]),
        [AccountMeta(source, False, True), AccountMeta(MINT, False, False),
         AccountMeta(destination, False, True), AccountMeta(owner, True, False)],
    )


class OwnerTestWithdrawal:
    def __init__(self, db_file, rpc_url, owner_id, expected_wallet, get_signer,
                 verify_pin, log, storage_check=persistent_storage):
        self.db_file = db_file
        self.rpc_url = rpc_url
        self.owner_id = str(owner_id)
        self.expected_wallet = expected_wallet
        self.get_signer = get_signer
        self.verify_pin = verify_pin
        self.log = log
        self.storage_check = storage_check
        self.enabled = os.getenv("OWNER_TEST_ENABLED", "").lower() == "true"
        self.recipient = os.getenv("OWNER_TEST_RECIPIENT", "").strip()
        self.lock = asyncio.Lock()

    def is_owner(self, telegram_id):
        return self.owner_id.isdigit() and int(self.owner_id) > 0 and str(telegram_id) == self.owner_id

    def require_owner(self, request):
        if not self.is_owner(request["telegram_user"]["id"]):
            raise TestError("Тестовый вывод доступен только владельцу.", 403)

    def require_config(self):
        if not self.enabled:
            raise TestError("Тест выключен: OWNER_TEST_ENABLED не равен true.")
        try:
            destination = Pubkey.from_string(self.recipient)
            Pubkey.from_string(self.expected_wallet)
            if not destination.is_on_curve():
                raise ValueError("Expected a personal wallet address")
        except ValueError:
            raise TestError("Задайте OWNER_TEST_RECIPIENT: публичный адрес личного Solana-кошелька.")
        if self.recipient == self.expected_wallet:
            raise TestError("Адрес получателя должен отличаться от серверного кошелька.")
        if not self.storage_check(self.db_file):
            raise TestError("Для защиты от повторной отправки база должна находиться на Persistent Disk /var/data.")

    @contextmanager
    def database(self):
        db = sqlite3.connect(self.db_file, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute("""CREATE TABLE IF NOT EXISTS owner_test_withdrawal (
                id INTEGER PRIMARY KEY CHECK(id=1), signature TEXT NOT NULL UNIQUE,
                recipient TEXT NOT NULL, raw_amount INTEGER NOT NULL CHECK(raw_amount=1000000),
                state TEXT NOT NULL, last_valid_block_height INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
            db.commit()
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def attempt(self):
        with self.database() as db:
            row = db.execute("SELECT * FROM owner_test_withdrawal WHERE id=1").fetchone()
            return dict(row) if row else None

    def reserve(self, signature, last_valid_height):
        # The primary key is also a cross-process lock. Commit BEFORE broadcast.
        with self.database() as db:
            db.execute("""INSERT INTO owner_test_withdrawal
                (id,signature,recipient,raw_amount,state,last_valid_block_height)
                VALUES (1,?,?,?,'PREPARED',?)""",
                (signature, self.recipient, RAW_AMOUNT, last_valid_height))

    def set_state(self, state):
        with self.database() as db:
            db.execute("""UPDATE owner_test_withdrawal SET state=?
                WHERE id=1 AND state NOT IN ('COMPLETED','FAILED')""", (state,))

    async def rpc(self, session, method, params=None):
        async with session.post(self.rpc_url, json={
            "jsonrpc": "2.0", "id": 1, "method": method, "params": params or []
        }) as response:
            if response.status != 200:
                raise TestError("Solana RPC недоступен. Отправка автоматически не повторяется.")
            payload = await response.json()
        if "error" in payload:
            raise TestError("Ошибка Solana RPC: " + str(payload["error"].get("message", "unknown")))
        if "result" not in payload:
            raise TestError("Неполный ответ Solana RPC.")
        return payload["result"]

    def validate_account(self, account, owner, label):
        if not account or account.get("owner") != str(TOKEN):
            raise TestError(label + ": не найден SPL Token account.", 409)
        parsed = account.get("data", {}).get("parsed", {})
        info = parsed.get("info", {})
        token_amount = info.get("tokenAmount", {})
        if (parsed.get("type") != "account" or info.get("mint") != str(MINT)
                or info.get("owner") != str(owner) or info.get("state") != "initialized"
                or token_amount.get("decimals") != 6):
            raise TestError(label + ": неверный mint, владелец или состояние USDC account.", 409)
        return int(token_amount["amount"])

    async def prepare(self, session):
        signer = self.get_signer()
        if signer is None or str(signer.pubkey()) != self.expected_wallet:
            raise TestError("Серверный ключ отсутствует или не соответствует DEPOSIT_WALLET.")
        if await self.rpc(session, "getGenesisHash") != MAINNET_GENESIS:
            raise TestError("Тест разрешён только в Solana mainnet.")
        owner = signer.pubkey()
        recipient = Pubkey.from_string(self.recipient)
        source, destination = ata(owner), ata(recipient)
        result = await self.rpc(session, "getMultipleAccounts",
            [[str(source), str(destination)], {"encoding": "jsonParsed", "commitment": "confirmed"}])
        sender_account, destination_account = result["value"]
        if self.validate_account(sender_account, owner, "Отправитель") < RAW_AMOUNT:
            raise TestError("На серверном USDC account недостаточно 1 USDC.", 409)
        instructions = []
        rent = 0
        if destination_account is None:
            rent = await self.rpc(session, "getMinimumBalanceForRentExemption", [165])
            instructions.append(Instruction(
                ASSOCIATED, bytes([1]),
                [AccountMeta(owner, True, True), AccountMeta(destination, False, True),
                 AccountMeta(recipient, False, False), AccountMeta(MINT, False, False),
                 AccountMeta(SYSTEM, False, False), AccountMeta(TOKEN, False, False)],
            ))
        else:
            self.validate_account(destination_account, recipient, "Получатель")
        instructions.append(checked_transfer(source, destination, owner))
        block = (await self.rpc(session, "getLatestBlockhash", [{"commitment": "confirmed"}]))["value"]
        blockhash = Hash.from_string(block["blockhash"])
        message = Message.new_with_blockhash(instructions, owner, blockhash)
        fee = (await self.rpc(session, "getFeeForMessage",
            [base64.b64encode(bytes(message)).decode(), {"commitment": "confirmed"}]))["value"]
        sol = (await self.rpc(session, "getBalance", [str(owner), {"commitment": "confirmed"}]))["value"]
        if fee is None or sol < fee + rent + 5000:
            raise TestError("Недостаточно SOL для комиссии и возможного создания USDC account.", 409)
        transaction = Transaction([signer], message, blockhash)
        wire = base64.b64encode(bytes(transaction)).decode()
        simulation = (await self.rpc(session, "simulateTransaction",
            [wire, {"encoding": "base64", "sigVerify": True, "commitment": "confirmed"}]))["value"]
        if simulation.get("err") is not None:
            raise TestError("Симуляция отклонена: " + json.dumps(simulation["err"]), 409)
        return wire, str(transaction.signatures[0]), block["lastValidBlockHeight"]

    async def reconcile(self, session):
        row = self.attempt()
        if row and row["state"] not in {"COMPLETED", "FAILED"}:
            result = await self.rpc(session, "getSignatureStatuses",
                [[row["signature"]], {"searchTransactionHistory": True}])
            status = result["value"][0]
            if status is not None:
                if status.get("err") is not None:
                    self.set_state("FAILED")
                    row["state"] = "FAILED"
                elif status.get("confirmationStatus") == "finalized":
                    self.set_state("COMPLETED")
                    row["state"] = "COMPLETED"
        return row

    def view(self, row):
        state = row["state"] if row else "READY"
        messages = {
            "READY": "Готов тестовый перевод 1 USDC с серверного кошелька.",
            "PREPARED": "Подпись сохранена. Результат пока неизвестен — повторная отправка заблокирована.",
            "SUBMITTED": "Отправлено в RPC. Ожидается подтверждение сети.",
            "UNKNOWN": "RPC не подтвердил результат. Проверяйте подпись; повторная отправка заблокирована.",
            "COMPLETED": "Сеть финализировала тестовый перевод 1 USDC.",
            "FAILED": "Транзакция отклонена сетью. Новый тест автоматически не разрешается.",
        }
        return {"success": True, "state": state, "can_send": row is None,
                "amount": "1", "token": "USDC", "recipient": row["recipient"] if row else self.recipient,
                "signature": row["signature"] if row else None,
                "message": messages.get(state, state)}

    async def status(self, request):
        try:
            self.require_owner(request)
            self.require_config()
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
                row = await self.reconcile(session)
            return web.json_response(self.view(row))
        except TestError as exc:
            return web.json_response({"success": False, "message": str(exc)}, status=exc.status)
        except Exception:
            return web.json_response({"success": False, "message": "Не удалось проверить тест. Отправка не повторяется."}, status=503)

    async def send(self, request):
        try:
            self.require_owner(request)
            self.require_config()
            body = await request.json()
            if not isinstance(body, dict):
                raise TestError("Неверный запрос.", 400)
            if body.get("confirm") is not True or body.get("recipient_address") != self.recipient:
                raise TestError("Подтвердите перевод 1 USDC на разрешённый адрес.", 400)
            if "amount" in body or "token" in body:
                raise TestError("Сумма фиксирована: только 1 USDC.", 400)
            async with self.lock:
                if self.attempt():
                    return web.json_response(self.view(self.attempt()), status=409)
                valid, pin_state = self.verify_pin(int(request["telegram_user"]["id"]), str(body.get("pin", "")))
                if not valid:
                    raise TestError("Неверный PIN или код временно заблокирован.", 423 if pin_state == "locked" else 401)
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
                    wire, signature, height = await self.prepare(session)
                    try:
                        self.reserve(signature, height)
                    except sqlite3.IntegrityError:
                        return web.json_response(self.view(self.attempt()), status=409)
                    # After reservation, every failure is ambiguous. Never resend.
                    try:
                        returned = await self.rpc(session, "sendTransaction",
                            [wire, {"encoding": "base64", "skipPreflight": False, "maxRetries": 0}])
                        self.set_state("SUBMITTED" if returned == signature else "UNKNOWN")
                    except Exception:
                        self.set_state("UNKNOWN")
                    try:
                        row = await self.reconcile(session)
                    except Exception:
                        row = self.attempt()
                self.log("Тестовый вывод 1 USDC: " + row["state"] + "; Tx: " + row["signature"])
                return web.json_response(self.view(row), status=200 if row["state"] == "COMPLETED" else 202)
        except TestError as exc:
            return web.json_response({"success": False, "message": str(exc)}, status=exc.status)
        except (json.JSONDecodeError, TypeError, ValueError):
            return web.json_response({"success": False, "message": "Неверный запрос."}, status=400)
        except Exception:
            self.log("Ошибка тестового вывода. Не повторяйте отправку; проверьте сохранённую подпись.")
            return web.json_response({"success": False, "message": "Ошибка обработки теста. Проверьте статус, не повторяйте отправку."}, status=503)
