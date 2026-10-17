import tempfile
import unittest
import uuid
import json

from solders.keypair import Keypair
from solders.signature import Signature

from custody_ledger import CustodyLedger, SOL, USDC, available, partner_account
from account_security import AccountSecurity
from wallet_runtime import WalletError


class PartnerProgramTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = self.tmp.name + "/ledger.db"
        self.ledger = CustodyLedger(self.db_path)
        self.ledger.seed_fees(100_000_000, 100_000_000)
        self.ledger.user(202, "partner202")
        code = self.ledger.partner_referral_code(202)
        self.assertTrue(self.ledger.bind_referral_code(303, code))
        self.ledger.user(303)
        code = self.ledger.partner_referral_code(303)
        self.assertTrue(self.ledger.bind_referral_code(404, code))
        self.ledger.user(404)
        code = self.ledger.partner_referral_code(404)
        self.assertTrue(self.ledger.bind_referral_code(101, code))
        self.ledger.user(101)
        invoice = self.ledger.invoice(101, USDC, 40_000_000)
        self.ledger.credit_deposit(invoice["id"], str(Signature.new_unique()),
                                   USDC, 40_000_000, invoice["created_at"])
        invoice = self.ledger.invoice(101, SOL, 10_000_000_000)
        self.ledger.credit_deposit(invoice["id"], str(Signature.new_unique()),
                                   SOL, 10_000_000_000, invoice["created_at"])

    def tearDown(self):
        self.assertTrue(self.ledger.audit())

    def close_trade(self, cost, proceeds, funding_asset=USDC):
        buy = self.ledger.reserve(101, str(uuid.uuid4()), "BUY", funding_asset, cost,
            {"output_asset": "test-mint", "symbol": "TEST", "decimals": 6})
        self.ledger.prepared(buy["id"], str(Signature.new_unique()), "offline-buy")
        self.ledger.finalize(buy["id"], cost, 0, 1_000_000)
        sell = self.ledger.reserve(101, str(uuid.uuid4()), "SELL", "test-mint", 1_000_000,
            {"output_asset": funding_asset}, position_id=buy["id"])
        self.ledger.prepared(sell["id"], str(Signature.new_unique()), "offline-sell")
        self.ledger.finalize(sell["id"], 1_000_000, 0, proceeds)
        return buy["id"]

    def test_three_levels_receive_percentages_of_realized_profit(self):
        position_id = self.close_trade(10_000_000, 11_000_000)
        with self.ledger.db() as db:
            rows = [dict(row) for row in db.execute(
                "SELECT * FROM custody_partner_earnings WHERE position_id=? ORDER BY level",
                (position_id,))]
        self.assertEqual([(row["partner_uid"], row["level"], row["amount_raw"])
                          for row in rows], [(404, 1, 10_000), (303, 2, 5_000), (202, 3, 2_500)])
        self.assertEqual(self.ledger.balance(101, USDC), 40_982_500)
        self.assertEqual(self.ledger.balance(404, USDC), 10_000)
        report = self.ledger.partner_report(404)
        direct = next(item for item in report["earnings"] if item["asset"] == USDC)
        self.assertEqual((direct["credited_raw"], direct["pending_raw"], direct["paid_raw"]),
                         (10_000, 0, 0))
        self.assertFalse(report["automatic_payouts"])
        self.assertTrue(report["balance_crediting"])
        with self.ledger.db() as db:
            self.assertEqual(db.execute("SELECT COALESCE((SELECT raw FROM custody_accounts WHERE account=? AND asset=?),0) AS raw",
                (partner_account(404), USDC)).fetchone()["raw"], 0)
            self.assertEqual(db.execute("SELECT state FROM custody_partner_earnings WHERE id=?",
                (rows[0]["id"],)).fetchone()["state"], "CREDITED")
        withdrawal = self.ledger.reserve(404, "ordinary-wallet-withdrawal", "WITHDRAW",
            USDC, 10_000, {}, "verified-wallet")
        self.assertEqual(withdrawal["state"], "RESERVED")

    def test_previous_losses_are_recovered_before_new_commission(self):
        first = self.close_trade(10_000_000, 9_000_000)
        with self.ledger.db() as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM custody_partner_earnings WHERE position_id=?", (first,)).fetchone()[0], 0)
        second = self.close_trade(10_000_000, 11_500_000)
        with self.ledger.db() as db:
            rows = [dict(row) for row in db.execute(
                "SELECT level,profit_basis_raw,amount_raw FROM custody_partner_earnings WHERE position_id=? ORDER BY level",
                (second,))]
        self.assertEqual(rows, [
            {"level": 1, "profit_basis_raw": 500_000, "amount_raw": 5_000},
            {"level": 2, "profit_basis_raw": 500_000, "amount_raw": 2_500},
            {"level": 3, "profit_basis_raw": 500_000, "amount_raw": 1_250},
        ])
        self.assertEqual(self.ledger.balance(404, USDC), 5_000)

    def test_sol_and_usdc_are_credited_separately_after_loss_recovery(self):
        losing_sol_trade = self.close_trade(1_000_000_000, 900_000_000, SOL)
        with self.ledger.db() as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM custody_partner_earnings WHERE position_id=?",
                (losing_sol_trade,)).fetchone()[0], 0)

        self.close_trade(1_000_000_000, 1_200_000_000, SOL)
        self.close_trade(10_000_000, 11_000_000, USDC)
        with self.ledger.db() as db:
            rows = [dict(row) for row in db.execute(
                "SELECT level,asset,profit_basis_raw,amount_raw,state FROM custody_partner_earnings "
                "WHERE trader_uid=101 ORDER BY asset,level")]
        sol_rows = [row for row in rows if row["asset"] == SOL]
        usdc_rows = [row for row in rows if row["asset"] == USDC]
        self.assertEqual([(row["level"], row["profit_basis_raw"], row["amount_raw"], row["state"])
                          for row in sol_rows],
                         [(1, 100_000_000, 1_000_000, "CREDITED"),
                          (2, 100_000_000, 500_000, "CREDITED"),
                          (3, 100_000_000, 250_000, "CREDITED")])
        self.assertEqual([(row["level"], row["profit_basis_raw"], row["amount_raw"], row["state"])
                          for row in usdc_rows],
                         [(1, 1_000_000, 10_000, "CREDITED"),
                          (2, 1_000_000, 5_000, "CREDITED"),
                          (3, 1_000_000, 2_500, "CREDITED")])
        self.assertEqual(self.ledger.balance(404, SOL), 1_000_000)
        self.assertEqual(self.ledger.balance(404, USDC), 10_000)
        self.assertIsNone(self.ledger.next_partner_earning())

    def test_legacy_queued_earnings_move_to_main_balance_once(self):
        amount = 10_000
        with self.ledger.db() as db:
            self.ledger.post(db, "legacy-partner-funds", "legacy partner balance", [
                ("external", USDC, -amount), (partner_account(404), USDC, amount)])
            db.execute("""INSERT INTO custody_partner_earnings
                (id,position_id,trader_uid,partner_uid,level,asset,profit_basis_raw,
                 amount_raw,state,created_at)
                VALUES('legacy-earning','old-position',101,404,1,? ,1_000_000,?,'QUEUED',1)""",
                (USDC, amount))
        migrated = CustodyLedger(self.db_path)
        self.assertEqual(migrated.balance(404, USDC), amount)
        self.assertEqual(migrated.balance(404, SOL), 0)
        self.assertEqual(migrated.credit_safe_partner_earnings(), 0)
        with self.ledger.db() as db:
            self.assertEqual(db.execute("SELECT raw FROM custody_accounts WHERE account=? AND asset=?",
                (partner_account(404), USDC)).fetchone()["raw"], 0)
            self.assertEqual(db.execute(
                "SELECT state FROM custody_partner_earnings WHERE id='legacy-earning'").fetchone()["state"],
                "CREDITED")

    def test_ambiguous_legacy_earning_is_not_double_credited(self):
        amount = 10_000
        with self.ledger.db() as db:
            self.ledger.post(db, "legacy-mismatched-partner-funds", "legacy partner mismatch", [
                ("external", USDC, -amount // 2), (partner_account(404), USDC, amount // 2)])
            db.execute("""INSERT INTO custody_partner_earnings
                (id,position_id,trader_uid,partner_uid,level,asset,profit_basis_raw,
                 amount_raw,state,created_at)
                VALUES('ambiguous-earning','old-position',101,404,1,? ,1_000_000,?,'QUEUED',1)""",
                (USDC, amount))
        migrated = CustodyLedger(self.db_path)
        self.assertEqual(migrated.balance(404, USDC), 0)
        self.assertEqual(migrated.credit_safe_partner_earnings(), 0)
        with self.ledger.db() as db:
            self.assertEqual(db.execute("SELECT raw FROM custody_accounts WHERE account=? AND asset=?",
                (partner_account(404), USDC)).fetchone()["raw"], amount // 2)
            self.assertEqual(db.execute(
                "SELECT state FROM custody_partner_earnings WHERE id='ambiguous-earning'").fetchone()["state"],
                "QUEUED")

    def test_invalid_or_late_referral_binding_is_rejected(self):
        self.assertFalse(self.ledger.bind_referral_code(101, "unknown"))
        self.assertFalse(self.ledger.bind_referral_code(101, self.ledger.partner_referral_code(101)))
        self.ledger.user(505)
        self.assertFalse(self.ledger.bind_referral_code(505, self.ledger.partner_referral_code(202)))


class PartnerConsentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addAsyncCleanup(self.tmp.cleanup)
        self.ledger = CustodyLedger(self.tmp.name + "/accounts.db")
        self.ledger.user(202)
        code = self.ledger.partner_referral_code(202)
        self.assertTrue(self.ledger.bind_referral_code(101, code))
        self.accounts = AccountSecurity(self.ledger, "https://example.test",
                                        lambda uid, pin: (True, "ok"), str(Keypair().pubkey()))

    async def test_referred_user_must_accept_profit_share_before_registering(self):
        state = self.accounts.state(101)
        self.assertTrue(state["partner_terms_required"])

        class Request(dict):
            def __init__(self, body):
                super().__init__(telegram_user={"id": 101})
                self.body = body

            async def json(self):
                return self.body

        with self.assertRaises(WalletError):
            await self.accounts.register(Request({
                "nickname": "ReferredOne", "accept_risk": True,
                "accept_partner_terms": False}))
        response = await self.accounts.register(Request({
            "nickname": "ReferredOne", "accept_risk": True,
            "accept_partner_terms": True}))
        self.assertTrue(json.loads(response.text)["registered"])
        self.assertFalse(self.accounts.state(101)["partner_terms_required"])


if __name__ == "__main__":
    unittest.main()
