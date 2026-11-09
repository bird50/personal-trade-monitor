"""Compare alert-engine strategy cores with their backtests on M1 history.

Each configured strategy core is stepped once per closed M5 bar (H1 visible once closed) with the
same parameters as config/alert-xm.json.

* Market strategies (RSI(2), CCI, Heikin-Ashi) are compared with ``backtest.engine.Backtest`` run
  with ``live_risk_gate``; signals are matched by (signal time, side). Differences come from rules
  the alert flow cannot model before the user's own fill (next-bar fill price, fill-time spread
  and drift), or from SL/TP touched intrabar on M1 versus the M5 range.
* TPSL London breakout is compared with the M1 simulator ``scripts/tpsl_london_breakout.py``
  (cost model ``xm_paper``, trades that pass the live gate); days are matched by (day, side).
  Differences come from the order of events inside one M5 bar.

  .venv/bin/python scripts/check_alert_parity.py                 # every configured strategy
  .venv/bin/python scripts/check_alert_parity.py --strategy tpsl
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from app.alert_engine import AlertPolicy, gate  # noqa: E402
from app.alert_strategies import make_core  # noqa: E402
from app.risk_manager import RiskPolicy  # noqa: E402
from backtest.data import load_m1, resample  # noqa: E402
from backtest.engine import Backtest, load_policies  # noqa: E402
from backtest.strategies import load  # noqa: E402


def core_signals(history, spec, risk):
    """Gated core signals: market (close_s, side); TPSL (day, side) of each fill."""
    core = make_core(spec)
    m5, h1 = resample(history.m1, 300), resample(history.m1, 3600)
    out, setups, h_index = [], 0, 0
    for bar in m5:
        close_s = bar["open_server_s"] + 300
        new_h1 = []
        while h_index < len(h1) and h1[h_index]["open_server_s"] + 3600 <= close_s:
            new_h1.append(h1[h_index])
            h_index += 1
        signals, _ = core.step(bar, new_h1, history.point, history.digits)
        spread = round(bar["spread_points"] * history.point, history.digits)
        for signal in signals:
            if signal["type"] == "entry":
                _, stop, target, reasons, _ = gate(risk, signal["side"], bar["close"], signal["sl"], signal["tp"],
                                                   signal["atr"], [spread])
                if not reasons:
                    core.open(signal["side"], stop, target, close_s)
                    out.append((close_s, signal["side"]))
            elif signal["type"] == "setup":
                accepted = {}
                for side, o in signal["orders"].items():
                    entry, stop, target, reasons, _ = gate(risk, side, o["level"], o["sl"], o["tp"], signal["atr"], [spread])
                    if not reasons:
                        accepted[side] = {"level": entry, "sl": stop, "tp": target}
                setups += bool(accepted)
                core.arm(accepted, alerted=False)
            elif signal["type"] == "fill":
                out.append((bar["open_server_s"] // 86400, signal["side"]))
    return out, setups


def compare(a, b):
    a, b = set(a), set(b)
    return {"matched": len(a & b), "only_backtest_count": len(b - a), "only_alerts_count": len(a - b),
            "only_backtest": sorted(b - a)[:10], "only_alerts": sorted(a - b)[:10]}


def market(history, spec, risk, paper):
    strategy = load(spec.strategy)(spec.params)
    result = Backtest(history, strategy, risk, paper, live_risk_gate=True).run()
    backtest = [(t["signal_time_s"], t["side"]) for t in result["trades"]]
    alerts, _ = core_signals(history, spec, risk)
    stats = result["signal_stats"]
    return {"backtest_trades": len(backtest), "alert_signals": len(alerts), **compare(alerts, backtest),
            "backtest_fill_rejections": {k: v for k, v in stats.items()
                                         if k.startswith("live_risk") or k in ("expired_before_fill", "fill_outside_sl_tp")}}


def tpsl(history, spec, risk):
    import tpsl_london_breakout as sim
    rules = {**sim.RULES, **{k: v for k, v in spec.effective_params.items() if k in sim.RULES and k != "min_range_bars"}}
    sim.RULES.update(rules)
    trades, _ = sim.run(history, "xm_paper", sim.m5_atr_by_close(history))
    backtest = [(t["entry_time_s"] // 86400, t["side"]) for t in trades if not t["gate_reasons"]]
    fills, setups = core_signals(history, spec, risk)
    return {"simulator_trades_passing_gate": len(backtest), "core_setups_armed": setups, "core_fills": len(fills),
            **compare(fills, backtest)}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data", default=str(ROOT / "data/history/xm_gold_m1_36m.csv"))
    parser.add_argument("--policy", default=str(ROOT / "config/alert-xm.json"))
    parser.add_argument("--strategy", help="strategy id from the policy (default: all)")
    args = parser.parse_args()
    policy = AlertPolicy.load(Path(args.policy))
    risk = RiskPolicy.load(policy.risk_path())
    history = load_m1(Path(args.data))
    _, paper = load_policies()
    report = {"m1_bars": len(history.m1)}
    for spec in policy.strategies:
        if args.strategy and spec.id != args.strategy:
            continue
        report[spec.id] = tpsl(history, spec, risk) if spec.kind == "pending" else market(history, spec, risk, paper)
        print(spec.id, json.dumps(report[spec.id]), file=sys.stderr, flush=True)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
