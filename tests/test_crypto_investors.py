import ast
from html.parser import HTMLParser
from pathlib import Path
import unittest


class Elements(HTMLParser):
    def __init__(self):
        super().__init__()
        self.by_id = {}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "id" in attrs:
            self.by_id[attrs["id"]] = (tag, attrs)


class CryptoInvestorsTests(unittest.TestCase):
    def test_button_opens_separate_read_only_investor_dashboard(self):
        source = (Path(__file__).resolve().parents[1] / "main.py").read_text()
        tree = ast.parse(source)
        html = next(
            ast.literal_eval(node.value)
            for node in tree.body
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "RAW_HTML"
                    for target in node.targets)
        )
        elements = Elements()
        elements.feed(html)
        tag, button = elements.by_id["btn-ai-crypto-investors"]
        self.assertEqual(tag, "button")
        self.assertEqual(button["type"], "button")
        self.assertEqual(button["onclick"], "gotoTab('ai-crypto-investors', null)")
        _, panel = elements.by_id["tab-ai-crypto-investors"]
        self.assertEqual(panel["class"], "tab-panel")
        self.assertIn(">Ai Crypto Investors</button>", html)
        panel_content = html.split('id="tab-ai-crypto-investors"', 1)[1].split(
            '<div id="nav"', 1
        )[0]
        self.assertIn("Open investor positions", panel_content)
        self.assertIn("Closed trades and realized results", panel_content)
        self.assertIn("Three entry tranches only while fresh chart data confirms an upward trend", panel_content)
        self.assertIn("This strategy has no stop-loss", panel_content)
        self.assertIn("may lose most or all of its value", panel_content)
        self.assertIn("read-only setup preview", panel_content)
        self.assertIn("cannot submit buys or sales", panel_content)
