import copy
import hashlib
import json
import tempfile
import time
import unittest
from unittest.mock import AsyncMock

from solders.pubkey import Pubkey
from custody_ledger import CustodyLedger
from meme_discovery import MemeDiscovery, TOKEN, pool_metrics

MINT = str(Pubkey.from_bytes(hashlib.sha256(b"first-candidate").digest()))
OTHER = str(Pubkey.from_bytes(hashlib.sha256(b"second-candidate").digest()))


def pool(mint=MINT, **updates):
    row = {"chainId":"solana","dexId":"raydium","baseToken":{"address":mint,"symbol":"CAT<script>"},
           "liquidity":{"usd":200000},"volume":{"h24":300000},
           "txns":{"h24":{"buys":600,"sells":400}}, "pairCreatedAt":int((time.time()-172800)*1000),
           "priceUsd":"0.01","priceChange":{"m5":1,"h1":10}}
    row.update(updates)
    return row


def mint_account(**updates):
    info = {"isInitialized":True,"decimals":6,"mintAuthority":None,"freezeAuthority":None}
    info.update(updates)
    return {"owner":TOKEN,"data":{"parsed":{"type":"mint","info":info}}}


class DiscoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ledger = CustodyLedger(self.tmp.name+"/db")
        self.rpc = AsyncMock(return_value={"value":[mint_account(),mint_account()]})
        self.discovery = MemeDiscovery(self.ledger,self.rpc,[])

        async def fetch(session, path):
            if path.startswith("/tokens/"):
                return [pool(),pool(OTHER)]
            return [{"chainId":"solana","tokenAddress":MINT},{"chainId":"solana","tokenAddress":OTHER}]
        self.discovery.fetch = AsyncMock(side_effect=fetch)

    async def test_scan_is_bounded_verified_and_not_a_profit_forecast(self):
        report = await self.discovery.scan(None)
        self.assertFalse(report["complete"])
        self.assertEqual(report["scanned"],2)
        self.assertEqual(report["eligible"],2)
        code=report["candidates"][0]["code"]
        self.assertTrue(self.discovery.entry_allowed(code))
        self.assertNotIn("<",report["candidates"][0]["symbol"])
        self.assertEqual(self.rpc.await_args.args[1],"getMultipleAccounts")
        self.assertLessEqual(len(self.discovery.candidates()),8)
        self.assertTrue(self.ledger.audit())

    async def test_authorities_token2022_invalid_or_uninitialized_mints_excluded(self):
        for bad in (mint_account(mintAuthority=MINT),mint_account(freezeAuthority=MINT),
                    mint_account(isInitialized=False),mint_account(decimals=True),
                    {**mint_account(),"owner":"Token-2022"},None):
            self.rpc.return_value={"value":[bad,bad]}
            result=await self.discovery.scan(None)
            self.assertEqual(result["eligible"],0)
            self.assertEqual(self.discovery.candidates(),[])

    async def test_failed_or_expired_scan_stops_buys_but_retains_identity(self):
        report=await self.discovery.scan(None)
        code=report["candidates"][0]["code"]
        self.discovery.fail()
        self.assertFalse(self.discovery.entry_allowed(code))
        self.assertEqual(self.discovery.candidates(),[])
        self.assertEqual(len(self.discovery.assets()),2)
        self.discovery.blocked=False
        with self.ledger.db() as db:
            db.execute("UPDATE custody_meme_assets SET checked_at=?",(int(time.time())-1801,))
        self.assertFalse(self.discovery.entry_allowed(code))

    async def test_price_refresh_cannot_re_admit_or_delete_held_assets(self):
        report=await self.discovery.scan(None)
        code=report["candidates"][0]["code"]
        self.discovery.fail()
        prices=await self.discovery.prices(None,[code])
        self.assertGreater(prices[code],0)
        self.assertFalse(self.discovery.entry_allowed(code))

    async def test_registry_survives_restart_but_requires_a_fresh_scan(self):
        before=await self.discovery.scan(None)
        restarted=MemeDiscovery(self.ledger,self.rpc,[])
        self.assertEqual(len(restarted.assets()),2)
        self.assertFalse(restarted.entry_allowed(before["candidates"][0]["code"]))

    async def test_seed_keeps_existing_position_symbol_and_deduplicates_mint(self):
        service=MemeDiscovery(self.ledger,self.rpc,[],{"WIF":{"mint":MINT,"decimals":6}})
        service.fetch=self.discovery.fetch
        report=await service.scan(None)
        matches=[r for r in report["candidates"] if r["mint"]==MINT]
        self.assertEqual(len(matches),1)
        self.assertEqual(matches[0]["code"],"WIF")

    async def test_identity_collision_does_not_rebind_an_old_mint(self):
        code="M_"+hashlib.sha256(MINT.encode()).hexdigest()[:16]
        with self.ledger.db() as db:
            db.execute("INSERT INTO custody_meme_assets VALUES(?,?,?,?,?)",
                       (code,OTHER,6,json.dumps({"eligible":False}),int(time.time())))
        with self.assertRaises(ValueError):
            await self.discovery.scan(None)
        self.assertEqual(self.discovery.assets()[0]["mint"],OTHER)
        self.assertTrue(self.ledger.audit())

    def test_unsafe_illiquid_young_pumped_or_malformed_pools_excluded(self):
        for changes in ({"liquidity":{"usd":99999}},{"volume":{"h24":99999}},
                        {"txns":{"h24":{"buys":1,"sells":1}}},{"priceUsd":"nan"},
                        {"pairCreatedAt":int(time.time()*1000)},{"priceChange":{"m5":36}},
                        {"dexId":"unknown"},{"chainId":"ethereum"},{"liquidity":"malformed"}):
            result=pool_metrics(pool(**changes))
            self.assertTrue(result is None or not result["eligible"],changes)
