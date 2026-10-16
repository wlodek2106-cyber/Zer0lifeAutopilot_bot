"""Conservative entry screening, not a prediction of trade profitability."""
from decimal import Decimal, InvalidOperation, ROUND_CEILING
import time

from wallet_runtime import WalletError

MAX_COST_BPS = 250
MAX_PRICE_IMPACT_PCT = Decimal("0.5")
MAX_QUOTE_AGE_SECONDS = 30
EXIT_NETWORK_LAMPORTS = 80_000  # one signature + capped priority fee + Jito tip


class EntryCostError(WalletError):
    pass


def quote_amounts(quote, input_mint, output_mint, amount):
    try:
        if not isinstance(quote, dict):
            raise ValueError()
        impact = Decimal(str(quote["priceImpactPct"]))
        output, minimum = int(quote["outAmount"]), int(quote["otherAmountThreshold"])
        if (quote["inputMint"] != input_mint or quote["outputMint"] != output_mint
                or int(quote["inAmount"]) != amount or quote["swapMode"] != "ExactIn"
                or not 0 <= int(quote["slippageBps"]) <= 40
                or not 0 < minimum <= output or not quote.get("routePlan")
                or type(quote.get("contextSlot")) is not int or quote["contextSlot"] <= 0
                or not impact.is_finite() or abs(impact) > MAX_PRICE_IMPACT_PCT):
            raise ValueError()
        return output, minimum
    except (ValueError, TypeError, KeyError, InvalidOperation, OverflowError):
        raise EntryCostError("ENTRY SKIPPED: incomplete quote or price impact exceeds 0.50%.", 409) from None


def assess_entry(context, entry_expense_lamports, now=None):
    """Both quote thresholds already include DEX fees, impact and slippage."""
    try:
        now = time.time() if now is None else now
        age = now - context["quoted_at"]
        price = Decimal(str(context["sol_usdc"]))
        raw = context["input_raw"]
        if (not 0 <= age <= MAX_QUOTE_AGE_SECONDS or not price.is_finite() or price <= 0
                or type(raw) is not int or raw <= 0
                or type(entry_expense_lamports) is not int or entry_expense_lamports < 0
                or type(context["exit_minimum_raw"]) is not int or context["exit_minimum_raw"] <= 0
                or type(context["exit_rent_lamports"]) is not int or context["exit_rent_lamports"] <= 0):
            raise ValueError()
        network_lamports = entry_expense_lamports + EXIT_NETWORK_LAMPORTS + context["exit_rent_lamports"]
        network_usdc_raw = int((Decimal(network_lamports) * price / 1000).to_integral_value(rounding=ROUND_CEILING))
        route_loss = max(0, raw - context["exit_minimum_raw"])
        cost = route_loss + network_usdc_raw
        allowed = cost * 10000 <= raw * MAX_COST_BPS
        return {"status": "ALLOW" if allowed else "SKIP", "checked_at": int(now),
                "order_usdc": raw / 1e6, "estimated_cost_usdc": cost / 1e6,
                "estimated_cost_pct": cost * 100 / raw, "max_cost_pct": MAX_COST_BPS / 100,
                "route_loss_usdc": route_loss / 1e6, "network_and_rent_usdc": network_usdc_raw / 1e6,
                "reason": ("Estimated entry and exit expenses are within the 2.50% budget."
                           if allowed else "ENTRY SKIPPED: estimated entry and exit expenses exceed 2.50% of the order."),
                "estimate_only": True}
    except (ValueError, KeyError, TypeError, InvalidOperation, OverflowError):
        raise EntryCostError("ENTRY SKIPPED: cost evidence is missing, invalid or older than 30 seconds.", 409) from None
