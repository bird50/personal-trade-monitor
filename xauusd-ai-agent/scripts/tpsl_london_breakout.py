"""Replicate the published TPSL gold London breakout on XM M1 history with locked rules.

Source rules (github.com/TradingTPSL/Trading-Strategies-TPSL.si, strategies/london-breakout-classic.md),
applied on the broker *server* wall clock (XM GMT+2/+3, same offsets as the source's Athens time):

* Mon-Fri, at most one trade per day.
* Range = bid high/low of M1 bars opening in [03:00, 08:00). Skip the day when the range width
  is < 0.3 % or > 1.6 % of price (08:00 open).
* From 08:00: buy stop at range high + 0.2 (triggered on ask), sell stop at range low - 0.2
  (triggered on bid). The first fill cancels the other.
* SL = opposite side of the range, capped at 0.6 % of price from the order level.
  TP = 2.5 R from the order level (R = |level - SL|). SL moves to the fill price once the trade is
  +1.5 R in favour (applies from the next M1 bar). Close open trades and cancel orders at 21:00.

The engine in ``backtest/`` only fills market orders at the next bar, so this is a standalone
M1 simulator. It is not part of the live alert service and is not a profitability claim.
Conservative conventions: a gap through a level fills at the bar open; when a fill bar also
reaches the SL, the SL is assumed hit; when both stop orders trigger in one M1 bar the order
nearer the bar open fills first (counted in ``both_in_bar``). Results are per 1 oz.

  .venv/bin/python scripts/tpsl_london_breakout.py --out .runtime/backtest/tpsl-london
"""
import argparse
import json
import random
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.data import load_m1, resample  # noqa: E402
from backtest.indicators import ATR  # noqa: E402
from app.risk_manager import RiskPolicy  # noqa: E402
from backtest.engine import RISK_POLICY  # noqa: E402

RULES = {"range_start_h": 3, "range_end_h": 8, "close_h": 21, "min_width_pct": 0.3, "max_width_pct": 1.6,
         "offset": 0.2, "stop_cap_pct": 0.6, "rr": 2.5, "breakeven_r": 1.5, "min_range_bars": 240}
# Cost models (price units per 1 oz). spread_mult scales the per-bar broker spread.
COSTS = {
    "tpsl": {"commission": 0.07, "slip_entry": 0.05, "slip_stop": 0.10, "slip_target": 0.0, "slip_time": 0.0,
             "spread_mult": 1.0},
    "tpsl_x2": {"commission": 0.14, "slip_entry": 0.10, "slip_stop": 0.20, "slip_target": 0.0, "slip_time": 0.0,
                "spread_mult": 2.0},
    "xm_paper": {"commission": 0.10, "slip_entry": 0.05, "slip_stop": 0.05, "slip_target": 0.05, "slip_time": 0.05,
                 "spread_mult": 1.0},
}
# Live Risk policy checks that can be evaluated at the 08:00 order time, read from the current policy.
_POLICY = RiskPolicy.load(RISK_POLICY)
GATE = {"min_stop_atr": float(_POLICY.min_stop_atr), "max_stop_atr": float(_POLICY.max_stop_atr),
        "max_spread_atr": float(_POLICY.max_spread_atr), "spread_floor": float(_POLICY.spread_price)}


def weekday(day):
    return (day + 3) % 7  # 0 = Monday


def m5_atr_by_close(history):
    """M5 ATR14 keyed by each M5 bar's close time (server seconds)."""
    atr, out = ATR(14), {}
    for bar in resample(history.m1, 300):
        value = atr.update(bar)
        if value is not None:
            out[bar["open_server_s"] + 300] = value
    return out


def simulate_day(bars, rules, cost, point, atr_at_open):
    """Return (trade dict | None, skip reason | None) for one server day's M1 bars."""
    day_start = bars[0]["open_server_s"] // 86400 * 86400
    rs, re_, close_t = (day_start + rules[k] * 3600 for k in ("range_start_h", "range_end_h", "close_h"))
    rng = [b for b in bars if rs <= b["open_server_s"] < re_]
    live = [b for b in bars if re_ <= b["open_server_s"]]
    if len(rng) < rules["min_range_bars"] or not live or live[0]["open_server_s"] >= close_t:
        return None, "missing_bars"
    hi, lo = max(b["high"] for b in rng), min(b["low"] for b in rng)
    price = live[0]["open"]
    width_pct = (hi - lo) / price * 100
    if width_pct < rules["min_width_pct"]:
        return None, "range_too_narrow"
    if width_pct > rules["max_width_pct"]:
        return None, "range_too_wide"
    cap = rules["stop_cap_pct"] / 100 * price
    buy_l, sell_l = hi + rules["offset"], lo - rules["offset"]
    orders = {"BUY": (buy_l, max(lo, buy_l - cap)), "SELL": (sell_l, min(hi, sell_l + cap))}
    sm = cost["spread_mult"]

    def spread(b):
        return b["spread_points"] * point * sm

    pre_spread = spread(rng[-1])
    pos, flags = None, []
    for b in live:
        t, sp = b["open_server_s"], spread(b)
        if t >= close_t:
            if pos:
                d = pos["dir"]
                q = b["open"] if d == 1 else b["open"] + sp
                return finish(pos, q - d * cost["slip_time"], "TIME", t, cost, flags, atr_at_open, pre_spread), None
            break
        if pos is None:
            ask_o, ask_h = b["open"] + sp, b["high"] + sp
            buy_hit = ask_o >= buy_l or ask_h >= buy_l
            sell_hit = b["open"] <= sell_l or b["low"] <= sell_l
            if not (buy_hit or sell_hit):
                continue
            if buy_hit and sell_hit:
                flags.append("both_in_bar")
                side = "BUY" if abs(ask_o - buy_l) <= abs(b["open"] - sell_l) else "SELL"
            else:
                side = "BUY" if buy_hit else "SELL"
            level, sl = orders[side]
            d = 1 if side == "BUY" else -1
            quote = (ask_o if ask_o >= buy_l else buy_l) if d == 1 else (b["open"] if b["open"] <= sell_l else sell_l)
            entry = quote + d * cost["slip_entry"]
            risk = abs(level - sl)
            pos = {"side": side, "dir": d, "entry": entry, "level": level, "sl": sl, "sl0": sl,
                   "tp": level + d * rules["rr"] * risk, "risk": risk, "entry_t": t, "be": False}
            # Fill bar: only the stop is checked (conservative), TP is not.
            if (d == 1 and b["low"] <= sl) or (d == -1 and b["high"] + sp >= sl):
                flags.append("sl_on_fill_bar")
                return finish(pos, sl - d * cost["slip_stop"], "SL", t, cost, flags, atr_at_open, pre_spread), None
            continue
        d = pos["dir"]
        o, h, l = (b["open"], b["high"], b["low"]) if d == 1 else (b["open"] + sp, b["high"] + sp, b["low"] + sp)
        sl, tp, kind = pos["sl"], pos["tp"], "BE" if pos["be"] else "SL"
        if d == 1:
            hit = (o, kind) if o <= sl else (sl, kind) if l <= sl else (o, "TP") if o >= tp else (tp, "TP") if h >= tp else None
        else:
            hit = (o, kind) if o >= sl else (sl, kind) if h >= sl else (o, "TP") if o <= tp else (tp, "TP") if l <= tp else None
        if hit:
            slip = cost["slip_target"] if hit[1] == "TP" else cost["slip_stop"]
            return finish(pos, hit[0] - d * slip, hit[1], t, cost, flags, atr_at_open, pre_spread), None
        favour = (h - pos["entry"]) if d == 1 else (pos["entry"] - l)
        if not pos["be"] and favour >= rules["breakeven_r"] * pos["risk"]:
            pos["sl"], pos["be"] = pos["entry"], True
    if pos:  # no bar at/after 21:00 (early close, data end): exit at the last close
        b, d = bars[-1], pos["dir"]
        q = b["close"] if d == 1 else b["close"] + spread(b)
        return finish(pos, q - d * cost["slip_time"], "TIME", b["open_server_s"] + 60, cost, flags, atr_at_open,
                      pre_spread), None
    return None, "no_fill"


def finish(pos, exit_price, reason, t, cost, flags, atr, pre_spread):
    d = pos["dir"]
    net = (exit_price - pos["entry"]) * d - cost["commission"]
    risk_usd = abs(pos["entry"] - pos["sl0"]) + cost["slip_stop"] + cost["commission"]
    gate = []
    if atr:
        stop_atr = pos["risk"] / atr
        if not GATE["min_stop_atr"] <= stop_atr <= GATE["max_stop_atr"]:
            gate.append("stop_atr_out_of_range")
        if max(GATE["spread_floor"], pre_spread) / atr > GATE["max_spread_atr"]:
            gate.append("spread_atr_too_high")
    else:
        gate.append("no_atr")
    return {"side": pos["side"], "entry_time_s": pos["entry_t"], "exit_time_s": t, "entry": round(pos["entry"], 2),
            "sl": round(pos["sl0"], 2), "tp": round(pos["tp"], 2), "exit": round(exit_price, 2), "reason": reason,
            "net_usd_per_oz": net, "r": net / risk_usd if risk_usd else 0.0,
            "stop_atr": pos["risk"] / atr if atr else None, "flags": flags, "gate_reasons": gate}


def summarize(trades, seed=7):
    if not trades:
        return {"trades": 0}
    nets = [t["net_usd_per_oz"] for t in trades]
    wins, losses = sum(n for n in nets if n > 0), -sum(n for n in nets if n < 0)
    eq = peak = dd = 0.0
    for n in nets:
        eq += n
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    months = defaultdict(float)
    for t in trades:
        months[month(t["entry_time_s"])] += t["net_usd_per_oz"]
    rnd, pfs = random.Random(seed), []
    for _ in range(2000):
        s = [rnd.choice(nets) for _ in nets]
        w, l = sum(x for x in s if x > 0), -sum(x for x in s if x < 0)
        pfs.append(w / l if l else float("inf"))
    pfs.sort()
    return {"trades": len(trades), "win_rate": sum(n > 0 for n in nets) / len(nets),
            "profit_factor": wins / losses if losses else None,
            "pf_90ci": [pfs[int(0.05 * len(pfs))], pfs[int(0.95 * len(pfs)) - 1]],
            "net_usd_per_oz": sum(nets), "avg_r": sum(t["r"] for t in trades) / len(trades),
            "max_dd_usd_per_oz": dd, "months": len(months), "losing_months": sum(v < 0 for v in months.values()),
            "exits": dict(Counter(t["reason"] for t in trades)),
            "sides": {s: round(sum(t["net_usd_per_oz"] for t in trades if t["side"] == s), 2) for s in ("BUY", "SELL")}}


def month(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m")


PERIODS = [("2023-11 → 2024-09", "2023-11", "2024-09"), ("2024-10 → 2025-09", "2024-10", "2025-09"),
           ("2025-10 → 2026-10", "2025-10", "2026-10")]


def run(history, cost_name, atr_map):
    cost, days, skips = COSTS[cost_name], defaultdict(list), Counter()
    for b in history.m1:
        days[b["open_server_s"] // 86400].append(b)
    trades = []
    for day in sorted(days):
        if weekday(day) >= 5:
            continue
        atr = atr_map.get(day * 86400 + RULES["range_end_h"] * 3600)
        trade, skip = simulate_day(days[day], RULES, cost, history.point, atr)
        if trade:
            trades.append(trade)
            for f in trade["flags"]:
                skips["flag:" + f] += 1
        else:
            skips[skip] += 1
    return trades, skips


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--data", default=str(ROOT / "data/history/xm_gold_m1_36m.csv"))
    ap.add_argument("--out", default=str(ROOT / ".runtime/backtest/tpsl-london"))
    args = ap.parse_args()
    history = load_m1(args.data)
    atr_map = m5_atr_by_close(history)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    report = {"data": args.data, "sha256": history.sha256, "rules": RULES, "costs": COSTS, "gate": GATE, "runs": {}}
    for cost_name in COSTS:
        trades, skips = run(history, cost_name, atr_map)
        gated = [t for t in trades if not t["gate_reasons"]]
        by_month = defaultdict(lambda: [0, 0.0])
        for t in trades:
            m = by_month[month(t["entry_time_s"])]
            m[0] += 1
            m[1] += t["net_usd_per_oz"]
        report["runs"][cost_name] = {
            "all": summarize(trades), "day_stats": dict(skips),
            "periods": {name: summarize([t for t in trades if a <= month(t["entry_time_s"]) <= b])
                        for name, a, b in PERIODS},
            "live_risk_gate": {"passed": summarize(gated),
                               "reasons": dict(Counter(r for t in trades for r in t["gate_reasons"])),
                               "periods": {name: summarize([t for t in gated if a <= month(t["entry_time_s"]) <= b])
                                           for name, a, b in PERIODS}},
            "months": {k: {"trades": v[0], "net_usd_per_oz": round(v[1], 2)} for k, v in sorted(by_month.items())},
        }
        (out / f"trades-{cost_name}.json").write_text(json.dumps(trades, indent=1))
    (out / "summary.json").write_text(json.dumps(report, indent=1))
    for name, r in report["runs"].items():
        a = r["all"]
        print(f"[{name}] trades={a['trades']} WR={a['win_rate']:.1%} PF={a['profit_factor']:.2f} "
              f"CI={a['pf_90ci'][0]:.2f}-{a['pf_90ci'][1]:.2f} net={a['net_usd_per_oz']:.0f} avgR={a['avg_r']:+.3f} "
              f"DD={a['max_dd_usd_per_oz']:.0f} losing_months={a['losing_months']}/{a['months']}")
    print("written", out)


if __name__ == "__main__":
    main()
