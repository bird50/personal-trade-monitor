"""Sweep a parameter grid and run walk-forward selection for one strategy.

Each configuration is replayed once over the whole data file; results are written to
.runtime/backtest/<run>/ as configs.json (fixed 8/4-month split and full-period statistics per
configuration), walk_forward.json (fold choices and stitched out-of-sample statistics for
each objective) and trades.json (all trades per configuration).

Example:
  .venv/bin/python scripts/walk_forward.py --strategy bb_squeeze_breakout_r2 \\
      --grid backtest/grids/bb_squeeze_breakout_r2.json --train-months 6 --test-months 1

Grid format: {"base": {params...}, "axes": {"rr": [1.4, 2.0], "exits": [{"label": "nobe",
"value": {"breakeven_r": null}}]}}. Pick configurations for the fixed split on in-sample
statistics only; walk_forward.json is the selection-free out-of-sample estimate.
"""
import argparse
import hashlib
import json
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest import metrics, walkforward as wf  # noqa: E402
from backtest.data import load_m1  # noqa: E402
from backtest.engine import Backtest, load_policies, policy_snapshot  # noqa: E402
from backtest.strategies import load  # noqa: E402
from run_backtest import DEFAULT_DATA, clean, read_params  # noqa: E402

_STATE = {}


def _init(data_path):
    _STATE["history"] = load_m1(data_path)
    _STATE["policies"] = load_policies()


def _replay(job):
    strategy_name, params, live_risk_gate = job
    risk, paper = _STATE["policies"]
    strategy = load(strategy_name)(params)
    return Backtest(_STATE["history"], strategy, risk, paper, live_risk_gate=live_risk_gate).run()["trades"]


def replay_all(strategy_name, configs, data_path, jobs, live_risk_gate=False):
    work = [(strategy_name, c["params"], live_risk_gate) for c in configs]
    if jobs <= 1:
        _init(data_path)
        return [_replay(job) for job in work]
    with ProcessPoolExecutor(max_workers=jobs, initializer=_init, initargs=(str(data_path),)) as pool:
        return list(pool.map(_replay, work))


def run(strategy_name, grid, data_path, train_months=6, test_months=1, anchored=False, min_trades=20,
        jobs=1, out_dir=ROOT / ".runtime/backtest", run_name=None, live_risk_gate=False):
    started = time.time()
    history = load_m1(data_path)
    risk, paper = load_policies()
    configs = wf.expand(grid)
    strategy_cls = load(strategy_name)
    for c in configs:
        c["params"] = strategy_cls(c["params"]).params  # validates and records effective values
    first, last = history.m1[0]["open_server_s"], history.m1[-1]["open_server_s"]
    seg = metrics.segments(first, last)
    fold_list = wf.folds(first, last, train_months, test_months, anchored)
    if not fold_list:
        raise SystemExit("Data too short for one walk-forward fold")
    all_trades = replay_all(strategy_name, configs, data_path, jobs, live_risk_gate)
    runs = {c["id"]: trades for c, trades in zip(configs, all_trades)}
    table = []
    for c in configs:
        trades = runs[c["id"]]
        table.append(c | {
            "in_sample": wf.stats(wf.window(trades, seg["in_sample"]), seg["in_sample"]),
            "out_of_sample": wf.stats(wf.window(trades, seg["out_of_sample"]), seg["out_of_sample"]),
            "full": wf.stats(trades, [first, last + 60]),
        })
    results = {obj: wf.walk_forward(runs, fold_list, obj, min_trades) for obj in wf.OBJECTIVES}
    # Reference: configuration 0 held fixed over the same test span (no re-selection).
    span = results["expectancy_r"]["test_span_server_s"]
    reference = wf.stats(wf.window(runs[0], span), span)
    digest = hashlib.sha256(json.dumps([c["params"] for c in configs], sort_keys=True).encode()).hexdigest()[:10]
    run_name = run_name or f"wf-{strategy_name}-{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}-{digest}"
    if not all(ch.isalnum() or ch in "-_." for ch in run_name):
        raise SystemExit("--run may contain only letters, digits, '-', '_' and '.'")
    target = Path(out_dir) / run_name
    target.mkdir(parents=True, exist_ok=True)
    meta = {"run": run_name, "strategy": strategy_name, "grid_sha256_10": digest, "configs": len(configs),
            "data": {"path": str(data_path), "sha256": history.sha256, "m1_bars": len(history.m1),
                     "first_server_s": first, "last_server_s": last, "split_server_s": seg["split_server_s"]},
            "walk_forward": {"train_months": train_months, "test_months": test_months, "anchored": anchored,
                             "min_trades": min_trades, "folds": len(fold_list)},
            "live_risk_gate": live_risk_gate,
            "policy": policy_snapshot(risk, paper), "elapsed_s": round(time.time() - started, 2),
            "notes": ["Each configuration is one continuous replay sliced by signal time.",
                      "Research simulation on historical bars; not evidence of live profitability."]}
    (target / "configs.json").write_text(json.dumps(clean(meta | {"configs": table}), indent=2, allow_nan=False))
    (target / "walk_forward.json").write_text(json.dumps(clean(meta | {"results": results, "reference_config0": reference}),
                                                         indent=2, allow_nan=False))
    (target / "trades.json").write_text(json.dumps(clean({str(k): v for k, v in runs.items()}), allow_nan=False))
    return target, meta, table, results, reference


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--strategy", required=True)
    parser.add_argument("--grid", required=True, help="JSON object or path to a JSON file")
    parser.add_argument("--data", default=str(DEFAULT_DATA))
    parser.add_argument("--train-months", type=int, default=6)
    parser.add_argument("--test-months", type=int, default=1)
    parser.add_argument("--anchored", action="store_true", help="expanding training window from the first bar")
    parser.add_argument("--min-trades", type=int, default=20, help="minimum training trades for a configuration")
    parser.add_argument("--live-risk-gate", action="store_true",
                        help="skip signals the live Risk/Paper filters would reject (as Paper would)")
    parser.add_argument("--jobs", type=int, default=max(1, min(6, (os.cpu_count() or 2) - 2)))
    parser.add_argument("--run")
    parser.add_argument("--out-dir", default=str(ROOT / ".runtime/backtest"))
    args = parser.parse_args()
    if not Path(args.data).is_file():
        raise SystemExit(f"Data file not found: {args.data}")
    if args.train_months < 1 or args.test_months < 1 or args.min_trades < 1:
        raise SystemExit("--train-months, --test-months and --min-trades must be >= 1")
    target, meta, table, results, reference = run(
        args.strategy, read_params(args.grid), Path(args.data), args.train_months, args.test_months,
        args.anchored, args.min_trades, args.jobs, Path(args.out_dir), args.run, args.live_risk_gate)
    keys = ("trades", "win_rate", "realized_rr", "expectancy_r", "profit_factor", "max_drawdown_r")
    print(json.dumps(clean({"output": str(target), "elapsed_s": meta["elapsed_s"], "folds": meta["walk_forward"]["folds"],
                            "walk_forward": {o: {k: r["out_of_sample"][k] for k in keys} | {"skipped_folds": r["skipped_folds"]}
                                             for o, r in results.items()},
                            "reference_config0": {k: reference[k] for k in keys}}), indent=2))


if __name__ == "__main__":
    main()
