"""Connors-style RSI(2) pullback in the H1 trend, round 2.

Same rules as ``rsi2_trend_pullback`` (round 1) with rsi_low=5, confirm=true, rr=1.1 as
defaults plus a cost-to-risk filter and an hour blackout:

* H1 EMA50/EMA200 are fed only from ``ctx.new["H1"]``; M5 RSI(2), EMA200 and ATR14 update on
  every M5 bar, including while a position is open.
* LONG bias: h1_close > h1_ema200, h1_ema50 > h1_ema200 and M5 close > m5_ema200. SHORT mirror.
* ``confirm=true``: long when the previous bar's RSI2 <= rsi_low and the current close is above
  the previous high (short: previous RSI2 >= 100 - rsi_low and close below the previous low).
  ``confirm=false``: current RSI2 <= rsi_low (short >= 100 - rsi_low).
* Re-arm: after a signal, the same side stays disarmed until RSI2 crosses back over 50.
* Filters: server hour (of ``ctx.time_s``) in [start_hour, end_hour); no Friday entries from
  20:00 server; spread <= max_spread_atr*ATR14; no new signal while a position is open.
* ``blackout_hours`` = [start, end) server hours in which no new signal is emitted ([] = off).
* ``cost_r_max``: with e = close + spread + 0.05 (BUY) or close - 0.05 (SELL),
  cost_r = (spread + 0.20) / (|e - sl| + 0.15); a setup with cost_r > cost_r_max is skipped
  and does not disarm its side (null = off).
* SL long = close - sl_atr*ATR14, short = close + sl_atr*ATR14 + spread. TP by the net-RR
  helper with ``rr``. Exits via ``exits`` (default breakeven 0.6R, lock 0.1R, time stop 24).

``self.filter_stats`` counts setups dropped by the blackout and the cost filter, and
``self.cost_filtered_times`` keeps their signal times (diagnostics only).
"""
from collections import Counter

from backtest.indicators import ATR, EMA, RSI

from .base import Strategy

SLIP, COMMISSION = 0.05, 0.10


def net_rr_tp(side, close, sl, spread, rr):
    if side == "BUY":
        e = close + spread + SLIP
        loss = (e - sl) + SLIP + COMMISSION
        return e + rr * loss + SLIP + COMMISSION + SLIP
    e = close - SLIP
    loss = (sl - e) + SLIP + COMMISSION
    return e - rr * loss - SLIP - COMMISSION - SLIP


def cost_ratio(side, close, sl, spread):
    e = close + spread + SLIP if side == "BUY" else close - SLIP
    return (spread + 2 * SLIP + COMMISSION) / (abs(e - sl) + SLIP + COMMISSION)


def server_hour(ctx):
    return ctx.time_s % 86400 // 3600


def entry_allowed(ctx, start_hour, end_hour):
    hour = server_hour(ctx)
    if not start_hour <= hour < end_hour:
        return False
    weekday = (ctx.bar["open_server_s"] // 86400 + 3) % 7
    return not (weekday == 4 and hour >= 20)


class RSI2TrendPullbackR2(Strategy):
    name = "rsi2_trend_pullback_r2"
    default_params = {
        "rsi_low": 5, "confirm": True, "sl_atr": 1.8, "rr": 1.1,
        "cost_r_max": None, "blackout_hours": [],
        "start_hour": 3, "end_hour": 22, "max_spread_atr": 0.20,
        "exits": {"breakeven_r": 0.6, "breakeven_lock_r": 0.1, "time_stop_bars": 24},
    }

    def __init__(self, params=None):
        super().__init__(params)
        p = self.params
        if p["blackout_hours"] and len(p["blackout_hours"]) != 2:
            raise ValueError("blackout_hours must be [] or [start, end)")
        self.h1_fast, self.h1_slow, self.h1_close = EMA(50), EMA(200), None
        self.rsi, self.ema200, self.atr = RSI(2), EMA(200), ATR(14)
        self.prev, self.prev_rsi = None, None
        self.armed = {"BUY": True, "SELL": True}
        self.filter_stats = Counter()
        self.cost_filtered_times = []

    def on_bar(self, ctx):
        p, bar = self.params, ctx.bar
        for hbar in ctx.new.get("H1", []):
            self.h1_fast.update(hbar["close"])
            self.h1_slow.update(hbar["close"])
            self.h1_close = hbar["close"]
        rsi = self.rsi.update(bar)
        ema200 = self.ema200.update(bar["close"])
        atr = self.atr.update(bar)
        prev, prev_rsi = self.prev, self.prev_rsi
        self.prev, self.prev_rsi = bar, rsi
        if rsi is not None:
            if rsi > 50:
                self.armed["BUY"] = True
            if rsi < 50:
                self.armed["SELL"] = True
        fast, slow = self.h1_fast.value, self.h1_slow.value
        if ctx.position is not None or None in (rsi, ema200, atr, fast, slow, prev, prev_rsi):
            return None
        spread = ctx.spread_price()
        if spread > p["max_spread_atr"] * atr or not entry_allowed(ctx, p["start_hour"], p["end_hour"]):
            return None
        close, low, high = bar["close"], p["rsi_low"], 100 - p["rsi_low"]
        side = None
        if self.h1_close > slow and fast > slow and close > ema200 and self.armed["BUY"]:
            if (prev_rsi <= low and close > prev["high"]) if p["confirm"] else rsi <= low:
                side = "BUY"
        elif self.h1_close < slow and fast < slow and close < ema200 and self.armed["SELL"]:
            if (prev_rsi >= high and close < prev["low"]) if p["confirm"] else rsi >= high:
                side = "SELL"
        if side is None:
            return None
        hour = server_hour(ctx)
        if p["blackout_hours"] and p["blackout_hours"][0] <= hour < p["blackout_hours"][1]:
            self.filter_stats["blackout"] += 1
            return None
        sl = close - p["sl_atr"] * atr if side == "BUY" else close + p["sl_atr"] * atr + spread
        if p["cost_r_max"] is not None and cost_ratio(side, close, sl, spread) > p["cost_r_max"]:
            self.filter_stats["cost_filtered"] += 1
            self.cost_filtered_times.append(ctx.time_s)
            return None
        self.armed[side] = False
        return self.signal(side, sl, net_rr_tp(side, close, sl, spread, p["rr"]), atr=atr, tag="rsi2")
