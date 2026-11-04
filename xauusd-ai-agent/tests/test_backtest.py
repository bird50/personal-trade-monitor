"""Backtest harness on synthetic data: costs, SL-first, causality, resampling, xm-0.1 parity."""
import json
import random
import sys
from pathlib import Path

import pytest

from app.config import ROOT
from app.database import Database
from app.mt5_bridge import Snapshot
from app.paper_trading import PaperPolicy
from app.risk_manager import RiskPolicy
from app.xm_engine import XMEngine, update_m5
from backtest import indicators as ind
from backtest import metrics
from backtest.data import History, load_m1, parse_meta, resample, synthetic_m1, write_csv, SYNTHETIC_META
from backtest.engine import Backtest
from backtest.strategies import load
from backtest.strategies.base import Strategy
from backtest.strategies.baseline_xm01 import BaselineXM01

sys.path.insert(0, str(ROOT / "scripts"))
import run_backtest  # noqa: E402

T0 = 1_767_571_200 + 3600  # Monday 2026-01-05 01:00 server time
POLICY = RiskPolicy.model_validate(json.loads((ROOT / "config/risk-xm-paper.json").read_text()) | {
    "spread_price": "0.60", "slippage_price_per_side": "0.05", "commission_usd_per_unit_round_trip": "0.10",
    "equity_usd": "10000", "risk_percent": "0.5", "max_notional_equity_multiple": "1",
    "quantity_step": "0.01", "price_tick": "0.01", "max_signal_age_seconds": 300})
PAPER = PaperPolicy.model_validate(json.loads((ROOT / "config/paper-xm.json").read_text()) | {"initial_balance_usd": "10000"})


def m1(t, o, h=None, l=None, c=None, spread=30, volume=10):
    h = max(o, c or o) if h is None else h
    l = min(o, c or o) if l is None else l
    return {"open_server_s": t, "open": o, "high": h, "low": l, "close": o if c is None else c,
            "tick_volume": volume, "spread_points": spread}


def flat(start, minutes, price=2400.0, spread=30):
    return [m1(start + i * 60, price, spread=spread) for i in range(minutes)]


class Scripted(Strategy):
    name = "scripted"
    default_params = {"signals": {}, "exits": {}}

    def on_bar(self, ctx):
        signal = self.params["signals"].get(ctx.time_s)
        return None if signal is None else self.signal(**signal)


def run(bars, signals, **kwargs):
    history = History(bars, point=0.01, digits=2)
    engine = Backtest(history, Scripted({"signals": signals}), POLICY, PAPER, **kwargs)
    return engine.run()


def test_buy_fills_at_ask_and_exits_on_bid_with_costs():
    bars = flat(T0, 5) + [m1(T0 + 300, 2400.0), m1(T0 + 360, 2400.5, 2402.2, 2400.4, 2401.0)]
    result = run(bars, {T0 + 300: {"side": "BUY", "sl": 2398.0, "tp": 2402.0}})
    trade = result["trades"][0]
    # Ask 2400.30 + 0.05 slippage; TP on bid 2402.00 - 0.05 slippage.
    assert trade["entry"] == 2400.35 and trade["exit"] == 2401.95 and trade["exit_reason"] == "TP"
    assert trade["entry_time_s"] == T0 + 300
    # Risk cap: notional 10000 / 2400 → 4.16 oz (sizing mirrors Risk + Paper).
    assert trade["quantity"] == 4.16
    assert trade["gross_pnl_usd"] == pytest.approx(1.60 * 4.16)
    assert trade["fees_usd"] == pytest.approx(0.10 * 4.16)
    loss_per_unit = (2400.35 - 2398.0) + 0.05 + 0.10
    assert trade["r"] == pytest.approx((1.60 - 0.10) / loss_per_unit)
    assert trade["planned_net_rr"] == pytest.approx(((2402.0 - 2400.35) - 0.05 - 0.10) / loss_per_unit)
    assert result["final_balance_usd"] == pytest.approx(10000 + 1.50 * 4.16)


def test_sell_fills_at_bid_and_stops_on_ask():
    # Bid high 2401.80 never reaches SL 2402, but ask = bid + 0.30 does.
    bars = flat(T0, 5) + [m1(T0 + 300, 2400.0), m1(T0 + 360, 2400.0, 2401.8, 2399.9, 2400.5)]
    trade = run(bars, {T0 + 300: {"side": "SELL", "sl": 2402.0, "tp": 2396.0}})["trades"][0]
    assert trade["entry"] == 2399.95
    assert trade["exit_reason"] == "SL" and trade["exit"] == 2402.05


def test_sl_assumed_first_when_both_hit_in_one_m1_bar():
    bars = flat(T0, 5) + [m1(T0 + 300, 2400.0), m1(T0 + 360, 2400.5, 2402.5, 2397.5, 2401.0)]
    trade = run(bars, {T0 + 300: {"side": "BUY", "sl": 2398.0, "tp": 2402.0}})["trades"][0]
    assert trade["exit_reason"] == "SL" and trade["exit"] == 2397.95
    assert trade["r"] == pytest.approx(-1.0)


def test_gap_through_stop_fills_at_open_and_stale_signal_expires():
    bars = flat(T0, 5) + [m1(T0 + 300, 2400.0), m1(T0 + 360, 2396.0, 2396.5, 2395.0, 2396.2)]
    trade = run(bars, {T0 + 300: {"side": "BUY", "sl": 2398.0, "tp": 2402.0}})["trades"][0]
    assert trade["exit"] == 2395.95 and trade["r"] < -1
    # Next bar after a weekend-like gap: approval would have expired.
    gap = flat(T0, 5) + [m1(T0 + 2 * 86400, 2400.0)]
    result = run(gap, {T0 + 300: {"side": "BUY", "sl": 2398.0, "tp": 2402.0}})
    assert result["trades"] == [] and result["signal_stats"]["expired_before_fill"] == 1


def test_fill_outside_levels_and_one_position_at_a_time():
    bars = flat(T0, 5) + [m1(T0 + 300, 2403.0)] + flat(T0 + 360, 4, 2403.0) + flat(T0 + 600, 10, 2400.0)
    result = run(bars, {T0 + 300: {"side": "BUY", "sl": 2398.0, "tp": 2402.0}})
    assert result["signal_stats"]["fill_outside_sl_tp"] == 1
    signals = {T0 + 300: {"side": "BUY", "sl": 2390.0, "tp": 2410.0},
               T0 + 600: {"side": "SELL", "sl": 2410.0, "tp": 2390.0}}
    result = run(flat(T0, 20), signals)
    assert result["signal_stats"]["ignored_in_position"] == 1
    assert [t["exit_reason"] for t in result["trades"]] == ["end_of_data"]


def test_breakeven_trailing_and_time_stop():
    path = flat(T0, 5) + [m1(T0 + 300, 2400.0), m1(T0 + 360, 2400.4, 2402.8, 2400.3, 2402.6),
                          m1(T0 + 420, 2401.0, 2401.1, 2400.0, 2400.2)]
    be = run(path, {T0 + 300: {"side": "BUY", "sl": 2398.0, "tp": 2406.0, "breakeven_r": 1}})["trades"][0]
    assert be["exit_reason"] == "BE" and be["exit"] == 2400.30 and be["sl_final"] == 2400.35
    trail = run(path, {T0 + 300: {"side": "BUY", "sl": 2398.0, "tp": 2406.0, "trail_start_r": 0.5, "trail_r": 0.5}})["trades"][0]
    # Extreme 2402.80 - 0.5 × 2.35 = 2401.625 → floor to tick 2401.62; next bar opens below it.
    assert trail["exit_reason"] == "TRAIL" and trail["sl_final"] == 2401.62 and trail["exit"] == 2400.95
    timed = run(flat(T0, 30), {T0 + 300: {"side": "BUY", "sl": 2390.0, "tp": 2410.0, "time_stop_bars": 2}})["trades"][0]
    assert timed["exit_reason"] == "TIME" and timed["exit_time_s"] == T0 + 900


def test_resample_by_server_time_and_header_meta(tmp_path):
    bars = [m1(T0 + i * 60, 2400.0 + i, 2400.5 + i, 2399.5 + i, 2400.2 + i, spread=20 + i, volume=i + 1)
            for i in range(250)]
    bars = [b for b in bars if not 7 <= (b["open_server_s"] - T0) // 60 < 9]  # gap inside one M5 bucket
    m5 = resample(bars, 300)
    assert [b["open_server_s"] for b in m5[:2]] == [T0, T0 + 300]
    second = [b for b in bars if T0 + 300 <= b["open_server_s"] < T0 + 600]
    assert m5[1]["open"] == second[0]["open"] and m5[1]["close"] == second[-1]["close"]
    assert m5[1]["high"] == max(b["high"] for b in second) and m5[1]["tick_volume"] == sum(b["tick_volume"] for b in second)
    assert m5[1]["spread_points"] == second[-1]["spread_points"] and m5[1]["spread_max_points"] == 29
    h1 = resample(bars, 3600)
    assert [b["open_server_s"] for b in h1] == [T0, T0 + 3600, T0 + 7200, T0 + 10800, T0 + 14400]
    h4 = resample(bars, 14400)
    assert [b["open_server_s"] % 14400 for b in h4] == [0, 0] and h4[0]["open_server_s"] == 1_767_571_200
    path = write_csv(tmp_path / "m1.csv", bars, SYNTHETIC_META)
    loaded = load_m1(path)
    assert loaded.m1 == bars and loaded.point == 0.01 and loaded.digits == 2
    assert loaded.meta["server"] == "XMGlobal-MT5 7" and loaded.meta["observed_utc_offset_s"] == "7200"
    assert loaded.ask(loaded.m1[0]) == 2400.40
    assert parse_meta("# server=XMGlobal-MT5 7 symbol=GOLD point=0.01000000")["point"] == "0.01000000"
    unordered = tmp_path / "bad.csv"
    unordered.write_text("time_server_s,open,high,low,close,tick_volume,spread_points\n120,1,1,1,1,1,1\n60,1,1,1,1,1,1\n")
    with pytest.raises(ValueError):
        load_m1(unordered)


class Recorder(Strategy):
    name = "recorder"

    def __init__(self, params=None):
        super().__init__(params)
        self.seen = []

    def on_bar(self, ctx):
        assert ctx.m5[-1] is ctx.bar and ctx.bar["open_server_s"] + 300 == ctx.time_s
        for name, period in (("H1", 3600), ("H4", 14400), ("D1", 86400)):
            assert all(b["open_server_s"] + period <= ctx.time_s for b in ctx.frames[name])
        self.seen.append((ctx.time_s, dict(ctx.bar), len(ctx.h1), ctx.h1[-1]["close"] if ctx.h1 else None,
                          len(ctx.h4), ctx.position is None))
        if len(ctx.m5) > 3 and ctx.position is None and ctx.bar["close"] > ctx.m5[-2]["close"] > ctx.m5[-3]["close"]:
            return self.signal("BUY", ctx.bar["close"] - 3, ctx.bar["close"] + 3)
        return None


def test_no_lookahead_future_bars_do_not_change_past_decisions():
    bars = synthetic_m1(days=6, seed=3)
    cut = bars[len(bars) // 2]["open_server_s"] // 300 * 300
    runs = []
    for data in (bars, [b for b in bars if b["open_server_s"] < cut]):
        strategy = Recorder()
        result = Backtest(History(data), strategy, POLICY, PAPER).run()
        runs.append((strategy.seen, result["trades"]))
    (full_seen, full_trades), (cut_seen, cut_trades) = runs
    assert full_seen[:len(cut_seen)] == cut_seen
    finished = [t for t in cut_trades if t["exit_reason"] != "end_of_data"]
    assert finished and full_trades[:len(finished)] == finished
    assert all(t["entry_time_s"] >= t["signal_time_s"] for t in full_trades)
    # Engine M5 bars equal the standalone resampler.
    strategy = Recorder()
    Backtest(History(bars), strategy, POLICY, PAPER).run()
    keys = ("open_server_s", "open", "high", "low", "close", "tick_volume", "spread_points")
    assert [{k: s[1][k] for k in keys} for s in strategy.seen] == [{k: b[k] for k in keys} for b in resample(bars, 300)]


def test_indicator_parity_with_live_engine_and_sanity():
    bars = resample(synthetic_m1(days=3, seed=11), 300)
    core, rsi, atr, adx, ema = ind.XMCore(), ind.RSI(14), ind.ATR(14), ind.ADX(14), ind.EMA(20)
    state = {}
    for bar in bars:
        live = update_m5(state, bar)
        assert core.update(bar) == live
        assert rsi.update(bar) == live["rsi"] and atr.update(bar) == live["atr"] and ema.update(bar["close"]) == live["ema"]
        assert adx.update(bar) == {"adx": live["adx"], "plus_di": live["plus_di"], "minus_di": live["minus_di"]}
    line = [{"open_server_s": T0 + i * 300, "open": 100 + i, "high": 100.5 + i, "low": 99.5 + i, "close": 100 + i,
             "tick_volume": 10 if i % 2 else 30} for i in range(40)]
    er, chop, bb, macd, srsi, kel = ind.EfficiencyRatio(10), ind.Choppiness(14), ind.Bollinger(20), ind.MACD(), ind.StochRSI(), ind.Keltner()
    for bar in line:
        values = er.update(bar), chop.update(bar), bb.update(bar), macd.update(bar), srsi.update(bar), kel.update(bar)
    assert values[0] == 1.0 and values[1] < 50 and values[2]["width"] > 0 and values[3]["macd"] > 0
    assert values[4]["k"] == 50.0 and values[5]["upper"] > values[5]["middle"] > values[5]["lower"]
    assert ind.RSI(2).update(1.0) is None
    vwap = ind.SessionVWAP()
    assert vwap.update(line[0]) == pytest.approx(100) and vwap.update(line[1]) == pytest.approx((100 * 30 + 101 * 10) / 40)
    day2 = {**line[0], "open_server_s": line[0]["open_server_s"] + 86400, "high": 200.5, "low": 199.5, "close": 200}
    assert vwap.update(day2) == pytest.approx(200)
    pivots = ind.DailyPivots()
    for bar in line:
        assert pivots.update(bar) is None
    p = pivots.update(day2)
    assert p["high"] == 139.5 and p["low"] == 99.5 and p["close"] == 139 and p["p"] == pytest.approx((139.5 + 99.5 + 139) / 3)
    asia = ind.AsianRange(0, 10)
    day = 1_767_571_200
    early = asia.update({"open_server_s": day + 9 * 3600 + 3300, "high": 5, "low": 1, "close": 2})
    assert early["complete"] is True and early["high"] == 5
    assert asia.update({"open_server_s": day + 11 * 3600, "high": 9, "low": 0.5, "close": 2})["high"] == 5
    rvol = ind.RelativeVolume(days=2)
    for d in range(3):
        value = rvol.update({"open_server_s": day + d * 86400, "tick_volume": 10 * (d + 1)})
    assert value == pytest.approx(30 / 15)


def parity_bars():
    """Sparse hours warm H1 EMA200 within one snapshot window, then continuous M1."""
    rng = random.Random(5)
    bars, price, start = [], 2300.0, 1_767_571_200
    for hour in range(230):
        for minute in range(5):
            o = price
            price += rng.gauss(0.25, 0.4)
            bars.append(m1(start + hour * 3600 + minute * 60, round(o, 2), round(max(o, price) + 0.1, 2),
                           round(min(o, price) - 0.1, 2), round(price, 2), spread=25))
            price = bars[-1]["close"]
    dense = synthetic_m1(days=3, start_server_s=start + 230 * 3600, seed=21, price=price, weekends=False, regime_minutes=360)
    return bars + dense


def snapshot(m5, h1, j, offset=7200):
    server = m5[j]["open_server_s"] + 300 + 400
    keys = ("open_server_s", "open", "high", "low", "close", "tick_volume", "spread_points")
    pick = lambda b: {k: b[k] for k in keys}
    return Snapshot.model_validate({"schema_version": 1, "source": "mt5", "account_mode": "demo",
        "server": "XMGlobal-MT5 7", "broker_symbol": "GOLD", "captured_at_utc_s": server - offset,
        "server_time_s": server, "observed_utc_offset_s": offset, "digits": 2, "point": 0.01, "tick_size": 0.01,
        "quote": {"time_server_ms": server * 1000, "bid": m5[j]["close"], "ask": round(m5[j]["close"] + 0.25, 2)},
        "m5": [pick(b) for b in m5[max(0, j - 999):j + 1]],
        "h1": [pick(b) for b in h1 if b["open_server_s"] + 3600 <= server][-1000:]})


def test_baseline_candidates_match_xm_engine(tmp_path):
    bars = parity_bars()
    m5, h1 = resample(bars, 300), resample(bars, 3600)
    strategy = BaselineXM01()
    Backtest(History(bars), strategy, POLICY, PAPER).run()
    db = Database(tmp_path / "xm.db")
    db.initialize()
    engine = XMEngine(db)
    boot = 229
    first = snapshot(m5, h1, boot)
    assert engine.process(first, first.captured_at_utc_s)["status"] == "bootstrapped_no_historical_signals"
    for j in list(range(boot + 25, len(m5), 25)) + [len(m5) - 1]:
        snap = snapshot(m5, h1, j)
        assert engine.process(snap, snap.captured_at_utc_s)["status"] == "evaluated"
    with db.connect() as conn:
        rows = conn.execute("SELECT open_server_s, evaluation_json FROM xm_bar_evaluations ORDER BY open_server_s").fetchall()
        state = json.loads(conn.execute("SELECT state_json FROM xm_engine").fetchone()[0])
    assert len(rows) == len(m5) - boot - 1
    live = {r[0]: json.loads(r[1])["candidate_side"] for r in rows}
    ours = {c["open_server_s"]: c["side"] for c in strategy.candidates if c["open_server_s"] in live}
    assert {t: s for t, s in live.items() if s} == ours
    assert len(ours) >= 5
    assert db.list_signals(0, 100) == []
    strip = lambda d: {k: v for k, v in d.items() if k != "previous_bar"}
    assert strip(state["m5_indicators"]) == strip(strategy.state["m5_indicators"])
    assert state["h1_indicators"] == strategy.state["h1_indicators"] and state["candidate_lock"] == strategy.state["candidate_lock"]
    assert state["pivots"] == strategy.state["pivots"]


def test_segments_and_metrics():
    first = 1_767_571_200 + 5 * 3600
    split = metrics.split_point(first)
    assert split == 1_767_571_200 + (31 + 28 + 31 + 30 + 31 + 30 + 31 + 31) * 86400  # 2026-09-05 server day
    trades = [{"net_pnl_usd": 20.0, "r": 2.0, "planned_net_rr": 2.0, "planned_gross_rr": 2.2, "exit_reason": "TP",
               "signal_time_s": first, "exit_time_s": first + 600, "bars_held_m5": 2, "side": "BUY",
               "balance_after": 10020.0, "equity_low": 9995.0},
              {"net_pnl_usd": -10.0, "r": -1.0, "planned_net_rr": 2.0, "planned_gross_rr": 2.2, "exit_reason": "SL",
               "signal_time_s": first + 86400 * 3, "exit_time_s": first + 86400 * 3 + 300, "bars_held_m5": 1,
               "side": "SELL", "balance_after": 10010.0, "equity_low": 10010.0, "live_risk_reasons": ["x"]}]
    s = metrics.summarize(trades, first, first + 604800)
    assert s["win_rate"] == 0.5 and s["realized_rr"] == 2.0 and s["expectancy_r"] == 0.5
    assert s["profit_factor"] == 2.0 and s["trades_per_week"] == pytest.approx(2.0) and s["trades_failing_live_risk"] == 1
    # Intratrade low 9995 from 10000 (0.05%), then 10020 → 10010 (0.0998%).
    assert s["max_drawdown_pct"] == pytest.approx(10 / 10020 * 100)
    assert metrics.session_of(first) == "asia" and metrics.session_of(1_767_571_200 + 16 * 3600) == "new_york"


def test_runner_end_to_end_and_strategy_loader(tmp_path):
    data = write_csv(tmp_path / "syn.csv", synthetic_m1(days=40), SYNTHETIC_META)
    assert load("baseline_xm01") is BaselineXM01
    with pytest.raises(ValueError):
        load("../app")
    with pytest.raises(ValueError):
        BaselineXM01({"unknown": 1})
    target, summary = run_backtest.run("baseline_xm01", {"structure_filter": False}, data, "all", out_dir=tmp_path / "out", run_name="t1")
    stored = json.loads((target / "summary.json").read_text())
    assert stored["results"]["in_sample"]["trades"] > 0 and set(stored["results"]) == {"in_sample", "out_of_sample"}
    assert stored["data"]["sha256"] and stored["policy"]["risk"]["slippage_price_per_side"]
    trades = json.loads((target / "trades.json").read_text())
    assert len(trades["in_sample"]) == stored["results"]["in_sample"]["trades"]
    # Default policy files are the live Paper configuration.
    engine = Backtest(load_m1(data), BaselineXM01())
    assert engine.p == RiskPolicy.load(ROOT / "config/risk-xm-paper.json")
    assert Path(target).parent == tmp_path / "out"
