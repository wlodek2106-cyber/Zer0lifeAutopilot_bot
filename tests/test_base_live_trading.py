import unittest
from unittest.mock import patch

import base_live_trading as base
from eth_abi import encode
from eth_account import Account
from eth_utils import keccak


TOKEN = "0x1111111111111111111111111111111111111111"
POOL = "0x2222222222222222222222222222222222222222"
WALLET = "0x3333333333333333333333333333333333333333"
USDC = base.BASE_USDC_ADDRESS


def encoded_address(value):
    return "0x" + encode(("address",), (value,)).hex()


class BaseV3EncodingTests(unittest.TestCase):
    def test_live_mode_is_off_without_explicit_kill_switch_and_dedicated_signer(self):
        result = base.live_environment_status({})
        self.assertFalse(result["ready"])
        self.assertFalse(result["kill_switch_enabled"])
        self.assertFalse(result["wallet_configured"])
        self.assertFalse(result["wallet_separate_from_bridge"])
        self.assertFalse(result["signer_matches_wallet"])

    def test_live_mode_stays_off_when_signer_does_not_match_wallet(self):
        result = base.live_environment_status({
            "BASE_LIVE_TRADING_ENABLED": "true",
            "BASE_TRADING_RPC_URL": "https://base-rpc.example",
            "BASE_TRADING_WALLET_ADDRESS": WALLET,
            "BASE_TRADING_PRIVATE_KEY": "not-a-private-key",
        })
        self.assertFalse(result["ready"])
        self.assertTrue(result["kill_switch_enabled"])
        self.assertFalse(result["signer_matches_wallet"])

    def test_live_mode_stays_off_without_a_matching_dedicated_signer(self):
        result = base.live_environment_status({
            "BASE_LIVE_TRADING_ENABLED": "true",
            "BASE_TRADING_RPC_URL": "https://base-rpc.example",
            "BASE_TRADING_WALLET_ADDRESS": WALLET,
            "BASE_TRADING_PRIVATE_KEY": "not-a-private-key",
        })
        self.assertTrue(result["executor_ready"])
        self.assertFalse(result["ready"])

    def test_dedicated_wallet_must_differ_from_bridge_wallet(self):
        result = base.live_environment_status({
            "BASE_LIVE_TRADING_ENABLED": "true",
            "BASE_TRADING_RPC_URL": "https://base-rpc.example",
            "BASE_TRADING_WALLET_ADDRESS": WALLET,
            "BASE_HOT_WALLET_ADDRESS": WALLET.upper().replace("0X", "0x"),
            "BASE_TRADING_PRIVATE_KEY": "not-a-private-key",
        })
        self.assertFalse(result["wallet_separate_from_bridge"])
        self.assertFalse(result["ready"])

    def test_swap_calldata_uses_exact_amount_minimum_and_wallet_recipient(self):
        data = base.encode_exact_input_single(
            USDC, TOKEN, 500, WALLET, 1_000_000, 990_000
        )
        self.assertEqual(data[:10], "0x" + keccak(
            text="exactInputSingle((address,address,uint24,address,uint256,uint256,uint160))"
        )[:4].hex())
        decoded = __import__("eth_abi").decode(
            ("(address,address,uint24,address,uint256,uint256,uint160)",),
            bytes.fromhex(data[10:]),
        )[0]
        self.assertEqual(decoded[0].lower(), USDC.lower())
        self.assertEqual(decoded[1].lower(), TOKEN.lower())
        self.assertEqual(decoded[3].lower(), WALLET.lower())
        self.assertEqual(decoded[4:6], (1_000_000, 990_000))

    def test_swap_call_is_wrapped_in_deadline_enforced_multicall(self):
        swap = base.encode_exact_input_single(
            USDC, TOKEN, 500, WALLET, 1_000_000, 990_000
        )
        wrapped = base.encode_deadline_multicall(swap, 1_800_000_000)
        self.assertEqual(wrapped[:10], "0x" + keccak(
            text="multicall(uint256,bytes[])"
        )[:4].hex())
        deadline, calls = __import__("eth_abi").decode(
            ("uint256", "bytes[]"), bytes.fromhex(wrapped[10:])
        )
        self.assertEqual(deadline, 1_800_000_000)
        self.assertEqual("0x" + calls[0].hex(), swap)

    def test_approval_is_exact_and_slippage_has_a_hard_maximum(self):
        data = base.encode_exact_approval(
            base.BASE_UNISWAP_V3_SWAP_ROUTER_02, 1_000_000
        )
        self.assertEqual(data[:10], "0x095ea7b3")
        spender, amount = __import__("eth_abi").decode(
            ("address", "uint256"), bytes.fromhex(data[10:])
        )
        self.assertEqual(spender.lower(), base.BASE_UNISWAP_V3_SWAP_ROUTER_02.lower())
        self.assertEqual(amount, 1_000_000)
        self.assertEqual(base.minimum_output(10_001, 100), 9_900)
        with self.assertRaises(base.BaseLiveTradingError):
            base.minimum_output(10_001, 101)

    def test_quote_and_pool_data_are_strictly_decoded(self):
        raw = "0x" + encode(
            ("uint256", "uint160", "uint32", "uint256"),
            (12_345, 99, 2, 80_000),
        ).hex()
        self.assertEqual(base.decode_quoter_v2_result(raw)["amount_out_raw"], 12_345)
        with self.assertRaises(base.BaseLiveTradingError):
            base.decode_quoter_v2_result("0x00")

    def test_only_base_usdc_pools_and_supported_fee_tiers_can_be_requested(self):
        data = base.encode_factory_get_pool(USDC, TOKEN, 500)
        self.assertTrue(data.startswith("0x"))
        with self.assertRaises(base.BaseLiveTradingError):
            base.encode_factory_get_pool(USDC, TOKEN, 250)
        with self.assertRaises(base.BaseLiveTradingError):
            base.encode_exact_input_single(USDC, USDC, 500, WALLET, 1, 1)


class BaseLiveReadinessTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_setup_returns_off_without_attempting_rpc(self):
        report = await base.BaseLiveReadiness({}).report(None)
        self.assertFalse(report["ready"])
        self.assertFalse(report["can_trade"])
        self.assertTrue(report["executor_ready"])
        self.assertIsNone(report["usdc_balance"])
        self.assertIsNone(report["eth_balance"])
        self.assertEqual(report["wallet_address"], "")
        self.assertIn("no transaction can be sent", report["message"].lower())
        self.assertFalse(report["persistent_storage_ready"])

    async def test_checks_base_network_even_without_trading_wallet(self):
        readiness = base.BaseLiveReadiness(
            {"BASE_RPC_URL": "https://base-rpc.example"},
            persistent_storage_check=lambda: True,
        )

        async def fake_rpc(_session, method, _params):
            self.assertEqual(method, "eth_chainId")
            return "0x2105"

        with patch.object(readiness, "_rpc", side_effect=fake_rpc):
            report = await readiness.report(None)

        self.assertEqual(report["chain_id"], base.BASE_CHAIN_ID)
        self.assertIn(
            {"name": "Base mainnet (8453)", "ok": True},
            report["checks"],
        )
        self.assertFalse(report["wallet_configured"])
        self.assertFalse(report["can_trade"])
        self.assertIsNone(report["usdc_balance"])
        self.assertIsNone(report["eth_balance"])
        self.assertIn("Base Mainnet is verified", report["message"])
        self.assertIn("matching signer", report["message"])

    async def test_live_mode_requires_persistent_operation_storage(self):
        account = Account.create()
        env = {
            "BASE_LIVE_TRADING_ENABLED": "true",
            "BASE_TRADING_RPC_URL": "https://base-rpc.example",
            "BASE_TRADING_WALLET_ADDRESS": account.address,
            "BASE_HOT_WALLET_ADDRESS": "0x4444444444444444444444444444444444444444",
            "BASE_TRADING_PRIVATE_KEY": account.key.hex(),
        }
        async def fake_rpc(_session, method, _params):
            if method == "eth_chainId":
                return "0x2105"
            if method == "eth_getCode":
                return "0x"
            if method == "eth_getBalance":
                return hex(10**16)
            if method == "eth_call":
                return "0x" + encode(("uint256",), (50_000_000,)).hex()
            raise AssertionError(method)

        for storage_ready in (False, True):
            readiness = base.BaseLiveReadiness(
                env, persistent_storage_check=lambda: storage_ready
            )
            with patch.object(readiness, "_rpc", side_effect=fake_rpc):
                report = await readiness.report(None)
            self.assertEqual(report["persistent_storage_ready"], storage_ready)
            self.assertEqual(report["ready"], storage_ready)
            self.assertEqual(report["can_trade"], storage_ready)


class BaseV3PoolVerificationTests(unittest.IsolatedAsyncioTestCase):
    async def test_pool_must_match_factory_pair_fee_and_requested_token(self):
        class FakeRpc:
            async def __call__(self, method, params):
                if method == "eth_blockNumber":
                    return "0x100"
                data = params[0]["data"]
                selector = data[:10]
                if selector == "0x" + keccak(text="factory()")[:4].hex():
                    return encoded_address(base.BASE_UNISWAP_V3_FACTORY)
                if selector == "0x" + keccak(text="token0()")[:4].hex():
                    return encoded_address(USDC)
                if selector == "0x" + keccak(text="token1()")[:4].hex():
                    return encoded_address(TOKEN)
                if selector == "0x" + keccak(text="fee()")[:4].hex():
                    return "0x" + encode(("uint24",), (500,)).hex()
                if params[0]["to"].lower() == base.BASE_UNISWAP_V3_FACTORY.lower():
                    return encoded_address(POOL)
                if params[0]["to"].lower() == base.BASE_UNISWAP_V3_QUOTER_V2.lower():
                    return "0x" + encode(
                        ("uint256", "uint160", "uint32", "uint256"),
                        (2_000_000, 123, 0, 90_000),
                    ).hex()
                raise AssertionError("Unexpected RPC contract call")

        market = base.BaseV3Market(FakeRpc())
        pool = await market.verify_pool(POOL, TOKEN)
        self.assertEqual(pool.address, POOL.lower())
        quote = await market.quote_exact_input(POOL, USDC, 1_000_000, TOKEN)
        self.assertEqual(quote["token_out"], TOKEN.lower())
        self.assertEqual(quote["amount_out_raw"], 2_000_000)
        self.assertEqual(quote["block_number"], 256)

    async def test_rejects_non_uniswap_factory_and_wrong_pool_pair(self):
        class FakeRpc:
            def __init__(self, factory):
                self.factory = factory

            async def __call__(self, method, params):
                data = params[0]["data"]
                selector = data[:10]
                if selector == "0x" + keccak(text="factory()")[:4].hex():
                    return encoded_address(self.factory)
                if selector == "0x" + keccak(text="token0()")[:4].hex():
                    return encoded_address(USDC)
                if selector == "0x" + keccak(text="token1()")[:4].hex():
                    return encoded_address(WALLET)
                if selector == "0x" + keccak(text="fee()")[:4].hex():
                    return "0x" + encode(("uint24",), (500,)).hex()
                if params[0]["to"].lower() == base.BASE_UNISWAP_V3_FACTORY.lower():
                    return encoded_address(POOL)
                raise AssertionError("Unexpected RPC contract call")

        with self.assertRaises(base.BaseLiveTradingError):
            await base.BaseV3Market(FakeRpc(WALLET)).verify_pool(POOL, TOKEN)
        with self.assertRaises(base.BaseLiveTradingError):
            await base.BaseV3Market(FakeRpc(base.BASE_UNISWAP_V3_FACTORY)).verify_pool(
                POOL, TOKEN
            )


if __name__ == "__main__":
    unittest.main()
