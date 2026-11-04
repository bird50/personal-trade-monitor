"""Trade statistics and the fixed in-sample / out-of-sample split.

R is net USD P&L divided by the planned loss at fill (stop distance + exit slippage +
commission, the Paper ``planned_risk_usd``). Realized RR = average winning R ÷ average
losing |R|. Sessions use the broker server wall clock (XM server time follows the New York
close, so these buckets stay aligned through DST without an assumed UTC offset).
"""
from collections import defaultdict
from datetime import datetime, timezone

IN_SAMPLE_MONTHS = 8
OUT_OF_SAMPLE_MONTHS = 4
# [start_hour, end_hour) on the server clock.
SESSIONS = {"asia": (0, 10), "london": (10, 15), "new_york": (15, 24)}


def session_of(server_s: int) -> str:
    hour = server_s % 86400 // 3600
    return next(name for name, (start, end) in SESSIONS.items() if start <= hour < end)


def add_months(server_s: int, months: int) -> int:
    """Calendar arithmetic on the server wall clock (no time-zone conversion)."""
    moment = datetime.fromtimestamp(server_s, timezone.utc)
    month = moment.month - 1 + months
    year, month = moment.year + month // 12, month % 12 + 1
    day = min(moment.day, [31, 29 if year % 4 == 0 and (year % 100 or year % 400 == 0) else 28,
                           31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1])
    return int(moment.replace(year=year, month=month, day=day).timestamp())


def split_point(first_s: int, months=IN_SAMPLE_MONTHS) -> int:
    """Start of out-of-sample: the server-day boundary ``months`` after the first bar's day."""
    return add_months(first_s // 86400 * 86400, months)


def segments(first_s: int, last_s: int):
    split = split_point(first_s)
    return {"in_sample": (first_s, min(split, last_s + 60)), "out_of_sample": (split, last_s + 60), "split_server_s": split}


def summarize(trades: list[dict], start_s: int | None = None, end_s: int | None = None,
              initial_balance: float = 10000.0, max_drawdown_pct: float | None = None) -> dict:
    n = len(trades)
    wins = [t for t in trades if t["net_pnl_usd"] > 0]
    losses = [t for t in trades if t["net_pnl_usd"] <= 0]
    avg_win_r = sum(t["r"] for t in wins) / len(wins) if wins else 0.0
    avg_loss_r = sum(t["r"] for t in losses) / len(losses) if losses else 0.0
    gross_win = sum(t["net_pnl_usd"] for t in wins)
    gross_loss = -sum(t["net_pnl_usd"] for t in losses)
    if start_s is None and trades:
        start_s = trades[0]["signal_time_s"]
    if end_s is None and trades:
        end_s = trades[-1]["exit_time_s"]
    weeks = max((end_s - start_s) / 604800, 1e-9) if start_s is not None and end_s is not None else None
    net = sum(t["net_pnl_usd"] for t in trades)
    if max_drawdown_pct is None:
        max_drawdown_pct = drawdown_pct(trades, initial_balance)
    exit_reasons = defaultdict(int)
    for t in trades:
        exit_reasons[t["exit_reason"]] += 1
    flagged = sum(1 for t in trades if t.get("live_risk_reasons"))
    return {
        "trades": n, "wins": len(wins), "losses": len(losses),
        "win_rate": len(wins) / n if n else 0.0,
        "avg_win_r": avg_win_r, "avg_loss_r": avg_loss_r,
        "planned_net_rr": sum(t["planned_net_rr"] for t in trades) / n if n else 0.0,
        "planned_gross_rr": sum(t["planned_gross_rr"] for t in trades) / n if n else 0.0,
        "realized_rr": avg_win_r / abs(avg_loss_r) if avg_loss_r else (float("inf") if wins else 0.0),
        "expectancy_r": sum(t["r"] for t in trades) / n if n else 0.0,
        "total_r": sum(t["r"] for t in trades),
        "profit_factor": gross_win / gross_loss if gross_loss else (float("inf") if gross_win else 0.0),
        "net_pnl_usd": net, "return_pct": net / initial_balance * 100 if initial_balance else 0.0,
        "max_drawdown_pct": max_drawdown_pct,
        "trades_per_week": n / weeks if weeks else 0.0, "weeks": weeks,
        "avg_bars_held_m5": sum(t["bars_held_m5"] for t in trades) / n if n else 0.0,
        "exit_reasons": dict(exit_reasons),
        "trades_failing_live_risk": flagged,
    }


def drawdown_pct(trades, initial_balance):
    peak, worst = initial_balance, 0.0
    for t in trades:
        low = t.get("equity_low", t["balance_after"])
        worst = max(worst, (peak - min(low, t["balance_after"])) / peak * 100 if peak > 0 else 0.0)
        peak = max(peak, t["balance_after"])
    return worst


def breakdown(trades, key):
    groups = defaultdict(list)
    for t in trades:
        groups[key(t)].append(t)
    out = {}
    for name, items in sorted(groups.items()):
        s = summarize(items, max_drawdown_pct=float("nan"))
        out[name] = {k: s[k] for k in ("trades", "win_rate", "expectancy_r", "realized_rr", "profit_factor", "net_pnl_usd")}
    return out


def report(result: dict) -> dict:
    trades = result["trades"]
    start = result["trade_from_s"] if result["trade_from_s"] is not None else result["first_m1_s"]
    end = result["trade_until_s"] if result["trade_until_s"] is not None else result["last_m1_s"] + 60
    summary = summarize(trades, start, end, result["initial_balance_usd"], result["max_drawdown_pct"])
    summary["by_session"] = breakdown(trades, lambda t: session_of(t["signal_time_s"]))
    summary["by_side"] = breakdown(trades, lambda t: t["side"])
    summary["signal_stats"] = result["signal_stats"]
    return summary
