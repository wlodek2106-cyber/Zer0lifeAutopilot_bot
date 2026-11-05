"""Validated mark-to-market portfolio accounting for paper trading only.

This module has no persistence, network, wallet, signer, or live-order access.
Callers supply trusted paper cash, positions, and latest prices.
"""
from dataclasses import dataclass
from math import isfinite


@dataclass(frozen=True)
class PositionValue:
    symbol: str
    quantity: float
    mark_price: float
    market_value: float


@dataclass(frozen=True)
class PortfolioSnapshot:
    cash: float
    positions: tuple
    market_value: float
    equity: float
    cost_basis: float
    unrealized_pnl: float
    realized_pnl: float
    total_pnl: float
    return_pct: float
    peak_equity: float
    drawdown_pct: float


class PaperPortfolio:
    """Compute portfolio metrics from a paper broker's balances and marks."""

    def __init__(self, starting_cash):
        self._number(starting_cash, "starting_cash")
        if starting_cash <= 0:
            raise ValueError("Starting cash must be positive.")
        self.starting_cash = float(starting_cash)
        self.peak_equity = float(starting_cash)
        self.realized_pnl = 0.0

    @staticmethod
    def _number(value, name):
        if type(value) not in (int, float) or not isfinite(value):
            raise ValueError(name + " must be a finite number.")

    def record_realized_pnl(self, amount):
        self._number(amount, "realized_pnl")
        updated = self.realized_pnl + amount
        if not isfinite(updated):
            raise ValueError("Realized PnL is outside safe numeric bounds.")
        self.realized_pnl = updated

    def snapshot(self, *, cash, positions, prices, cost_basis=None):
        self._number(cash, "cash")
        if cash < 0 or not isinstance(positions, dict) or not isinstance(prices, dict):
            raise ValueError("Invalid paper portfolio balances or price map.")
        if cost_basis is None:
            cost_basis = {}
        if not isinstance(cost_basis, dict):
            raise ValueError("Cost basis must be a mapping.")

        valued = []
        market_value = 0.0
        total_cost = 0.0
        for symbol, quantity in positions.items():
            if not isinstance(symbol, str) or not symbol.strip():
                raise ValueError("Position symbols must be non-empty text.")
            self._number(quantity, "position quantity")
            if quantity <= 0:
                raise ValueError("Position quantities must be positive.")
            if symbol not in prices:
                raise ValueError("Missing mark price for " + symbol + ".")
            price = prices[symbol]
            self._number(price, "mark price")
            if price <= 0:
                raise ValueError("Mark prices must be positive.")
            value = quantity * price
            basis = cost_basis.get(symbol, value)
            self._number(basis, "position cost basis")
            if basis < 0:
                raise ValueError("Position cost basis cannot be negative.")
            if not isfinite(value):
                raise ValueError("Position market value overflow.")
            market_value += value
            total_cost += basis
            if not isfinite(market_value) or not isfinite(total_cost):
                raise ValueError("Portfolio totals overflow.")
            valued.append(PositionValue(symbol, float(quantity), float(price), value))

        equity = cash + market_value
        unrealized = market_value - total_cost
        total_pnl = equity - self.starting_cash
        if not all(isfinite(v) for v in (equity, unrealized, total_pnl)):
            raise ValueError("Portfolio metrics overflow.")
        self.peak_equity = max(self.peak_equity, equity)
        drawdown = ((self.peak_equity - equity) / self.peak_equity
                    if self.peak_equity else 0.0)
        return_pct = (total_pnl / self.starting_cash) * 100.0
        return PortfolioSnapshot(
            cash=float(cash),
            positions=tuple(valued),
            market_value=market_value,
            equity=equity,
            cost_basis=total_cost,
            unrealized_pnl=unrealized,
            realized_pnl=self.realized_pnl,
            total_pnl=total_pnl,
            return_pct=return_pct,
            peak_equity=self.peak_equity,
            drawdown_pct=drawdown * 100.0,
        )
