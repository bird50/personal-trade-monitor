"""Run one offline backtest and write JSON results to .runtime/backtest/<run>/.

Examples:
  .venv/bin/python scripts/run_backtest.py --strategy baseline_xm01
  .venv/bin/python scripts/run_backtest.py --strategy baseline_xm01 --params '{"exits": {"breakeven_r": 1}}'
  .venv/bin/python scripts/run_backtest.py --write-synthetic /tmp/synthetic.csv --synthetic-days 30

The default segment is in_sample (first 8 months). Use --segment out_of_sample/all only for
final validation; do not tune parameters on out-of-sample results.
"""
import argparse
import hashlib
import json
import math
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest import metrics  # noqa: E402
from backtest.data import SYNTHETIC_META, load_m1, synthetic_m1, write_csv  # noqa: E402
from backtest.engine import PAPER_POLICY, RISK_POLICY, Backtest, load_policies, policy_snapshot  # noqa: E402
from backtest.strategies import load  # noqa: E402

DEFAULT_DATA = ROOT / "data/history/xm_gold_m1.csv"


def clean(value):
    """JSON without NaN/Infinity: non-finite floats become null."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {k: clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    return value


def read_params(text):
    if not text:
        return {}
    path = Path(text)
    raw = path.read_text() if not text.lstrip().startswith("{") and path.is_file() else text
    params = json.loads(raw)
    if not isinstance(params, dict):
        raise SystemExit("--params must be a JSON object")
    return params


def run(strategy_name, params, data_path, segment="in_sample", live_risk_gate=False,
        risk_path=RISK_POLICY, paper_path=PAPER_POLICY, out_dir=ROOT / ".runtime/backtest", run_name=None):
    history = load_m1(data_path)
    risk, paper = load_policies(risk_path, paper_path)
    strategy_cls = load(strategy_name)
    bounds = metrics.segments(history.m1[0]["open_server_s"], history.m1[-1]["open_server_s"])
    wanted = ("in_sample", "out_of_sample") if segment == "all" else (segment,)
    results, trades = {}, {}
    started = time.time()
    for name in wanted:
        start, end = bounds[name]
        engine = Backtest(history, strategy_cls(params), risk, paper, trade_from_s=start, trade_until_s=end,
                          live_risk_gate=live_risk_gate)
        raw = engine.run()
        results[name] = metrics.report(raw) | {"window_server_s": [start, end]}
        trades[name] = raw["trades"]
    effective = strategy_cls(params).params
    digest = hashlib.sha256(json.dumps(effective, sort_keys=True).encode()).hexdigest()[:10]
    run_name = run_name or f"{strategy_name}-{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}-{digest}"
    if not all(c.isalnum() or c in "-_." for c in run_name):
        raise SystemExit("--run may contain only letters, digits, '-', '_' and '.'")
    target = Path(out_dir) / run_name
    target.mkdir(parents=True, exist_ok=True)
    summary = {
        "run": run_name, "strategy": strategy_name, "params": effective, "params_sha256_10": digest,
        "segment": segment, "live_risk_gate": live_risk_gate,
        "data": {"path": str(data_path), "sha256": history.sha256, "meta": history.meta, "point": history.point,
                 "digits": history.digits, "m1_bars": len(history.m1),
                 "first_server_s": history.m1[0]["open_server_s"], "last_server_s": history.m1[-1]["open_server_s"],
                 "split_server_s": bounds["split_server_s"], "time_basis": "broker_server_wall_clock"},
        "policy": policy_snapshot(risk, paper), "sessions_server_hours": metrics.SESSIONS,
        "results": results, "elapsed_s": round(time.time() - started, 2),
        "notes": ["Research simulation on historical bars; not evidence of live profitability.",
                  "Daily loss lock, AI review and quote-age gates are not modelled."],
    }
    (target / "summary.json").write_text(json.dumps(clean(summary), indent=2, allow_nan=False))
    (target / "trades.json").write_text(json.dumps(clean(trades), allow_nan=False))
    return target, summary


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--strategy", help="module name under backtest/strategies/")
    parser.add_argument("--params", default="", help="JSON object or path to a JSON file")
    parser.add_argument("--data", default=str(DEFAULT_DATA))
    parser.add_argument("--segment", choices=("in_sample", "out_of_sample", "all"), default="in_sample")
    parser.add_argument("--live-risk-gate", action="store_true", help="skip signals the live Risk/Paper filters would reject")
    parser.add_argument("--run", help="run directory name (default: strategy-timestamp-paramhash)")
    parser.add_argument("--out-dir", default=str(ROOT / ".runtime/backtest"))
    parser.add_argument("--write-synthetic", metavar="PATH", help="write a synthetic M1 CSV (refuses to overwrite) and exit")
    parser.add_argument("--synthetic-days", type=int, default=30)
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()
    if args.write_synthetic:
        path = Path(args.write_synthetic)
        if path.exists():
            raise SystemExit(f"{path} exists; refusing to overwrite")
        write_csv(path, synthetic_m1(days=args.synthetic_days, seed=args.seed), SYNTHETIC_META)
        print(json.dumps({"written": str(path), "days": args.synthetic_days, "seed": args.seed}))
        return
    if not args.strategy:
        parser.error("--strategy is required")
    if not Path(args.data).is_file():
        raise SystemExit(f"Data file not found: {args.data}")
    target, summary = run(args.strategy, read_params(args.params), Path(args.data), args.segment,
                          args.live_risk_gate, out_dir=Path(args.out_dir), run_name=args.run)
    keys = ("trades", "win_rate", "planned_net_rr", "realized_rr", "expectancy_r", "profit_factor",
            "max_drawdown_pct", "trades_per_week", "net_pnl_usd", "trades_failing_live_risk")
    print(json.dumps(clean({"output": str(target), "elapsed_s": summary["elapsed_s"],
                            **{seg: {k: r[k] for k in keys} for seg, r in summary["results"].items()}}), indent=2))


if __name__ == "__main__":
    main()
