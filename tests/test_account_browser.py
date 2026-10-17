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
            assert(!document.getElementById('launch-terminal').disabled,'launch state restored after loading');
            document.getElementById('account-nickname').value='TraderNew';
            document.getElementById('account-risk').checked=true;
            document.getElementById('account-partner-terms').checked=true;
            await createAccount();await refresh();
            assert(document.getElementById('account-signup').style.display==='none','signup hidden after server acceptance');
            assert(accountState.nickname==='ServerChosen','server owns nickname state');
            assert(document.getElementById('tab-settings').classList.contains('active'),'passkey setup next');
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
                success:true,phase:'auth',purpose:'withdraw',options:{challenge:'YQ',rpId:'example.test',userVerification:'required',allowCredentials:[{id:'YQ',type:'public-key'}]},
                intent:{token:'SOL',raw:'9223372036854775807',recipient_address:'FullRecipient'}}));}
            window.verifyCalls++;return new Response(JSON.stringify({success:true,phase:'approved'}));
        };
        </script>"""
        page=page.replace("<script>",stub+"<script>",1)
        self.run_page(page,"""
            assert(location.hash==='','capability removed from visible history');
            assert(window.authCalls===0&&window.optionsCalls===0,'no automatic authentication');
            document.getElementById('pin').value='test-code';
            await document.getElementById('start').onclick();
            assert(window.optionsCalls===1&&window.authCalls===0,'first click only loads trusted details');
            assert(document.getElementById('details').textContent.includes('9223372036.854775807 SOL'),'exact BigInt display');
            await document.getElementById('start').onclick();
            assert(window.authCalls===1&&window.verifyCalls===0,'cancel sends no proof');
            await document.getElementById('start').onclick();
            assert(window.authCalls===2&&window.verifyCalls===1,'cancel retry preserves original options');
            assert(document.getElementById('status').textContent.includes('Withdrawal approved'),'approval shown without sending funds');
            assert(document.getElementById('pin').value==='','code input cleared');
        ""","#ticket="+"a"*43)

    def test_withdraw_approval_link_and_waiting_state_are_visible_in_wallet(self):
        source=ast.parse((ROOT/"main.py").read_text())
        page=next(ast.literal_eval(node.value) for node in source.body if isinstance(node,ast.Assign)
                  and any(isinstance(target,ast.Name) and target.id=="RAW_HTML" for target in node.targets))
        page=re.sub(r'<script src="[^"]+"></script>','',page)
        stub="""<script>
        window.Telegram={WebApp:{initData:'fixture',initDataUnsafe:{user:{id:101,username:'test'}},
            openLink(url){window.openedPasskeyUrl=url;},expand(){},ready(){}}};
        window.fetch=async(url,options={})=>{
            if(url==='/api/account/passkey')return new Response(JSON.stringify({
                success:true,ticket:'ticket-fixture',url:'https://example.test/passkey#ticket=fixture'
            }),{headers:{'Content-Type':'application/json'}});
            if(url.startsWith('/api/account/passkey_status'))return new Response(JSON.stringify({
                success:true,phase:'approved',expired:false,used:false
            }),{headers:{'Content-Type':'application/json'}});
            return new Response(JSON.stringify({success:false}),{headers:{'Content-Type':'application/json'}});
        };
        </script>"""
        page=page.replace("</head>",stub+"</head>")
        self.run_page(page,"""
            const ticket=await passkeyTicket({purpose:'withdraw'});
            const link=document.getElementById('withdraw-approval-link');
            const status=document.getElementById('withdraw-progress');
            assert(ticket==='ticket-fixture','withdrawal ticket returned');
            assert(link.style.display==='block'&&link.href==='https://example.test/passkey#ticket=fixture',
                'current approval link is visible directly in wallet');
            assert(window.openedPasskeyUrl===link.href,'app opens the current secure approval');
            assert(!status.hidden&&status.textContent.includes('transfer has not been sent yet'),
                'status does not claim the transaction was sent before passkey approval');
            await waitPasskey(ticket,'approved');
        """)
