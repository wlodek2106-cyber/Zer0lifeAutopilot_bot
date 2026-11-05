"""In-memory paper broker. No RPC, signer, wallet, or real order functionality."""
from dataclasses import dataclass
from math import isfinite


@dataclass(frozen=True)
class Fill:
    side: str
    quantity: float
    price: float
    fee: float
    cash_after: float


class PaperBroker:
    def __init__(self, starting_cash=1000.0, fee_bps=10.0, slippage_bps=5.0):
        values = (starting_cash, fee_bps, slippage_bps)
        if any(type(v) not in (int, float) or not isfinite(v) for v in values):
            raise ValueError("Broker configuration must be finite numbers.")
        if (starting_cash <= 0 or not 0 <= fee_bps <= 1000
                or not 0 <= slippage_bps <= 1000):
            raise ValueError("Broker configuration is outside safe bounds.")
        self.cash = float(starting_cash)
        self.fee_bps = float(fee_bps)
        self.slippage_bps = float(slippage_bps)
        self.positions = {}

    def buy(self, symbol, quantity, market_price):
        if not isinstance(symbol, str) or not symbol.strip() or type(quantity) not in (int, float) or type(market_price) not in (int, float):
            raise ValueError("Invalid paper order.")
        if not isfinite(quantity) or not isfinite(market_price) or quantity <= 0 or market_price <= 0:
            raise ValueError("Quantity and price must be finite and positive.")
        fill_price = market_price * (1 + self.slippage_bps / 10000)
        notional = quantity * fill_price
        fee = notional * self.fee_bps / 10000
        total = notional + fee
        held = self.positions.get(symbol, 0.0)
        if type(held) not in (int, float) or not isfinite(held) or held < 0:
            raise ValueError("Existing paper position is invalid.")
        new_position = held + quantity
        if (not all(isfinite(x) for x in (fill_price, notional, fee, total, new_position))
                or total < 0):
            raise ValueError("Paper order arithmetic overflowed.")
        if total > self.cash:
            raise ValueError("Insufficient paper cash.")
        self.cash -= total
        self.positions[symbol] = new_position
        return Fill("BUY", quantity, fill_price, fee, self.cash)

    def sell(self, symbol, quantity, market_price):
        if not isinstance(symbol, str) or not symbol.strip() or type(quantity) not in (int, float) or type(market_price) not in (int, float):
            raise ValueError("Invalid paper order.")
        if not isfinite(quantity) or not isfinite(market_price) or quantity <= 0 or market_price <= 0:
            raise ValueError("Quantity and price must be finite and positive.")
        held = self.positions.get(symbol, 0.0)
        if type(held) not in (int, float) or not isfinite(held) or held <= 0:
            raise ValueError("No paper position is available to sell.")
        if quantity > held:
            raise ValueError("Cannot sell more than the paper position.")
        fill_price = market_price * (1 - self.slippage_bps / 10000)
        gross = quantity * fill_price
        fee = gross * self.fee_bps / 10000
        cash_after = self.cash + gross - fee
        if not all(isfinite(x) for x in (fill_price, gross, fee, cash_after)):
            raise ValueError("Paper order arithmetic overflowed.")
        self.cash = cash_after
        remaining = held - quantity
        if remaining:
            self.positions[symbol] = remaining
        else:
            self.positions.pop(symbol, None)
        return Fill("SELL", quantity, fill_price, fee, cash_after)
