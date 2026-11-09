"""Telegram outbox and delivery with a mocked Bot API (no network, fake credentials)."""
import asyncio
import json
import logging

import httpx
import pytest
from fastapi.testclient import TestClient

from app.alert_engine import ensure_tables as ensure_alert_tables, event
from app.alert_main import create_alert_app
from app.config import Settings
from app.database import Database
from app.mt5_bridge import BridgeSettings, MarketStore
from app.telegram_notifier import TelegramConfig, TelegramNotifier, credentials_from_env

TOKEN = "123456789:" + "A" * 35
CHAT = "-1001234567890"


def config(**changes):
    data = {"version": "telegram-v1", "enabled": True, "events": ["trade_alert", "health_down"],
            "send_expired_alerts": False, "timeout_seconds": 1, "max_attempts": 3, "poll_seconds": 0.05}
    return TelegramConfig.model_validate(data | changes)


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "alerts.db")
    with database.connect() as conn:
        ensure_alert_tables(conn)
    return database


def trade_alert(conn, key="XM:XAUUSD:5:1:BUY:v", valid_until=1_000.0, side="BUY"):
    payload = {"side": side, "valid_until_utc_s": valid_until}
    conn.execute("INSERT INTO trade_alerts(alert_key,status,side,bar_close_utc_ms,created_at,payload_json) VALUES(?,?,?,?,?,?)",
                 (key, "alert", side, 1, 0, json.dumps(payload)))
    event(conn, "alert:" + key, "trade_alert", "success", f"เข้า {side} ได้ · GOLD", "ราคาอ้างอิง 1 · SL 0 · TP 2", 0)


class FakeTelegram:
    def __init__(self, *responses):
        self.responses, self.requests = list(responses), []

    def __call__(self, request: httpx.Request):
        self.requests.append(request)
        status, body = self.responses.pop(0) if self.responses else (200, {"ok": True, "result": {"message_id": len(self.requests)}})
        if isinstance(body, Exception):
            raise body
        return httpx.Response(status, json=body)


def deliver(notifier, fake, now):
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(fake)) as client:
            return await notifier.deliver(client, now)
    return asyncio.run(go())


def rows(db):
    with db.connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM telegram_outbox ORDER BY id")]


def test_credentials_validation(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    assert credentials_from_env()[2] == "missing_credentials"
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "not-a-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", CHAT)
    assert credentials_from_env()[2] == "invalid_bot_token"
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12ab")
    assert credentials_from_env()[2] == "invalid_chat_id"
    monkeypatch.setenv("TELEGRAM_CHAT_ID", CHAT)
    assert credentials_from_env() == (TOKEN, CHAT, None)
    with pytest.raises(ValueError):
        config(events=["anything"])


def test_baseline_then_queue_once_and_send(db):
    with db.connect() as conn:
        trade_alert(conn, "old")  # existed before Telegram was enabled: never sent
    n = TelegramNotifier(db, config(), TOKEN, CHAT)
    assert n.enqueue_events(now=100) == 0
    with db.connect() as conn:
        trade_alert(conn, "new", valid_until=200, side="SELL")
        event(conn, "mute:1", "alerts_muted", "info", "ปิดเสียง", "x", 0)  # kind not selected
        event(conn, "down:feed:1", "health_down", "warning", "ราคาจาก MT5 ขัดข้อง", "y", 0)
    assert n.enqueue_events(now=100) == 2
    assert n.enqueue_events(now=100) == 0  # cursor persisted, outbox keyed by event
    n2 = TelegramNotifier(db, config(), TOKEN, CHAT)  # restart
    assert n2.enqueue_events(now=101) == 0
    fake = FakeTelegram()
    assert deliver(n2, fake, now=110) == 2
    out = rows(db)
    assert [r["status"] for r in out] == ["sent", "sent"]
    assert out[0]["text"].startswith("📉 เข้า SELL ได้") and "ไม่ได้ส่งคำสั่งซื้อขาย" in out[0]["text"]
    assert out[1]["text"].startswith("⚠️ ราคาจาก MT5 ขัดข้อง")
    body = json.loads(fake.requests[0].content)
    assert body["chat_id"] == CHAT and fake.requests[0].url.path == f"/bot{TOKEN}/sendMessage"
    assert deliver(n2, fake, now=120) == 0 and len(fake.requests) == 2
    status = n2.status()
    assert status["state"] == "ready" and status["sent"] == 2 and status["chat"] == "…7890"
    assert TOKEN not in json.dumps(status)


def test_disabled_or_unconfigured_keeps_cursor_current(db):
    n = TelegramNotifier(db, config(enabled=False))
    with db.connect() as conn:
        trade_alert(conn, "a")
    assert n.enqueue_events() == 0 and n.status()["state"] == "disabled"
    missing = TelegramNotifier(db, config(), None, None, "missing_credentials")
    with db.connect() as conn:
        trade_alert(conn, "b")
    assert missing.enqueue_events() == 0 and missing.status()["state"] == "missing_credentials"
    assert missing.queue_test()["queued"] is False
    enabled = TelegramNotifier(db, config(), TOKEN, CHAT)
    assert enabled.enqueue_events() == 0 and rows(db) == []  # nothing from before activation


def test_expired_alert_is_not_sent_unless_configured(db):
    n = TelegramNotifier(db, config(), TOKEN, CHAT)
    n.enqueue_events(now=0)
    with db.connect() as conn:
        trade_alert(conn, "late", valid_until=50)
    n.enqueue_events(now=10)
    fake = FakeTelegram()
    deliver(n, fake, now=60)
    assert rows(db)[0]["status"] == "expired" and fake.requests == []
    marked = TelegramNotifier(db, config(send_expired_alerts=True), TOKEN, CHAT)
    with db.connect() as conn:
        trade_alert(conn, "late2", valid_until=50)
    marked.enqueue_events(now=10)
    deliver(marked, fake, now=60)
    assert json.loads(fake.requests[0].content)["text"].startswith("⌛ หมดอายุแล้ว")


def test_retry_rate_limit_and_permanent_failure_redact_token(db, caplog):
    n = TelegramNotifier(db, config(max_attempts=2), TOKEN, CHAT)
    n.enqueue_events(now=0)
    with db.connect() as conn:
        trade_alert(conn, "r", valid_until=10_000)
    n.enqueue_events(now=0)
    fake = FakeTelegram((429, {"ok": False, "description": "Too Many Requests", "parameters": {"retry_after": 7}}),
                        (502, {"ok": False, "description": "Bad Gateway"}),
                        (0, httpx.ConnectError(f"cannot reach https://api.telegram.org/bot{TOKEN}/sendMessage")))
    deliver(n, fake, now=1)
    row = rows(db)[0]
    assert row["status"] == "pending" and row["attempts"] == 0 and row["next_attempt_at"] == 8
    assert deliver(n, fake, now=5) == 0  # not due yet
    deliver(n, fake, now=8)
    row = rows(db)[0]
    assert row["attempts"] == 1 and row["next_attempt_at"] == 8 + 5 and row["last_error"] == "HTTP 502: Bad Gateway"
    deliver(n, fake, now=100)
    row = rows(db)[0]
    assert row["status"] == "failed" and row["last_error"] == "ConnectError"
    status = n.status()
    assert status["state"] == "failing" and TOKEN not in json.dumps(status)
    with db.connect() as conn:
        kinds = [r[0] for r in conn.execute("SELECT kind FROM control_events")]
    assert kinds.count("telegram_failed") == 1
    n.enqueue_events(now=101)
    assert len(rows(db)) == 1  # the failure warning is not mirrored to Telegram
    # Permanent errors stop immediately; a later success records recovery.
    with db.connect() as conn:
        trade_alert(conn, "p", valid_until=10_000)
    n.enqueue_events(now=101)
    deliver(n, FakeTelegram((403, {"ok": False, "description": f"Forbidden: bot{TOKEN} was blocked"})), now=102)
    assert rows(db)[1]["status"] == "failed" and TOKEN not in rows(db)[1]["last_error"]
    assert n.queue_test(now=103)["queued"] is True
    assert n.queue_test(now=103) == {"queued": False, "reason": "test_already_pending"}
    deliver(n, FakeTelegram(), now=104)
    assert n.status()["state"] == "ready"
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM control_events WHERE kind='telegram_recovered'").fetchone()[0] == 1
    assert TOKEN not in caplog.text


def test_alert_app_status_and_test_route(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", CHAT)
    settings = Settings(webhook_token="w" * 43, api_token="r" * 43, database_path=tmp_path / "unused.db",
                        allowed_symbols=("XM:XAUUSD",))
    feed = BridgeSettings(token="m" * 43, database_path=tmp_path / "mt5.db")
    MarketStore(feed.database_path).initialize()
    fake = FakeTelegram()
    app = create_alert_app(settings, feed, database_path=tmp_path / "alerts.db",
                           telegram_config=config(), telegram_transport=httpx.MockTransport(fake))
    auth = {"Authorization": "Bearer " + "r" * 43}
    with TestClient(app) as client:
        assert client.post("/telegram/test").status_code == 401
        state = client.get("/dashboard/state", headers=auth).json()["telegram"]
        assert state["state"] == "ready" and state["worker"] is True and state["active"] is True
        assert client.post("/telegram/test", headers=auth).json()["queued"] is True
        for _ in range(100):
            if fake.requests:
                break
            __import__("time").sleep(0.02)
        assert json.loads(fake.requests[0].content)["text"].startswith("🧪")
        for _ in range(100):
            if client.get("/telegram/status", headers=auth).json()["sent"] == 1:
                break
            __import__("time").sleep(0.02)
        assert client.get("/telegram/status", headers=auth).json()["sent"] == 1
    disabled = create_alert_app(settings, feed, database_path=tmp_path / "alerts2.db",
                                telegram_config=config(enabled=False), telegram_transport=httpx.MockTransport(fake))
    with TestClient(disabled) as client:
        response = client.post("/telegram/test", headers=auth)
        assert response.status_code == 409 and response.json()["reason"] == "disabled"
