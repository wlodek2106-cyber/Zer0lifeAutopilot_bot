import copy
import hashlib
import json
import tempfile
import time
import unittest
from unittest.mock import AsyncMock

from solders.pubkey import Pubkey
from custody_ledger import CustodyLedger
from meme_discovery import (
    MemeDiscovery, TARGETED_SEARCH_TERMS, TOKEN, TOKEN_2022, pool_metrics,
)

MINT = str(Pubkey.from_bytes(hashlib.sha256(b"first-candidate").digest()))
OTHER = str(Pubkey.from_bytes(hashlib.sha256(b"second-candidate").digest()))
OTC = "MukLDtJ8Cx9DxLbeyLRSWPSposTMWuwHANbuaudpump"


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


def otc_mint_account():
    return {"owner":TOKEN_2022, "data":{"parsed":{"type":"mint","info":{
        "isInitialized":True,"decimals":6,"mintAuthority":None,"freezeAuthority":None,
        "extensions":[
            {"extension":"metadataPointer","state":{"authority":None,"metadataAddress":OTC}},
            {"extension":"tokenMetadata","state":{"updateAuthority":None,"mint":OTC,
                "name":"OTC","symbol":"OTC","additionalMetadata":[]}},
        ],
    }}}}


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

    async def test_scan_includes_more_targeted_memecoin_sources(self):
        await self.discovery.scan(None)
        paths = {call.args[1] for call in self.discovery.fetch.await_args_list}
        expected = {f"/latest/dex/search?q={term}" for term in TARGETED_SEARCH_TERMS}
        self.assertTrue(expected.issubset(paths))

    async def test_only_exactly_seeded_otc_mint_can_use_supported_token_2022_metadata(self):
        seeded = MemeDiscovery(self.ledger, AsyncMock(return_value={"value":[otc_mint_account()]}), [], {
            "OTC":{"mint":OTC,"decimals":6,"token_program":TOKEN_2022,"label":"OTC"},
        })

        async def fetch(session, path):
            if path.startswith("/tokens/"):
                return [pool(OTC, baseToken={"address":OTC,"symbol":"OTC"})]
            return [{"chainId":"solana","tokenAddress":OTC}]

        seeded.fetch = AsyncMock(side_effect=fetch)
        report = await seeded.scan(None)
        candidate = next(row for row in report["candidates"] if row["mint"] == OTC)
        self.assertEqual(candidate["code"], "OTC")
        self.assertEqual(candidate["token_program"], TOKEN_2022)
        self.assertTrue(seeded.entry_allowed("OTC"))

    async def test_dynamic_token_2022_assets_remain_rejected(self):
        unseeded = MemeDiscovery(self.ledger, AsyncMock(return_value={"value":[otc_mint_account()]}), [])
        unseeded.queue_whale_mint(OTC)

        async def fetch(session, path):
            if path.startswith("/tokens/"):
                return [pool(OTC, baseToken={"address":OTC,"symbol":"OTC"})]
            return [{"chainId":"solana","tokenAddress":OTC}]

        unseeded.fetch = AsyncMock(side_effect=fetch)
        report = await unseeded.scan(None)
        self.assertEqual(report["eligible"], 0)
        self.assertIn("mint token program differs", report["whale_rejections"][0]["reasons"][0])

    def test_candidate_shortlist_rotates_across_all_verified_candidates(self):
        now = 1_800_000
        with self.ledger.db() as db:
            for index in range(20):
                code = f"M_{index}"
                mint = str(Pubkey.from_bytes(hashlib.sha256(code.encode()).digest()))
                metrics = {"code": code, "score": 100-index, "eligible": True}
                db.execute("INSERT INTO custody_meme_assets VALUES(?,?,?,?,?)",
                    (code, mint, 6, json.dumps(metrics), now-30))
        self.discovery.blocked = False

        first = self.discovery.candidates(now=now)
        second = self.discovery.candidates(now=now+15*60)

        self.assertEqual(len(first), 8)
        self.assertEqual(len(second), 8)
        self.assertEqual([row["code"] for row in first], [f"M_{i}" for i in range(8)])
        self.assertEqual([row["code"] for row in second], [f"M_{i}" for i in range(8, 16)])
        self.assertEqual(len({row["code"] for row in first+second}), 16)

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

    async def test_whale_buy_prioritizes_a_mint_but_keeps_existing_candidate_screen(self):
        self.assertTrue(self.discovery.queue_whale_mint(MINT))
        self.assertFalse(self.discovery.queue_whale_mint(MINT))
        report=await self.discovery.scan(None)
        self.assertEqual(report["whale_prioritized"],1)
        self.assertEqual(report["whale_candidates"],1)
        self.assertTrue(self.discovery.entry_allowed(report["candidates"][0]["code"]))
        self.assertEqual(self.discovery.pending_whale_mints(),[])

    async def test_identity_collision_does_not_rebind_an_old_mint(self):
        code="M_"+hashlib.sha256(MINT.encode()).hexdigest()[:16]
        with self.ledger.db() as db:
            db.execute("INSERT INTO custody_meme_assets VALUES(?,?,?,?,?)",
                       (code,OTHER,6,json.dumps({"eligible":False}),int(time.time())))
        with self.assertRaises(ValueError):
            await self.discovery.scan(None)
        self.assertEqual(self.discovery.assets()[0]["mint"],OTHER)
        self.assertTrue(self.ledger.audit())

    async def test_new_seed_alias_preserves_existing_mint_identity(self):
        old_code = "M_" + hashlib.sha256(MINT.encode()).hexdigest()[:16]
        with self.ledger.db() as db:
            db.execute("INSERT INTO custody_meme_assets VALUES(?,?,?,?,?)",
                       (old_code,MINT,6,json.dumps({"eligible":False,"symbol":"CAT","code":old_code}),
                        int(time.time())))
        seeded = MemeDiscovery(self.ledger,self.rpc,[],{"WIF":{"mint":MINT,"decimals":6}})
        seeded.fetch = self.discovery.fetch

        report = await seeded.scan(None)

        matches = [row for row in report["candidates"] if row["mint"] == MINT]
        self.assertEqual(len(matches),1)
        self.assertEqual(matches[0]["code"],old_code)
        self.assertEqual(sum(row["mint"] == MINT for row in seeded.assets()),1)

    def test_unsafe_illiquid_young_pumped_or_malformed_pools_excluded(self):
        for changes in ({"liquidity":{"usd":9999}},{"volume":{"h24":99999}},
                        {"txns":{"h24":{"buys":1,"sells":1}}},{"priceUsd":"nan"},
                        {"pairCreatedAt":int(time.time()*1000)},{"priceChange":{"m5":36}},
                        {"dexId":"unknown"},{"chainId":"ethereum"},{"liquidity":"malformed"}):
            result=pool_metrics(pool(**changes))
            self.assertTrue(result is None or not result["eligible"],changes)

    def test_pool_liquidity_minimum_is_ten_thousand(self):
        accepted=pool_metrics(pool(liquidity={"usd":10000}))
        self.assertTrue(accepted["eligible"])
        rejected=pool_metrics(pool(liquidity={"usd":9999}))
        self.assertFalse(rejected["eligible"])
        self.assertIn("liquidity $9,999 below $10,000",rejected["rejection_reasons"])

    def test_pool_metrics_explain_each_failed_threshold(self):
        result=pool_metrics(pool(
            liquidity={"usd":5000},
            volume={"h24":30000},
            txns={"h24":{"buys":10,"sells":5}},
            pairCreatedAt=int((time.time()-3600)*1000),
        ))
        self.assertFalse(result["eligible"])
        self.assertEqual(result["rejection_reasons"],[
            "liquidity $5,000 below $10,000",
            "24h volume $30,000 below $100,000",
            "24h trades 15 below 200",
            "pool age 1.0h below 24h",
        ])

    async def test_scan_reports_specific_reasons_for_rejected_whale_mint(self):
        self.discovery.queue_whale_mint(MINT)

        async def fetch(session, path):
            if path.startswith("/tokens/"):
                return [pool(
                    MINT,
                    liquidity={"usd":5000},
                    volume={"h24":30000},
                    txns={"h24":{"buys":10,"sells":5}},
                    pairCreatedAt=int((time.time()-3600)*1000),
                )]
            return [{"chainId":"solana","tokenAddress":MINT}]

        self.discovery.fetch=AsyncMock(side_effect=fetch)
        report=await self.discovery.scan(None)
        self.assertEqual(report["eligible"],0)
        self.assertEqual(len(report["whale_rejections"]),1)
        rejection=report["whale_rejections"][0]
        self.assertEqual(rejection["mint"],MINT[:6]+"…"+MINT[-5:])
        self.assertEqual(rejection["reasons"],[
            "liquidity $5,000 below $10,000",
            "24h volume $30,000 below $100,000",
            "24h trades 15 below 200",
            "pool age 1.0h below 24h",
        ])

    async def test_scan_reports_on_chain_authority_rejection(self):
        self.discovery.queue_whale_mint(MINT)
        self.rpc.return_value={"value":[mint_account(mintAuthority=MINT)]}

        async def fetch(session, path):
            if path.startswith("/tokens/"):
                return [pool(MINT)]
            return [{"chainId":"solana","tokenAddress":MINT}]

        self.discovery.fetch=AsyncMock(side_effect=fetch)
        report=await self.discovery.scan(None)
        self.assertEqual(report["eligible"],0)
        self.assertEqual(report["whale_rejections"][0]["reasons"],[
            "mint authority is still enabled",
        ])
