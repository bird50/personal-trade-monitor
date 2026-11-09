"""Read-only Telegram commands: ``/status``, ``/signal``, ``/help`` from the configured chat.

Off by default and switched at runtime (dashboard button or ``POST /telegram/commands``); the
choice is stored in ``control_meta`` so it survives restarts. While off the worker makes no
network calls at all. While on it long-polls ``getUpdates``: one request is held open by Telegram
for up to ``command_poll_seconds`` (default 25) and returns as soon as a message arrives, so an idle
Mac makes about two requests per minute and a command is answered within a second or two.

* Only messages from ``TELEGRAM_CHAT_ID`` are answered; other chats are counted and ignored.
* Messages older than ``command_max_age_seconds`` or sent before commands were switched on are
  ignored, so a Mac waking from sleep never answers a backlog.
* The update offset is stored with the queued replies in one transaction; a reply is keyed by its
  ``update_id`` in ``telegram_outbox`` and is never queued twice. Replies use the existing outbox
  (retries, token redaction) and expire unsent after ``command_max_age_seconds``.
* Commands only read the dashboard snapshot. Nothing here changes alert state or places orders.
* Telegram allows one ``getUpdates`` consumer per bot: while commands are on, a second reader
  (``scripts/telegram_chat_id.py``) or a webhook makes Telegram answer 409 and polling backs off.
"""
import asyncio
import json
import logging
import time
from contextlib import suppress
from datetime import datetime

import httpx
from starlette.concurrency import run_in_threadpool

from .telegram_notifier import MAX_TEXT, TelegramNotifier, meta, set_meta

log = logging.getLogger("uvicorn.error")
COMMANDS = {"status": "status", "signal": "signal", "help": "help", "start": "help"}
REPLIES_PER_MINUTE = 20
MONTHS = ("ม.ค.", "ก.พ.", "มี.ค.", "เม.ย.", "พ.ค.", "มิ.ย.", "ก.ค.", "ส.ค.", "ก.ย.", "ต.ค.", "พ.ย.", "ธ.ค.")
ENGINE = {
    "watching": "เฝ้าดูตลาด", "alert": "มีสัญญาณใหม่", "warming_up": "กำลังอุ่นเครื่อง indicator",
    "bootstrapped": "เริ่มคำนวณจากประวัติราคา", "waiting_for_h1": "รอแท่ง H1 ล่าสุด", "waiting_for_feed": "รอข้อมูลจาก MT5",
    "feed_stale": "ราคาจาก MT5 ไม่อัปเดต", "source_mismatch": "Server หรือ symbol ไม่ตรง policy",
    "tick_policy_mismatch": "Tick size ไม่ตรง policy", "insufficient_history": "ประวัติราคาไม่พอ",
    "out_of_order_feed": "ได้ข้อมูลเก่ากว่าที่คำนวณแล้ว", "engine_error": "คำนวณไม่สำเร็จ",
    "feed_database_unavailable": "อ่านฐานข้อมูลราคาไม่ได้"}
TELEGRAM_STATE = {"disabled": "ปิดอยู่", "missing_credentials": "ยังไม่ได้ใส่ token/chat ID", "failing": "ส่งไม่สำเร็จ",
                  "ready": "พร้อมส่ง", "invalid_bot_token": "bot token ไม่ถูกรูปแบบ", "invalid_chat_id": "chat ID ไม่ถูกรูปแบบ"}
HELP = ("🤖 คำสั่ง XM Control (อ่านอย่างเดียว)\n"
        "/status – สถานะระบบ ราคา engine และตลาด\n"
        "/signal – สัญญาณเข้า trade ล่าสุด\n"
        "/help – รายการคำสั่ง\n"
        "bot นี้ไม่ส่งคำสั่งซื้อขายและเปลี่ยนการตั้งค่าไม่ได้")


def hhmm(seconds):
    return "—" if not seconds else datetime.fromtimestamp(seconds).strftime("%H:%M")


def stamp(seconds):
    if not seconds:
        return "—"
    d = datetime.fromtimestamp(seconds)
    return f"{d.day} {MONTHS[d.month - 1]} {d:%H:%M}"


def price(value, digits=2):
    return "—" if value is None else f"{float(value):,.{int(digits)}f}"


def number(value, digits=1):
    return "—" if value is None else f"{float(value):.{digits}f}"


def parse_command(text):
    """``/status@SomeBot extra`` -> ``status``; non-commands -> None; unknown commands -> ``unknown``."""
    if not isinstance(text, str) or not text.startswith("/"):
        return None
    name = text.split()[0][1:].split("@")[0].lower()
    return COMMANDS.get(name, "unknown") if name else None


def status_text(snap, now=None) -> str:
    now = time.time() if now is None else now
    if not snap or not snap.get("connected"):
        return "⚠️ อ่านสถานะบริการไม่ได้"
    f, e = snap.get("feed") or {}, snap.get("engine") or {}
    w, i, digits = e.get("warmup") or {}, e.get("indicators") or {}, f.get("digits") or 2
    label = ENGINE.get(e.get("status"), e.get("status") or "รอข้อมูล")
    if not snap.get("ready"):
        head = f"🟠 ยังไม่พร้อมแจ้งเตือน · {label}"
    elif snap.get("muted"):
        head = "🔕 ปิดเสียงแจ้งเตือนอยู่ · ยังบันทึกสัญญาณ"
    else:
        head = "🟢 พร้อมแจ้งเตือนจังหวะเข้า trade"
    if f.get("available"):
        feed = f"Bid {price(f.get('bid'), digits)} · Ask {price(f.get('ask'), digits)}"
    else:
        feed = "ราคาไม่อัปเดต" if f.get("age_seconds") is not None else "ยังไม่มีข้อมูล"
    if f.get("age_seconds") is not None:
        feed += f" · อายุ {round(f['age_seconds'])} วินาที · M5 {f.get('m5_bars')} / H1 {f.get('h1_bars')}"
    if w.get("ready"):
        engine = f"{label} · แท่งล่าสุดปิด {hhmm(e.get('last_bar_close_utc_s'))}"
    elif w.get("h1") is not None:
        engine = f"{label} · H1 {w['h1']}/{w.get('required_h1')} · M5 {w.get('m5')}/{w.get('required_m5')}"
    else:
        engine = label
    pos = e.get("position")
    if pos:
        market = (f"ไม้ {pos.get('side')} ตามกลยุทธ์ยังไม่จบ · SL {price(pos.get('sl'), digits)} · "
                  f"TP {price(pos.get('tp'), digits)} · ปิด {hhmm(pos.get('time_stop_utc_s'))}")
    elif i.get("rsi2") is None:
        market = "รอข้อมูล"
    else:
        bias = {"BUY": "แนวโน้มขาขึ้น · รอ RSI(2) ย่อ", "SELL": "แนวโน้มขาลง · รอ RSI(2) เด้ง"}.get(
            i.get("bias"), "ไม่มีแนวโน้มชัด · ไม่เข้า")
        market = f"{bias} · RSI(2) {number(i.get('rsi2'))} · ATR {number(i.get('atr14'), 2)}"
    tg = snap.get("telegram") or {}
    telegram = (f"{TELEGRAM_STATE.get(tg.get('state'), tg.get('state'))} · รอส่ง {tg.get('pending', 0)} · "
                f"ล้มเหลว {tg.get('failed', 0)}")
    lines = [f"📊 สถานะ XM Control · {stamp(now)}", head, "",
             f"MT5: {feed}", f"Signal engine: {engine}", f"ตลาดตามกลยุทธ์: {market}", f"Telegram: {telegram}"]
    a = snap.get("latest_alert")
    if a and now <= (a.get("valid_until_utc_s") or 0):
        lines += ["", f"🔔 มีสัญญาณ {a.get('side')} ที่ยังเข้าได้ถึง {hhmm(a.get('valid_until_utc_s'))} · ดู /signal"]
    return "\n".join(lines)[:MAX_TEXT]


def signal_text(snap, now=None) -> str:
    now = time.time() if now is None else now
    if not snap or not snap.get("connected"):
        return "⚠️ อ่านสถานะบริการไม่ได้"
    a = snap.get("latest_alert")
    if not a:
        return "ยังไม่มีสัญญาณที่ผ่านเงื่อนไข\nระบบตรวจทุกแท่ง M5 ที่ปิดแล้วร่วมกับแนวโน้ม H1"
    d, side = a.get("digits") or 2, a.get("side")
    open_ = now <= (a.get("valid_until_utc_s") or 0)
    icon = "📈" if side == "BUY" else "📉"
    head = (f"{icon} สัญญาณ {side} · เข้าได้ถึง {hhmm(a.get('valid_until_utc_s'))}" if open_ else
            f"⌛ สัญญาณ {side} · หมดเวลาเข้าเมื่อ {hhmm(a.get('valid_until_utc_s'))} · ห้ามเข้าตามข้อความนี้")
    limit = "Ask ไม่เกิน" if side == "BUY" else "Bid ไม่ต่ำกว่า"
    lines = [head, f"แท่ง M5 ปิด {stamp(a.get('bar_close_utc_s'))} · {a.get('source_symbol', '')}".rstrip(" ·"),
             f"ราคาอ้างอิง {price(a.get('entry'), d)} · {limit} {price(a.get('entry_limit'), d)}",
             f"SL {price(a.get('sl'), d)} · TP {price(a.get('tp'), d)} · RR สุทธิ {number(a.get('net_reward_risk'), 2)}",
             f"ถ้ายังไม่ถึง SL/TP ให้ปิดเวลา {hhmm(a.get('time_stop_utc_s'))}",
             "XM Control แจ้งเตือนเท่านั้น ไม่ได้ส่งคำสั่งซื้อขาย"]
    return "\n".join(lines)[:MAX_TEXT]


class TelegramCommands:
    def __init__(self, notifier: TelegramNotifier, snapshot, transport=None):
        self.n, self.db, self.config = notifier, notifier.db, notifier.config
        self.snapshot = snapshot  # callable -> dashboard snapshot dict (runs in a worker thread)
        self.transport = transport if transport is not None else notifier.transport
        self.toggled, self._loop, self.polling, self.failures = asyncio.Event(), None, False, 0

    # ------------------------------------------------------------------ switch
    def enabled(self) -> bool:
        with self.db.connect() as conn:
            return meta(conn, "telegram_commands_enabled") == "1"

    def set_enabled(self, on: bool, now=None):
        now = time.time() if now is None else now
        if on and not self.n.active:
            return {"changed": False, "enabled": False, "reason": self.n.status()["state"]}
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            before = meta(conn, "telegram_commands_enabled") == "1"
            set_meta(conn, "telegram_commands_enabled", "1" if on else "0")
            if on and not before:
                set_meta(conn, "telegram_commands_since", now)
                set_meta(conn, "telegram_commands_failing", "0")
        if self._loop is not None and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self.toggled.set)
        return {"changed": before != on, "enabled": on}

    def status(self):
        with self.db.connect() as conn:
            on = meta(conn, "telegram_commands_enabled") == "1"
            failing = meta(conn, "telegram_commands_failing") == "1"
            error = meta(conn, "telegram_commands_last_error")
            last_command = meta(conn, "telegram_commands_last_at")
            last_poll = meta(conn, "telegram_commands_last_poll_at")
            rejected = int(meta(conn, "telegram_commands_rejected", 0))
        state = ("off" if not on else "unavailable" if not self.n.active else "failing" if failing else "listening")
        return {"enabled": on, "state": state, "polling": self.polling,
                "poll_seconds": self.config.command_poll_seconds,
                "last_command_at": float(last_command) if last_command else None,
                "last_poll_at": float(last_poll) if last_poll else None,
                "last_error": json.loads(error) if error and failing else None, "rejected_chats": rejected}

    # ------------------------------------------------------------------ updates
    def _authorized(self, chat) -> bool:
        if not isinstance(chat, dict) or not self.n.chat_id:
            return False
        if self.n.chat_id.startswith("@"):
            return ("@" + str(chat.get("username") or "")).lower() == self.n.chat_id.lower()
        return str(chat.get("id")) == self.n.chat_id

    def reply_text(self, command, snap, now) -> str:
        if command == "status":
            return status_text(snap, now)
        if command == "signal":
            return signal_text(snap, now)
        if command == "unknown":
            return "ไม่รู้จักคำสั่งนี้\n\n" + HELP
        return HELP

    def handle_updates(self, updates, now=None) -> int:
        """Queue replies for accepted commands and advance the stored offset; returns replies queued."""
        now = time.time() if now is None else now
        with self.db.connect() as conn:
            since = float(meta(conn, "telegram_commands_since", 0))
            offset = int(meta(conn, "telegram_commands_offset", 0))
        accepted, rejected = [], 0
        for update in updates if isinstance(updates, list) else []:
            uid = update.get("update_id") if isinstance(update, dict) else None
            if not isinstance(uid, int) or isinstance(uid, bool):
                continue
            offset = max(offset, uid + 1)
            message = update.get("message") or update.get("channel_post")
            if not isinstance(message, dict):
                continue
            command = parse_command(message.get("text"))
            if command is None:
                continue
            if not self._authorized(message.get("chat")):
                rejected += 1
                continue
            sent = message.get("date")
            if not isinstance(sent, (int, float)) or sent < since - 1 or now - sent > self.config.command_max_age_seconds:
                continue  # sent while commands were off, or a backlog after sleep
            accepted.append((uid, command))
        snap = None
        if any(command in ("status", "signal") for _, command in accepted):
            try:
                snap = self.snapshot()
            except Exception:
                log.warning("telegram_commands_snapshot_error")
        queued = 0
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            recent = conn.execute("SELECT COUNT(*) FROM telegram_outbox WHERE kind='command_reply' AND created_at>?",
                                  (now - 60,)).fetchone()[0]
            for uid, command in accepted:
                if recent + queued >= REPLIES_PER_MINUTE:
                    break
                cur = conn.execute("""INSERT OR IGNORE INTO telegram_outbox(event_key,kind,text,created_at,expires_at,
                    status,attempts,next_attempt_at) VALUES(?,?,?,?,?,'pending',0,?)""",
                                   (f"cmd:{uid}", "command_reply", self.reply_text(command, snap, now), now,
                                    now + self.config.command_max_age_seconds, now))
                queued += cur.rowcount
            set_meta(conn, "telegram_commands_offset", offset)
            if accepted:
                set_meta(conn, "telegram_commands_last_at", now)
            if rejected:
                set_meta(conn, "telegram_commands_rejected", int(meta(conn, "telegram_commands_rejected", 0)) + rejected)
        if queued:
            self.n.poke()
        return queued

    def _record(self, now, error=None):
        with self.db.connect() as conn:
            set_meta(conn, "telegram_commands_last_poll_at", now)
            set_meta(conn, "telegram_commands_failing", "1" if error else "0")
            if error:
                set_meta(conn, "telegram_commands_last_error", json.dumps({"at": now, "error": error}))

    def _offset(self):
        with self.db.connect() as conn:
            return int(meta(conn, "telegram_commands_offset", 0))

    async def poll_once(self, client: httpx.AsyncClient, now=None) -> float:
        """One getUpdates call; returns seconds to wait before the next call."""
        started = time.monotonic()
        params = {"timeout": int(self.config.command_poll_seconds),
                  "allowed_updates": json.dumps(["message", "channel_post"])}
        offset = await run_in_threadpool(self._offset)
        if offset:
            params["offset"] = offset
        try:
            response = await client.get(f"{self.n.api_base}/bot{self.n._token}/getUpdates", params=params)
        except httpx.HTTPError as exc:
            return await self._failed(self.n.clean(type(exc).__name__), None)
        try:
            data = response.json()
        except ValueError:
            data = {}
        if response.status_code != 200 or data.get("ok") is not True:
            error = self.n.clean(f"HTTP {response.status_code}: {data.get('description') or 'no description'}")
            wait = None
            if response.status_code == 409:
                wait = 60.0  # webhook set, or another getUpdates reader (e.g. telegram_chat_id.py)
            elif response.status_code == 429:
                after = (data.get("parameters") or {}).get("retry_after")
                wait = float(after) if isinstance(after, (int, float)) and 0 < after <= 3600 else None
            elif response.status_code in (401, 403, 404):
                wait = 300.0
            return await self._failed(error, wait)
        self.failures = 0
        now = time.time() if now is None else now
        await run_in_threadpool(self.handle_updates, data.get("result"), now)
        await run_in_threadpool(self._record, now)
        # Telegram holds the request open; if it ever answers instantly, still poll at most once a second.
        return max(0.0, 1.0 - (time.monotonic() - started))

    async def _failed(self, error, wait):
        self.failures += 1
        await run_in_threadpool(self._record, time.time(), error)
        log.warning("telegram_commands_poll_failed %s", error)
        return wait if wait is not None else min(300.0, 5.0 * 2 ** (self.failures - 1))

    async def _pause(self, seconds):
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self.toggled.wait(), seconds)

    async def run(self):
        self._loop = asyncio.get_running_loop()
        timeout = httpx.Timeout(self.config.timeout_seconds, read=self.config.command_poll_seconds + 10)
        async with httpx.AsyncClient(timeout=timeout, trust_env=False, follow_redirects=False,
                                     transport=self.transport) as client:
            while True:
                self.toggled.clear()
                try:
                    on = self.n.active and await run_in_threadpool(self.enabled)
                except Exception:
                    on = False
                if not on:
                    self.polling = False
                    await self.toggled.wait()  # no network traffic while switched off
                    continue
                self.polling = True
                poll = asyncio.create_task(self.poll_once(client))
                switch = asyncio.create_task(self.toggled.wait())
                done, _ = await asyncio.wait({poll, switch}, return_when=asyncio.FIRST_COMPLETED)
                if poll not in done:  # switched while a long poll was open: drop it and re-check
                    poll.cancel()
                    with suppress(asyncio.CancelledError, Exception):
                        await poll
                    continue
                switch.cancel()
                try:
                    wait = poll.result()
                except Exception as exc:
                    log.warning("telegram_commands_error %s", self.n.clean(type(exc).__name__))
                    wait = 30.0
                if wait:
                    self.toggled.clear()
                    await self._pause(wait)
