import ast
from pathlib import Path
import re
import shutil
import unittest
import test_account_browser as browser

ROOT=Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("chromium"),"Chromium not installed")
class WebBrowserTests(unittest.TestCase):
    def page(self,stub):
        tree=ast.parse((ROOT/"main.py").read_text())
        page=next(ast.literal_eval(n.value) for n in tree.body if isinstance(n,ast.Assign)
                  and any(isinstance(t,ast.Name) and t.id=="RAW_HTML" for t in n.targets))
        page=re.sub(r'<script src="[^"]+"></script>','',page)
        page=page.replace("</head>","<script>window.ZRL_WEB=true;</script>"+stub+"</head>")
        return page.replace("</body>",(ROOT/"web_login.html").read_text()+
                            "<script>"+(ROOT/"web_terminal.js").read_text()+"</script></body>")

    def test_standalone_signed_out_click_opens_touchable_login_and_no_payments(self):
        page=self.page("""<script>
        window.PublicKeyCredential=function(){};
        window.fetch=async()=>new Response(JSON.stringify({success:true,user:null,registration_url:'https://t.me/example_bot'}));
        </script>""")
        browser.AccountBrowserTests().run_page(page,"""
            await openTerminal();
            assert(!document.getElementById('web-login').hidden,'browser login visible without Telegram initData');
            const field=document.getElementById('web-nickname'),b=field.getBoundingClientRect();
            assert(document.elementFromPoint(b.x+b.width/2,b.y+b.height/2)===field,'login receives touch above splash');
            assert(!document.getElementById('web-register').hidden,'identity registration link present');
            assert(document.getElementById('subscription-card').hidden,'no payment before login');
        """)

    def test_admin_free_and_expiry_presentation_never_disables_manual_close(self):
        page=self.page("""<script>
        window.fetch=async(url)=>new Response(JSON.stringify(
            url==='/web-auth/me'?{success:true,user:{id:428821665,username:'Operator'},csrf:'fixture'}:
            {success:true,active:true,admin_free:true,expires_at:null,purchase_available:true}));
        </script>""")
        browser.AccountBrowserTests().run_page(page,"""
            await loadWebSession();await refreshAccess();
            assert(document.getElementById('subscription-purchase').hidden,'operator never asked to pay');
            assert(document.getElementById('subscription-state').textContent.includes('free permanent'),'operator state clear');
            showAccess({active:false,admin_free:false,expires_at:0,purchase_available:true});
            updateMemeToggleUI(false);updateToggleUI(false);
            assert(document.getElementById('btn-toggle').disabled,'core locked after expiry and regular refresh');
            assert(document.getElementById('btn-meme-toggle').disabled,'sniper locked after expiry and regular refresh');
            assert(!document.querySelector('.btn-sell-all').disabled,'manual close remains accessible');
            showAccess({active:true,admin_free:false,expires_at:1900000000,purchase_available:true});
            assert(!document.getElementById('btn-toggle').disabled,'paid access unlocked visually');
        """)

    def test_realized_pnl_shows_usdc_and_sol_separately_with_loss_color(self):
        page=self.page("<script>window.fetch=async()=>new Response(JSON.stringify({success:true}));</script>")
        browser.AccountBrowserTests().run_page(page,"""
            renderRealizedPnl({profit_usdc:0,profit_sol:-0.001325811});
            assert(document.getElementById('pnl-val').textContent==='+$0.00','USDC total stays in USDC');
            const sol=document.getElementById('pnl-sol-val');
            assert(sol.textContent==='-0.001325811 SOL','SOL loss is visible in native units');
            assert(sol.style.color==='rgb(248, 113, 113)','negative SOL PnL is colored red');
            renderRealizedPnl({profit_usdc:1.25,profit_sol:0.002});
            assert(document.getElementById('pnl-val').textContent==='+$1.25','positive USDC formatting');
            assert(sol.textContent==='+0.002000000 SOL','positive SOL formatting');
        """)

    def test_pending_operation_recovery_opens_touchable_confirmation(self):
        page=self.page("""<script>
        window.pendingRecoverySignature='fixture-signature';
        window.pendingRecoveryKind='BUY';
        window.confirm=()=>{throw Error('Native confirm should not be used in Telegram iOS');};
        window.fetch=async()=>new Response(JSON.stringify({success:false,message:'fixture'}));
        </script>""")
        self.assertIn("['BUY', 'SELL'].includes(op.kind)", page)
        self.assertIn("Reserved: ", page)
        browser.AccountBrowserTests().run_page(page,"""
            document.getElementById('app').style.display='block';
            document.getElementById('nav').style.display='flex';
            document.getElementById('web-login').hidden=true;
            document.getElementById('splash-screen').style.display='none';
            const button=document.getElementById('recover-pending-operation');
            button.style.display='block';
            assert(button.textContent.includes('Verify expired operation'),'generic recovery control is available');
            const box=button.getBoundingClientRect();
            assert(document.elementFromPoint(box.x+box.width/2,box.y+box.height/2)===button,
                'recovery button is the topmost touch target');
            button.click();
            const modal=document.getElementById('recovery-confirm-modal');
            assert(modal.style.display==='flex','tap opens in-page confirmation without native dialog');
            const confirm=document.querySelector('#recovery-confirm-modal button:last-child');
            assert(document.querySelector('#recovery-confirm-modal p').textContent.includes('BUY releases its reserved funds'),
                'confirmation explains when a pending BUY can release funds');
            const confirmBox=confirm.getBoundingClientRect();
            assert(document.elementFromPoint(confirmBox.x+confirmBox.width/2,confirmBox.y+confirmBox.height/2)===confirm,
                'confirm button is the topmost touch target');
            assert(modal.getAttribute('aria-modal')==='true','confirmation is announced as modal');
        """)

    def test_signin_two_click_native_gesture_with_server_cookie_not_telegram_header(self):
        page=self.page("""<script>
        window.PublicKeyCredential=function(){};window.calls=[];window.nativeCalls=0;window.signedIn=false;
        Object.defineProperty(navigator,'credentials',{value:{get:async()=>{
            window.nativeCalls++;return {id:'YQ',rawId:new Uint8Array([97]).buffer,type:'public-key',
              response:{authenticatorData:new Uint8Array([1]).buffer,clientDataJSON:new Uint8Array([2]).buffer,
                signature:new Uint8Array([3]).buffer},getClientExtensionResults:()=>({})};
        }}});
        window.alert=()=>{};
        window.fetch=async(url,options={})=>{
            window.calls.push({url,options});
            let data={success:true};
            if(url==='/web-auth/me')data={success:true,user:window.signedIn?{id:101,username:'Trader'}:null,csrf:'server-csrf'};
            if(url==='/web-auth/start')data={success:true,ticket:'a'.repeat(43)};
            if(url==='/passkey/options')data={success:true,options:{challenge:'YQ',rpId:'example.test',userVerification:'required',allowCredentials:[{id:'YQ',type:'public-key'}]}};
            if(url==='/web-auth/finish')window.signedIn=true;
            if(url==='/api/account')data={success:true,registered:true,nickname:'Trader',has_passkey:true};
            if(url==='/api/security_status')data={success:true,has_pin:true};
            if(url==='/api/subscription')data={success:true,active:false,admin_free:false,purchase_available:true};
            return new Response(JSON.stringify(data));
        };
        </script>""")
        browser.AccountBrowserTests().run_page(page,"""
            document.getElementById('web-nickname').value='Trader';
            document.getElementById('web-pin').value='test-code';
            await webSignIn.onclick();
            assert(window.nativeCalls===0,'first click only loads options');
            await webSignIn.onclick();
            assert(window.nativeCalls===1,'second explicit click invokes verification');
            assert(window.webSession.user.id===101,'trusted session identity loaded');
            assert(document.getElementById('web-pin').value==='','PIN cleared');
            assert(document.getElementById('web-login').hidden,'login closed');
            const request=window.calls.find(c=>c.url==='/api/account');
            assert(request.options.credentials==='same-origin','secure cookie transported');
            assert(request.options.headers['X-ZRL-CSRF']==='server-csrf','CSRF forwarded');
            assert(!request.options.headers['X-Telegram-Init-Data'],'no spoofed Telegram initData');
            assert(!window.calls.some(c=>c.url==='/api/withdraw'||c.url==='/api/toggle_trading'),'sign-in never sends money or starts trading');
        """)
