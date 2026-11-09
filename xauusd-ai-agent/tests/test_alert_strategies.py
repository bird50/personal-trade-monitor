"""Multi-strategy alert engine: checkpoints, selection, conflicts and the TPSL pending-order core."""
import json
import math
import random

import pytest

from app.alert_engine import AlertEngine, AlertPolicy, setup_text
from app.alert_strategies import TPSLCore, make_core
from app.config import ROOT
from app.database import Database
from app.mt5_bridge import Snapshot
from app.risk_manager import RiskPolicy
from backtest.data import resample

OFFSET = 7200
POINT, DIGITS = 0.01, 2
START = 1_767_571_200  # Monday 2026-01-05 00:00 server time
TARGET_DAY = 16  # Wednesday 2026-01-21


def policy(**changes):
    data = json.loads((ROOT / "config/alert-xm.json").read_text())
    data.update(warmup_h1_bars=200, warmup_m5_bars=200)
    data.update(changes)
    return AlertPolicy.model_validate(data)


@pytest.fixture(scope="module")
def risk():
    return RiskPolicy.load(ROOT / "config/risk-xm-paper.json")


def strip(rows):
    return [{k: r[k] for k in ("open_server_s", "open", "high", "low", "close", "tick_volume", "spread_points")} for r in rows]


@pytest.fixture(scope="module")
def volatile():
    """Random-walk M1 bars with alternating trends (synthetic, not market data)."""
    rng = random.Random(11)
    close, bars = 2400.0, []
    for minute in range(22 * 1440):
        drift = 0.12 if (minute // (3 * 1440)) % 2 == 0 else -0.12
        open_ = close
        close = max(100.0, open_ + rng.gauss(drift, 2.2))
        high, low = max(open_, close) + abs(rng.gauss(0, 0.8)), min(open_, close) - abs(rng.gauss(0, 0.8))
        bars.append({"open_server_s": START + minute * 60, "open": round(open_, 2), "high": round(high, 2),
                     "low": round(low, 2), "close": round(close, 2), "tick_volume": 30, "spread_points": 25})
        close = bars[-1]["close"]
    return strip(resample(bars, 300)), strip(resample(bars, 3600))


@pytest.fixture(scope="module")
def breakout():
    """Quiet M5 days (range too narrow for TPSL) except TARGET_DAY: a wide 03-08 range, then a rally."""
    bars, close = [], 2400.0
    for k in range(20 * 288):
        t = START + k * 300
        day, minute = divmod(k, 288)
        hour = minute * 5 / 60
        mid = 2400 + math.sin(k / 37 * 2 * math.pi)
        if day == TARGET_DAY and 3 <= hour < 8:
            mid = 2400 + 6 * math.sin(k / 37 * 2 * math.pi)
        elif day == TARGET_DAY and hour >= 8:
            mid = min(2400 + 0.5 * (minute - 96), 2445)
        elif day > TARGET_DAY:
            mid = 2445 + math.sin(k / 37 * 2 * math.pi)
        open_ = close
        close = round(mid, 2)
        bars.append({"open_server_s": t, "open": open_, "high": round(max(open_, close) + 1.5, 2),
                     "low": round(min(open_, close) - 1.5, 2), "close": close, "tick_volume": 30, "spread_points": 25})
    return bars, strip(resample(bars, 3600))


def snapshot_at(market, index, window=300):
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


def step_all(core, market, split=None):
    """Step a core over the whole market, restoring from its checkpoint at ``split``; collect signals."""
    m5, h1 = market
    out, h = [], 0
    for i, bar in enumerate(m5):
        if i == split:
            core = make_core(core.spec, json.loads(json.dumps(core.dump())))
        close_s = bar["open_server_s"] + 300
        new = []
        while h < len(h1) and h1[h]["open_server_s"] + 3600 <= close_s:
            new.append(h1[h])
            h += 1
        signals, exit_ = core.step(bar, new, POINT, DIGITS)
        for s in signals:
            if s["type"] == "entry":
                from decimal import Decimal
                core.open(s["side"], Decimal(str(round(s["sl"], 2))), Decimal(str(round(s["tp"], 2))), close_s)
            elif s["type"] == "setup":
                core.arm({k: {x: __import__("decimal").Decimal(str(round(v, 2))) for x, v in o.items()}
                          for k, o in s["orders"].items()}, alerted=True)
            out.append((close_s, s["type"], s.get("side"), round(s.get("sl") or 0, 4) if isinstance(s.get("sl"), float) else s.get("sl")))
        if exit_:
            out.append((close_s, "exit", exit_["side"], exit_["exit_reason"]))
    return out


@pytest.mark.parametrize("strategy_id", ["rsi2", "cci", "ha", "tpsl"])
def test_every_core_checkpoint_roundtrip_continues_identically(volatile, breakout, strategy_id):
    market = breakout if strategy_id == "tpsl" else volatile
    spec = policy().spec(strategy_id)
    baseline = step_all(make_core(spec), market)
    assert baseline, "the synthetic market should produce at least one signal"
    for split in (len(market[0]) // 3, len(market[0]) // 2 + 7):
        assert step_all(make_core(spec), market, split=split) == baseline


def test_tpsl_core_day_rules(breakout):
    spec = policy().spec("tpsl")
    events = step_all(make_core(spec), breakout)
    day_start = START + TARGET_DAY * 86400
    setups = [e for e in events if e[1] == "setup"]
    assert [e[0] for e in setups] == [day_start + 8 * 3600]  # quiet days are too narrow; one setup at 08:00
    kinds = [e[1:] for e in events if e[0] > day_start]
    assert kinds[1][:2] == ("fill", "BUY") and kinds[2][:2] == ("breakeven", "BUY")
    assert kinds[3][1:] == ("exit", "BUY", "TP")[1:] or kinds[3] == ("exit", "BUY", "TP")
    core = make_core(spec)
    for bar in breakout[0][: (TARGET_DAY + 1) * 288]:
        signals, _ = core.step(bar, [], POINT, DIGITS)
        if any(s["type"] == "setup" for s in signals):
            core.arm({}, alerted=False)  # nothing passed the Risk check
    assert core.orders is None and core.position is None and core.traded_day == TARGET_DAY + START // 86400


def test_tpsl_expires_at_close_time_and_never_trades_weekends():
    spec = policy().spec("tpsl")
    core = TPSLCore(spec)
    day = START // 86400 + 2  # Wednesday
    def bar(h, m, mid, width=3.0):
        return {"open_server_s": day * 86400 + h * 3600 + m * 60, "open": mid, "high": mid + width, "low": mid - width,
                "close": mid, "spread_points": 25}
    for i in range(59):  # 03:00-07:50; the 07:55 bar closes the range at 08:00
        core.step(bar(3 + i // 12, i % 12 * 5, 2400 + (6 if i % 2 else -6) * (i % 3 == 0), 3.0), [], POINT, DIGITS)
    signals, _ = core.step(bar(7, 55, 2400), [], POINT, DIGITS)
    setup = next(s for s in signals if s["type"] == "setup")
    assert setup["orders"]["BUY"]["level"] == pytest.approx(core.range_high + 0.2)
    core.arm({"BUY": {"level": 1, "sl": 0, "tp": 2}}, alerted=True)
    signals, exit_ = core.step(bar(21, 0, 2400), [], POINT, DIGITS)
    assert signals == [{"type": "expire", "had_orders": True, "had_position": False,
                        "at_server_s": day * 86400 + 21 * 3600}] and exit_ is None and core.orders is None
    saturday = TPSLCore(spec)
    sat = START // 86400 + 5
    for i in range(61):
        b = {"open_server_s": sat * 86400 + 3 * 3600 + i * 300, "open": 2400, "high": 2410, "low": 2390,
             "close": 2400, "spread_points": 25}
        signals, _ = saturday.step(b, [], POINT, DIGITS)
        assert not any(s["type"] == "setup" for s in signals)


def run_engine(tmp_path, risk, market, first, last, selection, pol=None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    eng = AlertEngine(Database(tmp_path / "alerts.db"), pol or policy(), risk)
    eng.set_selection(selection)
    boot = snapshot_at(market, first)
    eng.process(boot, now=boot.captured_at_utc_s + 1)
    for index in range(first + 1, last + 1):
        snap = snapshot_at(market, index)
        eng.process(snap, now=snap.captured_at_utc_s + 1)
    return eng


def rows(eng):
    with eng.db.connect() as conn:
        return [dict(r) | {"payload": json.loads(r["payload_json"])} for r in conn.execute("SELECT * FROM trade_alerts ORDER BY id")]


def events(eng, kind):
    with eng.db.connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM control_events WHERE kind=? ORDER BY id", (kind,))]


def test_engine_tpsl_setup_fill_breakeven_and_selection(tmp_path, risk, breakout):
    m5 = breakout[0]
    first, last = (TARGET_DAY - 1) * 288, (TARGET_DAY + 1) * 288 - 1
    eng = run_engine(tmp_path, risk, breakout, first, last, ["tpsl"])
    found = rows(eng)
    assert {r["payload"]["strategy_id"] for r in found} == {"tpsl"}  # RSI(2) is not selected
    assert sorted(r["side"] for r in found) == ["BUY", "SELL"] and {r["status"] for r in found} == {"alert"}
    buy = next(r["payload"] for r in found if r["side"] == "BUY")
    assert buy["order_type"] == "buy_stop" and buy["entry_limit"] is None and buy["breakeven_r"] == 1.5
    close_utc = START + TARGET_DAY * 86400 + 8 * 3600 - OFFSET
    assert buy["bar_close_utc_s"] == close_utc and buy["cancel_at_utc_s"] == close_utc + 13 * 3600
    alerts = events(eng, "trade_alert")
    assert len(alerts) == 1 and alerts[0]["title"] == "ตั้ง Buy Stop / Sell Stop · GOLD · TPSL London breakout"
    assert "ฝั่งที่ถูกชนก่อนให้ยกเลิกอีกฝั่ง" in alerts[0]["message"] and "+1.5R" in alerts[0]["message"]
    updates = [e["title"] for e in events(eng, "trade_update")]
    assert updates == ["TPSL London breakout: Buy Stop ถูกชน · GOLD", "TPSL London breakout: เลื่อน SL เป็นทุน · GOLD"]
    status = eng.status()
    assert status["selected"] == ["tpsl"] and status["strategies"]["tpsl"]["enabled"] is True
    assert status["strategies"]["tpsl"]["last_virtual_exit"]["exit_reason"] == "TP"
    assert status["strategies"]["rsi2"]["enabled"] is False and "summary" in status["strategies"]["rsi2"]
    # Switching the selection keeps the checkpoint (no reset, no warm-up).
    assert eng.set_selection(["rsi2", "cci"]) == {"enabled": ["rsi2", "cci"], "changed": True}
    assert eng.set_selection(["cci", "rsi2"])["changed"] is False
    snap = snapshot_at(breakout, last + 1)
    assert eng.process(snap, now=snap.captured_at_utc_s + 1)["status"] in ("watching", "alert")
    with eng.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM alert_engine_resets").fetchone()[0] == 0
    assert events(eng, "strategies_changed")[-1]["message"] == "แจ้งเตือนจาก: RSI(2) pullback, CCI + Williams %R"
    with pytest.raises(ValueError):
        eng.set_selection(["nope"])
    with pytest.raises(ValueError):
        eng.set_selection([])


def test_engine_mixed_strategies_and_conflict_skip(tmp_path, risk, volatile):
    m5 = volatile[0]
    first = 2600
    mixed = run_engine(tmp_path / "warn", risk, volatile, first, len(m5) - 1, ["rsi2", "cci", "ha"])
    found = rows(mixed)
    by = {}
    for r in found:
        by.setdefault(r["payload"]["strategy_id"], []).append(r)
    assert set(by) >= {"rsi2"} and len(by) >= 2, by.keys()
    # Each strategy's rows equal what that strategy produces alone.
    alone = run_engine(tmp_path / "alone", risk, volatile, first, len(m5) - 1, ["rsi2"])
    assert [(r["alert_key"], r["status"]) for r in rows(alone)] == [(r["alert_key"], r["status"]) for r in by["rsi2"]]
    skip = run_engine(tmp_path / "skip", risk, volatile, first, len(m5) - 1, ["rsi2", "cci", "ha"],
                      pol=policy(conflict="skip"))
    skipped = [r for r in rows(skip) if "other_strategy_open" in r["payload"]["reason_codes"]]
    opened = [r for r in rows(skip) if r["status"] == "alert"]
    assert opened and len(rows(skip)) == len(opened) + len([r for r in rows(skip) if r["status"] == "rejected"])
    assert all(r["status"] == "rejected" for r in skipped)
    assert skipped, "overlapping hypothetical trades should be skipped in conflict=skip"
    assert len(opened) < len([r for r in found if r["status"] == "alert"])


def test_setup_text_lists_orders_and_rules():
    common = {"digits": 2, "source_symbol": "GOLD", "strategy_label": "TPSL", "valid_until_utc_s": 1_767_600_000,
              "cancel_at_utc_s": 1_767_646_800, "breakeven_r": 1.5}
    title, message = setup_text([
        {**common, "side": "BUY", "entry": "2406.20", "sl": "2394.00", "tp": "2436.70", "net_reward_risk": "2.3"},
        {**common, "side": "SELL", "entry": "2393.80", "sl": "2406.00", "tp": "2363.30", "net_reward_risk": "2.3"}],
        conflicts=[("RSI(2) pullback", "BUY")])
    assert title == "ตั้ง Buy Stop / Sell Stop · GOLD · TPSL"
    assert "Buy Stop 2,406.20 · SL 2,394.00 · TP 2,436.70" in message and "เลื่อน SL เป็นทุนเมื่อได้ +1.5R" in message
    assert "⚠️ ขัดกับไม้สมมติที่ยังเปิดอยู่: RSI(2) pullback BUY" in message
