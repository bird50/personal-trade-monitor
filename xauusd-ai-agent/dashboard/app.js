'use strict';
const $ = id => document.getElementById(id);
let state = null, busy = false, refreshing = false, toastTimer;
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
const time = seconds => !seconds ? '—' : new Date(seconds * 1000).toLocaleTimeString('th-TH', {hour: '2-digit', minute: '2-digit'});
const stamp = seconds => !seconds ? '—' : new Date(seconds * 1000).toLocaleDateString('th-TH', {day: 'numeric', month: 'short'}) + ' · ' + time(seconds);
const price = (value, digits = 2) => value == null ? '—' : Number(value).toLocaleString('en-US', {minimumFractionDigits: digits, maximumFractionDigits: digits});
const fixed = (value, digits = 1) => value == null ? '—' : Number(value).toFixed(digits);
function text(id, value) { $(id).textContent = value }
function dot(id, ok, warn) { $(id).className = 'dot ' + (ok ? 'green' : warn ? 'red' : 'amber') }
function toast(message) { text('toast', message); $('toast').classList.add('show'); clearTimeout(toastTimer); toastTimer = setTimeout(() => $('toast').classList.remove('show'), 6500) }
async function bridge(action, extra = {}) { if (!window.webkit?.messageHandlers?.control) throw Error('เปิดหน้านี้ผ่านแอป XM Control'); return window.webkit.messageHandlers.control.postMessage({action, ...extra}) }
async function refresh() { if (refreshing) return; refreshing = true; try { state = await bridge('state'); render() } catch (e) { state = {connected: false}; render() } finally { refreshing = false } }

const ENGINE = {
  watching: 'เฝ้าดูตลาด', alert: 'มีสัญญาณใหม่', warming_up: 'กำลังอุ่นเครื่อง indicator', bootstrapped: 'เริ่มคำนวณจากประวัติราคา',
  waiting_for_h1: 'รอแท่ง H1 ล่าสุด', waiting_for_feed: 'รอข้อมูลจาก MT5', feed_stale: 'ราคาจาก MT5 ไม่อัปเดต',
  source_mismatch: 'Server หรือ symbol ไม่ตรง policy', tick_policy_mismatch: 'Tick size ไม่ตรง policy',
  insufficient_history: 'ประวัติราคาไม่พอ', out_of_order_feed: 'ได้ข้อมูลเก่ากว่าที่คำนวณแล้ว',
  engine_error: 'คำนวณไม่สำเร็จ', feed_database_unavailable: 'อ่านฐานข้อมูลราคาไม่ได้'};
const ALERT = {alert: 'แจ้งแล้ว', missed: 'เลยเวลาแจ้ง', warmup: 'ระหว่างอุ่นเครื่อง', rejected: 'ไม่ผ่าน Risk'};
const REASON = {net_reward_risk_too_low: 'RR สุทธิต่ำ', spread_atr_too_high: 'Spread สูงเทียบ ATR', stop_atr_out_of_range: 'ระยะ SL นอกช่วง',
  price_not_on_tick: 'ราคาไม่ตรง tick', levels_inconsistent: 'ระดับราคาไม่สอดคล้อง'};

function render() {
  const connected = state?.connected === true;
  $('mute').disabled = !connected || busy;
  const ns = state?.notifications;
  text('noticeStatus', ns === 'enabled' ? 'เปิดใช้งานแล้ว' : ns === 'denied' ? 'ยังไม่ได้รับอนุญาต' : 'ยังไม่ได้เปิดใช้งาน');
  text('enableNotice', ns === 'enabled' ? 'ทดสอบการแจ้งเตือน ↗' : ns === 'denied' ? 'ดูวิธีเปิดการแจ้งเตือน ↗' : 'เปิดการแจ้งเตือน ↗');
  dot('noticeDot', ns === 'enabled');
  const tg = state?.telegram;
  const TG = {disabled: 'ปิดอยู่', missing_credentials: 'ยังไม่ได้ใส่ token/chat ID', invalid_bot_token: 'bot token ไม่ถูกรูปแบบ',
    invalid_chat_id: 'chat ID ไม่ถูกรูปแบบ', failing: 'ส่งไม่สำเร็จ', ready: 'พร้อมส่ง'};
  text('telegramStatus', !connected || !tg ? 'ไม่มีข้อมูลปัจจุบัน' : (TG[tg.state] || tg.state) + (tg.worker === false ? ' · ตัวส่งหยุด' : ''));
  text('telegramDetail', !connected || !tg ? 'รอเชื่อมต่อบริการ' : !tg.enabled ? 'เปิดที่ config/telegram.json' : tg.last_error && tg.state === 'failing' ? tg.last_error.error
    : `ส่งล่าสุด ${stamp(tg.last_sent_at)} · รอส่ง ${tg.pending} · ล้มเหลว ${tg.failed}`);
  $('telegramTest').classList.toggle('hidden', !connected || !tg?.active);
  const cmd = tg?.commands;
  $('telegramCommands').classList.toggle('hidden', !connected || !cmd || (!tg?.active && !cmd.enabled));
  text('telegramCommands', cmd?.enabled ? 'ปิดรับคำสั่ง /status' : 'เปิดรับคำสั่ง /status');
  if (connected && tg?.enabled && cmd?.enabled) {
    const CMD = {listening: 'รับคำสั่งอยู่', failing: 'รับคำสั่งไม่ได้', unavailable: 'รับคำสั่งไม่ได้ (Telegram ไม่พร้อม)'};
    $('telegramDetail').textContent += ' · ' + (CMD[cmd.state] || cmd.state) + (cmd.state === 'failing' && cmd.last_error ? ` (${cmd.last_error.error})` : '');
  }
  dot('telegramDot', connected && tg?.state === 'ready' && tg?.worker !== false, connected && tg?.state === 'failing');
  if (!connected) {
    dot('systemDot', false); text('systemTitle', 'บริการในเครื่องยังไม่พร้อม'); text('lastUpdate', 'ไม่พบระบบที่ 127.0.0.1:8002');
    $('errorBanner').classList.remove('hidden');
    text('errorBanner', 'กด “เริ่มบริการ” เพื่อเปิดตัวรับราคาและระบบแจ้งเตือน หรือดู log ในโฟลเดอร์ .runtime หากเริ่มไม่สำเร็จ');
    for (const id of ['feed', 'engine', 'setup']) { text(id + 'Status', 'ไม่มีข้อมูลปัจจุบัน'); text(id + 'Detail', 'รอเชื่อมต่อบริการอีกครั้ง'); dot(id + 'Dot', false) }
    return;
  }
  $('errorBanner').classList.add('hidden');
  const f = state.feed, e = state.engine || {}, w = e.warmup || {}, digits = f.digits ?? 2;
  text('lastUpdate', 'อัปเดต ' + time(state.checked_at) + ' · รีเฟรชทุก 5 วินาที');
  dot('systemDot', state.ready);
  text('systemTitle', !state.ready ? 'ยังไม่พร้อมแจ้งเตือน · ' + (ENGINE[e.status] || e.status || 'รอข้อมูล') : state.muted ? 'ปิดเสียงแจ้งเตือนอยู่ · ยังบันทึกสัญญาณ' : 'พร้อมแจ้งเตือนจังหวะเข้า trade');
  text('mute', state.muted ? '▶ เปิดเสียงแจ้งเตือน' : 'Ⅱ ปิดเสียงแจ้งเตือน');
  text('feedStatus', f.available ? `Bid ${price(f.bid, digits)} · Ask ${price(f.ask, digits)}` : f.age_seconds != null ? 'ราคาไม่อัปเดต' : 'ยังไม่มีข้อมูล');
  text('feedDetail', f.age_seconds != null ? `อายุ ${Math.round(f.age_seconds)} วินาที · M5 ${f.m5_bars} / H1 ${f.h1_bars} แท่ง` : state.receiver?.available ? 'ตัวรับราคาพร้อม รอ EA ส่งข้อมูล' : 'ตัวรับราคาที่ 8001 ไม่ตอบ');
  dot('feedDot', f.available);
  text('engineStatus', ENGINE[e.status] || e.status || '—');
  text('engineDetail', w.ready ? `ข้อมูลพร้อม · แท่งล่าสุดปิด ${time(e.last_bar_close_utc_s)}` : w.h1 != null ? `อุ่นเครื่อง H1 ${w.h1}/${w.required_h1} · M5 ${w.m5}/${w.required_m5} แท่ง` : 'รอแท่งราคาแรก');
  dot('engineDot', w.ready && ['watching', 'alert'].includes(e.status), ['engine_error', 'source_mismatch', 'tick_policy_mismatch'].includes(e.status));
  renderSetup(e, digits); renderLatest(digits); renderRules(); renderAlerts(); renderEvents();
}

function renderSetup(e, digits) {
  const i = e.indicators || {}, pos = e.position;
  if (pos) {
    text('setupStatus', `ไม้ ${pos.side} ตามกลยุทธ์ยังไม่จบ`);
    text('setupDetail', `SL ${price(pos.sl, digits)} · TP ${price(pos.tp, digits)} · ปิดเวลา ${time(pos.time_stop_utc_s)}`);
    dot('setupDot', true); return;
  }
  const bias = i.bias === 'BUY' ? 'แนวโน้มขาขึ้น · รอ RSI(2) ย่อ' : i.bias === 'SELL' ? 'แนวโน้มขาลง · รอ RSI(2) เด้ง' : 'ไม่มีแนวโน้มชัด · ไม่เข้า';
  text('setupStatus', i.rsi2 == null ? 'รอข้อมูล' : bias);
  text('setupDetail', i.rsi2 == null ? '—' : `RSI(2) ${fixed(i.rsi2)} · ATR ${fixed(i.atr14, 2)} · H1 EMA50 ${price(i.h1_ema50, digits)}`);
  dot('setupDot', i.bias != null);
}

function renderLatest(digits) {
  const a = state.latest_alert, now = Date.now() / 1000;
  const side = $('latestSide');
  side.className = 'decision' + (a ? ' ' + a.side.toLowerCase() : '');
  if (!a) { text('latestState', 'WAITING'); $('latestState').className = 'tag muted'; return }
  const open = now <= a.valid_until_utc_s;
  text('latestSide', a.side);
  text('latestState', open ? 'เข้าได้' : 'หมดเวลาเข้า'); $('latestState').className = 'tag ' + (open ? 'live' : 'muted');
  text('latestMeta', `แท่ง M5 ปิด ${stamp(a.bar_close_utc_s)} · ${a.source_symbol}`);
  text('latestTitle', open ? `เข้า ${a.side} ได้ถึง ${time(a.valid_until_utc_s)}` : `สัญญาณ ${a.side} · หมดเวลาเข้าเมื่อ ${time(a.valid_until_utc_s)}`);
  text('latestDetail', `${a.side === 'BUY' ? 'Ask ไม่เกิน' : 'Bid ไม่ต่ำกว่า'} ${price(a.entry_limit, a.digits)} · ถ้ายังไม่ถึง SL/TP ให้ปิดเวลา ${time(a.time_stop_utc_s)}`);
  text('latestEntry', price(a.entry, a.digits)); text('latestSl', price(a.sl, a.digits)); text('latestTp', price(a.tp, a.digits)); text('latestRr', fixed(a.net_reward_risk, 2));
  text('latestHint', `RSI(2) แท่งก่อน ${fixed(a.indicators?.rsi2_previous)} → แท่งสัญญาณ ${fixed(a.indicators?.rsi2)} · ATR ${fixed(a.atr, 2)} · ระยะ SL ${fixed(a.stop_atr, 2)} ATR · spread ที่ใช้คิด ${a.spread_price_used}\nราคาทั้งหมดเป็น bid ของ XM Demo · ระบบไม่ได้ส่งคำสั่งและไม่รู้ราคาที่คุณได้จริง`);
}

function renderRules() {
  const s = state.strategy, p = state.policy;
  text('strategyName', `${s.name} · ${s.version}`);
  text('ruleEntry', `RSI(2) ≤ ${s.rsi_low} แล้วปิดเหนือ high ก่อนหน้า`);
  text('ruleSl', `${s.sl_atr} × ATR14`);
  text('ruleTp', `RR สุทธิ ${s.rr} หลังหักต้นทุน`);
  text('ruleTime', `${s.time_stop_bars} แท่ง M5 (${s.time_stop_bars * 5 / 60} ชม.)`);
  text('ruleHours', `${String(s.start_hour).padStart(2, '0')}:00–${String(s.end_hour).padStart(2, '0')}:00 · ไม่เข้าศุกร์หลัง 20:00`);
  text('ruleRisk', `RR ≥ ${p.min_net_reward_risk} · spread/ATR ≤ ${p.max_spread_atr}`);
  text('ruleFooter', `สัญญาณใช้ได้ ${p.max_signal_age_seconds / 60} นาทีหลังแท่งปิด · ราคาเข้าห่างได้ไม่เกิน ${p.max_entry_drift_atr} ATR`);
}

function renderAlerts() {
  const items = state.alerts || [], c = state.alert_counts || {};
  text('alertBadge', String(items.length));
  text('alertTotals', `แจ้งแล้ว ${c.alert || 0} · ไม่ผ่าน ${c.rejected || 0}`);
  $('alertsEmpty').classList.toggle('hidden', items.length > 0);
  $('alerts').innerHTML = items.map(a => {
    const why = a.reason_codes?.length ? ' · ' + a.reason_codes.map(r => REASON[r] || r).join(', ') : '';
    return `<tr><td>${esc(stamp(a.bar_close_utc_s))}<small>${esc((ALERT[a.status] || a.status) + why)}</small></td>`
      + `<td class="side-${a.side === 'BUY' ? 'buy' : 'sell'}">${esc(a.side)}</td><td>${esc(price(a.entry, a.digits))}</td>`
      + `<td>${esc(price(a.sl, a.digits))}<small>TP ${esc(price(a.tp, a.digits))}</small></td><td class="right">${esc(fixed(a.net_reward_risk, 2))}</td></tr>`;
  }).join('');
}

function renderEvents() {
  const items = state.recent_events || [];
  $('eventsEmpty').classList.toggle('hidden', items.length > 0);
  $('events').innerHTML = items.map(e => `<div class="event ${esc(e.level)}"><span class="event-mark">${e.level === 'warning' ? '!' : e.kind === 'trade_alert' ? '↗' : '●'}</span><div class="event-body"><strong>${esc(e.title)}</strong><p>${esc(e.message)}</p><time>${esc(stamp(e.at))}</time></div></div>`).join('');
}

async function action(name, extra) {
  if (busy) return; busy = true;
  document.querySelectorAll('.actions button').forEach(b => b.disabled = true);
  toast(name === 'start' ? 'กำลังเริ่มบริการในเครื่อง…' : name === 'stop' ? 'กำลังหยุดบริการ…' : 'กำลังบันทึก…');
  try {
    const r = await bridge(name, extra);
    toast(r.error || (name === 'start' ? 'เริ่มบริการแล้ว · กำลังตรวจการเชื่อมต่อ' : name === 'stop' ? 'หยุดบริการแล้ว · จะไม่มีการแจ้งเตือนจนกว่าจะเริ่มใหม่' : r.paused ? 'ปิดเสียงแจ้งเตือนแล้ว · ยังบันทึกสัญญาณต่อ' : 'เปิดแจ้งเตือนแล้ว'));
  } catch (e) { toast('ดำเนินการไม่สำเร็จ กรุณาตรวจบริการในเครื่อง') }
  finally { busy = false; document.querySelectorAll('.actions button').forEach(b => b.disabled = false); await refresh() }
}
$('start').onclick = () => action('start');
$('stop').onclick = () => action('stop');
$('mute').onclick = () => action('pause', {paused: !state?.muted});
$('telegramTest').onclick = async () => {
  try {
    const r = await bridge('telegramTest');
    toast(r.queued ? 'ส่งข้อความทดสอบเข้าคิวแล้ว · ตรวจในแชท Telegram' : r.reason === 'test_already_pending' ? 'ยังมีข้อความทดสอบรอส่งอยู่' : r.error || 'Telegram ยังไม่พร้อม กรุณาตรวจการตั้งค่า');
    await refresh();
  } catch (e) { toast('ส่งข้อความทดสอบไม่สำเร็จ') }
};
$('telegramCommands').onclick = async () => {
  const on = !state?.telegram?.commands?.enabled;
  try {
    const r = await bridge('telegramCommands', {enabled: on});
    toast(r.error || (r.enabled ? 'เปิดรับคำสั่งแล้ว · พิมพ์ /status ในแชท Telegram' : on ? 'Telegram ยังไม่พร้อม กรุณาตรวจการตั้งค่า' : 'ปิดรับคำสั่งแล้ว · Mac หยุดดึงข้อความจาก Telegram'));
    await refresh();
  } catch (e) { toast('บันทึกการตั้งค่าคำสั่งไม่สำเร็จ') }
};
$('enableNotice').onclick = async () => {
  try {
    const ns = state?.notifications;
    if (ns === 'denied') { toast('ไปที่ System Settings → Notifications → XM Control แล้วเปิด Allow Notifications'); return }
    const r = await bridge(ns === 'enabled' ? 'testNotification' : 'notifications');
    toast(r.notifications === 'enabled' ? (ns === 'enabled' ? (r.sent ? 'ส่งการแจ้งเตือนทดสอบแล้ว' : 'macOS ไม่รับการแจ้งเตือน กรุณาตรวจสิทธิ์') : 'เปิดการแจ้งเตือนแล้ว · กดอีกครั้งเพื่อทดสอบ') : r.notifications === 'denied' ? 'เปิดสิทธิ์ที่ System Settings → Notifications → XM Control' : 'ยังไม่ได้รับสิทธิ์แจ้งเตือน');
    await refresh();
  } catch (e) { toast('ตั้งค่าการแจ้งเตือนไม่สำเร็จ') }
};
document.querySelectorAll('.nav').forEach(b => b.onclick = () => {
  document.body.dataset.view = b.dataset.view; text('pageTitle', b.textContent.slice(1).trim());
  document.querySelectorAll('.nav').forEach(x => x.classList.toggle('active', x === b));
  document.querySelectorAll('[data-panel]').forEach(x => x.classList.toggle('hidden', !x.dataset.panel.split(' ').includes(b.dataset.view)));
});
function clock() { text('clock', new Date().toLocaleString('th-TH', {day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit', second: '2-digit'})) }
clock(); setInterval(clock, 1000); refresh(); setInterval(refresh, 5000);
