import asyncio
import copy
import json
import math
import time
from dataclasses import replace
from decimal import Decimal

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.ai_analyzer import AIAnalyzer
from app.config import ROOT, Settings
from app.database import Database
from app.models import Signal
from app.mt5_bridge import Snapshot
from app.risk_manager import RiskManager, RiskPolicy
from app.worker import AnalysisWorker
from app.xm_engine import XMEngine, choose_side, levels, smooth, update_m5, update_pivots
from test_receiver import payload
from test_structure import with_structure


@pytest.fixture
def sample():
    # Deterministic instant: M5 ends on an hour boundary to test H1 [1].
    now = int(time.time() // 3600) * 3600 + 10
    clock = now + 7200
    def bars(period):
        end = clock // period * period
        return [{"open_server_s": end - (300-i) * period,
                 "open": 2400 + i * 0.1, "high": 2401 + i * 0.1,
                 "low": 2399 + i * 0.1, "close": 2400 + i * 0.1,
                 "tick_volume": 10, "spread_points": 53} for i in range(300)]
    return Snapshot.model_validate({"schema_version": 1, "source": "mt5", "account_mode": "demo",
        "server": "XMGlobal-MT5 7", "broker_symbol": "GOLD", "captured_at_utc_s": now,
        "server_time_s": clock, "observed_utc_offset_s": 7200, "digits": 2,
        "point": 0.01, "tick_size": 0.01,
        "quote": {"time_server_ms": clock*1000, "bid": 2430, "ask": 2430.53},
        "m5": bars(300), "h1": bars(3600)})


def advance(sample, count=1):
    data = sample.model_dump()
    for _ in range(count):
        bar = copy.deepcopy(data["m5"][-1])
        bar["open_server_s"] += 300
        for key in ("open", "high", "low", "close"):
            bar[key] = round(bar[key] + 0.1, 2)
        data["m5"].append(bar)
        data["m5"] = data["m5"][-300:]
    data["captured_at_utc_s"] += count*300
    data["server_time_s"] += count*300
    data["quote"]["time_server_ms"] += count*300000
    while data["h1"][-1]["open_server_s"] + 7200 <= data["server_time_s"]:
        hbar = copy.deepcopy(data["h1"][-1])
        hbar["open_server_s"] += 3600
        for key in ("open","high","low","close"):
            hbar[key] = round(hbar[key]+0.1,2)
        data["h1"].append(hbar)
        data["h1"] = data["h1"][-300:]
    return Snapshot.model_validate(data)


@pytest.fixture
def db(tmp_path):
    db = Database(tmp_path / "xm.db")
    db.initialize()
    return db


def raw_state(db):
    with db.connect() as conn:
        return json.loads(conn.execute("SELECT state_json FROM xm_engine").fetchone()[0])


def test_indicator_reference_values_and_seed():
    state = {}
    assert smooth(state, "ema", 10.0, 3, ema=True) == 10
    assert smooth(state, "ema", 14.0, 3, ema=True) == 12
    assert smooth(state, "rma", 1.0, 3) is None
    assert smooth(state, "rma", 2.0, 3) is None
    assert smooth(state, "rma", 3.0, 3) == 2
    assert smooth(state, "rma", 5.0, 3) == 3
    state = {}
    for i in range(60):
        result = update_m5(state, {"open_server_s": i*300, "open": 100+i,
            "high": 101+i, "low": 99+i, "close": 100+i})
    assert result["atr"] == pytest.approx(2)
    assert result["rsi"] == 100
    assert result["plus_di"] == pytest.approx(50)
    assert result["minus_di"] == 0
    assert result["adx"] == pytest.approx(100)


@pytest.mark.parametrize("side,rsi,adx,expected", [
    ("BUY", 50, 20, "BUY"), ("BUY", 70, 30, None), ("BUY", 49.99, 30, None),
    ("BUY", 60, 19.99, None), ("SELL", 50, 20, "SELL"),
    ("SELL", 30, 30, None), ("SELL", 50.01, 30, None),
])
def test_entry_thresholds(side, rsi, adx, expected):
    buy = side == "BUY"
    metrics = {"rsi": rsi, "adx": adx, "atr": 2,
               "cross_up": buy, "cross_down": not buy,
               "plus_di": 30 if buy else 10, "minus_di": 10 if buy else 30}
    h1 = {"close": 103 if buy else 97, "ema_fast": 102 if buy else 98, "ema_slow": 100}
    # xm-0.1 RSI band, kept for backtest parity.
    assert choose_side(metrics, h1, rsi_band=True) == expected
    # xm-0.2 ignores RSI entirely (even a missing value); ADX, DI, EMA cross and H1 trend remain.
    assert choose_side(metrics, h1) == (None if adx < 20 else side)
    assert choose_side(metrics | {"rsi": None}, h1) == (None if adx < 20 else side)
    assert choose_side(metrics | {"plus_di": metrics["minus_di"], "minus_di": metrics["plus_di"]}, h1) is None


def test_pivot_waits_for_right_bars_and_never_reactivates():
    state = {}
    highs = [101, 102, 103, 110, 103, 102, 101, 112, 105]
    for i, high in enumerate(highs):
        close = 111 if i == 7 else 100
        structure = update_pivots(state, {"open_server_s": 1800000000+i*300,
                                         "high": high, "low": 99, "close": close}, i)
        if i < 6:
            assert structure["recent_high"] is None
        if i == 6:
            assert structure["recent_high"]["price"] == 110
            assert structure["recent_high"]["age_bars"] == 0
        if i >= 7:
            assert structure["recent_high"]["active"] is False


def test_tick_rounding():
    assert levels("BUY", 2500, 3.003, Decimal("0.01")) == (2500, 2495.5, 2509)
    assert levels("SELL", 2500, 3.003, Decimal("0.01")) == (2500, 2504.5, 2491)


def test_bootstrap_h1_alignment_and_restart(db, sample):
    engine = XMEngine(db)
    first = engine.process(sample, sample.captured_at_utc_s)
    assert first["status"] == "bootstrapped_no_historical_signals"
    assert db.list_signals(0, 100) == []
    state = raw_state(db)
    latest_h1_close = (sample.h1[-1].open_server_s+3600)*1000
    # M5 ending exactly on the hour must use the preceding H1, not H1 just closed.
    assert state["h1"]["bar_close_ms"] == latest_h1_close - 3600000
    later = advance(sample)
    engine = XMEngine(db)
    engine.process(later, later.captured_at_utc_s)
    state = raw_state(db)
    assert state["h1"]["bar_close_ms"] == latest_h1_close
    count = state["m5_indicators"]["ema20"]["count"]
    engine.process(later, later.captured_at_utc_s)
    assert raw_state(db)["m5_indicators"]["ema20"]["count"] == count
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM xm_bar_evaluations").fetchone()[0] == 1


def test_split_and_uninterrupted_updates_are_identical(tmp_path, sample):
    states = []
    for mode in ("split", "together"):
        db = Database(tmp_path / (mode+".db")); db.initialize()
        engine = XMEngine(db); engine.process(sample, sample.captured_at_utc_s)
        if mode == "split":
            one = advance(sample)
            engine.process(one, one.captured_at_utc_s)
            engine = XMEngine(db)
        two = advance(sample, 2)
        engine.process(two, two.captured_at_utc_s)
        states.append(raw_state(db))
    for field in ("m5_indicators", "h1_indicators", "pivots", "candidate_lock", "index"):
        assert states[0][field] == states[1][field]


@pytest.mark.parametrize("change,expected", [
    ("revision", "closed_history_revised_reset_required"),
    ("offset", "version_or_offset_changed_reset_required"),
    ("stale", "feed_stale"), ("gap", "missing_overlap_reset_required"),
    ("source", "source_mismatch"), ("tick", "tick_policy_mismatch"),
])
def test_bad_feed_does_not_advance_checkpoint(db, sample, change, expected):
    engine = XMEngine(db); engine.process(sample, sample.captured_at_utc_s)
    before = raw_state(db)
    data = sample.model_dump()
    now = sample.captured_at_utc_s
    if change == "revision":
        data["m5"][-1]["tick_volume"] += 1
    elif change == "offset":
        data["observed_utc_offset_s"] += 3600
        data["captured_at_utc_s"] -= 3600
        now -= 3600
    elif change == "stale":
        now += 121
    elif change == "gap":
        data = advance(sample, 301).model_dump()
        now = data["captured_at_utc_s"]
    elif change == "source":
        data["broker_symbol"] = "OTHER"
    else:
        data["tick_size"] = 0.1
    result = engine.process(Snapshot.model_validate(data), now)
    assert result["status"] == expected
    assert raw_state(db) == before
    assert db.list_signals(0,100) == []


def queue_candidate(db, sample, monkeypatch):
    engine = XMEngine(db); engine.process(sample, sample.captured_at_utc_s)
    # Isolate dispatch from the independently tested threshold rules.
    monkeypatch.setattr("app.xm_engine.choose_side", lambda metrics, h1: "BUY")
    later = advance(sample)
    result = engine.process(later, later.captured_at_utc_s)
    assert result["status"] == "signal_queued"
    return engine, later


def test_signal_provenance_queue_audit_and_idempotence(db, sample, monkeypatch):
    engine, later = queue_candidate(db, sample, monkeypatch)
    rows = db.list_signals(0,100)
    assert len(rows) == 1
    payload = rows[0]["payload"]
    assert payload["symbol"] == "XM:XAUUSD"
    assert payload["source"]["broker_symbol"] == "GOLD"
    assert payload["bar_close_ms"] == (later.m5[-1].open_server_s+300-7200)*1000
    assert payload["h1"]["bar_close_ms"] <= payload["source"]["bar_close_server_ms"]-300000
    assert db.get_analysis(rows[0]["id"])["status"] == "pending"
    engine.process(later, later.captured_at_utc_s)
    assert len(db.list_signals(0,100)) == 1
    with db.connect() as conn:
        audit = conn.execute("SELECT snapshot_json FROM xm_candidate_inputs").fetchone()
    assert json.loads(audit[0]) == later.model_dump()
    invalid = copy.deepcopy(payload); invalid["symbol"] = "OANDA:XAUUSD"
    with pytest.raises(ValidationError):
        Signal.model_validate(invalid)


def test_dispatch_and_checkpoint_roll_back_together(db, sample, monkeypatch):
    engine = XMEngine(db); engine.process(sample, sample.captured_at_utc_s)
    before = raw_state(db)
    original = db.insert_in_transaction
    def fail(conn, signal):
        original(conn, signal)
        raise RuntimeError("simulated crash before checkpoint")
    monkeypatch.setattr(db, "insert_in_transaction", fail)
    monkeypatch.setattr("app.xm_engine.choose_side", lambda m, h: "BUY")
    later = advance(sample)
    with pytest.raises(RuntimeError):
        engine.process(later, later.captured_at_utc_s)
    assert raw_state(db) == before
    assert db.list_signals(0,100) == []
    monkeypatch.setattr(db, "insert_in_transaction", original)
    engine.process(later, later.captured_at_utc_s)
    assert len(db.list_signals(0,100)) == 1


def test_ai_worker_consumes_xm_candidate_and_applies_risk(db, sample, monkeypatch):
    engine, later = queue_candidate(db, sample, monkeypatch)
    signal = db.list_signals(0,100)[0]
    config = Settings(webhook_token="w"*43, api_token="r"*43, database_path=db.path,
        risk_policy_path=ROOT/"config/risk-xm-paper.json", ai_enabled=True)
    def respond(request):
        content = json.loads(json.loads(request.content)["messages"][1]["content"])
        assert content["signal"]["source"]["provider"] == "xm_mt5_demo"
        result = {k: content["signal"][k] for k in ("entry", "sl", "tp", "risk_reward")}
        result.update(decision="WAIT", reasons=["Insufficient confirmed pivots"])
        return httpx.Response(200, json={"choices":[{"finish_reason":"stop", "message":{"content":json.dumps(result)}}]})
    monkeypatch.setattr("time.time", lambda: later.captured_at_utc_s)
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            worker = AnalysisWorker(db, AIAnalyzer(config,client),config)
            assert await worker.process_once()
    asyncio.run(run())
    analysis = db.get_analysis(signal["id"])
    assert analysis["status"] == "completed"
    assert analysis["effective_decision"] == "WAIT"
    risk = db.get_risk(signal["id"])
    assert risk["status"] == "rejected"  # Evidence is intentionally incomplete.
    assert Decimal(risk["calculations"]["spread_price_used"]) >= Decimal("0.60")


def test_risk_rejects_stale_quote_and_uses_wider_spread(db, sample, monkeypatch):
    engine, later = queue_candidate(db, sample, monkeypatch)
    signal = Signal.model_validate(db.list_signals(0,100)[0]["payload"])
    signal.source.ask = signal.source.bid + 2
    manager = RiskManager(RiskPolicy.load(ROOT/"config/risk-xm-paper.json"))
    result = {k:getattr(signal,k) for k in ("entry","sl","tp","risk_reward")}
    report = manager.evaluate(signal, result|{"decision":"WAIT"}, now=later.captured_at_utc_s+121)
    assert "stale_source_quote" in report["reason_codes"]
    assert Decimal(report["calculations"]["spread_price_used"]) == 2
    assert report["expires_at"] <= later.captured_at_utc_s+120


def test_feed_recovery_restores_last_evaluation(db, sample):
    engine = XMEngine(db); engine.process(sample,sample.captured_at_utc_s)
    engine.note("feed_stale")
    assert engine.process(sample,sample.captured_at_utc_s)["status"] == "bootstrapped_no_historical_signals"


def test_candidate_lock_persists_until_sl_or_tp_touched(db, sample, monkeypatch):
    engine, later = queue_candidate(db,sample,monkeypatch)
    second = advance(later)
    XMEngine(db).process(second,second.captured_at_utc_s)
    assert len(db.list_signals(0,100)) == 1
    assert raw_state(db)["candidate_lock"] is not None
    third = advance(second).model_dump()
    third["m5"][-1]["low"] = 2400
    third = Snapshot.model_validate(third)
    XMEngine(db).process(third,third.captured_at_utc_s)
    assert len(db.list_signals(0,100)) == 2


def test_stale_h1_cache_cannot_trigger_candidate(db, sample, monkeypatch):
    data = sample.model_dump()
    data["h1"] = data["h1"][:-3]
    data = Snapshot.model_validate(data)
    engine = XMEngine(db); engine.process(data,data.captured_at_utc_s)
    monkeypatch.setattr("app.xm_engine.choose_side",lambda m,h:"BUY")
    later_raw = advance(data).model_dump()
    later_raw["h1"] = [bar.model_dump() for bar in data.h1]
    later = Snapshot.model_validate(later_raw)
    result = engine.process(later,later.captured_at_utc_s)
    assert result["h1_ready"] is False
    assert result["candidate_side"] is None
    assert db.list_signals(0,100) == []


def test_old_signal_payload_hash_remains_compatible(tmp_path):
    raw = json.loads((ROOT/'examples/signal.json').read_text())
    signal = Signal.model_validate(raw)
    db = Database(tmp_path/'legacy.db'); db.initialize()
    # The original schema predates the optional top-level source field.
    record_id, _ = db.insert(signal)
    assert db.get_signal(record_id)["payload"].keys() == raw.keys()
    assert db.insert(Signal.model_validate(raw)) == (record_id,False)


@pytest.mark.parametrize("direction,side", [(1,"BUY"),(-1,"SELL")])
def test_natural_ohlc_cross_creates_candidate_without_mocking_rules(db,sample,direction,side):
    def price(i):
        return round(2500+direction*(0.01*i+math.sin(i*math.pi/12)),2)
    def candle(original,i):
        previous,close=price(i-1),price(i)
        return {**original,"open":previous,"close":close,
                "high":round(max(previous,close)+0.2,2),"low":round(min(previous,close)-0.2,2)}
    data=sample.model_dump()
    data["m5"]=[candle(b,i) for i,b in enumerate(data["m5"])]
    if direction<0:
        for i,b in enumerate(data["h1"]):
            close=2600-i*0.1
            b.update(open=close,close=close,high=close+1,low=close-1)
    current=Snapshot.model_validate(data)
    engine=XMEngine(db);engine.process(current,current.captured_at_utc_s)
    # The seed has an active candidate lock. Its TP is touched in the next wave;
    # the subsequent crossover (index 335) is the first eligible fresh candidate.
    for i in range(300,336):
        data=advance(current).model_dump()
        data["m5"][-1]=candle(data["m5"][-1],i)
        if direction<0:
            # Maintain the same causal H1 downtrend when a new hour appears.
            old={b.open_server_s for b in current.h1}
            for b in data["h1"]:
                if b["open_server_s"] not in old:
                    close=current.h1[-1].close-0.1
                    b.update(open=close,close=close,high=close+1,low=close-1)
        data["quote"].update(bid=price(i),ask=round(price(i)+0.53,2))
        current=Snapshot.model_validate(data)
        engine.process(current,current.captured_at_utc_s)
    signals=db.list_signals(0,100)
    assert len(signals)==1
    payload=signals[0]["payload"]
    assert payload["side"]==side
    assert payload["m5"]["adx"]>=20
    assert 30<payload["m5"]["rsi"]<70


def complete_xm(payload,side='BUY'):
    data=with_structure(payload,side)
    data.update(schema_version=3,strategy_version="xm-0.1",symbol="XM:XAUUSD")
    data["signal_id"]=f"XM:XAUUSD:5:{data['bar_close_ms']}:{side}:xm-0.1"
    shift=7200000
    data["h1"]["bar_close_ms"]+=shift
    for name in ("recent_high","previous_high","recent_low","previous_low"):
        point=data["context"]["market_structure"][name]
        point["pivot_close_ms"]+=shift
        point["confirmed_at_ms"]+=shift
    snapshot=data["bar_close_ms"]//1000+1
    data["source"]={"provider":"xm_mt5_demo","server":"XMGlobal-MT5 7","broker_symbol":"GOLD",
        "engine_version":"xm-0.1","context_time_basis":"broker_server_wall_clock",
        "bar_close_server_ms":data["bar_close_ms"]+shift,"snapshot_utc_s":snapshot,
        "quote_server_ms":(snapshot+7200)*1000,"observed_utc_offset_s":7200,
        "bid":2500,"ask":2500.53,"price_tick":0.01,"input_sha256":"a"*64}
    return data


def test_complete_xm_evidence_can_pass_ai_and_risk(db,payload,monkeypatch):
    data=complete_xm(payload)
    signal=Signal.model_validate(data)
    record_id,_=db.insert(signal)
    result={k:data[k] for k in ("entry","sl","tp","risk_reward")}|{"decision":"BUY","reasons":["Aligned confirmed pivots"]}
    config=Settings(webhook_token="w"*43,api_token="r"*43,database_path=db.path,
                    risk_policy_path=ROOT/"config/risk-xm-paper.json")
    monkeypatch.setattr("time.time",lambda:data["source"]["snapshot_utc_s"]+1)
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req:httpx.Response(200,json={"choices":[{"finish_reason":"stop","message":{"content":json.dumps(result)}}]}))) as client:
            assert await AnalysisWorker(db,AIAnalyzer(config,client),config).process_once()
    asyncio.run(run())
    analysis=db.get_analysis(record_id)
    assert analysis["status"]=="completed"
    assert analysis["effective_decision"]=="BUY"
    assert analysis["confirmed_quantity"] is not None
    assert db.get_risk(record_id)["status"]=="passed"


def test_xm_risk_pins_source_and_expires_after_quote(payload):
    data=complete_xm(payload)
    manager=RiskManager(RiskPolicy.load(ROOT/"config/risk-xm-paper.json"))
    result={k:data[k] for k in ("entry","sl","tp","risk_reward")}|{"decision":"BUY"}
    now=data["source"]["snapshot_utc_s"]
    data["source"]["quote_server_ms"]-=90000
    report=manager.evaluate(Signal.model_validate(data),result,now=now)
    assert report["status"]=="passed"
    assert report["expires_at"]==now+30
    data["source"]["server"]="OTHER"
    report=manager.evaluate(Signal.model_validate(data),result,now=now)
    assert "risk_source_not_allowed" in report["reason_codes"]


def test_xm_policy_cannot_omit_source_and_legacy_policy_hash_stable():
    import hashlib
    raw=json.loads((ROOT/'config/risk-paper.json').read_text())
    legacy=RiskManager(RiskPolicy.model_validate(raw))
    assert legacy.policy_hash==hashlib.sha256(json.dumps(raw,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    raw['allowed_symbols']=['XM:XAUUSD']
    with pytest.raises(ValidationError):
        RiskPolicy.model_validate(raw)


def test_xm_service_reads_local_feed_and_protects_status(tmp_path,sample,monkeypatch):
    from app.mt5_bridge import BridgeSettings,MarketStore
    from app.xm_main import create_xm_app
    bridge=BridgeSettings(token="m"*43,database_path=tmp_path/'feed.db')
    store=MarketStore(bridge.database_path);store.initialize();store.insert(sample)
    config=Settings(webhook_token="w"*43,api_token="r"*43,database_path=tmp_path/'service.db',
                    allowed_symbols=("XM:XAUUSD",),risk_policy_path=ROOT/'config/risk-xm-paper.json')
    monkeypatch.setattr("time.time",lambda:sample.captured_at_utc_s)
    with TestClient(create_xm_app(config,bridge)) as client:
        assert client.get('/xm/status').status_code==401
        headers={"Authorization":"Bearer "+config.api_token}
        deadline=time.monotonic()+3
        while time.monotonic()<deadline:
            status=client.get('/xm/status',headers=headers).json()
            if status['engine']['status']=='bootstrapped_no_historical_signals':
                break
            time.sleep(0.01)
        else:
            pytest.fail('XM engine did not consume local feed')
        assert status['engine_worker']=='running'
        assert client.get('/signals',headers=headers).json()['items']==[]
    with pytest.raises(ValueError,match='policy source'):
        with TestClient(create_xm_app(config,replace(bridge,server='OTHER'))):
            pass


def test_xm01_checkpoint_upgrades_in_place_and_unknown_version_still_blocks(db, sample):
    engine = XMEngine(db); engine.process(sample, sample.captured_at_utc_s)
    with db.connect() as conn:
        state = json.loads(conn.execute("SELECT state_json FROM xm_engine").fetchone()[0])
        before = {k: v for k, v in state.items() if k != "version"}
        conn.execute("UPDATE xm_engine SET state_json=?", (json.dumps(state | {"version": "xm-0.1"}),))
    later = advance(sample)
    result = engine.process(later, later.captured_at_utc_s)
    assert result["status"] == "evaluated" and result["engine_version"] == "xm-0.2"
    upgraded = raw_state(db)
    assert upgraded["version"] == "xm-0.2" and upgraded["upgraded_from"] == "xm-0.1"
    assert upgraded["index"] == before["index"] + 1  # indicator state continued, not rebuilt
    with db.connect() as conn:
        conn.execute("UPDATE xm_engine SET state_json=?", (json.dumps(upgraded | {"version": "xm-0.0"}),))
    blocked = engine.process(advance(later), advance(later).captured_at_utc_s)
    assert blocked["status"] == "version_or_offset_changed_reset_required"


def test_xm02_signal_versions_must_agree(payload):
    data = complete_xm(payload)
    data.update(strategy_version="xm-0.2", signal_id=data["signal_id"].replace(":xm-0.1", ":xm-0.2"))
    data["source"]["engine_version"] = "xm-0.2"
    assert Signal.model_validate(data).strategy_version == "xm-0.2"
    with pytest.raises(ValidationError):
        Signal.model_validate(data | {"source": data["source"] | {"engine_version": "xm-0.1"}})
    with pytest.raises(ValidationError):
        Signal.model_validate(data | {"signal_id": data["signal_id"].replace(":xm-0.2", ":xm-0.1")})


def test_xm_policy_rr_and_structure_rules(payload):
    raw = json.loads((ROOT / "config/risk-xm-paper.json").read_text())
    assert raw["min_net_reward_risk"] == "1.2" and raw["require_market_structure"] is False
    data = complete_xm(payload)
    result = {k: data[k] for k in ("entry", "sl", "tp", "risk_reward")} | {"decision": "BUY"}
    now = data["source"]["snapshot_utc_s"]
    # Break the pivot evidence: blocked only when the policy requires structure.
    data["context"]["market_structure"]["recent_low"]["active"] = False
    signal = Signal.model_validate(data)
    strict = RiskManager(RiskPolicy.model_validate({k: v for k, v in raw.items() if k != "require_market_structure"}))
    assert "broken_pivot_level" in strict.evaluate(signal, result, now=now)["reason_codes"]
    relaxed = RiskManager(RiskPolicy.model_validate(raw)).evaluate(signal, result, now=now)
    assert relaxed["status"] == "passed" and relaxed["waived_reason_codes"] == []
    assert relaxed["policy"]["require_market_structure"] is False
    # Net RR is now accepted from 1.2 instead of 1.5.
    assert RiskPolicy.model_validate(raw).min_net_reward_risk == Decimal("1.2")
    with pytest.raises(ValidationError):
        RiskPolicy.model_validate(raw | {"require_market_structure": "no"})
