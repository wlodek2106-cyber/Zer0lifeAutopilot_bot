/* Standalone only. Never included in the Telegram mini-app page. */
let webLoginPending=null,accessOrder=null,accessCheckBusy=false;
const webMessage=document.getElementById('web-login-status');
const webSignIn=document.getElementById('web-signin');
const b64decode=s=>Uint8Array.from(atob(s.replace(/-/g,'+').replace(/_/g,'/')+'='.repeat((4-s.length%4)%4)),c=>c.charCodeAt(0));
const b64encode=b=>btoa(String.fromCharCode(...new Uint8Array(b))).replace(/\+/g,'-').replace(/\//g,'_').replace(/=+$/,'');
function loginOptions(source){
 const o=JSON.parse(JSON.stringify(source));o.challenge=b64decode(o.challenge);
 if(o.allowCredentials)o.allowCredentials=o.allowCredentials.map(c=>({...c,id:b64decode(c.id)}));
 return o;
}
function loginCredential(c){
 const response={};
 for(const key of ['authenticatorData','clientDataJSON','signature','userHandle'])
  if(c.response[key])response[key]=b64encode(c.response[key]);
 return {id:c.id,rawId:b64encode(c.rawId),type:c.type,response,clientExtensionResults:c.getClientExtensionResults()};
}
async function webPost(path,body){
 const r=await fetch(path,{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},
  body:JSON.stringify(body)});
 const data=await r.json();
 if(!r.ok||!data.success)throw Error(data.message||'Request failed.');
 return data;
}
async function loadWebSession(){
 const r=await fetch('/web-auth/me',{credentials:'same-origin',cache:'no-store'});
 if(!r.ok)throw Error('Could not check browser session.');
 const data=await r.json();
 window.webSession=data.user?data:null;
 const register=document.getElementById('web-register');
 if(data.registration_url){register.href=data.registration_url;register.hidden=false;}
 return data;
}
webSignIn.onclick=async()=>{
 webSignIn.disabled=true;
 try{
  const pin=document.getElementById('web-pin').value;
  if(!window.PublicKeyCredential||!navigator.credentials)throw Error('Open the standalone terminal in Safari or Chrome with passkey support.');
  if(!webLoginPending){
   const data=await webPost('/web-auth/start',{nickname:document.getElementById('web-nickname').value,pin});
   const verification=await webPost('/passkey/options',{ticket:data.ticket,pin});
   webLoginPending={ticket:data.ticket,options:verification.options};
   webMessage.textContent='Tap below to approve this browser sign-in with your device. No funds are sent.';
   webSignIn.textContent='Sign in with passkey';
   return;
  }
  // A fresh direct user gesture, not a network callback, starts native WebAuthn.
  const c=await navigator.credentials.get({publicKey:loginOptions(webLoginPending.options)});
  await webPost('/passkey/verify',{ticket:webLoginPending.ticket,pin,credential:loginCredential(c)});
  await webPost('/web-auth/finish',{ticket:webLoginPending.ticket});
  webLoginPending=null;document.getElementById('web-pin').value='';
  await loadWebSession();
  document.getElementById('web-login').hidden=true;
  await openTerminal();
  await refreshAccess();
  focusRequestedPackage();
 }catch(e){
  webMessage.textContent=e.name==='NotAllowedError'?'Cancelled. Tap again to retry.':e.message;
  if(e.name!=='NotAllowedError'){webLoginPending=null;webSignIn.textContent='Continue';}
 }finally{webSignIn.disabled=false;}
};
for(const id of ['web-nickname','web-pin'])document.getElementById(id).addEventListener('input',()=>{
 webLoginPending=null;webSignIn.textContent='Continue';
});
const subscriptionCard=document.getElementById('subscription-card');
document.getElementById('tab-tariffs').prepend(subscriptionCard);
const tariffsNav=document.getElementById('nav-tariffs');
const requestedPackageId=['zrl_7d','sol_30d'].includes(new URLSearchParams(location.search).get('package'))?
  new URLSearchParams(location.search).get('package'):null;
function focusRequestedPackage(){
 if(!window.ZRL_WEB||!requestedPackageId)return;
 gotoTab('tariffs',tariffsNav);
 const button=[...document.querySelectorAll('[data-package-id]')]
  .find(candidate=>candidate.dataset.packageId===requestedPackageId);
 if(button){
  button.classList.add('requested-package');
  button.scrollIntoView({behavior:'smooth',block:'center'});
  if(!button.disabled)button.focus();
 }else{
  const status=document.getElementById('subscription-state');
  status?.setAttribute('tabindex','-1');status?.focus();
 }
}
window.focusRequestedPackage=focusRequestedPackage;
if(window.ZRL_WEB)tariffsNav.hidden=false;
let accessNotice=document.createElement('p');accessNotice.className='section-note';accessNotice.id='overview-access';
document.getElementById('tab-terminal').prepend(accessNotice);
const tariffModal=document.getElementById('tariff-modal');
let accessSnapshot=null,serverClockOffset=0,paywallDismissed=false,expiryRefreshStarted=false;
function showAccess(data){
  accessSnapshot=data;
  serverClockOffset=(Number(data.server_time||0)*1000)-Date.now();
  expiryRefreshStarted=false;
 window.webAccessActive=data.active===true;
 subscriptionCard.hidden=false;
  let text=data.admin_free?'Administrator · free permanent access':
   data.free_access?'Free access for everyone · until 1 Nov 2026, 00:00 Warsaw time':
   data.active?'Paid access active until '+new Date(data.expires_at*1000).toLocaleString():
   'Access expired · automatic trading is stopped';
  if(!data.trading_admitted&&!data.admin_free)
   text+=' · this account is not admitted to custody/trading';
 document.getElementById('subscription-state').textContent=text;
 accessNotice.textContent=text;
 document.getElementById('subscription-purchase').hidden=data.admin_free;
  document.querySelectorAll('[data-package-id]').forEach(button=>{
   button.disabled=!data.purchase_available||data.free_access===true;
  });
 if(!data.admin_free&&!data.purchase_available)
   document.getElementById('subscription-state').textContent=text+
    '. Paid checkout is not open for this account; no payment is requested.';
  const modalNote=document.getElementById('tariff-modal-note');
  modalNote.textContent=data.purchase_available?
   'Choose a package below. The payment is verified on Solana and is never added to your trading balance.':
   'Paid access is not open for this account. No payment request will be created; custody admission remains unchanged.';
  tariffModal.hidden=!(data.payment_required&&!data.admin_free&&!paywallDismissed);
  tickFreeCountdown();
 // Manual close-all lives in the activity card and is never disabled here.
  document.querySelectorAll('[onclick="toggleTrading()"],[onclick="toggleMemeTrading()"]')
   .forEach(b=>b.disabled=!data.active||data.trading_admitted!==true);
}
function showPaymentRequest(order,recipient){
 accessOrder=order.id;
 const paymentRecipient=order.recipient||recipient;
 if(!paymentRecipient)throw Error('Payment recipient is missing from this invoice.');
 document.getElementById('subscription-address').textContent=paymentRecipient;
  document.getElementById('subscription-invoice-label').textContent=
   `Exact invoice: ${order.amount} ${order.asset} · ${order.label} access`;
 document.getElementById('subscription-deadline').textContent=
   `Send ${order.amount} ${order.asset} from ${order.sender} before `+
   new Date(order.deadline*1000).toLocaleString()+
   '. If already sent within this window, verify that same transaction later; do not resend. Payment is not credited to your trading balance.';
  const params=new URLSearchParams({amount:order.amount,label:'AI Trader',message:`${order.days}-day access`});
  if(order.mint)params.set('spl-token',order.mint);
  const paymentUri=`solana:${paymentRecipient}?${params.toString()}`;
  const qr=document.getElementById('subscription-qr');
  qr.src='https://api.qrserver.com/v1/create-qr-code/?size=200x200&data='+encodeURIComponent(paymentUri);
  qr.hidden=false;
 document.getElementById('subscription-transfer').hidden=false;
}
async function createSubscriptionOrder(packageId){
 const buttons=[...document.querySelectorAll('[data-package-id]')];
 buttons.forEach(button=>button.disabled=true);
 try{
  const r=await apiFetch('/api/subscription/order',{method:'POST',headers:{'Content-Type':'application/json'},
   body:JSON.stringify({package_id:packageId})});
  const data=await r.json();
  if(!r.ok||!data.success)throw Error(data.message||'Could not create payment request.');
  paywallDismissed=true;tariffModal.hidden=true;
  showPaymentRequest(data.order,data.recipient);
  gotoTab('tariffs',tariffsNav);
  document.getElementById('subscription-transfer').scrollIntoView({behavior:'smooth',block:'nearest'});
 }catch(e){
  document.getElementById('subscription-state').textContent=e.message;
 }finally{
  buttons.forEach(button=>button.disabled=!accessSnapshot?.purchase_available||accessSnapshot?.free_access===true);
 }
}
async function refreshAccess(){
 if(!window.webSession?.user||accessCheckBusy)return;
 accessCheckBusy=true;
 try{
  const r=await apiFetch('/api/subscription'),data=await r.json();
  if(!r.ok||!data.success)throw Error(data.message||'Could not check access.');
  showAccess(data);
  if(data.pending_order&&!data.admin_free)showPaymentRequest(data.pending_order,data.recipient);
 }catch(e){document.getElementById('subscription-state').textContent=e.message;}
 finally{accessCheckBusy=false;}
}
document.querySelectorAll('[data-package-id]').forEach(button=>
 button.addEventListener('click',()=>createSubscriptionOrder(button.dataset.packageId)));
document.getElementById('tariff-modal-close').onclick=()=>{
 paywallDismissed=true;tariffModal.hidden=true;
};
document.addEventListener('keydown',event=>{
 if(event.key==='Escape'&&!tariffModal.hidden){
  paywallDismissed=true;tariffModal.hidden=true;
 }
});
document.getElementById('subscription-copy').onclick=async()=>{
 try{await navigator.clipboard.writeText(document.getElementById('subscription-address').textContent);}
 catch(e){document.getElementById('subscription-state').textContent='Select and copy the payment address shown below.';}
};
document.getElementById('subscription-verify').onclick=async()=>{
 const button=document.getElementById('subscription-verify');button.disabled=true;
 try{
  if(!accessOrder)throw Error('Create a payment request first.');
  const r=await apiFetch('/api/subscription/verify',{method:'POST',headers:{'Content-Type':'application/json'},
   body:JSON.stringify({order_id:accessOrder,tx_signature:document.getElementById('subscription-tx').value})});
  const data=await r.json();if(!r.ok||!data.success)throw Error(data.message||'Payment is not verified.');
  showAccess(data);document.getElementById('subscription-state').textContent+=' · Payment verified';
   document.getElementById('subscription-transfer').hidden=true;
   document.getElementById('subscription-qr').hidden=true;accessOrder=null;
 }catch(e){document.getElementById('subscription-state').textContent=e.message;}
 finally{button.disabled=false;}
};
document.getElementById('web-logout').onclick=async()=>{
 try{
  const r=await fetch('/web-auth/logout',{method:'POST',credentials:'same-origin',headers:{
   'Content-Type':'application/json','X-ZRL-CSRF':window.webSession.csrf},body:'{}'});
  if(!r.ok)throw Error('Could not sign out.');
  window.webSession=null;location.reload();
 }catch(e){document.getElementById('subscription-state').textContent=e.message;}
};
loadWebSession().then(async()=>{
 if(!window.webSession)document.getElementById('web-login').hidden=false;
  else if(requestedPackageId){
   document.getElementById('web-login').hidden=true;
   await openTerminal();
   await refreshAccess();
   if(document.getElementById('nav').style.display!=='none')focusRequestedPackage();
  }else refreshAccess();
}).catch(e=>{document.getElementById('web-login').hidden=false;webMessage.textContent=e.message;});
setInterval(refreshAccess,15000);
function tickFreeCountdown(){
 const label=document.getElementById('subscription-free-countdown');
  if(!accessSnapshot){label.textContent='';return;}
  const until=Number(accessSnapshot.free_access_until||0);
  if(!until){label.textContent='';return;}
  const seconds=until-Math.floor((Date.now()+serverClockOffset)/1000);
 if(seconds<=0){
   label.textContent=accessSnapshot.admin_free?
    'Public free access has ended · your administrator access remains permanent.':
    accessSnapshot.active?'Public free access has ended · your paid access remains active.':
    'Free access period has ended.';
   if(accessSnapshot.free_access&&!expiryRefreshStarted){expiryRefreshStarted=true;refreshAccess();}
  return;
 }
 const days=Math.floor(seconds/86400),hours=Math.floor(seconds%86400/3600);
 const minutes=Math.floor(seconds%3600/60),remaining=seconds%60;
 const countdown=`${days}d ${hours}h ${minutes}m ${remaining}s`;
  const ownerNote=accessSnapshot.admin_free?' · your administrator access remains permanent':'';
  label.textContent=`Public free access ends in ${countdown} · 1 Nov 2026 at 00:00 Europe/Warsaw${ownerNote}`;
  if(accessSnapshot.free_access&&!accessSnapshot.admin_free)
   accessNotice.textContent=`Free access · ${countdown} remaining · custody/trading admission is unchanged`;
}
setInterval(tickFreeCountdown,1000);
