import unittest

from solana_rpc_config import (
    DEFAULT_SOLANA_RPC_URL,
    resolve_solana_rpc,
)


class SolanaRpcConfigTests(unittest.TestCase):
    def test_helius_provider_uses_render_key_without_exposing_it_in_label(self):
        url, provider = resolve_solana_rpc({
            "SOLANA_RPC_PROVIDER": "helius",
            "SOLANA_RPC_URL": "https://api.mainnet-beta.solana.com",
            "HELIUS_API_KEY": "test-key/with+reserved",
        })
        self.assertEqual(
            url,
            "https://mainnet.helius-rpc.com/?api-key=test-key%2Fwith%2Breserved",
        )
        self.assertEqual(provider, "helius")

    def test_explicit_helius_url_remains_usable_without_separate_key(self):
        url, provider = resolve_solana_rpc({
            "SOLANA_RPC_PROVIDER": "helius",
            "SOLANA_RPC_URL": "https://mainnet.helius-rpc.com/?api-key=redacted-test",
        })
        self.assertEqual(url, "https://mainnet.helius-rpc.com/?api-key=redacted-test")
        self.assertEqual(provider, "helius")

    def test_missing_helius_key_is_reported_and_uses_existing_endpoint(self):
        url, provider = resolve_solana_rpc({
            "SOLANA_RPC_PROVIDER": "helius",
            "SOLANA_RPC_URL": "https://api.mainnet-beta.solana.com",
        })
        self.assertEqual(url, "https://api.mainnet-beta.solana.com")
        self.assertEqual(provider, "helius-key-missing")

    def test_unconfigured_helius_preserves_legacy_public_default(self):
        url, provider = resolve_solana_rpc({})
        self.assertEqual(url, DEFAULT_SOLANA_RPC_URL)
        self.assertEqual(provider, "public")

    def test_explicit_custom_endpoint_is_preserved_without_provider_override(self):
        url, provider = resolve_solana_rpc({
            "SOLANA_RPC_URL": "https://rpc.example.invalid"
        })
        self.assertEqual(url, "https://rpc.example.invalid")
        self.assertEqual(provider, "custom")

    def test_invalid_provider_is_reported_without_logging_endpoint_contents(self):
        url, provider = resolve_solana_rpc({
            "SOLANA_RPC_PROVIDER": "unexpected",
            "SOLANA_RPC_URL": "https://rpc.example.invalid",
        })
        self.assertEqual(url, "https://rpc.example.invalid")
        self.assertEqual(provider, "invalid-provider")


if __name__ == "__main__":
    unittest.main()
