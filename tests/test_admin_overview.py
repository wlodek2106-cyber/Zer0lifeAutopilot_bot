import tempfile
import unittest
import uuid

from custody_ledger import CustodyLedger, LedgerError, USDC


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
            self.ledger.credit_deposit(invoice["id"], str(uuid.uuid4()), USDC, amount, invoice["created_at"])
        before = {
            uid: self.ledger.balance(uid, USDC)
            for uid in (101, 202)
        }
        with self.ledger.db() as db:
            account_rows_before = db.execute(
                "SELECT account,asset,raw FROM custody_accounts ORDER BY account,asset"
            ).fetchall()

        snapshot = self.ledger.admin_overview_snapshot()
        self.assertTrue(snapshot["read_only"])
        self.assertEqual(snapshot["users_total"], 2)
        self.assertEqual(snapshot["customer_liabilities_raw"][USDC], 7_000_000)
        self.assertEqual(len(snapshot["recent_operations"]), 0)
        self.assertNotIn("alice", repr(snapshot))
        self.assertNotIn("bob", repr(snapshot))

        # A dashboard read must not credit, debit, reserve, or otherwise mutate
        # any user's personal balance or the underlying custody account rows.
        after = {
            uid: self.ledger.balance(uid, USDC)
            for uid in (101, 202)
        }
        with self.ledger.db() as db:
            account_rows_after = db.execute(
                "SELECT account,asset,raw FROM custody_accounts ORDER BY account,asset"
            ).fetchall()
        self.assertEqual(before, after)
        self.assertEqual(
            [tuple(row) for row in account_rows_before],
            [tuple(row) for row in account_rows_after],
        )
        self.assertTrue(self.ledger.audit())

    def test_recent_limit_is_validated(self):
        with self.assertRaises(LedgerError):
            self.ledger.admin_overview_snapshot(0)
        with self.assertRaises(LedgerError):
            self.ledger.admin_overview_snapshot(100)
        with self.assertRaises(LedgerError):
            self.ledger.admin_overview_snapshot(True)


if __name__ == "__main__":
    unittest.main()
