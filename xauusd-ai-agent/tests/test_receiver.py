import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


@pytest.fixture
def settings(tmp_path):
    return Settings(webhook_token="w" * 43, api_token="r" * 43, database_path=tmp_path / "signals.db")


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings)) as session:
        yield session


@pytest.fixture
def payload():
    data = json.loads((Path(__file__).resolve().parents[1] / "examples/signal.json").read_text())
    close_ms = int(time.time() // 300) * 300_000
    data["bar_close_ms"] = close_ms
    data["h1"]["bar_close_ms"] = ((close_ms - 300_000) // 3_600_000) * 3_600_000
    data["signal_id"] = f"OANDA:XAUUSD:5:{close_ms}:BUY"
    return data


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def send(client, settings, payload):
    return client.post("/webhook", headers=auth(settings.webhook_token), json=payload)


def test_ingest_read_and_persistence(client, settings, payload):
    response = send(client, settings, payload)
    assert response.status_code == 201
    assert response.json()["status"] == "received"
    record_id = response.json()["id"]
    assert client.get("/health").json()["database"] == "ok"
    with TestClient(create_app(settings)) as restarted:
        row = restarted.get(f"/signals/{record_id}", headers=auth(settings.api_token)).json()
        assert row["payload"]["entry"] == payload["entry"]
        assert row["payload"]["context"]["market_structure"] is None
        result = restarted.get("/signals", headers=auth(settings.api_token)).json()
        assert len(result["items"]) == 1
        assert result["next_after_id"] == record_id
        assert restarted.get(f"/signals?after_id={record_id}", headers=auth(settings.api_token)).json()["items"] == []


def test_sell_signal(client, settings, payload):
    payload.update(side="SELL", sl=2505, tp=2490)
    payload["signal_id"] = payload["signal_id"].replace(":BUY", ":SELL")
    payload["h1"].update(trend="bearish", close=2502, ema_fast=2510, ema_slow=2520)
    payload["m5"].update(ema=2501, rsi=45, plus_di=15, minus_di=28)
    assert send(client, settings, payload).status_code == 201


def test_duplicate_and_conflict(client, settings, payload):
    first = send(client, settings, payload)
    second = send(client, settings, payload)
    assert second.status_code == 200
    assert second.json()["duplicate"] is True
    assert first.json()["id"] == second.json()["id"]
    payload["m5"]["adx"] = 30
    assert send(client, settings, payload).status_code == 409
    assert len(client.get("/signals", headers=auth(settings.api_token)).json()["items"]) == 1


def test_concurrent_deliveries_are_atomic(client, settings, payload):
    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(lambda _: send(client, settings, payload), range(16)))
    assert [r.status_code for r in responses].count(201) == 1
    assert [r.status_code for r in responses].count(200) == 15
    assert len({r.json()["id"] for r in responses}) == 1


def test_auth_and_tradingview_route(client, settings, payload):
    assert client.post("/webhook", json=payload).status_code == 401
    assert client.post("/webhook", headers=auth(settings.api_token), json=payload).status_code == 401
    assert client.post("/webhook/tradingview/wrong", json=payload).status_code == 401
    assert client.get("/signals").status_code == 401
    assert client.get("/signals/1", headers=auth(settings.webhook_token)).status_code == 401
    assert client.post(f"/webhook/tradingview/{settings.webhook_token}", json=payload).status_code == 201
    stored = client.get("/signals", headers=auth(settings.api_token)).text
    assert settings.webhook_token not in stored and settings.api_token not in stored


@pytest.mark.parametrize("path,value", [
    (("entry",), "2500"), (("entry",), True), (("entry",), -1),
    (("schema_version",), True), (("schema_version",), 2),
    (("candle_confirmed",), False), (("candle_confirmed",), "true"),
    (("h1", "confirmed"), False), (("h1", "trend"), "bearish"),
    (("sl",), 2505), (("tp",), 2490), (("risk_reward",), 5),
    (("m5", "high"), 2497), (("m5", "close"), 2501),
    (("m5", "rsi"), 101), (("m5", "atr"), 0),
    (("context", "prior_range_low"), 2600), (("context", "market_structure"), "invented"),
    (("signal_id",), "wrong"), (("timeframe",), "60"),
    (("unknown_secret",), "do-not-echo-this"),
])
def test_invalid_signal_rejected_without_storage(client, settings, payload, path, value):
    obj = payload
    for key in path[:-1]:
        obj = obj[key]
    obj[path[-1]] = value
    response = send(client, settings, payload)
    assert response.status_code == 422
    assert "do-not-echo-this" not in response.text
    assert client.get("/signals", headers=auth(settings.api_token)).json()["items"] == []


@pytest.mark.parametrize("offset", [-3_600_000, 3_600_000, 1])
def test_stale_future_and_unaligned_time(client, settings, payload, offset):
    payload["bar_close_ms"] += offset
    payload["h1"]["bar_close_ms"] = payload["bar_close_ms"] - 3_600_000
    payload["signal_id"] = f'OANDA:XAUUSD:5:{payload["bar_close_ms"]}:BUY'
    assert send(client, settings, payload).status_code == 422


def test_h1_must_be_previously_closed(client, settings, payload):
    payload["h1"]["bar_close_ms"] = payload["bar_close_ms"]
    assert send(client, settings, payload).status_code == 422


def test_symbol_allowlist(client, settings, payload):
    payload["symbol"] = "OTHER:XAUUSD"
    payload["signal_id"] = payload["signal_id"].replace("OANDA:", "OTHER:")
    assert send(client, settings, payload).status_code == 422


@pytest.mark.parametrize("body", ['{', '{"entry":NaN}', '{"entry":Infinity}', '{"entry":1,"entry":2}', '\xff'])
def test_malformed_json(client, settings, body):
    response = client.post("/webhook", headers={**auth(settings.webhook_token), "Content-Type": "application/json"}, content=body.encode("latin1"))
    assert response.status_code == 400


def test_overflow_number(client, settings, payload):
    raw = json.dumps(payload).replace('"entry": 2500', '"entry": 1e999')
    response = client.post("/webhook", headers={**auth(settings.webhook_token), "Content-Type": "application/json"}, content=raw)
    assert response.status_code == 422


def test_content_type_and_size(client, settings):
    headers = auth(settings.webhook_token)
    assert client.post("/webhook", headers=headers, content="{}").status_code == 415
    headers["Content-Type"] = "application/json"
    assert client.post("/webhook", headers=headers, content=" " * (settings.max_body_bytes + 1)).status_code == 413
    # Streaming input deliberately has no Content-Length.
    assert client.post("/webhook", headers=headers, content=iter([b" " * 9000, b" " * 9000])).status_code == 413
    headers["Content-Encoding"] = "gzip"
    assert client.post("/webhook", headers=headers, content=b"{}").status_code == 415


def test_rate_limit(settings, payload):
    with TestClient(create_app(replace(settings, webhook_rate_per_minute=2))) as client:
        assert send(client, settings, payload).status_code == 201
        assert send(client, settings, payload).status_code == 200
        response = send(client, settings, payload)
        assert response.status_code == 429
        assert response.headers["Retry-After"] == "60"


def test_database_busy_returns_retryable_error(client, settings, payload):
    with sqlite3.connect(settings.database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        response = send(client, settings, payload)
        assert response.status_code == 503
        assert response.headers["Retry-After"] == "1"
        connection.rollback()
    assert send(client, settings, payload).status_code == 201


def test_missing_record_and_pagination_bounds(client, settings):
    headers = auth(settings.api_token)
    assert client.get("/signals/999", headers=headers).status_code == 404
    assert client.get("/signals?limit=1000", headers=headers).status_code == 422
    assert client.get("/signals?after_id=-1", headers=headers).status_code == 422
    assert client.get(f"/signals?after_id={2**64}", headers=headers).status_code == 422
    assert client.get(f"/signals/{2**64}", headers=headers).status_code == 422


def test_invalid_credentials_fail_at_startup(settings):
    with pytest.raises(ValueError):
        replace(settings, webhook_token="")
    with pytest.raises(ValueError):
        replace(settings, api_token=settings.webhook_token)
