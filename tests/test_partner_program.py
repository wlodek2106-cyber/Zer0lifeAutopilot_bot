import tempfile
import unittest
import uuid
import json

from solders.keypair import Keypair
from solders.signature import Signature

from custody_ledger import CustodyLedger, USDC, partner_account
from account_security import AccountSecurity
from wallet_runtime import WalletError


class PartnerProgramTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ledger = CustodyLedger(self.tmp.name + "/ledger.db")
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

    def tearDown(self):
        self.assertTrue(self.ledger.audit())

    def close_trade(self, cost, proceeds):
        buy = self.ledger.reserve(101, str(uuid.uuid4()), "BUY", USDC, cost,
            {"output_asset": "test-mint", "symbol": "TEST", "decimals": 6})
        self.ledger.prepared(buy["id"], str(Signature.new_unique()), "offline-buy")
        self.ledger.finalize(buy["id"], cost, 0, 1_000_000)
        sell = self.ledger.reserve(101, str(uuid.uuid4()), "SELL", "test-mint", 1_000_000,
            {"output_asset": USDC}, position_id=buy["id"])
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
        self.assertEqual(self.ledger.balance(404, USDC), 0)
        with self.ledger.db() as db:
            self.assertEqual(db.execute("SELECT raw FROM custody_accounts WHERE account=? AND asset=?",
                (partner_account(404), USDC)).fetchone()["raw"], 10_000)

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

    def test_partner_payout_is_escrowed_and_paid_once(self):
        self.close_trade(10_000_000, 11_000_000)
        recipient = str(Keypair().pubkey())
        with self.ledger.db() as db:
            for uid in (202, 303, 404):
                address = recipient if uid == 404 else str(Keypair().pubkey())
                db.execute("INSERT INTO custody_wallet_links(uid,address,created_at) VALUES(?,?,1)",
                           (uid, address))
        earning = self.ledger.next_partner_earning()
        self.assertEqual((earning["partner_uid"], earning["amount_raw"]), (404, 10_000))
        operation = self.ledger.reserve(404, "partner-" + earning["id"], "PARTNER_PAYOUT",
            earning["asset"], earning["amount_raw"], {}, recipient)
        self.ledger.set_partner_operation(earning["id"], operation["id"])
        self.ledger.prepared(operation["id"], str(Signature.new_unique()), "offline-payout")
        self.ledger.finalize(operation["id"], earning["amount_raw"], 5_000)
        self.assertEqual(self.ledger.next_partner_earning()["partner_uid"], 303)
        with self.ledger.db() as db:
            state = db.execute("SELECT state FROM custody_partner_earnings WHERE id=?",
                               (earning["id"],)).fetchone()["state"]
        self.assertEqual(state, "PAID")

    def test_payout_failure_returns_escrow_and_pauses_after_three_attempts(self):
        self.close_trade(10_000_000, 11_000_000)
        recipient = str(Keypair().pubkey())
        with self.ledger.db() as db:
            db.execute("INSERT INTO custody_wallet_links(uid,address,created_at) VALUES(?,?,1)",
                       (404, recipient))
        for attempt in range(3):
            earning = self.ledger.next_partner_earning()
            self.assertIsNotNone(earning)
            operation = self.ledger.reserve(404, "partner-attempt-" + str(attempt),
                "PARTNER_PAYOUT", earning["asset"], earning["amount_raw"], {}, recipient)
            self.ledger.set_partner_operation(earning["id"], operation["id"])
            self.ledger.fail(operation["id"])
            with self.ledger.db() as db:
                state = db.execute("SELECT state FROM custody_partner_earnings WHERE id=?",
                                   (earning["id"],)).fetchone()["state"]
                db.execute("UPDATE custody_partner_earnings SET next_attempt=0 WHERE id=?",
                           (earning["id"],))
        self.assertEqual(state, "NEEDS_REVIEW")
        with self.ledger.db() as db:
            self.assertEqual(db.execute("SELECT raw FROM custody_accounts WHERE account=? AND asset=?",
                (partner_account(404), USDC)).fetchone()["raw"], 10_000)
        self.assertTrue(self.ledger.retry_partner_earning(404, earning["id"]))
        self.assertEqual(self.ledger.next_partner_earning()["id"], earning["id"])

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
