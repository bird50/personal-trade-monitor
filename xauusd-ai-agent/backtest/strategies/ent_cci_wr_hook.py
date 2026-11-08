"""CCI extreme hook with Williams %R confirmation in a strong H1 trend.

* H1 EMA50, EMA200 and ADX14 (+DI/-DI) are fed only from ``ctx.new["H1"]``; M5 CCI(20),
  Williams %R(14), EMA200, ATR14 and Lowest/Highest(8) update on every M5 bar, including while
  a position is open.
* strong_up: h1_close > h1_ema50 > h1_ema200 and (h1_ema50 - h1_ema200) >= gap_atr * M5 ATR14,
  H1 ADX >= 20 and +DI > -DI, M5 close > M5 EMA200. strong_down is the mirror.
* CCI: tp = (h+l+c)/3, sma/mean deviation over the last 20 tp (incl. current),
  cci = (tp - sma) / (0.015 * md) (0 if md == 0). %R(14) = -100*(HH - close)/(HH - LL)
  (-50 if HH == LL).
* LONG: strong_up, min(cci[t-6..t-1]) <= -cci_level, cci[t-1] < -100 <= cci[t],
  wr[t] > -80 and wr[t] > wr[t-1], close > open, long armed. SHORT mirror. After a long the
  long side is disarmed until cci > 0 (short until cci < 0).
* Filters: M5 ATR14 >= min_atr, spread <= max_spread_atr*ATR14, server hour in
  [start_hour, end_hour), no Friday entries from 20:00 server, no signal while in a position.
* SL: long d = close - (Lowest(low,8) - 0.2*atr), short d = (Highest(high,8) + 0.2*atr +
  spread) - close, d clamped to [1.1, 2.8]*ATR14. TP by the net-RR helper with
  max(0.60, spread) and ``rr``.
"""
from collections import deque

from backtest.indicators import ADX, ATR, EMA, Highest, Lowest

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


class CCI:
    def __init__(self, length=20):
        self.length, self.tps, self.value = length, deque(maxlen=length), None

    def update(self, bar):
        self.tps.append((bar["high"] + bar["low"] + bar["close"]) / 3)
        if len(self.tps) < self.length:
            self.value = None
            return None
        sma = sum(self.tps) / self.length
        md = sum(abs(x - sma) for x in self.tps) / self.length
        self.value = (self.tps[-1] - sma) / (0.015 * md) if md else 0.0
        return self.value


class WilliamsR:
    def __init__(self, length=14):
        self.length, self.bars, self.value = length, deque(maxlen=length), None

    def update(self, bar):
        self.bars.append((bar["high"], bar["low"]))
        if len(self.bars) < self.length:
            self.value = None
            return None
        hh, ll = max(b[0] for b in self.bars), min(b[1] for b in self.bars)
        self.value = -100 * (hh - bar["close"]) / (hh - ll) if hh != ll else -50.0
        return self.value


class EntCCIWRHook(Strategy):
    name = "ent_cci_wr_hook"
    default_params = {
        "cci_level": 100, "gap_atr": 5, "rr": 2.0, "min_atr": 3.0, "max_spread_atr": 0.20,
        "start_hour": 2, "end_hour": 22, "exits": {},
    }

    def __init__(self, params=None):
        super().__init__(params)
        self.h1_fast, self.h1_slow, self.h1_adx, self.h1_close = EMA(50), EMA(200), ADX(14), None
        self.cci, self.wr = CCI(20), WilliamsR(14)
        self.ema200, self.atr = EMA(200), ATR(14)
        self.lowest, self.highest = Lowest(8), Highest(8)
        self.cci_hist = deque(maxlen=7)  # cci[t-6..t]
        self.prev_wr = None
        self.armed = {"BUY": True, "SELL": True}

    def on_bar(self, ctx):
        p, bar = self.params, ctx.bar
        for hbar in ctx.new.get("H1", []):
            self.h1_fast.update(hbar["close"])
            self.h1_slow.update(hbar["close"])
            self.h1_adx.update(hbar)
            self.h1_close = hbar["close"]
        cci, wr = self.cci.update(bar), self.wr.update(bar)
        ema200, atr = self.ema200.update(bar["close"]), self.atr.update(bar)
        low8, high8 = self.lowest.update(bar), self.highest.update(bar)
        prev_wr, self.prev_wr = self.prev_wr, wr
        if cci is None:
            self.cci_hist.clear()
            return None
        self.cci_hist.append(cci)
        if cci > 0:
            self.armed["BUY"] = True
        if cci < 0:
            self.armed["SELL"] = True
        fast, slow = self.h1_fast.value, self.h1_slow.value
        adx = self.h1_adx.value or {}
        if ctx.position is not None or len(self.cci_hist) < 7 or None in (
                wr, prev_wr, ema200, atr, low8, high8, fast, slow, self.h1_close,
                adx.get("adx"), adx.get("plus_di"), adx.get("minus_di")):
            return None
        spread = ctx.spread_price()
        if atr < p["min_atr"] or spread > p["max_spread_atr"] * atr:
            return None
        if not entry_allowed(ctx, p["start_hour"], p["end_hour"]):
            return None
        close, gap = bar["close"], p["gap_atr"] * atr
        past = list(self.cci_hist)[:-1]  # cci[t-6..t-1]
        prev_cci, level = past[-1], p["cci_level"]
        strong_adx = adx["adx"] >= 20
        side = None
        if (self.armed["BUY"] and self.h1_close > fast > slow and fast - slow >= gap and strong_adx
                and adx["plus_di"] > adx["minus_di"] and close > ema200
                and min(past) <= -level and prev_cci < -100 <= cci
                and wr > -80 and wr > prev_wr and close > bar["open"]):
            side = "BUY"
        elif (self.armed["SELL"] and self.h1_close < fast < slow and slow - fast >= gap and strong_adx
                and adx["minus_di"] > adx["plus_di"] and close < ema200
                and max(past) >= level and prev_cci > 100 >= cci
                and wr < -20 and wr < prev_wr and close < bar["open"]):
            side = "SELL"
        if side is None:
            return None
        if side == "BUY":
            d = close - (low8 - 0.2 * atr)
        else:
            d = (high8 + 0.2 * atr + spread) - close
        d = min(max(d, 1.1 * atr), 2.8 * atr)
        sl = close - d if side == "BUY" else close + d
        self.armed[side] = False
        return self.signal(side, sl, net_rr_tp(side, close, sl, spread, p["rr"]), atr=atr, tag="cci_hook")
