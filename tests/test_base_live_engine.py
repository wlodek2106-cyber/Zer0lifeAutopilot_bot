import os
import tempfile
import unittest
from unittest.mock import patch

from eth_account import Account

from base_live_engine import (
    BaseLiveEngine,
    BaseLiveEngineError,
    base_live_exit_reason,
)
from base_live_trading import BASE_USDC_ADDRESS, BASE_UNISWAP_V3_SWAP_ROUTER_02


TOKEN = "0x1111111111111111111111111111111111111111"
POOL = "0x2222222222222222222222222222222222222222"
TX_HASH = "0x" + "a" * 64


def topic_address(value):
    return "0x" + value.lower()[2:].rjust(64, "0")


class BaseLiveEngineStateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.engine = BaseLiveEngine(os.path.join(self.temp.name, "ledger.sqlite"), 77)

    def tearDown(self):
        self.temp.cleanup()

    def test_budget_is_bounded_and_default_mode_stays_off(self):
        self.assertEqual(self.engine.settings(77)["enabled"], 0)
        self.assertEqual(self.engine.save_budget(77, "100.25"), 100_250_000)
        with self.assertRaises(BaseLiveEngineError):
            self.engine.save_budget(77, "14.99")
        with self.assertRaises(BaseLiveEngineError):
            self.engine.save_budget(77, "10000.01")
        with self.assertRaises(BaseLiveEngineError) as error:
            self.engine.settings(88)
        self.assertEqual(error.exception.status, 403)

    def test_restart_always_pauses_live_mode(self):
        with self.engine.db() as db:
            db.execute("UPDATE base_live_settings SET enabled=1 WHERE uid=77")
        self.engine.pause_on_restart()
        self.assertEqual(self.engine.settings(77)["enabled"], 0)

    def test_shared_wallet_signer_requires_server_opt_in(self):
        account = Account.create()
        env = {
            "BASE_TRADING_WALLET_ADDRESS": account.address,
            "BASE_TRADING_PRIVATE_KEY": account.key.hex(),
            "BASE_HOT_WALLET_ADDRESS": account.address,
            "BASE_SHARED_WALLET_TRADING_ENABLED": "false",
        }
        with patch.dict(os.environ, env, clear=False):
            with self.assertRaises(BaseLiveEngineError):
                self.engine._env_wallet()
            with patch.dict(os.environ, {"BASE_SHARED_WALLET_TRADING_ENABLED": "true"}):
                self.assertEqual(self.engine._env_wallet(), account.address.lower())

    def test_only_one_pending_transaction_is_allowed(self):
        self.engine._record_operation(
            77, "SELL", TOKEN, "position-1",
            {"to": BASE_UNISWAP_V3_SWAP_ROUTER_02, "calldata": "0x12345678"},
        )
        with self.assertRaises(BaseLiveEngineError):
            self.engine._record_operation(
                77, "SELL", TOKEN, "position-2",
                {"to": BASE_UNISWAP_V3_SWAP_ROUTER_02, "calldata": "0x12345678"},
            )

    def test_exit_policy_uses_five_percent_stop_and_existing_profit_ladder(self):
        self.assertEqual(base_live_exit_reason(95, 100, 100, 1), "STOP_LOSS_5_PERCENT")
        self.assertEqual(base_live_exit_reason(103.5, 100, 103.5, 1), "TAKE_PROFIT_STEP_1")
        self.assertEqual(base_live_exit_reason(107, 100, 107, 2), "TAKE_PROFIT_STEP_2")
        self.assertEqual(base_live_exit_reason(104.5, 100, 110, 3), "TRAILING_TAKE_PROFIT_STEP_3")
        self.assertIsNone(base_live_exit_reason(102, 100, 105, 3))

    async def test_signing_path_persists_hash_before_one_broadcast_and_never_resends(self):
        account = Account.create()
        self.engine.readiness.persistent_storage_check = lambda: True
        env = {
            "BASE_LIVE_TRADING_ENABLED": "true",
            "BASE_SHARED_WALLET_TRADING_ENABLED": "true",
            "BASE_TRADING_RPC_URL": "https://base-rpc.invalid",
            "BASE_TRADING_WALLET_ADDRESS": account.address,
            "BASE_TRADING_PRIVATE_KEY": account.key.hex(),
            "BASE_HOT_WALLET_ADDRESS": account.address,
        }
        calls = []

        async def rpc(_session, method, _params):
            calls.append(method)
            return {
                "eth_chainId": hex(8453),
                "eth_gasPrice": hex(1_000_000_000),
                "eth_getTransactionCount": hex(7),
                "eth_estimateGas": hex(100_000),
                "eth_getBalance": hex(100_000_000_000_000_000),
                "eth_sendRawTransaction": TX_HASH,
            }[method]

        operation_id = self.engine._record_operation(
            77, "SELL", TOKEN, "position-1",
            {"to": BASE_UNISWAP_V3_SWAP_ROUTER_02, "calldata": "0x12345678"},
        )
        with self.engine.db() as db:
            db.execute("UPDATE base_live_settings SET enabled=1 WHERE uid=77")
        with patch.dict(os.environ, env, clear=False), \
                patch.object(self.engine, "_rpc", side_effect=rpc), \
                patch("base_live_engine.sign_base_transaction",
                      return_value={"transaction_hash": TX_HASH, "raw_transaction": "0xdead"}):
            first_hash = await self.engine._prepare_and_send(object(), operation_id)
            second_hash = await self.engine._prepare_and_send(object(), operation_id)
        self.assertEqual(first_hash, TX_HASH)
        self.assertIsNone(second_hash)
        self.assertEqual(calls.count("eth_sendRawTransaction"), 1)
        self.assertEqual(self.engine.pending_operation(77)["tx_hash"], TX_HASH)
        self.assertEqual(self.engine.pending_operation(77)["state"], "SUBMITTED")

    async def test_missing_persistent_storage_blocks_signing_and_pauses_mode(self):
        operation_id = self.engine._record_operation(
            77, "SELL", TOKEN, "position-1",
            {"to": BASE_UNISWAP_V3_SWAP_ROUTER_02, "calldata": "0x12345678"},
        )
        with self.engine.db() as db:
            db.execute("UPDATE base_live_settings SET enabled=1 WHERE uid=77")
        with patch.dict(os.environ, {"BASE_LIVE_TRADING_ENABLED": "true"}, clear=False), \
                patch.object(self.engine, "_rpc") as rpc, \
                patch("base_live_engine.sign_base_transaction") as signer:
            with self.assertRaises(BaseLiveEngineError):
                await self.engine._prepare_and_send(object(), operation_id)
        rpc.assert_not_called()
        signer.assert_not_called()
        self.assertEqual(self.engine.settings(77)["enabled"], 0)
        self.assertEqual(
            self.engine.status(77)["message"],
            "Base operation storage is not persistent; trading is paused.",
        )

    async def test_existing_nonzero_allowance_is_reset_before_exact_approval(self):
        account = Account.create()

        class FakeMarket:
            async def quote_exact_input(self, *_args, **_kwargs):
                return {"fee": 500, "amount_out_raw": 2 * 10**18, "block_number": 44}

        self.engine.settings(77)
        self.engine.save_budget(77, 15)
        with self.engine.db() as db:
            db.execute("UPDATE base_live_settings SET enabled=1 WHERE uid=77")
        with patch.dict(os.environ, {
            "BASE_LIVE_TRADING_ENABLED": "true",
            "BASE_TRADING_WALLET_ADDRESS": account.address,
            "BASE_TRADING_PRIVATE_KEY": account.key.hex(),
        }, clear=False), \
                patch.object(self.engine, "_market", return_value=FakeMarket()), \
                patch.object(self.engine, "_allowance", return_value=2_000_000):
            operation_id = await self.engine._queue_swap_or_approval(
                object(), 77, side="BUY", token=TOKEN, symbol="TST",
                pool=POOL, fee=500, decimals=18, amount_in_raw=1_000_000,
                entry_step=1, cycle_id="cycle", position_id="position",
            )
            reset = self.engine.pending_operation(77)
            self.assertEqual(reset["kind"], "REVOKE")
            reset_meta = __import__("json").loads(reset["metadata"])
            self.assertEqual(reset_meta["purpose"], "RESET_EXACT_ALLOWANCE")
            self.engine._update_operation(operation_id, state="CONFIRMED")
            await self.engine._advance_confirmed_approvals(object(), 77)
        with self.engine.db() as db:
            parent = db.execute(
                "SELECT metadata FROM base_live_operations WHERE id=?", (operation_id,)
            ).fetchone()
            child_id = __import__("json").loads(parent["metadata"])["child_operation_id"]
            child = db.execute(
                "SELECT kind,metadata FROM base_live_operations WHERE id=?", (child_id,)
            ).fetchone()
        self.assertEqual(child["kind"], "APPROVE")
        approval = __import__("json").loads(child["metadata"])
        self.assertEqual(approval["purpose"], "EXACT_SWAP_APPROVAL")
        approved_token, approved_amount = __import__("eth_abi").decode(
            ("address", "uint256"), bytes.fromhex(approval["calldata"][10:])
        )
        self.assertEqual(approved_token.lower(), BASE_UNISWAP_V3_SWAP_ROUTER_02.lower())
        self.assertEqual(approved_amount, 1_000_000)

    async def test_finalized_receipt_records_actual_buy_amounts(self):
        account = Account.create()
        env = {
            "BASE_TRADING_WALLET_ADDRESS": account.address,
            "BASE_TRADING_PRIVATE_KEY": account.key.hex(),
            "BASE_HOT_WALLET_ADDRESS": "0x3333333333333333333333333333333333333333",
        }
        cycle_id = "cycle-1"
        position_id = "position-1"
        self.engine.settings(77)
        self.engine.save_budget(77, 15)
        with self.engine.db() as db:
            db.execute("UPDATE base_live_settings SET enabled=1 WHERE uid=77")
        with self.engine.db() as db:
            db.execute("""INSERT INTO base_live_cycles(
                id,uid,token_address,symbol,pool_address,fee_tier,state,opened_at
            ) VALUES(?,?,?,?,?,?,'OPEN',?)""", (
                cycle_id, 77, TOKEN, "TST", POOL, 500, 1,
            ))
        metadata = {
            "side": "BUY",
            "token_address": TOKEN,
            "symbol": "TST",
            "pool_address": POOL,
            "fee_tier": 500,
            "token_decimals": 18,
            "input_token": BASE_USDC_ADDRESS,
            "output_token": TOKEN,
            "amount_in_raw": 10_000_000,
            "minimum_out_raw": 190 * 10**18,
            "entry_step": 1,
            "cycle_id": cycle_id,
            "position_id": position_id,
            "entry_candle_at": 1000,
        }
        with patch.dict(os.environ, {**env, "BASE_LIVE_TRADING_ENABLED": "true"}, clear=False):
            operation_id = self.engine._record_operation(77, "BUY", TOKEN, position_id, metadata)
        self.engine._update_operation(
            operation_id, tx_hash=TX_HASH, state="SUBMITTED"
        )
        receipt = {
            "blockNumber": "0x10",
            "status": "0x1",
            "logs": [
                {
                    "address": BASE_USDC_ADDRESS,
                    "topics": [
                        "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef",
                        topic_address(account.address),
                        topic_address(POOL),
                    ],
                    "data": hex(10_000_000),
                },
                {
                    "address": TOKEN,
                    "topics": [
                        "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef",
                        topic_address(POOL),
                        topic_address(account.address),
                    ],
                    "data": hex(200 * 10**18),
                },
            ],
        }

        async def rpc(_session, method, _params):
            if method == "eth_getTransactionReceipt":
                return receipt
            if method == "eth_getBlockByNumber":
                return {"number": "0x20"}
            raise AssertionError(method)

        with patch.dict(os.environ, env, clear=False), \
                patch.object(self.engine, "_rpc", side_effect=rpc):
            operation = self.engine.pending_operation(77)
            await self.engine._reconcile_operation(object(), operation)
        with self.engine.db() as db:
            position = db.execute(
                "SELECT * FROM base_live_positions WHERE id=?", (position_id,)
            ).fetchone()
            operation = db.execute(
                "SELECT state FROM base_live_operations WHERE id=?", (operation_id,)
            ).fetchone()
        self.assertEqual(operation["state"], "CONFIRMED")
        self.assertEqual(position["state"], "OPEN")
        self.assertEqual(int(position["token_raw"]), 200 * 10**18)
        self.assertEqual(position["cost_usdc_raw"], 10_000_000)
        self.assertAlmostEqual(position["entry_price_usdc"], 0.05)

    async def test_receipt_above_finalized_block_is_not_recorded(self):
        account = Account.create()
        env = {
            "BASE_TRADING_WALLET_ADDRESS": account.address,
            "BASE_TRADING_PRIVATE_KEY": account.key.hex(),
            "BASE_HOT_WALLET_ADDRESS": "0x3333333333333333333333333333333333333333",
        }
        self.engine.settings(77)
        self.engine.save_budget(77, 15)
        with self.engine.db() as db:
            db.execute("UPDATE base_live_settings SET enabled=1 WHERE uid=77")
        with patch.dict(os.environ, {**env, "BASE_LIVE_TRADING_ENABLED": "true"}, clear=False):
            operation_id = self.engine._record_operation(
                77, "BUY", TOKEN, "position-1",
                {
                "side": "BUY", "token_address": TOKEN, "symbol": "TST",
                "pool_address": POOL, "fee_tier": 500, "token_decimals": 18,
                "input_token": BASE_USDC_ADDRESS, "output_token": TOKEN,
                "amount_in_raw": 1_000_000, "minimum_out_raw": 1,
                "entry_step": 1, "cycle_id": "cycle", "position_id": "position-1",
                },
            )
        self.engine._update_operation(operation_id, tx_hash=TX_HASH, state="SUBMITTED")

        async def rpc(_session, method, _params):
            if method == "eth_getTransactionReceipt":
                return {"blockNumber": "0x30", "status": "0x1", "logs": []}
            if method == "eth_getBlockByNumber":
                return {"number": "0x20"}
            raise AssertionError(method)

        with patch.dict(os.environ, env, clear=False), \
                patch.object(self.engine, "_rpc", side_effect=rpc):
            await self.engine._reconcile_operation(
                object(), self.engine.pending_operation(77)
            )
        self.assertEqual(self.engine.pending_operation(77)["state"], "SUBMITTED")
        self.assertEqual(self.engine.open_positions(77), [])


if __name__ == "__main__":
    unittest.main()
