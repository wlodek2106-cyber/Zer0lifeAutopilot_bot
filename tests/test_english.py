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

    def test_interface_defaults_to_english_and_offers_three_languages(self):
        root = Path(__file__).resolve().parents[1]
        source = ast.parse(root.joinpath("main.py").read_text())
        page = next(ast.literal_eval(node.value) for node in source.body if isinstance(node, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == "RAW_HTML" for t in node.targets))
        self.assertIn('<html lang="en" data-ui-language="en"', page)
        self.assertIn('<option value="en">English</option>', page)
        self.assertIn('<option value="ru">Русский</option>', page)
        self.assertIn('<option value="uk">Українська</option>', page)
        self.assertIn("applyUiLanguage(data.ui_language||'en')", page)
        self.assertIn("/api/account/language", page)
        for value in (root.joinpath("wallet_link.html").read_text(),
                      root.joinpath("deposit_payment.html").read_text()):
            self.assertIsNone(re.search("[А-Яа-яЁё]", value))

    def test_long_trading_and_investor_descriptions_are_collapsible_and_localized(self):
        root = Path(__file__).resolve().parents[1]
        source = ast.parse(root.joinpath("main.py").read_text())
        page = next(ast.literal_eval(node.value) for node in source.body if isinstance(node, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == "RAW_HTML" for t in node.targets))
        self.assertEqual(page.count('<details class="trading-description-disclosure">'), 2)
        self.assertEqual(page.count('<details class="trading-analysis-disclosure">'), 3)
        self.assertIn('<details class="base-paper-fold solana-investor-description-disclosure">', page)
        self.assertIn("Exit rules &amp; risk", page)
        self.assertIn("Entry-cost details", page)
        for translation in (
                '["Exit rules & risk","Правила выхода и риски","Правила виходу та ризики"]',
                '["Entry-cost details","Расходы на вход","Витрати на вхід"]',
                ',"Автовыходы: стоп −5%; трейлинг −2% после +3,5%. Ручная продажа доступна.","Автовиходи: стоп −5%; трейл −2% після +3,5%. Ручний продаж доступний."',
                '["Scan timing & costs","Частота сканирования и расходы","Частота сканування та витрати"]',
                '["AI decisions & risk checks","Решения ИИ и проверки рисков","Рішення ШІ та перевірки ризиків"]',
                '["Outcome history & chart levels","История результатов и уровни графика","Історія результатів і рівні графіка"]',
                '["Candidate screening details","Как проверяются кандидаты","Як перевіряються кандидати"]',
                '["Candidates are screened before entry; not every candidate is traded.","Кандидаты проходят проверку перед входом; покупается не каждый.","Кандидати проходять перевірку перед входом; купується не кожен."]'):
            self.assertIn(translation, page)

    def test_exchange_requires_manual_confirmation_for_every_live_transfer(self):
        root = Path(__file__).resolve().parents[1]
        source = ast.parse(root.joinpath("main.py").read_text())
        page = next(ast.literal_eval(node.value) for node in source.body if isinstance(node, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == "RAW_HTML" for t in node.targets))
        exchange = page.split('<div id="tab-exchange"', 1)[1].split('<div id="tab-deposit"', 1)[0]
        for required in (
                "Solana → Base", "Base → Solana", "QUOTE · CONFIRM TO EXCHANGE",
                "Review the estimate and minimum receive. Press “Exchange” to confirm this transaction; the bot handles routing and status checks.",
                "Each transfer is confirmed separately. Your account is credited only after the destination transaction is verified as final.",
                'id="exchange-quote-button"', 'id="exchange-amount"'):
            self.assertIn(required, exchange)
        for required in ("function confirmExchangeTransfer(quote,button)", "/api/exchange/submit",
                         "provider_solana_deposit_address", "destination_address",
                         "The Solana transaction does not prove LI.FI will complete delivery"):
            self.assertIn(required, page)
        self.assertNotIn("Shared wallet. Separate user balances.", exchange)
        self.assertNotIn("Each user's in-app balances are tracked separately by account.", exchange)
        self.assertIn("const source = reverse ? 'Base' : 'Solana'", page)
        self.assertIn("const destination = reverse ? 'Solana' : 'Base'", page)
        self.assertNotRegex(exchange, r"<form\b")
        self.assertNotIn("exchange-submit", exchange)
        self.assertNotIn("exchange-approve", exchange)
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
