"""Shared, conservative OpenAI request pacing and rate-limit handling."""
import asyncio
import math
import os
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import aiohttp


URL = "https://api.openai.com/v1/chat/completions"
PAID_AI_ENABLED = False
MIN_REQUEST_INTERVAL_SECONDS = 5
RATE_LIMIT_BACKOFF_SECONDS = 60
MAX_RATE_LIMIT_BACKOFF_SECONDS = 1800
QUOTA_COOLDOWN_SECONDS = 900


def retry_after_seconds(value):
    try:
        seconds = float(value)
        if math.isfinite(seconds):
            return max(0, seconds)
    except (ValueError, TypeError):
        pass
    try:
        date = parsedate_to_datetime(value)
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
        return max(0, (date - datetime.now(timezone.utc)).total_seconds())
    except (ValueError, TypeError, OverflowError, AttributeError):
        return 0


class OpenAIUnavailable(RuntimeError):
    def __init__(self, message, retry_after_seconds=0):
        self.retry_after_seconds = max(0, math.ceil(retry_after_seconds))
        super().__init__(message)


class _RequestGate:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.next_request_at = 0.0
        self.cooldown_until = 0.0
        self.failures = 0
        self.reason = ""
        self.min_interval = MIN_REQUEST_INTERVAL_SECONDS

    def remaining(self):
        return max(0, math.ceil(self.cooldown_until - time.monotonic()))


def scheduler():
    loop = asyncio.get_running_loop()
    gate = getattr(loop, "_zrl_openai_request_gate", None)
    if gate is None:
        gate = _RequestGate()
        loop._zrl_openai_request_gate = gate
    return gate


def _error_code(body):
    error = body.get("error") if isinstance(body, dict) else None
    if not isinstance(error, dict):
        return ""
    code = error.get("code") or error.get("type")
    return code.strip().lower() if isinstance(code, str) else ""


async def chat_completion(session, payload, timeout_seconds):
    if not PAID_AI_ENABLED:
        raise OpenAIUnavailable(
            "Paid AI is disabled; no OpenAI request was sent.")
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise OpenAIUnavailable("OpenAI API key is not configured.")

    gate = scheduler()
    async with gate.lock:
        remaining = gate.remaining()
        if remaining:
            raise OpenAIUnavailable(gate.reason, remaining)

        delay = gate.next_request_at - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
        remaining = gate.remaining()
        if remaining:
            raise OpenAIUnavailable(gate.reason, remaining)

        gate.next_request_at = time.monotonic() + gate.min_interval
        headers = {
            "Authorization": "Bearer " + api_key,
            "Accept": "application/json",
        }
        async with session.post(
            URL, headers=headers, json=payload,
            timeout=aiohttp.ClientTimeout(total=timeout_seconds),
        ) as response:
            if response.status == 429:
                try:
                    error_body = await response.json()
                except (aiohttp.ClientError, ValueError, TypeError):
                    error_body = {}
                code = _error_code(error_body)
                if code in {"insufficient_quota", "billing_hard_limit_reached"}:
                    delay = QUOTA_COOLDOWN_SECONDS
                    gate.reason = (
                        "OpenAI API quota/billing limit reached (HTTP 429). "
                        "Check OpenAI project usage or billing; new AI BUYs remain blocked."
                    )
                else:
                    gate.failures = min(gate.failures + 1, 6)
                    backoff = min(
                        MAX_RATE_LIMIT_BACKOFF_SECONDS,
                        RATE_LIMIT_BACKOFF_SECONDS * (2 ** (gate.failures - 1)),
                    )
                    retry_after = retry_after_seconds(
                        getattr(response, "headers", {}).get("Retry-After"))
                    delay = max(backoff, retry_after)
                    gate.reason = (
                        f"OpenAI rate-limited (HTTP 429). Shared AI requests paused for "
                        f"{math.ceil(delay)} seconds; new AI BUYs remain blocked."
                    )
                gate.cooldown_until = max(
                    gate.cooldown_until, time.monotonic() + delay)
                raise OpenAIUnavailable(gate.reason, delay)
            if response.status != 200:
                raise OpenAIUnavailable(
                    f"OpenAI API unavailable (HTTP {response.status}).")
            try:
                body = await response.json()
            except (aiohttp.ClientError, ValueError, TypeError):
                raise OpenAIUnavailable("OpenAI API returned invalid JSON.") from None

        gate.failures = 0
        gate.cooldown_until = 0.0
        gate.reason = ""
        return body
