"""Read-only SOL buy-and-hold baseline. Never reconstructs historical entries."""
import execution_policy as policy
from custody_ledger import USDC
from wallet_runtime import WalletError


async def update(core, run, market):
    store = core.store
    b = store.benchmark(run["id"])
    if b is None:
        # Existing runs retain their strategy history, without a fabricated baseline.
        return True
    entering = b["raw"] is None
    try:
        if entering:
            expenses = await market.expenses()
            spend = run["initial_raw"]-expenses["expense_raw"]
            if spend <= 0:
                raise WalletError("WAIT: virtual capital cannot cover benchmark costs.", 409)
            entry = await market.quote(USDC, policy.WRAPPED_SOL, spend)
            quantity = policy.integer(entry.value["otherAmountThreshold"])
            entry.check("BUY", USDC, policy.WRAPPED_SOL, spend)
        else:
            quantity = b["raw"]
        exit_quote = await market.quote(policy.WRAPPED_SOL, USDC, quantity)
        exit_expenses = await market.expenses()
        head = await market.head()
        exit_quote.check("SELL", policy.WRAPPED_SOL, USDC, quantity, head)
        core.cost_evidence(exit_expenses, head)
        values = {"mark_raw": policy.integer(exit_quote.value["otherAmountThreshold"]),
                  "mark_expense_raw": exit_expenses["expense_raw"],
                  "mark_at": int(exit_quote.requested_at)}
        if entering:
            entry.check("BUY", USDC, policy.WRAPPED_SOL, spend, head)
            core.cost_evidence(expenses, head)
            values.update(started_at=int(entry.requested_at), raw=quantity,
                          entry_raw=spend, entry_expense_raw=expenses["expense_raw"], cash_raw=0)
        return store.benchmark_save(run, values)
    except Exception:
        # Clear the valuation immediately rather than display an old winning result.
        store.benchmark_save(run, {"mark_at": None})
        if entering:
            store.note(run, "WAIT: SOL baseline entry evidence unavailable; both strategies remain in virtual cash.")
            return False
        return True  # A benchmark outage must not block Core risk exits.
