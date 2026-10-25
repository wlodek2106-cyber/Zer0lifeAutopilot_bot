"""Mobile DOM simulations only: no real device biometric or wallet calls."""
import ast
import html
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("chromium"),"Chromium not installed")
class AccountBrowserTests(unittest.TestCase):
    def run_page(self,page,checks,fragment=""):
        page=page.replace("</html>","""<script>
        setTimeout(async()=>{const failures=[];const assert=(ok,msg)=>{if(!ok)failures.push(msg);};
        try{CHECKS}catch(e){failures.push(e.stack||String(e));}
        const out=document.createElement('pre');out.id='browser-test-result';out.textContent=JSON.stringify(failures);
        document.body.appendChild(out);},100);
        </script></html>""".replace("CHECKS",checks))
        with tempfile.TemporaryDirectory() as folder:
            file=Path(folder,"page.html");file.write_text(page)
            result=subprocess.run([shutil.which("chromium"),"--headless","--no-sandbox","--disable-gpu",
                "--disable-dev-shm-usage","--window-size=390,844","--virtual-time-budget=4000",
                "--dump-dom",file.as_uri()+fragment],capture_output=True,text=True,timeout=30)
        match=re.search(r'<pre id="browser-test-result">(.*?)</pre>',result.stdout,re.S)
        self.assertIsNotNone(match,result.stderr[-1500:])
        self.assertEqual(json.loads(html.unescape(match.group(1))),[])

    def test_new_user_sees_real_signup_form_and_server_name(self):
        source=ast.parse((ROOT/"main.py").read_text())
        page=next(ast.literal_eval(node.value) for node in source.body if isinstance(node,ast.Assign)
                  and any(isinstance(target,ast.Name) and target.id=="RAW_HTML" for target in node.targets))
        page=re.sub(r'<script src="[^"]+"></script>','',page)
        stub="""<script>
        window.Telegram={WebApp:{initData:'fixture',initDataUnsafe:{user:{id:101,username:'TelegramName'}},expand(){},ready(){}}};
        window.alert=()=>{};window.prompt=()=>{throw Error('No native nickname prompt');};
        let profile={success:true,registered:false,partner_terms_required:true,has_passkey:false,passkey_count:0,recovery_codes_remaining:0};
        window.requests=[];
        window.fetch=async(url,options={})=>{
            window.requests.push({url,body:options.body?JSON.parse(options.body):null,headers:options.headers});
            if(url==='/api/account/register')profile={...profile,registered:true,nickname:'ServerChosen'};
            const body=url.startsWith('/api/account')?profile:url==='/api/security_status'?{success:true,has_pin:true}:{success:true,user_balance:7,sol_balance:0,withdrawable:7,wallet_history:[],trades:[],logs:[],trading_settings:{},wallet_status:{}};
            return new Response(JSON.stringify(body),{headers:{'Content-Type':'application/json'}});
        };
        </script>"""
        page=page.replace("</head>",stub+"</head>")
        self.run_page(page,"""
            await openTerminal();
            assert(document.getElementById('account-signup').style.display==='block','signup shown after launch');
            assert(!document.getElementById('affiliate-terms').hidden,'referred signup discloses partner share');
            assert(!document.getElementById('account-partner-terms').checked,'commission consent is not pre-checked');
            assert(document.getElementById('app').style.display==='none','no terminal before signup');
            const field=document.getElementById('account-nickname'),box=field.getBoundingClientRect();
            assert(document.elementFromPoint(box.x+box.width/2,box.y+box.height/2)===field,
                'nickname field must be topmost and receive touch, not be behind splash');
            const submit=document.getElementById('account-create'),submitBox=submit.getBoundingClientRect();
            assert(document.elementFromPoint(submitBox.x+submitBox.width/2,submitBox.y+submitBox.height/2)===submit,
                'create account button receives touch');
            const partnerBox=document.getElementById('account-partner-terms');
            assert(partnerBox.getBoundingClientRect().width>=20&&partnerBox.getBoundingClientRect().height>=20,
                'referral consent checkbox is visibly large enough to tap');
            assert(!document.getElementById('launch-terminal').disabled,'launch state restored after loading');
            document.getElementById('account-nickname').value='TraderNew';
            document.getElementById('account-risk').checked=true;
            await createAccount();
            assert(!window.requests.some(r=>r.url==='/api/account/register'),
                'registration is not sent before referral terms are accepted');
            assert(document.getElementById('account-error').textContent.includes('referral commission agreement'),
                'missing referral consent is explained next to the form');
            assert(document.getElementById('account-signup').style.display==='block',
                'signup stays visible until required consent is checked');
            document.getElementById('account-partner-terms').checked=true;
            await createAccount();await refresh();
            assert(document.getElementById('account-signup').style.display==='none','signup hidden after server acceptance');
            assert(accountState.nickname==='ServerChosen','server owns nickname state');
            assert(document.getElementById('app').style.display==='block','account opens without passkey setup');
            assert(!document.getElementById('wallet-external-reconcile')&&
                !document.getElementById('withdraw-tx-link')&&
                !document.getElementById('btn-check-withdrawal'),
                'the wallet does not expose transaction verification controls');
            const request=window.requests.find(r=>r.url==='/api/account/register');
            assert(request.body.nickname==='TraderNew'&&request.body.accept_risk===true,'real registration request');
            assert(request.body.accept_partner_terms===true,'commission consent is explicitly sent');
            assert(request.headers['X-Telegram-Init-Data']==='fixture','signed Telegram data forwarded');
            assert(!window.requests.some(r=>r.url==='/api/withdraw'),'signup never withdraws funds');
        """)

    def test_browser_requires_second_click_and_supports_cancel_retry(self):
        page=(ROOT/"passkey.html").read_text()
        stub="""<script>
        window.PublicKeyCredential=function(){};
        window.optionsCalls=0;window.authCalls=0;window.verifyCalls=0;
        Object.defineProperty(navigator,'credentials',{value:{get:async()=>{
            window.authCalls++;if(window.authCalls===1)throw Object.assign(Error('cancelled'),{name:'NotAllowedError'});
            return {id:'YQ',rawId:new Uint8Array([97]).buffer,type:'public-key',
              response:{authenticatorData:new Uint8Array([1]).buffer,clientDataJSON:new Uint8Array([2]).buffer,
                signature:new Uint8Array([3]).buffer},getClientExtensionResults:()=>({})};
        }}});
        window.fetch=async(path,options)=>{
            if(path.endsWith('/options')){window.optionsCalls++;return new Response(JSON.stringify({
                success:true,phase:'auth',purpose:'login',options:{challenge:'YQ',rpId:'example.test',userVerification:'required',allowCredentials:[{id:'YQ',type:'public-key'}]}}));}
            window.verifyCalls++;return new Response(JSON.stringify({success:true,phase:'approved',purpose:'login'}));
        };
        </script>"""
        page=page.replace("<script>",stub+"<script>",1)
        self.run_page(page,"""
            assert(location.hash==='','capability removed from visible history');
            assert(window.authCalls===0&&window.optionsCalls===0,'no automatic authentication');
            document.getElementById('pin').value='test-code';
            await document.getElementById('start').onclick();
            assert(window.optionsCalls===1&&window.authCalls===0,'first click only loads trusted details');
            assert(document.getElementById('status').textContent.includes('Review the details'),'login details shown before approval');
            await document.getElementById('start').onclick();
            assert(window.authCalls===1&&window.verifyCalls===0,'cancel sends no proof');
            await document.getElementById('start').onclick();
            assert(window.authCalls===2&&window.verifyCalls===1,'cancel retry preserves original options');
            assert(document.getElementById('status').textContent.includes('Sign-in approved'),'sign-in approval shown');
            assert(document.getElementById('pin').value==='','code input cleared');
        ""","#ticket="+"a"*43)

    def test_old_withdrawal_passkey_link_is_rejected_in_favor_of_telegram_code(self):
        page=(ROOT/"passkey.html").read_text()
        stub="""<script>
        window.authCalls=0;
        Object.defineProperty(navigator,'credentials',{value:{get:async()=>{window.authCalls++;return {};}}});
        window.fetch=async()=>new Response(JSON.stringify({
            success:true,phase:'auth',purpose:'withdraw',
            options:{challenge:'YQ',rpId:'example.test',userVerification:'required',
                allowCredentials:[{id:'YQ',type:'public-key'}]},
            intent:{token:'USDC',raw:'5000000',recipient_address:'Recipient'}}));
        </script>"""
        page=page.replace("<script>",stub+"<script>",1)
        self.run_page(page,"""
            document.getElementById('pin').value='test-code';
            await document.getElementById('start').onclick();
            assert(document.getElementById('status').textContent.includes('one-time code sent in Telegram'),
                'legacy passkey withdrawal link explains the new flow');
            assert(window.authCalls===0,'legacy link cannot request passkey approval');
        ""","#ticket="+"a"*43)

    def test_withdrawal_uses_exact_details_telegram_code_without_passkey(self):
        source=ast.parse((ROOT/"main.py").read_text())
        page=next(ast.literal_eval(node.value) for node in source.body if isinstance(node,ast.Assign)
                  and any(isinstance(target,ast.Name) and target.id=="RAW_HTML" for target in node.targets))
        page=re.sub(r'<script src="[^"]+"></script>','',page)
        stub="""<script>
        window.Telegram={WebApp:{initData:'fixture',initDataUnsafe:{user:{id:101,username:'test'}},
            expand(){},ready(){}}};
        window.confirm=()=>true;
        window.requests=[];
        window.withdrawCodeAttempt=0;
        window.fetch=async(url,options={})=>{
            window.requests.push({url,body:options.body?JSON.parse(options.body):null});
            if(url==='/api/withdraw/code'&&window.withdrawCodeAttempt++===0)
                return new Response(JSON.stringify({success:false,message:
                    'Withdrawals and trading are paused because wallet funds do not cover recorded account balances. No transfer was sent.'}),
                    {status:409,headers:{'Content-Type':'application/json'}});
            const body=url==='/api/admin/legacy_balances/preview'?{
                    success:true,can_import:true,backed:true,eligible_count:2,eligible_usdc:'5',
                    candidate_count:2,candidate_usdc:'5',backing_available_usdc:'10',skipped:{},
                    digest:'a'.repeat(64)}:
                url==='/api/admin/legacy_balances/import'?{
                    success:true,message:'Confirmed legacy balances were imported into the ledger. No Solana transactions were sent.',
                    imported_count:2,amount_usdc:'5'}:
                url==='/api/admin/balance_check'?{
                    success:true,read_only:true,assets:{USDC:{wallet:'25.036356',ledger:'25.036256',difference:'0.0001'},
                        SOL:{wallet:'0.117571682',ledger:'0.103458998',difference:'0.014112684'}}}:
                url==='/api/withdraw/code'?{success:true,expires_in:300}:
                url==='/api/withdraw'?{success:true,state:'SUBMITTED',tx_signature:'1'.repeat(88)}:
                {success:true,user_balance:7,sol_balance:1,withdrawable:7,
                  wallet_history:[{type:'WITHDRAW',amount:'5 USDC',tx:'1'.repeat(88),status:'COMPLETED',
                      telegram_notified:true}],trades:[],
                  logs:[],is_admin:true,trading_settings:{order_usdc:1,order_sol:0.05,assets:[],max_positions:5},
                  wallet_status:{},pending_operations:[]};
            return new Response(JSON.stringify(body),{headers:{'Content-Type':'application/json'}});
        };
        </script>"""
        page=page.replace("</head>",stub+"</head>")
        self.run_page(page,"""
            await refresh();
            const address='ETX5mgkeGphNzucEKKM1NKvLQFKr5wdbjJt4J7kVmS';
            document.getElementById('withdraw-address').value=address;
            document.getElementById('withdraw-amount').value='5.000749';
            await requestWithdrawalCode();
            assert(!document.getElementById('withdraw-tx-link')&&
                !document.getElementById('btn-check-withdrawal')&&
                !document.getElementById('wallet-external-reconcile'),
                'withdrawal exposes no TX verification or recovery step');
            assert(document.getElementById('withdraw-code-status').textContent.includes('No code or transfer was sent'),
                'the balance block clearly says no code or transfer was sent');
            await secureWithdraw();
            assert(document.getElementById('withdraw-progress').hidden&&
                document.getElementById('withdraw-code-status').textContent.startsWith('Withdrawal paused:'),
                'tapping Withdraw without a code does not add a stale prompt or overwrite the block reason');
            assert(!window.requests.some(r=>r.url==='/api/withdraw'),'the blocked code request never submits a withdrawal');
            const codeRequest=window.requests.find(r=>r.url==='/api/withdraw/code');
            await requestWithdrawalCode();
            const successfulCodeRequest=window.requests.filter(r=>r.url==='/api/withdraw/code')[1];
            assert(successfulCodeRequest.body.recipient_address===address&&successfulCodeRequest.body.amount==='5.000749',
                'the Telegram code remains bound to the exact recipient and amount');
            assert(codeRequest.body.recipient_address===address&&codeRequest.body.amount==='5.000749',
                'code request is bound to exact recipient and amount');
            const codeInput=document.getElementById('withdraw-code');
            codeInput.value='123456';
            await secureWithdraw();
            const transfer=window.requests.find(r=>r.url==='/api/withdraw');
            const status=document.getElementById('withdraw-progress');
            assert(transfer.body.telegram_code==='123456','withdrawal sends the Telegram code');
            assert(transfer.body.recipient_address===address&&transfer.body.amount==='5.000749',
                'final transfer retains the approved amount and recipient');
            assert(!('pin' in transfer.body)&&!('passkey_ticket' in transfer.body),
                'withdrawal needs no personal PIN or passkey');
            assert(!status.hidden&&status.textContent.includes('confirmed on Solana'),
                'completed withdrawal status is shown in the wallet');
            assert(status.textContent.includes('5 USDC')&&status.textContent.includes('Telegram receipt sent'),
                'completed status shows the exact sent amount and Telegram receipt state');
            window.latestAccountStats={
                wallet_history:[],
                pending_operations:[{kind:'WITHDRAW',signature:'2'.repeat(88),state:'UNKNOWN'}]
            };
            activeWithdrawalSignature='2'.repeat(88);
            status.hidden=false;
            status.textContent='Withdrawal status is unclear. Do not submit it again.';
            clearWithdrawalCode();
            assert(!status.hidden&&
                status.textContent.includes('Do not submit'),
                'editing details never hides an unresolved withdrawal warning');
            window.latestAccountStats={
                wallet_history:[{type:'WITHDRAW',tx:'2'.repeat(88),status:'COMPLETED'}],
                pending_operations:[]
            };
            document.getElementById('withdraw-address').value='57S7TryAMhxRq5sMSyZxTkyCmzT8tPFKfXjzZwmv5';
            clearWithdrawalCode();
            assert(status.hidden,
                'changing details clears an old completed withdrawal status');
            assert(codeInput.value==='', 'changing transfer details clears an old Telegram code');
            assert(document.getElementById('chain-withdraw-balances').textContent.includes('Max withdrawal:'),
                'wallet shows the capped withdrawal amount: '+
                document.getElementById('chain-withdraw-balances').textContent+
                ' requests='+window.requests.map(r=>r.url).join(','));
            assert(document.getElementById('admin-custody-panel').style.display==='block',
                'bulk legacy import controls are restricted to administrator view');
            await previewLegacyBalances();
            assert(!document.getElementById('legacy-balance-import-button').hidden&&
                document.getElementById('legacy-balance-migration-status').textContent.includes('Eligible amount: 5 USDC'),
                'read-only preview shows the backed bulk import summary before approval');
            await importLegacyBalances();
            const migration=window.requests.find(r=>r.url==='/api/admin/legacy_balances/import');
            assert(migration.body.confirm===true&&migration.body.preview_digest==='a'.repeat(64),
                'bulk import requires explicit confirmation tied to the exact preview');
            assert(document.getElementById('legacy-balance-migration-status').textContent.includes('No Solana transactions were sent'),
                'import result clearly states that it changes only the internal ledger');
        """)
