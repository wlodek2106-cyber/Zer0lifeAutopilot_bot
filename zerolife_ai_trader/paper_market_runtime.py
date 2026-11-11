"""Async public-market-data entry point for paper-only signal execution."""
from .binance_public import fetch_public_price
from .paper_bridge import execute_paper_signal


async def execute_paper_signal_with_public_price(
    signal, *, dispatch_store, paper_store, token_allowlist,
    fee_bps=10, slippage_bps=5, starting_cash=1000.0,
    session=None, now=None,
):
    """Fetch a live public Spot quote, then record a simulated paper fill only."""
    if not isinstance(signal, dict):
        raise ValueError("Signal must be an object.")
    symbol = signal.get("symbol")
    quote = await fetch_public_price(symbol, session=session)
    result = execute_paper_signal(
        signal, dispatch_store=dispatch_store, paper_store=paper_store,
        token_allowlist=token_allowlist, market_price=quote["price"],
        fee_bps=fee_bps, slippage_bps=slippage_bps,
        starting_cash=starting_cash, now=now,
    )
    return {"market_quote": quote, "dispatch": result}
