"""Headless mobile DOM checks with mocked Telegram/API; never contacts the bot."""
import ast
import html
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest


class MenuBrowserTests(unittest.TestCase):
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
            "trades": [], "logs": [], "trading_active": False, "is_admin": True,
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
        window.prompt=()=>{throw new Error('native prompt must not be used for fees');};
        window.confirm=()=>{throw new Error('native confirm must not be used for fees');};
        window.fetch=async(url,options={})=>{
            if(url==='/api/admin/fee_reserve'){
                window.feePosts.push(JSON.parse(options.body));
                fixture.operator_fees={reserve_sol:'0.01',minimum_sol:'0.005',own_available_sol:'0.154009149',sufficient:true};
                return new Response(JSON.stringify({success:true,message:'Allocated',operator_fees:fixture.operator_fees}),
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
                assert(document.querySelectorAll('.nav-btn').length===6,'six navigation items including Exchange');
                const exchangeNav=Array.from(document.querySelectorAll('.nav-btn')).find(button=>button.textContent.includes('Exchange'));
                assert(!!exchangeNav,'Exchange is a distinct navigation destination');
                assert(exchangeNav.getAttribute('onclick').includes("'exchange'"),'Exchange navigation opens its own tab');
                const exchangePanel=document.getElementById('tab-exchange');
                assert(!!exchangePanel,'Exchange panel exists');
                 assert(exchangePanel.textContent.includes('No transfer can be started in either direction'),'not-configured state is explicit');
                assert(exchangePanel.textContent.includes('not configured'),'route availability is unambiguous');
                assert(exchangePanel.textContent.includes('Shared wallet. Separate user balances.'),'UID-isolated ledger is explained');
                assert(exchangePanel.textContent.includes('not an external withdrawal'),'internal bridge is distinguished from withdrawal');
                assert(exchangePanel.textContent.includes('Do not retry a transfer'),'pending and unknown transfers must not be retried');
                assert(exchangePanel.querySelector('.exchange-disabled').disabled,'bridge action is disabled');
                 assert(exchangePanel.querySelectorAll('input,select,form').length===0,'no transfer submission controls are present');
                 assert(document.getElementById('exchange-dir-solana-base').getAttribute('aria-pressed')==='true','Solana to Base is the initial direction');
                 assert(document.getElementById('exchange-dir-base-solana').getAttribute('aria-pressed')==='false','reverse direction starts unselected');
                 setExchangeDirection('base-to-solana');
                 assert(document.getElementById('exchange-direction-title').textContent==='Base → Solana','reverse route title updates');
                 assert(document.getElementById('exchange-source-title').textContent==='Use your Base USDC','reverse route source updates');
                 assert(document.getElementById('exchange-destination-title').textContent==='Credit your Solana balance','reverse route destination updates');
                 assert(document.getElementById('exchange-path-source').textContent==='YOUR BASE USDC','reverse route path source updates');
                 assert(document.getElementById('exchange-path-destination').textContent==='YOUR SOLANA BALANCE','reverse route path destination updates');
                 assert(document.getElementById('exchange-bridge-stage').classList.contains('bridge-stage--reverse'),'3D bridge direction reverses');
                 assert(document.getElementById('exchange-dir-base-solana').getAttribute('aria-pressed')==='true','reverse direction is accessible as selected');
                 setExchangeDirection('solana-to-base');
                 assert(document.getElementById('exchange-direction-title').textContent==='Solana → Base','forward route can be restored');
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
                assert(!/[А-Яа-яЁё]/.test(copy.textContent),'all UI text in English');
                assert(document.getElementById('strategy-help').closest('.tab-panel').id==='tab-settings','long strategy text in Settings');
                assert(!document.getElementById('strategy-help').open,'strategy help collapsed by default');
                const strategyText=document.getElementById('strategy-help').textContent;
                assert(strategyText.includes('OTC / USDC')&&strategyText.includes('OTC / SOL'),'both OTC quote pairs visible');
                assert(strategyText.includes('verified OTC Token-2022 mint'),'Token-2022 exception explained');
                assert(strategyText.includes('up to 200 Solana mints, verifies up to 40'),'expanded scan bounds are visible');
                assert(Array.from(document.querySelectorAll('summary')).some(el=>el.textContent==='Market discovery · DexScreener + charts'),'replacement market source is visible');
                assert(document.getElementById('meme-sniper-card').querySelectorAll('p.section-note').length===3,'compact sniper card');
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
                "--virtual-time-budget=4000", "--dump-dom", path.as_uri()
            ], capture_output=True, text=True, timeout=30)
        result = re.search(r'<pre id="browser-test-result">(.*?)</pre>', completed.stdout, re.S)
        self.assertIsNotNone(result, completed.stderr[-1500:])
        self.assertEqual(json.loads(html.unescape(result.group(1))), [])
