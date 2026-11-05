import unittest
from unittest.mock import AsyncMock, patch

from zerolife_ai_trader.binance_public import fetch_public_klines


class FakeResponse:
    status = 200

    def __init__(self, payload):
        self.payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def json(self):
        return self.payload


class FakeSession:
    def __init__(self, payload):
        self.payload = payload
        self.get_calls = []

    def get(self, *args, **kwargs):
        self.get_calls.append((args, kwargs))
        return FakeResponse(self.payload)


def klines(count=30, interval_ms=60_000):
    return [[i * interval_ms, "100", "102", "99", "101", "12"]
            for i in range(count)]


class BinancePublicClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_fetches_and_validates_public_klines(self):
        session = FakeSession(klines())
        result = await fetch_public_klines("BTCUSDT", "1m", limit=30, session=session)
        self.assertEqual(result["count"], 30)
        self.assertEqual(result["quality"], "complete")
        args, kwargs = session.get_calls[0]
        self.assertIn("/api/v3/klines", args[0])
        self.assertEqual(kwargs["params"], {
            "symbol": "BTCUSDT", "interval": "1m", "limit": 30,
        })
        self.assertFalse(kwargs["allow_redirects"])

    async def test_rejects_unsupported_symbol_interval_and_limit(self):
        for args in (("UNKNOWN", "1m"), ("BTCUSDT", "2m")):
            with self.subTest(args=args), self.assertRaises(ValueError):
                await fetch_public_klines(*args, session=FakeSession(klines()))
        with self.assertRaises(ValueError):
            await fetch_public_klines("BTCUSDT", "1m", limit=29,
                                      session=FakeSession(klines()))

    async def test_rejects_malformed_or_incomplete_provider_data(self):
        for payload in ([], [["bad"]], klines(29)):
            with self.subTest(payload=payload[:1]), self.assertRaises(ValueError):
                await fetch_public_klines("BTCUSDT", "1m", limit=30,
                                          session=FakeSession(payload))

    async def test_rejects_non_200_provider_response(self):
        class ErrorResponse(FakeResponse):
            status = 429
        class ErrorSession(FakeSession):
            def get(self, *args, **kwargs):
                return ErrorResponse(self.payload)
        with self.assertRaises(RuntimeError):
            await fetch_public_klines("BTCUSDT", "1m", limit=30,
                                      session=ErrorSession(klines()))


if __name__ == "__main__":
    unittest.main()
