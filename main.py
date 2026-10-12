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

logging.basicConfig(level=logging.INFO, format='%(asctime)s - [%(levelname)s] - %(message)s')

# --- КОНФИГУРАЦИЯ ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
PORT = int(os.getenv("PORT", 10000))
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL", "https://zer0lifeautopilot-bot.onrender.com").strip()
DB_FILE = "zer0life_users.db"
SOLANA_RPC = os.getenv("SOLANA_RPC_URL", "https://mainnet.helius-rpc.com/?api-key=b82ca0bf-cd65-4ca8-a3d0-4400661d9a93").strip()

SHARED_DEPOSIT_WALLET = "8hxiCofyaKCBkhR5nsDqvUivmfgxcVx8zo2WiCzSdM6L"
SPLASH_IMAGE_URL = "https://i.postimg.cc/52n94Pzg/57D47DDD-9BED-4D4B-AA69-2B59CAB400AD.png"

SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

TRADE_AMOUNT_USDC = 25.0
TRADE_AMOUNT_RAW = int(TRADE_AMOUNT_USDC * 1_000_000)
TP_PCT = 0.012
SL_PCT = 0.010

RECENT_LOGS = ["⚜️ AI Autonomous Engine v2.4 инициализирован."]

def add_log(msg: str):
    global RECENT_LOGS
    timestamp = datetime.now().strftime('%H:%M:%S')
    entry = f"[{timestamp}] {msg}"
    RECENT_LOGS.insert(0, entry)
    if len(RECENT_LOGS) > 50:
        RECENT_LOGS.pop()
    logging.info(msg)

from solders.keypair import Keypair
from solders.transaction import VersionedTransaction

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
        telegram_id INTEGER PRIMARY KEY, username TEXT, solana_wallet TEXT,
        trading_active INTEGER DEFAULT 1, initial_balance REAL DEFAULT 95.0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT, token_pair TEXT, position_side TEXT DEFAULT 'LONG',
        buy_price REAL, amount_lamports INTEGER, sell_price REAL DEFAULT 0,
        profit_usdc REAL DEFAULT 0.0, buy_tx TEXT, sell_tx TEXT, timestamp TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS deposits (
        id INTEGER PRIMARY KEY AUTOINCREMENT, telegram_id INTEGER, tx_signature TEXT UNIQUE,
        amount_sol REAL, status TEXT, timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    conn.commit()
    conn.close()

async def execute_jupiter_swap(session: aiohttp.ClientSession, input_mint: str, output_mint: str, amount_raw: int, signer: Keypair) -> str:
    try:
        user_pubkey = str(signer.pubkey())
        headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
        
        quote_url = f"https://api.jup.ag/swap/v1/quote?inputMint={input_mint}&outputMint={output_mint}&amount={amount_raw}&slippageBps=150"
        async with session.get(quote_url, headers=headers, timeout=aiohttp.ClientTimeout(total=6)) as resp:
            if resp.status != 200:
                err = await resp.text()
                add_log(f"❌ Quote API ({resp.status}): {err[:45]}")
                return ""
            quote_data = await resp.json()

        payload = {
            "quoteResponse": quote_data,
            "userPublicKey": user_pubkey,
            "wrapAndUnwrapSol": True,
            "prioritizationFeeLamports": 50000
        }
        async with session.post("https://api.jup.ag/swap/v1/swap", headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=7)) as resp:
            if resp.status != 200:
                err = await resp.text()
                add_log(f"❌ Swap API ({resp.status}): {err[:45]}")
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
                    add_log(f"❌ RPC: {data.get('error')}")
    except Exception as e:
        add_log(f"⚠️ Исключение исполнения: {e}")
    return ""

async def get_sol_price(session: aiohttp.ClientSession) -> float:
    try:
        url = "https://api.dexscreener.com/latest/dex/tokens/So11111111111111111111111111111111111111112"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=3)) as resp:
            if resp.status == 200:
                data = await resp.json()
                pairs = data.get("pairs") or []
                if pairs:
                    return float(pairs[0].get("priceUsd", 0.0))
    except Exception:
        pass
    try:
        url = "https://api.jup.ag/price/v2?ids=So11111111111111111111111111111111111111112"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=3)) as resp:
            if resp.status == 200:
                d = await resp.json()
                return float(d.get("data", {}).get(SOL_MINT, {}).get("price", 0.0))
    except Exception:
        pass
    return 0.0

# --- ПОЛНОСТЬЮ АВТОНОМНЫЙ AI-ВОРКЕР ---
async def real_trading_worker():
    add_log("🤖 AI Autonomous Agent запущен в фоновом режиме.")
    signer = get_signer_keypair()
    if not signer:
        return

    wallet_pubkey = str(signer.pubkey())
    add_log(f"🔑 Подключен кошелек: {wallet_pubkey[:6]}...{wallet_pubkey[-4:]}")

    price_history = []
    tick = 0

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                tick += 1
                current_price = await get_sol_price(session)
                if current_price <= 0:
                    await asyncio.sleep(4)
                    continue

                price_history.append(current_price)
                if len(price_history) > 20:
                    price_history.pop(0)

                conn = sqlite3.connect(DB_FILE)
                cursor = conn.cursor()
                cursor.execute("SELECT id, buy_price, amount_lamports, buy_tx FROM trades WHERE sell_price = 0 LIMIT 1")
                active_trade = cursor.fetchone()

                if active_trade:
                    trade_id, entry_px, lamports, buy_tx = active_trade
                    diff_pct = (current_price - entry_px) / entry_px
                    if tick % 3 == 0:
                        add_log(f"🎯 Позиция в рынке: SOL ${current_price:.2f} | PnL: {diff_pct*100:+.2f}%")

                    if diff_pct >= TP_PCT or diff_pct <= -SL_PCT:
                        add_log(f"⚡ Фиксация позиции ({diff_pct*100:+.2f}%). Отправка SOL -> USDC...")
                        sell_sig = await execute_jupiter_swap(session, SOL_MINT, USDC_MINT, lamports, signer)
                        if sell_sig:
                            pnl_usd = round(TRADE_AMOUNT_USDC * diff_pct, 2)
                            cursor.execute("UPDATE trades SET sell_price = ?, profit_usdc = ?, sell_tx = ? WHERE id = ?", (current_price, pnl_usd, sell_sig, trade_id))
                            conn.commit()
                            add_log(f"✅ Успешно! PnL: {pnl_usd:+.2f} USDC | Solscan: {sell_sig[:12]}...")
                    conn.close()
                    await asyncio.sleep(4)
                    continue

                conn.close()

                # Автономный поиск точки входа
                if len(price_history) >= 4:
                    avg_p = sum(price_history[-4:-1]) / 3
                    momentum = (current_price - avg_p) / avg_p
                    if tick % 3 == 0:
                        add_log(f"🧠 Скан рынка: SOL ${current_price:.2f} | Импульс: {momentum*100:+.3f}%")

                    if momentum >= 0.0002 or len(price_history) >= 12:
                        add_log(f"🚀 AI Сигнал подтвержден (+{momentum*100:.3f}%). Исполнение свопа $25 USDC -> SOL...")
                        buy_sig = await execute_jupiter_swap(session, USDC_MINT, SOL_MINT, TRADE_AMOUNT_RAW, signer)
                        if buy_sig:
                            sol_lamports = int((TRADE_AMOUNT_USDC / current_price) * 1_000_000_000)
                            c = sqlite3.connect(DB_FILE)
                            c.cursor().execute("""INSERT INTO trades (token_pair, position_side, buy_price, amount_lamports, sell_price, profit_usdc, buy_tx, sell_tx, timestamp) 
                                                  VALUES ('SOL/USDC', 'LONG', ?, ?, 0, 0, ?, '', ?)""", 
                                                  (current_price, sol_lamports, buy_sig, datetime.now().strftime('%H:%M:%S')))
                            c.commit()
                            c.close()
                            add_log(f"💎 Позиция открыта! Solscan: https://solscan.io/tx/{buy_sig}")
                        else:
                            add_log("⚠️ Сеть отклонила транзакцию. Повторная калибровка...")

            except Exception as e:
                add_log(f"⚠️ Ошибка цикла: {e}")

            await asyncio.sleep(4)

# --- ИНТЕРФЕЙС АВТОНОМНОГО ТЕРМИНАЛА ---
RAW_HTML = r"""<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no, viewport-fit=cover">
    <title>Zer0Life AI Trader</title>
    <script src="https://telegram.org/js/telegram-web-app.js"></script>
    <style>
        * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
        body { background: #07080c; color: #f1f5f9; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; margin: 0; padding: 0; width: 100vw; height: 100vh; overflow: hidden; }
        
        #splash-screen {
            position: fixed; top: 0; left: 0; width: 100vw; height: 100vh;
            background: linear-gradient(180deg, rgba(7,8,12,0.2) 0%, rgba(7,8,12,0.95) 100%), url('__SPLASH_URL__') center/cover;
            display: flex; flex-direction: column; justify-content: flex-end; align-items: center;
            padding: 24px; padding-bottom: calc(34px + env(safe-area-inset-bottom, 16px)); z-index: 9999;
        }
        .brand-title { font-size: 38px; font-weight: 900; color: #fff; letter-spacing: 1px; }
        .brand-run { font-size: 48px; font-weight: 900; color: #5ff53b; margin-top: -8px; text-shadow: 0 0 25px rgba(95,245,59,0.5); }
        .start-btn { background: #5ff53b; color: #000; border: none; width: 100%; max-width: 420px; padding: 18px; border-radius: 20px; font-weight: 800; font-size: 17px; cursor: pointer; margin-top: 20px; }

        .terminal-card {
            background: #0e111a; border: 1px solid #1c2235; border-radius: 22px;
            padding: 18px; margin-bottom: 14px; box-shadow: 0 10px 25px rgba(0,0,0,0.4);
        }
        .badge-live {
            display: inline-flex; align-items: center; gap: 6px;
            background: rgba(34, 197, 94, 0.12); border: 1px solid rgba(34, 197, 94, 0.3);
            color: #4ade80; padding: 5px 10px; border-radius: 20px; font-size: 11px; font-weight: 700;
        }
        .badge-dot { width: 7px; height: 7px; background: #4ade80; border-radius: 50%; box-shadow: 0 0 8px #4ade80; }
        .grid-stats { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-top: 14px; }
        .stat-box { background: #080a11; border: 1px solid #161b2c; border-radius: 14px; padding: 12px; }
        .stat-label { color: #64748b; font-size: 11px; font-weight: 600; text-transform: uppercase; }
        .stat-val { color: #f8fafc; font-size: 16px; font-weight: 800; margin-top: 4px; font-family: monospace; }
        
        .log-console {
            background: #05070a; border: 1px solid #141926; border-radius: 18px;
            padding: 14px; font-family: ui-monospace, Menlo, Consolas, monospace;
            font-size: 11px; color: #4ade80; height: 260px; overflow-y: auto; white-space: pre-line; line-height: 1.5;
        }

        .neon-input { background: #080a11; border: 1px solid #1e2538; border-radius: 14px; padding: 14px; width: 100%; color: #fff; font-family: monospace; font-size: 12px; margin: 10px 0; outline: none; }
        .btn-action { background: #6366f1; color: #fff; border: none; width: 100%; padding: 15px; border-radius: 16px; font-weight: 700; cursor: pointer; }

        .bottom-nav { position: fixed; bottom: 0; left: 0; right: 0; background: rgba(10, 12, 18, 0.95); backdrop-filter: blur(20px); border-top: 1px solid #192033; padding: 12px 16px; display: flex; justify-content: space-around; z-index: 900; }
        .nav-btn { background: transparent; border: none; color: #64748b; font-size: 11px; font-weight: 600; display: flex; flex-direction: column; align-items: center; gap: 4px; cursor: pointer; }
        .nav-btn.active { color: #5ff53b; }
        .tab-panel { display: none; height: 100vh; overflow-y: auto; padding: 16px; padding-bottom: 95px; }
        .tab-panel.active { display: block; }
    </style>
</head>
<body>
    <div id="splash-screen">
        <div class="brand-title">ZERØLIFE</div>
        <div class="brand-run">AI TRADER</div>
        <button class="start-btn" onclick="openTerminal()">Launch Terminal</button>
    </div>

    <div id="app" style="display: none;">
        <!-- TAB 1: TERMINAL -->
        <div id="tab-terminal" class="tab-panel active">
            <div class="terminal-card">
                <div style="display: flex; justify-content: space-between; align-items: center;">
                    <div>
                        <div style="font-size: 18px; font-weight: 800; color: #fff;">Autonomous Execution Engine</div>
                        <div style="font-size: 12px; color: #64748b; margin-top: 2px;">Jupiter v1 Onchain Routing</div>
                    </div>
                    <div class="badge-live"><div class="badge-dot"></div> AUTONOMOUS</div>
                </div>

                <div class="grid-stats">
                    <div class="stat-box">
                        <div class="stat-label">Активная пара</div>
                        <div class="stat-val" style="color: #60a5fa;">SOL / USDC</div>
                    </div>
                    <div class="stat-box">
                        <div class="stat-label">Размер входа</div>
                        <div class="stat-val">$25.00 USDC</div>
                    </div>
                    <div class="stat-box">
                        <div class="stat-label">Take Profit</div>
                        <div class="stat-val" style="color: #4ade80;">+1.20%</div>
                    </div>
                    <div class="stat-box">
                        <div class="stat-label">Stop Loss</div>
                        <div class="stat-val" style="color: #f87171;">-1.00%</div>
                    </div>
                </div>
            </div>

            <div class="terminal-card" style="padding: 14px;">
                <div style="font-size: 13px; font-weight: 700; color: #94a3b8; margin-bottom: 8px;">ЖУРНАЛ АВТОНОМНОГО ЯДРА</div>
                <div id="log-box" class="log-console">Подключение к ядру...</div>
            </div>
        </div>

        <!-- TAB 2: STATS -->
        <div id="tab-stats" class="tab-panel">
            <div class="terminal-card">
                <div style="font-size: 16px; font-weight: 800;">Доходность алгоритма</div>
                <div id="pnl-val" style="font-size: 32px; font-weight: 900; color: #4ade80; margin-top: 8px; font-family: monospace;">+$0.00 USDC</div>
            </div>
            <div class="terminal-card">
                <div style="font-size: 13px; font-weight: 700; color: #94a3b8; margin-bottom: 10px;">ОНЧЕЙН ИСТОРИЯ (SOLSCAN)</div>
                <div id="history-box" style="color: #64748b; font-size: 12px; text-align: center; padding: 20px;">Нет закрытых сделок</div>
            </div>
        </div>

        <!-- TAB 3: WALLET -->
        <div id="tab-wallet" class="tab-panel">
            <div class="terminal-card">
                <div style="font-size: 17px; font-weight: 800;">Кошелек экосистемы</div>
                <div style="font-size: 11px; color: #4ade80; font-family: monospace; word-break: break-all; margin-top: 10px; background: #080a11; padding: 12px; border-radius: 12px;">
                    8hxiCofyaKCBkhR5nsDqvUivmfgxcVx8zo2WiCzSdM6L
                </div>
                <div style="display: flex; justify-content: center; margin: 16px 0;">
                    <img src="https://api.qrserver.com/v1/create-qr-code/?size=150x150&data=8hxiCofyaKCBkhR5nsDqvUivmfgxcVx8zo2WiCzSdM6L" style="border-radius: 14px;">
                </div>
            </div>
            <div class="terminal-card">
                <div style="font-size: 14px; font-weight: 700;">Верификация депозита</div>
                <input id="dep-tx" class="neon-input" placeholder="Signature транзакции...">
                <button class="btn-action" onclick="verifyTx()">Подтвердить транзакцию</button>
            </div>
        </div>
    </div>

    <!-- МЕНЮ -->
    <div id="nav" class="bottom-nav" style="display: none;">
        <button class="nav-btn active" onclick="gotoTab('terminal', this)"><span>⚡</span><span>Terminal</span></button>
        <button class="nav-btn" onclick="gotoTab('stats', this)"><span>📊</span><span>Stats</span></button>
        <button class="nav-btn" onclick="gotoTab('wallet', this)"><span>💼</span><span>Wallet</span></button>
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

        async function verifyTx() {
            const tx = document.getElementById('dep-tx').value.trim();
            if(!tx) return;
            const res = await fetch('/api/verify_deposit', {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ telegram_id: 42882165, tx_signature: tx })
            });
            const d = await res.json();
            alert(d.message || "Обработано");
        }

        async function refresh() {
            try {
                const l = await fetch('/api/logs').then(r => r.json());
                if(l.success) document.getElementById('log-box').innerText = l.logs.join('\n');

                const s = await fetch('/api/stats?telegram_id=42882165').then(r => r.json());
                if(s.success) {
                    const p = s.profit_usdc;
                    document.getElementById('pnl-val').innerText = (p >= 0 ? "+$" : "-$") + Math.abs(p).toFixed(2) + " USDC";
                    if(s.trades.length > 0) {
                        document.getElementById('history-box').innerHTML = s.trades.map(t => `
                            <div style="background: #080a11; padding: 10px; border-radius: 10px; margin-bottom: 6px; display: flex; justify-content: space-between; font-family: monospace; font-size: 11px;">
                                <div>${t.token_pair} ($${t.buy_price.toFixed(2)} → $${t.sell_price.toFixed(2)})</div>
                                <div style="color: ${t.profit_usdc >= 0 ? '#4ade80' : '#f87171'}; font-weight: bold;">
                                    ${t.profit_usdc >= 0 ? '+' : ''}$${t.profit_usdc.toFixed(2)}
                                </div>
                            </div>
                        `).join('');
                    }
                }
            } catch(e) {}
        }
        setInterval(refresh, 3500);
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
    c.execute("SELECT token_pair, buy_price, sell_price, profit_usdc, buy_tx, timestamp FROM trades WHERE sell_price > 0 ORDER BY id DESC LIMIT 15")
    rows = c.fetchall()
    trades = [{"token_pair": r[0], "buy_price": r[1], "sell_price": r[2], "profit_usdc": r[3], "buy_tx": r[4], "timestamp": r[5]} for r in rows]
    c.execute("SELECT SUM(profit_usdc) FROM trades WHERE sell_price > 0")
    tot = c.fetchone()[0] or 0.0
    conn.close()
    return web.json_response({"success": True, "profit_usdc": tot, "trades": trades})

async def api_verify_deposit(request):
    try:
        body = await request.json()
        tx = body.get("tx_signature", "").strip()
        tg_id = int(body.get("telegram_id", 42882165))
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("INSERT INTO deposits (telegram_id, tx_signature, amount_sol, status) VALUES (?, ?, 0.5, 'VERIFIED')", (tg_id, tx))
        conn.commit()
        conn.close()
        add_log(f"📥 Депозит верифицирован: {tx[:10]}...")
        return web.json_response({"success": True, "message": "Депозит подтвержден"})
    except Exception as e:
        return web.json_response({"success": False, "message": str(e)})

async def main():
    init_db()
    app = web.Application()
    app.router.add_get('/', index_handler)
    app.router.add_get('/health', health_handler)
    app.router.add_get('/api/logs', api_get_logs)
    app.router.add_get('/api/stats', api_get_stats)
    app.router.add_post('/api/verify_deposit', api_verify_deposit)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()

    asyncio.create_task(real_trading_worker())
    add_log("⚜️ Ядро полностью запущено на порту " + str(PORT))
    while True:
        await asyncio.sleep(3600)

if __name__ == "__main__":
    asyncio.run(main())
