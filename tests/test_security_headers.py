import unittest
from types import SimpleNamespace

from aiohttp import web

import main


class SecurityHeadersTests(unittest.IsolatedAsyncioTestCase):
    async def test_api_responses_get_security_headers_and_are_not_cached(self):
        request = SimpleNamespace(path="/api/stats")

        async def handler(_request):
            return web.json_response({"private": "account data"})

        response = await main.security_headers_middleware(request, handler)

        self.assertEqual(response.headers["Strict-Transport-Security"], "max-age=31536000")
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self.assertEqual(response.headers["Referrer-Policy"], "no-referrer")
        self.assertEqual(response.headers["Permissions-Policy"],
                         "camera=(), microphone=(), geolocation=()")
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    async def test_health_error_response_gets_headers_and_is_not_cached(self):
        request = SimpleNamespace(path="/health")

        async def handler(_request):
            raise web.HTTPNotFound()

        response = await main.security_headers_middleware(request, handler)

        self.assertEqual(response.status, 404)
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    async def test_existing_content_security_policy_is_preserved(self):
        request = SimpleNamespace(path="/passkey")
        expected_csp = "default-src 'none'; frame-ancestors 'none'"

        async def handler(_request):
            return web.Response(headers={"Content-Security-Policy": expected_csp})

        response = await main.security_headers_middleware(request, handler)

        self.assertEqual(response.headers["Content-Security-Policy"], expected_csp)
        self.assertNotIn("Cache-Control", response.headers)
