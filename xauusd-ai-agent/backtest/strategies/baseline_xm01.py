"""Port of the live ``xm-0.1`` candidate rules (``app/xm_engine.py``) for parity backtests.

Same causal order as ``XMEngine.process``: admit H1 bars closed at/before the M5 *open*
(Pine ``[1]``), update M5 indicators and pivots, clear the virtual candidate lock when the
bar touches its SL/TP, then ``choose_side`` with a fresh H1 and 20 prior bars. Levels come
from ``levels`` (SL 1.5×ATR, TP 2× the stop distance). With ``structure_filter`` the
candidate must also pass ``confirmation_blockers`` like the live Risk/AI stage; the virtual
lock is set for every candidate either way, as in production.
"""
from decimal import Decimal
from types import SimpleNamespace

from app.models import MarketStructure
from app.structure import confirmation_blockers
from app.xm_engine import choose_side, levels, smooth, update_m5, update_pivots

from .base import Strategy


class BaselineXM01(Strategy):
    name = "baseline_xm01"
    default_params = {"structure_filter": True, "exits": {}}

    def __init__(self, params=None):
        super().__init__(params)
        self.state = {"m5_indicators": {}, "h1_indicators": {}, "h1": None, "recent_range": [],
                      "candidate_lock": None, "index": -1}
        self.h1_seen = 0
        self.candidates = []

    def _admit_h1(self, ctx, bar):
        st = self.state
        while self.h1_seen < len(ctx.h1) and ctx.h1[self.h1_seen]["open_server_s"] + 3600 <= bar["open_server_s"]:
            hbar = ctx.h1[self.h1_seen]
            self.h1_seen += 1
            fast = smooth(st["h1_indicators"], "fast", hbar["close"], 50, ema=True)
            slow = smooth(st["h1_indicators"], "slow", hbar["close"], 200, ema=True)
            if st["h1_indicators"]["slow"]["count"] >= 200:
                st["h1"] = {"confirmed": True, "bar_close_ms": (hbar["open_server_s"] + 3600) * 1000,
                            "close": hbar["close"], "ema_fast": fast, "ema_slow": slow}

    def on_bar(self, ctx):
        st, bar = self.state, ctx.bar
        self._admit_h1(ctx, bar)
        st["index"] += 1
        metrics = update_m5(st["m5_indicators"], bar)
        structure = update_pivots(st, bar, st["index"])
        lock = st["candidate_lock"]
        if lock and (bar["low"] <= lock["sl"] if lock["side"] == "BUY" else bar["high"] >= lock["sl"]):
            st["candidate_lock"] = None
        if lock and (bar["high"] >= lock["tp"] if lock["side"] == "BUY" else bar["low"] <= lock["tp"]):
            st["candidate_lock"] = None
        h1_fresh = st["h1"] is not None and 0 <= bar["open_server_s"] - st["h1"]["bar_close_ms"] / 1000 < 3600
        side = choose_side(metrics, st["h1"]) if h1_fresh else None
        prior = st["recent_range"]
        candidate = side if st["candidate_lock"] is None and len(prior) == 20 else None
        result = None
        if candidate:
            entry, sl, tp = levels(candidate, bar["close"], metrics["atr"], Decimal(str(ctx.tick)))
            st["candidate_lock"] = {"side": candidate, "sl": sl, "tp": tp}
            blockers = []
            if self.params["structure_filter"]:
                blockers = confirmation_blockers(SimpleNamespace(
                    side=candidate, entry=entry, sl=sl, tp=tp,
                    context=SimpleNamespace(market_structure=MarketStructure.model_validate(structure))))
            self.candidates.append({"open_server_s": bar["open_server_s"], "side": candidate,
                                    "entry": entry, "sl": sl, "tp": tp, "blockers": blockers})
            if not blockers:
                result = self.signal(candidate, sl, tp, atr=metrics["atr"], tag="xm-0.1")
        prior.append(bar)
        del prior[:-20]
        return result
