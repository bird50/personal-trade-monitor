"""M5 Supertrend flip back into a strong H1 trend with H4 bias (entry study, group 2).

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

* H4 bias: last closed H4 close > H4 EMA50 (long) / < (short), EMA50 fed only from ``ctx.new["H4"]``.
* Supertrend on M5 with ATR(10) Wilder and ``st_mult`` (bands carried over while the previous
  close stays beyond them; first valid bar up=bu, dn=bd, trend=+1).
* LONG when the trend flips -1 -> +1 on this bar, the -1 phase lasted 1..max_run bars, strong_up,
  H4 bias up and the bar is bullish. SHORT mirror. One signal per flip by construction.
* SL long = up[t] - 0.1 ATR14; short = dn[t] + 0.1 ATR14 + spread (then clamped).
"""
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


class EntSupertrendFlipH4(Strategy):
    name = "ent_supertrend_flip_h4"
    default_params = {
        "st_mult": 2.0, "gap_atr": 5, "max_run": 48, "rr": 2.0,
        "min_atr": 3.0, "max_spread_atr": 0.20, "start_hour": 2, "end_hour": 22,
        "exits": {},
    }

    def __init__(self, params=None):
        super().__init__(params)
        self.h1_fast, self.h1_slow, self.h1_close = EMA(50), EMA(200), None
        self.h4_ema, self.h4_close = EMA(50), None
        self.atr, self.atr10 = ATR(14), ATR(10)
        self.up = self.dn = self.trend = None
        self.prev_close = None
        self.run = 0  # bars in the current Supertrend phase

    def _supertrend(self, bar):
        """Update the Supertrend; returns (flipped_from, run_len_of_old_phase) or (None, 0)."""
        a10 = self.atr10.update(bar)
        close = bar["close"]
        flip, run_len = None, 0
        if a10 is not None:
            hl2 = (bar["high"] + bar["low"]) / 2
            bu, bd = hl2 - self.params["st_mult"] * a10, hl2 + self.params["st_mult"] * a10
            if self.trend is None:
                self.up, self.dn, self.trend, self.run = bu, bd, 1, 1
            else:
                prev_up, prev_dn, prev_trend, pc = self.up, self.dn, self.trend, self.prev_close
                up = max(bu, prev_up) if pc > prev_up else bu
                dn = min(bd, prev_dn) if pc < prev_dn else bd
                if prev_trend == -1 and close > prev_dn:
                    trend = 1
                elif prev_trend == 1 and close < prev_up:
                    trend = -1
                else:
                    trend = prev_trend
                if trend != prev_trend:
                    flip, run_len, self.run = prev_trend, self.run, 1
                else:
                    self.run += 1
                self.up, self.dn, self.trend = up, dn, trend
        self.prev_close = close
        return flip, run_len

    def on_bar(self, ctx):
        p, bar = self.params, ctx.bar
        for hbar in ctx.new.get("H1", []):
            self.h1_fast.update(hbar["close"])
            self.h1_slow.update(hbar["close"])
            self.h1_close = hbar["close"]
        for hbar in ctx.new.get("H4", []):
            self.h4_ema.update(hbar["close"])
            self.h4_close = hbar["close"]
        atr = self.atr.update(bar)
        flip, run_len = self._supertrend(bar)
        fast, slow, h4 = self.h1_fast.value, self.h1_slow.value, self.h4_ema.value
        if ctx.position is not None or flip is None or None in (atr, fast, slow, h4, self.h1_close, self.h4_close):
            return None
        spread = ctx.spread_price()
        if atr < p["min_atr"] or max(MIN_SPREAD, spread) > p["max_spread_atr"] * atr:
            return None
        if not entry_allowed(ctx, p["start_hour"], p["end_hour"]):
            return None
        if not 1 <= run_len <= p["max_run"]:
            return None
        close, gap = bar["close"], p["gap_atr"] * atr
        side = None
        if flip == -1 and self.trend == 1 and close > bar["open"]:
            if self.h1_close > fast > slow and fast - slow >= gap and self.h4_close > h4:
                side = "BUY"
        elif flip == 1 and self.trend == -1 and close < bar["open"]:
            if self.h1_close < fast < slow and slow - fast >= gap and self.h4_close < h4:
                side = "SELL"
        if side is None:
            return None
        raw = close - (self.up - 0.1 * atr) if side == "BUY" else (self.dn + 0.1 * atr + spread) - close
        d = min(max(raw, 1.1 * atr), 2.8 * atr)
        sl = close - d if side == "BUY" else close + d
        return self.signal(side, sl, net_rr_tp(side, close, sl, spread, p["rr"]), atr=atr, tag="st_flip")
