"""H1 trend + M5 Keltner pullback, filtered by H1 Kaufman Efficiency Ratio.

Rules (all on closed bars):

* H1 EMA50/EMA200/ER(10) are fed only from ``ctx.new["H1"]`` (closed H1 bars); the latest
  closed values are used. M5 Keltner(20, 10, 1.5), EMA50, ATR14, Lowest/Highest(6) update on
  every M5 bar, including while a position is open.
* LONG bias: h1_close > h1_ema50 > h1_ema200, h1_er >= er_min, kc.middle > m5_ema50.
  SHORT mirror.
* Pullback (long): among the last 6 M5 bars (current included) some bar has
  low <= middle - depth*atr (that bar's Keltner values) and no bar closed below
  lower - 0.5*atr. The pullback bar must be newer than the last long signal (one signal per
  pullback). Trigger: close > middle, close > open, close > previous high. Short mirror.
* Filters: server hour of ``ctx.time_s`` in [3, 21); no Friday entries from 20:00 server;
  spread <= 0.20*ATR14; no new signal while a position is open.
* SL long = Lowest(6) - 0.2*ATR14, short = Highest(6) + 0.2*ATR14 + spread, with the stop
  distance from close clamped to [1.0, 2.5]*ATR14. TP by the net-RR helper with ``rr``.
"""
from collections import deque

from backtest.indicators import ATR, EMA, EfficiencyRatio, Highest, Keltner, Lowest

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


def entry_allowed(ctx, start_hour, end_hour):
    hour = ctx.time_s % 86400 // 3600
    if not start_hour <= hour < end_hour:
        return False
    weekday = (ctx.bar["open_server_s"] // 86400 + 3) % 7
    return not (weekday == 4 and hour >= 20)


class KeltnerERPullback(Strategy):
    name = "keltner_er_pullback"
    default_params = {
        "er_min": 0.25, "depth": 0.5, "rr": 1.1, "lookback": 6,
        "start_hour": 3, "end_hour": 21, "max_spread_atr": 0.20,
        "exits": {"breakeven_r": 0.7, "breakeven_lock_r": 0.1, "time_stop_bars": 48},
    }

    def __init__(self, params=None):
        super().__init__(params)
        p = self.params
        self.h1_fast, self.h1_slow, self.h1_er = EMA(50), EMA(200), EfficiencyRatio(10)
        self.h1_close = None
        self.kc = Keltner(20, 10, 1.5)
        self.ema50, self.atr = EMA(50), ATR(14)
        self.lowest, self.highest = Lowest(p["lookback"]), Highest(p["lookback"])
        self.recent = deque(maxlen=p["lookback"])
        self.prev = None
        self.last_signal = {"BUY": -1, "SELL": -1}

    def on_bar(self, ctx):
        p, bar = self.params, ctx.bar
        for hbar in ctx.new.get("H1", []):
            self.h1_fast.update(hbar["close"])
            self.h1_slow.update(hbar["close"])
            self.h1_er.update(hbar)
            self.h1_close = hbar["close"]
        kc = self.kc.update(bar)
        ema50 = self.ema50.update(bar["close"])
        atr = self.atr.update(bar)
        low6, high6 = self.lowest.update(bar), self.highest.update(bar)
        prev, self.prev = self.prev, bar
        if kc is not None:
            self.recent.append({"index": ctx.index, "low": bar["low"], "high": bar["high"], "close": bar["close"], **kc})
        else:
            self.recent.clear()
        fast, slow, er = self.h1_fast.value, self.h1_slow.value, self.h1_er.value
        if (ctx.position is not None or None in (kc, ema50, atr, low6, high6, fast, slow, er, prev)
                or len(self.recent) < p["lookback"] or er < p["er_min"]):
            return None
        spread = ctx.spread_price()
        if spread > p["max_spread_atr"] * atr or not entry_allowed(ctx, p["start_hour"], p["end_hour"]):
            return None
        close, depth = bar["close"], p["depth"]
        side = None
        if self.h1_close > fast > slow and kc["middle"] > ema50:
            pulls = [r["index"] for r in self.recent if r["low"] <= r["middle"] - depth * r["atr"]]
            broken = any(r["close"] < r["lower"] - 0.5 * r["atr"] for r in self.recent)
            if (pulls and not broken and pulls[-1] > self.last_signal["BUY"]
                    and close > kc["middle"] and close > bar["open"] and close > prev["high"]):
                side = "BUY"
        elif self.h1_close < fast < slow and kc["middle"] < ema50:
            pulls = [r["index"] for r in self.recent if r["high"] >= r["middle"] + depth * r["atr"]]
            broken = any(r["close"] > r["upper"] + 0.5 * r["atr"] for r in self.recent)
            if (pulls and not broken and pulls[-1] > self.last_signal["SELL"]
                    and close < kc["middle"] and close < bar["open"] and close < prev["low"]):
                side = "SELL"
        if side is None:
            return None
        if side == "BUY":
            dist = min(max(close - (low6 - 0.2 * atr), 1.0 * atr), 2.5 * atr)
            sl = close - dist
        else:
            dist = min(max((high6 + 0.2 * atr + spread) - close, 1.0 * atr), 2.5 * atr)
            sl = close + dist
        self.last_signal[side] = ctx.index
        return self.signal(side, sl, net_rr_tp(side, close, sl, spread, p["rr"]), atr=atr, tag="kc_er")
