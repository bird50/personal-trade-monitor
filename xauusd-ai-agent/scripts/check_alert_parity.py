"""Compare alert-engine signals with the backtest's live-Risk-gated trades on M1 history.

The alert core is stepped once per closed M5 bar (H1 visible once closed) with the same
strategy class and parameters as config/alert-xm.json; the backtest runs the same strategy with
``live_risk_gate``. Signals are matched by (signal time, side). Differences come from rules
the alert flow cannot model before the user's own fill (next-bar fill price, fill-time spread
and drift), or from SL/TP touched intrabar on M1 versus the M5 range.

  .venv/bin/python scripts/check_alert_parity.py --data data/history/xm_gold_m1_36m.csv
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.alert_engine import AlertCore, AlertPolicy, gate  # noqa: E402
from app.risk_manager import RiskPolicy  # noqa: E402
from backtest.data import load_m1, resample  # noqa: E402
from backtest.engine import Backtest, load_policies  # noqa: E402
from backtest.strategies import load  # noqa: E402


def alert_signals(history, policy, risk):
    core = AlertCore(policy)
    m5, h1 = resample(history.m1, 300), resample(history.m1, 3600)
    out, h_index = [], 0
    for bar in m5:
        close_s = bar["open_server_s"] + 300
        new_h1 = []
        while h_index < len(h1) and h1[h_index]["open_server_s"] + 3600 <= close_s:
            new_h1.append(h1[h_index])
            h_index += 1
        signal, _ = core.step(bar, new_h1, history.point, history.digits)
        if not signal:
            continue
        spread = round(bar["spread_points"] * history.point, history.digits)
        _, stop, target, reasons, _ = gate(risk, signal["side"], bar["close"], signal["sl"], signal["tp"], signal["atr"], [spread])
        if not reasons:
            core.open(signal["side"], stop, target, close_s)
            out.append((close_s, signal["side"]))
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data", default=str(ROOT / "data/history/xm_gold_m1_36m.csv"))
    parser.add_argument("--policy", default=str(ROOT / "config/alert-xm.json"))
    args = parser.parse_args()
    policy = AlertPolicy.load(Path(args.policy))
    risk = RiskPolicy.load(policy.risk_path())
    history = load_m1(Path(args.data))
    _, paper = load_policies()
    strategy = load(policy.strategy)(policy.params)
    result = Backtest(history, strategy, risk, paper, live_risk_gate=True).run()
    backtest = [(t["signal_time_s"], t["side"]) for t in result["trades"]]
    alerts = alert_signals(history, policy, risk)
    only_backtest = sorted(set(backtest) - set(alerts))
    only_alerts = sorted(set(alerts) - set(backtest))
    stats = result["signal_stats"]
    print(json.dumps({
        "m1_bars": len(history.m1), "backtest_trades": len(backtest), "alert_signals": len(alerts),
        "matched": len(set(backtest) & set(alerts)), "only_backtest": only_backtest[:20],
        "only_alerts": only_alerts[:20], "only_backtest_count": len(only_backtest), "only_alerts_count": len(only_alerts),
        "backtest_fill_rejections": {k: v for k, v in stats.items() if k.startswith("live_risk") or k in ("expired_before_fill", "fill_outside_sl_tp")},
    }, indent=2))


if __name__ == "__main__":
    main()
