"""Certificate dependency and health reporting checks without starting the bot."""
import ast
import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest

import certifi
from aiohttp import web


ROOT = Path(__file__).resolve().parents[1]


class CertificateUpdateTests(unittest.IsolatedAsyncioTestCase):
    def test_patched_certificate_bundle_is_required_and_installed(self):
        requirements = ROOT.joinpath("requirements.txt").read_text().splitlines()
        self.assertIn("certifi>=2026.10.23", requirements)
        self.assertGreaterEqual(
            tuple(map(int, certifi.__version__.split("."))), (2026, 10, 19))

    async def test_health_reports_actual_bundle_without_changing_trading(self):
        source = ast.parse(ROOT.joinpath("main.py").read_text())
        handler = next(node for node in source.body
                       if isinstance(node, ast.AsyncFunctionDef)
                       and node.name == "health_handler")
        namespace = {"certifi": certifi, "os": os, "web": web}
        exec(compile(ast.Module(body=[handler], type_ignores=[]),
                     str(ROOT / "main.py"), "exec"), namespace)
        for active in (False, True):
            with self.subTest(active=active):
                runtime = SimpleNamespace(
                    readiness=lambda: {"wallet_configured": True},
                    ledger=SimpleNamespace(active_users=lambda: [1] if active else []))
                request = SimpleNamespace(app={"wallet_runtime": runtime})
                response = await namespace["health_handler"](request)
                result = json.loads(response.text)
                self.assertEqual(result["certifi_version"], certifi.__version__)
                self.assertEqual(result["trading_active"], active)
                self.assertEqual(result["status"], "custody_beta")
