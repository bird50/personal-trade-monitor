"""Heikin-Ashi colour change after a short pullback to EMA20, H1 Efficiency Ratio filter (group 2).

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

* H1 EfficiencyRatio(10) (fed only from ``ctx.new["H1"]``) >= er_min.
* Causal M5 Heikin-Ashi. A run is the consecutive red (or green) HA bars ending at t-1; a doji
  (ha_close == ha_open) ends any run. run_low/run_high are the real extremes of the run.
* LONG: red run of 2..12 bars, bar t green with no lower shadow (<= 0.05 ATR), real close above
  the previous real high, run_low <= EMA20 and run_low >= EMA50 - 1.0 ATR. SHORT mirror.
* SL long = run_low - 0.2 ATR; short = run_high + 0.2 ATR + spread (then clamped).
"""
from backtest.indicators import ATR, EMA, EfficiencyRatio

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


class EntHeikinAshiFlip(Strategy):
    name = "ent_heikin_ashi_flip"
    default_params = {
        "er_min": 0.30, "gap_atr": 5, "rr": 2.0, "min_atr": 3.0, "max_spread_atr": 0.20,
        "start_hour": 2, "end_hour": 22, "exits": {},
    }

    def __init__(self, params=None):
        super().__init__(params)
        self.h1_fast, self.h1_slow, self.h1_close, self.er = EMA(50), EMA(200), None, EfficiencyRatio(10)
        self.ema20, self.ema50, self.atr = EMA(20), EMA(50), ATR(14)
        self.ha_open = self.ha_close = None
        self.prev = None
        # Run of HA colour ending at the previous bar: colour (+1 green, -1 red, 0 none), length, extremes.
        self.run_color, self.run_len, self.run_low, self.run_high = 0, 0, None, None

    def _heikin_ashi(self, bar):
        o, h, l, c = bar["open"], bar["high"], bar["low"], bar["close"]
        ha_close = (o + h + l + c) / 4
        ha_open = (o + c) / 2 if self.ha_open is None else (self.ha_open + self.ha_close) / 2
        self.ha_open, self.ha_close = ha_open, ha_close
        return ha_open, ha_close, max(h, ha_open, ha_close), min(l, ha_open, ha_close)

    def on_bar(self, ctx):
        p, bar = self.params, ctx.bar
        for hbar in ctx.new.get("H1", []):
            self.h1_fast.update(hbar["close"])
            self.h1_slow.update(hbar["close"])
            self.er.update(hbar)
            self.h1_close = hbar["close"]
        e20, e50, atr = self.ema20.update(bar["close"]), self.ema50.update(bar["close"]), self.atr.update(bar)
        ha_open, ha_close, ha_high, ha_low = self._heikin_ashi(bar)
        color = 1 if ha_close > ha_open else -1 if ha_close < ha_open else 0
        # Snapshot the run that ended at t-1, then extend/restart it with bar t.
        run_color, run_len, run_low, run_high = self.run_color, self.run_len, self.run_low, self.run_high
        if color != 0 and color == self.run_color:
            self.run_len += 1
            self.run_low, self.run_high = min(self.run_low, bar["low"]), max(self.run_high, bar["high"])
        else:
            self.run_color, self.run_len = color, (1 if color else 0)
            self.run_low, self.run_high = bar["low"], bar["high"]
        prev, self.prev = self.prev, bar
        fast, slow, er = self.h1_fast.value, self.h1_slow.value, self.er.value
        if ctx.position is not None or None in (e20, e50, atr, fast, slow, er, self.h1_close, prev):
            return None
        if er < p["er_min"] or not 2 <= run_len <= 12:
            return None
        spread = ctx.spread_price()
        if atr < p["min_atr"] or max(MIN_SPREAD, spread) > p["max_spread_atr"] * atr:
            return None
        if not entry_allowed(ctx, p["start_hour"], p["end_hour"]):
            return None
        close, gap = bar["close"], p["gap_atr"] * atr
        side = None
        if (run_color == -1 and color == 1 and self.h1_close > fast > slow and fast - slow >= gap
                and min(ha_open, ha_close) - ha_low <= 0.05 * atr and close > prev["high"]
                and run_low <= e20 and run_low >= e50 - 1.0 * atr):
            side = "BUY"
        elif (run_color == 1 and color == -1 and self.h1_close < fast < slow and slow - fast >= gap
                and ha_high - max(ha_open, ha_close) <= 0.05 * atr and close < prev["low"]
                and run_high >= e20 and run_high <= e50 + 1.0 * atr):
            side = "SELL"
        if side is None:
            return None
        raw = close - (run_low - 0.2 * atr) if side == "BUY" else (run_high + 0.2 * atr + spread) - close
        d = min(max(raw, 1.1 * atr), 2.8 * atr)
        sl = close - d if side == "BUY" else close + d
        return self.signal(side, sl, net_rr_tp(side, close, sl, spread, p["rr"]), atr=atr, tag="ha_flip")
