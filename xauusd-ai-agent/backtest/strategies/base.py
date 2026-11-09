"""Strategy contract for the backtest engine.

``Strategy(params).on_bar(ctx)`` is called once per closed M5 bar, oldest first, including
while a position is open (keep indicators updated; the engine ignores signals then). Return
``None`` or ``{"side": "BUY"|"SELL", "sl": price, "tp": price}``. Optional keys:

* ``atr``: M5 ATR at the signal, enables the live Risk stop/spread/drift checks in the report.
* ``tag``: free-form label stored on the trade.
* exits: ``breakeven_r``, ``breakeven_lock_r``, ``trail_start_r``, ``trail_r``, ``time_stop_bars``
  (``time_stop_bars`` counts M5 bars after the signal close).

SL/TP are absolute bid-chart prices computed from information available at ``ctx.time_s``.
The fill happens at the next bar's open, so the realized risk distance differs from
``close - sl``. Use only ``ctx`` (closed bars) and your own state; never read files or the
history object directly.
"""
import copy


def merge(base: dict, override: dict | None) -> dict:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        out[key] = merge(out[key], value) if isinstance(value, dict) and isinstance(out.get(key), dict) else value
    return out


class Strategy:
    name = "base"
    default_params: dict = {"exits": {}}

    def __init__(self, params: dict | None = None):
        unknown = set(params or {}) - set(self.default_params) - {"exits"}
        if unknown:
            raise ValueError(f"Unknown parameters for {self.name}: {sorted(unknown)}")
        self.params = merge(self.default_params, params)

    def on_bar(self, ctx):
        raise NotImplementedError

    def signal(self, side, sl, tp, **extra):
        out = {"side": side, "sl": float(sl), "tp": float(tp)}
        out.update({k: v for k, v in (self.params.get("exits") or {}).items() if v is not None})
        out.update(extra)
        return out
