import json
import random

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.alert_engine import AlertCore, AlertEngine, AlertPolicy, alert_text, gate
from app.alert_main import create_alert_app
from app.config import ROOT, Settings
from app.database import Database
from app.mt5_bridge import BridgeSettings, MarketStore, Snapshot
from app.risk_manager import RiskPolicy
from backtest.data import resample

OFFSET = 7200
POINT, DIGITS = 0.01, 2


def policy(**changes):
    data = json.loads((ROOT / "config/alert-xm.json").read_text())
    data.update(warmup_h1_bars=200, warmup_m5_bars=200)
    data.update(changes)
    return AlertPolicy.model_validate(data)


@pytest.fixture(scope="module")
def risk():
    return RiskPolicy.load(ROOT / "config/risk-xm-paper.json")


@pytest.fixture(scope="module")
def market():
    """Volatile synthetic M1 bid bars with alternating multi-day trends (not market data)."""
    rng = random.Random(11)
    start, close, bars = 1_767_571_200, 2400.0, []  # Monday 2026-01-05 00:00 server time
    for minute in range(22 * 1440):
        t = start + minute * 60
        drift = 0.12 if (minute // (3 * 1440)) % 2 == 0 else -0.12
        open_ = close
        close = max(100.0, open_ + rng.gauss(drift, 2.2))
        high = max(open_, close) + abs(rng.gauss(0, 0.8))
        low = min(open_, close) - abs(rng.gauss(0, 0.8))
        bars.append({"open_server_s": t, "open": round(open_, 2), "high": round(high, 2), "low": round(low, 2),
                     "close": round(close, 2), "tick_volume": 30, "spread_points": 25})
        b = bars[-1]
        b["high"], b["low"] = max(b["high"], b["open"], b["close"]), min(b["low"], b["open"], b["close"])
        close = b["close"]
    strip = lambda rows: [{k: r[k] for k in ("open_server_s", "open", "high", "low", "close", "tick_volume", "spread_points")} for r in rows]
    return strip(resample(bars, 300)), strip(resample(bars, 3600))


def snapshot_at(market, index, window=300):
    """The EA snapshot taken 5 seconds after M5 bar ``index`` closed."""
    m5, h1 = market
    server = m5[index]["open_server_s"] + 305
    closed_h1 = [b for b in h1 if b["open_server_s"] + 3600 <= server][-window:]
    last = m5[index]
    return Snapshot.model_validate({
        "schema_version": 1, "source": "mt5", "account_mode": "demo", "server": "XMGlobal-MT5 7",
        "broker_symbol": "GOLD", "captured_at_utc_s": server - OFFSET, "server_time_s": server,
        "observed_utc_offset_s": OFFSET, "digits": DIGITS, "point": POINT, "tick_size": 0.01,
        "quote": {"time_server_ms": server * 1000, "bid": last["close"], "ask": round(last["close"] + 0.25, 2)},
        "m5": m5[max(0, index - window + 1): index + 1], "h1": closed_h1})


def replay(market, pol, risk, first, last):
    """Core stepped like the engine bootstrapped at ``first``: gated (close time, side, passed)."""
    m5, h1 = market
    boot = snapshot_at(market, first)
    h1_start = boot.h1[0].open_server_s
    queue = [b for b in h1 if b["open_server_s"] >= h1_start]
    core, out = AlertCore(pol), []
    start = boot.m5[0].open_server_s
    for bar in m5:
        if bar["open_server_s"] < start or bar["open_server_s"] > m5[last]["open_server_s"]:
            continue
        close_s = bar["open_server_s"] + 300
        new_h1 = [b for b in queue if b["open_server_s"] + 3600 <= close_s]
        queue = queue[len(new_h1):]
        signal, _ = core.step(bar, new_h1, POINT, DIGITS)
        if signal:
            spread = round(bar["spread_points"] * POINT, DIGITS)
            _, stop, target, reasons, _ = gate(risk, signal["side"], bar["close"], signal["sl"], signal["tp"], signal["atr"], [spread])
            if not reasons:
                core.open(signal["side"], stop, target, close_s)
            out.append((close_s, signal["side"], not reasons))
    return out


def engine(tmp_path, risk, pol=None):
    db = Database(tmp_path / "alerts.db")
    return AlertEngine(db, pol or policy(), risk)


def feed(eng, market, start, end, step=200):
    """Snapshots after bars start..end in jumps that keep an overlap with the checkpoint."""
    index = start
    while index < end:
        index = min(end, index + step)
        snap = snapshot_at(market, index)
        eng.process(snap, now=snap.captured_at_utc_s)


def rows(eng):
    with eng.db.connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM trade_alerts ORDER BY id")]


def events(eng, kind=None):
    with eng.db.connect() as conn:
        found = [dict(r) for r in conn.execute("SELECT * FROM control_events ORDER BY id")]
    return [e for e in found if kind is None or e["kind"] == kind]


def test_policy_is_the_walk_forward_choice_and_rejects_unsupported_exits():
    pol = AlertPolicy.load(ROOT / "config/alert-xm.json")
    params = pol.effective_params
    assert (params["rr"], params["sl_atr"], params["rsi_low"], params["cost_r_max"]) == (2.5, 1.8, 5, 0.08)
    assert params["exits"] == {"breakeven_r": None, "breakeven_lock_r": None, "time_stop_bars": 48}
    with pytest.raises(ValidationError):
        policy(params={**pol.params, "exits": {"breakeven_r": 0.6, "time_stop_bars": 24}})
    with pytest.raises(ValidationError):
        policy(params={**pol.params, "unknown": 1})


def test_checkpoint_roundtrip_continues_identically(market, risk):
    m5, h1 = market
    pol = policy()
    def run(split=None):
        core, out, h = AlertCore(pol), [], 0
        for i, bar in enumerate(m5):
            if i == split:
                core = AlertCore(pol, json.loads(json.dumps(core.dump())))
            close_s = bar["open_server_s"] + 300
            new = []
            while h < len(h1) and h1[h]["open_server_s"] + 3600 <= close_s:
                new.append(h1[h]); h += 1
            signal, _ = core.step(bar, new, POINT, DIGITS)
            if signal:
                _, stop, target, reasons, _ = gate(risk, signal["side"], bar["close"], signal["sl"], signal["tp"], signal["atr"], [0.25])
                if not reasons:
                    core.open(signal["side"], stop, target, close_s)
                out.append((close_s, signal["side"], round(signal["sl"], 6)))
        return out
    baseline = run()
    assert len(baseline) >= 5
    assert run(split=len(m5) // 2) == baseline


def test_engine_on_snapshots_matches_core_replay_and_alerts_once(tmp_path, market, risk):
    m5, _ = market
    first = 2600
    eng = engine(tmp_path, risk)
    boot = snapshot_at(market, first)
    assert eng.process(boot, now=boot.captured_at_utc_s + 1)["status"] == "bootstrapped"
    assert rows(eng) == []  # history never produces alerts
    for index in range(first + 1, len(m5)):
        snap = snapshot_at(market, index)
        status = eng.process(snap, now=snap.captured_at_utc_s + 1)
        assert status["status"] in ("watching", "alert")
        assert eng.process(snap, now=snap.captured_at_utc_s + 2)["new_bars"] == 0  # quote-only repoll
    expected = [x for x in replay(market, policy(), risk, first, len(m5) - 1) if x[0] > m5[first]["open_server_s"] + 300]
    found = [(json.loads(r["payload_json"])["bar_open_server_s"] + 300, r["side"], r["status"] != "rejected") for r in rows(eng)]
    assert found == expected
    passed = [r for r in rows(eng) if r["status"] == "alert"]
    assert len(passed) >= 3 and all(r["status"] in ("alert", "rejected") for r in rows(eng))
    alerts = events(eng, "trade_alert")
    assert len(alerts) == len(passed)
    payload = json.loads(passed[0]["payload_json"])
    assert alerts[0]["title"] == f"เข้า {payload['side']} ได้ · GOLD"
    assert "SL" in alerts[0]["message"] and "TP" in alerts[0]["message"]
    assert float(payload["net_reward_risk"]) >= 1.2 and payload["reason_codes"] == []
    assert payload["valid_until_utc_s"] == payload["bar_close_utc_s"] + 300
    assert payload["time_stop_utc_s"] == payload["bar_close_utc_s"] + 48 * 300


def test_mute_records_alert_without_event_and_late_snapshot_is_missed(tmp_path, market, risk):
    m5, _ = market
    first = 2600
    signals = [x for x in replay(market, policy(), risk, first, len(m5) - 1) if x[2] and x[0] > m5[first]["open_server_s"] + 300]
    target = next(i for i, b in enumerate(m5) if b["open_server_s"] + 300 == signals[0][0])
    second = next(i for i, b in enumerate(m5) if b["open_server_s"] + 300 == signals[1][0])
    eng = engine(tmp_path, risk)
    eng.process(snapshot_at(market, first), now=snapshot_at(market, first).captured_at_utc_s)
    feed(eng, market, first, target - 1)
    assert eng.set_muted(True) == {"muted": True, "changed": True}
    snap = snapshot_at(market, target)
    assert eng.process(snap, now=snap.captured_at_utc_s)["status"] == "alert"
    assert events(eng, "trade_alert") == [] and rows(eng)[-1]["status"] == "alert"
    assert events(eng, "alerts_muted")
    eng.set_muted(False)
    feed(eng, market, target, second - 1)
    late = snapshot_at(market, second + 1)  # the signal bar is no longer the latest closed bar
    eng.process(late, now=late.captured_at_utc_s)
    assert rows(eng)[-1]["status"] == "missed" and events(eng, "trade_alert") == []


def test_warmup_suppresses_notifications(tmp_path, market, risk):
    m5, _ = market
    eng = engine(tmp_path, risk, policy(warmup_h1_bars=5000, warmup_m5_bars=5000))
    first = 2600
    eng.process(snapshot_at(market, first), now=snapshot_at(market, first).captured_at_utc_s)
    feed(eng, market, first, len(m5) - 1, step=5)
    status = eng.status()
    assert status["status"] == "warming_up" and not status["warmup"]["ready"]
    assert rows(eng) and {r["status"] for r in rows(eng)} <= {"warmup", "rejected"}
    assert events(eng, "trade_alert") == []


def test_integrity_resets_archive_state_and_never_alert_history(tmp_path, market, risk):
    eng = engine(tmp_path, risk)
    first = 2600
    boot = snapshot_at(market, first)
    eng.process(boot, now=boot.captured_at_utc_s)
    data = snapshot_at(market, first - 1).model_dump()  # older bars delivered with a current clock
    data.update(captured_at_utc_s=boot.captured_at_utc_s, server_time_s=boot.server_time_s, quote=boot.quote.model_dump())
    older = Snapshot.model_validate(data)
    assert eng.process(older, now=boot.captured_at_utc_s)["status"] == "out_of_order_feed"
    data = snapshot_at(market, first + 1).model_dump()
    data["m5"][-5]["close"] = round(data["m5"][-5]["close"] + 0.01, 2)
    data["m5"][-5]["high"] = max(data["m5"][-5]["high"], data["m5"][-5]["close"])
    revised = Snapshot.model_validate(data)
    assert eng.process(revised, now=revised.captured_at_utc_s)["status"] == "bootstrapped"
    gap = snapshot_at(market, first + 400)
    assert eng.process(gap, now=gap.captured_at_utc_s)["status"] == "bootstrapped"
    with eng.db.connect() as conn:
        reasons = [r[0] for r in conn.execute("SELECT reason FROM alert_engine_resets ORDER BY id")]
    assert reasons == ["closed_history_revised", "missing_overlap"]
    assert len(events(eng, "engine_reset")) == 2 and rows(eng) == []


def test_source_freshness_and_h1_wait(tmp_path, market, risk):
    m5, _ = market
    eng = engine(tmp_path, risk)
    snap = snapshot_at(market, 2600)
    assert eng.process(snap, now=snap.captured_at_utc_s + 500)["status"] == "feed_stale"
    wrong = Snapshot.model_validate({**snap.model_dump(), "broker_symbol": "XAUUSD"})
    assert eng.process(wrong, now=snap.captured_at_utc_s)["status"] == "source_mismatch"
    eng.process(snap, now=snap.captured_at_utc_s)
    hour = next(i for i in range(2601, len(m5)) if (m5[i]["open_server_s"] + 300) % 3600 == 0)
    for index in range(2601, hour):
        s = snapshot_at(market, index)
        eng.process(s, now=s.captured_at_utc_s)
    data = snapshot_at(market, hour).model_dump()
    data["h1"] = data["h1"][:-1]  # H1 closing with this M5 bar not delivered yet
    early = Snapshot.model_validate(data)
    assert eng.process(early, now=early.captured_at_utc_s)["status"] == "waiting_for_h1"
    assert eng.status()["last_m5_server_s"] == m5[hour - 1]["open_server_s"]


def test_virtual_trade_blocks_until_tp_sl_or_time_stop():
    core = AlertCore(policy())
    core.open("BUY", __import__("decimal").Decimal("2390"), __import__("decimal").Decimal("2420"), 1000 * 300)
    bar = {"open_server_s": 1000 * 300, "open": 2400, "high": 2410, "low": 2395, "close": 2405, "spread_points": 25}
    assert core.manage(bar, POINT, DIGITS) is None
    assert core.manage({**bar, "high": 2420}, POINT, DIGITS)["exit_reason"] == "TP"
    core.open("SELL", __import__("decimal").Decimal("2410"), __import__("decimal").Decimal("2380"), 1000 * 300)
    assert core.manage({**bar, "high": 2409.80}, POINT, DIGITS)["exit_reason"] == "SL"  # ask = bid + 0.25
    core.open("SELL", __import__("decimal").Decimal("2410"), __import__("decimal").Decimal("2380"), 1000 * 300)
    assert core.manage({**bar, "open_server_s": 1000 * 300 + 48 * 300}, POINT, DIGITS)["exit_reason"] == "TIME"


def test_alert_text_mentions_limits_and_exit_time():
    title, message = alert_text({"digits": 2, "side": "SELL", "source_symbol": "GOLD", "entry": "2400.00",
                                 "sl": "2410.00", "tp": "2375.00", "entry_limit": "2398.00",
                                 "net_reward_risk": "2.4000", "valid_until_utc_s": 1_767_600_000,
                                 "time_stop_utc_s": 1_767_614_400})
    assert title == "เข้า SELL ได้ · GOLD"
    assert "Bid ≥ 2,398.00" in message and "SL 2,410.00" in message and "RR สุทธิ 2.40" in message


def test_alert_app_routes_auth_and_mute(tmp_path, market, monkeypatch):
    settings = Settings(webhook_token="w" * 43, api_token="r" * 43, database_path=tmp_path / "unused.db",
                        allowed_symbols=("XM:XAUUSD",))
    feed = BridgeSettings(token="m" * 43, database_path=tmp_path / "mt5.db")
    store = MarketStore(feed.database_path)
    store.initialize()
    snap = snapshot_at(market, 2600)
    monkeypatch.setattr("time.time", lambda: snap.captured_at_utc_s + 1)
    store.insert(snap)
    app = create_alert_app(settings, feed, policy(), database_path=tmp_path / "alerts.db")
    auth = {"Authorization": "Bearer " + "r" * 43}
    with TestClient(app) as client:
        assert client.get("/health").json()["mode"] == "alert_only"
        assert client.get("/xm/status").status_code == 401
        assert client.get("/dashboard/state", headers={"Authorization": "Bearer " + "w" * 43}).status_code == 401
        for _ in range(100):  # the poll task bootstraps from the stored MT5 snapshot
            state = client.get("/dashboard/state", headers=auth).json()
            if state["engine"]["status"] != "waiting_for_feed":
                break
            __import__("time").sleep(0.05)
        assert state["mode"] == "alert_only" and state["engine"]["status"] == "bootstrapped"
        assert state["workers"] == {"engine": True, "monitor": True} and state["feed"]["available"]
        assert state["strategy"]["rr"] == 2.5 and state["policy"]["min_net_reward_risk"] == "1.2"
        assert "account" not in state and "ai" not in state
        assert client.post("/dashboard/pause", headers=auth, json={"paused": True}).json() == {"paused": True, "changed": True}
        assert client.get("/dashboard/state", headers=auth).json()["muted"] is True
        assert client.post("/dashboard/pause", headers=auth, json={"paused": "yes"}).status_code == 422
        assert client.get("/alerts", headers=auth).json() == {"items": [], "next_after_id": 0}
