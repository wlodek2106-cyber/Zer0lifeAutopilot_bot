(() => {
"use strict";
if (window.__zrlPaperIntelligenceLoaded) return;
window.__zrlPaperIntelligenceLoaded = true;
const style=document.createElement("style");
style.textContent=".zpi-card{background:linear-gradient(145deg,#101d19,#090f0d);border:1px solid rgba(20,241,149,.22);border-radius:18px;padding:16px;margin:14px 0;color:#edf7f1}.zpi-head{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:12px}.zpi-title{font-size:17px;font-weight:850;margin:0}.zpi-tag{font-size:10px;color:#a8f6cb;border:1px solid #285b48;border-radius:20px;padding:5px 8px}.zpi-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:9px}.zpi-metric{background:#080f0d;border:1px solid #20372d;border-radius:13px;padding:12px;min-width:0}.zpi-label{font-size:10px;color:#8fa69a;text-transform:uppercase;letter-spacing:.8px;font-weight:800}.zpi-value{font-size:20px;font-weight:900;margin-top:6px;overflow-wrap:anywhere;font-family:ui-monospace,monospace}.zpi-note{font-size:11px;color:#91a198;line-height:1.45;margin:10px 0 0}.zpi-row{display:flex;justify-content:space-between;gap:10px;padding:10px 0;border-bottom:1px solid rgba(255,255,255,.07);font-size:12px}.zpi-positive{color:#66e6ad}.zpi-negative{color:#ff8d8d}.zpi-refresh{border:1px solid #315b49;background:#10251b;color:#c9ffe0;border-radius:10px;padding:8px 11px;font-weight:800}.zpi-status{font-size:12px;color:#a3b1aa;line-height:1.5}.zpi-chat{display:flex;gap:8px;margin-top:12px}.zpi-chat input{flex:1;min-width:0;background:#080f0d;border:1px solid #294438;border-radius:11px;padding:11px;color:#fff;font-size:13px}.zpi-chat button{background:#b7f52e;border:0;border-radius:11px;padding:0 13px;font-weight:900;color:#10170a}.zpi-answer{margin-top:10px;background:#0a1410;border-left:3px solid #a9ec37;border-radius:8px;padding:10px;font-size:12px;line-height:1.5;color:#dce9e1}@media(max-width:360px){.zpi-value{font-size:17px}.zpi-card{padding:13px}}";
document.head.appendChild(style);
const money=v=>{const n=Number(v);return Number.isFinite(n)?(n<0?"−$":"+$")+Math.abs(n).toFixed(2):"—";};
const number=v=>Number.isFinite(Number(v))?Number(v).toLocaleString(undefined,{maximumFractionDigits:6}):"—";
const esc=v=>String(v??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const stamp=v=>{if(!v)return 0;const s=String(v);const n=Date.parse(/[zZ]|[+-]\d\d:\d\d$/.test(s)?s:s.replace(" ","T")+"Z");return Number.isFinite(n)?n:0;};
function ensurePanels(){
 const overview=document.getElementById("tab-terminal"),trading=document.getElementById("tab-trading");
 if(overview&&!document.getElementById("zpi-overview")){
  const e=document.createElement("section");e.id="zpi-overview";e.className="zpi-card";
  e.innerHTML='<div class="zpi-head"><h3 class="zpi-title">Paper Trading · аналітика</h3><span class="zpi-tag">ЛИШЕ СИМУЛЯЦІЯ</span></div><div class="zpi-grid"><div class="zpi-metric"><div class="zpi-label">PnL за 7 днів</div><div class="zpi-value" data-zpi="pnl7">—</div></div><div class="zpi-metric"><div class="zpi-label">SELL у плюс</div><div class="zpi-value" data-zpi="winrate">—</div></div><div class="zpi-metric"><div class="zpi-label">Profit factor</div><div class="zpi-value" data-zpi="pf">—</div></div><div class="zpi-metric"><div class="zpi-label">Відкриті paper-позиції</div><div class="zpi-value" data-zpi="positions">—</div></div></div><p class="zpi-note" data-zpi="overview-status">Завантаження персонального журналу…</p>';
  overview.appendChild(e);
 }
 if(trading&&!document.getElementById("zpi-trading")){
  const e=document.createElement("section");e.id="zpi-trading";e.className="zpi-card";
  e.innerHTML='<div class="zpi-head"><h3 class="zpi-title">Trading Intelligence</h3><button type="button" class="zpi-refresh" data-zpi-refresh>↻ Оновити</button></div><div class="zpi-grid"><div class="zpi-metric"><div class="zpi-label">Реалізований PnL · весь журнал</div><div class="zpi-value" data-zpi="total">—</div></div><div class="zpi-metric"><div class="zpi-label">Комісії paper</div><div class="zpi-value" data-zpi="fees">—</div></div><div class="zpi-metric"><div class="zpi-label">SELL у плюс</div><div class="zpi-value" data-zpi="winrate2">—</div></div><div class="zpi-metric"><div class="zpi-label">Profit factor</div><div class="zpi-value" data-zpi="pf2">—</div></div></div><p class="zpi-note" data-zpi="account"></p><h4 style="margin:16px 0 4px;font-size:14px">Останні paper-виконання</h4><div data-zpi="fills" class="zpi-status">Завантаження…</div><h4 style="margin:16px 0 4px;font-size:14px">Помічник торгового журналу</h4><p class="zpi-note">Відповіді формуються локально з вашої статистики Paper Trading; це не зовнішня AI-модель і не торговий сигнал.</p><div class="zpi-chat"><input data-zpi-question maxlength="180" placeholder="Наприклад: який мій PnL?"><button type="button" data-zpi-ask>Запит</button></div><div data-zpi="answer" class="zpi-answer" hidden></div><p class="zpi-note" data-zpi="trading-status" role="status">Персональний журнал ще не завантажено.</p>';
  const anchor=document.getElementById("trading-ai-activity");if(anchor)anchor.insertAdjacentElement("afterend",e);else trading.appendChild(e);
 }
}
let latest=null;
function render(data){
 latest=data;const fills=Array.isArray(data.fills)?data.fills:[],sells=fills.filter(f=>f.side==="SELL"),now=Date.now(),week=now-604800000;
 const ws=sells.filter(f=>stamp(f.created_at)>=week&&stamp(f.created_at)<=now+60000),pnl7=ws.reduce((a,f)=>a+(Number(f.realized_pnl)||0),0);
 const wins=sells.filter(f=>Number(f.realized_pnl)>0).length,wr=sells.length?wins/sells.length*100:null;
 const gains=sells.reduce((a,f)=>a+Math.max(0,Number(f.realized_pnl)||0),0),losses=sells.reduce((a,f)=>a+Math.max(0,-(Number(f.realized_pnl)||0)),0),pf=losses>0?(gains/losses).toFixed(2):gains>0?"∞":"—";
 const summary=data.summary||{},account=data.account||{};
 const set=(k,v,positive)=>document.querySelectorAll('[data-zpi="'+k+'"]').forEach(el=>{el.textContent=v;if(positive!==undefined)el.classList.toggle("zpi-negative",!positive);});
 set("pnl7",money(pnl7),pnl7>=0);set("winrate",wr===null?"—":wr.toFixed(1)+"%");set("pf",pf);set("positions",String((account.positions||[]).length));
 set("total",money(summary.realized_pnl),Number(summary.realized_pnl||0)>=0);set("fees","$"+number(summary.fees_paid));set("winrate2",wr===null?"—":wr.toFixed(1)+"%");set("pf2",pf);
 const pos=(account.positions||[]).length?(account.positions||[]).map(p=>esc(p.symbol)+": "+number(p.quantity)).join(" · "):"Відкритих paper-позицій немає.";
 set("account","Paper cash: "+(account.cash==null?"ще не створено":"$"+number(account.cash))+" · "+pos);
 set("overview-status",sells.length+" SELL-виконань у журналі · "+ws.length+" за останні 7 днів. Це paper-дані, не реальний баланс.");
 const node=document.querySelector('[data-zpi="fills"]');if(node){if(!fills.length)node.textContent="Журнал поки порожній. Paper-виконання з’являться після симуляції.";else node.innerHTML=fills.slice(0,8).map(f=>{const p=Number(f.realized_pnl)||0,side=f.side==="SELL"?"SELL":"BUY";return '<div class="zpi-row"><span><b>'+esc(side)+" · "+esc(f.symbol)+'</b><br><span style="color:#82958a">'+number(f.quantity)+" @ $"+number(f.price)+'</span></span><b class="'+(p>=0?"zpi-positive":"zpi-negative")+'">'+(side==="SELL"?money(p):"—")+"</b></div>";}).join("");}
 set("trading-status","Оновлено "+new Date().toLocaleTimeString()+" · "+(Number(summary.fills)||0)+" fills · журнал тільки для читання.");
}
async function load(){
 ensurePanels();const status=document.querySelector('[data-zpi="trading-status"]')||document.querySelector('[data-zpi="overview-status"]');
 try{if(typeof apiFetch!=="function")throw Error("Захищений API-клієнт недоступний.");const response=await apiFetch("/api/ai-trader/paper-journal?limit=50"),data=await response.json();if(!response.ok||!data.success)throw Error(data.message||"Не вдалося завантажити журнал.");render(data);}
 catch(error){if(status)status.textContent=error.message||"Помилка завантаження Paper Journal.";document.querySelectorAll('[data-zpi="overview-status"]').forEach(el=>el.textContent=error.message||"Журнал тимчасово недоступний.");}
}
function answer(){
 if(!latest)return;const input=document.querySelector("[data-zpi-question]"),out=document.querySelector('[data-zpi="answer"]'),q=(input?.value||"").toLowerCase(),fills=latest.fills||[],sells=fills.filter(f=>f.side==="SELL"),pnl=Number(latest.summary?.realized_pnl)||0,win=sells.length?sells.filter(f=>Number(f.realized_pnl)>0).length/sells.length*100:null;let a;
 if(/win|виграш|прибутков|успішн/.test(q))a=win===null?"Ще немає SELL-виконань для розрахунку частки прибуткових продажів.":"Прибутковими були "+win.toFixed(1)+"% SELL-виконань. Це статистика виконань, не окремих повністю закритих угод.";
 else if(/fee|комісі/.test(q))a="Сумарні paper-комісії: $"+number(latest.summary?.fees_paid)+".";
 else if(/7|тиж|week/.test(q)){const w=Date.now()-604800000,n=sells.filter(f=>stamp(f.created_at)>=w).reduce((x,f)=>x+(Number(f.realized_pnl)||0),0);a="Реалізований PnL SELL-виконань за 7 днів: "+money(n)+".";}
 else if(/position|позиці/.test(q))a=(latest.account?.positions||[]).length?"Відкриті paper-позиції: "+latest.account.positions.map(p=>p.symbol+" ("+number(p.quantity)+")").join(", ")+".":"Відкритих paper-позицій немає.";
 else a="Загальний реалізований Paper PnL: "+money(pnl)+". У журналі "+(Number(latest.summary?.fills)||0)+" виконань, з них "+sells.length+" SELL.";
 out.textContent=a;out.hidden=false;
}
document.addEventListener("click",e=>{if(e.target.closest("[data-zpi-refresh]"))load();if(e.target.closest("[data-zpi-ask]"))answer();});
document.addEventListener("keydown",e=>{if(e.target.matches("[data-zpi-question]")&&e.key==="Enter")answer();});
if(document.readyState==="loading")document.addEventListener("DOMContentLoaded",load,{once:true});else load();
})();