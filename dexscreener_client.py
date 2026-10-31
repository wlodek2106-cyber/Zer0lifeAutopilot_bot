"""One paced, priority-aware DexScreener transport per application event loop.

Only discovery hints may be cached by callers. This transport never caches a
price response, retries a request itself, or bypasses an upstream cooldown.
"""
import asyncio
import math
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

PRICE = 0
CONTEXT = 1
DISCOVERY = 2
ORIGIN = "https://api.dexscreener.com"
DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS = 300
MAX_RATE_LIMIT_COOLDOWN_SECONDS = 1800
HEALTHY_RESPONSES_TO_RESET_BACKOFF = 10


class ProviderLimited(ValueError):
    def __init__(self, retry_after):
        self.retry_after = retry_after
        super().__init__("DexScreener unavailable: HTTP 429")


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


class Scheduler:
    def __init__(self):
        self.condition = asyncio.Condition()
        self.clock = time.monotonic
        self.min_interval = 2.0
        self.next_request_at = 0
        self.cooldown_until = 0
        self.rate_limit_failures = 0
        self.healthy_responses = 0
        self.active = False
        self.waiters = []
        self.sequence = 0

    def cooldown_remaining(self):
        return max(0, math.ceil(self.cooldown_until - self.clock()))

    async def acquire(self, priority):
        async with self.condition:
            self.sequence += 1
            ticket = (priority, self.sequence)
            self.waiters.append(ticket)
            # Wake a sleeping background waiter so it rechecks priority.
            self.condition.notify_all()
            try:
                while True:
                    remaining = self.cooldown_remaining()
                    if remaining:
                        raise ProviderLimited(remaining)
                    delay = self.next_request_at - self.clock()
                    if not self.active and ticket == min(self.waiters):
                        if delay <= 0:
                            self.active = True
                            self.next_request_at = self.clock() + self.min_interval
                            return
                        try:
                            await asyncio.wait_for(self.condition.wait(), timeout=delay)
                        except asyncio.TimeoutError:
                            pass
                    else:
                        await self.condition.wait()
            finally:
                self.waiters.remove(ticket)
                self.condition.notify_all()

    async def limited(self, retry_after):
        async with self.condition:
            self.rate_limit_failures = min(6, self.rate_limit_failures + 1)
            self.healthy_responses = 0
            backoff = min(
                MAX_RATE_LIMIT_COOLDOWN_SECONDS,
                DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS * 2 ** (self.rate_limit_failures - 1))
            delay = max(backoff,
                        retry_after_seconds(retry_after))
            self.cooldown_until = max(self.cooldown_until, self.clock() + delay)
            self.condition.notify_all()

    async def release(self):
        async with self.condition:
            self.active = False
            self.condition.notify_all()


def scheduler():
    # The production bot has one event loop. Attach state to it, not to a
    # participant, session, discovery instance or import-time event loop.
    loop = asyncio.get_running_loop()
    state = getattr(loop, "_zrl_dexscreener_scheduler", None)
    if state is None:
        state = Scheduler()
        loop._zrl_dexscreener_scheduler = state
    return state


def cooldown_remaining():
    try:
        return scheduler().cooldown_remaining()
    except RuntimeError:
        return 0  # Status before the application loop starts; no requests yet.


@asynccontextmanager
async def request(session, path, *, priority=CONTEXT, **kwargs):
    parsed = urlsplit(path)
    if parsed.scheme or parsed.netloc:
        if parsed.scheme != "https" or parsed.netloc != "api.dexscreener.com":
            raise ValueError("Unexpected market-data origin.")
        url = path
    else:
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError("Invalid market-data path.")
        url = ORIGIN + path
    state = scheduler()
    # A caller cannot accumulate an unbounded queue during slow network I/O.
    acquired = False
    try:
        async with asyncio.timeout(20):
            await state.acquire(priority)
            acquired = True
        async with session.get(url, **kwargs) as response:
            if response.status == 429:
                headers = getattr(response, "headers", {}) or {}
                await state.limited(headers.get("Retry-After"))
                raise ProviderLimited(state.cooldown_remaining())
            if response.status == 200:
                state.healthy_responses += 1
                if state.healthy_responses >= HEALTHY_RESPONSES_TO_RESET_BACKOFF:
                    state.rate_limit_failures = 0
                    state.healthy_responses = 0
            yield response
    finally:
        if acquired:
            await state.release()
