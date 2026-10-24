"""Strict, read-only SPL Token / Token-2022 asset validation helpers."""
import base64
import struct


TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM_ID = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
TOKEN_PROGRAM_IDS = (TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID)

_MINT_EXTENSION_NAMES = {"metadataPointer", "tokenMetadata"}
_TOKEN_ACCOUNT_EXTENSION_IDS = {7}  # ImmutableOwner


def validate_mint_account(account, mint, *, expected_decimals=None,
                          expected_symbol=None, expected_program=None,
                          allow_token_2022=False):
    """Return the token program only for a finalized, explicitly supported mint."""
    if not isinstance(account, dict):
        raise ValueError("on-chain mint account is unavailable")
    program = account.get("owner")
    if program not in TOKEN_PROGRAM_IDS:
        raise ValueError("mint is owned by an unsupported token program")
    if expected_program and program != expected_program:
        raise ValueError("mint token program differs from the saved asset identity")

    data = account.get("data")
    parsed = data.get("parsed") if isinstance(data, dict) else None
    info = parsed.get("info") if isinstance(parsed, dict) else None
    if not isinstance(info, dict) or parsed.get("type") != "mint":
        raise ValueError("on-chain mint data is unavailable")
    decimals = info.get("decimals")
    if (info.get("isInitialized") is not True or type(decimals) is not int
            or not 0 <= decimals <= 9):
        raise ValueError("mint is not initialized or its decimals are unsupported")
    if expected_decimals is not None and decimals != expected_decimals:
        raise ValueError("mint decimals differ from the saved asset identity")
    if info.get("mintAuthority") is not None:
        raise ValueError("mint authority is still enabled")
    if info.get("freezeAuthority") is not None:
        raise ValueError("freeze authority is still enabled")

    extensions = info.get("extensions") or []
    if not isinstance(extensions, list):
        raise ValueError("mint extensions could not be verified")
    if program == TOKEN_PROGRAM_ID:
        if extensions:
            raise ValueError("unexpected extensions on a legacy SPL mint")
        return program
    if not allow_token_2022:
        raise ValueError("Token-2022 is not enabled for this asset")

    by_name = {}
    for item in extensions:
        if not isinstance(item, dict) or item.get("extension") in by_name:
            raise ValueError("invalid or duplicate Token-2022 mint extension")
        by_name[item.get("extension")] = item.get("state")
    if set(by_name) != _MINT_EXTENSION_NAMES:
        raise ValueError("Token-2022 mint has an unsupported extension set")

    pointer = by_name.get("metadataPointer")
    metadata = by_name.get("tokenMetadata")
    if not isinstance(pointer, dict) or not isinstance(metadata, dict):
        raise ValueError("Token-2022 metadata extensions are incomplete")
    if pointer.get("authority") is not None or pointer.get("metadataAddress") != mint:
        raise ValueError("Token-2022 metadata pointer is mutable or points elsewhere")
    if (metadata.get("updateAuthority") is not None
            or metadata.get("mint") != mint
            or not isinstance(metadata.get("name"), str)
            or not isinstance(metadata.get("symbol"), str)
            or metadata.get("additionalMetadata") not in ([], None)):
        raise ValueError("Token-2022 metadata is mutable or does not match the mint")
    if expected_symbol is not None and (
            metadata.get("name") != expected_symbol or metadata.get("symbol") != expected_symbol):
        raise ValueError("Token-2022 name/symbol differs from the saved asset identity")
    return program


def _extension_tlvs(data, start):
    extensions = {}
    offset = start
    while offset < len(data):
        tail = data[offset:]
        if not any(tail):
            break
        if len(tail) < 4:
            raise ValueError("truncated Token-2022 account extension")
        extension_type, length = struct.unpack_from("<HH", data, offset)
        offset += 4
        if extension_type == 0 or offset + length > len(data):
            raise ValueError("invalid Token-2022 account extension length")
        if extension_type in extensions:
            raise ValueError("duplicate Token-2022 account extension")
        extensions[extension_type] = data[offset:offset + length]
        offset += length
    return extensions


def token_account_data(account, expected_program=None):
    """Decode the common token-account base fields; allow only immutable-owner TLV."""
    if not isinstance(account, dict):
        raise ValueError("missing token account")
    program = account.get("owner")
    if program not in TOKEN_PROGRAM_IDS or (
            expected_program is not None and program != expected_program):
        raise ValueError("token account is owned by an unexpected program")
    encoded = account.get("data")
    if not isinstance(encoded, list) or not encoded or not isinstance(encoded[0], str):
        raise ValueError("token account data is unavailable")
    try:
        data = base64.b64decode(encoded[0], validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError("token account data is malformed") from exc
    if len(data) < 165:
        raise ValueError("token account base data is truncated")
    if program == TOKEN_PROGRAM_ID:
        if len(data) != 165:
            raise ValueError("legacy SPL account has unexpected extension data")
        return data
    if len(data) == 165:
        return data
    if len(data) < 166 or data[165] != 2:
        raise ValueError("Token-2022 account type is invalid")
    extensions = _extension_tlvs(data, 166)
    if (set(extensions) - _TOKEN_ACCOUNT_EXTENSION_IDS
            or any(len(value) != 0 for value in extensions.values())):
        raise ValueError("Token-2022 account has unsupported extensions")
    return data
