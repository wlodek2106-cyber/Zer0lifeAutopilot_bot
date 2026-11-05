"""Paper-only bridge from validated AI signals into the persistent paper ledger.

No live execution adapters, wallet access, or network calls are present.
"""
import hashlib
import math

from .signal_dispatcher import dispatch_signal


def _paper_signal_id(signal_key):
    value = int.from_bytes(hashlib.sha256(signal_key.encode("utf-8")).digest()[:8], "big")
    return (value & ((1 << 63) - 1)) or 1


def execute_paper_signal(signal, *, dispatch_store, paper_store, token_allowlist,
                         market_price, fee_bps=10, slippage_bps=5,
                         starting_cash=1000.0, now=None):
    """Validate/dedupe a signal and execute it only in PaperTradeStore.

    BUY amount_usdc is converted to base quantity at market_price. SELL must
    identify the position as paper:<SYMBOL>; the ledger independently verifies
    that the user owns enough quantity of that symbol.
    """
    if type(market_price) not in (int, float) or not math.isfinite(market_price) or market_price <= 0:
        raise ValueError("Paper market price must be finite and positive.")
    for value, name in ((fee_bps, "fee_bps"), (slippage_bps, "slippage_bps")):
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0 or value > 1000:
            raise ValueError(name + " is outside safe bounds.")
    if type(starting_cash) not in (int, float) or not math.isfinite(starting_cash) or starting_cash <= 0:
        raise ValueError("Starting paper cash must be finite and positive.")

    def paper_executor(plan):
        signal_id = _paper_signal_id(plan["signal_key"])
        ledger_symbol = plan["chain"].upper() + ":" + plan["symbol"]
        if plan["side"] == "BUY":
            quantity = plan["amount_usdc"] / float(market_price)
            return paper_store.execute_buy(
                telegram_id=plan["telegram_id"], signal_id=signal_id,
                symbol=ledger_symbol, quantity=quantity,
                market_price=float(market_price), fee_bps=fee_bps,
                slippage_bps=slippage_bps, starting_cash=starting_cash,
            )
        expected_position = "paper:" + ledger_symbol
        if plan["position_id"] != expected_position:
            raise ValueError("SELL must reference the matching user's paper position.")
        return paper_store.execute_sell(
            telegram_id=plan["telegram_id"], signal_id=signal_id,
            symbol=ledger_symbol, quantity=plan["quantity"],
            market_price=float(market_price), fee_bps=fee_bps,
            slippage_bps=slippage_bps,
        )

    return dispatch_signal(signal, store=dispatch_store,
        token_allowlist=token_allowlist, now=now,
        live_enabled=False, paper_executor=paper_executor)
