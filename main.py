import asyncio
import aiohttp
from aiohttp import web
import sqlite3
import os
import logging
from datetime import datetime
import json
import base58
import base64
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction

logging.basicConfig(level=logging.INFO, format='%(asctime)s - [%(levelname)s] - %(message)s')

# --- КОНФИГУРАЦИЯ ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
PORT = int(os.getenv("PORT", 10000))
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL", "https://zer0lifeautopilot-bot.onrender.com").strip()

DB_DIR = "/var/data"
DB_FILE = os.path.join(DB_DIR, "zer0life_users.db") if os.path.exists(DB_DIR) else "zer0life_users.db"

SOLANA_RPC = os.getenv("SOLANA_RPC_URL", "https://mainnet.helius-rpc.com/?api-key=b82ca0bf-cd65-4ca8-a3d0-4400661d9a93").strip()
SPLASH_IMAGE_URL = "https://i.postimg.cc/52n94Pzg/57D47DDD-9BED-4D4B-AA69-2B59CAB400AD.png"

# Топ-5 проверенных активов с высокой ликвидностью
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
WATCHLIST = {
    "SOL":  {"mint": "So11111111111111111111111111111111111111112", "decimals": 9},
    "JUP":  {"mint": "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN", "decimals": 6},
    "RAY":  {"mint": "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R", "decimals": 6},
    "WIF":  {"mint": "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm", "decimals": 6},
    "PYTH": {"mint": "HZ1JovNiDcZvKhgWNgQX5Z59C3b22DoPtRUC544HLD37", "decimals": 6}
}

TRADE_AMOUNT_USDC = 20.0
TRADE_AMOUNT_RAW = int(TRADE_AMOUNT_USDC * 1_000_000)
TP_PCT = 0.035          # +3.5% Take Profit
SL_PCT = 0.020          # -2.0% Stop Loss
MAX_SLIPPAGE_BPS = 40   # 0.4% защита от сэндвич-атак

RECENT_LOGS = ["⚜️ Multi-Asset Engine v3.0 инициализирован."]

def add_log(msg: str):
    global RECENT_LOGS
    timestamp = datetime.now().strftime('%H:%M:%S')
    entry = f"[{timestamp}] {msg}"
    RECENT_LOGS.insert(0, entry)
    if len(RECENT_LOGS) > 50:
        RECENT_LOGS.pop()
    logging.info(msg)

def get_signer_keypair():
    pk_env = os.getenv("SOLANA_PRIVATE_KEY", "").strip()
    if not pk_env:
        add_log("❌ Ключ SOLANA_PRIVATE_KEY не задан в переменных.")
        return None
    try:
        pk_clean = pk_env.replace('"', '').replace("'", "").strip()
        if pk_clean.startswith("["):
            return Keypair.from_bytes(bytes(json.loads(pk_clean)))
        else:
            return Keypair.from_bytes(base58.b58decode(pk_clean))
    except Exception as e:
        add_log(f"❌ Ошибка ключа: {e}")
        return None

def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        telegram_id INTEGER PRIMARY KEY, username TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT, token_symbol TEXT, token_mint TEXT,
        buy_price REAL, amount_tokens REAL, sell_price REAL DEFAULT 0,
        profit_usdc REAL DEFAULT 0.0, buy_tx TEXT, sell_tx TEXT, timestamp TEXT)''')
    conn.commit()
    conn.close()

async def get_token_prices(session: aiohttp.ClientSession) -> dict:
    mints = ",".join([v["mint"] for v in WATCHLIST.values()])
    url = f"https://api.jup.ag/price/v2?ids={mints}"
    prices = {}
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=4)) as resp:
            if resp.status == 200:
                data = await resp.json()
                price_data = data.get("data", {})
                for sym, info in WATCHLIST.items():
                    p = price_data.get(info["mint"], {}).get("price")
                    if p:
                        prices[sym] = float(p)
    except Exception:
        pass
    return prices

async def execute_jupiter_swap(session: aiohttp.ClientSession, input_mint: str, output_mint: str, amount_raw: int, signer: Keypair) -> str:
    try:
        user_pubkey = str(signer.pubkey())
        headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
        quote_url = f"https://api.jup.ag/swap/v1/quote?inputMint={input_mint}&outputMint={output_mint}&amount={amount_raw}&slippageBps={MAX_SLIPPAGE_BPS}"
        
        async with session.get(quote_url, headers=headers, timeout=aiohttp.ClientTimeout(total=6)) as resp:
            if resp.status != 200:
                err = await resp.text()
                add_log(f"❌ Quote ({resp.status}): {err[:40]}")
                return ""
            quote_data = await resp.json()

        payload = {
            "quoteResponse": quote_data,
            "userPublicKey": user_pubkey,
            "wrapAndUnwrapSol": True,
            "prioritizationFeeLamports": 25000
        }
        async with session.post("https://api.jup.ag/swap/v1/swap", headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=7)) as resp:
            if resp.status != 200:
                err = await resp.text()
                add_log(f"❌ Swap API ({resp.status}): {err[:40]}")
                return ""
            res = await resp.json()
            swap_b64 = res.get("swapTransaction")

        if not swap_b64:
            return ""

        raw_bytes = base64.b64decode(swap_b64)
        tx = VersionedTransaction.from_bytes(raw_bytes)
        signed_tx = VersionedTransaction(tx.message, [signer])
        signed_b64 = base64.b64encode(bytes(signed_tx)).decode("utf-8")

        rpc_payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "sendTransaction",
            "params": [signed_b64, {"skipPreflight": True, "encoding": "base64"}]
        }
        async with session.post(SOLANA_RPC, json=rpc_payload, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            if resp.status == 200:
                data = await resp.json()
                sig = data.get("result")
                if sig:
                    return str(sig)
                else:
                    add_log(f"❌ RPC Err: {data.get('error')}")
    except Exception as e:
        add_log(f"⚠️ Ошибка свопа: {e}")
    return ""

async def real_trading_worker():
    add_log("🤖 Сканер Топ-5 активов запущен.")
    signer = get_signer_keypair()
    if not signer:
        return

    history = {sym: [] for sym in WATCHLIST}
    cooldown_until = 0

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                now_ts = datetime.now().timestamp()
                prices = await get_token_prices(session)
                if not prices:
                    await asyncio.sleep(15)
                    continue

                for sym, p in prices.items():
                    history[sym].append(p)
                    if len(history[sym]) > 30:
                        history[sym].pop(0)

                conn = sqlite3.connect(DB_FILE)
                c = conn.cursor()
                c.execute("SELECT id, token_symbol, token_mint, buy_price, amount_tokens FROM trades WHERE sell_price = 0 LIMIT 1")
                active_trade = c.fetchone()

                # 1. Мониторинг открытой позиции
                if active_trade:
                    trade_id, sym, mint, entry_px, amount_tokens = active_trade
                    cur_px = prices.get(sym, entry_px)
                    pnl_pct = (cur_px - entry_px) / entry_px
                    add_log(f"🎯 Позиция [{sym}]: ${cur_px:.4f} | PnL: {pnl_pct*100:+.2f}%")

                    if pnl_pct >= TP_PCT or pnl_pct <= -SL_PCT:
                        action = "Take Profit" if pnl_pct >= TP_PCT else "Stop Loss"
                        add_log(f"⚡ Закрытие позиции по {action} ({pnl_pct*100:+.2f}%). Продажа {sym} -> USDC...")
                        
                        raw_amount = int(amount_tokens * (10 ** WATCHLIST[sym]["decimals"]))
                        sell_sig = await execute_jupiter_swap(session, mint, USDC_MINT, raw_amount, signer)
                        
                        if sell_sig:
                            profit_usd = round(TRADE_AMOUNT_USDC * pnl_pct, 2)
                            c.execute("UPDATE trades SET sell_price = ?, profit_usdc = ?, sell_tx = ? WHERE id = ?", 
                                      (cur_px, profit_usd, sell_sig, trade_id))
                            conn.commit()
                            add_log(f"✅ Успешно! PnL: {profit_usd:+.2f} USDC | Tx: {sell_sig[:12]}...")
                            cooldown_until = now_ts + 900  # 15 минут паузы
                    conn.close()
                    await asyncio.sleep(20)
                    continue

                conn.close()

                # 2. Поиск точки входа при отсутствии позиций
                if now_ts < cooldown_until:
                    await asyncio.sleep(25)
                    continue

                best_candidate = None
                max_dip = 0.0

                for sym, p_list in history.items():
                    if len(p_list) >= 15:
                        avg_p = sum(p_list) / len(p_list)
                        cur_p = p_list[-1]
                        dip = (avg_p - cur_p) / avg_p
                        
                        # Сигнал: цена пролилась на 1.8%+ ниже средней и показывает отскок от локального дна
                        if dip >= 0.018 and cur_p > min(p_list[-4:]):
                            if dip > max_dip:
                                max_dip = dip
                                best_candidate = (sym, cur_p)

                if best_candidate:
                    sym, cur_p = best_candidate
                    mint = WATCHLIST[sym]["mint"]
                    add_log(f"🚀 Сигнал на отскок: {sym} просел на {max_dip*100:.2f}%. Покупка на $20 USDC...")
                    
                    buy_sig = await execute_jupiter_swap(session, USDC_MINT, mint, TRADE_AMOUNT_RAW, signer)
                    if buy_sig:
                        bought_tokens = TRADE_AMOUNT_USDC / cur_p
                        c_in = sqlite3.connect(DB_FILE)
                        c_in.cursor().execute("""INSERT INTO trades 
                            (token_symbol, token_mint, buy_price, amount_tokens, sell_price, profit_usdc, buy_tx, sell_tx, timestamp)
                            VALUES (?, ?, ?, ?, 0, 0, ?, '', ?)""",
                            (sym, mint, cur_p, bought_tokens, buy_sig, datetime.now().strftime('%H:%M:%S')))
                        c_in.commit()
                        c_in.close()
                        add_log(f"💎 Куплен {sym}! Tx: {buy_sig[:12]}...")

            except Exception as e:
                add_log(f"⚠️ Ошибка сканера: {e}")

            await asyncio.sleep(25)

# --- ИНТЕРФЕЙС И WEBHOOK ---
RAW_HTML = r"""<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no, viewport-fit=cover">
    <title>Zer0Life Multi-Asset</title>
    <script src="https://telegram.org/js/telegram-web-app.js"></script>
    <style>
        * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
        body { background: #07080c; color: #f1f5f9; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; margin: 0; padding: 0; width: 100vw; height: 100vh; overflow: hidden; }
        #splash-screen { position: fixed; top: 0; left: 0; width: 100vw; height: 100vh; background: linear-gradient(180deg, rgba(7,8,12,0.2) 0%, rgba(7,8,12,0.95) 100%), url('__SPLASH_URL__') center/cover; display: flex; flex-direction: column; justify-content: flex-end; align-items: center; padding: 24px; padding-bottom: calc(34px + env(safe-area-inset-bottom, 16px)); z-index: 9999; }
        .brand-title { font-size: 34px; font-weight: 900; color: #fff; }
        .brand-run { font-size: 42px; font-weight: 900; color: #5ff53b; margin-top: -6px; }
        .start-btn { background: #5ff53b; color: #000; border: none; width: 100%; max-width: 400px; padding: 18px; border-radius: 18px; font-weight: 800; font-size: 16px; cursor: pointer; margin-top: 18px; }
        .card { background: #0e111a; border: 1px solid #1c2235; border-radius: 20px; padding: 16px; margin-bottom: 12px; }
        .grid-stats { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-top: 10px; }
        .stat-box { background: #080a11; border: 1px solid #161b2c; border-radius: 12px; padding: 10px; }
        .stat-label { color: #64748b; font-size: 10px; font-weight: 600; text-transform: uppercase; }
        .stat-val { color: #f8fafc; font-size: 15px; font-weight: 800; margin-top: 2px; font-family: monospace; }
        .log-console { background: #05070a; border: 1px solid #141926; border-radius: 16px; padding: 12px; font-family: monospace; font-size: 11px; color: #4ade80; height: 300px; overflow-y: auto; white-space: pre-line; line-height: 1.5; }
        .bottom-nav { position: fixed; bottom: 0; left: 0; right: 0; background: rgba(10, 12, 18, 0.95); backdrop-filter: blur(20px); border-top: 1px solid #192033; padding: 12px 16px; display: flex; justify-content: space-around; z-index: 900; }
        .nav-btn { background: transparent; border: none; color: #64748b; font-size: 11px; font-weight: 600; cursor: pointer; }
        .nav-btn.active { color: #5ff53b; }
        .tab-panel { display: none; height: 100vh; overflow-y: auto; padding: 16px; padding-bottom: 95px; }
        .tab-panel.active { display: block; }
    </style>
</head>
<body>
    <div id="splash-screen">
        <div class="brand-title">ZERØLIFE</div>
        <div class="brand-run">MULTI-ASSET</div>
        <button class="start-btn" onclick="openTerminal()">Launch Terminal</button>
    </div>
    <div id="app" style="display: none;">
        <div id="tab-terminal" class="tab-panel active">
            <div class="card">
                <div style="font-size: 17px; font-weight: 800; color: #fff;">Топ-5 Ликвидных Активов</div>
                <div style="font-size: 11px; color: #64748b; margin-top: 2px;">SOL • JUP • RAY • WIF • PYTH</div>
                <div class="grid-stats">
                    <div class="stat-box"><div class="stat-label">Размер слота</div><div class="stat-val">$20.00 USDC</div></div>
                    <div class="stat-box"><div class="stat-label">Slippage</div><div class="stat-val" style="color: #60a5fa;">0.40%</div></div>
                    <div class="stat-box"><div class="stat-label">Take Profit</div><div class="stat-val" style="color: #4ade80;">+3.50%</div></div>
                    <div class="stat-box"><div class="stat-label">Stop Loss</div><div class="stat-val" style="color: #f87171;">-2.00%</div></div>
                </div>
            </div>
            <div class="card">
                <div style="font-size: 12px; font-weight: 700; color: #94a3b8; margin-bottom: 6px;">ЖУРНАЛ СКАНИРОВАНИЯ</div>
                <div id="log-box" class="log-console">Запуск ядра...</div>
            </div>
        </div>
        <div id="tab-stats" class="tab-panel">
            <div class="card">
                <div style="font-size: 15px; font-weight: 800;">Реализованный PnL</div>
                <div id="pnl-val" style="font-size: 28px; font-weight: 900; color: #4ade80; margin-top: 6px; font-family: monospace;">+$0.00 USDC</div>
            </div>
            <div class="card">
                <div style="font-size: 12px; font-weight: 700; color: #94a3b8; margin-bottom: 8px;">ИСТОРИЯ СДЕЛОК</div>
                <div id="history-box" style="color: #64748b; font-size: 11px; text-align: center; padding: 15px;">Нет закрытых сделок</div>
            </div>
        </div>
    </div>
    <div id="nav" class="bottom-nav" style="display: none;">
        <button class="nav-btn active" onclick="gotoTab('terminal', this)">⚡ Terminal</button>
        <button class="nav-btn" onclick="gotoTab('stats', this)">📊 Stats</button>
    </div>
    <script>
        function openTerminal() {
            document.getElementById('splash-screen').style.display = 'none';
            document.getElementById('app').style.display = 'block';
            document.getElementById('nav').style.display = 'flex';
            refresh();
        }
        function gotoTab(id, btn) {
            document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
            document.querySelectorAll('.nav-btn').forEach(b => b.classList.remove('active'));
            document.getElementById('tab-' + id).classList.add('active');
            btn.classList.add('active');
        }
        async function refresh() {
            try {
                const l = await fetch('/api/logs').then(r => r.json());
                if(l.success) document.getElementById('log-box').innerText = l.logs.join('\n');
                const s = await fetch('/api/stats').then(r => r.json());
                if(s.success) {
                    const p = s.profit_usdc;
                    document.getElementById('pnl-val').innerText = (p >= 0 ? "+$" : "-$") + Math.abs(p).toFixed(2) + " USDC";
                    if(s.trades.length > 0) {
                        document.getElementById('history-box').innerHTML = s.trades.map(t => `
                            <div style="background: #080a11; padding: 8px; border-radius: 8px; margin-bottom: 5px; display: flex; justify-content: space-between; font-family: monospace; font-size: 11px;">
                                <div>${t.token_symbol} ($${t.buy_price.toFixed(3)} → $${t.sell_price.toFixed(3)})</div>
                                <div style="color: ${t.profit_usdc >= 0 ? '#4ade80' : '#f87171'}; font-weight: bold;">${t.profit_usdc >= 0 ? '+' : ''}$${t.profit_usdc.toFixed(2)}</div>
                            </div>
                        `).join('');
                    }
                }
            } catch(e) {}
        }
        setInterval(refresh, 4000);
    </script>
</body>
</html>
"""

HTML_CONTENT = RAW_HTML.replace("__SPLASH_URL__", SPLASH_IMAGE_URL)

async def index_handler(request):
    return web.Response(text=HTML_CONTENT, content_type='text/html', headers={"Cache-Control": "no-store"})

async def health_handler(request):
    return web.Response(text="OK")

async def api_get_logs(request):
    return web.json_response({"success": True, "logs": RECENT_LOGS})

async def api_get_stats(request):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT token_symbol, buy_price, sell_price, profit_usdc, buy_tx, timestamp FROM trades WHERE sell_price > 0 ORDER BY id DESC LIMIT 15")
    rows = c.fetchall()
    trades = [{"token_symbol": r[0], "buy_price": r[1], "sell_price": r[2], "profit_usdc": r[3], "buy_tx": r[4], "timestamp": r[5]} for r in rows]
    c.execute("SELECT SUM(profit_usdc) FROM trades WHERE sell_price > 0")
    tot = c.fetchone()[0] or 0.0
    conn.close()
    return web.json_response({"success": True, "profit_usdc": tot, "trades": trades})

async def telegram_webhook_handler(request):
    try:
        data = await request.json()
        if "message" in data:
            message = data["message"]
            chat_id = message.get("chat", {}).get("id")
            text = message.get("text", "")
            username = message.get("from", {}).get("username", "Unknown")

            if text.startswith("/start") and chat_id and TELEGRAM_TOKEN:
                keyboard = {
                    "inline_keyboard": [[{"text": "🚀 Launch Terminal", "web_app": {"url": RENDER_URL}}]]
                }
                send_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
                payload = {
                    "chat_id": chat_id,
                    "text": "👋 Терминал **ZERØLIFE Multi-Asset** запущен.\nОтслеживание корзины: SOL, JUP, RAY, WIF, PYTH.",
                    "parse_mode": "Markdown",
                    "reply_markup": keyboard
                }
                async with aiohttp.ClientSession() as session:
                    await session.post(send_url, json=payload)
        return web.Response(text="OK")
    except Exception as e:
        return web.Response(text="Error", status=500)

async def set_telegram_webhook():
    if not TELEGRAM_TOKEN or not RENDER_URL:
        return
    webhook_url = f"{RENDER_URL.rstrip('/')}/webhook"
    set_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setWebhook?url={webhook_url}"
    async with aiohttp.ClientSession() as session:
        try:
            await session.get(set_url)
        except Exception:
            pass

async def main():
    init_db()
    app = web.Application()
    app.router.add_get('/', index_handler)
    app.router.add_get('/health', health_handler)
    app.router.add_get('/api/logs', api_get_logs)
    app.router.add_get('/api/stats', api_get_stats)
    app.router.add_post('/webhook', telegram_webhook_handler)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()

    await set_telegram_webhook()
    asyncio.create_task(real_trading_worker())
    add_log("⚜️ Ядро сканирования запущено на порту " + str(PORT))
    while True:
        await asyncio.sleep(3600)

if __name__ == "__main__":
    asyncio.run(main())
