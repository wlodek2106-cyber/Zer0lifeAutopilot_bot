import asyncio
import hashlib
import unittest

from solders.pubkey import Pubkey
from solders.signature import Signature

from whale_stream import (
    DEX_PROGRAMS,
    MAX_WHALE_LAMPORTS,
    MIN_WHALE_LAMPORTS,
    HeliusWhaleStream,
    parse_whale_buy,
)


BUYER = str(Pubkey.from_bytes(hashlib.sha256(b"whale-buyer").digest()))
MINT = str(Pubkey.from_bytes(hashlib.sha256(b"whale-token").digest()))
OTHER_MINT = str(Pubkey.from_bytes(hashlib.sha256(b"other-whale-token").digest()))
TOKEN_ACCOUNT = str(Pubkey.from_bytes(hashlib.sha256(b"token-account").digest()))
SIGNATURE = str(Signature.default())
PUMP = DEX_PROGRAMS["Pump.fun"]


def token_balance(mint, amount, index=2, owner=BUYER):
    return {
        "accountIndex": index,
        "mint": mint,
        "owner": owner,
        "uiTokenAmount": {"amount": str(amount), "decimals": 6, "uiAmount": amount / 1_000_000},
    }


def notification(sol_spend=2_000_000_000, *, program=PUMP, mint=MINT, pre_amount=10, post_amount=20,
                 err=None, extra_post=None, signature=SIGNATURE, new_token_account=False):
    fee = 5_000
    rent = 2_039_280 if new_token_account else 0
    account_keys = [
        {"pubkey": BUYER, "signer": True, "writable": True},
        {"pubkey": program, "signer": False, "writable": False},
        {"pubkey": TOKEN_ACCOUNT, "signer": False, "writable": True},
    ]
    pre_balances = [20_000_000_000, 0, 0 if new_token_account else 2_039_280]
    post_balances = [20_000_000_000 - sol_spend - fee - rent, 0,
                     2_039_280 if new_token_account else 2_039_280]
    pre_tokens = [] if new_token_account else [token_balance(mint, pre_amount)]
    post_tokens = [token_balance(mint, post_amount)]
    if extra_post:
        post_tokens.extend(extra_post)
    return {
        "method": "transactionNotification",
        "params": {"result": {
            "slot": 123,
            "signature": signature,
            "transaction": {"message": {
                "accountKeys": account_keys,
                "instructions": [{"programId": program}],
            }, "signatures": [signature]},
            "meta": {
                "err": err,
                "fee": fee,
                "preBalances": pre_balances,
                "postBalances": post_balances,
                "preTokenBalances": pre_tokens,
                "postTokenBalances": post_tokens,
            },
        }},
    }


class WhaleSignalTests(unittest.IsolatedAsyncioTestCase):
    def test_valid_confirmed_program_buy_in_1_to_10_sol_range(self):
        event = parse_whale_buy(notification(sol_spend=2_500_000_000), observed_at=100)
        self.assertEqual(event["mint"], MINT)
        self.assertEqual(event["program"], "Pump.fun")
        self.assertEqual(event["sol_lamports"], 2_500_000_000)
        self.assertEqual(event["seen_at"], 100)

    def test_inclusive_thresholds_and_out_of_range_buys(self):
        self.assertIsNotNone(parse_whale_buy(notification(sol_spend=MIN_WHALE_LAMPORTS)))
        self.assertIsNotNone(parse_whale_buy(notification(sol_spend=MAX_WHALE_LAMPORTS)))
        self.assertIsNone(parse_whale_buy(notification(sol_spend=MIN_WHALE_LAMPORTS - 1)))
        self.assertIsNone(parse_whale_buy(notification(sol_spend=MAX_WHALE_LAMPORTS + 1)))

    def test_created_token_account_rent_is_not_counted_as_whale_spend(self):
        event = parse_whale_buy(notification(sol_spend=3_000_000_000, new_token_account=True))
        self.assertEqual(event["sol_lamports"], 3_000_000_000)

    def test_rejects_failed_unsupported_sell_and_ambiguous_transactions(self):
        self.assertIsNone(parse_whale_buy(notification(err={"InstructionError": [0, "failed"]})))
        self.assertIsNone(parse_whale_buy(notification(program=str(Pubkey.from_bytes(b"x" * 32)))))
        self.assertIsNone(parse_whale_buy(notification(pre_amount=20, post_amount=10)))
        self.assertIsNone(parse_whale_buy(notification(
            extra_post=[token_balance(OTHER_MINT, 5_000_000)])))

    def test_rejects_malformed_signatures_and_missing_transaction_meta(self):
        self.assertIsNone(parse_whale_buy(notification(signature="not-a-signature")))
        payload = notification()
        del payload["params"]["result"]["meta"]["err"]
        self.assertIsNone(parse_whale_buy(payload))

    async def test_free_stream_config_subscribes_only_to_supported_programs_and_confirmed_data(self):
        stream = HeliusWhaleStream("unit-test-key")
        payload = stream.subscription()
        self.assertEqual(payload["method"], "transactionSubscribe")
        self.assertEqual(payload["params"][0]["accountInclude"], list(DEX_PROGRAMS.values()))
        self.assertEqual(payload["params"][1]["commitment"], "confirmed")
        self.assertEqual(payload["params"][1]["encoding"], "jsonParsed")
        self.assertNotIn("api-key", str(payload))
        for address in DEX_PROGRAMS.values():
            self.assertEqual(str(Pubkey.from_string(address)), address)

    def test_lookup_table_program_address_is_resolved_before_filtering(self):
        payload = notification(sol_spend=2_000_000_000)
        value = payload["params"]["result"]
        message = value["transaction"]["message"]
        message["accountKeys"] = [message["accountKeys"][0], message["accountKeys"][2]]
        message["instructions"] = [{"programIdIndex": 2}]
        value["meta"]["loadedAddresses"] = {"writable": [], "readonly": [PUMP]}
        value["meta"]["preBalances"] = [20_000_000_000, 2_039_280, 0]
        value["meta"]["postBalances"] = [17_999_995_000, 2_039_280, 0]
        value["meta"]["preTokenBalances"][0]["accountIndex"] = 1
        value["meta"]["postTokenBalances"][0]["accountIndex"] = 1
        self.assertIsNotNone(parse_whale_buy(payload))

    async def test_no_key_means_no_socket_and_explicit_waiting_status(self):
        stream = HeliusWhaleStream("")
        await stream.run()
        status = stream.snapshot()
        self.assertEqual(status["state"], "waiting_for_key")
        self.assertFalse(status["enabled"])
        self.assertIn("HELIUS_API_KEY", status["message"])
        self.assertEqual(status["recent_signals"], [])

    async def test_signal_deduplication_and_callback_queue(self):
        seen = []
        stream = HeliusWhaleStream("unit-test-key", on_signal=seen.append)
        payload = notification()
        self.assertTrue(await stream.handle_notification(payload))
        self.assertFalse(await stream.handle_notification(payload))
        self.assertEqual(len(seen), 1)
        self.assertEqual(stream.snapshot()["signals_seen"], 1)
        self.assertEqual(stream.snapshot()["recent_signals"][0]["sol"], 2.0)


if __name__ == "__main__":
    unittest.main()
