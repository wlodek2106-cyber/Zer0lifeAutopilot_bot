"""Loopback-only browser fixture: exact UI source + real paper control/tick code.

No real authentication bypass is added to the application. This process has no
custody ledger, signer, RPC client, live AI client or transfer routes.
"""
import ast
import re
import sys
import tempfile
import time
import uuid
from pathlib import Path
from aiohttp import web

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT/"tests"))
from custody_runtime import CustodyRuntime
from paper_core import PaperCore
from wallet_runtime import WalletError
from test_paper_core import Market, WATCH


def page():
    tree = ast.parse((ROOT/"main.py").read_text())
    html = next(ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "RAW_HTML" for t in n.targets))
    style = "\n".join(re.findall(r"<style[^>]*>(.*?)</style>", html, re.S))
    start = html.index('<div class="card">\n                <h3>Virtual Core')
    end = html.index('<div id="trading-sniper">', start)
    card = html[start:end]
    js_start = html.index("let paperReport = null")
    js_end = html.index("async function refresh() {", js_start)
    return ('<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<style>'+style+'</style></head><body><main style="max-width:600px;margin:0 auto;padding:16px">'
            +card+'</main><script>const apiFetch=fetch;'+html[js_start:js_end]+
            'refreshPaper();setInterval(refreshPaper,1000);</script></body></html>')


def application(directory):
    runtime = object.__new__(CustodyRuntime)
    runtime.paper = PaperCore(directory+"/paper.sqlite3", WATCH, lambda: ["SOL", "JTO"])
    runtime.uid = lambda request: 101  # Fixture identity only; no real app auth is run.
    app = web.Application()

    async def index(request):
        return web.Response(text=page(), content_type="text/html")

    async def control(request):
        try:
            return await runtime.paper_control(request, None)
        except WalletError as exc:
            return web.json_response({"success": False, "message": str(exc)}, status=400)

    async def status(request):
        return await runtime.paper_status(request, None)

    async def tick(request):
        action = (await request.json()).get("action", "BUY")
        market = Market()
        market.proceeds = 30_900_000 if action == "SELL" else 29_940_000
        snapshot = {"id": uuid.uuid4().hex, "created_at": time.time(),
            "prices": {"JTO": 1.}, "samples": {"JTO": 5}, "decisions": {
                "main": {"action": action, "symbol": "JTO", "reason": "Offline browser fixture"}}}
        await runtime.paper.tick(101, market, snapshot, {"JTO": 1.})
        return await status(request)

    app.router.add_get("/", index)
    app.router.add_get("/api/paper_core", status)
    app.router.add_post("/api/paper_core", control)
    app.router.add_post("/test/tick", tick)
    return app


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="paper-browser-") as directory:
        web.run_app(application(directory), host="127.0.0.1", port=8766)
