"""Deterministic admission policy. No model, signer, RPC writes or policy overrides."""
import math
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from types import MappingProxyType

from custody_ledger import USDC, SOL
from entry_costs import MAX_COST_BPS, MAX_PRICE_IMPACT_PCT, MAX_QUOTE_AGE_SECONDS, assess_entry
from wallet_runtime import WalletError

VERSION = "independent-execution-v3-chart-confirmation"
MAX_POSITIONS = MappingProxyType({"main": 5, "sniper": 5})
DECISION_SECONDS = 120
QUOTE_SECONDS = MAX_QUOTE_AGE_SECONDS
MAX_SLOT_DISTANCE = 75
MAX_SLIPPAGE_BPS = 40
MAX_IMPACT_PCT = MAX_PRICE_IMPACT_PCT
WRAPPED_SOL = "So11111111111111111111111111111111111111112"


class ExecutionDenied(WalletError):
    pass


@dataclass(frozen=True)
class Verdict:
    allowed: bool
    code: str
    reason: str

    def require(self):
        if not self.allowed:
            raise ExecutionDenied(self.reason, 409)
        return self


def allow():
    return Verdict(True, "allow", "Independent checks passed; this is not a profit forecast.")


def deny(code, reason):
    return Verdict(False, code, reason)


def positive(value):
    return (type(value) in (int, float) and math.isfinite(value) and value > 0)


def bounded_pct(value):
    return type(value) in (int, float) and -5000 <= value <= 5000


def risk_exit_reason(price, entry):
    if not positive(price) or not positive(entry):
        return None
    change = price / entry - 1
    if change >= .035:
        return "TAKE_PROFIT"
    if change <= -.020:
        return "STOP_LOSS"
    return None


def risk_exit(price, entry):
    return risk_exit_reason(price, entry) is not None


def core_entry_signal(symbol, context):
    """Require fresh chart confirmation for Core entries; never gate exits."""
    if not isinstance(context, dict) or not isinstance(context.get(symbol), dict):
        return deny("core_context", "SKIPPED: Core 5-minute/1-hour market context is missing.")
    metrics = context[symbol]
    if metrics.get("chart_ready") != 1:
        return deny("core_chart_data",
                    "SKIPPED: fresh 5-minute and 1-hour OHLCV candles are unavailable; Core BUY is blocked.")
    if metrics.get("chart_buy_setup") != 1:
        return deny("core_chart_confirmation",
                    "SKIPPED: charts show no confirmed support hold or breakout-retest with momentum confirmation.")
    m5, h1 = metrics.get("change_m5_pct"), metrics.get("change_h1_pct")
    if not (bounded_pct(m5) and bounded_pct(h1)):
        return deny("core_context", "SKIPPED: Core needs fresh 5-minute and 1-hour price changes.")
    if m5 <= 0 or h1 <= 0:
        return deny("core_trend", "SKIPPED: Core has no aligned positive 5-minute and 1-hour trend.")
    if m5 > 3 or h1 > 10:
        return deny("core_extended", "SKIPPED: Core signal is overextended; the guide warns against chasing.")
    h6 = metrics.get("change_h6_pct")
    if bounded_pct(h6) and h6 < -5:
        return deny("core_drawdown", "SKIPPED: Core 6-hour decline is below the configured trend limit.")

    buyer_pressure = any(
        type(metrics.get("buys_" + window)) is int
        and type(metrics.get("sells_" + window)) is int
        and metrics["buys_" + window] > metrics["sells_" + window]
        for window in ("m5", "h1")
    )
    sol_metrics = context.get("SOL")
    relative_strength = (
        symbol != "SOL" and isinstance(sol_metrics, dict) and (
            (bounded_pct(sol_metrics.get("change_m5_pct"))
             and m5 > sol_metrics["change_m5_pct"])
            or (bounded_pct(sol_metrics.get("change_h1_pct"))
                and h1 > sol_metrics["change_h1_pct"])
        )
    )
    if not buyer_pressure and not relative_strength:
        return deny("core_confirmation",
                    "SKIPPED: Core lacks buyer-flow or relative-strength confirmation.")
    return allow()


def fresh(created_at, now, maximum):
    return (type(created_at) in (int, float) and math.isfinite(created_at)
            and 0 <= now - created_at <= maximum)


def integer(value):
    if type(value) is int:
        return value
    if isinstance(value, str) and value.isascii() and value.isdigit():
        return int(value)
    raise ValueError("not an integer amount")


def proposal(*, mode, decision, universe, created_at, original, prices, samples,
             held, balance, order_raw, main_context=None, now=None):
    """A proposal cannot supply amounts, accounts, flags, limits or exceptions."""
    now = time.time() if now is None else now
    if (mode not in MAX_POSITIONS or not isinstance(decision, dict)
            or set(decision) != {"action", "symbol", "reason"}
            or decision.get("action") not in ("BUY", "SELL", "HOLD")
            or not isinstance(decision.get("reason"), str)
            or not 1 <= len(decision["reason"]) <= 350):
        return deny("schema", "BLOCKED: invalid AI proposal; policy overrides are not accepted.")
    action, symbol = decision["action"], decision["symbol"]
    if symbol is not None and not isinstance(symbol, str):
        return deny("schema", "BLOCKED: invalid AI asset identifier.")
    if not fresh(created_at, now, DECISION_SECONDS):
        return deny("stale", "SKIPPED: AI decision is stale or has an invalid timestamp.")
    if action == "HOLD":
        return deny("hold", "WAIT: AI chose HOLD. No transaction requested.")
    if symbol not in universe:
        return deny("universe", "BLOCKED: the proposed asset is not currently allowlisted.")
    old, price = original.get(symbol), prices.get(symbol)
    if not positive(old) or not positive(price) or abs(price / old - 1) > .01:
        return deny("price", "SKIPPED: price data is missing/invalid or moved more than 1% since analysis.")
    if mode == "sniper":
        old_sol, sol = original.get("SOL"), prices.get("SOL")
        if not positive(old_sol) or not positive(sol):
            return deny("sol_price", "SKIPPED: valid SOL reference prices are unavailable.")
        if abs((price / sol) / (old / old_sol) - 1) > .01:
            return deny("relative_price", "SKIPPED: token/SOL price drift exceeds the 1% safety limit.")
    if action == "SELL":
        return allow() if any(p["symbol"] == symbol for p in held) else deny(
            "sell_skipped", "SKIPPED: no owned open position in this asset.")
    if (type(samples.get(symbol)) is not int or samples[symbol] < 5
            or (mode == "sniper" and (type(samples.get("SOL")) is not int or samples["SOL"] < 5))):
        return deny("warmup", "SKIPPED: fewer than five price samples; waiting for market-data warm-up.")
    if len(held) >= MAX_POSITIONS[mode]:
        return deny("position_limit", "SKIPPED: this mode's open-position limit has been reached.")
    if any(p["symbol"] == symbol for p in held):
        return deny("already_open", "SKIPPED: you already hold an open position in this asset.")
    if type(order_raw) is not int or order_raw <= 0 or balance < order_raw:
        return deny("balance", "SKIPPED: your available balance is below the saved purchase size.")
    if mode == "main":
        signal = core_entry_signal(symbol, main_context)
        if not signal.allowed:
            return signal
    return allow()


def order(*, uid, kind, asset, output_asset, raw, metadata, position_id,
          settings, positions, watchlist, universes, balance, reserved=False):
    """Runs before reserving and again immediately before preparing the signed wire."""
    if kind not in ("BUY", "SELL") or type(raw) is not int or raw <= 0:
        return deny("order", "BLOCKED: invalid order type or amount.")
    if kind == "SELL":
        position = next((p for p in positions if p["id"] == position_id), None)
        if (not position or position["uid"] != uid or position["asset"] != asset
                or position["funding_asset"] != output_asset or position["raw"] != raw
                or position["state"] != ("RESERVED" if reserved else "OPEN")):
            return deny("ownership", "BLOCKED: sell does not match this account's owned position.")
        # Entry-efficiency/position-count filters must never prevent an exit.
        return allow()
    if asset not in (USDC, SOL) or not settings:
        return deny("funding", "BLOCKED: invalid funding account.")
    mode = "main" if asset == USDC else "sniper"
    flag, key = ("trading", "order_raw") if mode == "main" else ("sniper_trading", "sniper_order_raw")
    if not settings.get(flag):
        return deny("paused", "BLOCKED: real trading is OFF for this mode.")
    symbol = metadata.get("symbol")
    watch = watchlist.get(symbol)
    if not watch or symbol not in universes[mode]:
        return deny("universe", "BLOCKED: order asset is not currently allowlisted.")
    expected = SOL if watch["mint"] == WRAPPED_SOL else watch["mint"]
    if output_asset != expected or metadata.get("decimals") != watch["decimals"]:
        return deny("asset", "BLOCKED: order does not match the server's asset catalog.")
    if raw != settings[key] or balance < raw:
        return deny("size", "BLOCKED: order exceeds the saved amount or available funds.")
    held = [p for p in positions if p["funding_asset"] == asset]
    if len(held) >= MAX_POSITIONS[mode]:
        return deny("position_limit", "BLOCKED: open-position limit reached.")
    if any(p["symbol"] == symbol for p in held):
        return deny("already_open", "BLOCKED: an owned position in this asset is already open.")
    return allow()


def quote(*, kind, value, input_mint, output_mint, raw, received_at, head_slot, now=None):
    now = time.time() if now is None else now
    if not fresh(received_at, now, QUOTE_SECONDS):
        return deny("quote_age", "BLOCKED: quote is stale or has an invalid timestamp.")
    try:
        if (kind not in ("BUY", "SELL") or not isinstance(value, dict)
                or value["inputMint"] != input_mint or value["outputMint"] != output_mint
                or type(raw) is not int or raw <= 0
                or integer(value["inAmount"]) != raw or value["swapMode"] != "ExactIn"
                or not 0 <= integer(value["slippageBps"]) <= MAX_SLIPPAGE_BPS
                or not 0 < integer(value["otherAmountThreshold"]) <= integer(value["outAmount"])
                or not value.get("routePlan")
                or type(value["contextSlot"]) is not int or value["contextSlot"] <= 0
                or type(head_slot) is not int
                or abs(value["contextSlot"] - head_slot) > MAX_SLOT_DISTANCE):
            raise ValueError()
        if kind == "BUY":
            impact = Decimal(str(value["priceImpactPct"]))
            if not impact.is_finite():
                raise ValueError()
            if abs(impact) > MAX_IMPACT_PCT:
                return deny("liquidity", "ENTRY SKIPPED: quoted route impact exceeds 0.50% at this order size.")
    except (KeyError, TypeError, ValueError, InvalidOperation, OverflowError):
        return deny("quote", "BLOCKED: incomplete, stale-slot or mismatched executable quote.")
    return allow()


def costs(context, expense_lamports, now=None):
    # Includes quoted DEX fees/impact/slippage and simulated network/rent expenses.
    return assess_entry(context, expense_lamports, now=now)


def describe():
    return {"version": VERSION, "ai_role": "proposal_only", "override_allowed": False,
            "decision_max_age_seconds": DECISION_SECONDS, "quote_max_age_seconds": QUOTE_SECONDS,
            "quote_max_slot_distance": MAX_SLOT_DISTANCE, "slippage_bps": MAX_SLIPPAGE_BPS,
            "buy_max_price_impact_pct": float(MAX_IMPACT_PCT), "core_max_cost_pct": MAX_COST_BPS / 100,
            "max_positions": dict(MAX_POSITIONS), "liquidity_basis": "executable_route_price_impact",
            "entry_filters_never_block_exits": True}
