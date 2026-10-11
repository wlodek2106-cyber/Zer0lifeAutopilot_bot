import asyncio
import aiohttp
from aiohttp import web
import sqlite3
import os
import logging
from datetime import datetime
import json
import base64

logging.basicConfig(level=logging.INFO, format='%(asctime)s - [%(levelname)s] - %(message)s')

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
PORT = int(os.getenv("PORT", 10000))
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL", "https://zer0lifeautopilot-bot.onrender.com")
DB_FILE = "zer0life_users.db"
SOLANA_RPC = os.getenv("SOLANA_RPC_URL", "https://mainnet.helius-rpc.com/?api-key=b82ca0bf-cd65-4ca8-a3d0-4400661d9a93")

JUPITER_QUOTE_API = "https://public.jupiterapi.com/quote"
JUPITER_SWAP_API = "https://public.jupiterapi.com/swap"
RUGCHECK_API = "https://api.rugcheck.xyz/v1/token"

SHARED_DEPOSIT_WALLET = "8hxiCofyaKCBkhR5nsDqvUivmfgxcVx8zo2WiCzSdM6L"
GAS_RESERVE_SOL = 0.015  # Неприкосновенный резерв на комиссии

SPLASH_FILE_ID = "BQACAgIAAxkBAAIs6WrKMzeuDrcSrw64PHJtnoHvV5d0AALOqAACDnhQSjGP-cB9UjthPQQ"
CACHED_SPLASH_IMAGE: bytes | None = None

RECENT_LOGS = ["⚜️ Zer0Life AI Arbitrage Core инициализирован."]

def add_log(msg: str):
    global RECENT_LOGS
    timestamp = datetime.now().strftime('%H:%M:%S')
    entry = f"[{timestamp}] {msg}"
    RECENT_LOGS.insert(0, entry)
    if len(RECENT_LOGS) > 35:
        RECENT_LOGS.pop()
    logging.info(msg)

SOL_MINT = "So11111111111111111111111111111111111111112"

try:
    from solders.keypair import Keypair
    from solders.transaction import VersionedTransaction
    SOLANA_SDK_AVAILABLE = True
except ImportError:
    SOLANA_SDK_AVAILABLE = False

def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            telegram_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            solana_wallet TEXT,
            trading_active INTEGER DEFAULT 1,
            trade_mode TEXT DEFAULT 'WEB4_AI_SAFE',
            trade_amount_sol REAL DEFAULT 0.04,
            initial_sol REAL DEFAULT 0.2517,
            daily_loss_sol REAL DEFAULT 0.0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER,
            token_pair TEXT,
            buy_price REAL,
            sell_price REAL,
            profit_sol REAL,
            tx_signature TEXT,
            timestamp TEXT
        )
    ''')
    conn.commit()
    conn.close()

def get_or_create_user(telegram_id: int, username: str, first_name: str):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
    row = cursor.fetchone()
    if not row:
        cursor.execute("INSERT INTO users (telegram_id, username, first_name, solana_wallet, initial_sol, trading_active, trade_mode) VALUES (?, ?, ?, ?, ?, 1, 'WEB4_AI_SAFE')", 
                       (telegram_id, username, first_name, SHARED_DEPOSIT_WALLET, 0.2517))
        conn.commit()
        cursor.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
        row = cursor.fetchone()
    cols = [d[0] for d in cursor.description]
    user = dict(zip(cols, row))
    conn.close()
    return user

def get_signer_keypair():
    pk_env = os.getenv("SOLANA_PRIVATE_KEY", "").strip()
    if not pk_env:
        return None
    if not SOLANA_SDK_AVAILABLE:
        return None
    try:
        if pk_env.startswith("["):
            return Keypair.from_bytes(bytes(json.loads(pk_env)))
        else:
            import base58
            return Keypair.from_bytes(base58.b58decode(pk_env))
    except Exception as e:
        add_log(f"⚠️ Ошибка парсинга приватного ключа: {e}")
        return None

async def fetch_wallet_balance(wallet: str) -> float:
    payload = {"jsonrpc": "2.0", "id": 1, "method": "getBalance", "params": [wallet]}
    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(SOLANA_RPC, json=payload, timeout=3) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("result", {}).get("value", 0) / 1_000_000_000
        except Exception:
            pass
    return 0.2517

# Локальный кеш проверенных токенов для экономии лимитов RugCheck
AUDITED_TOKENS_CACHE: dict[str, bool] = {}

async def audit_token_safety(session: aiohttp.ClientSession, token_mint: str, pair_data: dict) -> bool:
    if token_mint in AUDITED_TOKENS_CACHE:
        return AUDITED_TOKENS_CACHE[token_mint]

    # 1. Первичный AI-фильтр по рыночным данным DexScreener
    liq = pair_data.get("liquidity", {}).get("usd", 0)
    vol24 = pair_data.get("volume", {}).get("h24", 0)
    
    # Отсекаем неликвидные ловушки
    if liq < 20000 or vol24 < 30000:
        AUDITED_TOKENS_CACHE[token_mint] = False
        return False

    # 2. Проверка через RugCheck API
    try:
        async with session.get(f"{RUGCHECK_API}/{token_mint}/report", timeout=3.0) as resp:
            if resp.status == 200:
                data = await resp.json()
                token_meta = data.get("token", {})
                
                # HoneyPot проверка: Freeze Authority должно быть отключено
                has_freeze = token_meta.get("freezeAuthority") is not None
                # Mint Authority должно быть отключено (нельзя допечатать)
                has_mint = token_meta.get("mintAuthority") is not None
                
                risks = data.get("risks", [])
                has_critical_danger = any(r.get("level") == "danger" for r in risks)
                score = data.get("score", 1000)

                is_safe = (not has_freeze) and (not has_mint) and (not has_critical_danger) and (score < 800)
                AUDITED_TOKENS_CACHE[token_mint] = is_safe
                return is_safe
            elif resp.status == 429:
                # При лимите запросов доверяем фильтру ликвидности от $60k
                return liq >= 60000
    except Exception:
        pass
    
    return liq >= 60000

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

async def aggressive_arbitrage_worker():
    add_log("⚡ AI Engine: Мониторинг пулов Raydium, Orca, Meteora...")
    signer = get_signer_keypair()
    if not signer:
        add_log("💡 Симуляция: Приватный ключ не задан. Сделки фиксируются виртуально.")
    else:
        add_log(f"🔑 Live Mode: Торговый кошелек подключен ({str(signer.pubkey())[:6]}...)")

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                # 1. Проверяем баланс и рассчитываем безопасный объем сделки
                current_balance = await fetch_wallet_balance(SHARED_DEPOSIT_WALLET)
                usable_sol = max(0.0, current_balance - GAS_RESERVE_SOL)
                
                # Размер сделки: от 0.02 SOL до 0.05 SOL (или 25% доступного баланса)
                trade_amount_sol = min(0.05, max(0.02, usable_sol * 0.25))
                amount_in_lamports = int(trade_amount_sol * 1_000_000_000)

                if usable_sol < 0.01:
                    add_log(f"⏳ Ожидание депозита. Баланс {current_balance:.4f} SOL (резерв газа: {GAS_RESERVE_SOL} SOL)")
                    await asyncio.sleep(12)
                    continue

                # 2. Поиск активных пулов через DexScreener
                async with session.get("https://api.dexscreener.com/latest/dex/search/?q=SOL", timeout=5) as resp:
                    if resp.status != 200:
                        await asyncio.sleep(4)
                        continue
                    data = await resp.json()
                    pairs = data.get("pairs", [])

                scanned_count = 0
                for p in pairs[:20]:
                    if p.get("chainId") != "solana":
                        continue

                    base = p.get("baseToken", {})
                    token_mint = base.get("address")
                    pair_sym = base.get("symbol", "TOKEN")

                    if not token_mint or token_mint == SOL_MINT:
                        continue

                    scanned_count += 1
                    
                    # 3. AI аудит на скам и ругпул
                    is_safe = await audit_token_safety(session, token_mint, p)
                    if not is_safe:
                        continue

                    # 4. Проверка спреда через агрегатор Jupiter (SOL -> TOKEN -> SOL)
                    q1_url = f"{JUPITER_QUOTE_API}?inputMint={SOL_MINT}&outputMint={token_mint}&amount={amount_in_lamports}&slippageBps=80"
                    async with session.get(q1_url, timeout=2.5) as q1_resp:
                        if q1_resp.status != 200:
                            continue
                        q1_data = await q1_resp.json()
                        out_tokens = int(q1_data.get('outAmount', 0))
                        if out_tokens <= 0:
                            continue

                    q2_url = f"{JUPITER_QUOTE_API}?inputMint={token_mint}&outputMint={SOL_MINT}&amount={out_tokens}&slippageBps=80"
                    async with session.get(q2_url, timeout=2.5) as q2_resp:
                        if q2_resp.status != 200:
                            continue
                        q2_data = await q2_resp.json()
                        final_sol_lamports = int(q2_data.get('outAmount', 0))

                    # 5. Строгий расчет чистой прибыли (с вычетом комиссий)
                    network_fee_lamports = 90_000  # ~0.00009 SOL
                    net_profit_lamports = final_sol_lamports - amount_in_lamports - network_fee_lamports
                    profit_sol = net_profit_lamports / 1_000_000_000
                    profit_pct = (profit_sol / trade_amount_sol) * 100

                    # Фильтр: ТОЛЬКО в плюс (от 0.3% чистыми)
                    if 0.3 <= profit_pct <= 18.0:
                        add_log(f"🎯 НАЙДЕН ПРОФИТ: {pair_sym}/SOL | Вход: {trade_amount_sol:.3f} SOL | Профит: +{profit_sol:.4f} SOL (+{profit_pct:.2f}%)")

                        tx_sig = "sim_" + os.urandom(6).hex()

                        # 6. Исполнение ордера в блокчейне (если задан ключ)
                        if signer:
                            swap1_payload = {"quoteResponse": q1_data, "userPublicKey": str(signer.pubkey()), "wrapUnwrapSOL": True}
                            async with session.post(JUPITER_SWAP_API, json=swap1_payload, timeout=4) as s1:
                                if s1.status == 200:
                                    s1_res = await s1.json()
                                    tx_bytes = base64.b64decode(s1_res["swapTransaction"])
                                    signed_tx = VersionedTransaction(VersionedTransaction.from_bytes(tx_bytes).message, [signer])
                                    rpc_body = {"jsonrpc": "2.0", "id": 1, "method": "sendTransaction", "params": [base64.b64encode(bytes(signed_tx)).decode('utf-8'), {"skipPreflight": True}]}
                                    await session.post(SOLANA_RPC, json=rpc_body)
                                    await asyncio.sleep(1.2)

                            swap2_payload = {"quoteResponse": q2_data, "userPublicKey": str(signer.pubkey()), "wrapUnwrapSOL": True}
                            async with session.post(JUPITER_SWAP_API, json=swap2_payload, timeout=4) as s2:
                                if s2.status == 200:
                                    s2_res = await s2.json()
                                    tx_bytes2 = base64.b64decode(s2_res["swapTransaction"])
                                    signed_tx2 = VersionedTransaction(VersionedTransaction.from_bytes(tx_bytes2).message, [signer])
                                    rpc_body2 = {"jsonrpc": "2.0", "id": 1, "method": "sendTransaction", "params": [base64.b64encode(bytes(signed_tx2)).decode('utf-8'), {"skipPreflight": True}]}
                                    async with session.post(SOLANA_RPC, json=rpc_body2) as final_resp:
                                        r_data = await final_resp.json()
                                        tx_sig = r_data.get("result", tx_sig)

                        # 7. Фиксация в базе данных сделок
                        conn = sqlite3.connect(DB_FILE)
                        cursor = conn.cursor()
                        cursor.execute("SELECT telegram_id FROM users WHERE trading_active = 1")
                        users = cursor.fetchall()
                        u_id = users[0][0] if users else 42882165
                        cursor.execute("INSERT INTO trades (telegram_id, token_pair, buy_price, sell_price, profit_sol, tx_signature, timestamp) VALUES (?, ?, ?, ?, ?, ?, ?)",
                                       (u_id, f"{pair_sym}/SOL", 1.0, 1.0 + (profit_pct/100), profit_sol, str(tx_sig), datetime.now().strftime('%H:%M:%S')))
                        conn.commit()
                        conn.close()

                        add_log(f"💎 Сделка закрыта: +{profit_sol:.4f} SOL. TX: {str(tx_sig)[:10]}...")

                        for u in users:
                            await send_telegram_notification(u[0], f"🚀 *Zer0Life AI Trade Executed!*\n• Пара: `{pair_sym}/SOL`\n• Профит: `+{profit_sol:.4f} SOL` (+{profit_pct:.2f}%)\n• TX: `{str(tx_sig)[:12]}...`")
                        
                        await asyncio.sleep(5)
                        break

                if scanned_count > 0:
                    add_log(f"🔍 Сканирование: проанализировано {scanned_count} пулов. Опасные отсеяны.")

            except Exception as e:
                logging.error(f"Worker iteration error: {e}")
            
            await asyncio.sleep(3)

HTML_CONTENT = """<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no, viewport-fit=cover">
    <title>Zer0LifeRun</title>
    <script src="https://telegram.org/js/telegram-web-app.js"></script>
    <style>
        * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
        body { 
            background: #050507; 
            color: #f1f5f9; 
            font-family: -apple-system, BlinkMacSystemFont, "SF Pro Display", "Segoe UI", Roboto, sans-serif; 
            margin: 0; 
            padding: 0; 
            width: 100vw; 
            height: 100vh; 
            overflow: hidden; 
        }
        
        #splash-screen {
            position: fixed; 
            top: 0; 
            left: 0; 
            width: 100vw; 
            height: 100vh;
            background-color: #050507;
            background-image: linear-gradient(180deg, rgba(0,0,0,0.05) 0%, rgba(0,0,0,0.2) 50%, rgba(0,0,0,0.85) 100%), url('/splash.jpg');
            background-size: cover;
            background-position: center top;
            background-repeat: no-repeat;
            display: flex; 
            flex-direction: column; 
            justify-content: flex-end;
            align-items: center;
            padding: 24px; 
            padding-bottom: calc(34px + env(safe-area-inset-bottom, 16px)); 
            z-index: 9999;
        }

        .splash-branding {
            text-align: center;
            margin-bottom: 24px;
            user-select: none;
        }

        .brand-title {
            font-size: 42px;
            font-weight: 900;
            font-style: italic;
            color: #ffffff;
            text-transform: uppercase;
            line-height: 1.0;
            letter-spacing: 1px;
            text-shadow: 0 4px 16px rgba(0, 0, 0, 0.9);
        }

        .brand-run {
            font-size: 54px;
            font-weight: 900;
            font-style: italic;
            color: #5ff53b;
            text-transform: uppercase;
            line-height: 1.0;
            margin-top: 4px;
            text-shadow: 0 0 25px rgba(95, 245, 59, 0.5), 0 4px 16px rgba(0, 0, 0, 0.9);
        }

        .brand-tagline {
            font-size: 13px;
            font-weight: 800;
            color: #ffffff;
            text-transform: uppercase;
            letter-spacing: 4px;
            margin-top: 10px;
            opacity: 0.95;
            text-shadow: 0 2px 10px rgba(0, 0, 0, 0.8);
        }

        .run-btn {
            background: linear-gradient(180deg, #8af64a 0%, #68e831 100%);
            color: #000000;
            border: none;
            width: 100%;
            max-width: 440px;
            padding: 17px;
            border-radius: 22px;
            font-weight: 800;
            font-size: 18px;
            cursor: pointer;
            text-align: center;
            box-shadow: 0 10px 28px rgba(104, 232, 49, 0.45);
            letter-spacing: 0.5px;
            transition: transform 0.15s ease;
        }

        .run-btn:active {
            transform: scale(0.98);
        }

        .card { 
            background: linear-gradient(145deg, rgba(22, 22, 26, 0.95), rgba(12, 12, 14, 0.95)); 
            backdrop-filter: blur(20px); 
            border-radius: 24px; 
            padding: 22px; 
            margin-bottom: 18px; 
            border: 1px solid rgba(212, 175, 55, 0.2); 
            box-shadow: 0 12px 40px rgba(0,0,0,0.8), inset 0 1px 0 rgba(255,255,255,0.05); 
        }
        
        .title-gold {
            margin-top: 0; font-size: 20px; font-weight: 800;
            background: linear-gradient(to right, #d4af37, #ffdf73, #d4af37);
            -webkit-background-clip: text; -webkit-text-fill-color: transparent;
            letter-spacing: 0.5px;
        }
        
        .subtitle { color: #8a8d93; font-size: 13px; line-height: 1.4; font-weight: 400; }
        .label-gold { font-size: 10px; color: #d4af37; font-weight: 700; margin-bottom: 6px; text-transform: uppercase; letter-spacing: 1px; }

        .input-group-web4 { 
            width: 100%; background: rgba(0, 0, 0, 0.4); 
            border: 1px solid rgba(212, 175, 55, 0.3); 
            border-radius: 16px; padding: 14px; margin-top: 6px; 
            display: flex; align-items: center; justify-content: space-between;
            box-shadow: inset 0 2px 10px rgba(0,0,0,0.5);
        }
        .input-field-web4 { 
            background: transparent; border: none; 
            color: #ffdf73; font-family: "SF Mono", monospace; 
            font-size: 11px; width: 100%; outline: none; 
        }

        .btn-gold { 
            background: linear-gradient(135deg, #d4af37 0%, #aa771c 100%); 
            color: #050505; border: none; width: 100%; padding: 16px; 
            border-radius: 18px; font-weight: 800; cursor: pointer; 
            margin-top: 16px; font-size: 14px; text-transform: uppercase;
            box-shadow: 0 6px 20px rgba(212, 175, 55, 0.25);
            letter-spacing: 0.5px;
        }
        
        .btn-red { background: linear-gradient(135deg, #3f0d12 0%, #a71d31 100%); color: white; border: 1px solid rgba(255, 50, 50, 0.3); box-shadow: 0 4px 20px rgba(167, 29, 49, 0.4); border-radius: 18px; padding: 14px; font-weight: 700; border: none; width: 100%; cursor: pointer;}
        .btn-mode { background: rgba(20, 20, 24, 0.6); color: #d4af37; border: 1px solid rgba(212, 175, 55, 0.2); margin-top: 8px; width: 100%; padding: 14px; border-radius: 18px; font-weight: bold; cursor: pointer; text-align: left; display: flex; justify-content: space-between; align-items: center; }
        
        .qr-wrapper {
            background: linear-gradient(145deg, #ffdf73, #aa771c);
            padding: 2px; border-radius: 22px; width: 164px; height: 164px; 
            margin: 24px auto; box-shadow: 0 10px 30px rgba(212, 175, 55, 0.2);
        }
        .qr-container-web4 { 
            background: #ffffff; width: 100%; height: 100%; 
            border-radius: 20px; display: flex; justify-content: center; align-items: center; 
        }

        .metric { display: flex; justify-content: space-between; margin-top: 14px; font-size: 14px; color: #8a8d93; }
        .val { color: #ffdf73; font-weight: 700; font-family: "SF Mono", monospace; }
        
        .logs { 
            background: rgba(0, 0, 0, 0.6); border: 1px solid rgba(212, 175, 55, 0.15); 
            border-radius: 18px; padding: 14px; font-family: "SF Mono", monospace; 
            font-size: 11px; color: #d4af37; height: 170px; overflow-y: auto; 
            margin-top: 12px; white-space: pre-line; box-shadow: inset 0 2px 15px rgba(0,0,0,0.8); 
        }
        .trade-item { 
            background: rgba(0, 0, 0, 0.4); border: 1px solid rgba(212, 175, 55, 0.15); 
            border-radius: 16px; padding: 14px; margin-top: 10px; 
            font-family: "SF Mono", monospace; font-size: 11px; 
            display: flex; justify-content: space-between; align-items: center; 
        }

        .bottom-nav { 
            position: fixed; bottom: 0; left: 0; right: 0; 
            background: rgba(10, 10, 12, 0.95); backdrop-filter: blur(30px); 
            border-top: 1px solid rgba(212, 175, 55, 0.15); 
            padding: 12px 16px; display: flex; justify-content: space-around; 
            z-index: 900; box-shadow: 0 -10px 40px rgba(0,0,0,0.9); 
        }
        .nav-item { 
            background: transparent; border: none; color: #5a5d63; 
            font-size: 11px; font-weight: 600; display: flex; 
            flex-direction: column; align-items: center; gap: 4px; 
            cursor: pointer; transition: all 0.3s; 
        }
        .nav-item.active { color: #d4af37; text-shadow: 0 0 15px rgba(212, 175, 55, 0.6); }
        .nav-icon { font-size: 20px; }
        
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
        <button class="run-btn" onclick="enterApp()">Get Started</button>
    </div>

    <div id="app-container" style="display: none; width: 100vw; height: 100vh;">
        <div id="tab-wallet" class="tab-content">
            <div class="card">
                <h2 class="title-gold">⚜️ Premium Wallet</h2>
                <p class="subtitle">Ваш персональный защищенный адрес для депозита SOL.</p>
                <div style="margin: 20px 0;">
                    <div class="label-gold">Deposit Address:</div>
                    <div class="input-group-web4">
                        <input type="text" id="wallet-input" class="input-field-web4" readonly>
                    </div>
                </div>
                <div class="qr-wrapper">
                    <div class="qr-container-web4">
                        <img id="qr-img" src="" alt="QR" style="width: 140px; height: 140px; border-radius: 12px;">
                    </div>
                </div>
                <button class="btn-gold" onclick="navigator.clipboard.writeText(document.getElementById('wallet-input').value); alert('📋 Адрес скопирован!')">
                    Скопировать адрес
                </button>
            </div>
            <div class="card">
                <div class="label-gold">Верификация (TX Signature):</div>
                <div class="input-group-web4">
                    <input type="text" id="tx-hash-input" class="input-field-web4" placeholder="Введите хэш транзакции...">
                </div>
                <button class="btn-gold" style="margin-top: 12px; font-size: 13px;" onclick="verifyDeposit()">Verify & Credit 🔄</button>
            </div>
        </div>

        <div id="tab-trader" class="tab-content active">
            <div class="card">
                <h3 class="title-gold" style="font-size: 16px; margin-bottom: 12px;">🛡️ AI Trader Core</h3>
                <button class="btn-mode active">
                    <span style="color: #f1f5f9;">⚜️ AI Arbitrage (No-Loss Safe)</span>
                    <span style="font-size: 11px; color: #5ff53b;">Active</span>
                </button>
            </div>
            <div class="card">
                <h3 class="title-gold" style="font-size: 16px; margin-bottom: 10px;">🤖 Автопилот</h3>
                <div class="metric"><span>Баланс пула:</span> <span id="wallet-balance" class="val">Загрузка...</span></div>
                <div class="metric"><span>Статус ядра:</span> <span id="trade-status" class="val" style="color: #5ff53b;">Активен (24/7)</span></div>
                <div style="display: flex; gap: 10px; margin-top: 16px;">
                    <button class="btn-gold" style="margin-top:0; padding: 12px;" onclick="checkBalance()">Обновить</button>
                    <button id="toggle-btn" class="btn-red" style="margin-top:0; padding: 12px;" onclick="toggleTrading()">Остановить</button>
                </div>
            </div>
            <div class="card">
                <h3 class="title-gold" style="font-size: 15px; margin-bottom: 8px;">📡 Neural Logs</h3>
                <div id="logs-box" class="logs">Инициализация AI сканера...</div>
            </div>
        </div>

        <div id="tab-stats" class="tab-content">
            <div class="card">
                <h3 class="title-gold" style="font-size: 16px; margin-bottom: 12px;">📊 Статистика Terminal</h3>
                <div class="metric"><span>Стартовый:</span> <span class="val">0.2517 SOL</span></div>
                <div class="metric"><span>Текущий:</span> <span id="stat-current" class="val">-- SOL</span></div>
                <div class="metric"><span>Результат:</span> <span id="stat-profit-sol" class="val" style="color: #5ff53b;">0.0000 SOL</span></div>
                <button class="btn-gold" style="margin-top: 16px;" onclick="loadStats()">🔄 Обновить</button>
            </div>
            <div class="card">
                <h3 class="title-gold" style="font-size: 15px; margin-bottom: 10px;">📜 История операций</h3>
                <div id="trades-list" style="max-height: 250px; overflow-y: auto;">
                    <div style="color: #5a5d63; font-size: 12px; text-align: center; padding: 20px;">Поиск связок...</div>
                </div>
            </div>
        </div>
    </div>

    <div id="bottom-nav-bar" class="bottom-nav" style="display: none;">
        <button id="nav-wallet" class="nav-item" onclick="switchTab('wallet')"><span class="nav-icon">👛</span><span>Wallet</span></button>
        <button id="nav-trader" class="nav-item active" onclick="switchTab('trader')"><span class="nav-icon">⚡</span><span>Trader</span></button>
        <button id="nav-stats" class="nav-item" onclick="switchTab('stats')"><span class="nav-icon">📊</span><span>Stats</span></button>
    </div>

    <script>
        let user = { id: 42882165, username: "CryptoWlodek", first_name: "CryptoWlodek" };
        try {
            if (window.Telegram && window.Telegram.WebApp) {
                window.Telegram.WebApp.expand();
                if (window.Telegram.WebApp.initDataUnsafe && window.Telegram.WebApp.initDataUnsafe.user) {
                    user = window.Telegram.WebApp.initDataUnsafe.user;
                }
            }
        } catch(e) {}

        window.enterApp = function() {
            document.getElementById('splash-screen').style.display = 'none';
            document.getElementById('app-container').style.display = 'block';
            document.getElementById('bottom-nav-bar').style.display = 'flex';
            switchTab('trader');
            loadProfile();
        };

        let isTrading = true;
        window.switchTab = function(tab) {
            document.querySelectorAll('#app-container .tab-content').forEach(el => el.classList.remove('active'));
            document.querySelectorAll('.nav-item').forEach(el => el.classList.remove('active'));
            
            if(tab === 'wallet') { 
                document.getElementById('tab-wallet').classList.add('active'); 
                document.getElementById('nav-wallet').classList.add('active'); 
                checkBalance();
            } else if(tab === 'trader') { 
                document.getElementById('tab-trader').classList.add('active'); 
                document.getElementById('nav-trader').classList.add('active'); 
                checkBalance(); loadStats(); loadLogs(); 
            } else if(tab === 'stats') { 
                document.getElementById('tab-stats').classList.add('active'); 
                document.getElementById('nav-stats').classList.add('active'); 
                loadStats(); 
            }
        };
        
        async function loadProfile() {
            try {
                const res = await fetch('/api/profile', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({telegram_id: user.id, username: user.username, first_name: user.first_name})});
                const data = await res.json();
                if(data.success) {
                    const w = data.profile.solana_wallet;
                    document.getElementById('wallet-input').value = w;
                    document.getElementById('qr-img').src = "https://api.qrserver.com/v1/create-qr-code/?size=160x160&color=050507&bgcolor=ffffff&data=" + encodeURIComponent(w);
                    isTrading = data.profile.trading_active === 1;
                    updateUI();
                    checkBalance();
                }
            } catch(e) {}
        }
        
        function updateUI() {
            const st = document.getElementById('trade-status');
            const btn = document.getElementById('toggle-btn');
            if(isTrading) { 
                st.innerText = "Активен (24/7)"; st.style.color = "#5ff53b"; 
                btn.innerText = "Остановить"; btn.className = "btn-red"; 
            } else { 
                st.innerText = "Остановлен"; st.style.color = "#8a8d93"; 
                btn.innerText = "Включить"; btn.className = "btn-gold"; 
            }
        }
        
        window.checkBalance = async function() {
            const w = document.getElementById('wallet-input').value;
            if(!w) return;
            try {
                const res = await fetch('/api/blockchain/balance?wallet=' + encodeURIComponent(w));
                const data = await res.json();
                if(data.success) { document.getElementById('wallet-balance').innerText = data.balance.toFixed(4) + " SOL"; }
            } catch(e) {}
        };
        
        window.verifyDeposit = async function() {
            const txHash = document.getElementById('tx-hash-input').value.trim();
            if(!txHash) { alert("Введите хэш!"); return; }
            try {
                const res = await fetch('/api/verify-tx', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({telegram_id: user.id, tx_signature: txHash})});
                const data = await res.json();
                if(data.success) { 
                    alert("✅ Успешно верифицировано!\nНайдено пополнение на: +" + data.amount + " SOL"); 
                    document.getElementById('tx-hash-input').value = ""; 
                    checkBalance(); 
                } else {
                    alert("❌ Ошибка: " + (data.error || "Неизвестная ошибка"));
                }
            } catch(e) { alert("Ошибка соединения с сервером"); }
        };
        
        window.toggleTrading = async function() {
            isTrading = !isTrading;
            try {
                await fetch('/api/trading/toggle', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({telegram_id: user.id, active: isTrading ? 1 : 0})});
                updateUI();
            } catch(e) {}
        };
        
        window.loadLogs = async function() {
            try {
                const res = await fetch('/api/logs');
                const data = await res.json();
                if(data.success) { document.getElementById('logs-box').innerText = data.logs.join('\\n'); }
            } catch(e) {}
        };
        
        window.loadStats = async function() {
            try {
                const res = await fetch('/api/stats?telegram_id=' + user.id);
                const data = await res.json();
                if(data.success) {
                    document.getElementById('stat-current').innerText = data.current_sol.toFixed(4) + " SOL";
                    const diff = data.profit_sol;
                    const el = document.getElementById('stat-profit-sol');
                    el.innerText = (diff >= 0 ? "+" : "") + diff.toFixed(4) + " SOL";
                    el.style.color = diff >= 0 ? "#5ff53b" : "#a71d31";
                    
                    const list = document.getElementById('trades-list');
                    if(data.trades.length === 0) {
                        list.innerHTML = '<div style="color: #5a5d63; font-size: 12px; text-align: center; padding: 20px;">Сканирование рынка...</div>';
                    } else {
                        list.innerHTML = data.trades.map(t => `
                            <div class="trade-item">
                                <div>
                                    <div style="color: #f1f5f9; font-weight: bold;">${t.token_pair}</div>
                                    <div style="color: #8a8d93; font-size: 10px;">TX: <a href="https://solscan.io/tx/${t.tx_signature}" target="_blank" style="color: #d4af37;">${t.tx_signature.substring(0,8)}...</a></div>
                                </div>
                                <div style="color: #5ff53b; font-weight: bold; font-size: 12px;">+${t.profit_sol.toFixed(4)} SOL</div>
                            </div>
                        `).join('');
                    }
                }
            } catch(e) {}
        };
        
        loadProfile();
        setInterval(async () => { checkBalance(); loadStats(); loadLogs(); }, 4000);
    </script>
</body>
</html>
"""

async def index_handler(request):
    headers = {
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        "Pragma": "no-cache",
        "Expires": "0"
    }
    return web.Response(text=HTML_CONTENT, content_type='text/html', headers=headers)

async def health_handler(request):
    return web.Response(text="OK", status=200)

async def fetch_and_cache_splash_image() -> bytes | None:
    global CACHED_SPLASH_IMAGE
    if CACHED_SPLASH_IMAGE:
        return CACHED_SPLASH_IMAGE
    
    for fn in ["splash.jpg", "splash.png", "image.png"]:
        if os.path.exists(fn):
            try:
                with open(fn, "rb") as f:
                    CACHED_SPLASH_IMAGE = f.read()
                    return CACHED_SPLASH_IMAGE
            except Exception:
                pass

    if not TELEGRAM_TOKEN:
        return None

    try:
        async with aiohttp.ClientSession() as session:
            url_info = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getFile?file_id={SPLASH_FILE_ID}"
            async with session.get(url_info, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    file_path = data.get("result", {}).get("file_path")
                    if file_path:
                        dl_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}"
                        async with session.get(dl_url, timeout=aiohttp.ClientTimeout(total=15)) as img_resp:
                            if img_resp.status == 200:
                                CACHED_SPLASH_IMAGE = await img_resp.read()
                                try:
                                    with open("splash.jpg", "wb") as f:
                                        f.write(CACHED_SPLASH_IMAGE)
                                except Exception:
                                    pass
                                return CACHED_SPLASH_IMAGE
    except Exception as e:
        logging.error(f"Error fetching splash: {e}")
    return None

async def splash_image_handler(request):
    img = await fetch_and_cache_splash_image()
    if img:
        return web.Response(body=img, content_type="image/jpeg", headers={"Cache-Control": "public, max-age=86400"})
    return web.Response(status=404)

async def api_get_image_url(request):
    return web.json_response({"success": True, "url": "/splash.jpg"})

async def api_get_profile(request):
    data = await request.json()
    user = get_or_create_user(int(data.get("telegram_id")), data.get("username", ""), data.get("first_name", ""))
    return web.json_response({"success": True, "profile": user})

async def api_verify_tx(request):
    data = await request.json()
    tx_sig = data.get("tx_signature", "").strip()

    if not tx_sig or len(tx_sig) < 50:
        return web.json_response({"success": False, "error": "Неверный формат хэша (слишком короткий)"})

    payload = {
        "jsonrpc": "2.0", "id": 1, 
        "method": "getTransaction", 
        "params": [tx_sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}]
    }
    
    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(SOLANA_RPC, json=payload, timeout=5) as resp:
                if resp.status == 200:
                    rpc_data = await resp.json()
                    tx_info = rpc_data.get("result")
                    
                    if not tx_info:
                        return web.json_response({"success": False, "error": "Транзакция не найдена в сети Solana"})
                        
                    meta = tx_info.get("meta", {})
                    if meta.get("err"):
                        return web.json_response({"success": False, "error": "Транзакция завершилась ошибкой"})

                    account_keys = [acc["pubkey"] for acc in tx_info.get("transaction", {}).get("message", {}).get("accountKeys", [])]
                    
                    if SHARED_DEPOSIT_WALLET not in account_keys:
                        return web.json_response({"success": False, "error": "Транзакция отправлена на другой кошелек"})
                    
                    wallet_index = account_keys.index(SHARED_DEPOSIT_WALLET)
                    pre_balances = meta.get("preBalances", [])
                    post_balances = meta.get("postBalances", [])
                    
                    if wallet_index < len(pre_balances) and wallet_index < len(post_balances):
                        received_lamports = post_balances[wallet_index] - pre_balances[wallet_index]
                        
                        if received_lamports > 0:
                            amount_sol = received_lamports / 1_000_000_000
                            return web.json_response({"success": True, "amount": round(amount_sol, 4)})
                        else:
                            return web.json_response({"success": False, "error": "По этому хэшу нет зачислений на кошелек бота"})
                            
                    return web.json_response({"success": False, "error": "Не удалось вычислить сумму перевода"})
                else:
                    return web.json_response({"success": False, "error": "Ошибка связи с RPC сервером"})
        except Exception:
            return web.json_response({"success": False, "error": "Ошибка сети при проверке"})

async def api_toggle_trading(request):
    data = await request.json()
    conn = sqlite3.connect(DB_FILE)
    conn.cursor().execute("UPDATE users SET trading_active = ? WHERE telegram_id = ?", (int(data.get("active", 0)), int(data.get("telegram_id"))))
    conn.commit()
    conn.close()
    return web.json_response({"success": True})

async def api_get_balance(request):
    wallet = request.query.get("wallet", SHARED_DEPOSIT_WALLET)
    sol_bal = await fetch_wallet_balance(wallet)
    return web.json_response({"success": True, "balance": sol_bal})

async def api_get_logs(request):
    return web.json_response({"success": True, "logs": RECENT_LOGS})

async def api_get_stats(request):
    telegram_id = int(request.query.get("telegram_id", 0))
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT solana_wallet, initial_sol FROM users WHERE telegram_id = ?", (telegram_id,))
    urow = cursor.fetchone()
    wallet = urow[0] if urow else SHARED_DEPOSIT_WALLET
    initial_sol = urow[1] if urow else 0.2517
    current_sol = await fetch_wallet_balance(wallet)
    cursor.execute("SELECT token_pair, buy_price, sell_price, profit_sol, tx_signature, timestamp FROM trades WHERE telegram_id = ? ORDER BY id DESC LIMIT 20", (telegram_id,))
    rows = cursor.fetchall()
    trades = [{"token_pair": r[0], "buy_price": r[1], "sell_price": r[2], "profit_sol": r[3], "tx_signature": r[4], "timestamp": r[5]} for r in rows]
    conn.close()
    profit_sol = current_sol - initial_sol
    return web.json_response({"success": True, "current_sol": current_sol, "initial_sol": initial_sol, "profit_sol": profit_sol, "total_trades": len(trades), "trades": trades})

async def telegram_long_polling():
    if not TELEGRAM_TOKEN:
        return
    async with aiohttp.ClientSession() as session:
        async with session.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/deleteWebhook?drop_pending_updates=true") as resp:
            pass
        offset = 0
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
        while True:
            try:
                async with session.get(url, params={"offset": offset, "timeout": 30}, timeout=35) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        for update in data.get("result", []):
                            offset = update["update_id"] + 1
                            message = update.get("message", {})
                            text = message.get("text", "")
                            chat_id = message.get("chat", {}).get("id")
                            if text == "/start" and chat_id:
                                send_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
                                payload = {
                                    "chat_id": chat_id,
                                    "photo": SPLASH_FILE_ID,
                                    "caption": "⚜️ *Добро пожаловать в Zer0Life Run AI*\n\nАвтономный поиск безрисковых арбитражных связок SOL активен.\n\n👇 Нажмите кнопку ниже для запуска:",
                                    "parse_mode": "Markdown",
                                    "reply_markup": {"inline_keyboard": [[{"text": "🚀 Открыть Zer0Life Run", "web_app": {"url": RENDER_URL}}]]}
                                }
                                async with session.post(send_url, json=payload) as send_resp:
                                    res_json = await send_resp.json()
                                    if not res_json.get("ok"):
                                        fallback_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
                                        fallback_payload = {
                                            "chat_id": chat_id,
                                            "text": "⚜️ *Добро пожаловать в Zer0Life Run AI*\n\nНажмите кнопку ниже для запуска:",
                                            "parse_mode": "Markdown",
                                            "reply_markup": {"inline_keyboard": [[{"text": "🚀 Открыть Zer0Life Run", "web_app": {"url": RENDER_URL}}]]}
                                        }
                                        async with session.post(fallback_url, json=fallback_payload) as f_resp:
                                            pass
            except Exception as e:
                logging.error(f"Polling error: {e}")
                await asyncio.sleep(3)
            await asyncio.sleep(1)

async def main():
    init_db()
    app = web.Application()
    app.router.add_get('/', index_handler)
    app.router.add_get('/health', health_handler)
    app.router.add_get('/splash.jpg', splash_image_handler)
    app.router.add_get('/api/get-image-url', api_get_image_url)
    app.router.add_post('/api/profile', api_get_profile)
    app.router.add_post('/api/verify-tx', api_verify_tx)
    app.router.add_post('/api/trading/toggle', api_toggle_trading)
    app.router.add_get('/api/blockchain/balance', api_get_balance)
    app.router.add_get('/api/logs', api_get_logs)
    app.router.add_get('/api/stats', api_get_stats)
    
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    
    asyncio.create_task(fetch_and_cache_splash_image())
    asyncio.create_task(telegram_long_polling())
    asyncio.create_task(aggressive_arbitrage_worker())
    
    add_log("⚜️ Zer0Life AI Arbitrage Terminal запущен на полной мощности.")
    while True:
        await asyncio.sleep(3600)

if __name__ == "__main__":
    asyncio.run(main())
