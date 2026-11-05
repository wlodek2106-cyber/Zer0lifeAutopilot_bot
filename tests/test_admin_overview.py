import tempfile
import unittest

from solders.signature import Signature

from custody_ledger import CustodyLedger, USDC


class AdminOverviewSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ledger = CustodyLedger(self.tmp.name + "/ledger.sqlite3")
        self.ledger.user(101, "alice")
        self.ledger.user(202, "bob")

    def test_snapshot_is_aggregate_and_preserves_per_user_liabilities(self):
        for uid, amount in ((101, 2_000_000), (202, 5_000_000)):
            invoice = self.ledger.invoice(uid, USDC, amount)
            self.ledger.credit_deposit(invoice["id"], str(Signature.new_unique()), USDC, amount, invoice["created_at"])
        snapshot = self.ledger.admin_overview_snapshot()
        self.assertTrue(snapshot["read_only"])
        self.assertEqual(snapshot["users_total"], 2)
        self.assertEqual(snapshot["customer_liabilities_raw"][USDC], 7_000_000)
        self.assertEqual(len(snapshot["recent_operations"]), 0)
        self.assertNotIn("alice", repr(snapshot))
        self.assertNotIn("bob", repr(snapshot))
        self.assertTrue(self.ledger.audit())

    def test_recent_limit_is_validated(self):
        with self.assertRaises(Exception):
            self.ledger.admin_overview_snapshot(0)
        with self.assertRaises(Exception):
            self.ledger.admin_overview_snapshot(100)


if __name__ == "__main__":
    unittest.main()
