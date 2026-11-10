"""Safe configuration for the application's ordinary Solana JSON-RPC endpoint."""
from urllib.parse import quote, urlsplit


DEFAULT_SOLANA_RPC_URL = "https://api.mainnet-beta.solana.com"
HELIUS_SOLANA_RPC_URL = "https://mainnet.helius-rpc.com/"


def _is_helius_url(url):
    try:
        return (urlsplit(str(url)).hostname or "").lower() == "mainnet.helius-rpc.com"
    except ValueError:
        return False


def resolve_solana_rpc(environ):
    """Return (endpoint, safe provider label); never return the key in diagnostics."""
    env = environ or {}
    requested = str(env.get("SOLANA_RPC_PROVIDER", "")).strip().lower()
    configured_url = str(env.get("SOLANA_RPC_URL", "")).strip()
    api_key = str(env.get("HELIUS_API_KEY", "")).strip()

    if requested == "helius":
        if api_key:
            return (
                HELIUS_SOLANA_RPC_URL + "?api-key=" + quote(api_key, safe=""),
                "helius",
            )
        if _is_helius_url(configured_url):
            return configured_url, "helius"
        return configured_url or DEFAULT_SOLANA_RPC_URL, "helius-key-missing"

    if requested not in ("", "custom", "public"):
        return configured_url or DEFAULT_SOLANA_RPC_URL, "invalid-provider"

    if configured_url:
        if _is_helius_url(configured_url):
            return configured_url, "helius"
        return configured_url, "custom"
    return DEFAULT_SOLANA_RPC_URL, "public"
