"""Strategy cores for the alert-only engine, one per configured strategy.

Every core is stepped once per closed M5 bar with the bars visible at that bar's close and keeps
its own hypothetical trade, so each strategy behaves exactly as when it was backtested alone.

* ``MarketCore`` wraps a backtest strategy class (``backtest/strategies``): a signal at an M5
  close is a market entry. Supported: ``rsi2_trend_pullback_r2``, ``ent_cci_wr_hook``,
  ``ent_heikin_ashi_flip``.
* ``TPSLCore`` is the published TPSL gold London breakout (rules as in
  ``scripts/tpsl_london_breakout.py``): range 03:00-08:00 server, buy stop above / sell stop
  below at 08:00, one trade per day, breakeven at +1.5 R, everything closed at 21:00. The M1
  simulator checks fills on M1 bars; this core sees M5 bars (an M5 high/low contains its M1
  bars, so the range is identical, while the order of events inside one M5 bar is less precise).

Cores never size, fill or book anything. Their state is checkpointed as JSON.
"""
from collections import Counter, deque
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backtest import indicators
from backtest.engine import Context
from backtest.strategies import load as load_strategy
from backtest.strategies.ent_cci_wr_hook import CCI, WilliamsR

MARKET_STRATEGIES = ("rsi2_trend_pullback_r2", "ent_cci_wr_hook", "ent_heikin_ashi_flip")
PENDING_STRATEGIES = ("tpsl_london_breakout",)
SERIALIZABLE = {cls.__name__: cls for cls in (
    indicators.EMA, indicators.RMA, indicators.RSI, indicators.ATR, indicators.ADX, indicators.EfficiencyRatio,
    indicators.Highest, indicators.Lowest, CCI, WilliamsR)}
DIAGNOSTIC_FIELDS = ("params", "cost_filtered_times")
UNSUPPORTED_EXITS = ("breakeven_r", "breakeven_lock_r", "trail_start_r", "trail_r")


class TPSLParams(BaseModel):
    """Locked TPSL London breakout rules (server wall clock)."""
    model_config = ConfigDict(extra="forbid", frozen=True)
    range_start_h: Annotated[int, Field(ge=0, le=23, strict=True)] = 3
    range_end_h: Annotated[int, Field(ge=1, le=23, strict=True)] = 8
    close_h: Annotated[int, Field(ge=2, le=24, strict=True)] = 21
    min_width_pct: Annotated[float, Field(gt=0, le=10)] = 0.3
    max_width_pct: Annotated[float, Field(gt=0, le=10)] = 1.6
    offset: Annotated[float, Field(ge=0, le=10)] = 0.2
    stop_cap_pct: Annotated[float, Field(gt=0, le=10)] = 0.6
    rr: Annotated[float, Field(gt=0, le=20)] = 2.5
    breakeven_r: Annotated[float | None, Field(gt=0, le=20)] = 1.5
    min_range_bars: Annotated[int, Field(ge=1, le=288, strict=True)] = 48  # M5 bars (240 M1 bars in the script)

    @model_validator(mode="after")
    def ordered(self):
        if not self.range_start_h < self.range_end_h < self.close_h:
            raise ValueError("TPSL needs range_start_h < range_end_h < close_h")
        if self.min_width_pct >= self.max_width_pct:
            raise ValueError("TPSL needs min_width_pct < max_width_pct")
        return self


class StrategySpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,15}$")]
    label: Annotated[str, Field(min_length=1, max_length=40)]
    strategy: Literal[MARKET_STRATEGIES + PENDING_STRATEGIES]
    strategy_version: Annotated[str, Field(pattern=r"^[a-z0-9.-]{1,40}$")]
    enabled: bool = False  # default selection before the user picks strategies in XM Control
    params: dict

    @model_validator(mode="after")
    def supported(self):
        if self.kind == "pending":
            TPSLParams.model_validate(self.params)
            return self
        exits = self.effective_params.get("exits") or {}  # also rejects unknown parameters
        if any(exits.get(key) is not None for key in UNSUPPORTED_EXITS):
            raise ValueError("Alert mode tracks fixed SL/TP only; breakeven and trailing exits are not supported")
        stop = exits.get("time_stop_bars")
        if stop is not None and (not isinstance(stop, int) or isinstance(stop, bool) or not 1 <= stop <= 2016):
            raise ValueError("time_stop_bars must be null or an integer 1-2016")
        return self

    @property
    def kind(self) -> str:
        return "pending" if self.strategy in PENDING_STRATEGIES else "market"

    @property
    def effective_params(self) -> dict:
        if self.kind == "pending":
            return TPSLParams.model_validate(self.params).model_dump()
        return load_strategy(self.strategy)(self.params).params

    @property
    def time_stop_bars(self) -> int | None:
        if self.kind == "pending":
            return None
        return (self.effective_params.get("exits") or {}).get("time_stop_bars")


# ---------------------------------------------------------------------- checkpoint values
def dump_value(value):
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Counter):
        return {"counter": dict(value)}
    if isinstance(value, deque):
        return {"deque": [dump_value(item) for item in value], "maxlen": value.maxlen}
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("State dictionaries need string keys")
        return {"dict": {key: dump_value(item) for key, item in value.items()}}
    if isinstance(value, (list, tuple)):
        return {"list": [dump_value(item) for item in value]}
    name = type(value).__name__
    if SERIALIZABLE.get(name) is type(value):
        return {"indicator": name, "fields": {key: dump_value(item) for key, item in vars(value).items()}}
    raise TypeError(f"Cannot checkpoint {name}")


def load_value(value):
    if not isinstance(value, dict):
        return value
    if "counter" in value:
        return Counter(value["counter"])
    if "deque" in value:
        return deque((load_value(item) for item in value["deque"]), maxlen=value["maxlen"])
    if "dict" in value:
        return {key: load_value(item) for key, item in value["dict"].items()}
    if "list" in value:
        return [load_value(item) for item in value["list"]]
    cls = SERIALIZABLE[value["indicator"]]
    obj = cls.__new__(cls)
    obj.__dict__.update({key: load_value(item) for key, item in value["fields"].items()})
    return obj


def fmt(value, digits=2):
    return "—" if value is None else f"{float(value):,.{digits}f}"


def hhmm(server_s):
    return f"{server_s % 86400 // 3600:02d}:{server_s % 3600 // 60:02d}"


# ---------------------------------------------------------------------- market strategies
class MarketCore:
    """A backtest strategy plus the hypothetical one-trade blocker, stepped once per closed M5 bar."""
    kind = "market"

    def __init__(self, spec: StrategySpec, state: dict | None = None):
        self.spec = spec
        self.strategy = load_strategy(spec.strategy)(spec.params)
        self.position, self.m5_count, self.h1_count = None, 0, 0
        if state:
            for key, value in state["strategy"].items():
                setattr(self.strategy, key, load_value(value))
            self.position, self.m5_count, self.h1_count = state["position"], state["m5_count"], state["h1_count"]

    def dump(self) -> dict:
        fields = {key: dump_value(value) for key, value in vars(self.strategy).items() if key not in DIAGNOSTIC_FIELDS}
        return {"strategy": fields, "position": self.position, "m5_count": self.m5_count, "h1_count": self.h1_count}

    def manage(self, bar, point, digits):
        """Close the hypothetical trade on time stop (at bar open) or a touched SL/TP."""
        pos = self.position
        if pos is None:
            return None
        if pos.get("time_stop_server_s") is not None and bar["open_server_s"] >= pos["time_stop_server_s"]:
            reason = "TIME"
        else:
            spread = round(bar["spread_points"] * point, digits)
            sl, tp = float(pos["sl"]), float(pos["tp"])
            if pos["side"] == "BUY":
                reason = "SL" if bar["low"] <= sl else "TP" if bar["high"] >= tp else None
            else:
                reason = "SL" if bar["high"] + spread >= sl else "TP" if bar["low"] + spread <= tp else None
        if reason is None:
            return None
        self.position = None
        return {**pos, "exit_reason": reason, "exit_bar_server_s": bar["open_server_s"]}

    def step(self, bar, new_h1, point, digits):
        """Returns (signals, exit). A signal is ``{"type": "entry", "side", "sl", "tp", "atr", ...}``."""
        exit_ = self.manage(bar, point, digits)
        ctx = Context(bar["open_server_s"] + 300, bar, self.m5_count, [], {"H1": [], "H4": [], "D1": []},
                      {"M5": [bar], "H1": list(new_h1)}, None if self.position is None else dict(self.position),
                      0.0, point, digits, float(point))
        trigger_rsi = getattr(self.strategy, "prev_rsi", None)  # RSI(2) confirm fires on the previous bar
        signal = self.strategy.on_bar(ctx)
        self.m5_count += 1
        self.h1_count += len(new_h1)
        if not signal:
            return [], exit_
        signal = {**signal, "type": "entry"}
        if self.spec.strategy == "rsi2_trend_pullback_r2":
            signal["rsi2_previous"] = trigger_rsi
        return [signal], exit_

    def open(self, side, sl, tp, signal_close_server_s):
        bars = self.spec.time_stop_bars
        self.position = {"side": side, "sl": format(sl, "f"), "tp": format(tp, "f"),
                         "signal_close_server_s": signal_close_server_s,
                         "time_stop_server_s": None if bars is None else signal_close_server_s + bars * 300}

    def indicators(self) -> dict:
        s, name = self.strategy, self.spec.strategy
        fast, slow, close = s.h1_fast.value, s.h1_slow.value, s.h1_close
        out = {"h1_close": close, "h1_ema50": fast, "h1_ema200": slow, "atr14": s.atr.value, "bias": None}
        if name == "rsi2_trend_pullback_r2":
            if None not in (fast, slow, close, s.ema200.value) and s.prev is not None:
                m5_close = s.prev["close"]
                if close > slow and fast > slow and m5_close > s.ema200.value:
                    out["bias"] = "BUY"
                elif close < slow and fast < slow and m5_close < s.ema200.value:
                    out["bias"] = "SELL"
            out.update(rsi2=s.rsi.value, m5_ema200=s.ema200.value, armed=dict(s.armed),
                       m5_close=None if s.prev is None else s.prev["close"])
            return out
        atr, gap = s.atr.value, s.params["gap_atr"]
        if None not in (fast, slow, close, atr):
            if close > fast > slow and fast - slow >= gap * atr:
                out["bias"] = "BUY"
            elif close < fast < slow and slow - fast >= gap * atr:
                out["bias"] = "SELL"
            out["ema_gap_atr"] = abs(fast - slow) / atr if atr else None
        if name == "ent_cci_wr_hook":
            adx = s.h1_adx.value or {}
            if out["bias"] and not (adx.get("adx") is not None and adx["adx"] >= 20 and
                                    (adx["plus_di"] > adx["minus_di"]) == (out["bias"] == "BUY")):
                out["bias"] = None
            out.update(cci=s.cci.value, williams_r=s.wr.value, h1_adx=adx.get("adx"), armed=dict(s.armed))
        else:
            er = s.er.value
            if out["bias"] and (er is None or er < s.params["er_min"]):
                out["bias"] = None
            out.update(h1_er=er, ha_run={1: "green", -1: "red"}.get(s.run_color), ha_run_bars=s.run_len)
        return out

    def summary(self) -> str:
        i, name = self.indicators(), self.spec.strategy
        if self.position:
            p = self.position
            return f"ไม้ {p['side']} สมมติยังไม่จบ · SL {fmt(p['sl'])} · TP {fmt(p['tp'])}"
        if i["atr14"] is None or i["h1_ema200"] is None:
            return "รอข้อมูล"
        if name == "rsi2_trend_pullback_r2":
            if i["rsi2"] is None:
                return "รอข้อมูล"
            bias = {"BUY": "แนวโน้มขาขึ้น · รอ RSI(2) ย่อ", "SELL": "แนวโน้มขาลง · รอ RSI(2) เด้ง"}.get(
                i["bias"], "ไม่มีแนวโน้มชัด · ไม่เข้า")
            return f"{bias} · RSI(2) {fmt(i['rsi2'], 1)} · ATR {fmt(i['atr14'])}"
        if name == "ent_cci_wr_hook":
            bias = {"BUY": "เทรนด์แรงขาขึ้น · รอ CCI หักหัวขึ้น", "SELL": "เทรนด์แรงขาลง · รอ CCI หักหัวลง"}.get(
                i["bias"], "ไม่มีเทรนด์แรง · ไม่เข้า")
            return f"{bias} · CCI {fmt(i['cci'], 0)} · %R {fmt(i['williams_r'], 0)}"
        bias = {"BUY": "เทรนด์แรงขาขึ้น · รอ HA ย่อแล้วกลับเขียว", "SELL": "เทรนด์แรงขาลง · รอ HA เด้งแล้วกลับแดง"}.get(
            i["bias"], "ไม่มีเทรนด์แรง · ไม่เข้า")
        return f"{bias} · ER {fmt(i['h1_er'])} · ห่าง EMA {fmt(i.get('ema_gap_atr'), 1)} ATR"


# ---------------------------------------------------------------------- TPSL London breakout
class TPSLCore:
    """Daily range breakout with two stop orders (one-cancels-other), tracked on M5 bars."""
    kind = "pending"

    def __init__(self, spec: StrategySpec, state: dict | None = None):
        self.spec, self.p = spec, TPSLParams.model_validate(spec.params)
        self.atr = indicators.ATR(14)
        self.day = self.range_high = self.range_low = None
        self.range_bars = 0
        self.orders = None      # {"BUY": {"level","sl","tp"}, ...} armed after the Risk check
        self.position = None
        self.traded_day = None  # one setup per day
        self.skip_reason = None
        self.alerted = False    # the setup was notified, so fills/updates are notified too
        self.m5_count = self.h1_count = 0
        if state:
            for key, value in state.items():
                setattr(self, key, load_value(value))

    FIELDS = ("atr", "day", "range_high", "range_low", "range_bars", "orders", "position", "traded_day",
              "skip_reason", "alerted", "m5_count", "h1_count")

    def dump(self) -> dict:
        return {key: dump_value(getattr(self, key)) for key in self.FIELDS}

    def _day_time(self, day, hour):
        return day * 86400 + hour * 3600

    def _expire(self, at_s, reason="TIME"):
        """Cancel pending orders and close an open trade (21:00 or a gap past the day)."""
        out = {"type": "expire", "had_orders": self.orders is not None, "had_position": self.position is not None,
               "at_server_s": at_s}
        exit_ = None
        if self.position:
            exit_ = {**self.position, "exit_reason": reason, "exit_bar_server_s": at_s}
        self.orders = self.position = None
        return out, exit_

    def step(self, bar, new_h1, point, digits):
        p, open_s = self.p, bar["open_server_s"]
        day, close_s = open_s // 86400, open_s + 300
        sp = round(bar["spread_points"] * point, digits)
        signals, exit_ = [], None
        atr = self.atr.update(bar)
        self.m5_count += 1
        self.h1_count += len(new_h1)
        if day != self.day:
            if self.orders or self.position:  # data gap across the close time
                sig, exit_ = self._expire(open_s)
                signals.append(sig)
            self.day, self.range_high, self.range_low, self.range_bars, self.skip_reason = day, None, None, 0, None
        t = open_s - day * 86400
        if (self.orders or self.position) and t >= p.close_h * 3600:
            sig, exit_ = self._expire(open_s)
            signals.append(sig)
        elif self.position:
            exit_ = self._manage(bar, sp, signals)
        elif self.orders:
            exit_ = self._fill(bar, sp, signals)
        if p.range_start_h * 3600 <= t < p.range_end_h * 3600:
            self.range_high = bar["high"] if self.range_high is None else max(self.range_high, bar["high"])
            self.range_low = bar["low"] if self.range_low is None else min(self.range_low, bar["low"])
            self.range_bars += 1
        if close_s == self._day_time(day, p.range_end_h) and (day + 3) % 7 < 5 and self.traded_day != day:
            setup = self._setup(bar, atr)
            if setup:
                signals.append(setup)
        return signals, exit_

    def _setup(self, bar, atr):
        p = self.p
        if self.range_bars < p.min_range_bars or self.range_high is None:
            self.skip_reason = "missing_bars"
            return None
        price = bar["close"]  # the 08:00 open is not known yet at the order time
        width = (self.range_high - self.range_low) / price * 100
        if width < p.min_width_pct or width > p.max_width_pct:
            self.skip_reason = "range_too_narrow" if width < p.min_width_pct else "range_too_wide"
            return None
        if atr is None:
            self.skip_reason = "missing_bars"
            return None
        cap = p.stop_cap_pct / 100 * price
        buy, sell = self.range_high + p.offset, self.range_low - p.offset
        buy_sl, sell_sl = max(self.range_low, buy - cap), min(self.range_high, sell + cap)
        orders = {"BUY": {"level": buy, "sl": buy_sl, "tp": buy + p.rr * (buy - buy_sl)},
                  "SELL": {"level": sell, "sl": sell_sl, "tp": sell - p.rr * (sell_sl - sell)}}
        self.traded_day = self.day
        return {"type": "setup", "orders": orders, "atr": atr, "range_high": self.range_high,
                "range_low": self.range_low, "width_pct": width,
                "cancel_server_s": self._day_time(self.day, p.close_h)}

    def arm(self, orders: dict, alerted: bool):
        """Keep the orders that passed the Risk check (Decimal levels from the engine)."""
        self.orders = {side: {k: format(v, "f") for k, v in o.items()} for side, o in orders.items()} or None
        self.alerted = alerted and self.orders is not None

    def _fill(self, bar, sp, signals):
        orders, o = self.orders, bar["open"]
        buy, sell = orders.get("BUY"), orders.get("SELL")
        ask_o, ask_h = o + sp, bar["high"] + sp
        buy_hit = buy is not None and ask_h >= float(buy["level"])
        sell_hit = sell is not None and bar["low"] <= float(sell["level"])
        if not (buy_hit or sell_hit):
            return None
        if buy_hit and sell_hit:
            side = "BUY" if abs(ask_o - float(buy["level"])) <= abs(o - float(sell["level"])) else "SELL"
        else:
            side = "BUY" if buy_hit else "SELL"
        order, d = orders[side], 1 if side == "BUY" else -1
        level = float(order["level"])
        entry = (ask_o if ask_o >= level else level) if d == 1 else (o if o <= level else level)
        self.orders = None
        self.position = {"side": side, "entry": entry, "level": order["level"], "sl": order["sl"],
                         "sl0": order["sl"], "tp": order["tp"], "risk": abs(level - float(order["sl"])),
                         "fill_bar_server_s": bar["open_server_s"], "breakeven": False}
        signals.append({"type": "fill", "side": side, "entry": entry, "level": order["level"], "sl": order["sl"],
                        "tp": order["tp"], "cancelled": [s for s in orders if s != side]})
        sl = float(order["sl"])
        if (d == 1 and bar["low"] <= sl) or (d == -1 and bar["high"] + sp >= sl):  # conservative fill bar
            pos, self.position = self.position, None
            return {**pos, "exit_reason": "SL", "exit_bar_server_s": bar["open_server_s"]}
        return None

    def _manage(self, bar, sp, signals):
        pos = self.position
        d = 1 if pos["side"] == "BUY" else -1
        o, h, l = (bar["open"], bar["high"], bar["low"]) if d == 1 else (bar["open"] + sp, bar["high"] + sp, bar["low"] + sp)
        sl, tp = float(pos["sl"]), float(pos["tp"])
        kind = "BE" if pos["breakeven"] else "SL"
        if d == 1:
            reason = kind if (o <= sl or l <= sl) else "TP" if (o >= tp or h >= tp) else None
        else:
            reason = kind if (o >= sl or h >= sl) else "TP" if (o <= tp or l <= tp) else None
        if reason:
            self.position = None
            return {**pos, "exit_reason": reason, "exit_bar_server_s": bar["open_server_s"]}
        favour = (h - pos["entry"]) if d == 1 else (pos["entry"] - l)
        if self.p.breakeven_r is not None and not pos["breakeven"] and favour >= self.p.breakeven_r * pos["risk"]:
            pos["sl"], pos["breakeven"] = format(Decimal(str(round(pos["entry"], 2))), "f"), True
            signals.append({"type": "breakeven", "side": pos["side"], "sl": pos["sl"], "tp": pos["tp"]})
        return None

    def indicators(self) -> dict:
        p = self.p
        phase = ("position" if self.position else "orders" if self.orders else
                 "done" if self.traded_day == self.day and self.day is not None else
                 "skipped" if self.skip_reason else "range")
        return {"phase": phase, "range_high": self.range_high, "range_low": self.range_low,
                "range_bars": self.range_bars, "orders": self.orders, "skip_reason": self.skip_reason,
                "atr14": self.atr.value, "bias": None,
                "window": f"{p.range_start_h:02d}:00–{p.range_end_h:02d}:00", "close": f"{p.close_h:02d}:00"}

    def summary(self) -> str:
        i, p = self.indicators(), self.p
        if self.position:
            pos = self.position
            be = " · SL อยู่ที่ทุนแล้ว" if pos["breakeven"] else ""
            return f"ไม้ {pos['side']} สมมติยังไม่จบ · SL {fmt(pos['sl'])} · TP {fmt(pos['tp'])}{be} · ปิด {i['close']}"
        if self.orders:
            parts = [f"{'Buy' if s == 'BUY' else 'Sell'} Stop {fmt(o['level'])}" for s, o in self.orders.items()]
            return " / ".join(parts) + f" · รอราคาชน · ยกเลิก {i['close']}"
        if i["phase"] == "skipped":
            return {"range_too_narrow": "กรอบวันนี้แคบเกิน · ไม่เทรด", "range_too_wide": "กรอบวันนี้กว้างเกิน · ไม่เทรด",
                    "missing_bars": "ข้อมูลช่วงกรอบไม่ครบ · ไม่เทรด"}.get(self.skip_reason, "วันนี้ไม่เทรด")
        if i["phase"] == "done":
            return "วันนี้ตั้งคำสั่งไปแล้ว · รอวันถัดไป"
        if self.range_high is not None:
            return f"กำลังสร้างกรอบ {i['window']} · H {fmt(self.range_high)} / L {fmt(self.range_low)}"
        return f"รอสร้างกรอบ {i['window']} (เวลา server) · ตั้งคำสั่ง {p.range_end_h:02d}:00"


def make_core(spec: StrategySpec, state: dict | None = None):
    return (TPSLCore if spec.kind == "pending" else MarketCore)(spec, state)


def rules(spec: StrategySpec) -> list[list[str]]:
    """Short Thai rule rows for the dashboard: [[label, value], ...]."""
    p = spec.effective_params
    hours = lambda: f"{p['start_hour']:02d}:00–{p['end_hour']:02d}:00 server · ไม่เข้าศุกร์หลัง 20:00"
    if spec.strategy == "rsi2_trend_pullback_r2":
        stop = p["exits"].get("time_stop_bars")
        return [["แนวโน้ม", "H1 EMA50/EMA200 + M5 EMA200"],
                ["จังหวะเข้า", f"RSI(2) ≤ {p['rsi_low']} แล้วปิดเหนือ high ก่อนหน้า (SELL กลับด้าน)"],
                ["Stop Loss", f"{p['sl_atr']} × ATR14"], ["Take Profit", f"RR สุทธิ {p['rr']} หลังหักต้นทุน"],
                ["ปิดตามเวลา", f"{stop} แท่ง M5 ({stop * 5 / 60:g} ชม.)" if stop else "ไม่มี"], ["ช่วงเวลาเข้า", hours()]]
    if spec.strategy == "ent_cci_wr_hook":
        return [["แนวโน้ม", f"H1 close > EMA50 > EMA200 ห่าง ≥ {p['gap_atr']} ATR · ADX ≥ 20"],
                ["จังหวะเข้า", f"CCI(20) เคยถึง ±{p['cci_level']} แล้วหักกลับผ่าน ±100 · %R ยืนยัน"],
                ["Stop Loss", "Low/High 8 แท่ง ± 0.2 ATR (1.1–2.8 ATR)"], ["Take Profit", f"RR สุทธิ {p['rr']} หลังหักต้นทุน"],
                ["ปิดตามเวลา", "ไม่มี ถือจนถึง SL/TP"], ["ช่วงเวลาเข้า", hours()]]
    if spec.strategy == "ent_heikin_ashi_flip":
        return [["แนวโน้ม", f"H1 EMA50/EMA200 ห่าง ≥ {p['gap_atr']} ATR · ER(10) ≥ {p['er_min']}"],
                ["จังหวะเข้า", "HA แดง 2–12 แท่งย่อถึง EMA20 แล้วกลับเขียว (SELL กลับด้าน)"],
                ["Stop Loss", "ปลายช่วงย่อ ± 0.2 ATR (1.1–2.8 ATR)"], ["Take Profit", f"RR สุทธิ {p['rr']} หลังหักต้นทุน"],
                ["ปิดตามเวลา", "ไม่มี ถือจนถึง SL/TP"], ["ช่วงเวลาเข้า", hours()]]
    be = f" · SL เป็นทุนที่ +{p['breakeven_r']:g}R" if p["breakeven_r"] else ""
    return [["กรอบราคา", f"High/Low {p['range_start_h']:02d}:00–{p['range_end_h']:02d}:00 server · กว้าง "
                          f"{p['min_width_pct']:g}–{p['max_width_pct']:g}% ของราคา"],
            ["คำสั่งรอ", f"Buy Stop ที่ High + {p['offset']:g} / Sell Stop ที่ Low − {p['offset']:g} · ชนก่อนยกเลิกอีกฝั่ง"],
            ["Stop Loss", f"อีกฝั่งของกรอบ ไม่เกิน {p['stop_cap_pct']:g}% ของราคา"],
            ["Take Profit", f"{p['rr']:g}R จากราคาคำสั่ง{be}"],
            ["ปิด", f"{p['close_h']:02d}:00 server · วันละไม่เกิน 1 ไม้ · จันทร์–ศุกร์"]]
