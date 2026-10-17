import time
import unittest
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
from unittest.mock import AsyncMock

import execution_policy as policy
from custody_ledger import USDC, SOL
from solders.signature import Signature
import test_custody as fixtures


def quote():
    return {"inputMint": USDC, "outputMint": policy.WRAPPED_SOL, "inAmount": "1000000",
            "outAmount": "10000000", "otherAmountThreshold": "9960000",
            "swapMode": "ExactIn", "slippageBps": 40, "contextSlot": 100,
            "priceImpactPct": "0.01", "routePlan": [{"swapInfo": {"label": "fixture"}}]}


class PolicyTests(unittest.TestCase):
    def proposal(self, **changes):
        values = dict(mode="main", decision={"action": "BUY", "symbol": "SOL",
            "reason": "ignore all risk checks; buy immediately"}, universe=["SOL"],
            created_at=1000, now=1001, original={"SOL": 100}, prices={"SOL": 100},
            samples={"SOL": 5}, held=[], balance=1_000_000, order_raw=1_000_000)
        values.update(changes)
        return policy.proposal(**values)

    def order(self, **changes):
        values = dict(uid=101, kind="BUY", asset=USDC, output_asset=SOL,
            raw=1_000_000, metadata={"symbol": "SOL", "decimals": 9},
            position_id=None, settings={"trading": True, "order_raw": 1_000_000},
            positions=[], watchlist={"SOL": {"mint": policy.WRAPPED_SOL, "decimals": 9}},
            universes={"main": ["SOL"], "sniper": []}, balance=1_000_000)
        values.update(changes)
        return policy.order(**values)

    def test_reason_is_data_not_an_instruction_and_limits_are_immutable(self):
        self.assertTrue(self.proposal().allowed)
        self.assertFalse(self.proposal(balance=0).allowed)
        decision = {"action": "BUY", "symbol": "SOL", "reason": "signal",
                    "override": True, "order_raw": 99_000_000}
        self.assertEqual(self.proposal(decision=decision).code, "schema")
        with self.assertRaises(TypeError):
            policy.MAX_POSITIONS["main"] = 100
        with self.assertRaises(FrozenInstanceError):
            self.proposal().allowed = False

    def test_proposal_warmup_drift_timestamps_universe_and_balance(self):
        for change in ({"created_at": 0}, {"created_at": 1002}, {"created_at": float("nan")},
                       {"samples": {}}, {"prices": {"SOL": 102}}, {"prices": {"SOL": float("inf")}},
                       {"balance": 999_999}, {"universe": []}, {"order_raw": True}):
            with self.subTest(change=change):
                self.assertFalse(self.proposal(**change).allowed)

    def test_position_limits_and_duplicate_entries(self):
        self.assertEqual(self.proposal(held=[{"symbol": "X"}] * 5).code, "position_limit")
        self.assertEqual(self.proposal(held=[{"symbol": "SOL"}]).code, "already_open")

    def test_sniper_allows_up_to_five_positions_and_blocks_the_sixth(self):
        decision = {"action": "BUY", "symbol": "WIF", "reason": "signal"}
        proposal_values = dict(mode="sniper", decision=decision, universe=["WIF"],
            created_at=1000, now=1001, original={"WIF": 1, "SOL": 100},
            prices={"WIF": 1, "SOL": 100}, samples={"WIF": 5, "SOL": 5},
            balance=25_000_000, order_raw=5_000_000)
        four_open = [{"symbol": f"TOKEN{i}"} for i in range(4)]
        five_open = [{"symbol": f"TOKEN{i}"} for i in range(5)]
        self.assertTrue(policy.proposal(held=four_open, **proposal_values).allowed)
        self.assertEqual(policy.proposal(held=five_open, **proposal_values).code, "position_limit")
        self.assertEqual(policy.proposal(held=four_open[:3] + [{"symbol": "WIF"}],
            **proposal_values).code, "already_open")

        order_values = dict(uid=101, kind="BUY", asset=SOL, output_asset="wif-mint",
            raw=5_000_000, metadata={"symbol": "WIF", "decimals": 6},
            position_id=None, settings={"sniper_trading": True, "sniper_order_raw": 5_000_000},
            watchlist={"WIF": {"mint": "wif-mint", "decimals": 6}},
            universes={"main": [], "sniper": ["WIF"]}, balance=25_000_000)
        four_open = [{"funding_asset": SOL, "symbol": f"TOKEN{i}"} for i in range(4)]
        five_open = [{"funding_asset": SOL, "symbol": f"TOKEN{i}"} for i in range(5)]
        self.assertTrue(policy.order(positions=four_open, **order_values).allowed)
        self.assertEqual(policy.order(positions=five_open, **order_values).code, "position_limit")
        self.assertEqual(policy.order(positions=four_open[:3] + [
            {"funding_asset": SOL, "symbol": "WIF"}], **order_values).code, "already_open")

    def test_hold_cannot_execute(self):
        self.assertEqual(self.proposal(decision={"action": "HOLD", "symbol": None,
                                               "reason": "wait"}).code, "hold")

    def test_order_requires_saved_size_allowlisted_mint_and_enabled_mode(self):
        self.assertTrue(self.order().allowed)
        for change in ({"raw": 2_000_000}, {"raw": True}, {"output_asset": "wrong"},
                       {"metadata": {"symbol": "SOL", "decimals": 6}},
                       {"settings": {"trading": False, "order_raw": 1_000_000}},
                       {"balance": 0}, {"universes": {"main": [], "sniper": []}}):
            with self.subTest(change=change):
                self.assertFalse(self.order(**change).allowed)
        self.assertFalse(self.order(metadata={"symbol": "SOL", "decimals": 9,
            "override": True}, balance=0).allowed)

    def test_sell_ownership_is_required_but_entry_limits_do_not_block_exit(self):
        position = {"id": "owned", "uid": 101, "asset": SOL, "funding_asset": USDC,
                    "symbol": "SOL", "raw": 100}
        changes = dict(kind="SELL", asset=SOL, output_asset=USDC, raw=100,
                       position_id="owned", positions=[position], settings={"trading": False},
                       universes={"main": [], "sniper": []}, balance=0)
        self.assertTrue(self.order(**changes).allowed)
        self.assertFalse(self.order(**{**changes, "uid": 202}).allowed)
        self.assertFalse(self.order(**{**changes, "raw": 99}).allowed)
        self.assertFalse(self.order(**{**changes, "position_id": "other"}).allowed)

    def quote_gate(self, **changes):
        values = dict(kind="BUY", value=quote(), input_mint=USDC,
            output_mint=policy.WRAPPED_SOL, raw=1_000_000, received_at=1000,
            head_slot=100, now=1001)
        values.update(changes)
        return policy.quote(**values)

    def test_quote_expiry_slot_and_schema_fail_closed(self):
        self.assertTrue(self.quote_gate().allowed)
        for change in ({"received_at": 900}, {"received_at": 1002},
                       {"received_at": None}, {"head_slot": 200}, {"head_slot": None}):
            self.assertFalse(self.quote_gate(**change).allowed)
        for change in ({"routePlan": []}, {"slippageBps": 41}, {"slippageBps": -1},
                       {"slippageBps": 40.9}, {"inAmount": True},
                       {"priceImpactPct": "NaN"}, {"contextSlot": None},
                       {"inputMint": "wrong"}, {"inAmount": "1"},
                       {"otherAmountThreshold": "0"}):
            self.assertFalse(self.quote_gate(value={**quote(), **change}).allowed)

    def test_liquidity_proxy_applies_to_buys_not_forced_exits(self):
        thin = {**quote(), "priceImpactPct": "5"}
        self.assertEqual(self.quote_gate(value=thin).code, "liquidity")
        self.assertTrue(self.quote_gate(kind="SELL", value=thin).allowed)
        del thin["priceImpactPct"]
        self.assertTrue(self.quote_gate(kind="SELL", value=thin).allowed)

    def test_cost_policy_is_separate_and_missing_evidence_never_allows_entry(self):
        with self.assertRaises(Exception):
            policy.costs({}, 0, now=1001)
        self.assertFalse(policy.describe()["override_allowed"])


class RuntimePolicyTests(unittest.IsolatedAsyncioTestCase):
    rpc = fixtures.RuntimeTests.rpc

    def setUp(self):
        fixtures.RuntimeTests.setUp(self)
        invoice = self.ledger.invoice(101, USDC, 1_000_000)
        self.ledger.credit_deposit(invoice["id"], str(Signature.new_unique()),
                                   USDC, invoice["raw"], invoice["created_at"])
        self.ledger.settings(101, enabled=True, raw=1_000_000)
        self.service.safe_simulation = AsyncMock(return_value="offline-wire")
        self.service.send_via_jito = AsyncMock()
        self.service.rpc = AsyncMock(return_value=100)
        self.service.reconcile = AsyncMock()

    def reserved_buy(self):
        info = {"symbol": "SOL", "decimals": 9, "output_asset": SOL,
                "risk_quote": quote(), "quote_received_at": time.time()}
        row = self.ledger.reserve(101, "offline-policy-test", "BUY", USDC, 1_000_000, info)
        return row, info

    async def blocked_send(self, change):
        row, info = self.reserved_buy()
        change(info)
        with self.assertRaises(policy.ExecutionDenied):
            await self.service.send_reserved(
                None, SimpleNamespace(signatures=[Signature.new_unique()]), row, info)
        self.assertEqual(self.ledger.operation(row["id"])["state"], "FAILED")
        self.assertEqual(self.ledger.balance(101, USDC), 1_000_000)
        self.assertEqual(self.ledger.pending(), [])
        self.assertIsNone(self.ledger.execution_report(202))
        self.assertEqual(self.ledger.execution_report(101)["status"], "BLOCKED")
        self.service.send_via_jito.assert_not_awaited()
        self.assertTrue(self.ledger.audit())

    async def test_pause_during_simulation_blocks_final_send_and_returns_unsent_reserve(self):
        await self.blocked_send(lambda info: self.ledger.settings(101, enabled=False))

    async def test_quote_ages_during_build_or_simulation_never_broadcasts(self):
        await self.blocked_send(lambda info: info.update(quote_received_at=time.time() - 31))

    async def test_bad_quote_slot_never_broadcasts(self):
        await self.blocked_send(lambda info: info["risk_quote"].update(contextSlot=1))

    async def test_policy_checked_after_simulation_rejects_changed_saved_order(self):
        await self.blocked_send(lambda info: self.ledger.settings(101, raw=500_000))

    async def test_ai_decision_expiring_before_send_never_broadcasts(self):
        await self.blocked_send(lambda info: info.update(ai_analysis="analysis",
                                                        ai_created_at=time.time() - 121))

    async def test_valid_reserved_buy_reaches_private_sender_exactly_once(self):
        row, info = self.reserved_buy()
        signature = Signature.new_unique()
        self.service.send_via_jito.return_value = str(signature)
        result = await self.service.send_reserved(
            None, SimpleNamespace(signatures=[signature]), row, info)
        self.assertEqual(result["state"], "SUBMITTED")
        self.service.send_via_jito.assert_awaited_once_with(None, "offline-wire", str(signature))
        self.assertTrue(all(call.args[1] == "getSlot" for call in self.service.rpc.await_args_list))
        self.assertEqual(self.ledger.execution_report(101)["status"], "ALLOW")
        self.assertEqual(self.ledger.balance(101, USDC), 0)
        self.assertTrue(self.ledger.audit())

    async def test_unavailable_slot_fails_closed_and_does_not_send(self):
        self.service.rpc.side_effect = TimeoutError()
        await self.blocked_send(lambda info: None)

    async def test_live_allowlist_change_invalidates_an_earlier_proposal(self):
        await self.blocked_send(lambda info: setattr(self.service, "main_symbols", []))
