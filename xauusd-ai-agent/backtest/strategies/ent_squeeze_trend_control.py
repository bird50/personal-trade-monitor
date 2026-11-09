"""CONTROL: Bollinger-inside-Keltner squeeze release breakout with a strong H1 trend filter.

Variant of ``bb_squeeze_breakout_r2``:

* H1 EMA50/EMA200 are fed only from ``ctx.new["H1"]``; M5 Bollinger(20, 2.0),
  Keltner(20, 20, 1.5) and ATR14 update on every M5 bar, including while a position is open.
* strong_up: h1_close > h1_ema50 > h1_ema200 and (h1_ema50 - h1_ema200) >= gap_atr * M5 ATR14;
  strong_down is the mirror (replaces the r2 filter h1_close vs EMA50).
* squeeze_on = bb.upper < kc.upper and bb.lower > kc.lower. On the first squeeze_off bar after
  >= min_squeeze consecutive squeeze_on bars a release window of ``window_bars`` bars opens
  (release bar = bar 1) and the count resets; BB/KC None resets count and window. The
  countdown keeps running in a position / outside the session. One signal per window.
* In the window: LONG when strong_up, close > bb.upper and close > open; SHORT when
  strong_down, close < bb.lower and close < open.
* Filters: M5 ATR14 >= min_atr, spread <= max_spread_atr*ATR14, server hour in
  [start_hour, end_hour), no Friday entries from 20:00 server, no signal while in a position.
* SL: long d = close - (bb.middle - 0.3*atr), short d = (bb.middle + 0.3*atr + spread) - close,
  clamped to [1.1, 2.8]*ATR14. TP by the net-RR helper with max(0.60, spread) and ``rr``.
  Default exits {} (no breakeven/time stop).
"""
from backtest.indicators import ATR, EMA, Bollinger, Keltner

from .base import Strategy

SLIP, COMMISSION, MIN_SPREAD = 0.05, 0.10, 0.60


def net_rr_tp(side, close, sl, spread, rr):
    """TP with live net RR == rr at the signal close: costs c = max(0.60, spread) + 2*slip +
    commission, loss = |close - sl| + c, reward = |tp - close| - c. The fill-time recheck
    (loss + spread + slip, reward - slip - commission) then stays >= rr."""
    c = max(MIN_SPREAD, spread) + 2 * SLIP + COMMISSION
    move = rr * (abs(close - sl) + c) + c
    return close + move if side == "BUY" else close - move


def server_hour(ctx):
    return ctx.time_s % 86400 // 3600


def entry_allowed(ctx, start_hour, end_hour):
    hour = server_hour(ctx)
    if not start_hour <= hour < end_hour:
        return False
    weekday = (ctx.bar["open_server_s"] // 86400 + 3) % 7
    return not (weekday == 4 and hour >= 20)


class EntSqueezeTrendControl(Strategy):
    name = "ent_squeeze_trend_control"
    default_params = {
        "min_squeeze": 12, "window_bars": 3, "gap_atr": 5, "rr": 2.5, "min_atr": 3.0,
        "max_spread_atr": 0.20, "start_hour": 2, "end_hour": 22, "exits": {},
    }

    def __init__(self, params=None):
        super().__init__(params)
        self.h1_fast, self.h1_slow, self.h1_close = EMA(50), EMA(200), None
        self.bb, self.kc, self.atr = Bollinger(20, 2.0), Keltner(20, 20, 1.5), ATR(14)
        self.squeeze_count = 0
        self.window_left = 0

    def on_bar(self, ctx):
        p, bar = self.params, ctx.bar
        for hbar in ctx.new.get("H1", []):
            self.h1_fast.update(hbar["close"])
            self.h1_slow.update(hbar["close"])
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
        fast, slow = self.h1_fast.value, self.h1_slow.value
        if ctx.position is not None or None in (atr, fast, slow, self.h1_close):
            return None
        spread = ctx.spread_price()
        if atr < p["min_atr"] or spread > p["max_spread_atr"] * atr:
            return None
        if not entry_allowed(ctx, p["start_hour"], p["end_hour"]):
            return None
        close, gap = bar["close"], p["gap_atr"] * atr
        strong_up = self.h1_close > fast > slow and fast - slow >= gap
        strong_down = self.h1_close < fast < slow and slow - fast >= gap
        side = None
        if strong_up and close > bb["upper"] and close > bar["open"]:
            side = "BUY"
        elif strong_down and close < bb["lower"] and close < bar["open"]:
            side = "SELL"
        if side is None:
            return None
        if side == "BUY":
            d = close - (bb["middle"] - 0.3 * atr)
        else:
            d = (bb["middle"] + 0.3 * atr + spread) - close
        d = min(max(d, 1.1 * atr), 2.8 * atr)
        sl = close - d if side == "BUY" else close + d
        self.window_left = 0  # one signal per window
        return self.signal(side, sl, net_rr_tp(side, close, sl, spread, p["rr"]), atr=atr, tag="squeeze_ctl")
