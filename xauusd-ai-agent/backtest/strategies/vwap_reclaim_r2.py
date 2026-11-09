"""Session VWAP dip-and-reclaim in the direction of the H1 trend (round 2).

Rules (server wall clock, M5 closed bars only):

* Indicators updated every M5 bar: ``SessionVWAP(reset_hour=anchor_hour)``, ``RelativeVolume(20)``,
  M5 ``ATR(14)``, ``Lowest(6)`` / ``Highest(6)``; H1 EMA50/EMA200 and the last closed H1 close from
  ``ctx.new["H1"]`` (closed H1 bars only). ``bars_since_anchor`` resets when the VWAP anchor changes
  and must be >= ``min_bars_since_anchor``. Per-bar history keeps max(setup_bars, slope_bars)+1 bars.
* Window: ``ctx.time_s`` server hour in [window_start_hour, window_end_hour); no entry on Friday
  at/after ``friday_cutoff_hour``; no entry when the hour falls in ``blackout_hours`` ([start, end)
  pair, or a list of such pairs; empty = none).
* Trend filter (``trend_filter``):
  - ``strict``: LONG needs h1_close > EMA50 > EMA200; SHORT h1_close < EMA50 < EMA200.
  - ``ema50``: LONG needs h1_close > EMA50; SHORT h1_close < EMA50.
  Both also need VWAP slope in the trade direction: vwap_now vs vwap ``slope_bars`` bars ago.
* Setup: one of the previous ``setup_bars`` bars (current excluded) closed below its own VWAP with
  low <= its VWAP - dip_atr x its ATR14 (SHORT mirrored). Trigger: close > vwap + 0.1 ATR,
  close > open, close > previous high, RVOL >= rvol_min (SHORT mirrored, close < previous low).
* One signal per setup: the dip bar index must be greater than the last signal's bar index.
* Common: spread_price <= max_spread_atr x ATR14.
* SL long = Lowest(low, 6) - 0.2 ATR; short = Highest(high, 6) + 0.2 ATR + spread; |close - sl|
  clamped to [1.0, 2.5] ATR. TP from the net-RR helper (planned Paper net RR == ``rr``).
* Exits: breakeven at 0.7R locking 0.1R, time stop 48 M5 bars. ``atr`` = M5 ATR14, tag ``vwap``.
"""
from collections import deque
from datetime import datetime, timezone

from backtest.indicators import ATR, EMA, Highest, Lowest, RelativeVolume, SessionVWAP

from .base import Strategy

SLIPPAGE = 0.05      # config/risk-xm-paper.json slippage_price_per_side
COMMISSION = 0.10    # config/risk-xm-paper.json commission_usd_per_unit_round_trip


def net_rr_tp(side, close, sl, spread, rr):
    """TP so that (reward - costs) / (risk + costs) == rr at an estimated next-open fill."""
    if side == "BUY":
        entry = close + spread + SLIPPAGE
        loss = entry - sl + SLIPPAGE + COMMISSION
        return entry + rr * loss + SLIPPAGE + COMMISSION
    entry = close - SLIPPAGE
    loss = sl - entry + SLIPPAGE + COMMISSION
    return entry - rr * loss - SLIPPAGE - COMMISSION


def clamp_stop(side, close, sl, atr, lo, hi):
    dist = min(max(abs(close - sl), lo * atr), hi * atr)
    return close - dist if side == "BUY" else close + dist


def blackout_ranges(value):
    """Normalise ``[start, end]`` or ``[[start, end], ...]`` into a list of pairs."""
    if not value:
        return []
    if all(isinstance(v, (int, float)) for v in value):
        if len(value) != 2:
            raise ValueError("blackout_hours must be [start, end] or a list of pairs")
        return [tuple(value)]
    return [tuple(v) for v in value]


class VWAPReclaimR2(Strategy):
    name = "vwap_reclaim_r2"
    default_params = {
        "anchor_hour": 0, "dip_atr": 0.8, "rvol_min": 0.8, "rr": 1.3,
        "window_start_hour": 10, "window_end_hour": 21, "friday_cutoff_hour": 20,
        "min_bars_since_anchor": 12, "slope_bars": 12, "setup_bars": 6, "max_spread_atr": 0.20,
        "trend_filter": "strict", "blackout_hours": [],
        "exits": {"breakeven_r": 0.7, "breakeven_lock_r": 0.1, "time_stop_bars": 48},
    }

    def __init__(self, params=None):
        super().__init__(params)
        p = self.params
        if p["trend_filter"] not in ("strict", "ema50"):
            raise ValueError("trend_filter must be 'strict' or 'ema50'")
        self.blackout = blackout_ranges(p["blackout_hours"])
        self.vwap = SessionVWAP(p["anchor_hour"])
        self.rvol = RelativeVolume(20)
        self.atr = ATR(14)
        self.lowest, self.highest = Lowest(6), Highest(6)
        self.h1_fast, self.h1_slow = EMA(50), EMA(200)
        self.h1_close = None
        self.anchor = None
        self.bars_since_anchor = 0
        self.hist = deque(maxlen=max(p["slope_bars"], p["setup_bars"]) + 1)   # per-bar snapshots
        self.last_signal_index = -1

    def on_bar(self, ctx):
        p, bar = self.params, ctx.bar
        for hbar in ctx.new.get("H1", []):
            self.h1_fast.update(hbar["close"])
            self.h1_slow.update(hbar["close"])
            self.h1_close = hbar["close"]
        vwap = self.vwap.update(bar)
        if self.vwap.anchor != self.anchor:
            self.anchor, self.bars_since_anchor = self.vwap.anchor, 0
            self.hist.clear()
        self.bars_since_anchor += 1
        rvol = self.rvol.update(bar)
        atr = self.atr.update(bar)
        low6, high6 = self.lowest.update(bar), self.highest.update(bar)
        prev = self.hist[-1] if self.hist else None
        snap = {"index": ctx.index, "vwap": vwap, "atr": atr, "close": bar["close"],
                "high": bar["high"], "low": bar["low"]}
        result = self._decide(ctx, p, bar, vwap, rvol, atr, low6, high6, prev)
        self.hist.append(snap)
        return result

    def _trend(self, p, h1c, fast, slow):
        if p["trend_filter"] == "strict":
            if slow is None:
                return None
            if h1c > fast > slow:
                return "BUY"
            if h1c < fast < slow:
                return "SELL"
            return None
        if h1c > fast:
            return "BUY"
        if h1c < fast:
            return "SELL"
        return None

    def _decide(self, ctx, p, bar, vwap, rvol, atr, low6, high6, prev):
        if atr is None or rvol is None or low6 is None or prev is None or ctx.position is not None:
            return None
        if self.bars_since_anchor < p["min_bars_since_anchor"] or len(self.hist) < p["slope_bars"]:
            return None
        hour = ctx.time_s % 86400 // 3600
        if not p["window_start_hour"] <= hour < p["window_end_hour"]:
            return None
        if any(start <= hour < end for start, end in self.blackout):
            return None
        if datetime.fromtimestamp(ctx.time_s, timezone.utc).weekday() == 4 and hour >= p["friday_cutoff_hour"]:
            return None
        fast, slow, h1c = self.h1_fast.value, self.h1_slow.value, self.h1_close
        if fast is None or h1c is None:
            return None
        spread = ctx.spread_price()
        if spread > p["max_spread_atr"] * atr:
            return None
        trend = self._trend(p, h1c, fast, slow)
        if trend is None:
            return None
        vwap_then = self.hist[-p["slope_bars"]]["vwap"]   # vwap slope_bars bars before the current bar
        recent = list(self.hist)[-p["setup_bars"]:]
        close, o = bar["close"], bar["open"]
        side = dip_index = None
        if trend == "BUY" and vwap > vwap_then:
            dips = [s["index"] for s in recent if s["atr"] is not None and s["close"] < s["vwap"]
                    and s["low"] <= s["vwap"] - p["dip_atr"] * s["atr"]]
            if dips and close > vwap + 0.1 * atr and close > o and close > prev["high"] and rvol >= p["rvol_min"]:
                side, dip_index = "BUY", dips[-1]
        elif trend == "SELL" and vwap < vwap_then:
            dips = [s["index"] for s in recent if s["atr"] is not None and s["close"] > s["vwap"]
                    and s["high"] >= s["vwap"] + p["dip_atr"] * s["atr"]]
            if dips and close < vwap - 0.1 * atr and close < o and close < prev["low"] and rvol >= p["rvol_min"]:
                side, dip_index = "SELL", dips[-1]
        if side is None or dip_index <= self.last_signal_index:
            return None
        self.last_signal_index = ctx.index
        raw = low6 - 0.2 * atr if side == "BUY" else high6 + 0.2 * atr + spread
        sl = clamp_stop(side, close, raw, atr, 1.0, 2.5)
        tp = net_rr_tp(side, close, sl, spread, p["rr"])
        return self.signal(side, sl, tp, atr=atr, tag="vwap")
