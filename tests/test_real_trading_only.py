"""The removed virtual product must not remain reachable or consume AI work."""
import ast
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class RealTradingOnlyTests(unittest.TestCase):
    def test_no_virtual_ui_polling_or_routes(self):
        source = (ROOT / "main.py").read_text()
        for removed in ("Virtual Core", "/api/paper_core", "controlPaper",
                        "refreshPaper", "renderPaper", 'id="paper-', "SOL HOLD"):
            self.assertNotIn(removed, source)
        for retained in ("trading-strategy", "trading-sniper",
                         "Independent execution guard", "Check readiness"):
            self.assertIn(retained, source)

    def test_runtime_does_not_load_or_schedule_virtual_modules(self):
        source = (ROOT / "custody_runtime.py").read_text()
        tree = ast.parse(source)
        for removed in ("self.paper", "paper_core", "paper_market", "paper_ready", "paper_users"):
            self.assertNotIn(removed, source)
        runtime = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "CustodyRuntime")
        names = {n.name for n in runtime.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        self.assertFalse(names & {"paper_status", "paper_control", "background_paper", "poll_paper"})
        self.assertTrue({"worker", "trade", "withdraw", "trading_readiness"} <= names)
        self.assertIn("self.ledger.restart_pause()", source)
        self.assertNotIn(".paper.sqlite3", source)


if __name__ == "__main__":
    unittest.main()
