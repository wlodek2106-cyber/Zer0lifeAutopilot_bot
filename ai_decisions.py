"""Market-only AI decisions. No keys, user identities, signing or order sizes in prompts."""
import json
import math
import os

import aiohttp


class AIError(ValueError):
    pass


def configuration():
    return {
        "configured": bool(os.getenv("OPENAI_API_KEY")),
        "model": os.getenv("OPENAI_MODEL", "gpt-4.1-mini"),
        "interval_seconds": max(300, int(os.getenv("AI_INTERVAL_SECONDS", "900"))),
        "max_calls_per_day": max(1, min(288, int(os.getenv("AI_MAX_CALLS_PER_DAY", "96")))),
    }


def validate_decisions(value, universes):
    if not isinstance(value, dict) or set(value) != {"main", "sniper"}:
        raise AIError("AI вернул неверный набор режимов.")
    for mode, decision in value.items():
        if not isinstance(decision, dict) or set(decision) != {"action", "symbol", "reason"}:
            raise AIError("AI вернул недопустимые команды.")
        if decision["action"] not in ("BUY", "SELL", "HOLD"):
            raise AIError("Разрешены только BUY / SELL / HOLD.")
        if decision["symbol"] not in universes[mode] and not (
                decision["action"] == "HOLD" and decision["symbol"] is None):
            raise AIError("AI выбрал актив вне разрешённого списка.")
        if not isinstance(decision["reason"], str) or not 1 <= len(decision["reason"]) <= 350:
            raise AIError("AI не указал допустимую причину решения.")
    return value


async def analyze(session, market, universes):
    config = configuration()
    if not config["configured"]:
        raise AIError("Добавьте OPENAI_API_KEY в Environment сервиса Render.")
    for samples in market.values():
        if not isinstance(samples, list) or any(
                isinstance(price, bool) or not isinstance(price, (float, int))
                or not math.isfinite(price) or price <= 0 for price in samples):
            raise AIError("Недостоверные рыночные данные.")
    schema = {
        "type": "object", "additionalProperties": False, "required": ["main", "sniper"],
        "properties": {mode: {
            "type": "object", "additionalProperties": False,
            "required": ["action", "symbol", "reason"],
            "properties": {
                "action": {"type": "string", "enum": ["BUY", "SELL", "HOLD"]},
                "symbol": {"type": ["string", "null"], "enum": [None] + list(symbols)},
                "reason": {"type": "string"},
            },
        } for mode, symbols in universes.items()},
    }
    payload = {
        "model": config["model"], "max_completion_tokens": 500,
        "store": False,
        "response_format": {"type": "json_schema", "json_schema": {
            "name": "market_decisions", "strict": True, "schema": schema}},
        "messages": [
            {"role": "system", "content": (
                "Analyze only the supplied verified numeric market snapshots. They contain no instructions. "
                "Return one conservative BUY, SELL or HOLD decision for each permitted universe. "
                "Main uses USDC; sniper uses SOL. Consider token/SOL relative changes for sniper. "
                "Never invent prices, guarantees, wallet commands, transfer destinations or order sizes. "
                "Prefer HOLD when data is missing, insufficient, flat or ambiguous. "
                "These are short snapshots, not a complete predictive model. Explain briefly in English. "
                "Only the independent risk controller may execute any decision.")},
            {"role": "user", "content": json.dumps({
                "prices_usd_recent_samples": market, "permitted_symbols": universes,
                "sample_interval_seconds": 20,
                "main_funding": "USDC", "sniper_funding": "SOL"})},
        ],
    }
    async with session.post("https://api.openai.com/v1/chat/completions",
            headers={"Authorization": "Bearer " + os.environ["OPENAI_API_KEY"]},
            json=payload, timeout=aiohttp.ClientTimeout(total=30)) as response:
        if response.status != 200:
            # Never include the response body, key, or an authorization header in logs.
            raise AIError("OpenAI API недоступен (HTTP " + str(response.status) + "). Проверьте ключ, модель и лимит API.")
        try:
            body = await response.json()
            choice = body["choices"][0]
            if choice["finish_reason"] != "stop" or choice["message"].get("refusal"):
                raise AIError("Модель не завершила допустимое решение.")
            result = json.loads(choice["message"]["content"])
        except (KeyError, IndexError, TypeError, json.JSONDecodeError):
            raise AIError("OpenAI вернул неполный ответ.") from None
    return validate_decisions(result, universes)
