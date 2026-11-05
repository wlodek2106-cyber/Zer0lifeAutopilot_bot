"""Fail-closed shared capital and daily-loss guard for dual-chain live mode.

Policy only: this module never signs or submits transactions. A capital ceiling
must be supplied from an explicitly approved configuration; no user limit is
assumed in code. Live mode defaults off.
"""
from decimal import Decimal, InvalidOperation

LIVE_MODE_DEFAULT_ENABLED = False
SUPPORTED_CHAINS = frozenset({"solana", "base"})


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
                     total_cap_usdc=None, chain_caps_usdc=None,
                     live_enabled=False):
    """Approve only when all risk inputs and explicitly approved caps are present.

    Exposures include open positions and reserved orders. pending_usdc is
    additional same-chain reserved capital. Loss is a positive loss magnitude.
    """
    if live_enabled is not True:
        raise LiveRiskGuardError("Live mode is disabled.")
    if chain not in SUPPORTED_CHAINS:
        raise LiveRiskGuardError("Unsupported live trading chain.")
    if total_cap_usdc is None or not isinstance(chain_caps_usdc, dict):
        raise LiveRiskGuardError("Explicitly approved capital caps are not configured.")
    if set(chain_caps_usdc) != SUPPORTED_CHAINS:
        raise LiveRiskGuardError("Capital caps must be configured for both chains.")
    total_cap = _amount(total_cap_usdc, "total_cap_usdc")
    chain_caps = {name: _amount(value, name + "_cap_usdc")
                  for name, value in chain_caps_usdc.items()}
    if total_cap <= 0 or any(value <= 0 for value in chain_caps.values()):
        raise LiveRiskGuardError("Capital caps must be greater than zero.")
    if sum(chain_caps.values(), Decimal("0")) > total_cap:
        raise LiveRiskGuardError("Per-chain caps exceed the combined capital cap.")

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
    if same + pending + order > chain_caps[chain]:
        raise LiveRiskGuardError("Per-chain USDC capital cap would be exceeded.")
    if same + other + pending + order > total_cap:
        raise LiveRiskGuardError("Combined Solana and Base USDC capital cap would be exceeded.")
    return {
        "allowed": True,
        "chain": chain,
        "order_usdc": str(order),
        "chain_cap_usdc": str(chain_caps[chain]),
        "combined_cap_usdc": str(total_cap),
        "remaining_chain_usdc": str(chain_caps[chain] - same - pending - order),
        "remaining_combined_usdc": str(total_cap - same - other - pending - order),
    }
