"""Fail-closed shared capital and daily-loss guard for dual-chain live mode.

This module is policy-only: it does not sign, submit, or broadcast transactions.
Live mode remains disabled until a separate, reviewed dispatcher is integrated.
"""
from decimal import Decimal, InvalidOperation

TOTAL_CAP_USDC = Decimal("100000")
CHAIN_CAPS_USDC = {
    "solana": Decimal("50000"),
    "base": Decimal("50000"),
}
LIVE_MODE_DEFAULT_ENABLED = False


class LiveRiskGuardError(ValueError):
    """A proposed live order violates a configured safety boundary."""


def _amount(value, field):
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise LiveRiskGuardError(field + " must be a valid USDC amount.") from exc
    if not amount.is_finite() or amount < 0:
        raise LiveRiskGuardError(field + " must be finite and non-negative.")
    return amount


def check_live_order(*, chain, order_usdc, chain_exposure_usdc,
                     other_chain_exposure_usdc, pending_usdc=0,
                     daily_realized_loss_usdc, daily_loss_limit_usdc,
                     live_enabled=False):
    """Return a safe approval result or raise; unknown/missing risk data fails closed.

    Exposures include open positions and any already-reserved orders. pending_usdc
    is additional same-chain reserved capital not yet reflected in exposure.
    daily_realized_loss_usdc is expressed as a positive loss magnitude.
    """
    if live_enabled is not True:
        raise LiveRiskGuardError("Live mode is disabled.")
    if chain not in CHAIN_CAPS_USDC:
        raise LiveRiskGuardError("Unsupported live trading chain.")
    order = _amount(order_usdc, "order_usdc")
    same = _amount(chain_exposure_usdc, "chain_exposure_usdc")
    other = _amount(other_chain_exposure_usdc, "other_chain_exposure_usdc")
    pending = _amount(pending_usdc, "pending_usdc")
    loss = _amount(daily_realized_loss_usdc, "daily_realized_loss_usdc")
    if daily_loss_limit_usdc is None:
        raise LiveRiskGuardError("Daily loss limit is not configured.")
    loss_limit = _amount(daily_loss_limit_usdc, "daily_loss_limit_usdc")
    if order <= 0:
        raise LiveRiskGuardError("Order size must be greater than zero.")
    if loss_limit <= 0:
        raise LiveRiskGuardError("Daily loss limit must be greater than zero.")
    if loss >= loss_limit:
        raise LiveRiskGuardError("Daily realized-loss limit reached.")
    if same + pending + order > CHAIN_CAPS_USDC[chain]:
        raise LiveRiskGuardError("Per-chain USDC capital cap would be exceeded.")
    if same + other + pending + order > TOTAL_CAP_USDC:
        raise LiveRiskGuardError("Combined Solana and Base USDC capital cap would be exceeded.")
    return {
        "allowed": True,
        "chain": chain,
        "order_usdc": str(order),
        "chain_cap_usdc": str(CHAIN_CAPS_USDC[chain]),
        "combined_cap_usdc": str(TOTAL_CAP_USDC),
        "remaining_chain_usdc": str(CHAIN_CAPS_USDC[chain] - same - pending - order),
        "remaining_combined_usdc": str(TOTAL_CAP_USDC - same - other - pending - order),
    }
