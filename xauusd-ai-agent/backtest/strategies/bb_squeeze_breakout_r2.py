"""Bollinger-inside-Keltner squeeze release breakout, round 2.

Same rules as ``bb_squeeze_breakout`` (round 1) plus two parameters:

* M5 Bollinger(20, 2.0), Keltner(20, 20, kc_mult) and ATR14 update every M5 bar; H1 EMA50 is
  fed only from ``ctx.new["H1"]`` (latest closed H1 close vs EMA50).
* squeeze_on = bb.upper < kc.upper and bb.lower > kc.lower. On the first squeeze_off bar after
  >= min_squeeze consecutive squeeze_on bars a release window of ``window_bars`` bars opens
  (the release bar is bar 1) and the count resets. One signal per window.
* In the window: LONG when close > bb.upper, close > open and (h1_filter off or
  h1_close > h1_ema50); SHORT when close < bb.lower, close < open and (h1_filter off or
  h1_close < h1_ema50).
* Filters: server hour (of ``ctx.time_s``) in [start_hour, end_hour); no Friday entries from
  20:00 server; spread <= max_spread_atr*ATR14; no new signal while a position is open.
* ``blackout_hours`` = [start, end) server hours of ``ctx.time_s`` in which no new signal is
  emitted; indicators and the window countdown keep running ([] disables it).
* SL long = bb.middle - 0.3*ATR14, short = bb.middle + 0.3*ATR14 + spread, stop distance from
  close clamped to [1.0, 2.5]*ATR14. TP by the net-RR helper with ``rr``. Exits via ``exits``
  (default breakeven 0.6R, lock 0.1R, time stop 36 M5 bars).
"""
from backtest.indicators import ATR, EMA, Bollinger, Keltner

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


def server_hour(ctx):
    return ctx.time_s % 86400 // 3600


def entry_allowed(ctx, start_hour, end_hour):
    hour = server_hour(ctx)
    if not start_hour <= hour < end_hour:
        return False
    weekday = (ctx.bar["open_server_s"] // 86400 + 3) % 7
    return not (weekday == 4 and hour >= 20)


def in_blackout(ctx, blackout):
    return bool(blackout) and blackout[0] <= server_hour(ctx) < blackout[1]


class BBSqueezeBreakoutR2(Strategy):
    name = "bb_squeeze_breakout_r2"
    default_params = {
        "min_squeeze": 12, "kc_mult": 1.5, "rr": 1.4, "h1_filter": True, "window_bars": 3,
        "blackout_hours": [], "start_hour": 2, "end_hour": 22, "max_spread_atr": 0.20,
        "exits": {"breakeven_r": 0.6, "breakeven_lock_r": 0.1, "time_stop_bars": 36},
    }

    def __init__(self, params=None):
        super().__init__(params)
        p = self.params
        if p["blackout_hours"] and len(p["blackout_hours"]) != 2:
            raise ValueError("blackout_hours must be [] or [start, end)")
        self.h1_ema, self.h1_close = EMA(50), None
        self.bb, self.kc, self.atr = Bollinger(20, 2.0), Keltner(20, 20, p["kc_mult"]), ATR(14)
        self.squeeze_count = 0
        self.window_left = 0

    def on_bar(self, ctx):
        p, bar = self.params, ctx.bar
        for hbar in ctx.new.get("H1", []):
            self.h1_ema.update(hbar["close"])
            self.h1_close = hbar["close"]
        bb, kc, atr = self.bb.update(bar), self.kc.update(bar), self.atr.update(bar)
        if bb is None or kc is None:
            self.squeeze_count, self.window_left = 0, 0
            return None
        if bb["upper"] < kc["upper"] and bb["lower"] > kc["lower"]:
            self.squeeze_count += 1
        else:
            if self.squeeze_count >= p["min_squeeze"]:
                self.window_left = p["window_bars"]
            self.squeeze_count = 0
        if self.window_left <= 0:
            return None
        self.window_left -= 1
        h1_ema = self.h1_ema.value
        if ctx.position is not None or atr is None or (p["h1_filter"] and h1_ema is None):
            return None
        if in_blackout(ctx, p["blackout_hours"]):
            return None
        spread = ctx.spread_price()
        if spread > p["max_spread_atr"] * atr or not entry_allowed(ctx, p["start_hour"], p["end_hour"]):
            return None
        close = bar["close"]
        side = None
        if close > bb["upper"] and close > bar["open"] and (not p["h1_filter"] or self.h1_close > h1_ema):
            side = "BUY"
        elif close < bb["lower"] and close < bar["open"] and (not p["h1_filter"] or self.h1_close < h1_ema):
            side = "SELL"
        if side is None:
            return None
        if side == "BUY":
            dist = min(max(close - (bb["middle"] - 0.3 * atr), 1.0 * atr), 2.5 * atr)
            sl = close - dist
        else:
            dist = min(max((bb["middle"] + 0.3 * atr + spread) - close, 1.0 * atr), 2.5 * atr)
            sl = close + dist
        self.window_left = 0  # one signal per window
        return self.signal(side, sl, net_rr_tp(side, close, sl, spread, p["rr"]), atr=atr, tag="squeeze")
