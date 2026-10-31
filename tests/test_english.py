import ast
import json
from pathlib import Path
import re
import unittest

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from english_text import english_json_middleware, payload, translate


class EnglishTests(unittest.TestCase):
    def test_owned_backend_messages_are_covered(self):
        root = Path(__file__).resolve().parents[1]
        for name in ("custody_runtime.py", "custody_ledger.py", "wallet_runtime.py",
                     "legacy_retirement.py", "tx_deposits.py", "ai_decisions.py"):
            for node in ast.walk(ast.parse(root.joinpath(name).read_text())):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    self.assertIsNone(re.search("[А-Яа-яЁё]", translate(node.value)),
                                      (name, node.lineno, node.value))

    def test_interface_and_wallet_pages_have_no_russian_copy(self):
        root = Path(__file__).resolve().parents[1]
        source = ast.parse(root.joinpath("main.py").read_text())
        page = next(ast.literal_eval(node.value) for node in source.body if isinstance(node, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == "RAW_HTML" for t in node.targets))
        for value in (page, root.joinpath("wallet_link.html").read_text(),
                      root.joinpath("deposit_payment.html").read_text()):
            self.assertIsNone(re.search("[А-Яа-яЁё]", value))

    def test_exchange_is_informational_and_explicitly_disabled(self):
        root = Path(__file__).resolve().parents[1]
        source = ast.parse(root.joinpath("main.py").read_text())
        page = next(ast.literal_eval(node.value) for node in source.body if isinstance(node, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == "RAW_HTML" for t in node.targets))
        exchange = page.split('<div id="tab-exchange"', 1)[1].split('<div id="tab-deposit"', 1)[0]
        for required in (
                "Solana to Base", "NOT CONFIGURED", "No transfer can be started yet",
                "not configured", "your own available Solana USDC balance",
                "quote and provider or network costs will be shown before you manually confirm",
                "only after the destination-chain transfer reaches finality",
                "Shared wallet. Separate user balances.", "Telegram account UID",
                "not an external withdrawal", "Do not retry a transfer",
                'class="exchange-disabled" type="button" disabled'):
            self.assertIn(required, exchange)
        self.assertNotRegex(exchange, r"<(form|input|select)\b")
        self.assertNotIn("fetch(", exchange)

    def test_presentation_does_not_change_money_status_or_identity(self):
        original = {"message": "Резерв комиссий: 0.001 SOL; требуется не меньше 0.005"
                    " SOL. В Настройках → Резерв комиссий выделите только свои SOL. Пользовательские средства автоматически не расходуются.",
                    "balance": 34.037005, "raw": 164900029, "state": "SUBMITTED",
                    "username": "ВКЛ", "token_symbol": "ВКЛ", "signature": "example"}
        converted = payload(original)
        self.assertIn("Fee reserve: 0.001 SOL; required minimum: 0.005 SOL.", converted["message"])
        for key in ("balance", "raw", "state", "username", "token_symbol", "signature"):
            self.assertEqual(converted[key], original[key])
        self.assertTrue(original["message"].startswith("Резерв"))

    def test_old_model_reasons_are_not_fabricated_translations(self):
        result = payload({"action": "HOLD", "symbol": None, "reason": "Нет данных."})
        self.assertEqual(result["action"], "HOLD")
        self.assertIn("Previous-language", result["reason"])
        self.assertEqual(payload({"reason": "No market signal."})["reason"], "No market signal.")

    def test_model_instructed_to_explain_in_english(self):
        source = Path(__file__).resolve().parents[1].joinpath("ai_decisions.py").read_text()
        self.assertIn("Explain briefly in English.", source)
        self.assertNotIn("Explain briefly in Russian.", source)


class EnglishHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def test_error_status_headers_and_json_preserved(self):
        app = web.Application(middlewares=[english_json_middleware])

        async def handler(request):
            return web.json_response({"success": False, "message": "Только администратор.",
                                      "raw": 10000000}, status=403, headers={"Cache-Control": "no-store"})
        app.router.add_get("/test", handler)
        async with TestClient(TestServer(app)) as client:
            response = await client.get("/test")
            raw = await response.read()
            self.assertEqual(response.status, 403)
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            self.assertEqual(response.content_length, len(raw))
            self.assertEqual(json.loads(raw), {"success": False, "message": "Administrator only.", "raw": 10000000})

    async def test_no_translation_of_html_or_signed_transaction_data(self):
        app = web.Application(middlewares=[english_json_middleware])

        async def html(request):
            return web.Response(text="ВКЛ", content_type="text/html")

        async def data(request):
            return web.json_response({"transaction": "AQID+abc/==", "mint": "example", "raw": 5000000})
        app.router.add_get("/html", html)
        app.router.add_get("/data", data)
        async with TestClient(TestServer(app)) as client:
            self.assertEqual(await (await client.get("/html")).text(), "ВКЛ")
            self.assertEqual(await (await client.get("/data")).json(),
                             {"transaction": "AQID+abc/==", "mint": "example", "raw": 5000000})
