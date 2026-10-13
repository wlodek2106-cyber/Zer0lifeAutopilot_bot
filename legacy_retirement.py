"""Read-only evidence for retiring an old owner-only signature, never a refund."""
from datetime import datetime, timezone
from wallet_runtime import WalletError, GENESIS, SYSTEM

RECOVERY_RPC = "https://api.mainnet-beta.solana.com"


async def prove_retirement(rpc, wallet, row):
    parsed = datetime.fromisoformat(row["timestamp"])
    created = (parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).timestamp()
    if await rpc("getGenesisHash", []) != GENESIS:
        raise WalletError("Проверка восстановления требует Solana mainnet.")
    sig = row["signature"]
    status = (await rpc("getSignatureStatuses", [[sig], {"searchTransactionHistory": True}]))["value"][0]
    tx_options = {"encoding": "jsonParsed", "commitment": "finalized", "maxSupportedTransactionVersion": 0}
    if status is not None or await rpc("getTransaction", [sig, tx_options]) is not None:
        raise WalletError("Сеть нашла операцию. Её результат нужно учесть, а не архивировать.", 409)
    nonce = await rpc("getProgramAccounts", [str(SYSTEM), {
        "encoding": "base64", "commitment": "finalized", "withContext": True,
        "dataSlice": {"offset": 0, "length": 0},
        "filters": [{"dataSize": 80}, {"memcmp": {"offset": 8, "bytes": wallet}}]}])
    if not isinstance(nonce.get("value"), list) or nonce["value"]:
        raise WalletError("Нельзя исключить durable nonce. Автоматическое архивирование запрещено.", 409)
    slot = nonce["context"]["slot"]
    now = await rpc("getBlockTime", [slot])
    if type(now) is not int or now - created < 3600:
        raise WalletError("Для проверки истечения старая запись должна быть старше часа по времени сети.", 409)
    cutoff, before, covered, records = created - 300, None, False, []
    for _ in range(3):
        options = {"limit": 1000, "commitment": "finalized"}
        if before:
            options["before"] = before
        page = await rpc("getSignaturesForAddress", [wallet, options])
        if not isinstance(page, list):
            raise WalletError("Неполная история кошелька.")
        for record in page:
            if record["signature"] == sig:
                raise WalletError("Подпись обнаружена в истории. Архивирование запрещено.", 409)
            if type(record.get("blockTime")) is not int:
                raise WalletError("В истории отсутствует время блока; проверка остановлена.")
            if record["blockTime"] < cutoff:
                covered = True
                break
            records.append(record)
        if covered or len(page) < 1000:
            covered = True
            break
        before = page[-1]["signature"]
    if not covered:
        raise WalletError("История слишком большая для полной проверки; запись сохранена.")
    anchors = [r["slot"] for r in records if r["blockTime"] > created + 300]
    if not anchors:
        raise WalletError("Нет более позднего подтверждённого блока кошелька для проверки истечения.")
    anchor = min(anchors)
    blocks = await rpc("getBlocksWithLimit", [anchor + 1, 151, {"commitment": "finalized"}])
    if (not isinstance(blocks, list) or len(blocks) != 151
            or any(type(block) is not int for block in blocks)
            or len(set(blocks)) != 151 or blocks != sorted(blocks)
            or blocks[-1] > slot or blocks[0] <= anchor):
        raise WalletError("Сеть не подтвердила истечение окна blockhash; запись сохранена.")
    for record in records:
        if record.get("err") is not None:
            continue
        tx = await rpc("getTransaction", [record["signature"], tx_options])
        if not tx or not tx.get("meta") or "err" not in tx["meta"]:
            raise WalletError("Недоступна полная история изменения nonce.")
        if tx["meta"]["err"] is not None:
            continue
        top = tx["transaction"]["message"]["instructions"]
        inner = tx["meta"].get("innerInstructions")
        if inner is None:
            raise WalletError("RPC не предоставил внутренние инструкции; проверка nonce остановлена.")
        instructions = top + [i for group in inner for i in group["instructions"]]
        for instruction in instructions:
            if instruction.get("programId") == str(SYSTEM):
                kind = instruction.get("parsed", {}).get("type")
                if not isinstance(kind, str) or "nonce" in kind.lower():
                    raise WalletError("История содержит изменение nonce или неполную инструкцию; запись сохранена.", 409)
    return {"rpc": RECOVERY_RPC, "signature": sig, "finalized_slot": slot,
            "finalized_time": now, "created_time": int(created), "history_cutoff": int(cutoff),
            "anchor_slot": anchor, "rooted_blocks_after_anchor": len(blocks),
            "nonce_accounts": 0, "nonce_changes_in_history": 0,
            "outcome": "UNKNOWN", "rebroadcast": False, "refund": False}
