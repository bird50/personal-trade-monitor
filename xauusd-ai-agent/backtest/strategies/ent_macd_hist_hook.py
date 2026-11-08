"""MACD histogram hook at the end of a pullback inside a strong H1 trend.

* H1 EMA50/EMA200 and h1_close are fed only from ``ctx.new["H1"]``.
  strong_up = h1_close > h1_ema50 > h1_ema200 and (h1_ema50 - h1_ema200) >= gap_atr * ATR14(M5).
  strong_down mirrors it.
* M5 MACD(12,26,9), ATR14, Lowest(low,6)/Highest(high,6) update on every bar (also in position).
* LONG at bar t: strong_up; MACD line > 0; at least 3 consecutive histogram bars < 0 ending at
  t-1; hook hist[t] > hist[t-1] and hist[t-1] <= hist[t-2]; close > open and close > previous
  high; long armed. Long disarms after a signal and re-arms on a bar with hist >= 0.
  SHORT mirrors it (re-arm on hist <= 0).
* Session [start_hour, end_hour) server hour, no Friday entries from 20:00; ATR14 >= min_atr and
  max(0.60, spread) <= max_spread_atr * ATR14.
* Stop: long d = close - (Lowest6 - 0.2 ATR), short d = (Highest6 + 0.2 ATR + spread) - close,
  clamped to [1.1, 2.8] ATR. TP by the net-RR helper with max(0.60, spread).
"""
from collections import deque

from backtest.indicators import ATR, EMA, MACD, Highest, Lowest

from .base import Strategy
from .rsi2_trend_pullback_r2 import entry_allowed, net_rr_tp

MIN_SPREAD = 0.60


class EntMacdHistHook(Strategy):
    name = "ent_macd_hist_hook"
    default_params = {
        "gap_atr": 5, "rr": 2.0, "min_atr": 3.0, "max_spread_atr": 0.20,
        "start_hour": 2, "end_hour": 22, "exits": {},
    }

    def __init__(self, params=None):
        super().__init__(params)
        self.h1_fast, self.h1_slow, self.h1_close = EMA(50), EMA(200), None
        self.macd, self.atr = MACD(12, 26, 9), ATR(14)
        self.lowest, self.highest = Lowest(6), Highest(6)
        self.hist = deque(maxlen=3)
        self.neg_run = self.pos_run = 0  # runs ending at the previous bar
        self.prev = None
        self.armed = {"BUY": True, "SELL": True}

    def on_bar(self, ctx):
        p, bar = self.params, ctx.bar
        for hbar in ctx.new.get("H1", []):
            self.h1_fast.update(hbar["close"])
            self.h1_slow.update(hbar["close"])
            self.h1_close = hbar["close"]
        macd = self.macd.update(bar)
        atr = self.atr.update(bar)
        low6, high6 = self.lowest.update(bar), self.highest.update(bar)
        prev, self.prev = self.prev, bar
        neg_run, pos_run = self.neg_run, self.pos_run
        hist = None if macd is None or macd["signal"] is None else macd["histogram"]
        if hist is None:
            self.hist.clear()
            self.neg_run = self.pos_run = 0
            return None
        self.hist.append(hist)
        self.neg_run = neg_run + 1 if hist < 0 else 0
        self.pos_run = pos_run + 1 if hist > 0 else 0
        if hist >= 0:
            self.armed["BUY"] = True
        if hist <= 0:
            self.armed["SELL"] = True
        fast, slow = self.h1_fast.value, self.h1_slow.value
        if ctx.position is not None or None in (atr, low6, high6, fast, slow, self.h1_close, prev) or len(self.hist) < 3:
            return None
        if not entry_allowed(ctx, p["start_hour"], p["end_hour"]):
            return None
        spread = ctx.spread_price()
        cost_spread = max(MIN_SPREAD, spread)
        if atr < p["min_atr"] or cost_spread > p["max_spread_atr"] * atr:
            return None
        h0, h1, h2 = self.hist[-1], self.hist[-2], self.hist[-3]
        close, line = bar["close"], macd["macd"]
        side = None
        if (self.h1_close > fast > slow and fast - slow >= p["gap_atr"] * atr and self.armed["BUY"]
                and line > 0 and neg_run >= 3 and h0 > h1 and h1 <= h2
                and close > bar["open"] and close > prev["high"]):
            side = "BUY"
        elif (self.h1_close < fast < slow and slow - fast >= p["gap_atr"] * atr and self.armed["SELL"]
                and line < 0 and pos_run >= 3 and h0 < h1 and h1 >= h2
                and close < bar["open"] and close < prev["low"]):
            side = "SELL"
        if side is None:
            return None
        if side == "BUY":
            d = close - (low6 - 0.2 * atr)
        else:
            d = (high6 + 0.2 * atr + spread) - close
        d = min(max(d, 1.1 * atr), 2.8 * atr)
        sl = close - d if side == "BUY" else close + d
        self.armed[side] = False
        return self.signal(side, sl, net_rr_tp(side, close, sl, cost_spread, p["rr"]), atr=atr, tag="macd_hook")
