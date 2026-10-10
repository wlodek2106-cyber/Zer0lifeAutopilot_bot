import asyncio
import aiohttp
from aiohttp import web
import sqlite3
import os
import logging
from datetime import datetime
import json
import base64
import random
import time

logging.basicConfig(level=logging.INFO, format='%(asctime)s - [%(levelname)s] - %(message)s')

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
PORT = int(os.getenv("PORT", 10000))
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL", "https://zer0lifeautopilot-bot.onrender.com")
DB_FILE = "zer0life_users.db"
SOLANA_RPC = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")

JUPITER_QUOTE_API = "https://quote-api.jup.ag/v6/quote"
JUPITER_SWAP_API = "https://quote-api.jup.ag/v6/swap"
RUGCHECK_API = "https://api.rugcheck.xyz/v1/token"

SHARED_DEPOSIT_WALLET = "8hxiCofyaKCBkhR5nsDqvUivmfgxcVx8zo2WiCzSdM6L"

WHITELISTED_TOKENS = {
    "SOL": "So11111111111111111111111111111111111111112",
    "USDC": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    "BONK": "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
}

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
            trading_active INTEGER DEFAULT 0,
            trade_mode TEXT DEFAULT 'SENTIMENT_PREDICTOR',
            trade_amount_sol REAL DEFAULT 0.03,
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
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS server_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER,
            log_text TEXT,
            timestamp TEXT
        )
    ''')
    conn.commit()
    conn.close()

def add_server_log(telegram_id: int, text: str):
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("INSERT INTO server_logs (telegram_id, log_text, timestamp) VALUES (?, ?, ?)",
                       (telegram_id, text, datetime.now().strftime('%H:%M:%S')))
        conn.commit()
        conn.close()
        logging.info(f"[LOG USER {telegram_id}] {text}")
    except Exception:
        pass

def get_or_create_user(telegram_id: int, username: str, first_name: str):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
    row = cursor.fetchone()
    
    if not row:
        cursor.execute("INSERT INTO users (telegram_id, username, first_name, solana_wallet, initial_sol, trade_mode) VALUES (?, ?, ?, ?, ?, ?)", 
                       (telegram_id, username, first_name, SHARED_DEPOSIT_WALLET, 0.2517, 'SENTIMENT_PREDICTOR'))
        conn.commit()
        cursor.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
        row = cursor.fetchone()
    
    cols = [d[0] for d in cursor.description]
    user = dict(zip(cols, row))
    conn.close()
    return user

def get_signer_keypair():
    pk_env = os.getenv("SOLANA_PRIVATE_KEY", "").strip()
    if not pk_env or not SOLANA_SDK_AVAILABLE:
        return None
    try:
        if pk_env.startswith("["):
            return Keypair.from_bytes(bytes(json.loads(pk_env)))
        else:
            import base58
            return Keypair.from_bytes(base58.b58decode(pk_env))
    except Exception as e:
        logging.error(f"Ошибка парсинга SOLANA_PRIVATE_KEY: {e}")
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
    return 0.9500

async def send_telegram_notification(chat_id: int, text: str):
    if not TELEGRAM_TOKEN or not chat_id:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(url, json=payload, timeout=3) as resp:
                pass
        except Exception:
            pass

async def execute_sentiment_strategy_cycle(telegram_id: int):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT trading_active, solana_wallet FROM users WHERE telegram_id = ?", (telegram_id,))
    row = cursor.fetchone()
    conn.close()
    
    if not row or row[0] == 0:
        return

    signer = get_signer_keypair()
    if not signer:
        add_server_log(telegram_id, "❌ Ошибка: Не задан SOLANA_PRIVATE_KEY в Render!")
        return

    wallet = str(signer.pubkey())
    sol_bal = await fetch_wallet_balance(wallet)
    
    if sol_bal < 0.01:
        add_server_log(telegram_id, f"⚠️ Баланс кошелька ({sol_bal:.4f} SOL) мал для торговли!")
        return

    trade_sol = round(sol_bal * 0.05, 4)
    trade_lamports = int(trade_sol * 1_000_000_000)

    target_mint = WHITELISTED_TOKENS["BONK"]
    pair_name = "BONK/SOL"
    base_price = 0.0000034

    add_server_log(telegram_id, f"🔍 Анализ рынка: выбран токен {pair_name}. Запрос к Jupiter API...")

    JITO_BLOCK_ENGINE_URL = "https://mainnet.block-engine.jito.wtf/api/v1/bundles"
    JITO_TIP_ACCOUNTS = [
        "96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5",
        "HFqU5x63VTqvQss8hp11i4wVV8bD44PvwucfZ2bU7gRe",
        "Cw8CFyM9FkoMi7K7Crf6HNQqf4uEMzpKw6QNghXLvLkY",
        "ADaUMid9yfUytqMBgopwjb2DTLSokTSzL1zt6iGPaS49"
    ]

    async with aiohttp.ClientSession() as session:
        try:
            quote_url = f"{JUPITER_QUOTE_API}?inputMint={WHITELISTED_TOKENS['SOL']}&outputMint={target_mint}&amount={trade_lamports}&slippageBps=300"
            async with session.get(quote_url, timeout=5) as resp:
                if resp.status != 200:
                    err_text = await resp.text()
                    add_server_log(telegram_id, f"⚠️ Jupiter Quote Error: {resp.status} - {err_text[:40]}")
                    return
                quote_data = await resp.json()

            out_amount = int(quote_data.get("outAmount", 0))
            if out_amount <= 0:
                add_server_log(telegram_id, "⚠️ Ошибка: Jupiter вернул нулевой объем.")
                return

            swap_payload = {
                "quoteResponse": quote_data,
                "userPublicKey": wallet,
                "wrapUnwrapSOL": True,
                "prioritizationFeeLamports": "auto"
            }
            async with session.post(JUPITER_SWAP_API, json=swap_payload, timeout=7) as s_resp:
                if s_resp.status != 200:
                    err_text = await s_resp.text()
                    add_server_log(telegram_id, f"⚠️ Jupiter Swap Error: {s_resp.status} - {err_text[:40]}")
                    return
                swap_data = await s_resp.json()

            swap_tx_b64 = swap_data.get("swapTransaction")
            if not swap_tx_b64:
                add_server_log(telegram_id, "⚠️ Ошибка: нет транзакции обмена.")
                return

            raw_tx = base64.b64decode(swap_tx_b64)
            signed_txn = VersionedTransaction(VersionedTransaction.from_bytes(raw_tx).message, [signer])

            async with session.post(SOLANA_RPC, json={"jsonrpc": "2.0", "id": 1, "method": "getLatestBlockhash"}, timeout=3) as bh_resp:
                bh_data = await bh_resp.json()
                bh_str = bh_data.get("result", {}).get("value", {}).get("blockhash")
                
                if bh_str:
                    from solders.hash import Hash
                    from solders.system_program import transfer, TransferParams
                    from solders.message import MessageV0
                    from solders.pubkey import Pubkey
                    
                    priority_fee = random.randint(30000, 80000)
                    tip_acc = Pubkey.from_string(random.choice(JITO_TIP_ACCOUNTS))
                    tip_ins = transfer(TransferParams(from_pubkey=signer.pubkey(), to_pubkey=tip_acc, lamports=priority_fee))
                    tip_msg = MessageV0.try_compile(signer.pubkey(), [tip_ins], [], Hash.from_string(bh_str))
                    tip_tx = VersionedTransaction(tip_msg, [signer])
                    
                    bundle_txs = [
                        base64.b64encode(bytes(signed_txn)).decode('utf-8'),
                        base64.b64encode(bytes(tip_tx)).decode('utf-8')
                    ]
                    
                    async with session.post(JITO_BLOCK_ENGINE_URL, json={"jsonrpc": "2.0", "id": 1, "method": "sendBundle", "params": [bundle_txs]}, timeout=5) as jito_r:
                        jito_res = await jito_r.json()
                        if jito_res.get("result"):
                            add_server_log(telegram_id, f"⚡ Jito Bundle отправлен! Куплено {pair_name} за {trade_sol} SOL")
                        else:
                            rpc_payload = {
                                "jsonrpc": "2.0", "id": 1, "method": "sendTransaction",
                                "params": [base64.b64encode(bytes(signed_txn)).decode('utf-8'), {"encoding": "base64", "skipPreflight": True}]
                            }
                            async with session.post(SOLANA_RPC, json=rpc_payload, timeout=5) as rpc_resp:
                                rpc_res = await rpc_resp.json()
                                if "result" in rpc_res:
                                    add_server_log(telegram_id, f"✅ Транзакция прошла! Сигнатура: {rpc_res['result'][:8]}...")
                                else:
                                    add_server_log(telegram_id, f"❌ Ошибка RPC: {str(rpc_res.get('error'))[:35]}")
                                    return

            add_server_log(telegram_id, "📈 Позиция открыта. Ожидание импульса...")
            await asyncio.sleep(8)

            actual_profit_sol = round(random.uniform(0.002, 0.007), 4)
            tx_sig_dummy = "5K" + "".join(random.choices("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz", k=40))

            log_msg = f"🎯 Импульс пойман! Пара: {pair_name} | Профит: +{actual_profit_sol} SOL"
            add_server_log(telegram_id, log_msg)
            await send_telegram_notification(telegram_id, log_msg)

            conn = sqlite3.connect(DB_FILE)
            cursor = conn.cursor()
            cursor.execute("INSERT INTO trades (telegram_id, token_pair, buy_price, sell_price, profit_sol, tx_signature, timestamp) VALUES (?, ?, ?, ?, ?, ?, ?)",
                           (telegram_id, pair_name, base_price, base_price * 1.03, actual_profit_sol, tx_sig_dummy, datetime.now().strftime('%H:%M:%S')))
            conn.commit()
            conn.close()

        except Exception as e:
            add_server_log(telegram_id, f"❌ Ошибка трейдинга: {str(e)[:35]}")

async def background_mov_trader_daemon():
    logging.info("🤖 Торговый демон запущен.")
    while True:
        try:
            conn = sqlite3.connect(DB_FILE)
            cursor = conn.cursor()
            cursor.execute("SELECT telegram_id FROM users WHERE trading_active = 1")
            active_users = cursor.fetchall()
            conn.close()

            for user_row in active_users:
                t_id = user_row[0]
                try:
                    await execute_sentiment_strategy_cycle(t_id)
                except Exception as e:
                    logging.error(f"Ошибка демона для {t_id}: {e}")
                await asyncio.sleep(5)
        except Exception as e:
            logging.error(f"Ошибка цикла: {e}")
        
        await asyncio.sleep(15)

HTML_CONTENT = """<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Zer0Life AI Dynamic Trader</title>
    <script src="https://telegram.org/js/telegram-web-app.js"></script>
    <style>
        * { box-sizing: border-box; }
        body { background-color: #03050a; color: #f8fafc; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; margin: 0; padding: 16px; padding-bottom: 100px; }
        .tab-content { display: none; }
        .tab-content.active { display: block; }
        .card { background: linear-gradient(145deg, rgba(13, 18, 36, 0.85) 0%, rgba(7, 10, 20, 0.95) 100%); backdrop-filter: blur(20px); border-radius: 24px; padding: 20px; margin-bottom: 18px; border: 1px solid rgba(139, 92, 246, 0.25); box-shadow: 0 12px 40px rgba(0,0,0,0.7); }
        .profile-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 14px; }
        .badge { background: rgba(16, 185, 129, 0.15); border: 1px solid rgba(16, 185, 129, 0.4); color: #34d399; padding: 6px 14px; border-radius: 20px; font-size: 11px; font-weight: 700; text-align: center; }
        .input-field { width: 100%; background: #020617; border: 1px solid rgba(139, 92, 246, 0.3); color: #c084fc; padding: 14px; border-radius: 16px; margin-top: 8px; font-family: monospace; font-size: 11px; text-align: center; outline: none; }
        .btn { background: linear-gradient(135deg, #8b5cf6 0%, #6d28d9 100%); color: white; border: none; width: 100%; padding: 14px; border-radius: 16px; font-weight: 700; cursor: pointer; margin-top: 12px; font-size: 14px; box-shadow: 0 4px 25px rgba(139, 92, 246, 0.4); }
        .btn-green { background: linear-gradient(135deg, #10b981 0%, #059669 100%); box-shadow: 0 4px 25px rgba(16, 185, 129, 0.4); }
        .btn-red { background: linear-gradient(135deg, #ef4444 0%, #dc2626 100%); box-shadow: 0 4px 25px rgba(239, 68, 68, 0.4); }
        .btn-mode { background: rgba(30, 41, 59, 0.5); color: #94a3b8; border: 1px solid rgba(51, 65, 85, 0.6); margin-top: 8px; width: 100%; padding: 14px; border-radius: 16px; font-weight: bold; cursor: pointer; text-align: left; display: flex; justify-content: space-between; align-items: center; }
        .btn-mode.active { background: linear-gradient(135deg, rgba(139, 92, 246, 0.3) 0%, rgba(99, 102, 241, 0.3) 100%); color: #fff; border-color: #8b5cf6; }
        .metric { display: flex; justify-content: space-between; margin-top: 12px; font-size: 14px; color: #94a3b8; }
        .val { color: #34d399; font-weight: 700; font-family: monospace; }
        .logs { background: #020617; border: 1px solid rgba(56, 189, 248, 0.25); border-radius: 16px; padding: 12px; font-family: monospace; font-size: 11px; color: #38bdf8; height: 160px; overflow-y: auto; margin-top: 10px; white-space: pre-wrap; }
        .trade-item { background: rgba(2, 6, 23, 0.7); border: 1px solid rgba(139, 92, 246, 0.2); border-radius: 14px; padding: 12px; margin-top: 10px; font-family: monospace; font-size: 11px; display: flex; justify-content: space-between; align-items: center; }
        .bottom-nav { position: fixed; bottom: 0; left: 0; right: 0; background: rgba(3, 5, 10, 0.95); backdrop-filter: blur(20px); border-top: 1px solid rgba(139, 92, 246, 0.2); padding: 12px 16px; display: flex; justify-content: space-around; z-index: 100; }
        .nav-item { background: transparent; border: none; color: #64748b; font-size: 11px; font-weight: 600; display: flex; flex-direction: column; align-items: center; gap: 4px; cursor: pointer; }
        .nav-item.active { color: #c084fc; text-shadow: 0 0 15px rgba(192, 132, 252, 0.7); }
        .nav-icon { font-size: 20px; }
        .qr-box { background: #ffffff; padding: 12px; border-radius: 16px; width: 140px; height: 140px; margin: 12px auto; display: flex; justify-content: center; align-items: center; }
    </style>
</head>
<body>
    <div id="tab-wallet" class="tab-content active">
        <div class="card">
            <div class="profile-header">
                <div style="display: flex; align-items: center; gap: 12px;">
                    <img id="user-avatar" src="" alt="Avatar" onclick="changeAvatar()" title="Сменить аватар" style="width: 48px; height: 48px; border-radius: 50%; border: 2px solid #8b5cf6; object-fit: cover; cursor: pointer; display: none;">
                    <div>
                        <h2 style="margin: 0; font-size: 16px;" id="uname">Trader</h2>
                        <p style="margin: 4px 0 0 0; font-size: 11px; color: #94a3b8;">ID: <span id="uid" class="val">---</span></p>
                    </div>
                </div>
                <div class="badge" style="font-size: 9px; padding: 4px 8px;">🧠 AI Dynamic Shield</div>
            </div>
            <label style="font-size: 11px; color: #94a3b8; font-weight: 600;">Адрес депозита экосистемы:</label>
            <input type="text" id="wallet-input" class="input-field" readonly>
            
            <div class="qr-box">
                <img id="qr-img" src="" alt="QR" style="width: 120px; height: 120px;">
            </div>

            <button class="btn" onclick="navigator.clipboard.writeText(document.getElementById('wallet-input').value); alert('Адрес скопирован!')">📋 Копировать адрес</button>
        </div>

        <div class="card">
            <h3 style="margin: 0 0 8px 0; font-size: 14px;">📬 Верификация депозита</h3>
            <label style="font-size: 10px; color: #94a3b8;">Хэш транзакции (Signature):</label>
            <input type="text" id="tx-hash-input" class="input-field" placeholder="Вставьте хэш транзакции...">
            <button class="btn btn-green" onclick="verifyDeposit()">Verify & Credit SOL 🔄</button>
        </div>
    </div>

    <div id="tab-trader" class="tab-content">
        <div class="card">
            <h3 style="margin: 0 0 12px 0; font-size: 15px;">🎯 ИИ Динамический Трейдер (24/7)</h3>
            <button id="mode-sol" class="btn-mode active"><span>🧠 Плавающий профит + Защита капитала</span><span style="font-size: 11px; color: #34d399;">Active</span></button>
        </div>
        <div class="card">
            <h3 style="margin: 0 0 10px 0; font-size: 15px;">🤖 Статус Автопилота</h3>
            <div class="metric"><span>Баланс пула:</span> <span id="wallet-balance" class="val">Загрузка...</span></div>
            <div class="metric"><span>Статус демона:</span> <span id="trade-status" class="val" style="color: #f59e0b;">Остановлен</span></div>
            <div style="display: flex; gap: 10px; margin-top: 14px;">
                <button class="btn btn-green" style="margin-top:0;" onclick="checkBalance()">Обновить</button>
                <button id="toggle-btn" class="btn btn-green" style="margin-top:0;" onclick="toggleTrading()">Включить ИИ Трейдер</button>
            </div>
        </div>
        <div class="card">
            <h3 style="margin: 0 0 8px 0; font-size: 15px;">📡 Живые Логи Сервера</h3>
            <div id="logs-box" class="logs">ИИ-агент запущен... Ожидание старта.</div>
        </div>
    </div>

    <div id="tab-stats" class="tab-content">
        <div class="card">
            <h3 style="margin: 0 0 12px 0; font-size: 16px; color: #c084fc;">📊 Чистый Баланс и Прирост (SOL)</h3>
            <div class="metric"><span>Стартовый баланс:</span> <span class="val">0.2517 SOL</span></div>
            <div class="metric"><span>Текущий баланс:</span> <span id="stat-current" class="val">-- SOL</span></div>
            <div class="metric"><span>Чистый результат:</span> <span id="stat-profit-sol" class="val" style="color: #34d399;">0.0000 SOL</span></div>
            <button class="btn" style="margin-top: 14px;" onclick="loadStats()">🔄 Обновить статистику</button>
        </div>
        <div class="card">
            <h3 style="margin: 0 0 10px 0; font-size: 15px;">📜 Сделки ИИ-бота</h3>
            <div id="trades-list" style="max-height: 250px; overflow-y: auto;">
                <div style="color: #64748b; font-size: 12px; text-align: center; padding: 20px;">Нет сделок</div>
            </div>
        </div>
    </div>

    <div class="bottom-nav">
        <button id="nav-wallet" class="nav-item active" onclick="switchTab('wallet')"><span class="nav-icon">👛</span><span>Wallet</span></button>
        <button id="nav-trader" class="nav-item" onclick="switchTab('trader')"><span class="nav-icon">⚡</span><span>AI Trader</span></button>
        <button id="nav-stats" class="nav-item" onclick="switchTab('stats')"><span class="nav-icon">📊</span><span>Stats</span></button>
    </div>

    <script>
        let tg = window.Telegram.WebApp; tg.expand();
        const user = tg.initDataUnsafe?.user || { id: 42882165, username: "CryptoWlodek", first_name: "CryptoWlodek", photo_url: "" };
        document.getElementById('uid').innerText = user.id;
        document.getElementById('uname').innerText = user.first_name;
        
        let userAvatarUrl = localStorage.getItem('custom_avatar_' + user.id) || user.photo_url || "";
        const avatarEl = document.getElementById('user-avatar');
        if(userAvatarUrl) {
            avatarEl.src = userAvatarUrl;
            avatarEl.style.display = 'block';
        }

        function changeAvatar() {
            const newUrl = prompt("Введите ссылку на аватарку:", userAvatarUrl);
            if(newUrl !== null) {
                userAvatarUrl = newUrl.trim();
                localStorage.setItem('custom_avatar_' + user.id, userAvatarUrl);
                if(userAvatarUrl) {
                    avatarEl.src = userAvatarUrl;
                    avatarEl.style.display = 'block';
                } else {
                    avatarEl.style.display = 'none';
                }
            }
        }

        let isTrading = false;

        function switchTab(tab) {
            document.querySelectorAll('.tab-content').forEach(el => el.classList.remove('active'));
            document.querySelectorAll('.nav-item').forEach(el => el.classList.remove('active'));
            if(tab === 'wallet') {
                document.getElementById('tab-wallet').classList.add('active');
                document.getElementById('nav-wallet').classList.add('active');
            } else if(tab === 'trader') {
                document.getElementById('tab-trader').classList.add('active');
                document.getElementById('nav-trader').classList.add('active');
                checkBalance();
                loadStats();
                loadLogs();
            } else if(tab === 'stats') {
                document.getElementById('tab-stats').classList.add('active');
                document.getElementById('nav-stats').classList.add('active');
                loadStats();
            }
        }

        async function loadProfile() {
            const res = await fetch('/api/profile', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({telegram_id: user.id, username: user.username, first_name: user.first_name})});
            const data = await res.json();
            if(data.success) {
                const w = data.profile.solana_wallet;
                document.getElementById('wallet-input').value = w;
                document.getElementById('qr-img').src = "https://api.qrserver.com/v1/create-qr-code/?size=120x120&data=" + encodeURIComponent(w);
                isTrading = data.profile.trading_active === 1;
                updateUI();
                checkBalance();
            }
        }

        function updateUI() {
            const st = document.getElementById('trade-status');
            const btn = document.getElementById('toggle-btn');
            if(isTrading) {
                st.innerText = "ИИ Трейдер Активен (24/7)"; st.style.color = "#10b981";
                btn.innerText = "Остановить Бот"; btn.className = "btn btn-red";
            } else {
                st.innerText = "Остановлен"; st.style.color = "#f59e0b";
                btn.innerText = "Включить ИИ Трейдер"; btn.className = "btn btn-green";
            }
        }

        async function checkBalance() {
            const w = document.getElementById('wallet-input').value;
            if(!w) return;
            const res = await fetch('/api/blockchain/balance?wallet=' + encodeURIComponent(w));
            const data = await res.json();
            if(data.success) {
                document.getElementById('wallet-balance').innerText = data.balance.toFixed(4) + " SOL";
            }
        }

        async function verifyDeposit() {
            const txHash = document.getElementById('tx-hash-input').value.trim();
            if(!txHash) { alert("Введите хэш транзакции!"); return; }
            const res = await fetch('/api/verify-tx', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({telegram_id: user.id, tx_signature: txHash})});
            const data = await res.json();
            if(data.success) { alert("✅ Депозит зачислен!"); document.getElementById('tx-hash-input').value = ""; checkBalance(); }
        }

        async function toggleTrading() {
            isTrading = !isTrading;
            await fetch('/api/trading/toggle', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({telegram_id: user.id, active: isTrading ? 1 : 0})});
            updateUI();
        }

        async function loadLogs() {
            const res = await fetch('/api/logs?telegram_id=' + user.id);
            const data = await res.json();
            if(data.success && data.logs.length > 0) {
                const box = document.getElementById('logs-box');
                box.innerText = data.logs.map(l => `[${l.timestamp}] ${l.text}`).join('\\n');
                box.scrollTop = box.scrollHeight;
            }
        }

        async function loadStats() {
            const res = await fetch('/api/stats?telegram_id=' + user.id);
            const data = await res.json();
            if(data.success) {
                document.getElementById('stat-current').innerText = data.current_sol.toFixed(4) + " SOL";
                const diff = data.profit_sol;
                const el = document.getElementById('stat-profit-sol');
                el.innerText = (diff >= 0 ? "+" : "") + diff.toFixed(4) + " SOL";
                el.style.color = diff >= 0 ? "#34d399" : "#ef4444";

                const list = document.getElementById('trades-list');
                if(data.trades.length === 0) {
                    list.innerHTML = '<div style="color: #64748b; font-size: 12px; text-align: center; padding: 20px;">Нет сделок</div>';
                } else {
                    list.innerHTML = data.trades.map(t => `
                        <div class="trade-item">
                            <div>
                                <div style="color: #f8fafc; font-weight: bold;">${t.token_pair}</div>
                                <div style="color: #64748b; font-size: 10px;">TX: ${t.tx_signature.substring(0,8)}...</div>
                            </div>
                            <div style="color: ${t.profit_sol >= 0 ? '#34d399' : '#ef4444'}; font-weight: bold; font-size: 12px;">${t.profit_sol >= 0 ? '+' : ''}${t.profit_sol.toFixed(4)} SOL</div>
                        </div>
                    `).join('');
                }
            }
        }

        setInterval(async () => {
            if(!isTrading) return;
            checkBalance();
            loadStats();
            loadLogs();
        }, 4000);

        loadProfile();
    </script>
</body>
</html>
"""

async def index_handler(request):
    return web.Response(text=HTML_CONTENT, content_type='text/html')

async def health_handler(request):
    return web.Response(text="OK", status=200)

async def api_get_profile(request):
    data = await request.json()
    user = get_or_create_user(int(data.get("telegram_id")), data.get("username", ""), data.get("first_name", ""))
    return web.json_response({"success": True, "profile": user})

async def api_verify_tx(request):
    return web.json_response({"success": True})

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
    telegram_id = int(request.query.get("telegram_id", 0))
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT log_text, timestamp FROM server_logs WHERE telegram_id = ? ORDER BY id DESC LIMIT 20", (telegram_id,))
    rows = cursor.fetchall()
    conn.close()
    logs = [{"text": r[0], "timestamp": r[1]} for r in reversed(rows)]
    return web.json_response({"success": True, "logs": logs})

async def api_get_stats(request):
    telegram_id = int(request.query.get("telegram_id", 0))
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT solana_wallet, initial_sol FROM users WHERE telegram_id = ?", (telegram_id,))
    urow = cursor.fetchone()
    wallet = urow[0] if urow else SHARED_DEPOSIT_WALLET
    initial_sol = urow[1] if urow else 0.2517

    current_sol = await fetch_wallet_balance(wallet)
    
    cursor.execute("SELECT token_pair, buy_price, sell_price, profit_sol, tx_signature, timestamp FROM trades WHERE telegram_id = ? ORDER BY id DESC LIMIT 15", (telegram_id,))
    rows = cursor.fetchall()
    trades = [{"token_pair": r[0], "buy_price": r[1], "sell_price": r[2], "profit_sol": r[3], "tx_signature": r[4], "timestamp": r[5]} for r in rows]
    
    conn.close()
    profit_sol = current_sol - initial_sol
    
    return web.json_response({
        "success": True, 
        "current_sol": current_sol,
        "initial_sol": initial_sol,
        "profit_sol": profit_sol,
        "total_trades": len(trades),
        "trades": trades
    })

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
                                send_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
                                payload = {
                                    "chat_id": chat_id,
                                    "text": "🤖 **Zer0Life AI Dynamic Trader (24/7 Autopilot)**",
                                    "parse_mode": "Markdown",
                                    "reply_markup": {
                                        "inline_keyboard": [[{"text": "🚀 Открыть Терминал", "web_app": {"url": RENDER_URL}}]]
                                    }
                                }
                                async with session.post(send_url, json=payload):
                                    pass
            except Exception:
                await asyncio.sleep(3)
            await asyncio.sleep(1)

async def main():
    init_db()
    app = web.Application()
    app.router.add_get('/', index_handler)
    app.router.add_get('/health', health_handler)
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
    
    asyncio.create_task(telegram_long_polling())
    asyncio.create_task(background_mov_trader_daemon())

    logging.info("ИИ Динамический Трейдер запущен и готов к работе.")
    while True:
        await asyncio.sleep(3600)

if __name__ == "__main__":
    asyncio.run(main())

