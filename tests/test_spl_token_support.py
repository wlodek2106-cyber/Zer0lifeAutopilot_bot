import base64
import unittest

from solders.pubkey import Pubkey

from spl_token_support import (
    TOKEN_PROGRAM_ID,
    TOKEN_2022_PROGRAM_ID,
    token_account_data,
    validate_mint_account,
)
from wallet_runtime import associated
from wallet_runtime import WalletRuntime, SOL, USDC


OTC_MINT = "MukLDtJ8Cx9DxLbeyLRSWPSposTMWuwHANbuaudpump"


def otc_mint_account(**changes):
    info = {
        "isInitialized": True,
        "decimals": 6,
        "mintAuthority": None,
        "freezeAuthority": None,
        "extensions": [
            {"extension": "metadataPointer", "state": {
                "authority": None, "metadataAddress": OTC_MINT}},
            {"extension": "tokenMetadata", "state": {
                "updateAuthority": None, "mint": OTC_MINT,
                "name": "OTC", "symbol": "OTC", "additionalMetadata": []}},
        ],
    }
    info.update(changes)
    return {"owner": TOKEN_2022_PROGRAM_ID,
            "data": {"parsed": {"type": "mint", "info": info}}}


def token_account(program, mint, *, extension=None):
    raw = bytearray(165)
    raw[:32] = bytes(Pubkey.from_string(mint))
    raw[32:64] = bytes(Pubkey.from_string("11111111111111111111111111111111"))
    raw[108] = 1
    if extension:
        raw.extend(b"\x02")  # Token-2022 Account type.
        raw.extend(int(extension).to_bytes(2, "little"))
        raw.extend((0).to_bytes(2, "little"))  # ImmutableOwner has no payload.
    return {"owner": program, "data": [base64.b64encode(raw).decode(), "base64"]}


class MintValidationTests(unittest.TestCase):
    def test_exact_otc_token_2022_metadata_is_accepted_only_when_enabled(self):
        account = otc_mint_account()
        with self.assertRaisesRegex(ValueError, "not enabled"):
            validate_mint_account(account, OTC_MINT, expected_decimals=6,
                                  expected_symbol="OTC")
        self.assertEqual(
            validate_mint_account(account, OTC_MINT, expected_decimals=6,
                                  expected_symbol="OTC",
                                  expected_program=TOKEN_2022_PROGRAM_ID,
                                  allow_token_2022=True),
            TOKEN_2022_PROGRAM_ID,
        )

    def test_token_2022_rejects_mutability_mint_changes_and_unknown_extensions(self):
        variants = (
            {"mintAuthority": "authority"},
            {"freezeAuthority": "authority"},
            {"extensions": [
                {"extension": "metadataPointer", "state": {
                    "authority": "authority", "metadataAddress": OTC_MINT}},
                {"extension": "tokenMetadata", "state": {
                    "updateAuthority": None, "mint": OTC_MINT,
                    "name": "OTC", "symbol": "OTC", "additionalMetadata": []}},
            ]},
            {"extensions": [
                {"extension": "metadataPointer", "state": {
                    "authority": None, "metadataAddress": OTC_MINT}},
                {"extension": "tokenMetadata", "state": {
                    "updateAuthority": None, "mint": OTC_MINT,
                    "name": "OTC", "symbol": "OTC", "additionalMetadata": []}},
                {"extension": "transferHook", "state": {}},
            ]},
            {"extensions": [
                {"extension": "metadataPointer", "state": {
                    "authority": None, "metadataAddress": OTC_MINT}},
                {"extension": "tokenMetadata", "state": {
                    "updateAuthority": "authority", "mint": OTC_MINT,
                    "name": "OTC", "symbol": "OTC", "additionalMetadata": []}},
            ]},
        )
        for variant in variants:
            with self.subTest(variant=variant), self.assertRaises(ValueError):
                validate_mint_account(
                    otc_mint_account(**variant), OTC_MINT,
                    expected_decimals=6, expected_symbol="OTC",
                    expected_program=TOKEN_2022_PROGRAM_ID,
                    allow_token_2022=True)

    def test_token_2022_rejects_symbol_decimals_and_program_mismatch(self):
        checks = (
            (otc_mint_account(), {"expected_symbol": "NOT-OTC"}),
            (otc_mint_account(decimals=9), {"expected_decimals": 6}),
        )
        for account, expected in checks:
            with self.subTest(expected=expected), self.assertRaises(ValueError):
                validate_mint_account(
                    account, OTC_MINT, expected_program=TOKEN_2022_PROGRAM_ID,
                    allow_token_2022=True, **expected)
        with self.assertRaisesRegex(ValueError, "differs from the saved"):
            validate_mint_account(
                {**otc_mint_account(), "owner": TOKEN_PROGRAM_ID}, OTC_MINT,
                expected_program=TOKEN_2022_PROGRAM_ID, allow_token_2022=True)

    def test_legacy_spl_mint_keeps_existing_validation_path(self):
        account = {"owner": TOKEN_PROGRAM_ID, "data": {"parsed": {
            "type": "mint", "info": {
                "isInitialized": True, "decimals": 6,
                "mintAuthority": None, "freezeAuthority": None}}}}
        self.assertEqual(validate_mint_account(account, OTC_MINT), TOKEN_PROGRAM_ID)


class TokenAccountTests(unittest.IsolatedAsyncioTestCase):
    def test_legacy_and_immutable_owner_token_2022_accounts_are_supported(self):
        legacy = token_account(TOKEN_PROGRAM_ID, OTC_MINT)
        token_2022 = token_account(TOKEN_2022_PROGRAM_ID, OTC_MINT, extension=7)
        self.assertEqual(len(token_account_data(legacy)), 165)
        self.assertEqual(len(token_account_data(token_2022)), 170)

    def test_token_2022_account_program_extensions_and_associated_address_are_checked(self):
        with self.assertRaisesRegex(ValueError, "unsupported extensions"):
            token_account_data(token_account(TOKEN_2022_PROGRAM_ID, OTC_MINT, extension=12))
        with self.assertRaisesRegex(ValueError, "unexpected program"):
            token_account_data(token_account(TOKEN_2022_PROGRAM_ID, OTC_MINT),
                               expected_program=TOKEN_PROGRAM_ID)
        wallet = Pubkey.from_string("11111111111111111111111111111111")
        mint = Pubkey.from_string(OTC_MINT)
        self.assertNotEqual(
            associated(wallet, mint, Pubkey.from_string(TOKEN_PROGRAM_ID)),
            associated(wallet, mint, Pubkey.from_string(TOKEN_2022_PROGRAM_ID)),
        )

    async def test_wallet_balance_scan_counts_classic_and_token_2022_accounts(self):
        wallet = Pubkey.from_string("11111111111111111111111111111111")
        runtime = object.__new__(WalletRuntime)
        runtime.wallet = str(wallet)
        calls = []

        async def rpc(session, method, params):
            if method == "getBalance":
                return {"value": 2_000_000_000}
            if method == "getTokenAccountsByOwner":
                program = params[1]["programId"]
                calls.append(program)
                mint = OTC_MINT if program == TOKEN_2022_PROGRAM_ID else USDC
                decimals = 6 if mint == USDC else 6
                return {"value": [{
                    "account": {"owner": program, "data": {"parsed": {"info": {
                        "owner": str(wallet), "mint": mint, "state": "initialized",
                        "tokenAmount": {"amount": "2500000", "decimals": decimals},
                    }}}}
                }]}
            raise AssertionError(method)

        runtime.rpc = rpc
        balances = await runtime.balances(None)
        self.assertEqual(set(calls), {TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID})
        self.assertEqual(balances[USDC]["raw"], 2_500_000)
        self.assertEqual(balances[OTC_MINT]["raw"], 2_500_000)
        self.assertEqual(balances[SOL]["raw"], 2_000_000_000)


if __name__ == "__main__":
    unittest.main()
