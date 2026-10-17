"""Quote-only market reader. Never builds, signs, simulates or sends a transaction."""
import time
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING

import aiohttp

import execution_policy as policy
from custody_ledger import USDC
from entry_costs import EXIT_NETWORK_LAMPORTS
from wallet_runtime import WalletError


@dataclass(frozen=True)
class Evidence:
    value: dict
    requested_at: float
    head_slot: int

    def check(self, kind, source, destination, raw, head=None):
        return policy.quote(kind=kind, value=self.value, input_mint=source,
            output_mint=destination, raw=raw, received_at=self.requested_at,
            head_slot=self.head_slot if head is None else head).require()


class QuoteMarket:
    def __init__(self, session, read_rpc, headers=None):
        self.session, self.read_rpc, self.headers = session, read_rpc, headers or {}
        self._expenses = None

    async def head(self):
        value = await self.read_rpc("getSlot", [{"commitment": "confirmed"}])
        if type(value) is not int or value <= 0:
            raise WalletError("Paper test paused: current Solana slot is unavailable.", 503)
        return value

    async def quote(self, source, destination, raw):
        started = time.time()
        async with self.session.get("https://api.jup.ag/swap/v1/quote",
                headers=self.headers, params={"inputMint": source, "outputMint": destination,
                "amount": raw, "slippageBps": policy.MAX_SLIPPAGE_BPS},
                timeout=aiohttp.ClientTimeout(total=8)) as response:
            if response.status != 200:
                raise WalletError("Paper quote unavailable; no virtual fill recorded.", 503)
            value = await response.json()
        return Evidence(value, started, await self.head())

    async def expenses(self):
        """Each leg assumes capped network/tip costs AND a new token account's rent.

        No rent rebate is assumed. Live fees are paid by the operator; paper equity
        deliberately includes them to evaluate strategy economics, not ledger charges.
        """
        if self._expenses and policy.fresh(self._expenses["quoted_at"], time.time(), 20):
            return self._expenses
        reference = await self.quote(policy.WRAPPED_SOL, USDC, 1_000_000_000)
        reference.check("BUY", policy.WRAPPED_SOL, USDC, 1_000_000_000)
        rent = await self.read_rpc("getMinimumBalanceForRentExemption",
                                   [165, {"commitment": "confirmed"}])
        if type(rent) is not int or rent <= 0:
            raise WalletError("Paper cost evidence is unavailable; no virtual fill recorded.", 503)
        price = Decimal(policy.integer(reference.value["outAmount"])) / Decimal(1_000_000)
        lamports = EXIT_NETWORK_LAMPORTS + rent
        raw = int((Decimal(lamports) * price / 1000).to_integral_value(rounding=ROUND_CEILING))
        self._expenses = {"quoted_at": reference.requested_at, "sol_usdc": str(price),
            "rent_lamports": rent, "expense_lamports": lamports, "expense_raw": raw,
            "context_slot": reference.value["contextSlot"]}
        return self._expenses
