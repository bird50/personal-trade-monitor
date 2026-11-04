"""Causal streaming indicators. Feed each *closed* bar once, oldest first.

Every ``update(bar)`` uses only that bar and earlier bars and returns ``None`` until the
indicator is warmed up. EMA/Wilder smoothing reuses ``app.xm_engine.smooth`` so values match
the live XM engine; ``XMCore`` wraps ``update_m5`` itself (EMA20, RSI14, ATR14, ADX14, DI).
Time-of-day and session helpers use the broker server wall clock (``open_server_s``).
"""
import math
from collections import deque

from app.xm_engine import smooth, update_m5


def true_range(bar, previous):
    if previous is None:
        return bar["high"] - bar["low"]
    return max(bar["high"] - bar["low"], abs(bar["high"] - previous["close"]), abs(bar["low"] - previous["close"]))


class XMCore:
    """Exact live ``update_m5`` outputs: ema, rsi, adx, atr, plus_di, minus_di, cross_up/down."""

    def __init__(self):
        self.state = {}
        self.value = None

    def update(self, bar):
        self.value = update_m5(self.state, bar)
        return self.value


class EMA:
    def __init__(self, length):
        self.length, self.state, self.value = length, {}, None

    def update(self, value):
        self.value = smooth(self.state, "v", value, self.length, ema=True)
        return self.value


class RMA:
    """Wilder moving average: SMA seed of ``length`` values, then alpha = 1/length."""

    def __init__(self, length):
        self.length, self.state, self.value = length, {}, None

    def update(self, value):
        self.value = smooth(self.state, "v", value, self.length)
        return self.value


class SMA:
    def __init__(self, length):
        self.length, self.window, self.total, self.value = length, deque(), 0.0, None

    def update(self, value):
        self.window.append(value)
        self.total += value
        if len(self.window) > self.length:
            self.total -= self.window.popleft()
        self.value = self.total / self.length if len(self.window) == self.length else None
        return self.value


class RSI:
    """Wilder RSI on closes, same edge cases as the live engine (``RSI(2)`` = ``RSI(2)``)."""

    def __init__(self, length=14):
        self.length, self.state, self.previous, self.value = length, {}, None, None

    def update(self, bar_or_close):
        close = bar_or_close["close"] if isinstance(bar_or_close, dict) else bar_or_close
        change = None if self.previous is None else close - self.previous
        self.previous = close
        gain = smooth(self.state, "gain", max(change, 0) if change is not None else None, self.length)
        loss = smooth(self.state, "loss", max(-change, 0) if change is not None else None, self.length)
        self.value = None if gain is None or loss is None else 50.0 if gain == loss == 0 else 100.0 if loss == 0 else 100 - 100 / (1 + gain / loss)
        return self.value


class ATR:
    def __init__(self, length=14):
        self.rma, self.previous, self.value = RMA(length), None, None

    def update(self, bar):
        self.value = self.rma.update(true_range(bar, self.previous))
        self.previous = bar
        return self.value


class ADX:
    """Wilder ADX/+DI/-DI; returns ``{"adx", "plus_di", "minus_di"}`` (members may be None)."""

    def __init__(self, length=14):
        self.length, self.state, self.previous, self.value = length, {}, None, None

    def update(self, bar):
        previous, n, s = self.previous, self.length, self.state
        plus = minus = None
        if previous:
            up, down = bar["high"] - previous["high"], previous["low"] - bar["low"]
            plus = up if up > down and up > 0 else 0.0
            minus = down if down > up and down > 0 else 0.0
        dtr = smooth(s, "tr", true_range(bar, previous) if previous else None, n)
        dp, dm = smooth(s, "plus", plus, n), smooth(s, "minus", minus, n)
        pdi = None if dp is None else 100 * dp / dtr if dtr else 0.0
        mdi = None if dm is None else 100 * dm / dtr if dtr else 0.0
        dx = None if pdi is None or mdi is None else 100 * abs(pdi - mdi) / (pdi + mdi) if pdi + mdi else 0.0
        self.previous = bar
        self.value = {"adx": smooth(s, "adx", dx, n), "plus_di": pdi, "minus_di": mdi}
        return self.value


class EfficiencyRatio:
    """Kaufman ER = |close - close[n]| / sum(|close - close[1]|) over n bars, in [0, 1]."""

    def __init__(self, length=10):
        self.length, self.closes, self.value = length, deque(maxlen=length + 1), None

    def update(self, bar):
        self.closes.append(bar["close"])
        if len(self.closes) <= self.length:
            self.value = None
            return None
        c = list(self.closes)
        path = sum(abs(c[i] - c[i - 1]) for i in range(1, len(c)))
        self.value = abs(c[-1] - c[0]) / path if path else 0.0
        return self.value


class Choppiness:
    """CHOP = 100 * log10(sum TR(n) / (highest high(n) - lowest low(n))) / log10(n)."""

    def __init__(self, length=14):
        self.length, self.bars, self.trs, self.previous, self.value = length, deque(maxlen=length), deque(maxlen=length), None, None

    def update(self, bar):
        self.trs.append(true_range(bar, self.previous))
        self.bars.append(bar)
        self.previous = bar
        if len(self.bars) < self.length:
            self.value = None
            return None
        span = max(b["high"] for b in self.bars) - min(b["low"] for b in self.bars)
        self.value = 100 * math.log10(sum(self.trs) / span) / math.log10(self.length) if span > 0 else 100.0
        return self.value


class Bollinger:
    """SMA ± k·population stdev; ``width`` = (upper - lower) / middle."""

    def __init__(self, length=20, mult=2.0):
        self.length, self.mult, self.window, self.value = length, mult, deque(maxlen=length), None

    def update(self, bar_or_close):
        close = bar_or_close["close"] if isinstance(bar_or_close, dict) else bar_or_close
        self.window.append(close)
        if len(self.window) < self.length:
            self.value = None
            return None
        mean = sum(self.window) / self.length
        sd = math.sqrt(sum((x - mean) ** 2 for x in self.window) / self.length)
        upper, lower = mean + self.mult * sd, mean - self.mult * sd
        self.value = {"middle": mean, "upper": upper, "lower": lower, "stdev": sd,
                      "width": (upper - lower) / mean if mean else 0.0}
        return self.value


class Keltner:
    """EMA(close) ± mult·ATR (Wilder)."""

    def __init__(self, ema_length=20, atr_length=10, mult=1.5):
        self.ema, self.atr, self.mult, self.value = EMA(ema_length), ATR(atr_length), mult, None

    def update(self, bar):
        mid, atr = self.ema.update(bar["close"]), self.atr.update(bar)
        self.value = None if mid is None or atr is None else {
            "middle": mid, "upper": mid + self.mult * atr, "lower": mid - self.mult * atr, "atr": atr}
        return self.value


class StochRSI:
    """%K = SMA_k(100·(RSI - min RSI) / (max RSI - min RSI)), %D = SMA_d(%K)."""

    def __init__(self, rsi_length=14, stoch_length=14, k=3, d=3):
        self.rsi, self.window = RSI(rsi_length), deque(maxlen=stoch_length)
        self.k, self.d, self.stoch_length, self.value = SMA(k), SMA(d), stoch_length, None

    def update(self, bar):
        rsi = self.rsi.update(bar)
        self.value = None
        if rsi is None:
            return None
        self.window.append(rsi)
        if len(self.window) < self.stoch_length:
            return None
        lo, hi = min(self.window), max(self.window)
        raw = 100 * (rsi - lo) / (hi - lo) if hi > lo else 50.0
        k = self.k.update(raw)
        d = None if k is None else self.d.update(k)
        if k is not None:
            self.value = {"k": k, "d": d}
        return self.value


class MACD:
    def __init__(self, fast=12, slow=26, signal=9):
        self.fast, self.slow, self.signal, self.value = EMA(fast), EMA(slow), EMA(signal), None
        self.slow_length, self.count = slow, 0

    def update(self, bar_or_close):
        close = bar_or_close["close"] if isinstance(bar_or_close, dict) else bar_or_close
        self.count += 1
        fast, slow = self.fast.update(close), self.slow.update(close)
        if self.count < self.slow_length:
            self.value = None
            return None
        line = fast - slow
        signal = self.signal.update(line)
        self.value = {"macd": line, "signal": signal, "histogram": line - signal}
        return self.value


def server_day(bar):
    return bar["open_server_s"] // 86400


def server_minute_of_day(bar):
    return bar["open_server_s"] % 86400 // 60


class SessionVWAP:
    """Tick-volume weighted typical price, reset at each server day (or at ``reset_hour``)."""

    def __init__(self, reset_hour=0):
        self.offset, self.anchor, self.pv, self.volume, self.value = reset_hour * 3600, None, 0.0, 0.0, None

    def update(self, bar):
        anchor = (bar["open_server_s"] - self.offset) // 86400
        if anchor != self.anchor:
            self.anchor, self.pv, self.volume = anchor, 0.0, 0.0
        volume = max(bar["tick_volume"], 0)
        typical = (bar["high"] + bar["low"] + bar["close"]) / 3
        self.pv += typical * volume
        self.volume += volume
        self.value = self.pv / self.volume if self.volume else typical
        return self.value


class RelativeVolume:
    """Bar tick volume ÷ mean volume of the same server time-of-day slot over prior ``days``."""

    def __init__(self, days=20):
        self.days, self.history, self.value = days, {}, None

    def update(self, bar):
        slot = self.history.setdefault(server_minute_of_day(bar), deque(maxlen=self.days))
        mean = sum(slot) / len(slot) if len(slot) == self.days else None
        self.value = None if not mean else bar["tick_volume"] / mean
        slot.append(bar["tick_volume"])
        return self.value


class SessionRange:
    """High/low of a server-clock window, e.g. Asia ``start_hour=0, end_hour=10``.

    Returns ``{"high", "low", "complete", "day"}``: the running range while inside the window,
    then the finished range (``complete=True``) for the rest of that server day. Before the
    window opens on a new day the value is ``None``.
    """

    def __init__(self, start_hour=0, end_hour=10):
        self.start, self.end = start_hour * 3600, end_hour * 3600
        self.day, self.high, self.low, self.value = None, None, None, None

    def update(self, bar):
        day, second = server_day(bar), bar["open_server_s"] % 86400
        if day != self.day:
            self.day, self.high, self.low = day, None, None
        if self.start <= second < self.end:
            self.high = bar["high"] if self.high is None else max(self.high, bar["high"])
            self.low = bar["low"] if self.low is None else min(self.low, bar["low"])
        if self.high is None:
            self.value = None
        else:
            # Complete once this bar closes at/after the window end.
            self.value = {"high": self.high, "low": self.low, "day": day,
                          "complete": second + bar.get("period_s", 300) >= self.end}
        return self.value


class AsianRange(SessionRange):
    """Server 00:00-10:00 (XM server time; London opens near 10:00 server all year)."""

    def __init__(self, start_hour=0, end_hour=10):
        super().__init__(start_hour, end_hour)


class DailyPivots:
    """Classic floor pivots from the previous *completed* server day (P, R1-R3, S1-S3)."""

    def __init__(self):
        self.day, self.high, self.low, self.close, self.value = None, None, None, None, None

    def update(self, bar):
        day = server_day(bar)
        if day != self.day:
            if self.day is not None:
                h, l, c = self.high, self.low, self.close
                p = (h + l + c) / 3
                self.value = {"day": self.day, "high": h, "low": l, "close": c, "p": p,
                              "r1": 2 * p - l, "s1": 2 * p - h, "r2": p + (h - l), "s2": p - (h - l),
                              "r3": h + 2 * (p - l), "s3": l - 2 * (h - p)}
            self.day, self.high, self.low = day, bar["high"], bar["low"]
        self.high, self.low, self.close = max(self.high, bar["high"]), min(self.low, bar["low"]), bar["close"]
        return self.value


class Highest:
    def __init__(self, length, key="high"):
        self.window, self.key, self.length, self.value = deque(maxlen=length), key, length, None

    def update(self, bar):
        self.window.append(bar[self.key])
        self.value = max(self.window) if len(self.window) == self.length else None
        return self.value


class Lowest(Highest):
    def __init__(self, length, key="low"):
        super().__init__(length, key)

    def update(self, bar):
        self.window.append(bar[self.key])
        self.value = min(self.window) if len(self.window) == self.length else None
        return self.value
