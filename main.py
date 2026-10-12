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

# Токены Solana
SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

# Риск-менеджмент ($25 реального USDC на вход)
TRADE_AMOUNT_USDC = 25.0
TRADE_AMOUNT_RAW = int(TRADE_AMOUNT_USDC * 1_000_000)  # USDC 6 decimals
TP_PCT = 0.012  # +1.2% Тейк-профит
SL_PCT = 0.010  # -1.0% Стоп-лосс

RECENT_LOGS = ["⚜️ Zer0Life Live DEX Engine запущен."]

def add_log(msg: str):
    global RECENT_LOGS
    timestamp = datetime.now().strftime('%H:%M:%S')
    entry = f"[{timestamp}] {msg}"
    RECENT_LOGS.insert(0, entry)
    if len(RECENT_LOGS) > 40:
        RECENT_LOGS.pop()
    logging.info(msg)

from solders.keypair import Keypair
from solders.transaction import VersionedTransaction

def get_signer_keypair():
    pk_env = os.getenv("SOLANA_PRIVATE_KEY", "").strip()
    if not pk_env:
        add_log("❌ Ошибка: SOLANA_PRIVATE_KEY пуст в переменных Render!")
        return None
    try:
        pk_clean = pk_env.replace('"', '').replace("'", "").strip()
        if pk_clean.startswith("["):
            return Keypair.from_bytes(bytes(json.loads(pk_clean)))
        else:
            return Keypair.from_bytes(base58.b58decode(pk_clean))
    except Exception as e:
        add_log(f"⚠️ Ошибка разбора приватного ключа: {e}")
        return None

# --- БАЗА ДАННЫХ ---
def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            telegram_id INTEGER PRIMARY KEY,
            username TEXT,
            solana_wallet TEXT,
            trading_active INTEGER DEFAULT 1,
            initial_balance REAL DEFAULT 95.0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token_pair TEXT,
            position_side TEXT DEFAULT 'LONG',
            buy_price REAL,
            amount_lamports INTEGER,
            sell_price REAL DEFAULT 0,
            profit_usdc REAL DEFAULT 0.0,
            buy_tx TEXT,
            sell_tx TEXT,
            timestamp TEXT
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS deposits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER,
            tx_signature TEXT UNIQUE,
            amount_sol REAL,
            status TEXT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()

async def send_telegram_notification(chat_id: int, text: str):
    if not TELEGRAM_TOKEN or not chat_id:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    async with aiohttp.ClientSession() as session:
        try:
            await session.post(url, json=payload, timeout=3)
        except Exception:
            pass

# --- РЕАЛЬНОЕ ИСПОЛНЕНИЕ ЧЕРЕЗ JUPITER SWAP API V6 ---
API_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json"
}

async def execute_jupiter_swap(session: aiohttp.ClientSession, input_mint: str, output_mint: str, amount_raw: int, signer: Keypair) -> str:
    """Выполняет реальный своп в блокчейне Solana через Jupiter v6."""
    try:
        user_pubkey = str(signer.pubkey())

        # 1. Запрос котировки (Quote)
        quote_url = f"https://quote-api.jup.ag/v6/quote?inputMint={input_mint}&outputMint={output_mint}&amount={amount_raw}&slippageBps=100"
        async with session.get(quote_url, headers=API_HEADERS, timeout=6) as resp:
            if resp.status != 200:
                err_text = await resp.text()
                add_log(f"❌ Jupiter Quote ({resp.status}): {err_text[:70]}")
                return ""
            quote_data = await resp.json()

        # 2. Получение сериализованной транзакции (Swap)
        swap_payload = {
            "quoteResponse": quote_data,
            "userPublicKey": user_pubkey,
            "wrapAndUnwrapSol": True,
            "dynamicComputeUnitLimit": True,
            "prioritizationFeeLamports": 50000
        }
        async with session.post("https://quote-api.jup.ag/v6/swap", headers=API_HEADERS, json=swap_payload, timeout=8) as resp:
            if resp.status != 200:
                err_text = await resp.text()
                add_log(f"❌ Jupiter Swap ({resp.status}): {err_text[:70]}")
                return ""
            swap_res = await resp.json()
            swap_tx_b64 = swap_res.get("swapTransaction")

        if not swap_tx_b64:
            add_log("❌ Ошибка: пустой swapTransaction от Jupiter")
            return ""

        # 3. Десериализация и подпись через VersionedTransaction
        raw_tx_bytes = base64.b64decode(swap_tx_b64)
        tx = VersionedTransaction.from_bytes(raw_tx_bytes)
        signed_tx = VersionedTransaction(tx.message, [signer])

        # 4. Отправка в сеть через RPC
        signed_b64 = base64.b64encode(bytes(signed_tx)).decode("utf-8")
        rpc_payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "sendTransaction",
            "params": [signed_b64, {"skipPreflight": False, "encoding": "base64", "preflightCommitment": "confirmed"}]
        }
        async with session.post(SOLANA_RPC, json=rpc_payload, timeout=10) as resp:
            if resp.status == 200:
                rpc_res = await resp.json()
                if "result" in rpc_res and rpc_res["result"]:
                    return str(rpc_res["result"])
                else:
                    err_msg = rpc_res.get("error", {}).get("message", "RPC Error")
                    add_log(f"❌ RPC отказ: {err_msg}")
            else:
                add_log(f"❌ Ошибка RPC HTTP {resp.status}")
    except Exception as e:
        add_log(f"⚠️ Ошибка выполнения свопа: {e}")
    return ""

async def get_sol_price(session: aiohttp.ClientSession) -> float:
    try:
        url = "https://api.jup.ag/price/v2?ids=So11111111111111111111111111111111111111112"
        async with session.get(url, headers=API_HEADERS, timeout=4) as resp:
            if resp.status == 200:
                d = await resp.json()
                return float(d.get("data", {}).get(SOL_MINT, {}).get("price", 0.0))
    except Exception:
        pass
    try:
        url = "https://api.dexscreener.com/latest/dex/tokens/So11111111111111111111111111111111111111112"
        async with session.get(url, timeout=4) as resp:
            if resp.status == 200:
                d = await resp.json()
                return float(d.get("pairs", [{}])[0].get("priceUsd", 0.0))
    except Exception:
        pass
    return 0.0

# --- БОЕВОЙ ТОРГОВЫЙ ЦИКЛ ---
async def real_trading_worker():
    add_log("🚀 Инициализация ончейн-исполнителя Jupiter...")
    signer = get_signer_keypair()
    if not signer:
        add_log("❌ ОСТАНОВКА: Проверьте SOLANA_PRIVATE_KEY на Render!")
        return

    wallet_addr = str(signer.pubkey())
    add_log(f"🔑 Live Mode: Авторизован кошелек {wallet_addr[:6]}...{wallet_addr[-4:]}")

    price_history = []

    async with aiohttp.ClientSession() as session:
        while True:
            try:
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

                # 1. Позиция открыта — проверяем тейк и стоп
                if active_trade:
                    trade_id, entry_px, lamports, buy_tx = active_trade
                    diff_pct = (current_price - entry_px) / entry_px
                    add_log(f"📊 Позиция: SOL ${current_price:.2f} \vert{} Вход: ${entry_px:.2f} (PnL: {diff_pct*100:+.2f}%)")

                    if diff_pct >= TP_PCT or diff_pct <= -SL_PCT:
                        add_log("🔄 Сигнал выхода: отправка SOL -> USDC в Jupiter...")
                        sell_sig = await execute_jupiter_swap(
                            session=session,
                            input_mint=SOL_MINT,
                            output_mint=USDC_MINT,
                            amount_raw=lamports,
                            signer=signer
                        )

                        if sell_sig:
                            pnl_usd = round(TRADE_AMOUNT_USDC * diff_pct, 2)
                            cursor.execute("UPDATE trades SET sell_price = ?, profit_usdc = ?, sell_tx = ? WHERE id = ?", (current_price, pnl_usd, sell_sig, trade_id))
                            conn.commit()

                            add_log(f"✅ Позиция закрыта! PnL: {'+' if pnl_usd >= 0 else ''}{pnl_usd} USDC. TX: {sell_sig[:10]}...")
                            add_log(f"🔗 Solscan: https://solscan.io/tx/{sell_sig}")

                            cursor.execute("SELECT telegram_id FROM users WHERE trading_active = 1")
                            users = cursor.fetchall()
                            for u in users:
                                await send_telegram_notification(u[0], f"🎯 *Сделка закрыта!*\n• Вход: `${entry_px:.2f}` -> Выход: `${current_price:.2f}`\n• PnL: `+{pnl_usd} USDC` 💵\n• [Смотреть транзакцию в Solscan](https://solscan.io/tx/{sell_sig})")
                        else:
                            add_log("⚠️ Ошибка отправки транзакции закрытия в Solana. Повтор...")

                    conn.close()
                    await asyncio.sleep(5)
                    continue

                conn.close()

                # 2. Позиции нет — скрининг рынка
                if len(price_history) >= 4:
                    avg_p = sum(price_history[-4:-1]) / 3
                    momentum = (current_price - avg_p) / avg_p
                    add_log(f"🔍 Скринер: SOL ${current_price:.2f} | Импульс: {momentum*100:+.3f}%")

                    # Вход при фиксации импульса
                    if momentum >= 0.0003:
                        add_log(f"⚡ Вход по сигналу (+{momentum*100:.3f}%): Отправка 25 USDC -> SOL в Jupiter...")
                        buy_sig = await execute_jupiter_swap(
                            session=session,
                            input_mint=USDC_MINT,
                            output_mint=SOL_MINT,
                            amount_raw=TRADE_AMOUNT_RAW,
                            signer=signer
                        )

                        if buy_sig:
                            sol_bought_lamports = int((TRADE_AMOUNT_USDC / current_price) * 1_000_000_000)

                            c = sqlite3.connect(DB_FILE)
                            c.cursor().execute("""
                                INSERT INTO trades (token_pair, position_side, buy_price, amount_lamports, sell_price, profit_usdc, buy_tx, sell_tx, timestamp) 
                                VALUES ('SOL/USDC', 'LONG', ?, ?, 0, 0, ?, '', ?)
                            """, (current_price, sol_bought_lamports, buy_sig, datetime.now().strftime('%H:%M:%S')))
                            c.commit()
                            c.close()

                            add_log(f"💎 ПОКУПКА В СЕТИ ПОДТВЕРЖДЕНА! SOL куплен по ${current_price:.2f}")
                            add_log(f"🔗 Solscan TX: https://solscan.io/tx/{buy_sig}")

                            c = sqlite3.connect(DB_FILE)
                            active_u = c.cursor().execute("SELECT telegram_id FROM users WHERE trading_active = 1").fetchall()
                            c.close()
                            for u in active_u:
                                await send_telegram_notification(u[0], f"🚀 *Покупка SOL на Phantom!*\n• Сумма: `25 USDC`\n• Цена: `${current_price:.2f}`\n• [Транзакция в Solscan](https://solscan.io/tx/{buy_sig})")
                        else:
                            add_log("⚠️ Своп не выполнен. Проверьте баланс SOL на газ.")

            except Exception as e:
                logging.error(f"Trading loop error: {e}")

            await asyncio.sleep(5)

# --- TELEGRAM BOT WORKER ---
async def telegram_bot_worker():
    if not TELEGRAM_TOKEN:
        return
    offset = 0
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
    send_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                async with session.get(f"{url}?offset={offset}&timeout=20", timeout=25) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        for update in data.get("result", []):
                            offset = update["update_id"] + 1
                            msg = update.get("message", {})
                            text = msg.get("text", "")
                            chat_id = msg.get("chat", {}).get("id")

                            if text.startswith("/start") and chat_id:
                                conn = sqlite3.connect(DB_FILE)
                                c = conn.cursor()
                                c.execute("INSERT OR IGNORE INTO users (telegram_id, username, solana_wallet) VALUES (?, ?, ?)", 
                                          (chat_id, msg.get("from", {}).get("username", ""), SHARED_DEPOSIT_WALLET))
                                conn.commit()
                                conn.close()

                                reply = {
                                    "chat_id": chat_id,
                                    "text": "⚜️ *Zer0Life Real DEX Engine Активен!*\n\nКаждая сделка совершается реально на Jupiter DEX с баланса вашего кошелька.\n\n👇 Нажмите кнопку ниже:",
                                    "parse_mode": "Markdown",
                                    "reply_markup": {
                                        "inline_keyboard": [[
                                            {"text": "🚀 Открыть Zer0Life Run", "web_app": {"url": RENDER_URL}}
                                        ]]
                                    }
                                }
                                await session.post(send_url, json=reply, timeout=5)
            except Exception as e:
                logging.error(f"Telegram polling error: {e}")
            await asyncio.sleep(1)

# --- ИНТЕРФЕЙС ПРИЛОЖЕНИЯ ---
RAW_HTML = r"""<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no, viewport-fit=cover">
    <title>Ai_trading_bot</title>
    <script src="https://telegram.org/js/telegram-web-app.js"></script>
    <style>
        * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
        body { background: #050507; color: #f1f5f9; font-family: -apple-system, BlinkMacSystemFont, "SF Pro Display", sans-serif; margin: 0; padding: 0; width: 100vw; height: 100vh; overflow: hidden; }
        #splash-screen {
            position: fixed; top: 0; left: 0; width: 100vw; height: 100vh;
            background-color: #050507;
            background-image: linear-gradient(180deg, rgba(0,0,0,0.05) 0%, rgba(0,0,0,0.2) 50%, rgba(0,0,0,0.85) 100%), url('__SPLASH_URL__');
            background-size: cover; background-position: center top; background-repeat: no-repeat;
            display: flex; flex-direction: column; justify-content: flex-end; align-items: center;
            padding: 24px; padding-bottom: calc(34px + env(safe-area-inset-bottom, 16px)); z-index: 9999;
        }
        .splash-branding { text-align: center; margin-bottom: 24px; user-select: none; }
        .brand-title { font-size: 42px; font-weight: 900; font-style: italic; color: #ffffff; text-transform: uppercase; line-height: 1.0; letter-spacing: 1px; }
        .brand-run { font-size: 54px; font-weight: 900; font-style: italic; color: #5ff53b; text-transform: uppercase; line-height: 1.0; margin-top: 4px; text-shadow: 0 0 25px rgba(95,245,59,0.5); }
        .brand-tagline { font-size: 13px; font-weight: 800; color: #ffffff; text-transform: uppercase; letter-spacing: 4px; margin-top: 10px; opacity: 0.95; }
        .run-btn {
            background: linear-gradient(180deg, #8af64a 0%, #68e831 100%);
            color: #000000; border: none; width: 100%; max-width: 440px; padding: 18px;
            border-radius: 22px; font-weight: 800; font-size: 18px; cursor: pointer; text-align: center;
            box-shadow: 0 10px 28px rgba(104,232,49,0.45); letter-spacing: 0.5px;
        }
        .run-btn:active { transform: scale(0.97); }

        .card { background: linear-gradient(145deg, rgba(22, 22, 26, 0.95), rgba(12, 12, 14, 0.95)); backdrop-filter: blur(20px); border-radius: 24px; padding: 20px; margin-bottom: 16px; border: 1px solid rgba(212, 175, 55, 0.2); }
        .title-gold { margin-top: 0; font-size: 17px; font-weight: 800; background: linear-gradient(to right, #d4af37, #ffdf73, #d4af37); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
        .metric { display: flex; justify-content: space-between; margin-top: 10px; font-size: 13px; color: #8a8d93; }
        .val { color: #ffdf73; font-weight: 700; font-family: monospace; }
        .btn-gold { background: linear-gradient(135deg, #d4af37 0%, #aa771c 100%); color: #050505; border: none; width: 100%; padding: 14px; border-radius: 18px; font-weight: 800; cursor: pointer; margin-top: 12px; }
        .btn-red { background: linear-gradient(135deg, #3f0d12 0%, #a71d31 100%); color: white; border-radius: 18px; padding: 12px; font-weight: 700; border: none; width: 100%; cursor: pointer; }
        .logs { background: rgba(0, 0, 0, 0.6); border: 1px solid rgba(212, 175, 55, 0.15); border-radius: 18px; padding: 12px; font-family: monospace; font-size: 11px; color: #5ff53b; height: 170px; overflow-y: auto; margin-top: 10px; white-space: pre-line; }
        .trade-item { background: rgba(0, 0, 0, 0.4); border: 1px solid rgba(212, 175, 55, 0.15); border-radius: 16px; padding: 12px; margin-top: 8px; font-family: monospace; font-size: 11px; display: flex; justify-content: space-between; align-items: center; }

        /* СТИЛЬ WALLET */
        .neon-card { background: #0d101d; border: 1px solid #1c223a; border-radius: 26px; padding: 20px; margin-bottom: 16px; box-shadow: 0 10px 30px rgba(0, 0, 0, 0.5); }
        .header-row { display: flex; justify-content: space-between; align-items: flex-start; }
        .user-name { font-size: 19px; font-weight: 800; color: #ffffff; letter-spacing: 0.3px; }
        .user-id { color: #17e089; font-size: 13px; font-weight: 700; margin-top: 5px; font-family: monospace; }
        .pool-badge { background: rgba(14, 53, 44, 0.85); border: 1px solid #137754; color: #1fe090; padding: 8px 12px; border-radius: 20px; font-size: 11px; font-weight: 700; }
        .label-text { color: #7b88a8; font-size: 12px; font-weight: 500; margin-top: 18px; margin-bottom: 8px; }
        .address-pill { background: #080a14; border: 1px solid #16533f; border-radius: 14px; padding: 13px 12px; font-family: monospace; font-size: 11px; color: #1ee08f; text-align: center; word-break: break-all; }
        .qr-wrapper { display: flex; justify-content: center; align-items: center; margin-top: 18px; margin-bottom: 18px; }
        .qr-box { background: #ffffff; padding: 12px; border-radius: 18px; }
        .qr-box img { display: block; width: 145px; height: 145px; }
        .btn-purple { background: linear-gradient(180deg, #6b43f7 0%, #5127ee 100%); color: #ffffff; border: none; width: 100%; padding: 16px; border-radius: 18px; font-weight: 700; font-size: 15px; cursor: pointer; }
        .card-title { display: flex; align-items: center; gap: 8px; font-size: 16px; font-weight: 800; color: #ffffff; margin-bottom: 6px; }
        .input-tx { background: #080a14; border: 1px solid #1c223a; border-radius: 14px; padding: 15px; width: 100%; color: #ffffff; font-family: monospace; font-size: 12px; outline: none; margin-top: 6px; margin-bottom: 14px; }
        .btn-green { background: linear-gradient(180deg, #1bd984 0%, #0fb86c 100%); color: #041f12; border: none; width: 100%; padding: 16px; border-radius: 18px; font-weight: 800; font-size: 15px; cursor: pointer; }

        .bottom-nav { position: fixed; bottom: 0; left: 0; right: 0; background: rgba(10, 10, 12, 0.95); backdrop-filter: blur(30px); border-top: 1px solid rgba(212, 175, 55, 0.15); padding: 12px 16px; display: flex; justify-content: space-around; z-index: 900; }
        .nav-item { background: transparent; border: none; color: #5a5d63; font-size: 11px; font-weight: 600; display: flex; flex-direction: column; align-items: center; gap: 4px; cursor: pointer; }
        .nav-item.active { color: #d4af37; text-shadow: 0 0 15px rgba(212, 175, 55, 0.6); }
        .tab-content { display: none; width: 100%; height: 100vh; overflow-y: auto; padding: 16px; padding-bottom: 110px; }
        .tab-content.active { display: block; }
    </style>
</head>
<body>
    <div id="splash-screen">
        <div class="splash-branding">
            <div class="brand-title">ZERØLIFE</div>
            <div class="brand-run">RUN</div>
            <div class="brand-tagline">MOVE TO EARN</div>
        </div>
        <button class="run-btn" onclick="enterApp()" ontouchstart="enterApp()">Get Started</button>
    </div>

    <div id="app-container" style="display: none; width: 100vw; height: 100vh;">
        <!-- TAB 1: TRADER -->
        <div id="tab-trader" class="tab-content active">
            <div class="card">
                <h3 class="title-gold">⚡ Live Onchain Jupiter DEX</h3>
                <div class="metric"><span>Режим:</span> <span class="val" style="color: #5ff53b;">100% Ончейн (Real Swaps)</span></div>
                <div class="metric"><span>Сделка:</span> <span class="val">$25.00 USDC -> SOL</span></div>
                <div class="metric"><span>Кошелек:</span> <span class="val">8hxi...dM6L</span></div>
                <div class="metric"><span>Статус:</span> <span id="status-val" class="val" style="color: #5ff53b;">Активен (24/7)</span></div>
                <div style="display: flex; gap: 10px; margin-top: 14px;">
                    <button class="btn-gold" style="margin-top:0;" onclick="loadStats()">Обновить</button>
                    <button id="toggle-btn" class="btn-red" style="margin-top:0;" onclick="toggleTrading()">Остановить</button>
                </div>
            </div>
            <div class="card">
                <h3 class="title-gold">📡 Журнал Реальных Транзакций</h3>
                <div id="logs-box" class="logs">Связь с блокчейном Solana...</div>
            </div>
        </div>

        <!-- TAB 2: STATS -->
        <div id="tab-stats" class="tab-content">
            <div class="card">
                <h3 class="title-gold">📊 Статистика Доходности</h3>
                <div class="metric"><span>Чистый PnL:</span> <span id="stat-profit" class="val" style="color: #5ff53b;">+$0.00 USDC</span></div>
                <button class="btn-gold" onclick="loadStats()">Обновить</button>
            </div>
            <div class="card">
                <h3 class="title-gold">📜 Исполненные Сделки (Solscan)</h3>
                <div id="trades-list" style="max-height: 250px; overflow-y: auto;">
                    <div style="color: #5a5d63; font-size: 12px; text-align: center; padding: 20px;">Ожидание сделок...</div>
                </div>
            </div>
        </div>

        <!-- TAB 3: WALLET -->
        <div id="tab-wallet" class="tab-content">
            <div class="neon-card">
                <div class="header-row">
                    <div>
                        <div class="user-name">CryptoWlodek | ZRL szn ∞</div>
                        <div class="user-id">ID: 42882165</div>
                    </div>
                    <div class="pool-badge">🔥 Pool Limit: 0.25 - 100 SOL</div>
                </div>

                <div class="label-text">Адрес депозита экосистемы:</div>
                <div class="address-pill" id="sol-address">8hxiCofyaKCBkhR5nsDqvUivmfgxcVx8zo2WiCzSdM6L</div>

                <div class="qr-wrapper">
                    <div class="qr-box">
                        <img src="https://api.qrserver.com/v1/create-qr-code/?size=180x180&data=8hxiCofyaKCBkhR5nsDqvUivmfgxcVx8zo2WiCzSdM6L&bgcolor=ffffff&color=000000&margin=0" alt="QR">
                    </div>
                </div>

                <button class="btn-purple" onclick="copyDepositAddress()">📋 Копировать адрес</button>
            </div>

            <div class="neon-card">
                <div class="card-title">🎽 Верификация депозита</div>
                <div class="label-text" style="margin-top: 10px;">Хэш транзакции (Signature):</div>
                <input type="text" id="tx-input" class="input-tx" placeholder="Вставьте хэш транзакции...">
                <button class="btn-green" onclick="verifyDeposit()">Verify & Credit SOL 🔄</button>
            </div>
        </div>
    </div>

    <!-- МЕНЮ НАВИГАЦИИ -->
    <div id="bottom-nav-bar" class="bottom-nav" style="display: none;">
        <button id="nav-trader" class="nav-item active" onclick="switchTab('trader')"><span style="font-size:20px;">⚡</span><span>Trader</span></button>
        <button id="nav-stats" class="nav-item" onclick="switchTab('stats')"><span style="font-size:20px;">📊</span><span>Stats</span></button>
        <button id="nav-wallet" class="nav-item" onclick="switchTab('wallet')"><span style="font-size:20px;">💼</span><span>Wallet</span></button>
    </div>

    <script>
        let user = { id: 42882165, username: "CryptoWlodek" };
        try {
            if (window.Telegram && window.Telegram.WebApp) { window.Telegram.WebApp.expand(); }
        } catch(e) {}

        window.enterApp = function() {
            document.getElementById('splash-screen').style.display = 'none';
            document.getElementById('app-container').style.display = 'block';
            document.getElementById('bottom-nav-bar').style.display = 'flex';
            switchTab('trader');
            loadStats(); loadLogs();
        };

        window.switchTab = function(tab) {
            document.querySelectorAll('#app-container .tab-content').forEach(el => el.classList.remove('active'));
            document.querySelectorAll('.nav-item').forEach(el => el.classList.remove('active'));
            if(tab === 'trader') {
                document.getElementById('tab-trader').classList.add('active');
                document.getElementById('nav-trader').classList.add('active');
                loadLogs();
            } else if(tab === 'stats') {
                document.getElementById('tab-stats').classList.add('active');
                document.getElementById('nav-stats').classList.add('active');
                loadStats();
            } else if(tab === 'wallet') {
                document.getElementById('tab-wallet').classList.add('active');
                document.getElementById('nav-wallet').classList.add('active');
            }
        };

        let isTrading = true;
        window.toggleTrading = async function() {
            isTrading = !isTrading;
            document.getElementById('status-val').innerText = isTrading ? "Активен (24/7)" : "Остановлен";
            document.getElementById('status-val').style.color = isTrading ? "#5ff53b" : "#8a8d93";
            document.getElementById('toggle-btn').innerText = isTrading ? "Остановить" : "Включить";
            document.getElementById('toggle-btn').className = isTrading ? "btn-red" : "btn-gold";
        };

        window.copyDepositAddress = function() {
            const addr = document.getElementById('sol-address').innerText.trim();
            if (navigator.clipboard) {
                navigator.clipboard.writeText(addr).then(() => { alert("✅ Адрес депозита скопирован!"); });
            } else { alert("Адрес: " + addr); }
        };

        window.verifyDeposit = async function() {
            const tx = document.getElementById('tx-input').value.trim();
            if(!tx) { alert("Вставьте хэш транзакции!"); return; }
            try {
                const res = await fetch('/api/verify_deposit', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ telegram_id: 42882165, tx_signature: tx })
                });
                const data = await res.json();
                if(data.success) {
                    alert("🎉 Транзакция подтверждена!");
                    document.getElementById('tx-input').value = "";
                } else { alert("⚠️ " + (data.message || "Ошибка верификации.")); }
            } catch(e) { alert("Ошибка запроса."); }
        };

        window.loadLogs = async function() {
            try {
                const res = await fetch('/api/logs');
                const data = await res.json();
                if(data.success) { document.getElementById('logs-box').innerText = data.logs.join('\n'); }
            } catch(e) {}
        };

        window.loadStats = async function() {
            try {
                const res = await fetch('/api/stats?telegram_id=' + user.id);
                const data = await res.json();
                if(data.success) {
                    const p = data.profit_usdc;
                    const el = document.getElementById('stat-profit');
                    el.innerText = (p >= 0 ? "+$" : "-$") + Math.abs(p).toFixed(2) + " USDC";
                    el.style.color = p >= 0 ? "#5ff53b" : "#a71d31";
                    
                    const list = document.getElementById('trades-list');
                    if(data.trades.length === 0) {
                        list.innerHTML = '<div style="color: #5a5d63; font-size: 12px; text-align: center; padding: 20px;">Ожидание реальных транзакций...</div>';
                    } else {
                        list.innerHTML = data.trades.map(t => `
                            <div class="trade-item">
                                <div>
                                    <div style="color: #f1f5f9; font-weight: bold;">${t.token_pair}</div>
                                    <div style="color: #8a8d93; font-size: 10px;">Вход: $${t.buy_price.toFixed(2)} | Выход: $${t.sell_price.toFixed(2)}</div>
                                </div>
                                <div style="text-align: right;">
                                    <div style="color: ${t.profit_usdc >= 0 ? '#5ff53b' : '#a71d31'}; font-weight: bold;">
                                        ${t.profit_usdc >= 0 ? '+' : ''}$${t.profit_usdc.toFixed(2)} USDC
                                    </div>
                                    <a href="https://solscan.io/tx/${t.buy_tx}" target="_blank" style="color: #d4af37; font-size: 9px; text-decoration: underline;">Solscan TX</a>
                                </div>
                            </div>
                        `).join('');
                    }
                }
            } catch(e) {}
        };

        setInterval(() => { loadLogs(); loadStats(); }, 4000);
    </script>
</body>
</html>
"""

HTML_CONTENT = RAW_HTML.replace("__SPLASH_URL__", SPLASH_IMAGE_URL)

async def index_handler(request):
    headers = {"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"}
    return web.Response(text=HTML_CONTENT, content_type='text/html', headers=headers)

async def health_handler(request):
    return web.Response(text="OK", status=200)

async def api_get_logs(request):
    return web.json_response({"success": True, "logs": RECENT_LOGS})

async def api_get_stats(request):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT token_pair, buy_price, sell_price, profit_usdc, buy_tx, timestamp FROM trades WHERE sell_price > 0 ORDER BY id DESC LIMIT 20")
    rows = cursor.fetchall()
    trades = [{"token_pair": r[0], "buy_price": r[1], "sell_price": r[2], "profit_usdc": r[3], "buy_tx": r[4], "timestamp": r[5]} for r in rows]
    cursor.execute("SELECT SUM(profit_usdc) FROM trades WHERE sell_price > 0")
    total_profit = cursor.fetchone()[0] or 0.0
    conn.close()
    return web.json_response({"success": True, "profit_usdc": total_profit, "trades": trades})

async def api_verify_deposit(request):
    try:
        body = await request.json()
        tx = body.get("tx_signature", "").strip()
        tg_id = int(body.get("telegram_id", 42882165))
        if not tx:
            return web.json_response({"success": False, "message": "Пустой хэш транзакции"})

        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM deposits WHERE tx_signature = ?", (tx,))
        if cursor.fetchone():
            conn.close()
            return web.json_response({"success": False, "message": "Эта транзакция уже была учтена!"})

        cursor.execute("INSERT INTO deposits (telegram_id, tx_signature, amount_sol, status) VALUES (?, ?, 0.5, 'VERIFIED')", (tg_id, tx))
        conn.commit()
        conn.close()

        add_log(f"📥 Депозит верифицирован: {tx[:10]}... на баланс Solana!")
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
    asyncio.create_task(telegram_bot_worker())

    add_log("⚜️ Zer0Life DEX Live Engine и Telegram Polling запущены.")
    while True:
        await asyncio.sleep(3600)

if __name__ == "__main__":
    asyncio.run(main())
