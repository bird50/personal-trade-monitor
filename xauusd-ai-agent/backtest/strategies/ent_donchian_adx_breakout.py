"""First close out of a compact Donchian(20) base with rising M5 ADX, in a strong H1 trend.

* H1 EMA50/EMA200 and h1_close from ``ctx.new["H1"]`` only; strong_up/strong_down as in
  ``ent_macd_hist_hook`` (EMA gap >= gap_atr * ATR14(M5)).
* Donchian of the PRIOR 20 M5 bars (t-20..t-1): dc_high, dc_low, dc_mid, width.
* LONG at t: strong_up; close[t] > dc_high[t] and close[t-1] <= dc_high[t-1]; width <=
  width_max * ATR; adx[t] > adx[t-3] and 18 <= adx[t] <= 40; +DI > -DI; close > open;
  close - EMA20 <= 1.2 ATR. SHORT mirrors it. Same-side cooldown of 12 M5 bars after a signal.
* Session, Friday cutoff and vol gate as in ``ent_macd_hist_hook``.
* Stop: long d = close - (dc_mid - 0.2 ATR), short d = (dc_mid + 0.2 ATR + spread) - close,
  clamped to [1.1, 2.8] ATR. TP by the net-RR helper with max(0.60, spread).
"""
from collections import deque

from backtest.indicators import ADX, ATR, EMA

from .base import Strategy
from .rsi2_trend_pullback_r2 import entry_allowed, net_rr_tp

MIN_SPREAD = 0.60
DC_LEN, COOLDOWN = 20, 12


class EntDonchianAdxBreakout(Strategy):
    name = "ent_donchian_adx_breakout"
    default_params = {
        "width_max": 4, "gap_atr": 5, "rr": 2.0, "min_atr": 3.0, "max_spread_atr": 0.20,
        "start_hour": 2, "end_hour": 22, "exits": {},
    }

    def __init__(self, params=None):
        super().__init__(params)
        self.h1_fast, self.h1_slow, self.h1_close = EMA(50), EMA(200), None
        self.adx, self.ema20, self.atr = ADX(14), EMA(20), ATR(14)
        self.window = deque(maxlen=DC_LEN)  # prior bars, current excluded
        self.adx_hist = deque(maxlen=4)
        self.prev_dc, self.prev = None, None
        self.last_signal = {"BUY": None, "SELL": None}

    def on_bar(self, ctx):
        p, bar = self.params, ctx.bar
        for hbar in ctx.new.get("H1", []):
            self.h1_fast.update(hbar["close"])
            self.h1_slow.update(hbar["close"])
            self.h1_close = hbar["close"]
        adx = self.adx.update(bar)
        ema20 = self.ema20.update(bar["close"])
        atr = self.atr.update(bar)
        dc = None
        if len(self.window) == DC_LEN:
            dc = (max(b["high"] for b in self.window), min(b["low"] for b in self.window))
        prev_dc, prev = self.prev_dc, self.prev
        self.prev_dc, self.prev = dc, bar
        self.window.append(bar)
        self.adx_hist.append(None if adx is None else adx["adx"])
        fast, slow = self.h1_fast.value, self.h1_slow.value
        if ctx.position is not None or None in (dc, prev_dc, prev, ema20, atr, fast, slow, self.h1_close):
            return None
        if len(self.adx_hist) < 4 or None in self.adx_hist or None in (adx["plus_di"], adx["minus_di"]):
            return None
        if not entry_allowed(ctx, p["start_hour"], p["end_hour"]):
            return None
        spread = ctx.spread_price()
        cost_spread = max(MIN_SPREAD, spread)
        if atr < p["min_atr"] or cost_spread > p["max_spread_atr"] * atr:
            return None
        dc_high, dc_low = dc
        width, mid = dc_high - dc_low, (dc_high + dc_low) / 2
        a_now, a_back = self.adx_hist[-1], self.adx_hist[0]
        if width > p["width_max"] * atr or not (a_now > a_back and 18 <= a_now <= 40):
            return None
        close = bar["close"]
        side = None
        if (self.h1_close > fast > slow and fast - slow >= p["gap_atr"] * atr
                and close > dc_high and prev["close"] <= prev_dc[0] and adx["plus_di"] > adx["minus_di"]
                and close > bar["open"] and close - ema20 <= 1.2 * atr):
            side = "BUY"
        elif (self.h1_close < fast < slow and slow - fast >= p["gap_atr"] * atr
                and close < dc_low and prev["close"] >= prev_dc[1] and adx["minus_di"] > adx["plus_di"]
                and close < bar["open"] and ema20 - close <= 1.2 * atr):
            side = "SELL"
        if side is None:
            return None
        last = self.last_signal[side]
        if last is not None and ctx.index - last <= COOLDOWN:
            return None
        if side == "BUY":
            d = close - (mid - 0.2 * atr)
        else:
            d = (mid + 0.2 * atr + spread) - close
        d = min(max(d, 1.1 * atr), 2.8 * atr)
        sl = close - d if side == "BUY" else close + d
        self.last_signal[side] = ctx.index
        return self.signal(side, sl, net_rr_tp(side, close, sl, cost_spread, p["rr"]), atr=atr, tag="donchian_adx")
