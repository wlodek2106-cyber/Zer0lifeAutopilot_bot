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

    def telegram_page(self,stub):
        tree=ast.parse((ROOT/"main.py").read_text())
        page=next(ast.literal_eval(n.value) for n in tree.body if isinstance(n,ast.Assign)
                  and any(isinstance(t,ast.Name) and t.id=="RAW_HTML" for t in n.targets))
        page=re.sub(r'<script src="[^"]+"></script>','',page)
        return page.replace("</head>",stub+"</head>")

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
        const now=Math.floor(Date.now()/1000);
        window.fetch=async(url)=>new Response(JSON.stringify(
            url==='/web-auth/me'?{success:true,user:{id:428821665,username:'Operator'},csrf:'fixture'}:
            {success:true,active:true,admin_free:true,expires_at:null,purchase_available:true,trading_admitted:true,
             free_access_until:now+90,server_time:now}));
        </script>""")
        browser.AccountBrowserTests().run_page(page,"""
            await loadWebSession();await refreshAccess();
            assert(document.getElementById('subscription-purchase').hidden,'operator never asked to pay');
            assert(document.getElementById('subscription-state').textContent.includes('free permanent'),'operator state clear');
            assert(document.getElementById('subscription-free-countdown').textContent.includes('ends in'),
              'operator sees the public free-access countdown without losing permanent admin access');
            showAccess({active:false,admin_free:false,expires_at:0,purchase_available:true,trading_admitted:true});
            updateMemeToggleUI(false);updateToggleUI(false);
            assert(document.getElementById('btn-toggle').disabled,'core locked after expiry and regular refresh');
            assert(document.getElementById('btn-meme-toggle').disabled,'sniper locked after expiry and regular refresh');
            assert(!document.querySelector('.btn-sell-all').disabled,'manual close remains accessible');
            showAccess({active:true,admin_free:false,expires_at:1900000000,purchase_available:true,trading_admitted:true});
            assert(!document.getElementById('btn-toggle').disabled,'paid access unlocked visually');
        """)

    def test_tariffs_tab_countdown_expiry_prompt_and_zrl_qr(self):
        page=self.page("<script>window.fetch=async()=>new Response(JSON.stringify({success:true}));</script>")
        browser.AccountBrowserTests().run_page(page,"""
            const now=Math.floor(Date.now()/1000);
            assert(!document.getElementById('nav-tariffs').hidden,'standalone menu exposes Tariffs');
            showAccess({active:true,admin_free:false,free_access:true,free_access_until:now+90,
              server_time:now,payment_required:false,purchase_available:true,trading_admitted:true});
            assert(document.getElementById('subscription-free-countdown').textContent.includes('ends in'),
              'free access countdown is visible');
            assert(document.getElementById('tariff-modal').hidden,'no payment prompt during free access');
            assert([...document.querySelectorAll('[data-package-id]')].every(button=>button.disabled),
              'payment cannot be requested during free access');
            showAccess({active:false,admin_free:false,free_access:false,payment_required:true,
              free_access_until:now-1,server_time:now,purchase_available:true,trading_admitted:true});
            assert(!document.getElementById('tariff-modal').hidden,'expired access prompts for a plan');
            assert(document.getElementById('tariff-dialog').textContent.includes('100,000 ZRL'),
              'modal presents the ZRL option');
            const receiver='57S7TryAMhxRq5sMSyZxTkyCmzT8tPFKfXjzZwvmv5db';
            showPaymentRequest({id:'invoice',amount:'100000.000001',asset:'ZRL',label:'7 days',
              days:7,mint:'AyfWjjJ9FPYfsgKCUaZ7PwMpoHi8ujf4m3pYxy7MmDiq',
              recipient:receiver,sender:'sender-wallet',deadline:now+3600},'fallback-recipient');
            assert(!document.getElementById('subscription-qr').hidden,'payment QR is visible');
            assert(document.getElementById('subscription-invoice-label').textContent.includes('100000.000001 ZRL'),
              'QR amount is shown exactly');
            assert(document.getElementById('subscription-qr').src.includes('spl-token'),
              'ZRL QR encodes the verified mint');
            assert(document.getElementById('subscription-address').textContent===receiver,
              'invoice recipient overrides the generic fallback');
            const solRecipient='CXKWBCBBGcL4hmxoMSPKB3CzGqeZYFT2Ke4hgCJ3wi4Y';
            showPaymentRequest({id:'sol-invoice',amount:'0.500000001',asset:'SOL',label:'30 days',
              days:30,mint:'',recipient:solRecipient,sender:'sender-wallet',deadline:now+3600},receiver);
            assert(document.getElementById('subscription-address').textContent===solRecipient,
              'SOL tariff displays its separate receiver');
            assert(document.getElementById('subscription-qr').src.includes(encodeURIComponent('solana:'+solRecipient)),
              'SOL QR encodes the separate receiver');
        """)

    def test_telegram_menu_shows_countdown_and_expiry_prompt_without_opening_checkout_for_unadmitted_user(self):
        page=self.telegram_page("""<script>
        window.Telegram={WebApp:{initData:'signed-fixture',initDataUnsafe:{user:{id:101}},expand(){},ready(){},openLink(){}}};
        const now=Math.floor(Date.now()/1000);
        window.tariffResponse={success:true,active:true,free_access:true,free_access_until:now+90,
          server_time:now,payment_required:false,purchase_available:true,trading_admitted:true};
        window.fetch=async(url)=>new Response(JSON.stringify(url==='/api/subscription'?window.tariffResponse:{success:true}));
        </script>""")
        browser.AccountBrowserTests().run_page(page,"""
            gotoTab('tariffs',document.getElementById('nav-tariffs'));
            await new Promise(resolve=>setTimeout(resolve,50));
            assert(!document.getElementById('nav-tariffs').hidden,'Tariffs appears in the Telegram menu');
            assert(!document.getElementById('telegram-tariff-view').hidden,'Telegram tariff details open');
            assert(document.getElementById('telegram-tariff-countdown').textContent.includes('ends in'),
              'Telegram view shows the free-access countdown');
            assert(!document.getElementById('telegram-tariff-modal').classList.contains('active'),
              'no expiry modal while free access is active');
            document.querySelector('[data-telegram-package="zrl_7d"]').click();
            assert(document.getElementById('telegram-package-modal').classList.contains('active'),
              'package details open while free access is active');
            assert(document.getElementById('telegram-package-modal-checkout').hidden,
              'checkout stays unavailable until free access ends');
            closeTelegramPackage();
             window.tariffResponse={success:true,active:false,free_access:false,trial_available:true,
               trial_active:false,payment_required:true,purchase_available:false,
               trading_admitted:false,server_time:now,free_access_until:now-1};
             await refreshTelegramTariffs();
             assert(!document.getElementById('telegram-trial-start').hidden,
               'new account sees the one-time free-trial button');
             assert(!document.getElementById('telegram-tariff-modal').classList.contains('active'),
               'eligible new user is not pushed into paid checkout');
             assert(document.getElementById('telegram-tariff-status').textContent.includes('qualifies'),
               'trial eligibility is explained in the tariff screen');
            window.tariffResponse={success:true,active:false,free_access:false,payment_required:true,
              purchase_available:false,trading_admitted:false,server_time:now,free_access_until:now-1};
            await refreshTelegramTariffs();
            assert(document.getElementById('telegram-tariff-modal').classList.contains('active'),
              'expiry prompt appears inside the mini app');
            assert(document.getElementById('telegram-tariff-modal-message').textContent.includes('not admitted'),
              'the prompt explains that subscription does not grant trading admission');
            assert(document.getElementById('telegram-tariff-modal-checkout').hidden,
              'non-admitted account cannot be sent to payment checkout');
            window.tariffResponse={success:true,active:true,admin_free:true,free_access:false,
              free_access_until:now+90,server_time:now,payment_required:false,purchase_available:true};
            await refreshTelegramTariffs();
            assert(document.getElementById('telegram-tariff-countdown').textContent.includes('ends in'),
              'administrator also sees the public free-access countdown');
            assert(document.getElementById('telegram-tariff-status').textContent.includes('permanent'),
              'administrator keeps permanent access');
            document.querySelector('[data-telegram-package="zrl_7d"]').click();
            assert(document.getElementById('telegram-package-modal-status').textContent.includes('permanent free access'),
              'package click explains that administrator access does not require payment');
            assert(document.getElementById('telegram-package-modal-checkout').hidden,
              'administrator is not sent to a payment flow');
        """)

    def test_telegram_tariff_cards_open_details_and_selected_package_checkout(self):
        page=self.telegram_page("""<script>
        window.openedTariffUrl='';
        const NativeURL=window.URL;
        window.URL=class extends NativeURL{
          constructor(input,base){super(input,typeof base==='string'&&base.startsWith('http')?base:'https://ai-trader.test');}
        };
        window.Telegram={WebApp:{initData:'signed-fixture',initDataUnsafe:{user:{id:101}},expand(){},ready(){},
          openLink(url){window.openedTariffUrl=url;}}};
        const now=Math.floor(Date.now()/1000);
        window.tariffResponse={success:true,active:false,free_access:false,free_access_until:now-1,
          server_time:now,payment_required:true,purchase_available:true,trading_admitted:true};
        window.fetch=async(url)=>new Response(JSON.stringify(url==='/api/subscription'?window.tariffResponse:{success:true}));
        </script>""")
        browser.AccountBrowserTests().run_page(page,"""
            gotoTab('tariffs',document.getElementById('nav-tariffs'));
            await refreshTelegramTariffs();
            document.querySelector('[data-telegram-package="sol_30d"]').click();
            const modal=document.getElementById('telegram-package-modal');
            await new Promise(resolve=>{
              const started=Date.now();
              const waitForAccess=()=>{
                const status=document.getElementById('telegram-package-modal-status').textContent;
                if(!status.includes('Checking account access…')||Date.now()-started>1500)resolve();
                else setTimeout(waitForAccess,10);
              };
              waitForAccess();
            });
            assert(modal.classList.contains('active'),'tapping a package opens its details');
            assert(document.getElementById('telegram-package-modal-title').textContent.includes('30-day'),
              'selected package is identified');
            const checkout=document.getElementById('telegram-package-modal-checkout');
            assert(!checkout.hidden,'eligible expired account can continue to secure checkout: '+JSON.stringify({
              hidden:checkout.hidden,access:telegramTariffState,status:document.getElementById('telegram-package-modal-status').textContent}));
            checkout.click();
            assert(new URL(window.openedTariffUrl).searchParams.get('package')==='sol_30d',
              'checkout opens with the selected SOL package');
        """)

    def test_checkout_deep_link_selects_the_requested_package(self):
        page=self.page("<script>window.fetch=async()=>new Response(JSON.stringify({success:true,user:null}));</script>")
        browser.AccountBrowserTests().run_page(page,"""
            focusRequestedPackage();
            assert(document.getElementById('tab-tariffs').classList.contains('active'),
              'checkout link opens the Tariffs tab');
            assert(document.querySelector('[data-package-id="sol_30d"]').classList.contains('requested-package'),
              'the selected package is highlighted');
        ""","?package=sol_30d")

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

    def test_pending_operation_recovery_is_automatic_and_operation_specific(self):
        page=self.page("""<script>
        window.confirm=()=>{throw Error('Native confirm should not be used in Telegram iOS');};
        window.fetch=async()=>new Response(JSON.stringify({success:false,message:'fixture'}));
        </script>""")
        self.assertNotIn('recover-pending-operation', page)
        self.assertNotIn('recovery-confirm-modal', page)
        self.assertNotIn('/api/recover_expired_operation', page)
        self.assertIn("Amount: ", page)
        self.assertNotIn("Reserved: ", page)
        self.assertIn("Do not sell this position again.", page)
        self.assertIn("The server checks the result automatically.", page)
        self.assertNotIn("Do not submit another withdrawal until this status is resolved.", page)

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
