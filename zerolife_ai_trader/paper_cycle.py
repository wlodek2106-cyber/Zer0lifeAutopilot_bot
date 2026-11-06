"""End-to-end public-market analysis and optional paper-only execution cycle."""
from datetime import datetime, timezone

from .binance_public import fetch_public_klines, fetch_public_price
from .paper_bridge import execute_paper_signal
from .settings import validate_settings
from .signal_engine import evaluate_watchlist


async def run_paper_cycle(
    *, telegram_id, chain, settings, dispatch_store, paper_store,
    token_allowlist, session=None, now=None, candle_limit=120,
):
    """Fetch candles, evaluate the configured strategy, and optionally paper-fill.

    Auto-execution is strictly controlled by the per-user paperAutoExecute flag.
    BUY and SELL both pass through the validated, idempotent paper bridge.
    No live execution path is present.
    """
    if type(telegram_id) is not int or telegram_id <= 0:
        raise ValueError("Invalid Telegram account owner.")
    if chain not in {"solana", "base"}:
        raise ValueError("Unsupported execution chain.")
    config = validate_settings(settings)
    if config["source"] != "binance_public":
        raise ValueError("This cycle currently supports Binance public data only.")
    if type(candle_limit) is not int or not 30 <= candle_limit <= 1000:
        raise ValueError("Candle limit must be between 30 and 1000.")
    observed_at = datetime.now(timezone.utc).timestamp() if now is None else float(now)

    symbols = config["symbols"].split(",")
    price_series = {}
    candle_ids = {}
    for symbol in symbols:
        history = await fetch_public_klines(
            symbol, config["timeframe"], limit=candle_limit, session=session
        )
        candles = history["candles"]
        price_series[symbol] = [item["close"] for item in candles]
        candle_ids[symbol] = str(candles[-1]["timestamp_ms"])

    account = paper_store.account(telegram_id=telegram_id)
    cash = account["cash"] if account["cash"] is not None else config["capital"]
    open_positions = len(account["positions"])
    analysis = evaluate_watchlist(
        price_series, config, equity=config["capital"], cash=cash,
        open_positions=open_positions, daily_pnl=0.0,
    )
    executions = []
    if config["paperAutoExecute"]:
        for item in analysis:
            action = item["action"]
            if action not in {"PAPER_BUY", "SELL"}:
                continue
            symbol = item["symbol"]
            if symbol not in token_allowlist.get(chain, {}):
                executions.append({"symbol": symbol, "status": "SKIPPED",
                                   "reason": "No server token mapping for selected chain."})
                continue
            quote = await fetch_public_price(symbol, session=session)
            signal = {
                "telegram_id": telegram_id, "chain": chain, "symbol": symbol,
                "side": "BUY" if action == "PAPER_BUY" else "SELL",
                "timeframe": config["timeframe"], "candle_id": candle_ids[symbol],
                "observed_at": observed_at, "confidence": item["confidence"],
            }
            if action == "PAPER_BUY":
                signal["amount_usdc"] = item["risk"]["quantity"] * quote["price"]
            else:
                ledger_symbol = chain.upper() + ":" + symbol
                position = next((p for p in account["positions"]
                                 if p["symbol"] == ledger_symbol), None)
                if position is None:
                    executions.append({"symbol": symbol, "status": "SKIPPED",
                                       "reason": "No matching paper position to sell."})
                    continue
                signal["position_id"] = "paper:" + ledger_symbol
                signal["quantity"] = position["quantity"]
            result = execute_paper_signal(
                signal, dispatch_store=dispatch_store, paper_store=paper_store,
                token_allowlist=token_allowlist, market_price=quote["price"],
                fee_bps=config["fee"], slippage_bps=config["slippage"],
                starting_cash=config["capital"], now=observed_at,
            )
            executions.append({"symbol": symbol, "market_quote": quote,
                               "dispatch": result})
    return {
        "telegram_id": telegram_id, "chain": chain,
        "source": "binance_public", "timeframe": config["timeframe"],
        "paper_auto_execute": config["paperAutoExecute"],
        "analysis": analysis, "executions": executions,
    }
