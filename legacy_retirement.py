"""Read-only evidence that a missing signature cannot still execute."""
from datetime import datetime, timezone
from wallet_runtime import WalletError, GENESIS, SYSTEM
import base64
from solders.transaction import VersionedTransaction

RECOVERY_RPC = "https://api.mainnet-beta.solana.com"


async def prove_signed_expiry(rpc, wallet, row):
    """Prove a persisted, non-nonce signed transaction absent after rooted expiry."""
    try:
        tx = VersionedTransaction.from_bytes(base64.b64decode(row["signed_wire"], validate=True))
        keys = tx.message.account_keys
        if (len(tx.signatures) != 1 or tx.message.header.num_required_signatures != 1
                or str(keys[0]) != wallet or str(tx.signatures[0]) != row["signature"]
                or not all(tx.verify_with_results())):
            raise ValueError("Invalid signed operation")
        # Durable-nonce transactions require advanceNonceAccount as instruction zero.
        # An unresolved program ID is not evidence that a transaction is non-nonce.
        if tx.message.instructions:
            first = tx.message.instructions[0]
            if first.program_id_index >= len(keys):
                raise ValueError("Unresolved first program")
            if (keys[first.program_id_index] == SYSTEM
                    and bytes(first.data)[:4] == (4).to_bytes(4, "little")):
                raise ValueError("Durable nonce")
    except Exception:
        raise WalletError("The signed operation cannot be proved non-nonce; funds remain reserved.", 409) from None
    signature = row["signature"]
    options = {"encoding": "jsonParsed", "commitment": "finalized", "maxSupportedTransactionVersion": 0}
    validity = await rpc("isBlockhashValid", [str(tx.message.recent_blockhash), {"commitment": "finalized"}])
    if (not isinstance(validity, dict) or validity.get("value") is not False
            or type(validity.get("context", {}).get("slot")) is not int):
        raise WalletError("The transaction has not been proved expired; funds remain reserved.", 409)
    # Check absence AFTER rooted expiry, not just before it.
    statuses = await rpc("getSignatureStatuses", [[signature], {"searchTransactionHistory": True}])
    if (not isinstance(statuses, dict) or statuses.get("value") != [None]
            or type(statuses.get("context", {}).get("slot")) is not int
            or statuses["context"]["slot"] < validity["context"]["slot"]
            or await rpc("getTransaction", [signature, options]) is not None):
        raise WalletError("The network found the operation or returned incomplete evidence; funds remain reserved.", 409)
    created = datetime.fromisoformat(row["timestamp"]).timestamp()
    before = None
    covered = False
    for _ in range(3):
        history_options = {"limit": 1000, "commitment": "finalized",
                           "minContextSlot": validity["context"]["slot"]}
        if before:
            history_options["before"] = before
        page = await rpc("getSignaturesForAddress", [wallet, history_options])
        if not isinstance(page, list):
            raise WalletError("Incomplete wallet history; funds remain reserved.", 409)
        for record in page:
            if record.get("signature") == signature:
                raise WalletError("The signature appears in wallet history; funds remain reserved.", 409)
            if type(record.get("blockTime")) is not int:
                raise WalletError("Wallet history is missing timestamps; funds remain reserved.", 409)
            if record["blockTime"] < created - 300:
                covered = True
        if covered or len(page) < 1000:
            covered = True
            break
        before = page[-1]["signature"]
    if not covered:
        raise WalletError("Wallet history could not be fully checked; funds remain reserved.", 409)
    return {"rpc": RECOVERY_RPC, "signature": signature,
            "expiry_proof": "signed_non_nonce_blockhash_finalized_expired",
            "finalized_slot": validity["context"]["slot"],
            "status_slot": statuses["context"]["slot"], "wallet_history_checked": True,
            "outcome": "UNKNOWN",
            "rebroadcast": False, "refund": False}


async def prove_retirement(rpc, wallet, row):
    parsed = datetime.fromisoformat(row["timestamp"])
    created = (parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).timestamp()
    if await rpc("getGenesisHash", []) != GENESIS:
        raise WalletError("Recovery verification requires Solana mainnet.")
    if row.get("signed_wire"):
        return await prove_signed_expiry(rpc, wallet, row)
    sig = row["signature"]
    status = (await rpc("getSignatureStatuses", [[sig], {"searchTransactionHistory": True}]))["value"][0]
    tx_options = {"encoding": "jsonParsed", "commitment": "finalized", "maxSupportedTransactionVersion": 0}
    if status is not None or await rpc("getTransaction", [sig, tx_options]) is not None:
        raise WalletError("The network found this operation; reconcile its result instead of recovering it.", 409)
    nonce = await rpc("getProgramAccounts", [str(SYSTEM), {
        "encoding": "base64", "commitment": "finalized", "withContext": True,
        "dataSlice": {"offset": 0, "length": 0},
        "filters": [{"dataSize": 80}, {"memcmp": {"offset": 8, "bytes": wallet}}]}])
    if not isinstance(nonce.get("value"), list) or nonce["value"]:
        raise WalletError("A durable nonce cannot be ruled out; automatic recovery is blocked.", 409)
    slot = nonce["context"]["slot"]
    now = await rpc("getBlockTime", [slot])
    if type(now) is not int or now - created < 3600:
        raise WalletError("The operation must be at least one hour old by network time.", 409)
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
                raise WalletError("The signature appears in wallet history; recovery is blocked.", 409)
            if type(record.get("blockTime")) is not int:
                raise WalletError("Wallet history is missing block time; verification stopped.")
            if record["blockTime"] < cutoff:
                covered = True
                break
            records.append(record)
        if covered or len(page) < 1000:
            covered = True
            break
        before = page[-1]["signature"]
    if not covered:
        raise WalletError("Wallet history is too large to verify completely; the record was preserved.")
    anchors = [r["slot"] for r in records if r["blockTime"] > created + 300]
    if anchors:
        anchor = min(anchors)
        blocks = await rpc("getBlocksWithLimit", [anchor + 1, 151, {"commitment": "finalized"}])
        if (not isinstance(blocks, list) or len(blocks) != 151
                or any(type(block) is not int for block in blocks)
                or len(set(blocks)) != 151 or blocks != sorted(blocks)
                or blocks[-1] > slot or blocks[0] <= anchor):
            raise WalletError("The network did not prove blockhash expiry; the record was preserved.")
        expiry_proof, rooted_blocks = "151_finalized_blocks", len(blocks)
    else:
        recent_blockhash = row.get("recent_blockhash")
        if not isinstance(recent_blockhash, str) or not recent_blockhash:
            raise WalletError("No later wallet transaction or signed blockhash is available; the record was preserved.")
        validity = await rpc("isBlockhashValid", [recent_blockhash, {"commitment": "finalized"}])
        if not isinstance(validity, dict) or validity.get("value") is not False:
            raise WalletError("The network did not prove blockhash expiry; the record was preserved.")
        expiry_proof, rooted_blocks = "isBlockhashValid_false", 0
    for record in records:
        if record.get("err") is not None:
            continue
        tx = await rpc("getTransaction", [record["signature"], tx_options])
        if not tx or not tx.get("meta") or "err" not in tx["meta"]:
            raise WalletError("Full history of nonce changes is unavailable.")
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
                    raise WalletError("Wallet history contains a nonce change or incomplete instruction; the record was preserved.", 409)
    return {"rpc": RECOVERY_RPC, "signature": sig, "finalized_slot": slot,
            "finalized_time": now, "created_time": int(created), "history_cutoff": int(cutoff),
            "anchor_slot": min(anchors) if anchors else None,
            "rooted_blocks_after_anchor": rooted_blocks, "expiry_proof": expiry_proof,
            "nonce_accounts": 0, "nonce_changes_in_history": 0,
            "outcome": "UNKNOWN", "rebroadcast": False, "refund": False}
