"""Headless mobile DOM checks with mocked Telegram/API; never contacts the bot."""
import ast
import html
import json
from pathlib import Path
from html.parser import HTMLParser
import re
import shutil
import subprocess
import tempfile
import unittest


class MenuBrowserTests(unittest.TestCase):
    def test_static_interface_copy_has_translations(self):
        source = ast.parse(Path(__file__).resolve().parents[1].joinpath("main.py").read_text())
        page = next(ast.literal_eval(node.value) for node in source.body
                    if isinstance(node, ast.Assign) and any(
                        isinstance(target, ast.Name) and target.id == "RAW_HTML" for target in node.targets))
        rows_start = page.index("const UI_COPY_ROWS = [")
        rows_end = page.index("const UI_COPY = {", rows_start)
        rows_source = page[rows_start:rows_end]
        row_pattern = re.compile(r'\[\s*("(?:\\.|[^"\\])*")\s*,\s*("(?:\\.|[^"\\])*")\s*,\s*("(?:\\.|[^"\\])*")\s*\]')
        rows = [(json.loads(english), json.loads(russian), json.loads(ukrainian))
                for english, russian, ukrainian in row_pattern.findall(rows_source)]
        self.assertGreater(len(rows), 350, "both translation tables should be included in the audit")
        keys = [english for english, _russian, _ukrainian in rows]

        class VisibleCopy(HTMLParser):
            def __init__(self):
                super().__init__()
                self.skip_depth = 0
                self.strings = set()

            def handle_starttag(self, tag, attrs):
                if tag in {"script", "style"}:
                    self.skip_depth += 1
                elif not self.skip_depth:
                    self.strings.update(
                        " ".join(value.split())
                        for name, value in attrs
                        if name in {"placeholder", "title", "aria-label", "alt"} and value
                    )

            def handle_endtag(self, tag):
                if tag in {"script", "style"} and self.skip_depth:
                    self.skip_depth -= 1

            def handle_data(self, data):
                text = " ".join(data.split())
                if text and not self.skip_depth:
                    self.strings.add(text)

        visible = VisibleCopy()
        visible.feed(page)
        ignored_brand_or_placeholder = {
            "English", "Life", "ZER0LIFE · PARTNER DESK", "Zer", "__DEPOSIT_WALLET__",
            "BASE", "SOLANA", "SOLANA / USDC", "SOLANA · USDC", "USDC", "B",
            "!", "×", "—", "→", "↗", "↻", "⇄", "⇆", "▤", "◇", "◈", "●", "⚙", "✕",
        }

        def is_translated(text):
            for key in sorted(keys, key=len, reverse=True):
                if text == key:
                    return True
                if len(key.strip()) < 8:
                    continue
                if re.fullmatch(r"[A-Za-z0-9_]+", key):
                    if re.search(r"(?<![A-Za-z0-9_])" + re.escape(key) + r"(?![A-Za-z0-9_])", text):
                        return True
                elif key and key in text:
                    return True
            return False

        uncovered = [
            text for text in sorted(visible.strings)
            if text not in ignored_brand_or_placeholder
            and not re.fullmatch(r"[+\-−$€\d.,()%/· ]+", text)
            and not re.fullmatch(r"[+\-−$€\d.,%/· ]+[A-Z]{2,5}", text)
            and not is_translated(text)
        ]
        self.assertEqual(uncovered, [], "untranslated static interface copy: " + "; ".join(uncovered))

    @unittest.skipUnless(shutil.which("chromium"), "Chromium not installed")
    def test_mobile_sections_and_live_values(self):
        source = ast.parse(Path(__file__).resolve().parents[1].joinpath("main.py").read_text())
        page = next(ast.literal_eval(node.value) for node in source.body
                    if isinstance(node, ast.Assign) and any(
                        isinstance(target, ast.Name) and target.id == "RAW_HTML" for target in node.targets))
        page = re.sub(r'<script src="[^"]+"></script>', '', page)
        fixture = {
            "success": True, "username": "fixture", "user_balance": 34.037005,
            "sol_balance": .164009149, "withdrawable": 34.037005, "wallet_history": [],
            "trades": [
                {
                    "status": "CLOSED", "closed_at": 1793600000, "funding_currency": "USDC",
                    "profit_units": .123456, "profit_usdc": .123456, "profit_pct": 2.5,
                    "token_symbol": "TST", "symbol": "TST", "buy_price": 1.0, "sell_price": 1.025,
                    "amount_tokens": 10.0, "timestamp": 1793590000, "position_id": "fixture-profit",
                    "wallet_address": "FIXTURE_PRIVATE_WALLET_ADDRESS",
                    "token_mint": "FIXTURE_TOKEN_MINT_ADDRESS",
                    "buy_tx": "BUY_PRIVATE_TX_SIGNATURE_12345678901234567890",
                    "sell_tx": "SELL_PRIVATE_TX_SIGNATURE_09876543210987654321",
                },
                {
                    "status": "CLOSED", "closed_at": 1793600001, "funding_currency": "USDC",
                    "profit_units": -.5, "profit_usdc": -.5, "profit_pct": -1.0,
                    "token_symbol": "LOSS", "symbol": "LOSS", "buy_price": 1.0, "sell_price": .99,
                    "amount_tokens": 10.0, "timestamp": 1793590001, "position_id": "fixture-loss",
                },
                {
                    "status": "OPEN", "closed_at": None, "funding_currency": "USDC",
                    "profit_units": 1.0, "profit_usdc": 1.0, "profit_pct": 1.0,
                    "token_symbol": "OPEN", "symbol": "OPEN", "buy_price": 1.0, "sell_price": 1.0,
                    "amount_tokens": 10.0, "timestamp": 1793590002, "position_id": "fixture-open",
                },
                {
                    "status": "CLOSED", "closed_at": None, "funding_currency": "USDC",
                    "profit_units": .2, "profit_usdc": .2, "profit_pct": .5,
                    "token_symbol": "NO_TIME", "symbol": "NO_TIME", "buy_price": 1.0, "sell_price": 1.01,
                    "amount_tokens": 10.0, "timestamp": 1793590003, "position_id": "fixture-no-time",
                },
            ],
            "logs": [], "trading_active": False, "is_admin": True,
            "trading_admitted": True,
            "wallet_status": {"wallet_configured": True, "persistent_storage": True, "finance_allowed": True},
            "trading_settings": {"order_usdc": 1, "max_positions": 5, "assets": ["SOL", "JUP"]},
            "execution_policy": {
                "decision_max_age_seconds": 60, "quote_max_age_seconds": 20,
                "quote_max_slot_distance": 20, "slippage_bps": 40,
                "buy_max_price_impact_pct": 1.25, "core_max_cost_pct": 2.5,
                "max_positions": {"main": 5, "sniper": 5},
            },
            "operator_fees": {"reserve_sol": "0", "minimum_sol": "0.005", "own_available_sol": "0.164009149", "sufficient": False},
        }
        stub = """<script>
        window.testErrors=[];
        window.onerror=(message)=>window.testErrors.push(String(message));
        window.onunhandledrejection=(event)=>window.testErrors.push(String(event.reason));
        window.Telegram={WebApp:{initData:'fixture',initDataUnsafe:{user:{id:101,username:'fixture'}},expand(){},ready(){}}};
        const fixture=FIXTURE;
        window.feePosts=[];
        window.languagePosts=[];
        window.prompt=()=>{throw new Error('native prompt must not be used for fees');};
        window.confirm=()=>{throw new Error('native confirm must not be used for fees');};
        window.fetch=async(url,options={})=>{
            if(url==='/api/admin/fee_reserve'){
                window.feePosts.push(JSON.parse(options.body));
                fixture.operator_fees={reserve_sol:'0.01',minimum_sol:'0.005',own_available_sol:'0.154009149',sufficient:true};
                return new Response(JSON.stringify({success:true,message:'Allocated',operator_fees:fixture.operator_fees}),
                    {status:200,headers:{'Content-Type':'application/json'}});
            }
            if(url==='/api/account/language'){
                const body=JSON.parse(options.body);
                window.languagePosts.push(body);
                return new Response(JSON.stringify({success:true,ui_language:body.language}),
                    {status:200,headers:{'Content-Type':'application/json'}});
            }
            return new Response(JSON.stringify(fixture),{status:200,headers:{'Content-Type':'application/json'}});
        };
        </script>""".replace("FIXTURE", json.dumps(fixture))
        page = page.replace("</head>", stub + "</head>")
        checks = """<script>
        setTimeout(async()=>{
            const failures=[];
            try {
                proceedToTerminal(); await refresh();
                const assert=(condition,message)=>{if(!condition)failures.push(message);};
                window.nativeShares=[];
                Object.defineProperty(navigator,'canShare',{configurable:true,
                    value:data=>Array.isArray(data.files)&&data.files.length===1});
                Object.defineProperty(navigator,'share',{configurable:true,value:async data=>{
                    window.nativeShares.push({text:data.text,files:(data.files||[]).map(file=>file.name)});
                }});
                assert(document.querySelectorAll('.nav-btn').length===6,'six navigation items including Exchange');
                const exchangeNav=Array.from(document.querySelectorAll('.nav-btn')).find(button=>button.textContent.includes('Exchange'));
                assert(!!exchangeNav,'Exchange is a distinct navigation destination');
                assert(exchangeNav.getAttribute('onclick').includes("'exchange'"),'Exchange navigation opens its own tab');
                const exchangePanel=document.getElementById('tab-exchange');
                assert(!!exchangePanel,'Exchange panel exists');
                assert(exchangePanel.textContent.includes('Quote first · no automatic sending'),'manual-only state is explicit');
                assert(exchangePanel.textContent.includes('each transfer still requires your separate confirmation'),'manual confirmation is required');
                assert(!exchangePanel.textContent.includes('Separate user balances.'),'irrelevant balance note is omitted');
                assert(!exchangePanel.textContent.includes("Each user's in-app balances"),'untranslated balance note is omitted');
                assert(exchangePanel.querySelector('#exchange-quote-route-details'),'route recipient details are displayed');
                assert(exchangePanel.querySelector('#exchange-amount'),'quote amount input is present');
                assert(exchangePanel.querySelector('#exchange-quote-button'),'read-only quote control is present');
                assert(!exchangePanel.querySelector('form'),'there is no transfer form');
                assert(!exchangePanel.querySelector('[data-transfer-submit]'),'there is no transfer submission control');
                gotoTab('stats',null);
                const shareButtons=document.querySelectorAll('.share-profitable-trade');
                assert(shareButtons.length===1,'only a finalized profitable closed trade has a sharing button');
                shareButtons[0]?.click();
                const shareDialog=document.getElementById('trade-share-dialog');
                const tradeCaption=document.getElementById('trade-share-caption').value;
                assert(shareDialog.open,'sharing opens a review dialog');
                const shareRect=shareDialog.getBoundingClientRect();
                assert(shareRect.left>=0&&shareRect.right<=innerWidth&&shareRect.top>=0&&shareRect.bottom<=innerHeight,
                    'share review dialog fits the mobile viewport');
                assert(tradeCaption.includes('TST')&&tradeCaption.includes('+0.123456 USDC'),
                    'share caption includes the ticker and exact realized USDC result');
                assert(!tradeCaption.includes('BUY_PRIVATE_TX_SIGNATURE')&&!tradeCaption.includes('SELL_PRIVATE_TX_SIGNATURE'),
                    'share caption excludes transaction signatures');
                assert(!tradeCaption.includes('FIXTURE_PRIVATE_WALLET_ADDRESS')
                    &&!tradeCaption.includes('FIXTURE_TOKEN_MINT_ADDRESS'),'share caption excludes wallet and token addresses');
                assert(document.getElementById('trade-share-canvas').width===1080
                    &&document.getElementById('trade-share-canvas').height===1350,'share card uses a portrait social image');
                const shareSubmit=document.getElementById('trade-share-submit');
                assert(shareSubmit.disabled,'sharing requires the user to review and confirm the card');
                const shareConsent=document.getElementById('trade-share-confirmation');
                shareConsent.checked=true;shareConsent.dispatchEvent(new Event('change',{bubbles:true}));
                for(let i=0;i<80&&shareSubmit.disabled;i++)await new Promise(resolve=>setTimeout(resolve,25));
                assert(!shareSubmit.disabled,'confirmation enables sharing after the card is prepared: '+JSON.stringify({
                    status:document.getElementById('trade-share-status').textContent,
                    ready:tradeShareState?.ready,imageReady:tradeShareState?.imageReady,
                    confirmed:shareConsent.checked
                }));
                shareSubmit.click();await new Promise(resolve=>setTimeout(resolve,20));
                assert(window.nativeShares.length===1,'share action uses the device share menu once');
                assert(window.nativeShares[0]?.files?.length===1
                    &&window.nativeShares[0].files[0].endsWith('.png'),'device share menu receives the card image');
                assert(!window.nativeShares[0]?.text?.includes('BUY_PRIVATE_TX_SIGNATURE')
                    &&!window.nativeShares[0]?.text?.includes('SELL_PRIVATE_TX_SIGNATURE')
                    &&!window.nativeShares[0]?.text?.includes('FIXTURE_PRIVATE_WALLET_ADDRESS')
                    &&!window.nativeShares[0]?.text?.includes('FIXTURE_TOKEN_MINT_ADDRESS'),
                    'native share payload excludes transaction and wallet identifiers');
                closeTradeShareDialog();
                gotoTab('terminal',document.querySelectorAll('.nav-btn')[0]);
                 assert(document.getElementById('exchange-dir-solana-base').getAttribute('aria-pressed')==='true','Solana to Base is the initial direction');
                 assert(document.getElementById('exchange-dir-base-solana').getAttribute('aria-pressed')==='false','reverse direction starts unselected');
                 setExchangeDirection('base-to-solana');
                 assert(document.getElementById('exchange-direction-title').textContent==='Base → Solana','reverse route title updates');
                 assert(document.getElementById('exchange-source-title').textContent==='Use your Base USDC','reverse route source updates');
                 assert(document.getElementById('exchange-destination-title').textContent==='Review Solana estimate','reverse route destination updates');
                 assert(document.getElementById('exchange-path-source').textContent==='YOUR BASE USDC','reverse route path source updates');
                 assert(document.getElementById('exchange-path-destination').textContent==='SOLANA USDC ESTIMATE','reverse route path destination updates');
                 assert(document.getElementById('exchange-bridge-stage').classList.contains('bridge-stage--reverse'),'3D bridge direction reverses');
                 assert(document.getElementById('exchange-dir-base-solana').getAttribute('aria-pressed')==='true','reverse direction is accessible as selected');
                 setExchangeDirection('solana-to-base');
                 assert(document.getElementById('exchange-direction-title').textContent==='Solana → Base','forward route can be restored');
                  const basePaperFixture={
                      success:true,budget_usd:100,open_cost_usd:25,free_budget_usd:75,realized_gross_pnl_usd:1.25,
                      scanned_at:1793500000,live_trading:false,
                      message:'Base scan completed; no entry or exit trigger passed. No live transactions.',
                      pnl_note:'Gross price-only simulation. Gas, DEX fees, routing, price impact and failed fills are excluded; results are not a profit forecast.',
                      candidates:[],open_positions:[],closed_trades:[]
                  };
                  renderBasePaper(basePaperFixture);
                  applyUiLanguage('ru');
                 assert(document.documentElement.lang==='ru','Russian language sets the document language');
                  assert(uiLocale()==='ru-RU','Russian dates and numbers use the Russian locale');
                   gotoTab('stats',null);
                   document.querySelector('.share-profitable-trade')?.click();
                   assert(document.getElementById('trade-share-caption').value.includes('Реализованная прибыль'),
                       'manual share caption follows the Russian account language');
                   closeTradeShareDialog();gotoTab('terminal',document.querySelectorAll('.nav-btn')[0]);
                 assert(exchangeNav.textContent.includes('Обмен'),'navigation is translated to Russian');
                  assert(document.getElementById('ui-language-select').value==='ru','selector follows active language');
                  assert(localizeUiText('Token to Base')==='Token на Base','short words translate without corrupting part of a token name');
                  assert(localizeUiText('AI Crypto Investors')==='AI-криптоинвесторы','Investors title is localized in Russian');
                  assert(document.getElementById('base-paper-status').textContent.includes('Сканирование Base завершено: условия для входа или выхода не выполнены.'),
                      'dynamic Base scan status is translated in Russian');
                  assert(document.getElementById('base-paper-status').textContent.includes('Только симуляция · реальные транзакции: ВЫКЛ'),
                      'Base real-trading status is translated in Russian');
                  assert(document.getElementById('base-paper-candidates').textContent.includes('Последнее сканирование не нашло подходящих торговых пулов Base.'),
                      'empty Base candidate state is translated in Russian');
                  assert(document.getElementById('base-paper-open-positions').textContent.includes('Открытых виртуальных позиций нет.'),
                      'empty Base positions state is translated in Russian');
                   const ruTransferError=localizeUiText('The Solana transaction failed safe validation [USDC_TRANSFER_COUNT_2]; nothing was sent.');
                   assert(ruTransferError==='Транзакция Solana не прошла безопасную проверку [USDC_TRANSFER_COUNT_2]; ничего не отправлено.',
                       'Solana validation errors are translated without hiding their diagnostic code');
                   assert(localizeUiText('Withdrawal status is unclear. Do not submit it again; the server is checking automatically.')
                       .includes('Статус вывода неясен'),
                       'pending withdrawal warnings use the selected language');
                  assert(localizeBasePaperText('Paper scan complete: 2 simulated buys, 1 simulated exits. No live transactions.')
                      ==='Симуляция завершена. Виртуальные покупки: 2; виртуальные выходы: 1. Реальных транзакций не было.',
                      'count-bearing scan results are localized in Russian');
                 applyUiLanguage('uk');
                 assert(document.documentElement.lang==='uk','Ukrainian language sets document language');
                  assert(uiLocale()==='uk-UA','Ukrainian dates and numbers use the Ukrainian locale');
                   gotoTab('stats',null);
                   document.querySelector('.share-profitable-trade')?.click();
                   assert(document.getElementById('trade-share-caption').value.includes('Реалізований прибуток'),
                       'manual share caption follows the Ukrainian account language');
                   closeTradeShareDialog();gotoTab('terminal',document.querySelectorAll('.nav-btn')[0]);
                   const ukTransferError=localizeUiText('The Solana transaction failed safe validation [USDC_TRANSFER_COUNT_2]; nothing was sent.');
                   assert(ukTransferError==='Транзакція Solana не пройшла безпечну перевірку [USDC_TRANSFER_COUNT_2]; нічого не надіслано.',
                       'Solana validation errors are also translated into Ukrainian');
                  assert(exchangeNav.textContent.includes('Обмін'),'navigation is translated to Ukrainian');
                  assert(localizeUiText('Token to Base')==='Token до Base','whole-word translation also works in Ukrainian');
                  assert(localizeUiText('BASE NETWORK · L2')==='МЕРЕЖА BASE · L2','Base network label is localized in Ukrainian');
                  assert(document.getElementById('base-paper-status').textContent.includes('Сканування Base завершено: умов для входу чи виходу не виявлено.'),
                      'dynamic Base scan status is translated in Ukrainian');
                  assert(document.getElementById('base-paper-pnl-note').textContent.includes('Симуляція враховує лише зміну ціни.'),
                      'Base simulation explanation is translated in Ukrainian');
                  assert(document.getElementById('base-paper-budget').textContent.includes(','),
                      'Base currency values follow the selected locale');
                  assert(localizeBasePaperText('Paper scan complete: 2 simulated buys, 1 simulated exits. No live transactions.')
                      ==='Симуляцію завершено. Віртуальні покупки: 2; віртуальні виходи: 1. Реальних транзакцій не було.',
                      'count-bearing scan results are localized in Ukrainian');
                 document.getElementById('ui-language-select').value='uk';
                 await saveUiLanguage();
                 assert(window.languagePosts.length===1&&window.languagePosts[0].language==='uk',
                     'selected language is saved to the account API');
                 applyUiLanguage('en');
                  assert(exchangeNav.textContent.includes('Exchange'),'English can be restored');
                  assert(uiLocale()===undefined,'English keeps the device locale');
                  assert(document.getElementById('base-paper-status').textContent.includes('Base scan completed; no entry or exit trigger passed.'),
                      'Base status returns to English without stale translated copy');
                assert(document.getElementById('meme-sniper-card').closest('.tab-panel').id==='tab-trading','SOL sniper in Trading, not Overview');
                assert(document.getElementById('sniper-sol-balance').textContent==='0.164009 SOL','sniper shows personal SOL');
                assert(document.getElementById('sniper-position-cap').textContent==='Screened Solana candidates · up to 5 positions','sniper cap matches server policy');
                assert(document.getElementById('btn-meme-toggle').disabled,'unconnected sniper cannot trade');
                assert(document.getElementById('meme-toggle-text').textContent==='NOT CONNECTED','honest sniper status');
                assert(document.getElementById('btn-toggle').closest('.tab-panel').id==='tab-trading','controls in Trading');
                assert(document.getElementById('admin-custody-panel').closest('.tab-panel').id==='tab-settings','admin in Settings');
                assert(document.getElementById('port-balance').textContent==='34.037005 USDC','personal USDC value');
                assert(document.getElementById('port-withdrawable').textContent==='0.164009 SOL','separate SOL value');
                assert(document.getElementById('strategy-order-size').textContent==='1.00 USDC','server size not legacy $20');
                assert(document.getElementById('fee-status').textContent.includes('Reserve: 0 SOL'),'shows actual empty reserve');
                assert(window.feePosts.length===0,'no automatic fee allocation');
                document.getElementById('fee-amount').value='0,01';
                await setupFeeReserve();
                assert(window.feePosts.length===0,'explicit ownership confirmation required');
                document.getElementById('fee-confirm').checked=true;
                await setupFeeReserve();
                assert(window.feePosts.length===1,'one explicitly requested allocation');
                assert(window.feePosts[0].amount_sol==='0.01','mobile comma normalized');
                assert(document.getElementById('fee-status').textContent.includes('Reserve: 0.01 SOL'),'actual post-allocation reserve visible');
                assert(!document.getElementById('fee-confirm').checked,'confirmation cleared after success');
                const copy=document.body.cloneNode(true);
                copy.querySelectorAll('script,style').forEach(el=>el.remove());
                copy.querySelector('#ui-language-select')?.remove();
                assert(!/[А-Яа-яЁё]/.test(copy.textContent),'all UI text in English');
                assert(document.getElementById('strategy-help').closest('.tab-panel').id==='tab-settings','long strategy text in Settings');
                assert(!document.getElementById('strategy-help').open,'strategy help collapsed by default');
                const strategyText=document.getElementById('strategy-help').textContent;
                assert(strategyText.includes('OTC / USDC')&&strategyText.includes('OTC / SOL'),'both OTC quote pairs visible');
                assert(strategyText.includes('verified OTC Token-2022 mint'),'Token-2022 exception explained');
                assert(strategyText.includes('up to 200 Solana mints, verifies up to 40'),'expanded scan bounds are visible');
                assert(Array.from(document.querySelectorAll('summary')).some(el=>el.textContent==='Market discovery · DexScreener + charts'),'replacement market source is visible');
                 const sniperNoteCount=document.getElementById('meme-sniper-card').querySelectorAll('p.section-note:not(#sniper-pnl)').length;
                 assert(sniperNoteCount===3,'compact sniper card has 3 explanatory notes, found '+sniperNoteCount);
                fixture.ai_status={configured:true,paid_ai_enabled:false,signal_engine:'rules-v1',
                    model:'gpt-4.1',interval_seconds:90,
                    last_analyzed_at:1791925101,sniper_assets:[],
                    asset_labels:{SOL:'SOL',JUP:'JUP'},
                    chart_context:{
                        SOL:{chart_ready:1,chart_trend_h1:1,chart_support_low_usd:100,
                             chart_support_high_usd:101,chart_resistance_low_usd:105,
                             chart_resistance_high_usd:106,chart_rsi14_5m:53.8,
                             chart_volume_ratio_5m:2.4,chart_buy_setup:1,chart_sell_setup:0},
                        JUP:{chart_ready:0,chart_m5_candles:0,chart_h1_candles:0}
                    },
                    last_decisions:{main:{action:'HOLD',symbol:null,reason:'No significant signal.\\\\n6h change -0.16% is above the -5% floor.'},
                                    sniper:{action:'HOLD',symbol:null,reason:'No eligible candidates.'}}};
                fixture.ai_status.market_discovery={provider:'DexScreener + GeckoTerminal',scan_interval_seconds:300};
                fixture.ai_status.timings_ms={market_data:12,analyst:430,risk_controller:75,total:517};
                fixture.trading_active=true;fixture.sniper_active=true;
                fixture.trading_settings.assets=['SOL','JUP','RAY','WIF','PYTH','BONK','BOME','POPCAT','MEW','PENGU','KMNO','NOS','SHDW'];
                await refresh();
                assert(document.getElementById('toggle-text').textContent==='Autopilot: ON','enabled core status in English');
                assert(document.getElementById('meme-toggle-text').textContent==='AI: ON','enabled sniper status in English');
                 assert(document.getElementById('ai-status').textContent.includes('OpenAI OFF · no paid AI requests'),
                     'paid AI is disabled in the rules-only interface');
                 assert(document.getElementById('ai-latency').textContent.includes('No OpenAI requests. Market data, RPC provider use and on-chain transaction fees may still have costs.'),
                     'remaining provider and transaction costs are disclosed');
                const sourceText=document.getElementById('whale-signal-status').textContent;
                assert(sourceText.includes('Helius WebSocket OFF'),'paid stream is visibly disabled');
                assert(sourceText.includes('every 300 seconds'),'bounded replacement cadence is visible');
                assert(sourceText.includes('Neither waits for whale events'),'scheduled entries do not wait for whales');
                const coreAssets=document.getElementById('strategy-assets').textContent;
                assert(!['WIF','BONK','BOME','POPCAT','MEW','PENGU'].some(symbol=>coreAssets.includes(symbol)),
                    'core asset list excludes every sniper-only seed when there are no live candidates');
                assert(['KMNO','NOS','SHDW'].every(symbol=>coreAssets.includes(symbol)),
                    'validated USDC assets remain visible in the Core list');
                assert(document.getElementById('ai-decisions').textContent.includes('No significant signal.'),'English model explanation');
                fixture.ai_status.risk_controller={enabled:true,verdicts:{
                    main:{approved:false,trend:'DOWN',reason:'Hourly trend is declining.'},
                    sniper:{approved:true,trend:'UP',reason:'Fresh support bounce confirmed.'}}};
                fixture.ai_status.learning={sample_count:7,minimum_sample:20,max_outcomes:500};
                await refresh();
                assert(document.getElementById('ai-risk-review').textContent.includes('BLOCKED · DOWN'),
                    'risk rejection visible in English');
                assert(document.getElementById('ai-risk-review').textContent.includes('APPROVED · UP'),
                    'approved independent sniper review visible');
                assert(document.getElementById('ai-learning').textContent.includes('7 finalized closed trades'),
                    'honest outcome memory count visible');
                const analysisText=document.getElementById('ai-decisions').textContent;
                assert(analysisText.includes('No significant signal.\\n6h change -0.16%'),
                    'escaped model newlines render as line breaks');
                 assert(!analysisText.includes('\\\\n'),'analysis does not show literal slash-n text');
                 applyUiLanguage('uk');
                 const ukAiStatus=document.getElementById('ai-status').textContent;
                 const ukAiDecisions=document.getElementById('ai-decisions').textContent;
                 const ukAiRisk=document.getElementById('ai-risk-review').textContent;
                 const ukAiLearning=document.getElementById('ai-learning').textContent;
                 assert(ukAiStatus.includes('Правила v1')&&!ukAiStatus.includes('no paid AI requests'),
                     'live AI status follows Ukrainian');
                 assert(ukAiDecisions.includes('Значущого сигналу немає.')&&ukAiDecisions.includes('Зміна за 6 год'),
                     'dynamic AI explanations follow Ukrainian');
                 assert(ukAiRisk.includes('ЗАБЛОКОВАНО · ВНИЗ')&&ukAiRisk.includes('СХВАЛЕНО · ВГОРУ'),
                     'dynamic risk verdicts follow Ukrainian');
                 assert(ukAiLearning.includes('Історія результатів угод')&&ukAiLearning.includes('обмежені дані'),
                     'dynamic outcome history follows Ukrainian');
                 assert(!document.getElementById('ai-latency').textContent.includes('No OpenAI requests'),
                     'provider-cost status is translated');
                 applyUiLanguage('ru');
                 assert(document.getElementById('ai-decisions').textContent.includes('Значимого сигнала нет.'),
                     'dynamic analysis switches cleanly to Russian');
                 applyUiLanguage('en');
                fixture.ai_status.decision_created_at={main:null,sniper:fixture.ai_status.last_analyzed_at};
                await refresh();
                assert(document.getElementById('ai-decisions').textContent.includes('Core · USDC · Awaiting analysis'),
                    'a whale timestamp cannot masquerade as a Core analysis timestamp');
                const chartText=document.getElementById('ai-chart-zones').textContent;
                assert(chartText.includes('USD\\nSOL:'),'chart heading ends with a real line break');
                assert(chartText.includes('YES\\nJUP: fresh candles unavailable'),
                    'chart rows use real line breaks');
                assert(!chartText.includes('\\\\n'),'chart zones do not show literal slash-n text');
                const tabs=['terminal','trading','deposit','exchange','settings'];
                for(let i=0;i<tabs.length;i++){
                    gotoTab(tabs[i],document.querySelectorAll('.nav-btn')[i]);
                    assert(document.querySelectorAll('.tab-panel.active').length===1,'single active panel');
                    assert(getComputedStyle(document.getElementById('tab-'+tabs[i])).display==='block','visible '+tabs[i]);
                    assert(document.documentElement.scrollWidth<=innerWidth,'no horizontal overflow '+tabs[i]);
                }
                const investorTab=document.getElementById('tab-ai-crypto-investors');
                investorTab.classList.add('active');
                investorTab.style.display='block';
                const baseCard=document.getElementById('base-paper-panel');
                baseCard.style.display='block';
                const baseHead=baseCard.querySelector('.base-paper-head');
                const baseTitle=baseHead.querySelector('h2').getBoundingClientRect();
                const baseBadge=baseHead.querySelector('.base-paper-badge').getBoundingClientRect();
                assert(baseBadge.top>=baseTitle.bottom-1,'Base live-state badge sits below the title on mobile');
                assert(getComputedStyle(baseCard.querySelector('.base-paper-metrics')).gridTemplateColumns.split(/\\s+/).length===2,
                    'Base metrics use two readable columns on mobile');
                const baseButtons=[...baseCard.querySelectorAll('.base-paper-controls button')].map(button=>button.getBoundingClientRect());
                const baseCardRect=baseCard.getBoundingClientRect();
                assert(baseButtons.every(rect=>rect.left>=baseCardRect.left&&rect.right<=baseCardRect.right),
                    'Base controls stay within the card on mobile');
                assert(baseButtons[0].right<=baseButtons[1].left+1,'Base action buttons do not overlap');
                const solanaCard=investorTab.querySelector('.solana-investor-card');
                const solanaHead=solanaCard.querySelector('.base-paper-head');
                assert(solanaHead.querySelector('.solana-investor-badge').getBoundingClientRect().top
                    >=solanaHead.querySelector('h2').getBoundingClientRect().bottom-1,
                    'Solana autopilot badge does not overlap the title on mobile');
                assert(document.documentElement.scrollWidth<=innerWidth,'AI Crypto Investors has no horizontal overflow');
                fixture.is_admin=false;
                fixture.trading_admitted=true;
                await refresh();
                const tradingNav=document.querySelectorAll('.nav-btn')[1];
                const tradingPanel=document.getElementById('tab-trading');
                assert(getComputedStyle(tradingNav).display!=='none','admitted Telegram participant sees Trading');
                fixture.trading_admitted=false;
                await refresh();
                assert(getComputedStyle(tradingNav).display==='none','non-admitted participant cannot see Trading');
                assert(tradingPanel.style.display==='none','non-admitted Trading panel is hidden');
                fixture.trading_admitted=true;
                await refresh();
                assert(getComputedStyle(tradingNav).display!=='none','Trading returns when admission is restored');
                assert(tradingPanel.style.display!=='none','Trading panel is restored when admission returns');
                failures.push(...window.testErrors);
            } catch(e) {failures.push(String(e));}
            const result=document.createElement('pre');result.id='browser-test-result';
            result.textContent=JSON.stringify(failures);document.body.appendChild(result);
        },100);
        </script>"""
        page = page.replace("</body>", checks + "</body>")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "menu.html")
            path.write_text(page)
            completed = subprocess.run([
                shutil.which("chromium"), "--headless", "--no-sandbox", "--disable-gpu",
                "--disable-dev-shm-usage", "--window-size=390,844",
                "--virtual-time-budget=8000", "--dump-dom", path.as_uri()
            ], capture_output=True, text=True, timeout=30)
        result = re.search(r'<pre id="browser-test-result">(.*?)</pre>', completed.stdout, re.S)
        self.assertIsNotNone(result, completed.stderr[-1500:])
        self.assertEqual(json.loads(html.unescape(result.group(1))), [])
