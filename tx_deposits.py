"""Attribute ordinary incoming transfers to a cryptographically linked sender."""
from wallet_runtime import WalletError, SYSTEM, TOKEN, WalletRuntime, associated
from solders.pubkey import Pubkey
from custody_ledger import SOL, USDC


def incoming_amounts(tx, wallet, sender, no_match_ok=False):
    keys = tx["transaction"]["message"]["accountKeys"]
    addresses = [k["pubkey"] for k in keys]
    signers = {k["pubkey"] for k in keys if k.get("signer")}
    if wallet in signers or sender not in signers or wallet == sender:
        raise WalletError("Это не входящий перевод с вашего подтверждённого кошелька.", 403)
    meta = tx["meta"]
    if meta.get("innerInstructions") is None:
        raise WalletError("RPC не предоставил полные инструкции перевода.", 409)
    instructions = tx["transaction"]["message"]["instructions"] + [
        i for group in meta["innerInstructions"] for i in group["instructions"]]
    pre = {addresses[b["accountIndex"]]: b for b in meta.get("preTokenBalances", []) or []}
    post = {addresses[b["accountIndex"]]: b for b in meta.get("postTokenBalances", []) or []}
    amounts = {}
    for ix in instructions:
        parsed = ix.get("parsed", {})
        if not isinstance(parsed, dict):
            continue
        info, kind = parsed.get("info", {}), parsed.get("type")
        asset, raw = None, None
        if ix.get("programId") == str(SYSTEM) and kind == "transfer" and info.get("destination") == wallet:
            if info.get("source") != sender:
                raise WalletError("Перевод содержит деньги другого отправителя.", 403)
            asset, raw = SOL, info.get("lamports")
        elif ix.get("programId") == str(TOKEN) and kind in ("transfer", "transferChecked"):
            target = post.get(info.get("destination"), {})
            if target.get("owner") != wallet or target.get("mint") != USDC:
                continue
            if info.get("destination") != str(associated(Pubkey.from_string(wallet), Pubkey.from_string(USDC))):
                raise WalletError("USDC должен поступить на обычный USDC ATA общего кошелька.", 409)
            source = pre.get(info.get("source"), {})
            authority = info.get("authority")
            # Phantom can include the authority twice in account metas. RPC then
            # names it multisigAuthority even for a single ordinary wallet signer.
            # Accept only this exact case; real foreign/multi-signer authority fails.
            if (authority is None and info.get("multisigAuthority") == sender
                    and info.get("signers") == [sender]):
                authority = sender
            if (source.get("owner") != sender or source.get("mint") != USDC
                    or authority != sender):
                raise WalletError("USDC отправлен не с вашего подтверждённого кошелька.", 403)
            if kind == "transferChecked" and (info.get("mint") != USDC
                    or info.get("tokenAmount", {}).get("decimals") != 6):
                raise WalletError("Неверный mint/decimals USDC.", 409)
            asset = USDC
            raw = info.get("amount") if kind == "transfer" else info.get("tokenAmount", {}).get("amount")
        if asset:
            raw = int(raw)
            if raw <= 0 or raw > 2**63 - 1:
                raise WalletError("Недопустимая сумма перевода.", 409)
            amounts[asset] = amounts.get(asset, 0) + raw
    if not amounts:
        if no_match_ok:
            return {}
        raise WalletError("В Tx нет перевода USDC или SOL с вашего кошелька на общий адрес.", 409)
    for asset, raw in amounts.items():
        if asset == SOL:
            index = addresses.index(wallet)
            delta = meta["postBalances"][index] - meta["preBalances"][index]
        else:
            delta = WalletRuntime.delta(tx, wallet, USDC)
        if delta < raw:
            raise WalletError("Фактическое поступление меньше суммы перевода.", 409)
    return amounts


def incoming_amounts_for_wallets(tx, wallets, sender):
    """Attribute an incoming deposit to any configured legacy/current custody address."""
    targets = list(dict.fromkeys(str(wallet) for wallet in wallets if wallet))
    if not targets:
        raise WalletError("No custody deposit address is configured.", 503)
    amounts = {}
    matched = False
    for target in targets:
        received = incoming_amounts(tx, target, sender, no_match_ok=True)
        if not received:
            continue
        matched = True
        for asset, raw in received.items():
            amounts[asset] = amounts.get(asset, 0) + raw
    if not matched:
        raise WalletError("No SOL or USDC transfer to the reserve or hot-wallet address was found.", 409)
    return amounts
