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

        for path in ("/", "/terminal", "/terminal/", "/web-terminal.js", "/dashboard.js"):
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

    async def test_terminal_trailing_slash_returns_html_with_web_bootstrap(self):
        response = await main.index_handler(SimpleNamespace(path="/terminal/"))
        self.assertEqual(response.status, 200)
        self.assertEqual(response.content_type, "text/html")
        self.assertIn("window.ZRL_WEB=true", response.text)
        self.assertIn("/web-terminal.js", response.text)


    async def test_telegram_identity_precedes_browser_fallback_on_terminal(self):
        source = main.RAW_HTML
        user_start = source.index("function getUserData()")
        user_end = source.index("async function apiFetch(", user_start)
        user_flow = source[user_start:user_end]
        self.assertLess(user_flow.index("if (tg?.initData && telegramUser?.id)"),
                        user_flow.index("if(window.ZRL_WEB)"))

        api_start = source.index("async function apiFetch(")
        api_end = source.index("function openSupport(", api_start)
        api_flow = source[api_start:api_end]
        self.assertLess(api_flow.index("if (tg?.initData)"),
                        api_flow.index("if(window.ZRL_WEB)"))
        self.assertIn("'X-Telegram-Init-Data': tg.initData", api_flow)

    async def test_tariffs_remain_available_inside_telegram_on_terminal_route(self):
        source = main.RAW_HTML
        self.assertIn(
            "if((window.ZRL_WEB&&!tg?.initData)||telegramTariffBusy||!getUserData())return;",
            source,
        )
        self.assertIn(
            "if((window.ZRL_WEB&&!tg?.initData)||!telegramTariffState)return;",
            source,
        )
        self.assertIn(
            "if(id==='tariffs'&&(!window.ZRL_WEB||tg?.initData)){",
            source,
        )
        self.assertIn(
            "if(!window.ZRL_WEB||tg?.initData)refreshTelegramTariffs();",
            source,
        )


    async def test_institutional_dashboard_uses_real_paper_journal_metrics(self):
        dashboard = (Path(__file__).resolve().parents[1] / "static" / "dashboard.js").read_text()
        self.assertIn("renderClosedTrades(data.fills||[])", dashboard)
        self.assertIn("function renderClosedTrades(fills)", dashboard)
        self.assertIn('String(f.side).toUpperCase()==="SELL"', dashboard)
        self.assertIn('id="zrl-win-rate"', dashboard)
        self.assertIn('id="zrl-profit-factor"', dashboard)
        self.assertIn('id="zrl-agent-question"', dashboard)
        self.assertIn('loadBacktestHistory();loadPaperJournal();', dashboard)
        self.assertIn("window.zrlDeskContext", dashboard)

    async def test_root_handler_opens_terminal_with_web_bootstrap(self):
        response = await main.index_handler(SimpleNamespace(path="/"))
        self.assertEqual(response.status, 200)
        self.assertEqual(response.content_type, "text/html")
        self.assertIn("<!DOCTYPE html>", response.text)
        self.assertIn("window.ZRL_WEB=true", response.text)
        self.assertIn("/web-terminal.js", response.text)


if __name__ == "__main__":
    unittest.main()
