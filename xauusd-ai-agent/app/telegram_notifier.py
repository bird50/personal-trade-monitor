"""Mirror alert-service events to one Telegram chat (send-only bot).

Flow: ``control_events`` (the same rows XM Control turns into macOS notifications) are copied
into ``telegram_outbox`` by a persisted cursor, then a worker sends pending rows with the Bot API
``sendMessage``. The outbox is keyed by ``event_key`` so restarts, duplicate snapshots and repeated
scans never queue a message twice. Delivery is at-least-once: a crash between Telegram accepting a
message and the row being marked ``sent`` can repeat that one message.

* Configuration: ``config/telegram.json`` (no secrets). Credentials come only from the environment
  (``TELEGRAM_BOT_TOKEN``, ``TELEGRAM_CHAT_ID``, loaded from ``.env``).
* When disabled or unconfigured the cursor follows the newest event, so enabling later never
  floods the chat with old events.
* A trade alert that is past its ``valid_until_utc_s`` when it would be sent is marked ``expired``
  (or sent with an expiry banner when ``send_expired_alerts`` is true).
* The token is part of the request URL. Errors are stored and logged as status codes, Telegram's
  ``description`` field or the exception class name only, with any token text removed.
* Read-only chat commands (``/status`` and friends) live in ``telegram_commands`` and are off
  unless switched on; their replies go through this outbox as ``command_reply`` rows.
"""
import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Annotated, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from .config import ROOT

TELEGRAM_CONFIG = ROOT / "config/telegram.json"
API_BASE = "https://api.telegram.org"
TOKEN_RE = re.compile(r"^\d{5,20}:[A-Za-z0-9_-]{30,100}$")
CHAT_RE = re.compile(r"^(-?\d{1,20}|@[A-Za-z][A-Za-z0-9_]{4,31})$")
EVENT_KINDS = ("trade_alert", "health_down", "health_recovered", "engine_reset", "alerts_muted", "alerts_unmuted")
MAX_TEXT = 4000
log = logging.getLogger("uvicorn.error")


class TelegramConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: Literal["telegram-v1"]
    enabled: bool
    events: Annotated[tuple[Literal[EVENT_KINDS], ...], Field(min_length=1, max_length=len(EVENT_KINDS))]
    send_expired_alerts: bool = False
    timeout_seconds: Annotated[float, Field(gt=0, le=30)] = 5
    max_attempts: Annotated[int, Field(ge=1, le=50, strict=True)] = 8
    poll_seconds: Annotated[float, Field(ge=0.05, le=60)] = 3
    command_poll_seconds: Annotated[int, Field(ge=1, le=50, strict=True)] = 25
    command_max_age_seconds: Annotated[int, Field(ge=10, le=600, strict=True)] = 120

    @classmethod
    def load(cls, path: Path = TELEGRAM_CONFIG):
        path = Path(path)
        if not path.exists():
            return cls(version="telegram-v1", enabled=False, events=("trade_alert",))
        return cls.model_validate_json(path.read_text())


def credentials_from_env():
    """(token, chat_id, problem). ``problem`` is a short code when the pair is missing or malformed."""
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat:
        return None, None, "missing_credentials"
    if not TOKEN_RE.fullmatch(token):
        return None, None, "invalid_bot_token"
    if not CHAT_RE.fullmatch(chat):
        return None, None, "invalid_chat_id"
    return token, chat, None


def ensure_tables(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS telegram_outbox (
        id INTEGER PRIMARY KEY, event_key TEXT UNIQUE NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL,
        created_at REAL NOT NULL, expires_at REAL,
        status TEXT NOT NULL CHECK(status IN ('pending','sent','failed','expired')),
        attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at REAL NOT NULL,
        sent_at REAL, telegram_message_id INTEGER, last_error TEXT)""")
    conn.execute("CREATE INDEX IF NOT EXISTS telegram_outbox_pending ON telegram_outbox(status, next_attempt_at)")
    conn.execute("CREATE TABLE IF NOT EXISTS control_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)")


def meta(conn, key, default=None):
    row = conn.execute("SELECT value FROM control_meta WHERE key=?", (key,)).fetchone()
    return default if row is None else row[0]


def set_meta(conn, key, value):
    conn.execute("INSERT INTO control_meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))


def message_text(row, payload=None) -> str:
    if row["kind"] == "trade_alert":
        icon = "📈" if (payload or {}).get("side") == "BUY" else "📉" if (payload or {}).get("side") == "SELL" else "🔔"
    else:
        icon = {"warning": "⚠️", "success": "✅"}.get(row["level"], "ℹ️")
    text = f"{icon} {row['title']}\n{row['message']}"
    if row["kind"] == "trade_alert":
        text += "\nXM Control แจ้งเตือนเท่านั้น ไม่ได้ส่งคำสั่งซื้อขาย"
    return text[:MAX_TEXT]


class TelegramNotifier:
    def __init__(self, db, config: TelegramConfig, token=None, chat_id=None, problem=None, transport=None,
                 api_base=API_BASE):
        self.db, self.config = db, config
        self._token, self.chat_id = token, chat_id
        self.problem = problem if config.enabled else None
        self.transport, self.api_base = transport, api_base
        self.wake, self._loop = asyncio.Event(), None
        with db.connect() as conn:
            ensure_tables(conn)

    @classmethod
    def from_env(cls, db, config: TelegramConfig, **kwargs):
        token, chat, problem = credentials_from_env() if config.enabled else (None, None, None)
        return cls(db, config, token, chat, problem, **kwargs)

    @property
    def active(self) -> bool:
        return self.config.enabled and self._token is not None and self.chat_id is not None

    def clean(self, text: str) -> str:
        text = str(text)
        if self._token:
            text = text.replace(self._token, "<token>")
        return re.sub(r"bot\d{5,20}:[A-Za-z0-9_-]+", "bot<token>", text)[:300]

    # ------------------------------------------------------------------ queue
    def enqueue_events(self, now=None) -> int:
        """Copy new control events into the outbox; returns how many were queued."""
        now = time.time() if now is None else now
        queued = 0
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            newest = conn.execute("SELECT COALESCE(MAX(id),0) FROM control_events").fetchone()[0]
            cursor = meta(conn, "telegram_event_cursor")
            if not self.active or cursor is None or int(cursor) > newest:
                set_meta(conn, "telegram_event_cursor", newest)
                return 0
            rows = conn.execute("SELECT * FROM control_events WHERE id>? ORDER BY id LIMIT 200", (int(cursor),)).fetchall()
            for row in rows:
                if row["kind"] in self.config.events:
                    payload, expires = None, None
                    if row["kind"] == "trade_alert" and row["event_key"].startswith("alert:"):
                        found = conn.execute("SELECT payload_json FROM trade_alerts WHERE alert_key=?",
                                             (row["event_key"][6:],)).fetchone()
                        if found:
                            payload = json.loads(found[0])
                            expires = payload.get("valid_until_utc_s")
                    cur = conn.execute("""INSERT OR IGNORE INTO telegram_outbox(event_key,kind,text,created_at,expires_at,
                        status,attempts,next_attempt_at) VALUES(?,?,?,?,?,'pending',0,?)""",
                                       ("event:" + row["event_key"], row["kind"], message_text(row, payload), now, expires, now))
                    queued += cur.rowcount
            if rows:
                set_meta(conn, "telegram_event_cursor", rows[-1]["id"])
        if queued:
            self.poke()
        return queued

    def queue_test(self, now=None):
        now = time.time() if now is None else now
        if not self.active:
            return {"queued": False, "reason": self.status()["state"]}
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT 1 FROM telegram_outbox WHERE kind='telegram_test' AND status='pending'").fetchone():
                return {"queued": False, "reason": "test_already_pending"}
            cur = conn.execute("""INSERT INTO telegram_outbox(event_key,kind,text,created_at,expires_at,status,attempts,
                next_attempt_at) VALUES(?,?,?,?,NULL,'pending',0,?)""",
                               (f"test:{time.time_ns()}", "telegram_test",
                                "🧪 ทดสอบการแจ้งเตือน Telegram จาก XM Control\nถ้าเห็นข้อความนี้ แจ้งเตือนสัญญาณจะส่งมาที่แชทนี้", now, now))
        self.poke()
        return {"queued": True, "id": cur.lastrowid}

    def poke(self):
        """Wake the worker early; safe from threadpool callers."""
        if self._loop is not None and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self.wake.set)

    # ------------------------------------------------------------------ delivery
    def _due(self, now):
        with self.db.connect() as conn:
            return [dict(r) for r in conn.execute("""SELECT * FROM telegram_outbox WHERE status='pending' AND next_attempt_at<=?
                ORDER BY id LIMIT 5""", (now,))]

    def _finish(self, row, now, status, error=None, message_id=None, retry_at=None, count_attempt=True):
        attempts = row["attempts"] + (1 if count_attempt else 0)
        if status == "pending" and attempts >= self.config.max_attempts:
            status = "failed"
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("""UPDATE telegram_outbox SET status=?, attempts=?, next_attempt_at=?, sent_at=?,
                telegram_message_id=?, last_error=? WHERE id=? AND status='pending'""",
                         (status, attempts, retry_at or now, now if status == "sent" else None, message_id, error, row["id"]))
            if status == "sent":
                set_meta(conn, "telegram_last_sent_at", now)
            elif error:
                set_meta(conn, "telegram_last_error", json.dumps({"at": now, "error": error, "outbox_id": row["id"]}))
            self._health(conn, status, error, now)

    def _health(self, conn, status, error, now):
        """One warning event when delivery starts failing and one when it recovers (not sent to Telegram)."""
        from .alert_engine import event  # local import: alert_engine imports nothing from here
        failing = meta(conn, "telegram_failing") == "1"
        if status == "failed" and not failing:
            set_meta(conn, "telegram_failing", "1")
            event(conn, f"telegram_failed:{time.time_ns()}", "telegram_failed", "warning", "ส่ง Telegram ไม่สำเร็จ",
                  f"{error or 'ไม่ทราบสาเหตุ'} · ตรวจ bot token, chat ID และอินเทอร์เน็ต", now)
        elif status == "sent" and failing:
            set_meta(conn, "telegram_failing", "0")
            event(conn, f"telegram_ok:{time.time_ns()}", "telegram_recovered", "success", "ส่ง Telegram ได้อีกครั้ง",
                  "ส่งข้อความล่าสุดสำเร็จ", now)

    def backoff(self, attempts):
        return min(300.0, 5.0 * 2 ** max(0, attempts))

    async def deliver(self, client: httpx.AsyncClient, now=None) -> int:
        """Send due messages once; returns how many rows changed state."""
        if not self.active:
            return 0
        now = time.time() if now is None else now
        rows = await run_in_threadpool(self._due, now)
        for row in rows:
            if row["expires_at"] is not None and now > row["expires_at"]:
                if row["kind"] != "trade_alert" or not self.config.send_expired_alerts:
                    await run_in_threadpool(self._finish, row, now, "expired", None, None, None, False)
                    continue
                row["text"] = ("⌛ หมดอายุแล้ว (ส่งช้า) · ห้ามเข้าตามข้อความนี้\n" + row["text"])[:MAX_TEXT]
            outcome = await self._send(client, row, now)
            await run_in_threadpool(self._finish, row, now, *outcome)
        return len(rows)

    async def _send(self, client, row, now):
        """Return (status, error, message_id, retry_at, count_attempt) for ``_finish``."""
        url = f"{self.api_base}/bot{self._token}/sendMessage"
        body = {"chat_id": self.chat_id, "text": row["text"], "disable_web_page_preview": True}
        try:
            response = await client.post(url, json=body)
        except httpx.HTTPError as exc:
            return "pending", self.clean(type(exc).__name__), None, now + self.backoff(row["attempts"]), True
        try:
            data = response.json()
        except ValueError:
            data = {}
        if response.status_code == 200 and data.get("ok") is True:
            return "sent", None, (data.get("result") or {}).get("message_id"), None, False
        error = self.clean(f"HTTP {response.status_code}: {data.get('description') or 'no description'}")
        if response.status_code == 429:
            wait = (data.get("parameters") or {}).get("retry_after")
            wait = float(wait) if isinstance(wait, (int, float)) and 0 < wait <= 3600 else self.backoff(row["attempts"])
            return "pending", error, None, now + wait, False
        if response.status_code >= 500:
            return "pending", error, None, now + self.backoff(row["attempts"]), True
        return "failed", error, None, None, True  # 400/401/403/404: bad chat, token or bot blocked

    async def run(self):
        self._loop = asyncio.get_running_loop()
        timeout = httpx.Timeout(self.config.timeout_seconds)
        async with httpx.AsyncClient(timeout=timeout, trust_env=False, follow_redirects=False,
                                     transport=self.transport) as client:
            while True:
                try:
                    await run_in_threadpool(self.enqueue_events)
                    await self.deliver(client)
                except Exception as exc:
                    log.warning("telegram_notifier_error %s", self.clean(type(exc).__name__))
                self.wake.clear()
                try:
                    await asyncio.wait_for(self.wake.wait(), self.config.poll_seconds)
                except asyncio.TimeoutError:
                    pass

    # ------------------------------------------------------------------ status
    def status(self):
        with self.db.connect() as conn:
            counts = dict(conn.execute("SELECT status, COUNT(*) FROM telegram_outbox GROUP BY status").fetchall())
            last_sent = meta(conn, "telegram_last_sent_at")
            last_error = meta(conn, "telegram_last_error")
            failing = meta(conn, "telegram_failing") == "1"
        state = ("disabled" if not self.config.enabled else self.problem if self.problem else
                 "failing" if failing else "ready")
        chat = None
        if self.chat_id:
            chat = self.chat_id if self.chat_id.startswith("@") else "…" + self.chat_id[-4:]
        return {"enabled": self.config.enabled, "active": self.active, "state": state, "chat": chat,
                "events": list(self.config.events), "pending": counts.get("pending", 0), "sent": counts.get("sent", 0),
                "failed": counts.get("failed", 0), "expired": counts.get("expired", 0),
                "last_sent_at": float(last_sent) if last_sent else None,
                "last_error": json.loads(last_error) if last_error else None}
