"""Read-only Telegram commands with a mocked Bot API (no network, fake credentials)."""
import asyncio
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from app.alert_engine import ensure_tables as ensure_alert_tables
from app.alert_main import create_alert_app
from app.config import Settings
from app.database import Database
from app.mt5_bridge import BridgeSettings, MarketStore
from app.telegram_commands import TelegramCommands, parse_command, signal_text, status_text
from app.telegram_notifier import TelegramConfig, TelegramNotifier

TOKEN = "123456789:" + "A" * 35
CHAT = "-1001234567890"
NOW = 1_800_000_000.0

SNAP = {
    "connected": True, "ready": True, "muted": False,
    "feed": {"available": True, "bid": 4116.16, "ask": 4116.73, "age_seconds": 3.2, "m5_bars": 1000, "h1_bars": 1000,
             "digits": 2},
    "engine": {"status": "watching", "last_bar_close_utc_s": NOW - 120,
               "warmup": {"ready": True, "h1": 1000, "required_h1": 400, "m5": 1000, "required_m5": 400},
               "indicators": {"bias": "BUY", "rsi2": 21.34, "atr14": 3.891}},
    "telegram": {"state": "ready", "pending": 0, "failed": 0},
    "latest_alert": None,
}


def config(**changes):
    data = {"version": "telegram-v1", "enabled": True, "events": ["trade_alert"], "timeout_seconds": 1,
            "max_attempts": 3, "poll_seconds": 0.05, "command_poll_seconds": 1, "command_max_age_seconds": 120}
    return TelegramConfig.model_validate(data | changes)


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "alerts.db")
    with database.connect() as conn:
        ensure_alert_tables(conn)
    return database


def outbox(db):
    with db.connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM telegram_outbox ORDER BY id")]


def update(uid, text, date=NOW, chat=CHAT):
    return {"update_id": uid, "message": {"message_id": uid, "date": date, "text": text,
                                          "chat": {"id": int(chat), "type": "group"}}}


def test_parse_and_formatting():
    assert parse_command("/status") == "status" and parse_command("/Status@XMBot now") == "status"
    assert parse_command("/start") == "help" and parse_command("/nope") == "unknown"
    assert parse_command("status") is None and parse_command(None) is None and parse_command("/") is None
    text = status_text(SNAP, NOW)
    assert text.startswith("📊 สถานะ XM Control") and "🟢 พร้อมแจ้งเตือน" in text
    assert "Bid 4,116.16 · Ask 4,116.73 · อายุ 3 วินาที · M5 1000 / H1 1000" in text
    assert "แนวโน้มขาขึ้น · รอ RSI(2) ย่อ · RSI(2) 21.3 · ATR 3.89" in text and "Telegram: พร้อมส่ง" in text
    assert status_text({"connected": False}) == "⚠️ อ่านสถานะบริการไม่ได้"
    assert signal_text(SNAP, NOW).startswith("ยังไม่มีสัญญาณ")
    alert = {"side": "SELL", "valid_until_utc_s": NOW + 60, "bar_close_utc_s": NOW - 240, "source_symbol": "GOLD",
             "entry": 4100.5, "entry_limit": 4099.5, "sl": 4107.0, "tp": 4084.0, "net_reward_risk": 2.5,
             "time_stop_utc_s": NOW + 14000, "digits": 2}
    live = signal_text(SNAP | {"latest_alert": alert}, NOW)
    assert live.startswith("📉 สัญญาณ SELL · เข้าได้ถึง") and "Bid ไม่ต่ำกว่า 4,099.50" in live
    assert "ไม่ได้ส่งคำสั่งซื้อขาย" in live
    assert "ดู /signal" in status_text(SNAP | {"latest_alert": alert}, NOW)
    assert signal_text(SNAP | {"latest_alert": alert}, NOW + 61).startswith("⌛")


def test_switch_requires_working_telegram_and_is_stored(db):
    off = TelegramCommands(TelegramNotifier(db, config(enabled=False)), lambda: SNAP)
    assert off.set_enabled(True) == {"changed": False, "enabled": False, "reason": "disabled"}
    assert off.status()["state"] == "off"
    n = TelegramNotifier(db, config(), TOKEN, CHAT)
    c = TelegramCommands(n, lambda: SNAP)
    assert c.set_enabled(True, now=NOW) == {"changed": True, "enabled": True}
    assert c.set_enabled(True, now=NOW + 50) == {"changed": False, "enabled": True}
    again = TelegramCommands(TelegramNotifier(db, config(), TOKEN, CHAT), lambda: SNAP)  # restart
    assert again.enabled() and again.status()["state"] == "listening"
    assert again.set_enabled(False)["changed"] is True and again.status()["state"] == "off"


def test_only_configured_chat_fresh_commands_once(db):
    calls = []
    n = TelegramNotifier(db, config(), TOKEN, CHAT)
    c = TelegramCommands(n, lambda: calls.append(1) or SNAP)
    c.set_enabled(True, now=NOW - 10)
    updates = [update(10, "/status"), update(11, "/status", chat="-100999"),  # stranger
               update(12, "/signal", date=NOW - 30),  # sent before commands were switched on
               update(13, "hello"), update(14, "/what"), update(15, "/help@XMBot"),
               {"update_id": 16, "edited_message": {"text": "/status"}}, {"update_id": True}]
    assert c.handle_updates(updates, now=NOW) == 3
    rows = outbox(db)
    assert [r["event_key"] for r in rows] == ["cmd:10", "cmd:14", "cmd:15"]
    assert rows[0]["text"].startswith("📊") and rows[1]["text"].startswith("ไม่รู้จักคำสั่ง") and "/signal" in rows[2]["text"]
    assert all(r["kind"] == "command_reply" and r["expires_at"] == NOW + 120 for r in rows)
    assert calls == [1]  # one snapshot per batch
    status = c.status()
    assert status["rejected_chats"] == 1 and status["last_command_at"] == NOW
    with db.connect() as conn:
        assert conn.execute("SELECT value FROM control_meta WHERE key='telegram_commands_offset'").fetchone()[0] == "17"
    assert c.handle_updates(updates, now=NOW) == 0 and len(outbox(db)) == 3  # Telegram re-delivers: no duplicates
    assert c.handle_updates([update(20, "/status", date=NOW - 500)], now=NOW) == 0  # backlog after sleep


def test_reply_rate_limit(db):
    c = TelegramCommands(TelegramNotifier(db, config(), TOKEN, CHAT), lambda: SNAP)
    c.set_enabled(True, now=NOW - 5)
    assert c.handle_updates([update(i, "/help") for i in range(1, 30)], now=NOW) == 20


def test_expired_reply_is_dropped_even_when_expired_alerts_are_sent(db):
    n = TelegramNotifier(db, config(send_expired_alerts=True), TOKEN, CHAT)
    c = TelegramCommands(n, lambda: SNAP)
    c.set_enabled(True, now=NOW - 5)
    c.handle_updates([update(1, "/status")], now=NOW)
    requests = []

    async def go():
        transport = httpx.MockTransport(lambda r: requests.append(r) or httpx.Response(200, json={"ok": True}))
        async with httpx.AsyncClient(transport=transport) as client:
            await n.deliver(client, NOW + 500)
    asyncio.run(go())
    assert outbox(db)[0]["status"] == "expired" and requests == []


def poll(c, handler):
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await c.poll_once(client)
    return asyncio.run(go())


def test_poll_offset_conflict_and_token_redaction(db, caplog):
    c = TelegramCommands(TelegramNotifier(db, config(), TOKEN, CHAT), lambda: SNAP)
    c.set_enabled(True, now=time.time() - 5)
    seen = []

    def ok(request):
        seen.append(request)
        return httpx.Response(200, json={"ok": True, "result": [update(41, "/help", date=time.time())]})
    assert poll(c, ok) <= 1.0
    assert seen[0].url.path == f"/bot{TOKEN}/getUpdates" and "offset" not in seen[0].url.params
    assert seen[0].url.params["timeout"] == "1" and json.loads(seen[0].url.params["allowed_updates"]) == ["message", "channel_post"]
    assert outbox(db)[0]["event_key"] == "cmd:41"
    poll(c, ok)
    assert seen[1].url.params["offset"] == "42"
    conflict = lambda r: httpx.Response(409, json={"ok": False, "description": "Conflict: terminated by other getUpdates request"})
    assert poll(c, conflict) == 60.0
    status = c.status()
    assert status["state"] == "failing" and status["last_error"]["error"].startswith("HTTP 409")

    def broken(request):
        raise httpx.ConnectError(f"cannot reach https://api.telegram.org/bot{TOKEN}/getUpdates")
    assert poll(c, broken) == 10.0  # second consecutive failure
    assert c.status()["last_error"]["error"] == "ConnectError"
    assert poll(c, lambda r: httpx.Response(200, json={"ok": True, "result": []})) <= 1.0
    assert c.status()["state"] == "listening" and c.failures == 0
    assert TOKEN not in caplog.text and TOKEN not in json.dumps(c.status())


def test_app_switch_no_polling_while_off(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", CHAT)
    settings = Settings(webhook_token="w" * 43, api_token="r" * 43, database_path=tmp_path / "unused.db",
                        allowed_symbols=("XM:XAUUSD",))
    feed = BridgeSettings(token="m" * 43, database_path=tmp_path / "mt5.db")
    MarketStore(feed.database_path).initialize()
    gets, sends, served = [], [], []

    def telegram(request):
        if request.url.path.endswith("/getUpdates"):
            gets.append(request)
            result = [] if served else [update(7, "/status", date=time.time())]
            served.append(1)
            return httpx.Response(200, json={"ok": True, "result": result})
        sends.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": len(sends)}})
    app = create_alert_app(settings, feed, database_path=tmp_path / "alerts.db", telegram_config=config(),
                           telegram_transport=httpx.MockTransport(telegram))
    auth = {"Authorization": "Bearer " + "r" * 43}

    def wait(check):
        for _ in range(150):
            if check():
                return True
            time.sleep(0.02)
        return False
    with TestClient(app) as client:
        assert client.post("/telegram/commands", json={"enabled": True}).status_code == 401
        time.sleep(0.3)
        assert gets == []  # off by default: no getUpdates at all
        state = client.get("/dashboard/state", headers=auth).json()["telegram"]["commands"]
        assert state["enabled"] is False and state["state"] == "off" and state["worker"] is True
        response = client.post("/telegram/commands", headers=auth, json={"enabled": True}).json()
        assert response["enabled"] is True and response["commands"]["state"] == "listening"
        assert wait(lambda: sends)
        assert sends[0]["text"].startswith("📊 สถานะ XM Control") and sends[0]["chat_id"] == CHAT
        assert client.post("/telegram/commands", headers=auth, json={"enabled": False}).json()["enabled"] is False
        time.sleep(1.2)  # let any open poll finish
        count = len(gets)
        time.sleep(0.5)
        assert len(gets) == count  # switched off: polling stopped
        assert client.post("/telegram/commands", headers=auth, json={"enabled": "yes"}).status_code == 422
    disabled = create_alert_app(settings, feed, database_path=tmp_path / "alerts2.db",
                                telegram_config=config(enabled=False), telegram_transport=httpx.MockTransport(telegram))
    with TestClient(disabled) as client:
        response = client.post("/telegram/commands", headers=auth, json={"enabled": True})
        assert response.status_code == 409 and response.json()["reason"] == "disabled"
