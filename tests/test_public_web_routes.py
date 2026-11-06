"""Smoke tests for the public browser terminal routes."""
import asyncio
from pathlib import Path
import sys
import unittest
from types import SimpleNamespace

from aiohttp import web
from aiohttp.test_utils import make_mocked_request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main


class PublicWebRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_root_and_terminal_routes_resolve(self):
        app = web.Application()
        main.register_public_web_routes(app)

        for path in ("/", "/terminal", "/web-terminal.js", "/dashboard.js"):
            with self.subTest(path=path):
                match = await app.router.resolve(make_mocked_request("GET", path, app=app))
                self.assertNotIsInstance(match, web.HTTPNotFound)

    async def test_terminal_handler_returns_html_with_web_bootstrap(self):
        response = await main.index_handler(SimpleNamespace(path="/terminal"))
        body = response.text
        self.assertEqual(response.status, 200)
        self.assertEqual(response.content_type, "text/html")
        self.assertIn("window.ZRL_WEB=true", body)
        self.assertIn('/web-terminal.js', body)
        self.assertIn("<!DOCTYPE html>", body)

    async def test_root_handler_returns_html_without_terminal_bootstrap(self):
        response = await main.index_handler(SimpleNamespace(path="/"))
        self.assertEqual(response.status, 200)
        self.assertEqual(response.content_type, "text/html")
        self.assertIn("<!DOCTYPE html>", response.text)
        self.assertNotIn("window.ZRL_WEB=true", response.text)


if __name__ == "__main__":
    unittest.main()
