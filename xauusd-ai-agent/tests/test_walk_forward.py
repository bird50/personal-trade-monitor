"""Walk-forward helpers: grid expansion, fold windows, selection without lookahead, CLI."""
import json
import math
import sys

import pytest

from app.config import ROOT
from backtest import walkforward as wf
from backtest.data import SYNTHETIC_META, synthetic_m1, write_csv

sys.path.insert(0, str(ROOT / "scripts"))
import walk_forward  # noqa: E402

DAY = 86400
T0 = 1_767_225_600  # 2026-01-01 00:00 server time


def trade(t, r):
    return {"signal_time_s": t, "exit_time_s": t + 600, "r": r, "net_pnl_usd": r * 10, "planned_net_rr": 1.5,
            "planned_gross_rr": 1.7, "exit_reason": "TP" if r > 0 else "SL", "bars_held_m5": 2,
            "balance_after": 10000 + r * 10}


def test_expand_labels_nested_keys_and_first_combination():
    grid = {"base": {"rr": 1.0, "exits": {"time_stop_bars": 36}},
            "axes": {"rr": [1.4, 2.0], "exits": [{"label": "nobe", "value": {"breakeven_r": None}}, {}],
                     "exits.time_stop_bars": [36, 72]}}
    configs = wf.expand(grid)
    assert len(configs) == 8 and [c["id"] for c in configs] == list(range(8))
    first = configs[0]
    assert first["label"] == "rr=1.4 exits=nobe exits.time_stop_bars=36"
    assert first["params"] == {"rr": 1.4, "exits": {"time_stop_bars": 36, "breakeven_r": None}}
    assert configs[-1]["params"] == {"rr": 2.0, "exits": {"time_stop_bars": 72}}
    assert grid["base"] == {"rr": 1.0, "exits": {"time_stop_bars": 36}}  # not mutated
    with pytest.raises(ValueError):
        wf.expand({"axes": {"rr": []}})


def test_folds_rolling_anchored_and_partial_tail():
    last = T0 + 365 * DAY - 60  # through 2026-12-31
    rolling = wf.folds(T0 + 3600, last, train_months=6, test_months=2)
    assert [f["test"][0] for f in rolling] == [T0 + d * DAY for d in (181, 243, 304)]  # Jul 1, Sep 1, Nov 1
    assert rolling[-1]["test"][1] == last + 60
    assert rolling[0]["train"] == [T0 + 3600, T0 + 181 * DAY]
    assert rolling[1]["train"][0] == T0 + 59 * DAY  # Mar 1: six months before Sep 1
    anchored = wf.folds(T0, last, 6, 2, anchored=True)
    assert all(f["train"][0] == T0 for f in anchored)
    # A tail shorter than a week is not a fold.
    assert len(wf.folds(T0, T0 + 184 * DAY, 6, 1)) == 0
    assert len(wf.folds(T0, T0 + 190 * DAY, 6, 1)) == 1


def test_selection_uses_training_window_only_and_stitches_test_trades():
    fold_list = [{"fold": 1, "train": [T0, T0 + 10 * DAY], "test": [T0 + 10 * DAY, T0 + 20 * DAY]},
                 {"fold": 2, "train": [T0 + 10 * DAY, T0 + 20 * DAY], "test": [T0 + 20 * DAY, T0 + 30 * DAY]}]
    # Config 0 wins days 0-10 then loses; config 1 is the reverse. Selection must not peek ahead.
    runs = {0: [trade(T0 + d * DAY, 1.0 if d < 10 else -1.0) for d in range(30)],
            1: [trade(T0 + d * DAY, -1.0 if d < 10 else 0.5) for d in range(30)]}
    out = wf.walk_forward(runs, fold_list, "expectancy_r", min_trades=5)
    assert [f["config"] for f in out["folds"]] == [0, 1]
    assert out["folds"][0]["test_stats"]["expectancy_r"] == -1.0
    assert out["folds"][1]["test_stats"]["expectancy_r"] == 0.5
    assert out["out_of_sample"]["trades"] == 20
    assert out["out_of_sample"]["expectancy_r"] == pytest.approx(-0.25)
    assert out["out_of_sample"]["max_drawdown_r"] == pytest.approx(10.0)
    # Too few training trades → the fold is skipped, not filled with a fallback.
    skipped = wf.walk_forward(runs, fold_list, "expectancy_r", min_trades=11)
    assert skipped["skipped_folds"] == 2 and skipped["out_of_sample"]["trades"] == 0
    # win_rate objective only considers configurations that were profitable in training.
    losing = {0: [trade(T0 + d * DAY, -1.0) for d in range(20)]}
    assert wf.walk_forward(losing, fold_list[:1], "win_rate", 5)["folds"][0]["config"] is None


def test_stats_r_profit_factor_and_month_concentration():
    trades = [trade(T0, 2.0), trade(T0 + DAY, -1.0), trade(T0 + 40 * DAY, 3.0)]
    s = wf.stats(trades)
    assert s["profit_factor_r"] == 5.0 and s["tp_rate"] == pytest.approx(2 / 3)
    assert s["best_month_share"] == pytest.approx(0.75)  # Feb +3 of total +4
    assert math.isnan(wf.stats([trade(T0, -1.0)])["best_month_share"])


def test_cli_run_end_to_end_and_parallel_matches_serial(tmp_path):
    data = write_csv(tmp_path / "syn.csv", synthetic_m1(days=75), SYNTHETIC_META)
    grid = {"base": {"structure_filter": False}, "axes": {"exits": [{}, {"label": "be1", "value": {"breakeven_r": 1}}]}}
    target, meta, table, results, reference = walk_forward.run(
        "baseline_xm01", grid, data, train_months=1, test_months=1, min_trades=1, jobs=1,
        out_dir=tmp_path / "out", run_name="wf")
    assert meta["walk_forward"]["folds"] >= 1 and len(table) == 2
    assert table[0]["full"]["trades"] > 0
    assert {p.name for p in target.iterdir()} == {"configs.json", "walk_forward.json", "trades.json"}
    stored = json.loads((target / "walk_forward.json").read_text())
    assert set(stored["results"]) == set(wf.OBJECTIVES)
    _, _, table2, results2, _ = walk_forward.run(
        "baseline_xm01", grid, data, train_months=1, test_months=1, min_trades=1, jobs=2,
        out_dir=tmp_path / "out", run_name="wf2")
    gated = walk_forward.run("baseline_xm01", grid, data, train_months=1, test_months=1, min_trades=1, jobs=1,
                             out_dir=tmp_path / "out", run_name="wf3", live_risk_gate=True)
    assert gated[1]["live_risk_gate"] is True
    assert all(t["live_risk_reasons"] == [] for t in json.loads((gated[0] / "trades.json").read_text())["0"])
    assert json.dumps(table2) == json.dumps(table) and json.dumps(results2) == json.dumps(results)  # NaN-safe
