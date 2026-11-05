import unittest
from types import SimpleNamespace

from aiohttp import web

from custody_runtime import CustodyRuntime
from wallet_runtime import WalletError


class FakeLedger:
    def __init__(self):
        self.snapshot_calls = 0
        self.audit_calls = 0

    def admin_overview_snapshot(self, recent_limit=10):
        self.snapshot_calls += 1
        return {
            "success": True,
            "read_only": True,
            "users_total": 2,
            "customer_liabilities_raw": {"USDC": 7000000},
            "recent_operations": [],
        }

    def audit(self):
        self.audit_calls += 1
        return True


class AdminOverviewAuthorizationTests(unittest.IsolatedAsyncioTestCase):
    def make_runtime(self):
        runtime = CustodyRuntime.__new__(CustodyRuntime)
        runtime.owner_id = 428821665
        runtime.ledger = FakeLedger()
        runtime.uid = lambda request: request.telegram_uid
        return runtime

    async def test_non_owner_is_denied_before_ledger_access(self):
        runtime = self.make_runtime()
        request = SimpleNamespace(telegram_uid=101)

        with self.assertRaises(WalletError) as raised:
            await runtime.admin_overview(request, None)

        self.assertEqual(raised.exception.status, 403)
        self.assertEqual(runtime.ledger.snapshot_calls, 0)
        self.assertEqual(runtime.ledger.audit_calls, 0)

    async def test_owner_receives_read_only_aggregate_without_cache(self):
        runtime = self.make_runtime()
        request = SimpleNamespace(telegram_uid=runtime.owner_id)

        response = await runtime.admin_overview(request, None)

        self.assertIsInstance(response, web.Response)
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers.get("Cache-Control"), "no-store")
        self.assertEqual(runtime.ledger.snapshot_calls, 1)
        self.assertEqual(runtime.ledger.audit_calls, 1)
        self.assertIn(b'"read_only": true', response.body)
        self.assertNotIn(b"telegram_uid", response.body)


if __name__ == "__main__":
    unittest.main()
