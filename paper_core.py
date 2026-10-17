"""Forward-only Core simulation using the real deterministic execution policy.

No signer, real ledger, transfer function, live toggle or payout function is accepted.
Model estimates are deliberately not represented as real blockchain fills.
"""
import time

import execution_policy as policy
from custody_ledger import USDC, SOL
from paper_store import PaperStore
import paper_benchmark
from wallet_runtime import WalletError


class PaperCore:
    def __init__(self, path, watchlist, universe):
        self.store = PaperStore(path)
        self.watchlist, self.universe = watchlist, universe

    def settings(self, run):
        # Synthetic context ONLY, never written to real custody settings.
        return {"trading": bool(run["enabled"]), "order_raw": run["order_raw"]}

    def held(self, run):
        return [{**p, "uid": run["uid"], "asset": SOL if p["mint"] == policy.WRAPPED_SOL else p["mint"],
                 "funding_asset": USDC} for p in self.store.positions(run["id"])]

    def order(self, run, kind, symbol, positions, position=None):
        watch = self.watchlist[symbol] if kind == "BUY" else {
            "mint": position["mint"], "decimals": position["decimals"]}
        asset = SOL if watch["mint"] == policy.WRAPPED_SOL else watch["mint"]
        return policy.order(uid=run["uid"], kind=kind, asset=USDC if kind == "BUY" else asset,
            output_asset=asset if kind == "BUY" else USDC,
            raw=run["order_raw"] if kind == "BUY" else position["raw"],
            metadata={"symbol": symbol, "decimals": watch["decimals"]},
            position_id=position["id"] if position else None, settings=self.settings(run),
            positions=positions, watchlist=self.watchlist, universes={"main": self.universe()},
            balance=run["cash_raw"]).require()

    def cost_evidence(self, expenses, head):
        if (not policy.fresh(expenses["quoted_at"], time.time(), policy.QUOTE_SECONDS)
                or abs(expenses["context_slot"]-head) > policy.MAX_SLOT_DISTANCE):
            raise WalletError("BLOCKED: paper expense evidence is stale.", 409)

    async def sell_evidence(self, market, position):
        evidence = await market.quote(position["mint"], USDC, position["raw"])
        expenses = await market.expenses()
        head = await market.head()
        evidence.check("SELL", position["mint"], USDC, position["raw"], head)
        self.cost_evidence(expenses, head)
        return evidence, expenses

    async def tick(self, uid, market, snapshot, prices):
        run = self.store.current(uid)
        if not run or not run["enabled"]:
            return
        try:
            if await paper_benchmark.update(self, run, market):
                await self._tick(run, market, snapshot, prices)
        except WalletError as exc:
            self.store.note(run, str(exc))
        except Exception as exc:
            # Provider payloads and credentials must not enter a user's report.
            self.store.note(run, "BLOCKED: paper data/check failed ("+type(exc).__name__+"). No fill recorded.")
        finally:
            self.store.checkpoint(run)

    async def _tick(self, run, market, snapshot, prices):
        positions = self.held(run)
        marks, failed = {}, False
        for position in positions:
            try:
                evidence, expenses = await self.sell_evidence(market, position)
                proceeds = policy.integer(evidence.value["otherAmountThreshold"])
                self.store.mark(run, position, proceeds, expenses["expense_raw"], evidence.requested_at)
                marks[position["id"]] = (evidence, expenses)
            except WalletError:
                failed = True
        # Same Core stop/take-profit ratios as the live strategy; never a promise.
        target = next((p for p in positions if p["id"] in marks
                       and policy.positive(prices.get(p["symbol"]))
                       and policy.risk_exit(prices[p["symbol"]], p["entry_price"])), None)
        if target:
            evidence, expenses = marks[target["id"]]
            self.order(run, "SELL", target["symbol"], positions, target)
            head = await market.head()
            evidence.check("SELL", target["mint"], USDC, target["raw"], head)
            self.cost_evidence(expenses, head)
            closed = self.store.close(run, target, policy.integer(evidence.value["otherAmountThreshold"]),
                                     expenses["expense_raw"], "Core stop / take-profit trigger")
            self.store.note(run, "Virtual Core risk exit recorded." if closed else "Run changed; virtual exit discarded.")
            return
        if failed:
            self.store.note(run, "WAIT: an open position cannot be valued with a fresh exit quote. No new entry.")
            return
        if not snapshot:
            self.store.note(run, "WAIT: warming market data or awaiting AI within its existing API quota.")
            return
        decision = snapshot["decisions"]["main"]
        verdict = policy.proposal(mode="main", decision=decision, universe=self.universe(),
            created_at=snapshot["created_at"], original=snapshot["prices"], prices=prices,
            samples=snapshot["samples"], held=positions, balance=run["cash_raw"], order_raw=run["order_raw"])
        if not verdict.allowed:
            self.store.note(run, verdict.reason)
            return
        aid = snapshot["id"]
        if not isinstance(aid, str) or not aid:
            raise WalletError("BLOCKED: paper analysis has no server identifier.", 409)
        if self.store.claimed(run["id"], aid):
            self.store.note(run, "WAIT: this AI analysis already produced a virtual fill.")
            return
        action, symbol = decision["action"], decision["symbol"]
        position = next((p for p in positions if p["symbol"] == symbol), None)
        self.order(run, action, symbol, positions, position)
        if action == "SELL":
            evidence, expenses = marks[position["id"]]
            head = await market.head()
            evidence.check("SELL", position["mint"], USDC, position["raw"], head)
            self.cost_evidence(expenses, head)
            if not policy.fresh(snapshot["created_at"], time.time(), policy.DECISION_SECONDS):
                raise WalletError("BLOCKED: AI decision expired before virtual exit.", 409)
            closed = self.store.close(run, position, policy.integer(evidence.value["otherAmountThreshold"]),
                                     expenses["expense_raw"], decision["reason"], aid)
            self.store.note(run, "Virtual SELL recorded." if closed else "Run changed; virtual SELL discarded.")
            return
        watch = self.watchlist[symbol]
        forward = await market.quote(USDC, watch["mint"], run["order_raw"])
        token_raw = policy.integer(forward.value["otherAmountThreshold"])
        forward.check("BUY", USDC, watch["mint"], run["order_raw"])
        reverse = await market.quote(watch["mint"], USDC, token_raw)
        expenses = await market.expenses()
        head = await market.head()
        forward.check("BUY", USDC, watch["mint"], run["order_raw"], head)
        reverse.check("BUY", watch["mint"], USDC, token_raw, head)
        self.cost_evidence(expenses, head)
        costs = policy.costs({"quoted_at": min(forward.requested_at, reverse.requested_at, expenses["quoted_at"]),
            "input_raw": run["order_raw"], "exit_minimum_raw": policy.integer(reverse.value["otherAmountThreshold"]),
            "exit_rent_lamports": expenses["rent_lamports"], "sol_usdc": expenses["sol_usdc"]},
            expenses["expense_lamports"])
        if costs["status"] != "ALLOW":
            self.store.note(run, costs["reason"])
            return
        # Re-read server-owned state after network waits. Pauses/new runs invalidate
        # the in-flight revision even if a user resumes before this coroutine returns.
        current = self.store.current(run["uid"])
        if not current or current["id"] != run["id"] or current["revision"] != run["revision"]:
            return
        self.order(current, "BUY", symbol, self.held(current))
        if not policy.fresh(snapshot["created_at"], time.time(), policy.DECISION_SECONDS):
            raise WalletError("BLOCKED: AI decision expired before virtual entry.", 409)
        bought = self.store.buy(run, symbol, watch, token_raw, expenses["expense_raw"],
                                (run["order_raw"]/1_000_000)/(token_raw/10**watch["decimals"]),
                                aid, decision["reason"])
        if bought:
            owned = next(p for p in self.store.positions(run["id"]) if p["symbol"] == symbol)
            self.store.mark(run, owned, policy.integer(reverse.value["otherAmountThreshold"]),
                            expenses["expense_raw"], reverse.requested_at)
        self.store.note(run, "Virtual BUY recorded using minimum quoted output." if bought
                        else "BLOCKED: run changed, analysis claimed or virtual cash cannot cover order plus expenses.")
