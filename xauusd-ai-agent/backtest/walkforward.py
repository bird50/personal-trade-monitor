"""Parameter sweeps and walk-forward selection over cached full-history runs.

Each configuration of a grid is replayed once over the whole file (no trading window), and
its trades are then sliced by ``signal_time_s``. A fold picks the configuration that scores
best on its training window and keeps that configuration's trades in the following test
window; the stitched test trades are the walk-forward out-of-sample result.

Slicing one continuous run is an approximation of re-running each window separately: the
one-position-at-a-time occupancy and the equity used for sizing carry across window
boundaries. R multiples do not depend on equity, so R-based statistics are unaffected by the
latter.
"""
import copy
import itertools
import json
from datetime import datetime, timezone

from . import metrics
from .strategies.base import merge

OBJECTIVES = ("expectancy_r", "win_rate")


def expand(grid: dict) -> list[dict]:
    """``{"base": {...}, "axes": {key: [values...]}}`` → labelled configurations.

    An axis value may be ``{"label": str, "value": any}`` to name it; otherwise its JSON text
    is the label. Axis keys may use dots for nested parameters (``exits.breakeven_r``). The
    first value of every axis combined gives configuration 0.
    """
    base = grid.get("base") or {}
    axes = grid.get("axes") or {}
    if not isinstance(base, dict) or not isinstance(axes, dict) or any(not isinstance(v, list) or not v for v in axes.values()):
        raise ValueError("grid must be {'base': {...}, 'axes': {key: [non-empty list]}}")
    out = []
    for combo in itertools.product(*axes.values()):
        params, labels = copy.deepcopy(base), []
        for key, item in zip(axes, combo):
            label, value = (item["label"], item["value"]) if isinstance(item, dict) and set(item) == {"label", "value"} \
                else (json.dumps(item, sort_keys=True), item)
            labels.append(f"{key}={label}")
            *path, leaf = key.split(".")
            node = params
            for part in path:
                node = node.setdefault(part, {})
            node[leaf] = merge(node[leaf], value) if isinstance(value, dict) and isinstance(node.get(leaf), dict) else copy.deepcopy(value)
        out.append({"id": len(out), "label": " ".join(labels), "params": params})
    return out


def folds(first_s: int, last_s: int, train_months=6, test_months=1, anchored=False, min_test_days=7):
    """Consecutive test windows after an initial training span, on server-day boundaries."""
    start = first_s // 86400 * 86400
    end = last_s + 60
    out, k = [], 0
    while True:
        test_start = metrics.add_months(start, train_months + k * test_months)
        if test_start >= end:
            break
        test_end = min(metrics.add_months(start, train_months + (k + 1) * test_months), end)
        if (test_end - test_start) < min_test_days * 86400:
            break
        train_start = start if anchored else max(start, metrics.add_months(test_start, -train_months))
        out.append({"fold": k + 1, "train": [max(train_start, first_s), test_start], "test": [test_start, test_end]})
        k += 1
    return out


def window(trades, span):
    return [t for t in trades if span[0] <= t["signal_time_s"] < span[1]]


def r_drawdown(trades) -> float:
    """Largest peak-to-trough fall of cumulative R."""
    total = peak = worst = 0.0
    for t in trades:
        total += t["r"]
        peak = max(peak, total)
        worst = max(worst, peak - total)
    return worst


def usd_drawdown_pct(trades, initial=10000.0) -> float:
    """Closed-trade drawdown of a stitched sequence, re-based on ``initial`` USD."""
    balance = peak = initial
    worst = 0.0
    for t in trades:
        balance += t["net_pnl_usd"]
        peak = max(peak, balance)
        worst = max(worst, (peak - balance) / peak * 100)
    return worst


def stats(trades, span=None) -> dict:
    s = metrics.summarize(trades, *(span or (None, None)), max_drawdown_pct=usd_drawdown_pct(trades))
    keys = ("trades", "win_rate", "planned_net_rr", "realized_rr", "expectancy_r", "total_r", "profit_factor",
            "max_drawdown_pct", "trades_per_week", "net_pnl_usd", "exit_reasons", "trades_failing_live_risk")
    win_r = sum(t["r"] for t in trades if t["r"] > 0)
    loss_r = -sum(t["r"] for t in trades if t["r"] <= 0)
    return {k: s[k] for k in keys} | {
        "max_drawdown_r": r_drawdown(trades),
        # Equal-risk view: USD profit factor also reflects notional-cap sizing differences.
        "profit_factor_r": win_r / loss_r if loss_r else (float("inf") if win_r else 0.0),
        "tp_rate": s["exit_reasons"].get("TP", 0) / s["trades"] if s["trades"] else 0.0,
        "best_month_share": best_month_share(trades)}


def best_month_share(trades) -> float:
    """Share of total R from the best server-calendar month (1.0+ means the rest lost money)."""
    total = sum(t["r"] for t in trades)
    if total <= 0:
        return float("nan")
    months = {}
    for t in trades:
        key = datetime.fromtimestamp(t["signal_time_s"], timezone.utc).strftime("%Y-%m")  # server wall clock
        months[key] = months.get(key, 0.0) + t["r"]
    return max(months.values()) / total


def score(s: dict, objective: str, min_trades: int):
    """Sort key (higher is better) or None when the configuration is not eligible."""
    if s["trades"] < min_trades:
        return None
    if objective == "expectancy_r":
        return (s["expectancy_r"], s["win_rate"])
    if objective == "win_rate":
        # Highest win rate among configurations that made money in training.
        if s["expectancy_r"] <= 0 or s["profit_factor"] < 1.1:
            return None
        return (s["win_rate"], s["expectancy_r"])
    raise ValueError(f"objective must be one of {OBJECTIVES}")


def walk_forward(runs: dict, fold_list: list, objective="expectancy_r", min_trades=20) -> dict:
    """``runs``: {config_id: trades}. Returns per-fold choices and the stitched test trades."""
    chosen, stitched = [], []
    for f in fold_list:
        ranked = []
        for cid, trades in runs.items():
            s = stats(window(trades, f["train"]), f["train"])
            key = score(s, objective, min_trades)
            if key is not None:
                ranked.append((key, -cid, cid, s))
        if not ranked:
            chosen.append(f | {"config": None, "train_stats": None, "test_stats": stats([], f["test"])})
            continue
        _, _, cid, train_stats = max(ranked)
        test = window(runs[cid], f["test"])
        stitched.extend(test)
        chosen.append(f | {"config": cid, "train_stats": train_stats, "test_stats": stats(test, f["test"])})
    span = [fold_list[0]["test"][0], fold_list[-1]["test"][1]] if fold_list else None
    return {"objective": objective, "min_trades": min_trades, "folds": chosen,
            "out_of_sample": stats(stitched, span), "test_span_server_s": span,
            "skipped_folds": sum(1 for c in chosen if c["config"] is None)}
