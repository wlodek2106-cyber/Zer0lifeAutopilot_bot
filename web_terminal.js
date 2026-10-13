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
 }catch(e){
  webMessage.textContent=e.name==='NotAllowedError'?'Cancelled. Tap again to retry.':e.message;
  if(e.name!=='NotAllowedError'){webLoginPending=null;webSignIn.textContent='Continue';}
 }finally{webSignIn.disabled=false;}
};
for(const id of ['web-nickname','web-pin'])document.getElementById(id).addEventListener('input',()=>{
 webLoginPending=null;webSignIn.textContent='Continue';
});
const subscriptionCard=document.getElementById('subscription-card');
document.getElementById('tab-trading').insertBefore(subscriptionCard,document.getElementById('trading-controls'));
let accessNotice=document.createElement('p');accessNotice.className='section-note';accessNotice.id='overview-access';
document.getElementById('tab-terminal').prepend(accessNotice);
function showAccess(data){
 window.webAccessActive=data.active===true;
 subscriptionCard.hidden=false;
 const text=data.admin_free?'Administrator · free permanent access':
  data.active?'Active until '+new Date(data.expires_at*1000).toLocaleString():
  'Inactive · automatic trading is stopped';
 document.getElementById('subscription-state').textContent=text;
 accessNotice.textContent=text;
 document.getElementById('subscription-purchase').hidden=data.admin_free;
 document.getElementById('subscription-order').disabled=!data.purchase_available;
 if(!data.admin_free&&!data.purchase_available)
  document.getElementById('subscription-state').textContent=text+'. Customer access is not open; no payment is requested.';
 // Manual close-all lives in the activity card and is never disabled here.
 document.querySelectorAll('[onclick="toggleTrading()"],[onclick="toggleMemeTrading()"]').forEach(b=>b.disabled=!data.active);
}
function showPaymentRequest(order,recipient){
 accessOrder=order.id;
 document.getElementById('subscription-address').textContent=recipient;
 document.getElementById('subscription-deadline').textContent=
  'Send from '+order.sender+' before '+new Date(order.deadline*1000).toLocaleString()+
  '. Already sent? Verify the same Tx even after this window; do not resend.';
 document.getElementById('subscription-transfer').hidden=false;
}
async function refreshAccess(){
 if(!window.webSession?.user||accessCheckBusy)return;
 accessCheckBusy=true;
 try{
  const r=await apiFetch('/api/subscription'),data=await r.json();
  if(!r.ok||!data.success)throw Error(data.message||'Could not check access.');
  showAccess(data);
  if(data.pending_order&&!data.admin_free&&!accessOrder)showPaymentRequest(data.pending_order,data.recipient);
 }catch(e){document.getElementById('subscription-state').textContent=e.message;}
 finally{accessCheckBusy=false;}
}
document.getElementById('subscription-order').onclick=async()=>{
 const button=document.getElementById('subscription-order');button.disabled=true;
 try{
  const r=await apiFetch('/api/subscription/order',{method:'POST'}),data=await r.json();
  if(!r.ok||!data.success)throw Error(data.message||'Could not create payment request.');
  showPaymentRequest(data.order,data.recipient);
 }catch(e){document.getElementById('subscription-state').textContent=e.message;}
 finally{button.disabled=false;}
};
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
  document.getElementById('subscription-transfer').hidden=true;accessOrder=null;
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
loadWebSession().then(()=>{
 if(!window.webSession)document.getElementById('web-login').hidden=false;
 else refreshAccess();
}).catch(e=>{document.getElementById('web-login').hidden=false;webMessage.textContent=e.message;});
setInterval(refreshAccess,15000);
