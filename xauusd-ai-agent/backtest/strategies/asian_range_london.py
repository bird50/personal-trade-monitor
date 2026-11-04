"""Asian range (server 02:00-10:00) traded at the London open, breakout or sweep-reclaim mode.

Rules (server wall clock, M5 closed bars only):

* Indicators updated every M5 bar: ``SessionRange(2, 10)``, ``RelativeVolume(20)`` on M5
  tick volume, M5 ``ATR(14)``; H1 ``ATR(14)`` fed from ``ctx.new["H1"]``.
* A day is usable once the range is complete and its width is within
  ``[width_min, width_max] x H1 ATR14`` (H1 ATR snapshotted when the range completes).
* Trade window: ``ctx.time_s`` time-of-day in [10:00, 13:00); at most one signal per server day
  (a signal is not emitted, and the day is not consumed, while a position is open).
* ``mode="breakout"``: LONG when close > range.high + 0.1 ATR, RVOL >= rvol_min and the bar closes
  in its top 30 % ((close-low) >= 0.7 (high-low)); SHORT mirrored.
  SL long = max(range_mid, close - 2.5 ATR), forced to at least 1.0 ATR from close;
  short = min(range_mid, close + 2.5 ATR) + spread, same minimum distance.
* ``mode="fade"``: from 10:00 track sweeps (bar low < range.low - 0.1 ATR -> sweep_low = min such low,
  mirrored for sweep_high) and the max RVOL of the window bars. LONG when the latest sweep is a low
  sweep, close > range.low + 0.05 ATR, close > open and max RVOL >= rvol_min; SHORT mirrored.
  SL long = sweep_low - 0.2 ATR; short = sweep_high + 0.2 ATR + spread; |close - sl| clamped to
  [1.0, 2.5] ATR.
* Common: spread_price <= 0.20 ATR14. TP from the net-RR helper so that the Paper planned net RR
  (fees and slippage included, estimated at the next-bar fill) equals ``rr``.
* Exits: breakeven at 0.7R locking 0.1R, time stop 60 M5 bars. ``atr`` = M5 ATR14, tag ``asia_<mode>``.
"""
from backtest.indicators import ATR, RelativeVolume, SessionRange, server_day

from .base import Strategy

SLIPPAGE = 0.05      # config/risk-xm-paper.json slippage_price_per_side
COMMISSION = 0.10    # config/risk-xm-paper.json commission_usd_per_unit_round_trip


def net_rr_tp(side, close, sl, spread, rr):
    """TP so that (reward - costs) / (risk + costs) == rr at an estimated next-open fill."""
    if side == "BUY":
        entry = close + spread + SLIPPAGE
        loss = entry - sl + SLIPPAGE + COMMISSION
        return entry + rr * loss + SLIPPAGE + COMMISSION
    entry = close - SLIPPAGE
    loss = sl - entry + SLIPPAGE + COMMISSION
    return entry - rr * loss - SLIPPAGE - COMMISSION


def clamp_stop(side, close, sl, atr, lo, hi):
    dist = min(max(abs(close - sl), lo * atr), hi * atr)
    return close - dist if side == "BUY" else close + dist


class AsianRangeLondon(Strategy):
    name = "asian_range_london"
    default_params = {
        "mode": "breakout", "rvol_min": 1.2, "width_min": 0.5, "width_max": 2.5, "rr": 1.1,
        "range_start_hour": 2, "range_end_hour": 10, "window_start_hour": 10, "window_end_hour": 13,
        "max_spread_atr": 0.20,
        "exits": {"breakeven_r": 0.7, "breakeven_lock_r": 0.1, "time_stop_bars": 60},
    }

    def __init__(self, params=None):
        super().__init__(params)
        p = self.params
        if p["mode"] not in ("breakout", "fade"):
            raise ValueError("mode must be 'breakout' or 'fade'")
        self.range = SessionRange(p["range_start_hour"], p["range_end_hour"])
        self.rvol = RelativeVolume(20)
        self.atr = ATR(14)
        self.h1_atr = ATR(14)
        self.day = None
        self.day_h1_atr = None      # H1 ATR snapshot when the range completed
        self.signalled_day = None
        self.sweep_low = self.sweep_high = None
        self.sweep_low_t = self.sweep_high_t = -1
        self.max_rvol = None
        self.stats = {"days_range": 0, "days_width_ok": 0}

    def on_bar(self, ctx):
        p, bar = self.params, ctx.bar
        for hbar in ctx.new.get("H1", []):
            self.h1_atr.update(hbar)
        rng = self.range.update(bar)
        rvol = self.rvol.update(bar)
        atr = self.atr.update(bar)
        day = server_day(bar)
        if day != self.day:
            self.day, self.day_h1_atr = day, None
            self.sweep_low = self.sweep_high = None
            self.sweep_low_t = self.sweep_high_t = -1
            self.max_rvol = None
        if rng is None or not rng["complete"] or rng["day"] != day:
            return None
        if self.day_h1_atr is None:
            if self.h1_atr.value is None:
                return None
            self.day_h1_atr = self.h1_atr.value
            self.stats["days_range"] += 1
            if p["width_min"] <= (rng["high"] - rng["low"]) / self.day_h1_atr <= p["width_max"]:
                self.stats["days_width_ok"] += 1
        tod = ctx.time_s % 86400
        if not p["window_start_hour"] * 3600 <= tod < p["window_end_hour"] * 3600 or atr is None:
            return None
        hi, lo = rng["high"], rng["low"]
        # Window state (fade) is tracked on every window bar regardless of filters.
        if rvol is not None:
            self.max_rvol = rvol if self.max_rvol is None else max(self.max_rvol, rvol)
        if bar["low"] < lo - 0.1 * atr:
            self.sweep_low = bar["low"] if self.sweep_low is None else min(self.sweep_low, bar["low"])
            self.sweep_low_t = ctx.time_s
        if bar["high"] > hi + 0.1 * atr:
            self.sweep_high = bar["high"] if self.sweep_high is None else max(self.sweep_high, bar["high"])
            self.sweep_high_t = ctx.time_s
        if self.signalled_day == day or ctx.position is not None:
            return None
        if not p["width_min"] <= (hi - lo) / self.day_h1_atr <= p["width_max"]:
            return None
        spread = ctx.spread_price()
        if spread > p["max_spread_atr"] * atr:
            return None
        close, o, h, l = bar["close"], bar["open"], bar["high"], bar["low"]
        side = sl = None
        if p["mode"] == "breakout":
            if rvol is None or rvol < p["rvol_min"] or h <= l:
                return None
            mid = (hi + lo) / 2
            if close > hi + 0.1 * atr and close - l >= 0.7 * (h - l):
                side, sl = "BUY", max(mid, close - 2.5 * atr)
                if close - sl < 1.0 * atr:
                    sl = close - 1.0 * atr
            elif close < lo - 0.1 * atr and h - close >= 0.7 * (h - l):
                side, sl = "SELL", min(mid, close + 2.5 * atr) + spread
                if sl - close < 1.0 * atr:
                    sl = close + 1.0 * atr
        else:
            if self.max_rvol is None or self.max_rvol < p["rvol_min"]:
                return None
            latest = "low" if self.sweep_low_t > self.sweep_high_t else "high" if self.sweep_high_t > self.sweep_low_t else None
            if latest == "low" and close > lo + 0.05 * atr and close > o:
                side = "BUY"
                sl = clamp_stop(side, close, self.sweep_low - 0.2 * atr, atr, 1.0, 2.5)
            elif latest == "high" and close < hi - 0.05 * atr and close < o:
                side = "SELL"
                sl = clamp_stop(side, close, self.sweep_high + 0.2 * atr + spread, atr, 1.0, 2.5)
        if side is None:
            return None
        self.signalled_day = day
        tp = net_rr_tp(side, close, sl, spread, p["rr"])
        return self.signal(side, sl, tp, atr=atr, tag="asia_" + p["mode"])
