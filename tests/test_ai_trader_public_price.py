import unittest

from zerolife_ai_trader.binance_public import fetch_public_price
from zerolife_ai_trader.paper_market_runtime import execute_paper_signal_with_public_price


class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload, self.status = payload, status
    async def __aenter__(self): return self
    async def __aexit__(self, *args): return False
    async def json(self): return self.payload


class FakeSession:
    def __init__(self, payload, status=200):
        self.payload, self.status, self.calls = payload, status, []
    def get(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return FakeResponse(self.payload, self.status)


class PublicPriceTests(unittest.IsolatedAsyncioTestCase):
    async def test_fetches_validated_spot_price(self):
        session = FakeSession({"symbol": "SOLUSDC", "price": "145.25"})
        result = await fetch_public_price("SOLUSDC", session=session)
        self.assertEqual(result["price"], 145.25)
        self.assertEqual(result["source"], "binance_spot_public")
        args, kwargs = session.calls[0]
        self.assertIn("/api/v3/ticker/price", args[0])
        self.assertEqual(kwargs["params"], {"symbol": "SOLUSDC"})
        self.assertFalse(kwargs["allow_redirects"])

    async def test_rejects_invalid_or_mismatched_price(self):
        for payload in (
            {"symbol": "ETHUSDC", "price": "10"},
            {"symbol": "SOLUSDC", "price": "nan"},
            {"symbol": "SOLUSDC", "price": "0"},
            {"symbol": "SOLUSDC", "price": "bad"},
        ):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                await fetch_public_price("SOLUSDC", session=FakeSession(payload))

    async def test_rejects_unsupported_symbol_and_provider_failure(self):
        with self.assertRaises(ValueError):
            await fetch_public_price("UNKNOWN", session=FakeSession({}))
        with self.assertRaises(RuntimeError):
            await fetch_public_price("SOLUSDC", session=FakeSession({}, status=503))


if __name__ == "__main__":
    unittest.main()
