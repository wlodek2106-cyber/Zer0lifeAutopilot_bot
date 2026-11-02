import time
import unittest
from unittest.mock import patch

import geckoterminal_client as client


class Response:
    def __init__(self, status=200, headers=None):
        self.status = status
        self.headers = headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class Session:
    def __init__(self, responses=None):
        self.responses = list(responses or [Response()])
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, time.monotonic()))
        return self.responses.pop(0)


class GeckoTerminalClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_relative_network_paths_are_allowed(self):
        session = Session()
        for path in (
            "https://attacker.invalid/networks/solana/pools",
            "//attacker.invalid/networks/solana/pools",
            "/tokens/solana/mint",
        ):
            with self.subTest(path=path):
                with self.assertRaisesRegex(ValueError, "Invalid GeckoTerminal"):
                    async with client.request(session, path):
                        pass
        self.assertEqual(session.calls, [])

    async def test_429_honors_retry_after_and_blocks_followup_requests(self):
        session = Session([Response(429, {"Retry-After": "120"}), Response()])

        with self.assertRaises(client.ProviderLimited):
            async with client.request(session, "/networks/solana/trending_pools"):
                pass

        self.assertGreaterEqual(client.cooldown_remaining(), 119)
        with self.assertRaises(client.ProviderLimited):
            async with client.request(session, "/networks/solana/trending_pools"):
                pass
        self.assertEqual(len(session.calls), 1)

    async def test_request_spacing_is_shared(self):
        session = Session([Response(), Response()])
        with patch.object(client, "MIN_REQUEST_INTERVAL", 0.02):
            for _ in range(2):
                async with client.request(session, "/networks/solana/trending_pools"):
                    pass

        self.assertEqual(len(session.calls), 2)
        self.assertGreaterEqual(session.calls[1][1] - session.calls[0][1], 0.018)
