"""StochRSI turn out of an oversold/overbought pullback, H1 ADX/DI trend strength.

* H1 EMA50/EMA200/ADX14 are fed only from ``ctx.new["H1"]``.
  up = h1_close > h1_ema50 > h1_ema200, h1 ADX >= adx_min and +DI > -DI; down mirrors it.
* M5 StochRSI(14,14,3,3), EMA50, ATR14, Lowest(low,6)/Highest(high,6) update on every bar.
* Long arm: set on a bar with %K <= os_level; cleared after a long signal or on a bar with
  %K > 50 that produced no signal. LONG at t: up; armed; %K crosses above %D (k[t-1] < d[t-1]
  and k[t] > d[t]); k[t] <= 50; close > M5 EMA50; close > open. SHORT mirrors it
  (arm on %K >= 100 - os_level, clear on %K < 50).
* Session, Friday cutoff and vol gate (ATR14 >= min_atr, max(0.60, spread) <= max_spread_atr*ATR)
  as in ``ent_macd_hist_hook``; same stop (Lowest6/Highest6 +-0.2 ATR, clamped 1.1-2.8 ATR) and
  net-RR TP.
"""
from backtest.indicators import ADX, ATR, EMA, Highest, Lowest, StochRSI

from .base import Strategy
from .rsi2_trend_pullback_r2 import entry_allowed, net_rr_tp

MIN_SPREAD = 0.60


class EntStochRsiAdxTurn(Strategy):
    name = "ent_stochrsi_adx_turn"
    default_params = {
        "adx_min": 25, "os_level": 20, "rr": 2.0, "min_atr": 3.0, "max_spread_atr": 0.20,
        "start_hour": 2, "end_hour": 22, "exits": {},
    }

    def __init__(self, params=None):
        super().__init__(params)
        self.h1_fast, self.h1_slow, self.h1_adx, self.h1_close = EMA(50), EMA(200), ADX(14), None
        self.stoch, self.ema50, self.atr = StochRSI(14, 14, 3, 3), EMA(50), ATR(14)
        self.lowest, self.highest = Lowest(6), Highest(6)
        self.prev_kd = None
        self.arm = {"BUY": False, "SELL": False}

    def _setup(self, ctx, k, d, ema50, atr, low6, high6):
        p, bar = self.params, ctx.bar
        prev_kd = self.prev_kd
        fast, slow, adx = self.h1_fast.value, self.h1_slow.value, self.h1_adx.value
        if ctx.position is not None or prev_kd is None or None in (d, prev_kd[1], ema50, atr, low6, high6, fast, slow, self.h1_close):
            return None
        if adx is None or None in (adx["adx"], adx["plus_di"], adx["minus_di"]):
            return None
        if not entry_allowed(ctx, p["start_hour"], p["end_hour"]):
            return None
        spread = ctx.spread_price()
        if atr < p["min_atr"] or max(MIN_SPREAD, spread) > p["max_spread_atr"] * atr:
            return None
        close = bar["close"]
        pk, pd = prev_kd
        strong = adx["adx"] >= p["adx_min"]
        if (self.h1_close > fast > slow and strong and adx["plus_di"] > adx["minus_di"] and self.arm["BUY"]
                and pk < pd and k > d and k <= 50 and close > ema50 and close > bar["open"]):
            return "BUY"
        if (self.h1_close < fast < slow and strong and adx["minus_di"] > adx["plus_di"] and self.arm["SELL"]
                and pk > pd and k < d and k >= 50 and close < ema50 and close < bar["open"]):
            return "SELL"
        return None

    def on_bar(self, ctx):
        p, bar = self.params, ctx.bar
        for hbar in ctx.new.get("H1", []):
            self.h1_fast.update(hbar["close"])
            self.h1_slow.update(hbar["close"])
            self.h1_adx.update(hbar)
            self.h1_close = hbar["close"]
        st = self.stoch.update(bar)
        ema50 = self.ema50.update(bar["close"])
        atr = self.atr.update(bar)
        low6, high6 = self.lowest.update(bar), self.highest.update(bar)
        if st is None:
            self.prev_kd = None
            return None
        k, d = st["k"], st["d"]
        if k <= p["os_level"]:
            self.arm["BUY"] = True
        if k >= 100 - p["os_level"]:
            self.arm["SELL"] = True
        side = self._setup(ctx, k, d, ema50, atr, low6, high6)
        self.prev_kd = (k, d)
        if side is None:
            if k > 50:
                self.arm["BUY"] = False
            if k < 50:
                self.arm["SELL"] = False
            return None
        self.arm[side] = False
        close, spread = bar["close"], ctx.spread_price()
        if side == "BUY":
            dist = close - (low6 - 0.2 * atr)
        else:
            dist = (high6 + 0.2 * atr + spread) - close
        dist = min(max(dist, 1.1 * atr), 2.8 * atr)
        sl = close - dist if side == "BUY" else close + dist
        tp = net_rr_tp(side, close, sl, max(MIN_SPREAD, spread), p["rr"])
        return self.signal(side, sl, tp, atr=atr, tag="stochrsi_turn")
