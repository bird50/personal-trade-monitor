'use strict';
const $ = id => document.getElementById(id);
let state = null, selectedTab = 'open', busy = false, providerBusy=false, refreshing = false, toastTimer;
const money = value => value == null ? '—' : Number(value).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2});
const num = value => value == null ? '—' : Number(value).toLocaleString('en-US',{maximumFractionDigits:3});
const time = seconds => !seconds ? '—' : new Date(seconds*1000).toLocaleTimeString('th-TH',{hour:'2-digit',minute:'2-digit',second:'2-digit'});
const duration = seconds => {const s=Math.max(0,Math.round(seconds||0));return s>=3600?`${Math.floor(s/3600)} ชม. ${Math.floor(s%3600/60)} นาที`:s>=60?`${Math.floor(s/60)} นาที ${s%60} วินาที`:`${s} วินาที`};
const esc = value => String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function text(id,value){$(id).textContent=value}
function dot(id,ok){$(id).className='dot '+(ok?'green':'amber')}
function signed(id,value){text(id,(Number(value)>0?'+':'')+money(value));$(id).classList.toggle('positive',Number(value)>0);$(id).classList.toggle('negative',Number(value)<0)}
function toast(message){text('toast',message);$('toast').classList.add('show');clearTimeout(toastTimer);toastTimer=setTimeout(()=>$('toast').classList.remove('show'),6500)}
async function bridge(action,extra={}){if(!window.webkit?.messageHandlers?.control)throw Error('เปิดหน้านี้ผ่านแอป XM Control');return window.webkit.messageHandlers.control.postMessage({action,...extra})}
async function refresh(){if(refreshing)return;refreshing=true;try{state=await bridge('state');render()}catch(e){state={connected:false};render()}finally{refreshing=false}}
function render(){
 const connected=state?.connected===true;
 $('pause').disabled=!connected||busy;
 $('providerLm').disabled=!connected||providerBusy;$('providerCodex').disabled=!connected||providerBusy;
 dot('systemDot',connected);
 text('lastUpdate',connected?'อัปเดต '+time(state.checked_at)+' · รีเฟรชทุก 5 วินาที':'ไม่พบระบบที่ 127.0.0.1:8002');
 const ns=state?.notifications;
 text('noticeStatus',ns==='enabled'?'เปิดใช้งานแล้ว':ns==='denied'?'ยังไม่ได้รับอนุญาต':'ยังไม่ได้เปิดใช้งาน');
 text('enableNotice',ns==='enabled'?'ทดสอบการแจ้งเตือน ↗':ns==='denied'?'ดูวิธีเปิดการแจ้งเตือน ↗':'เปิดการแจ้งเตือน ↗');dot('noticeDot',ns==='enabled');
 if(!connected){
  text('systemTitle','บริการในเครื่องยังไม่พร้อม');$('errorBanner').classList.remove('hidden');text('errorBanner','กด “เริ่มบริการ” เพื่อเปิดตัวรับราคาและระบบ Paper Trading หรือดู log ในโฟลเดอร์ .runtime หากเริ่มไม่สำเร็จ');
  ['feedStatus','aiStatus','paperStatus'].forEach(id=>text(id,'ไม่มีข้อมูลปัจจุบัน'));['feedDot','aiDot','paperDot'].forEach(id=>dot(id,false));
  ['feedDetail','aiDetail','paperDetail'].forEach(id=>text(id,'รอเชื่อมต่อบริการอีกครั้ง'));text('entryStatus','สถานะการเปิดไม้ยังไม่ยืนยัน · บริการออฟไลน์');
  text('valuation','ข้อมูลบัญชีรอเชื่อมต่ออีกครั้ง');return;
 }
 const a=state.account,f=state.feed,ai=state.ai,t=state.ticks||{};
 const selection=state.ai_selection||{provider:'lm_studio',label:'LM Studio',model:state.model};
 const codex=selection.provider==='codex',aiReady=ai.ready??(ai.available&&ai.model_loaded);
 const codexIssue={codex_cli_unavailable:'ไม่พบ Codex CLI ในเครื่อง ตรวจการติดตั้ง',codex_usage_limit:'ถึงขีดจำกัดการใช้ Codex รอรีเซ็ตโควตาหรือเลือก LM Studio',codex_model_unavailable:'บัญชีนี้ยังเรียกโมเดล Codex ที่ตั้งไว้ไม่ได้'}[ai.error]||'Codex ยังไม่พร้อม กรุณาใช้ codex login ด้วยบัญชี ChatGPT ใน Terminal';
 for(const [id,provider] of [['providerLm','lm_studio'],['providerCodex','codex']]){const chosen=selection.provider===provider;$(id).classList.toggle('selected',chosen);$(id).setAttribute('aria-pressed',String(chosen))}
 text('providerModel',selection.label+' · '+selection.model);
 const active=selection.in_flight;
 text('providerUsage',active?`กำลังวิเคราะห์ #${active.signal_record_id} ด้วย ${active.provider==='codex'?'Codex':'LM Studio'}`:codex?'ใช้โควตา Codex · ส่งสัญญาณไป OpenAI':'ประมวลผลผ่าน LM Studio ตามที่ตั้งไว้');
 text('pipeline',`◈  XM → Python → ${selection.label} → Risk → Paper`);
 $('aiStatus').parentElement.querySelector('small').textContent=codex?'CODEX · CHATGPT':'LM STUDIO';
 const recovering=!a.valuation_fresh&&t.status==='catching_up';
 const workers=Object.values(state.workers).every(Boolean),ready=a.entries_enabled&&f.available&&aiReady&&workers;
 dot('systemDot',ready);text('systemTitle',a.operator_paused?'พักการเปิดไม้ใหม่ · ติดตามไม้เดิมต่อ':ready?'ระบบพร้อม · รอสัญญาณเข้าเงื่อนไข':'ระบบทำงาน · มีเงื่อนไขที่ต้องตรวจสอบ');
 const warning=!f.available?'ราคาจาก MT5 ยังไม่สด ตรวจการเชื่อมต่อ XM และ EA ทั้งสองตัว':!aiReady?(codex?codexIssue:'LM Studio ยังไม่พร้อม ตรวจ Server และโมเดลที่ตั้งไว้'):recovering?`กำลังรับ Tick ย้อนหลัง · ตามหลัง ${duration(t.coverage_age_seconds)} · EA ยังส่งข้อมูลอยู่ รอให้ตามทันก่อนเปิดไม้ใหม่`:!a.valuation_fresh?'ยังไม่มี Tick สดสำหรับประเมินบัญชี ตรวจ EA XMPaperTicks':!workers?'มี worker หยุดทำงาน กรุณาตรวจ log':!a.policy_matches?'กฎบัญชีเปลี่ยน ระบบระงับการเปิดสถานะใหม่':a.day?.locked?'ถึงเพดานขาดทุนรายวัน ระบบติดตามไม้เดิมต่อ':'';
 $('errorBanner').classList.toggle('hidden',!warning);text('errorBanner',warning);
 text('pause',a.operator_paused?'▶ เปิดรับไม้ใหม่':'Ⅱ พักเปิดไม้ใหม่');
 text('feedStatus',f.available?'รับราคาสด':'ราคาไม่พร้อม');dot('feedDot',f.available);text('feedDetail',f.bid!=null?`${f.symbol} ${num(f.bid)} / ${num(f.ask)} · ${Math.round(f.age_seconds)}s`:'รอ EA XMPriceBridge');
 text('aiStatus',codex?(aiReady?'ล็อกอินพร้อมเรียกวิเคราะห์':'ยังไม่พร้อมใช้งาน'):aiReady?'โมเดลพร้อมใช้งาน':ai.available?'ไม่พบโมเดลที่ตั้งไว้':'เชื่อมต่อไม่ได้');dot('aiDot',aiReady);text('aiDetail',state.model);
 text('paperStatus',a.valuation_fresh?'ติดตาม Tick สด':recovering?'กำลังรับ Tick ย้อนหลัง':'รอ Tick / ตรวจข้อมูล');dot('paperDot',a.valuation_fresh);text('paperDetail',recovering?'ตามหลัง '+duration(t.coverage_age_seconds):a.status+(a.mark_age_seconds!=null?' · '+Math.round(a.mark_age_seconds)+'s':''));
 text('equity',money(a.equity_usd));text('balance',money(a.balance_usd));signed('floating',a.unrealized_pnl_usd);signed('realized',a.realized_cash_pnl_usd);
 text('valuation',a.valuation_fresh?'มูลค่าบัญชี ณ Tick ล่าสุด':'มูลค่าจาก Tick เดิม · ราคายังไม่สด');text('openCount',`${a.open_positions} สถานะเปิด · Risk $${money(a.open_planned_risk_usd)}`);text('closedCount',`${a.closed_trades} เทรดปิด · รวมค่าธรรมเนียมที่บันทึกแล้ว`);
 text('risk',state.policy.risk_percent+'%');text('dailyRisk',state.policy.daily_loss_percent+'%');text('maxPositions',state.policy.max_positions+' สถานะ');text('entryStatus',a.operator_paused?'Ⅱ พักการเปิดไม้ใหม่ตามคำสั่งผู้ใช้':a.entries_enabled?'● ผ่านเงื่อนไขบัญชี · รอ AI / Risk ต่อสัญญาณ':'● ยังไม่ผ่านเงื่อนไขบัญชี');
 renderChart(state.equity_history);renderPositions();renderAnalysis();renderEvents();
}
function renderChart(points){
 const p=(points||[]).filter(x=>Number.isFinite(Number(x.equity)));
 $('chartEmpty').classList.toggle('hidden',p.length>0);if(!p.length){$('chartContent').innerHTML='';return}
 let lo=Math.min(...p.map(x=>Number(x.equity))),hi=Math.max(...p.map(x=>Number(x.equity)));const pad=Math.max((hi-lo)*.2,1);lo-=pad;hi+=pad;
 const x=i=>p.length===1?350:10+i/(p.length-1)*680,y=v=>145-(v-lo)/(hi-lo)*130;
 const line=p.map((v,i)=>`${i?'L':'M'}${x(i).toFixed(2)},${y(Number(v.equity)).toFixed(2)}`).join(' ');
 const area=line+` L${x(p.length-1)},155 L${x(0)},155 Z`;
 $('chartContent').innerHTML=`<path d="M0 15H700 M0 80H700 M0 145H700" fill="none" stroke="#2a3236" stroke-dasharray="3 5"/><path d="${area}" fill="url(#area)"/><path d="${line}" fill="none" stroke="#d7b978" stroke-width="2"/><circle cx="${x(p.length-1)}" cy="${y(Number(p.at(-1).equity))}" r="3" fill="#e4cd99"/>`;
 text('chartFrom',time(p[0].at));text('chartTo',time(p.at(-1).at));text('chartScale',`USD ${money(lo+pad)} – ${money(hi-pad)} · ${p.at(-1).fresh?'Tick สด ณ จุดล่าสุด':'จุดล่าสุดใช้ราคาเดิม'}`);
}
function renderPositions(){if(!state?.connected)return;const rows=selectedTab==='open'?state.positions:state.closed;const items=[...rows].reverse();text('positionBadge',state.account.open_positions);$('positionsEmpty').classList.toggle('hidden',items.length>0);$('positions').innerHTML=items.map(p=>`<tr><td><b class="${p.side==='BUY'?'positive':'negative'}">${esc(p.side)}</b> <small>#${p.id} · ${time((selectedTab==='open'?p.opened_utc_ms:p.closed_utc_ms)/1000)}</small></td><td>${num(p.quantity)}</td><td>${num(p.entry)}</td><td>${num(p.sl)}<small>${num(p.tp)}</small></td><td class="right ${Number(p.net_pnl??p.unrealized_pnl)>0?'positive':Number(p.net_pnl??p.unrealized_pnl)<0?'negative':''}">${money(p.net_pnl??p.unrealized_pnl)}<small>${selectedTab==='closed'?esc(p.exit_reason):'ยังไม่ปิดสถานะ'}</small></td></tr>`).join('');
 const empty=$('positionsEmpty');empty.querySelector('strong').textContent=selectedTab==='open'?'ยังไม่มีสถานะเปิด':'ยังไม่มีเทรดที่ปิดแล้ว';empty.querySelector('p').textContent=selectedTab==='open'?'ระบบจะเปิดเมื่อมีสัญญาณผ่าน AI และ Risk ครบทุกเงื่อนไข':'เมื่อปิดตาม SL/TP ระบบจะบันทึกกำไรขาดทุนไว้ที่นี่';
}
function renderAnalysis(){const a=state.analysis; text('analysisModel',a?`${a.provider==='codex'?'Codex':'LM Studio'} · ${a.model||'ยังไม่ระบุโมเดล'}`:'รอสัญญาณใหม่');text('candidateCount',state.candidate_count+' สัญญาณ');text('engineStatus','Signal engine · '+(state.engine.status||'—'));if(!a)return;
 text('analysisStatus',a.status.toUpperCase());text('decision',a.effective_decision);text('analysisTitle',`สัญญาณ #${a.signal_record_id} · AI ${a.ai_decision}`);text('analysisDetail',`Risk: ${a.risk_status} · ${new Date(a.updated_at*1000).toLocaleDateString('th-TH',{day:'numeric',month:'short'})} ${time(a.updated_at)}`);text('analysisReason',a.result?.reasons?.join('\n')||a.error_code||'รอ AI วิเคราะห์สัญญาณ ไม่เปิดสถานะจนกว่าจะผ่านการตรวจครบ');
}
function renderEvents(){const items=state.recent_events||[];$('eventsEmpty').classList.toggle('hidden',items.length>0);$('events').innerHTML=items.map(e=>`<div class="event ${esc(e.level)}"><span class="event-mark">${e.level==='warning'?'!':e.kind==='analysis'?'✧':e.kind==='paper_open'?'↗':e.kind==='paper_close'?'↙':'●'}</span><div class="event-body"><strong>${esc(e.title)}</strong><p>${esc(e.message)}</p><time>${new Date(e.at*1000).toLocaleDateString('th-TH',{day:'numeric',month:'short'})} · ${time(e.at)}</time></div></div>`).join('')}
async function action(name,extra){if(busy)return;busy=true;document.querySelectorAll('.actions button').forEach(b=>b.disabled=true);toast(name==='start'?'กำลังเริ่มบริการในเครื่อง…':name==='stop'?'กำลังตรวจสถานะก่อนหยุดบริการ…':'กำลังบันทึก…');try{const r=await bridge(name,extra);toast(r.error|| (name==='start'?'เริ่มบริการแล้ว · กำลังตรวจการเชื่อมต่อ':name==='stop'?'หยุดบริการแล้ว · เปิดครั้งถัดไปยังพักเปิดไม้ใหม่อยู่':r.paused?'พักเปิดไม้ใหม่แล้ว · ยังติดตาม SL/TP เดิม':'เปิดรับไม้ใหม่แล้ว · ต้องผ่าน AI และ Risk'))}catch(e){toast('ดำเนินการไม่สำเร็จ กรุณาตรวจบริการในเครื่อง')}finally{busy=false;document.querySelectorAll('.actions button').forEach(b=>b.disabled=false);await refresh()}}
$('start').onclick=()=>action('start');$('stop').onclick=()=>action('stop');$('pause').onclick=()=>action('pause',{paused:!state?.account?.operator_paused});
async function selectProvider(provider){if(providerBusy||!state?.connected||state.ai_selection?.provider===provider)return;providerBusy=true;$('providerLm').disabled=true;$('providerCodex').disabled=true;try{const r=await bridge('provider',{provider});toast(r.error||`เลือก ${provider==='codex'?'Codex':'LM Studio'} แล้ว · มีผลกับงานใหม่`)}catch(e){toast('บันทึกตัวเลือก AI ไม่สำเร็จ')}finally{providerBusy=false;await refresh()}}
$('providerLm').onclick=()=>selectProvider('lm_studio');$('providerCodex').onclick=()=>selectProvider('codex');
$('enableNotice').onclick=async()=>{try{const ns=state?.notifications;if(ns==='denied'){toast('ไปที่ System Settings → Notifications → XM Control แล้วเปิด Allow Notifications');return}const r=await bridge(ns==='enabled'?'testNotification':'notifications');toast(r.notifications==='enabled'?(ns==='enabled'?(r.sent?'ส่งการแจ้งเตือนทดสอบแล้ว':'macOS ไม่รับการแจ้งเตือน กรุณาตรวจสิทธิ์'):'เปิดการแจ้งเตือนแล้ว · กดอีกครั้งเพื่อทดสอบ'):r.notifications==='denied'?'เปิดสิทธิ์ที่ System Settings → Notifications → XM Control':'ยังไม่ได้รับสิทธิ์แจ้งเตือน');await refresh()}catch(e){toast('ตั้งค่าการแจ้งเตือนไม่สำเร็จ')}};
document.querySelectorAll('.tab').forEach(b=>b.onclick=()=>{selectedTab=b.dataset.tab;document.querySelectorAll('.tab').forEach(x=>x.classList.toggle('active',x===b));renderPositions()});
document.querySelectorAll('.nav').forEach(b=>b.onclick=()=>{document.body.dataset.view=b.dataset.view;text('pageTitle',b.textContent.slice(1).trim());document.querySelectorAll('.nav').forEach(x=>x.classList.toggle('active',x===b));document.querySelectorAll('[data-panel]').forEach(x=>x.classList.toggle('hidden',!x.dataset.panel.split(' ').includes(b.dataset.view)))});
function clock(){text('clock',new Date().toLocaleString('th-TH',{day:'numeric',month:'short',hour:'2-digit',minute:'2-digit',second:'2-digit'}))}clock();setInterval(clock,1000);refresh();setInterval(refresh,5000);
