import ast
import asyncio
import math
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

import aiohttp
import dexscreener_client as client
from custody_ledger import CustodyLedger
from market_context import fetch_main_market_context, fetch_sniper_market_context
from meme_discovery import MemeDiscovery


class Response:
    def __init__(self, status=200, data=None, headers=None, started=None, finish=None):
        self.status = status
        self.data = [] if data is None else data
        self.headers = headers or {}
        self.started, self.finish = started, finish

    async def __aenter__(self):
        if self.started:
            self.started.set()
        if self.finish:
            await self.finish.wait()
        return self

    async def __aexit__(self, *args):
        return False

    async def json(self):
        return self.data


class Session:
    def __init__(self, response=None):
        self.response = response or Response()
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, time.monotonic()))
        return self.response


class DexScreenerClientTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.discovery = MemeDiscovery(CustodyLedger(self.tmp.name + "/db"), AsyncMock(), [])
        self.watchlist = {"SOL": {"mint": "sol-mint", "decimals": 9}}

    def main_prices_function(self):
        # Load the real function without importing main or starting the bot.
        source = Path(__file__).resolve().parents[1].joinpath("main.py").read_text()
        tree = ast.parse(source)
        function = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)
                        and node.name == "get_token_prices")
        namespace = {"aiohttp": aiohttp, "math": math, "WATCHLIST": self.watchlist,
                     "dexscreener": client}
        exec(compile(ast.Module(body=[function], type_ignores=[]), "main.py", "exec"), namespace)
        return namespace["get_token_prices"]

    async def test_429_from_scanner_stops_core_prices_context_and_other_scanners(self):
        bad = Session(Response(429, headers={"Retry-After": "240"}))
        good = Session()
        with self.assertRaises(client.ProviderLimited):
            await self.discovery.fetch(bad, "/token-profiles/latest/v1")
        other = MemeDiscovery(self.discovery.ledger, AsyncMock(), [])
        for caller in (
            lambda: other.fetch(good, "/token-profiles/latest/v1"),
            lambda: fetch_main_market_context(good, self.watchlist, ["SOL"]),
            lambda: fetch_sniper_market_context(good, self.watchlist, ["SOL"]),
        ):
            with self.assertRaises(client.ProviderLimited):
                await caller()
        self.assertEqual(await self.main_prices_function()(good), {})
        self.assertEqual(len(bad.calls), 1)
        self.assertEqual(good.calls, [])
        self.assertGreaterEqual(other.cooldown_remaining(), 239)

    async def test_429_from_core_prices_is_shared_with_scanner_and_status(self):
        bad = Session(Response(429))
        self.assertEqual(await self.main_prices_function()(bad), {})
        self.assertGreaterEqual(self.discovery.cooldown_remaining(), 59)
        status = self.discovery.status_report()
        self.assertTrue(status["entry_paused"])
        self.assertIn("Shared market-data requests", status["message"])
        with self.assertRaises(client.ProviderLimited):
            await self.discovery.fetch(Session(), "/tokens/v1/solana/mint")
        self.assertFalse(self.discovery.entry_allowed("ANY"))

    async def test_all_callers_share_start_spacing_without_price_cache(self):
        state = client.scheduler()
        state.min_interval = .025
        session = Session()
        await self.discovery.fetch(session, "/tokens/v1/solana/mint")
        await fetch_main_market_context(session, self.watchlist, ["SOL"])
        await self.main_prices_function()(session)
        await self.discovery.fetch(session, "/tokens/v1/solana/mint")
        self.assertEqual(len(session.calls), 4)
        times = [when for _, when in session.calls]
        self.assertTrue(all(b - a >= .023 for a, b in zip(times, times[1:])), times)

    async def test_price_priority_passes_queued_scan_but_not_running_request(self):
        state = client.scheduler()
        state.min_interval = .001
        await state.acquire(client.DISCOVERY)
        order = []
        async def queued(priority, name):
            await state.acquire(priority)
            order.append(name)
            await state.release()
        scan = asyncio.create_task(queued(client.DISCOVERY, "scan"))
        await asyncio.sleep(0)
        context = asyncio.create_task(queued(client.CONTEXT, "context"))
        price = asyncio.create_task(queued(client.PRICE, "held_price"))
        await asyncio.sleep(0)
        self.assertEqual(order, [])  # Never cancel an in-flight market response.
        await state.release()
        await asyncio.gather(scan, context, price)
        self.assertEqual(order, ["held_price", "context", "scan"])
        self.assertEqual(state.waiters, [])

    async def test_priority_is_rechecked_during_pacing_sleep(self):
        state = client.scheduler()
        state.min_interval = .03
        state.next_request_at = time.monotonic() + .03
        order = []
        async def fetch(priority, name):
            async with client.request(Session(), "/tokens/v1/solana/" + name, priority=priority):
                order.append(name)
        low = asyncio.create_task(fetch(client.DISCOVERY, "scan"))
        await asyncio.sleep(.005)
        high = asyncio.create_task(fetch(client.PRICE, "held"))
        await asyncio.gather(low, high)
        self.assertEqual(order, ["held", "scan"])

    async def test_cancelled_waiter_and_failed_request_release_gate(self):
        state = client.scheduler()
        state.min_interval = 0
        await state.acquire(client.DISCOVERY)
        waiting = asyncio.create_task(state.acquire(client.PRICE))
        await asyncio.sleep(0)
        waiting.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiting
        await state.release()
        self.assertEqual(state.waiters, [])
        class BrokenSession:
            def get(self, *args, **kwargs):
                raise aiohttp.ClientConnectionError()
        with self.assertRaises(aiohttp.ClientConnectionError):
            async with client.request(BrokenSession(), "/tokens/v1/solana/mint"):
                pass
        self.assertFalse(state.active)
        async with client.request(Session(), "/tokens/v1/solana/mint"):
            pass

    async def test_429_rejects_already_queued_requests_without_new_http(self):
        started, finish = asyncio.Event(), asyncio.Event()
        session = Session(Response(429, started=started, finish=finish))
        async def fetch():
            async with client.request(session, "/tokens/v1/solana/mint"):
                pass
        first = asyncio.create_task(fetch())
        await started.wait()
        pending = asyncio.create_task(fetch())
        await asyncio.sleep(0)
        finish.set()
        results = await asyncio.gather(first, pending, return_exceptions=True)
        self.assertTrue(all(isinstance(error, client.ProviderLimited) for error in results))
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(client.scheduler().waiters, [])

    async def test_cooldown_honors_header_and_recovers_without_automatic_retries(self):
        state = client.scheduler()
        now = [1000]
        state.clock = lambda: now[0]
        bad = Session(Response(429, headers={"Retry-After": "240"}))
        with self.assertRaises(client.ProviderLimited):
            async with client.request(bad, "/tokens/v1/solana/mint"):
                pass
        self.assertEqual(state.cooldown_remaining(), 240)
        now[0] = 1241
        good = Session()
        async with client.request(good, "/tokens/v1/solana/mint"):
            pass
        self.assertEqual(state.rate_limit_failures, 0)
        self.assertEqual(len(good.calls), 1)

    async def test_independent_scanner_creation_never_clears_global_cooldown(self):
        await client.scheduler().limited("90")
        other = MemeDiscovery(self.discovery.ledger, AsyncMock(), [])
        self.assertGreaterEqual(other.cooldown_remaining(), 89)
        with self.assertRaises(client.ProviderLimited):
            await other.fetch(Session(), "/latest/dex/search?q=meme")

    async def test_only_dexscreener_origin_is_accepted(self):
        session = Session()
        for path in ("https://other.invalid/tokens", "//other.invalid/path",
                     "http://api.dexscreener.com/tokens", "not-a-path"):
            with self.assertRaises(ValueError):
                async with client.request(session, path):
                    pass
        self.assertEqual(session.calls, [])


if __name__ == "__main__":
    unittest.main()
