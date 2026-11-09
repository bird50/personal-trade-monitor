"""Engulfing / pin-bar rejection at the M5 EMA20-EMA50 zone in a strong H1 trend (group 2).

COMMON block (shared by the ``ent_*`` entry studies):

* H1 EMA50/EMA200 and the last H1 close are fed only from ``ctx.new["H1"]``.
  strong_up = h1_close > EMA50 > EMA200 and EMA50 - EMA200 >= gap_atr * M5 ATR14;
  strong_down mirror.
* M5 ATR14 >= min_atr; max(0.60, spread) <= max_spread_atr * ATR14; server hour in
  [start_hour, end_hour); no Friday entries from 20:00 server; no new signal while a position is
  open (indicators keep updating on every M5 bar).
* Stop distance d (from the M5 close) clamped to [1.1, 2.8] x ATR14; TP by the net-RR helper with
  costs max(0.60, spread) + 2 x 0.05 slippage + 0.10 commission, so planned net RR = rr.

This strategy:

* M5 alignment long: EMA20 > EMA50 and EMA50[t] > EMA50[t-12] (short mirror).
* Zone touch long: low <= EMA20 + 0.1 ATR and low >= EMA50 - 0.5 ATR, close > EMA20.
* Pattern long: bullish engulfing (body >= 0.3 ATR) or bullish pin (range >= 0.8 ATR, lower wick
  >= 0.55 range and >= 2 x body, close in the top 40%). Short mirror.
* Arming: after a long signal, longs wait until a later bar has low > EMA20 + 0.5 ATR
  (short: high < EMA20 - 0.5 ATR).
* SL long = min(low[t], low[t-1]) - 0.25 ATR; short = max(high[t], high[t-1]) + 0.25 ATR + spread.
"""
from collections import deque

from backtest.indicators import ATR, EMA

from .base import Strategy

SLIP, COMMISSION, MIN_SPREAD = 0.05, 0.10, 0.60


def net_rr_tp(side, close, sl, spread, rr):
    """TP so that (|tp-close| - c) / (|close-sl| + c) = rr with c = max(0.60, spread) + 0.20."""
    c = max(MIN_SPREAD, spread) + 2 * SLIP + COMMISSION
    dist = rr * (abs(close - sl) + c) + c
    return close + dist if side == "BUY" else close - dist


def entry_allowed(ctx, start_hour, end_hour):
    hour = ctx.time_s % 86400 // 3600
    if not start_hour <= hour < end_hour:
        return False
    weekday = (ctx.bar["open_server_s"] // 86400 + 3) % 7
    return not (weekday == 4 and hour >= 20)


def bullish_pattern(bar, prev, atr):
    o, h, l, c = bar["open"], bar["high"], bar["low"], bar["close"]
    if (prev["close"] < prev["open"] and c > o and c >= prev["open"] and o <= prev["close"]
            and c - o >= 0.3 * atr):
        return "pa_engulf"
    rng = h - l
    wick = min(o, c) - l
    if rng >= 0.8 * atr and wick >= 0.55 * rng and wick >= 2 * abs(c - o) and c >= l + 0.6 * rng:
        return "pa_pin"
    return None


def bearish_pattern(bar, prev, atr):
    o, h, l, c = bar["open"], bar["high"], bar["low"], bar["close"]
    if (prev["close"] > prev["open"] and c < o and c <= prev["open"] and o >= prev["close"]
            and o - c >= 0.3 * atr):
        return "pa_engulf"
    rng = h - l
    wick = h - max(o, c)
    if rng >= 0.8 * atr and wick >= 0.55 * rng and wick >= 2 * abs(c - o) and c <= h - 0.6 * rng:
        return "pa_pin"
    return None


class EntEngulfPinEmaZone(Strategy):
    name = "ent_engulf_pin_ema_zone"
    default_params = {
        "gap_atr": 5, "rr": 2.0, "min_atr": 3.0, "max_spread_atr": 0.20,
        "start_hour": 2, "end_hour": 22, "exits": {},
    }

    def __init__(self, params=None):
        super().__init__(params)
        self.h1_fast, self.h1_slow, self.h1_close = EMA(50), EMA(200), None
        self.ema20, self.ema50, self.atr = EMA(20), EMA(50), ATR(14)
        self.ema50_hist = deque(maxlen=13)
        self.prev = None
        self.armed = {"BUY": True, "SELL": True}

    def on_bar(self, ctx):
        p, bar = self.params, ctx.bar
        for hbar in ctx.new.get("H1", []):
            self.h1_fast.update(hbar["close"])
            self.h1_slow.update(hbar["close"])
            self.h1_close = hbar["close"]
        e20, e50, atr = self.ema20.update(bar["close"]), self.ema50.update(bar["close"]), self.atr.update(bar)
        if e50 is not None:
            self.ema50_hist.append(e50)
        prev, self.prev = self.prev, bar
        if None in (e20, atr):
            return None
        # Re-arm on bars after the signal bar (the signal bar disarms below, after this check).
        if bar["low"] > e20 + 0.5 * atr:
            self.armed["BUY"] = True
        if bar["high"] < e20 - 0.5 * atr:
            self.armed["SELL"] = True
        fast, slow = self.h1_fast.value, self.h1_slow.value
        if ctx.position is not None or None in (e50, fast, slow, self.h1_close, prev) or len(self.ema50_hist) < 13:
            return None
        spread = ctx.spread_price()
        if atr < p["min_atr"] or max(MIN_SPREAD, spread) > p["max_spread_atr"] * atr:
            return None
        if not entry_allowed(ctx, p["start_hour"], p["end_hour"]):
            return None
        close, low, high, gap = bar["close"], bar["low"], bar["high"], p["gap_atr"] * atr
        e50_old = self.ema50_hist[0]
        side = tag = None
        if (self.armed["BUY"] and self.h1_close > fast > slow and fast - slow >= gap
                and e20 > e50 and e50 > e50_old
                and low <= e20 + 0.1 * atr and low >= e50 - 0.5 * atr and close > e20):
            tag = bullish_pattern(bar, prev, atr)
            side = "BUY" if tag else None
        elif (self.armed["SELL"] and self.h1_close < fast < slow and slow - fast >= gap
                and e20 < e50 and e50 < e50_old
                and high >= e20 - 0.1 * atr and high <= e50 + 0.5 * atr and close < e20):
            tag = bearish_pattern(bar, prev, atr)
            side = "SELL" if tag else None
        if side is None:
            return None
        if side == "BUY":
            raw = close - (min(low, prev["low"]) - 0.25 * atr)
        else:
            raw = (max(high, prev["high"]) + 0.25 * atr + spread) - close
        d = min(max(raw, 1.1 * atr), 2.8 * atr)
        sl = close - d if side == "BUY" else close + d
        self.armed[side] = False
        return self.signal(side, sl, net_rr_tp(side, close, sl, spread, p["rr"]), atr=atr, tag=tag)
