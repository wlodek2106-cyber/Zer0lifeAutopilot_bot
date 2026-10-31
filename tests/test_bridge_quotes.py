import os
import tempfile
import unittest
from unittest.mock import patch

from bridge_quotes import (
    BASE_CHAIN_ID,
    BASE_USDC_ADDRESS,
    SOLANA_CHAIN_ID,
    SOLANA_USDC,
    BridgeQuoteError,
    BridgeQuoteService,
    parse_usdc_raw,
)


SOLANA_WALLET = "11111111111111111111111111111111"
BASE_WALLET = "0x4574D50bB68D0A65F10b62655338A1C8b4315040"
SOLANA_USDC_ASSET = SOLANA_USDC


def quote_payload(direction):
    if direction == "solana-to-base":
        from_chain, to_chain = SOLANA_CHAIN_ID, BASE_CHAIN_ID
        from_address, to_address = SOLANA_WALLET, BASE_WALLET
        from_token, to_token = SOLANA_USDC, BASE_USDC_ADDRESS
        transaction = {"data": "server-only-transaction-bytes", "svmTransactionVersion": 0}
        approval = None
        gas_symbol = "SOL"
    else:
        from_chain, to_chain = BASE_CHAIN_ID, SOLANA_CHAIN_ID
        from_address, to_address = BASE_WALLET, SOLANA_WALLET
        from_token, to_token = BASE_USDC_ADDRESS, SOLANA_USDC
        transaction = {
            "chainId": BASE_CHAIN_ID, "from": BASE_WALLET,
            "to": "0x" + "b" * 40, "data": "0x1234",
            "value": "0x0", "gasLimit": "0x5208",
        }
        approval = "0x" + "c" * 40
        gas_symbol = "ETH"
    return {
        "tool": "test-bridge",
        "toolDetails": {"name": "Test Bridge"},
        "action": {
            "fromChainId": from_chain, "toChainId": to_chain,
            "fromAmount": "5000000", "fromAddress": from_address,
            "toAddress": to_address,
            "fromToken": {"address": from_token, "symbol": "USDC"},
            "toToken": {"address": to_token, "symbol": "USDC"},
        },
        "estimate": {
            "fromAmount": "5000000", "toAmount": "4980000",
            "toAmountMin": "4950000", "approvalAddress": approval,
            "feeCosts": [{
                "name": "Provider fee", "amountUSD": "0.02",
                "included": True, "token": {"symbol": "USDC"},
            }],
            "gasCosts": [{
                "type": "SEND", "amountUSD": "0.01",
                "token": {"symbol": gas_symbol},
            }],
        },
        "transactionRequest": transaction,
    }


class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def json(self):
        return self.payload


class FakeSession:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status = status
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return FakeResponse(self.payload, self.status)


class FakeLedger:
    def __init__(self, available=0):
        self.available = available
        self.assets = []

    def balance(self, uid, asset):
        self.assets.append((uid, asset))
        return self.available


class BridgeQuoteTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_file = os.path.join(self.temp_dir.name, "ledger.sqlite")
        self.ledger = FakeLedger()
        self.service = BridgeQuoteService(self.db_file, self.ledger, SOLANA_WALLET)

    def tearDown(self):
        self.temp_dir.cleanup()

    def service_with_balance(self, available):
        return BridgeQuoteService(self.db_file, FakeLedger(available), SOLANA_WALLET)

    def test_usdc_amount_requires_exact_six_decimal_precision_and_is_capped(self):
        self.assertEqual(parse_usdc_raw("0.000001"), 1)
        self.assertEqual(parse_usdc_raw("10.25"), 10_250_000)
        for value in ("0", "-1", "1.0000001", "10000.000001", "NaN", "Infinity", True):
            with self.subTest(value=value), self.assertRaises(BridgeQuoteError):
                parse_usdc_raw(value)

    async def test_sol_to_base_quote_is_read_only_and_hides_provider_transaction(self):
        session = FakeSession(quote_payload("solana-to-base"))
        with patch.dict(os.environ, {
            "LIFI_API_KEY": "test-only-key",
            "BASE_HOT_WALLET_ADDRESS": BASE_WALLET,
        }):
            quote = await self.service.quote(session, 77, "solana-to-base", "5")

        self.assertEqual(len(session.calls), 1)
        url, request = session.calls[0]
        self.assertEqual(url, "https://li.quest/v1/quote")
        self.assertEqual(request["params"]["fromChain"], str(SOLANA_CHAIN_ID))
        self.assertEqual(request["params"]["toToken"], BASE_USDC_ADDRESS)
        self.assertEqual(request["headers"]["x-lifi-api-key"], "test-only-key")
        self.assertEqual(self.ledger.assets, [(77, SOLANA_USDC_ASSET)])
        self.assertEqual(quote["amount_out_min"], "4.95")
        self.assertEqual(quote["source_available"], "0")
        self.assertFalse(quote["sufficient_source_balance"])
        self.assertTrue(quote["quote_only"])
        self.assertFalse(quote["transfer_enabled"])
        self.assertNotIn("transactionRequest", quote)
        self.assertNotIn("server-only-transaction-bytes", str(quote))
        self.assertFalse(hasattr(self.service, "submit"))
        self.assertFalse(hasattr(self.service, "approve"))

    async def test_base_to_sol_quote_works_without_balance_or_signer(self):
        session = FakeSession(quote_payload("base-to-solana"))
        service = self.service_with_balance(0)
        with patch.dict(os.environ, {
            "LIFI_API_KEY": "test-only-key",
            "BASE_HOT_WALLET_ADDRESS": BASE_WALLET,
        }):
            quote = await service.quote(session, 77, "base-to-solana", "5")

        self.assertEqual(requested := session.calls[0][1]["params"]["fromToken"], BASE_USDC_ADDRESS)
        self.assertEqual(requested, BASE_USDC_ADDRESS)
        self.assertEqual(quote["direction"], "base-to-solana")
        self.assertTrue(quote["requires_approval"])
        self.assertFalse(quote["transfer_enabled"])
        self.assertNotIn("transactionRequest", quote)

    async def test_wrong_route_is_rejected_and_never_displayed(self):
        bad = quote_payload("solana-to-base")
        bad["action"]["toAddress"] = "0x" + "9" * 40
        session = FakeSession(bad)
        with patch.dict(os.environ, {
            "LIFI_API_KEY": "test-only-key",
            "BASE_HOT_WALLET_ADDRESS": BASE_WALLET,
        }):
            with self.assertRaises(BridgeQuoteError):
                await self.service.quote(session, 77, "solana-to-base", "5")

    async def test_missing_key_fails_before_provider_request(self):
        session = FakeSession(quote_payload("solana-to-base"))
        env = {"LIFI_API_KEY": "", "BASE_HOT_WALLET_ADDRESS": BASE_WALLET}
        with patch.dict(os.environ, env):
            with self.assertRaisesRegex(BridgeQuoteError, "API key"):
                await self.service.quote(session, 77, "solana-to-base", "5")
        self.assertEqual(session.calls, [])

    async def test_quote_requests_are_rate_limited_per_user(self):
        self.service._reserve_quote_attempt(77, 1000)
        with self.assertRaisesRegex(BridgeQuoteError, "few seconds"):
            self.service._reserve_quote_attempt(77, 1001)

    async def test_provider_rate_limit_is_reported_without_sending(self):
        session = FakeSession({}, status=429)
        with patch.dict(os.environ, {
            "LIFI_API_KEY": "test-only-key",
            "BASE_HOT_WALLET_ADDRESS": BASE_WALLET,
        }):
            with self.assertRaisesRegex(BridgeQuoteError, "rate-limiting"):
                await self.service.quote(session, 77, "solana-to-base", "5")
        self.assertEqual(len(session.calls), 1)


if __name__ == "__main__":
    unittest.main()
