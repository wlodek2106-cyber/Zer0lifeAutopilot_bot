"""Independent risk gate for paper orders. Deny by default on invalid inputs."""
from dataclasses import dataclass
from math import isfinite


@dataclass(frozen=True)
class RiskLimits:
    max_order_fraction: float = 0.02
    max_open_positions: int = 5
    max_daily_loss_fraction: float = 0.03
    min_confidence: float = 0.60


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    quantity: float
    reason: str


def assess_order(*, equity, cash, price, confidence, open_positions=0,
                 daily_pnl=0.0, limits=None):
    limits = RiskLimits() if limits is None else limits
    if not isinstance(limits, RiskLimits):
        return RiskDecision(False, 0.0, "Invalid risk-limit configuration.")
    limit_numbers = (limits.max_order_fraction, limits.max_daily_loss_fraction,
                     limits.min_confidence)
    if (any(type(x) not in (int, float) or not isfinite(x) for x in limit_numbers)
            or not 0 < limits.max_order_fraction <= 1
            or not 0 < limits.max_daily_loss_fraction <= 1
            or not 0 <= limits.min_confidence <= 1
            or type(limits.max_open_positions) is not int
            or limits.max_open_positions < 1):
        return RiskDecision(False, 0.0, "Invalid risk-limit configuration.")
    numbers = (equity, cash, price, confidence, daily_pnl)
    if any(type(x) not in (int, float) or not isfinite(x) for x in numbers):
        return RiskDecision(False, 0.0, "Invalid or non-finite risk input.")
    if equity <= 0 or cash < 0 or price <= 0:
        return RiskDecision(False, 0.0, "Equity, cash, or price is outside safe bounds.")
    if type(open_positions) is not int or open_positions < 0:
        return RiskDecision(False, 0.0, "Invalid open-position count.")
    if open_positions >= limits.max_open_positions:
        return RiskDecision(False, 0.0, "Maximum open positions reached.")
    if confidence < limits.min_confidence:
        return RiskDecision(False, 0.0, "Signal confidence is below the configured threshold.")
    if daily_pnl <= -equity * limits.max_daily_loss_fraction:
        return RiskDecision(False, 0.0, "Daily loss limit reached.")
    budget = min(cash, equity * limits.max_order_fraction)
    quantity = budget / price
    if quantity <= 0:
        return RiskDecision(False, 0.0, "No available paper-trading budget.")
    return RiskDecision(True, quantity, "Paper risk checks passed.")
