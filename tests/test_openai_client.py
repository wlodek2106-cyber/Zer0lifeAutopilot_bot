import os
import unittest
from unittest.mock import patch

import openai_client


class FakeResponse:
    def __init__(self, status, body, headers=None):
        self.status = status
        self.body = body
        self.headers = headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def json(self):
        return self.body


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.calls = 0
        self.kwargs = None

    def post(self, url, **kwargs):
        self.calls += 1
        self.kwargs = kwargs
        return self.response


class OpenAIClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.paid_ai_patch = patch.object(
            openai_client, "PAID_AI_ENABLED", True)
        self.paid_ai_patch.start()
        self.addCleanup(self.paid_ai_patch.stop)
        self.gate = openai_client.scheduler()
        self.gate.min_interval = 0
        self.gate.next_request_at = 0
        self.gate.cooldown_until = 0
        self.gate.failures = 0
        self.gate.reason = ""

    async def test_disabled_paid_ai_never_sends_a_request(self):
        session = FakeSession(FakeResponse(200, {"choices": []}))
        with patch.object(openai_client, "PAID_AI_ENABLED", False), \
                patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}):
            with self.assertRaisesRegex(
                    openai_client.OpenAIUnavailable, "no OpenAI request was sent"):
                await openai_client.chat_completion(
                    session, {"model": "test"}, 1)
        self.assertEqual(session.calls, 0)

    async def test_rate_limit_honors_retry_after_and_shares_cooldown(self):
        first = FakeSession(FakeResponse(429,
            {"error": {"code": "rate_limit_exceeded"}},
            {"Retry-After": "120"}))
        second = FakeSession(FakeResponse(200, {"choices": []}))
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}), \
                patch.object(openai_client, "MIN_REQUEST_INTERVAL_SECONDS", 0):
            with self.assertRaises(openai_client.OpenAIUnavailable) as raised:
                await openai_client.chat_completion(first, {"model": "test"}, 1)
            self.assertGreaterEqual(raised.exception.retry_after_seconds, 120)
            self.assertIn("rate-limited", str(raised.exception))

            with self.assertRaises(openai_client.OpenAIUnavailable) as blocked:
                await openai_client.chat_completion(second, {"model": "test"}, 1)
        self.assertGreater(blocked.exception.retry_after_seconds, 0)
        self.assertEqual(first.calls, 1)
        self.assertEqual(second.calls, 0)

    async def test_quota_error_is_not_retried_as_a_transient_rate_limit(self):
        first = FakeSession(FakeResponse(429,
            {"error": {"code": "insufficient_quota"}}))
        second = FakeSession(FakeResponse(200, {"choices": []}))
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}), \
                patch.object(openai_client, "MIN_REQUEST_INTERVAL_SECONDS", 0):
            with self.assertRaises(openai_client.OpenAIUnavailable) as raised:
                await openai_client.chat_completion(first, {"model": "test"}, 1)
            self.assertIn("quota/billing limit", str(raised.exception))
            self.assertIn("billing", str(raised.exception))

            with self.assertRaises(openai_client.OpenAIUnavailable) as blocked:
                await openai_client.chat_completion(second, {"model": "test"}, 1)
        self.assertIn("quota/billing limit", str(blocked.exception))
        self.assertEqual(first.calls, 1)
        self.assertEqual(second.calls, 0)

    async def test_key_is_never_part_of_request_payload(self):
        session = FakeSession(FakeResponse(200, {"choices": [{"finish_reason": "stop"}]}))
        with patch.dict(os.environ, {"OPENAI_API_KEY": "not-real-test-key"}), \
                patch.object(openai_client, "MIN_REQUEST_INTERVAL_SECONDS", 0):
            await openai_client.chat_completion(session, {"model": "test"}, 1)
        self.assertNotIn("not-real-test-key", repr(session.kwargs["json"]))
        self.assertEqual(session.kwargs["headers"]["Authorization"], "Bearer not-real-test-key")
