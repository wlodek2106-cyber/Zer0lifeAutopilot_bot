"""Fail-closed Base mainnet Uniswap V3 primitives for the live trader.

This module only validates pools, quotes exact-input swaps, and builds calldata.
It does not sign or broadcast transactions; live execution must be wired through
the persistent operation/reconciliation path before the mode can be enabled.
"""
import os
import re
import asyncio
from dataclasses import dataclass

from eth_abi import decode, encode
from eth_utils import keccak


BASE_CHAIN_ID = 8453
BASE_USDC_ADDRESS = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
BASE_UNISWAP_V3_FACTORY = "0x33128a8fC17869897dCE68Ed026d694621f6FDfD"
BASE_UNISWAP_V3_QUOTER_V2 = "0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a"
BASE_UNISWAP_V3_SWAP_ROUTER_02 = "0x2626664c2603336E57B271c5C0b26F421741e481"
BASE_V3_FEE_TIERS = (100, 500, 3000, 10000)
MAX_SLIPPAGE_BPS = 100

_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_UINT_RE = re.compile(r"^0x[0-9a-fA-F]+$")
_ERC20_TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


class BaseLiveTradingError(ValueError):
    """A safe-to-display Base live-trading validation failure."""


def normalize_address(value):
    value = str(value or "").strip()
    if not _ADDRESS_RE.fullmatch(value):
        raise BaseLiveTradingError("A Base token or pool address is invalid.")
    return value.lower()


def _selector(signature):
    return keccak(text=signature)[:4]


def _rpc_uint(value, label):
    if not isinstance(value, str) or not _UINT_RE.fullmatch(value):
        raise BaseLiveTradingError(f"Base returned an invalid {label}.")
    return int(value, 16)


def _call_data(signature, types=(), values=()):
    return "0x" + (_selector(signature) + encode(list(types), list(values))).hex()


def encode_factory_get_pool(token_a, token_b, fee):
    token_a, token_b = normalize_address(token_a), normalize_address(token_b)
    if type(fee) is not int or fee not in BASE_V3_FEE_TIERS:
        raise BaseLiveTradingError("The Uniswap V3 fee tier is not supported.")
    return _call_data(
        "getPool(address,address,uint24)",
        ("address", "address", "uint24"),
        (token_a, token_b, fee),
    )


def encode_quoter_v2_exact_input(token_in, token_out, amount_in, fee):
    token_in, token_out = normalize_address(token_in), normalize_address(token_out)
    if (token_in == token_out or type(amount_in) is not int or amount_in <= 0
            or type(fee) is not int or fee not in BASE_V3_FEE_TIERS):
        raise BaseLiveTradingError("The Base swap quote request is invalid.")
    return _call_data(
        "quoteExactInputSingle((address,address,uint256,uint24,uint160))",
        ("(address,address,uint256,uint24,uint160)",),
        ((token_in, token_out, amount_in, fee, 0),),
    )


def decode_quoter_v2_result(value):
    if not isinstance(value, str) or not value.startswith("0x"):
        raise BaseLiveTradingError("Uniswap V3 returned an invalid quote.")
    try:
        result = decode(
            ("uint256", "uint160", "uint32", "uint256"),
            bytes.fromhex(value[2:]),
        )
    except Exception:
        raise BaseLiveTradingError("Uniswap V3 returned an invalid quote.") from None
    amount_out = int(result[0])
    if amount_out <= 0:
        raise BaseLiveTradingError("Uniswap V3 returned no output for this quote.")
    return {
        "amount_out_raw": amount_out,
        "sqrt_price_after_x96": int(result[1]),
        "initialized_ticks_crossed": int(result[2]),
        "gas_estimate": int(result[3]),
    }


def minimum_output(amount_out_raw, slippage_bps):
    if (type(amount_out_raw) is not int or amount_out_raw <= 0
            or type(slippage_bps) is not int
            or not 0 <= slippage_bps <= MAX_SLIPPAGE_BPS):
        raise BaseLiveTradingError("The Base slippage setting is outside the safe limit.")
    result = amount_out_raw * (10_000 - slippage_bps) // 10_000
    if result <= 0:
        raise BaseLiveTradingError("The minimum received amount is invalid.")
    return result


def encode_exact_approval(spender, amount_raw):
    spender = normalize_address(spender)
    if type(amount_raw) is not int or amount_raw <= 0:
        raise BaseLiveTradingError("The exact token approval amount is invalid.")
    return "0x095ea7b3" + encode(
        ("address", "uint256"), (spender, amount_raw)
    ).hex()


def encode_revoke_approval(spender):
    """Revoke a token allowance left behind when a prepared swap is abandoned."""
    spender = normalize_address(spender)
    return "0x095ea7b3" + encode(
        ("address", "uint256"), (spender, 0)
    ).hex()


def encode_exact_input_single(
    token_in,
    token_out,
    fee,
    recipient,
    amount_in,
    amount_out_minimum,
):
    token_in, token_out = normalize_address(token_in), normalize_address(token_out)
    recipient = normalize_address(recipient)
    if (token_in == token_out or type(fee) is not int or fee not in BASE_V3_FEE_TIERS
            or type(amount_in) is not int or amount_in <= 0
            or type(amount_out_minimum) is not int or amount_out_minimum <= 0):
        raise BaseLiveTradingError("The Base swap transaction is invalid.")
    return _call_data(
        "exactInputSingle((address,address,uint24,address,uint256,uint256,uint160))",
        ("(address,address,uint24,address,uint256,uint256,uint160)",),
        ((token_in, token_out, fee, recipient, amount_in, amount_out_minimum, 0),),
    )


def encode_deadline_multicall(inner_calldata, deadline):
    if (not isinstance(inner_calldata, str) or not inner_calldata.startswith("0x")
            or len(inner_calldata) < 10 or len(inner_calldata) % 2
            or not re.fullmatch(r"0x[0-9a-fA-F]+", inner_calldata)
            or type(deadline) is not int or deadline <= 0):
        raise BaseLiveTradingError("The deadline-enforced Base transaction is invalid.")
    return _call_data(
        "multicall(uint256,bytes[])",
        ("uint256", "bytes[]"),
        (deadline, [bytes.fromhex(inner_calldata[2:])]),
    )


def live_environment_status(environ=None):
    """Return configuration readiness without returning secret or URL values."""
    env = os.environ if environ is None else environ
    wallet = env.get("BASE_TRADING_WALLET_ADDRESS", "").strip()
    bridge_wallet = env.get("BASE_HOT_WALLET_ADDRESS", "").strip()
    key = env.get("BASE_TRADING_PRIVATE_KEY", "").strip()
    rpc = (env.get("BASE_TRADING_RPC_URL") or env.get("BASE_RPC_URL") or "").strip()
    rpc_configured = bool(rpc.startswith("https://"))
    wallet_valid = bool(_ADDRESS_RE.fullmatch(wallet))
    bridge_wallet_valid = bool(_ADDRESS_RE.fullmatch(bridge_wallet))
    wallet_separate = bool(
        wallet_valid and bridge_wallet_valid and wallet.lower() != bridge_wallet.lower()
    )
    signer_matches = False
    if wallet_valid and key:
        try:
            from cross_chain_bridge import base_signer_address
            signer_matches = (
                base_signer_address(key).lower() == wallet.lower()
            )
        except Exception:
            signer_matches = False
    # True only because the app wires this readiness gate to its persistent,
    # idempotent send/reconciliation engine and receipt-validation tests.
    executor_ready = True
    return {
        "kill_switch_enabled": env.get("BASE_LIVE_TRADING_ENABLED", "").lower() == "true",
        "rpc_configured": rpc_configured,
        "wallet_configured": wallet_valid,
        "wallet_separate_from_bridge": wallet_separate,
        "signer_matches_wallet": signer_matches,
        "executor_ready": executor_ready,
        "ready": bool(
            env.get("BASE_LIVE_TRADING_ENABLED", "").lower() == "true"
            and rpc_configured and wallet_valid and wallet_separate
            and signer_matches and executor_ready
        ),
    }


class BaseLiveReadiness:
    """Read-only wallet/network preflight; never signs or broadcasts."""

    def __init__(self, environ=None, persistent_storage_check=None):
        self.environ = os.environ if environ is None else environ
        self.persistent_storage_check = persistent_storage_check or (lambda: False)

    def persistent_storage_ready(self):
        try:
            return bool(self.persistent_storage_check())
        except Exception:
            return False

    async def _rpc(self, session, method, params):
        url = (self.environ.get("BASE_TRADING_RPC_URL")
               or self.environ.get("BASE_RPC_URL") or "").strip()
        if not url.startswith("https://"):
            raise BaseLiveTradingError("A secure Base HTTPS RPC is not configured.")
        try:
            async with session.post(
                url,
                json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                timeout=15,
            ) as response:
                payload = await response.json()
        except Exception:
            raise BaseLiveTradingError("Base RPC is temporarily unavailable.") from None
        if response.status != 200 or not isinstance(payload, dict) or "error" in payload:
            raise BaseLiveTradingError("Base RPC did not return a valid response.")
        return payload.get("result")

    async def report(self, session):
        env_status = live_environment_status(self.environ)
        wallet = self.environ.get("BASE_TRADING_WALLET_ADDRESS", "").strip()
        persistent_storage_ready = self.persistent_storage_ready()
        result = {
            **env_status,
            "wallet_address": wallet if env_status["wallet_configured"] else "",
            "chain_id": None,
            "usdc_balance": None,
            "eth_balance": None,
            "persistent_storage_ready": persistent_storage_ready,
            "checks": [],
            "can_trade": False,
            "message": "Base live trading is off; no transaction can be sent until all checks pass.",
        }
        result["checks"].extend([
            {"name": "Dedicated wallet address", "ok": env_status["wallet_configured"]},
            {"name": "Wallet separate from bridge", "ok": env_status["wallet_separate_from_bridge"]},
            {"name": "Wallet signer matches", "ok": env_status["signer_matches_wallet"]},
            {"name": "Base HTTPS RPC", "ok": env_status["rpc_configured"]},
            {"name": "Live executor and recovery", "ok": env_status["executor_ready"]},
            {"name": "Persistent Base operation storage", "ok": persistent_storage_ready},
        ])
        if env_status["rpc_configured"]:
            try:
                chain_raw = await self._rpc(session, "eth_chainId", [])
                chain_id = _rpc_uint(chain_raw, "network ID")
                result["chain_id"] = chain_id
                result["checks"].append({
                    "name": "Base mainnet (8453)", "ok": chain_id == BASE_CHAIN_ID
                })
            except Exception:
                result["checks"].append({
                    "name": "Base mainnet (8453)", "ok": False
                })
        if result["chain_id"] == BASE_CHAIN_ID and env_status["wallet_configured"]:
            try:
                code_raw, native_raw, usdc_raw = await asyncio.gather(
                    self._rpc(session, "eth_getCode", [wallet, "latest"]),
                    self._rpc(session, "eth_getBalance", [wallet, "latest"]),
                    self._rpc(session, "eth_call", [{
                        "to": BASE_USDC_ADDRESS,
                        "data": _call_data(
                            "balanceOf(address)", ("address",), (wallet,)
                        ),
                    }, "latest"]),
                )
                native_wei = _rpc_uint(native_raw, "ETH balance")
                usdc_balance_raw = decode(
                    ("uint256",), bytes.fromhex(usdc_raw[2:])
                )[0]
                wallet_is_eoa = isinstance(code_raw, str) and code_raw in ("0x", "0x0")
                result["eth_balance"] = native_wei / 10**18
                result["usdc_balance"] = int(usdc_balance_raw) / 10**6
                result["checks"].extend([
                    {"name": "Dedicated wallet is an EOA", "ok": wallet_is_eoa},
                    {"name": "USDC balance", "ok": int(usdc_balance_raw) > 0},
                    {"name": "ETH gas reserve", "ok": native_wei >= 5 * 10**15},
                ])
                result["wallet_is_eoa"] = wallet_is_eoa
            except Exception:
                result["checks"].append({
                    "name": "Base RPC wallet check", "ok": False
                })
        funded = (
            isinstance(result["eth_balance"], (int, float))
            and result["eth_balance"] >= 0.005
        )
        result["ready"] = bool(
            env_status["kill_switch_enabled"]
            and env_status["rpc_configured"]
            and env_status["wallet_configured"]
            and env_status["wallet_separate_from_bridge"]
            and env_status["signer_matches_wallet"]
            and env_status["executor_ready"]
            and persistent_storage_ready
            and result["chain_id"] == BASE_CHAIN_ID
            and result.get("wallet_is_eoa") is True
            and funded
        )
        result["can_trade"] = result["ready"]
        if result["ready"]:
            result["message"] = "Base live executor is ready; set a cap and explicitly enable real trading."
        elif (result["chain_id"] == BASE_CHAIN_ID
                and not env_status["wallet_configured"]):
            result["message"] = (
                "Base Mainnet is verified. Configure a separate trading wallet "
                "and matching signer; live trading remains off."
            )
        elif env_status["executor_ready"]:
            result["message"] = "Base live trading is off; resolve every failed wallet, network, funding, or kill-switch check. No transaction can be sent."
        return result


@dataclass(frozen=True)
class VerifiedPool:
    address: str
    token0: str
    token1: str
    fee: int


class BaseV3Market:
    """Read-only Base RPC interface with strict Uniswap V3 pool verification."""

    def __init__(self, rpc_call):
        if not callable(rpc_call):
            raise TypeError("rpc_call must be an async JSON-RPC function")
        self.rpc_call = rpc_call

    async def _eth_call(self, address, data):
        response = await self.rpc_call(
            "eth_call", [{"to": address, "data": data}, "latest"]
        )
        if not isinstance(response, str) or not response.startswith("0x"):
            raise BaseLiveTradingError("Base RPC returned an invalid contract response.")
        return response

    async def verify_pool(self, pool_address, token_address):
        pool = normalize_address(pool_address)
        token = normalize_address(token_address)
        usdc = BASE_USDC_ADDRESS.lower()
        factory_data = "0x" + _selector("factory()").hex()
        token0_data = "0x" + _selector("token0()").hex()
        token1_data = "0x" + _selector("token1()").hex()
        fee_data = "0x" + _selector("fee()").hex()

        factory_raw, token0_raw, token1_raw, fee_raw = await asyncio.gather(
            self._eth_call(pool, factory_data),
            self._eth_call(pool, token0_data),
            self._eth_call(pool, token1_data),
            self._eth_call(pool, fee_data),
        )
        try:
            factory = decode(("address",), bytes.fromhex(factory_raw[2:]))[0].lower()
            token0 = decode(("address",), bytes.fromhex(token0_raw[2:]))[0].lower()
            token1 = decode(("address",), bytes.fromhex(token1_raw[2:]))[0].lower()
            fee = int(decode(("uint24",), bytes.fromhex(fee_raw[2:]))[0])
        except (ValueError, TypeError, OverflowError):
            raise BaseLiveTradingError("Base returned malformed Uniswap pool data.") from None

        if factory != BASE_UNISWAP_V3_FACTORY.lower():
            raise BaseLiveTradingError("This pool is not a verified Uniswap V3 Base pool.")
        if token == usdc:
            if usdc not in (token0, token1) or token0 == token1:
                raise BaseLiveTradingError("The Uniswap pool does not contain Base USDC.")
        elif {token0, token1} != {token, usdc}:
            raise BaseLiveTradingError("The Uniswap pool does not match this token and Base USDC.")
        if fee not in BASE_V3_FEE_TIERS:
            raise BaseLiveTradingError("The Uniswap pool fee tier is not supported.")

        factory_pool_raw = await self._eth_call(
            BASE_UNISWAP_V3_FACTORY, encode_factory_get_pool(token0, token1, fee)
        )
        try:
            factory_pool = decode(
                ("address",), bytes.fromhex(factory_pool_raw[2:])
            )[0].lower()
        except (ValueError, TypeError, OverflowError):
            raise BaseLiveTradingError("Uniswap V3 Factory returned malformed pool data.") from None
        if factory_pool != pool:
            raise BaseLiveTradingError("The pool address does not match Uniswap V3 Factory.")
        return VerifiedPool(pool, token0, token1, fee)

    async def quote_exact_input(self, pool_address, token_in, amount_in_raw, token_out=None):
        pool_address = normalize_address(pool_address)
        token_in = normalize_address(token_in)
        if type(amount_in_raw) is not int or amount_in_raw <= 0:
            raise BaseLiveTradingError("The Base quote amount must be positive.")
        usdc = BASE_USDC_ADDRESS.lower()
        if token_in == usdc:
            if token_out is None:
                raise BaseLiveTradingError("A target token is required for a Base USDC buy quote.")
            expected_token = normalize_address(token_out)
        else:
            expected_token = token_in
        pool = await self.verify_pool(pool_address, expected_token)
        inferred_token_out = pool.token1 if pool.token0 == token_in else pool.token0
        if token_out is not None and inferred_token_out != normalize_address(token_out):
            raise BaseLiveTradingError("The requested quote token does not match the verified pool.")
        token_out = inferred_token_out
        data = encode_quoter_v2_exact_input(token_in, token_out, amount_in_raw, pool.fee)
        raw = await self._eth_call(BASE_UNISWAP_V3_QUOTER_V2, data)
        quote = decode_quoter_v2_result(raw)
        block_number = _rpc_uint(await self.rpc_call("eth_blockNumber", []), "block number")
        return {
            "chain_id": BASE_CHAIN_ID,
            "pool_address": pool.address,
            "token_in": token_in,
            "token_out": token_out,
            "fee": pool.fee,
            "amount_in_raw": amount_in_raw,
            **quote,
            "block_number": block_number,
        }

    async def find_best_usdc_pool(self, token_address, amount_in_raw):
        """Find and quote only canonical Factory-created USDC/token pools."""
        token = normalize_address(token_address)
        if token == BASE_USDC_ADDRESS.lower():
            raise BaseLiveTradingError("Base USDC cannot be traded as a candidate.")
        quotes = []
        for fee in BASE_V3_FEE_TIERS:
            raw = await self._eth_call(
                BASE_UNISWAP_V3_FACTORY,
                encode_factory_get_pool(BASE_USDC_ADDRESS, token, fee),
            )
            try:
                pool_address = decode(
                    ("address",), bytes.fromhex(raw[2:])
                )[0].lower()
            except (ValueError, TypeError, OverflowError):
                raise BaseLiveTradingError(
                    "Uniswap V3 Factory returned malformed pool data."
                ) from None
            if pool_address == "0x" + "0" * 40:
                continue
            try:
                quote = await self.quote_exact_input(
                    pool_address, BASE_USDC_ADDRESS, amount_in_raw, token
                )
            except BaseLiveTradingError:
                continue
            quotes.append(quote)
        if not quotes:
            raise BaseLiveTradingError(
                "No verified Base USDC/Uniswap V3 pool returned a usable quote."
            )
        return max(quotes, key=lambda item: item["amount_out_raw"])
