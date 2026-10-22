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
    HeliusStreamError,
    WHALE_SIGNAL_PROGRAMS,
    parse_log_notification,
    parse_whale_buy,
)


BUYER = str(Pubkey.from_bytes(hashlib.sha256(b"whale-buyer").digest()))
MINT = str(Pubkey.from_bytes(hashlib.sha256(b"whale-token").digest()))
OTHER_MINT = str(Pubkey.from_bytes(hashlib.sha256(b"other-whale-token").digest()))
TOKEN_ACCOUNT = str(Pubkey.from_bytes(hashlib.sha256(b"token-account").digest()))
SIGNATURE = str(Signature.default())
SIGNATURE_2 = str(Signature.from_bytes(hashlib.sha512(b"whale-tx-2").digest()))
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


def logs_notification(signature=SIGNATURE, *, logs=None, err=None):
    return {
        "jsonrpc": "2.0",
        "method": "logsNotification",
        "params": {"result": {
            "context": {"slot": 123},
            "value": {
                "signature": signature,
                "err": err,
                "logs": logs if logs is not None else ["Program log: Instruction: Buy"],
            },
        }},
    }


class FakeHttpResponse:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def json(self, content_type=None):
        return self.payload


class FakeHttpSession:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status = status
        self.url = None
        self.request = None

    def post(self, url, *, json, timeout):
        self.url = url
        self.request = json
        return FakeHttpResponse(self.payload, self.status)


class WhaleSignalTests(unittest.IsolatedAsyncioTestCase):
    async def test_stream_can_be_disabled_even_when_api_key_is_present(self):
        stream = HeliusWhaleStream("unit-test-key", enabled=False)

        await stream.run()

        status = stream.snapshot()
        self.assertFalse(status["enabled"])
        self.assertEqual(status["state"], "stopped")
        self.assertIn("Scheduled candidate scanning remains available", status["message"])

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

    async def test_stream_subscribes_to_all_curated_dex_programs(self):
        stream = HeliusWhaleStream("unit-test-key")
        requests = stream.subscriptions()
        self.assertEqual(len(requests), len(WHALE_SIGNAL_PROGRAMS))
        self.assertTrue(all(request["method"] == "logsSubscribe" for request in requests))
        self.assertEqual(
            [request["params"][0]["mentions"][0] for request in requests],
            list(WHALE_SIGNAL_PROGRAMS.values()),
        )
        self.assertTrue(all(request["params"][1]["commitment"] == "confirmed" for request in requests))
        self.assertEqual(list(WHALE_SIGNAL_PROGRAMS.items()), list(DEX_PROGRAMS.items()))
        self.assertEqual(stream.snapshot()["supported_programs"], list(DEX_PROGRAMS))
        for address in DEX_PROGRAMS.values():
            self.assertEqual(str(Pubkey.from_string(address)), address)

    def test_log_parser_accepts_confirmed_buy_and_swap_signatures(self):
        self.assertEqual(parse_log_notification(logs_notification()), {"signature": SIGNATURE, "slot": 123})
        self.assertEqual(
            parse_log_notification(logs_notification(logs=["Program log: Instruction: SwapBaseIn"])),
            {"signature": SIGNATURE, "slot": 123},
        )
        self.assertEqual(
            parse_log_notification(logs_notification(logs=["Program log: Event: Swap"])),
            {"signature": SIGNATURE, "slot": 123},
        )
        self.assertIsNone(parse_log_notification(logs_notification(logs=["Program log: Initialize"])))
        self.assertIsNone(parse_log_notification(logs_notification(err={"InstructionError": [0, "failed"]})))
        self.assertIsNone(parse_log_notification(logs_notification(signature="bad")))

    def test_transaction_parser_recognizes_buys_on_each_curated_dex(self):
        for name, program in DEX_PROGRAMS.items():
            with self.subTest(dex=name):
                event = parse_whale_buy(notification(program=program))
                self.assertEqual(event["program"], name)

    async def test_logs_are_deduplicated_and_queue_overflow_keeps_newer_events(self):
        stream = HeliusWhaleStream("unit-test-key")
        stream._transaction_queue = asyncio.Queue(maxsize=1)
        self.assertTrue(await stream.handle_logs_notification(logs_notification()))
        self.assertFalse(await stream.handle_logs_notification(logs_notification()))
        self.assertTrue(await stream.handle_logs_notification(logs_notification(SIGNATURE_2)))
        self.assertEqual(stream.snapshot()["queued_lookups"], 1)
        self.assertEqual(stream.snapshot()["dropped_lookups"], 1)
        self.assertEqual(stream._transaction_queue.get_nowait()[0], SIGNATURE_2)

    async def test_transaction_lookup_uses_confirmed_standard_rpc_and_parses_the_result(self):
        signal = []
        stream = HeliusWhaleStream("unit-test-key", on_signal=signal.append)
        tx = notification()["params"]["result"]
        session = FakeHttpSession({"jsonrpc": "2.0", "result": tx})
        await stream._fetch_transaction(session, SIGNATURE)
        self.assertEqual(session.request["method"], "getTransaction")
        self.assertEqual(session.request["params"][1]["commitment"], "confirmed")
        self.assertEqual(session.request["params"][1]["encoding"], "jsonParsed")
        self.assertTrue(session.url.endswith("?api-key=unit-test-key"))
        self.assertEqual(signal[0]["mint"], MINT)
        self.assertEqual(stream.snapshot()["transaction_lookups"], 1)

    async def test_transaction_lookup_auth_failure_has_sanitized_diagnostic(self):
        stream = HeliusWhaleStream("unit-test-key")
        with self.assertRaises(HeliusStreamError) as raised:
            await stream._fetch_transaction(FakeHttpSession({}, status=403), SIGNATURE)
        self.assertEqual(raised.exception.code, "AuthRejected")
        stream.state = "connected"
        stream.last_error = raised.exception.code
        self.assertIn("check the Render secret", stream.snapshot()["message"])

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
