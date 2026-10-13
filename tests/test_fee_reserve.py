import asyncio
import json
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock

from custody_ledger import CustodyLedger, LedgerError, SOL, FEE_CAP, available
from custody_runtime import CustodyRuntime, WalletError, GENESIS


class Request(dict):
    def __init__(self, uid, body):
        super().__init__(telegram_user={"id": uid})
        self.body = body

    async def json(self):
        return self.body


class FeeReserveTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.runtime = object.__new__(CustodyRuntime)
        self.runtime.ledger = CustodyLedger(os.path.join(self.tmp.name, "db"))
        self.runtime.owner_id = 101
        self.runtime.ledger.user(101)
        self.runtime.ledger.user(202)
        with self.runtime.ledger.db() as db:
            self.runtime.ledger.post(db, "fixture", "test deposits", [
                ("external", SOL, -50_000_000), (available(101), SOL, 20_000_000),
                (available(202), SOL, 30_000_000)])
        self.runtime.lock = asyncio.Lock()
        self.runtime.ready = Mock()
        self.runtime.admit = Mock()
        self.runtime.reconcile = AsyncMock()
        self.runtime.solvent = AsyncMock(return_value={SOL: {"raw": 50_000_000}})
        self.runtime.rpc = AsyncMock(return_value=GENESIS)

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_allocation_is_visible_and_leaves_other_user_unchanged(self):
        response = await self.runtime.reserve_fees(Request(101, {
            "amount_sol": "0.01", "confirm_own_unassigned": True}), None)
        data = json.loads(response.text)
        self.assertTrue(data["success"])
        self.assertEqual(data["operator_fees"]["reserve_sol"], "0.01")
        self.assertEqual(data["operator_fees"]["minimum_sol"], "0.005")
        self.assertEqual(data["operator_fees"]["own_available_sol"], "0.01")
        self.assertTrue(data["operator_fees"]["sufficient"])
        self.assertEqual(self.runtime.ledger.balance(202, SOL), 30_000_000)
        self.assertTrue(self.runtime.ledger.audit())

    async def test_allocation_requires_explicit_owner_confirmation(self):
        with self.assertRaises(WalletError):
            await self.runtime.reserve_fees(Request(101, {"amount_sol": "0.01"}), None)
        self.assertEqual(self.runtime.operator_fees(101)["reserve_sol"], "0")

    async def test_nonowner_cannot_allocate_or_read_operator_summary(self):
        with self.assertRaises(WalletError):
            await self.runtime.reserve_fees(Request(202, {
                "amount_sol": "0.01", "confirm_own_unassigned": True}), None)
        self.assertIsNone(self.runtime.operator_fees(202))

    async def test_cannot_take_extra_from_other_users(self):
        with self.assertRaises(LedgerError):
            await self.runtime.reserve_fees(Request(101, {
                "amount_sol": "0.03", "confirm_own_unassigned": True}), None)
        self.assertEqual(self.runtime.ledger.balance(101, SOL), 20_000_000)
        self.assertEqual(self.runtime.operator_fees(101)["reserve_sol"], "0")

    async def test_threshold_matches_actual_ledger_not_personal_sol(self):
        with self.assertRaisesRegex(WalletError, "Резерв комиссий: 0 SOL; требуется не меньше 0.005 SOL"):
            await self.runtime.trading_preflight(101, None, 1_000_000)
        self.runtime.ledger.seed_fees(FEE_CAP, 50_000_000, 101)
        self.assertTrue(self.runtime.operator_fees(101)["sufficient"])
