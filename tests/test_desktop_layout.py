import ast
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "main.py"


def embedded_html():
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    assignment = next(
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "RAW_HTML" for target in node.targets)
    )
    return ast.literal_eval(assignment.value)


def test_desktop_navigation_and_workspace_are_responsive_without_replacing_mobile():
    html = embedded_html()
    desktop = re.search(
        r"@media\s*\(min-width:\s*1024px\)\s*\{([\s\S]*?)\n        \}\n    </style>",
        html,
    )
    assert desktop, "A desktop-only adaptation should leave the existing mobile rules intact."
    css = desktop.group(1)
    for rule in (
        "#nav.bottom-nav",
        "flex-direction: column",
        "#app .tab-panel.active",
        "grid-template-columns: minmax(0, 1fr) minmax(0, 1fr)",
        "grid-auto-rows: max-content",
        "#app #tab-trading #trading-ai-activity",
        "#app #tab-trading #trading-strategy",
        "margin: 0 0 0 226px",
    ):
        assert rule in css
    assert ":nth-child(" not in css


def test_existing_tabs_and_auth_visibility_are_preserved():
    html = embedded_html()
    for tab_id in ("terminal", "trading", "deposit", "settings", "tariffs"):
        assert f'id="tab-{tab_id}"' in html
    for label in ("Overview", "Trading", "Wallet", "Settings", "Tariffs"):
        assert f">{label}</button>" in html
    assert 'id="nav" class="bottom-nav" style="display: none;"' in html
    assert '<div id="app" data-ui-version="console-ai-v1" style="display: none;">' in html
    assert re.search(r"\.tab-panel\s*\{\s*display:\s*none", html)
    assert "function gotoTab(id, btn)" in html
    assert "function openTerminal()" in html
