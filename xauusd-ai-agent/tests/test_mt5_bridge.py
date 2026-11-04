import copy
import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app.mt5_bridge import BridgeSettings, create_bridge


@pytest.fixture
def settings(tmp_path):
    return BridgeSettings(token="m" * 43, database_path=tmp_path / "market.db")


@pytest.fixture
def client(settings):
    with TestClient(create_bridge(settings)) as session:
        yield session


@pytest.fixture
def payload():
    now = int(time.time())
    server = now + 7200
    def bars(period):
        end = server // period * period
        return [{"open_server_s": end - i * period, "open": 2500, "high": 2502,
                 "low": 2499, "close": 2501, "tick_volume": 20, "spread_points": 30}
                for i in range(300, 0, -1)]
    return {"schema_version": 1, "source": "mt5", "account_mode": "demo",
            "server": "XMGlobal-MT5 7", "broker_symbol": "GOLD",
            "captured_at_utc_s": now, "server_time_s": server, "observed_utc_offset_s": 7200,
            "digits": 2, "point": 0.01, "tick_size": 0.01,
            "quote": {"time_server_ms": server * 1000, "bid": 2500, "ask": 2500.30},
            "m5": bars(300), "h1": bars(3600)}


def send(client, settings, payload):
    return client.post("/mt5/snapshot", json=payload,
                       headers={"Authorization": "Bearer " + settings.token})


def read(client, settings):
    return client.get("/mt5/latest?symbol=GOLD", headers={"Authorization": "Bearer " + settings.token})


def test_persistence_provenance_and_no_signals(client, settings, payload):
    result = send(client, settings, payload)
    assert result.status_code == 201
    assert result.json()["m5_bars"] == 300
    with TestClient(create_bridge(settings)) as restarted:
        row = read(restarted, settings).json()
        assert row["status"] == "fresh"
        assert row["mode"] == "market_data_only"
        # Historical server times are preserved, not shifted using today's UTC offset.
        assert row["payload"] == payload
    with sqlite3.connect(settings.database_path) as db:
        tables = {x[0] for x in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "mt5_snapshots" in tables and "signals" not in tables


def test_auth_no_data_and_duplicate(client, settings, payload):
    assert client.get("/health").status_code == 200
    assert client.post("/mt5/snapshot", json=payload).status_code == 401
    assert client.get("/mt5/latest?symbol=GOLD").status_code == 401
    assert read(client, settings).status_code == 404
    assert send(client, settings, payload).status_code == 201
    assert send(client, settings, payload).status_code == 200
    payload["quote"]["ask"] += 0.01
    assert send(client, settings, payload).status_code == 409


def test_rejection_diagnostics_are_private_and_do_not_echo_payload(client,settings,payload):
    assert client.get('/mt5/status').status_code==401
    headers={"Authorization":"Bearer "+settings.token}
    payload['unknown_secret']='never-echo-this-credential'
    assert send(client,settings,payload).status_code==422
    result=client.get('/mt5/status',headers=headers)
    assert 'never-echo-this-credential' not in result.text
    assert result.json()['last_attempt']['reason']=='schema_validation'
    payload.pop('unknown_secret')
    payload['broker_symbol']='EURUSD'
    assert send(client,settings,payload).status_code==422
    assert client.get('/mt5/status',headers=headers).json()['last_attempt']['broker_symbol']=='EURUSD'
    payload['broker_symbol']='GOLD'
    assert send(client,settings,payload).status_code==201
    assert client.get('/mt5/status',headers=headers).json()['last_attempt']['status']=='accepted'


def test_concurrent_duplicate_is_atomic(client, settings, payload):
    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(lambda _: send(client, settings, payload), range(8)))
    assert [r.status_code for r in responses].count(201) == 1
    assert [r.status_code for r in responses].count(200) == 7


@pytest.mark.parametrize("path,value", [
    (("account_mode",), "real"), (("server",), "Other-Server"),
    (("broker_symbol",), "EURUSD"), (("schema_version",), True),
    (("captured_at_utc_s",), 1), (("observed_utc_offset_s",), 0),
    (("quote", "bid"), 2501), (("quote", "time_server_ms"), 1),
    (("m5", 0, "high"), 2490), (("h1", 0, "tick_volume"), -1),
    (("m5", 0, "open_server_s"), 1), (("m5",), []),
    (("digits",), 9), (("point",), 0), (("tick_size",), "0.01"),
    (("unknown_secret",), "must-not-echo"),
])
def test_invalid_data_never_stored(client, settings, payload, path, value):
    obj = payload
    for key in path[:-1]:
        obj = obj[key]
    obj[path[-1]] = value
    response = send(client, settings, payload)
    assert response.status_code == 422
    assert "must-not-echo" not in response.text
    assert read(client, settings).status_code == 404


def test_ordering_open_bar_and_stale_history(client, settings, payload):
    for frame in ("m5", "h1"):
        invalid = copy.deepcopy(payload)
        invalid[frame].reverse()
        assert send(client, settings, invalid).status_code == 422
        invalid = copy.deepcopy(payload)
        period = 300 if frame == "m5" else 3600
        invalid[frame][-1]["open_server_s"] = invalid["server_time_s"] // period * period
        assert send(client, settings, invalid).status_code == 422
    payload["m5"] = payload["m5"][:-5]
    assert send(client, settings, payload).status_code == 422


def test_stale_read_after_time_passes(client, settings, payload, monkeypatch):
    assert send(client, settings, payload).status_code == 201
    monkeypatch.setattr("app.mt5_bridge.time.time", lambda: payload["captured_at_utc_s"] + 121)
    assert read(client, settings).json()["status"] == "stale"
    assert send(client, settings, payload).status_code == 422


def test_strict_json_limits_and_rate(client, settings, payload):
    headers = {"Authorization": "Bearer " + settings.token, "Content-Type": "application/json"}
    for body in ('{"x":1,"x":2}', '{"x":NaN}', '{'):
        assert client.post("/mt5/snapshot", headers=headers, content=body).status_code == 400
    assert client.post("/mt5/snapshot", headers=headers,
                       content=iter([b" " * 300000, b" " * 300000])).status_code == 413
    headers["Content-Encoding"] = "gzip"
    assert client.post("/mt5/snapshot", headers=headers, content="{}").status_code == 415
    for _ in range(15):
        send(client, settings, payload)
    assert send(client, settings, payload).status_code == 429


def test_catchup_bursts_have_separate_bounded_budget(client,settings,payload):
    now=payload['captured_at_utc_s'];stamp=(now+7200)*1000
    body={'schema_version':1,'source':'mt5','account_mode':'demo','server':settings.server,'broker_symbol':'GOLD',
          'captured_at_utc_s':now,'server_time_s':now+7200,'observed_utc_offset_s':7200,
          'from_server_ms':stamp-1,'through_server_ms':stamp,
          'ticks':[{'time_server_ms':stamp,'ordinal':0,'bid':2500,'ask':2500.3}]}
    headers={'Authorization':'Bearer '+settings.token}
    assert client.post('/mt5/ticks',json=body).status_code==401
    for page in range(32):
        if page:
            body['from_server_ms']=body['through_server_ms']+1
            body['through_server_ms']+=1
            body['ticks'][0]['time_server_ms']=body['through_server_ms']
        assert client.post('/mt5/ticks',json=body,headers=headers).status_code in (200,201)
    assert send(client,settings,payload).status_code==201
    for _ in range(32):
        assert client.post('/mt5/ticks',json=body,headers=headers).status_code==200
    assert client.post('/mt5/ticks',json=body,headers=headers).status_code==429
    assert send(client,settings,payload).status_code==200


def test_rolling_storage_bound(settings, payload):
    from app.mt5_bridge import MarketStore, Snapshot
    store = MarketStore(settings.database_path)
    store.initialize()
    for i in range(245):
        item = copy.deepcopy(payload)
        item["captured_at_utc_s"] += i
        item["server_time_s"] += i
        item["quote"]["time_server_ms"] += i * 1000
        store.insert(Snapshot.model_validate(item))
    with sqlite3.connect(settings.database_path) as db:
        assert db.execute("SELECT COUNT(*) FROM mt5_snapshots").fetchone()[0] == 240
    assert store.latest("GOLD")["payload"]["captured_at_utc_s"] == payload["captured_at_utc_s"] + 244
