"""Deterministic bar-replay engine: closed-M5 signals, next-bar fills, M1 exit path.

Cost and sizing rules mirror the live Paper flow (``app/risk_manager.py`` and
``app/paper_trading.py``) with values read from ``config/risk-xm-paper.json`` and
``config/paper-xm.json``:

* A strategy sees a closed M5 bar at its close time ``open_server_s + 300``. Higher frames
  become visible only once their nominal close is <= that time.
* Entry fills at the open of the next available M1 bar: BUY at ask (+slippage, ceiling to tick),
  SELL at bid (-slippage, floor to tick). A signal older than ``max_signal_age_seconds`` at that
  bar (e.g. across a weekend gap) expires, as it would in the live approval flow.
* SL/TP are checked on every M1 bar from the fill bar onward, on bid for BUY and on
  ask (= bid + spread_points × point) for SELL. When SL and TP are both reachable in one M1
  bar, SL is assumed first. A gap through a level fills at the bar open.
* Exits pay slippage, commission is half the round trip per side, quantity is sized with the
  Paper formula (risk % of current equity, Risk-approved cap from policy equity, notional cap).
* One position at a time. Daily loss lock, AI review and quote-age checks are not modelled.
"""
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from pathlib import Path

from app.config import ROOT
from app.paper_trading import PaperPolicy
from app.risk_manager import RiskPolicy
from app.xm_engine import tick_round

from .data import FRAMES, History, ask_price

RISK_POLICY = ROOT / "config/risk-xm-paper.json"
PAPER_POLICY = ROOT / "config/paper-xm.json"
EXIT_KEYS = ("breakeven_r", "breakeven_lock_r", "trail_start_r", "trail_r", "time_stop_bars")


def D(value) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def to_tick(value: Decimal, tick: Decimal, rounding) -> Decimal:
    return (value / tick).to_integral_value(rounding=rounding) * tick


@dataclass
class Context:
    """What a strategy may see at an M5 close. Lists are shared; do not mutate them."""
    time_s: int
    bar: dict
    index: int
    m5: list
    frames: dict
    new: dict
    position: dict | None
    equity: float
    point: float
    digits: int
    tick: float

    @property
    def h1(self):
        return self.frames["H1"]

    @property
    def h4(self):
        return self.frames["H4"]

    @property
    def d1(self):
        return self.frames["D1"]

    def ask(self, bar=None, key="close"):
        bar = self.bar if bar is None else bar
        return ask_price(bar[key], bar["spread_points"], self.point, self.digits)

    def spread_price(self, bar=None):
        bar = self.bar if bar is None else bar
        return round(bar["spread_points"] * self.point, self.digits)


@dataclass
class Backtest:
    history: History
    strategy: object
    risk_policy: RiskPolicy = None
    paper_policy: PaperPolicy = None
    trade_from_s: int | None = None
    trade_until_s: int | None = None
    live_risk_gate: bool = False
    trades: list = field(default_factory=list)
    signal_stats: Counter = field(default_factory=Counter)

    def __post_init__(self):
        self.p = self.risk_policy or RiskPolicy.load(RISK_POLICY)
        self.paper = self.paper_policy or PaperPolicy.load(PAPER_POLICY)
        self.tick = D(self.p.price_tick)
        self.balance = D(self.paper.initial_balance_usd)
        self.peak = self.balance
        self.max_dd = Decimal(0)
        self.position = None
        self.pending = None
        self.m5 = []
        self.frames = {name: [] for name in ("H1", "H4", "D1")}
        self.queued = {name: [] for name in self.frames}
        self.current = {name: None for name in ("M5", *self.frames)}

    # ------------------------------------------------------------------ helpers
    def ask(self, bid, bar):
        return ask_price(bid, bar["spread_points"], self.history.point, self.history.digits)

    def in_window(self, t):
        return (self.trade_from_s is None or t >= self.trade_from_s) and (self.trade_until_s is None or t < self.trade_until_s)

    # ------------------------------------------------------------------ main loop
    def run(self):
        for bar in self.history.m1:
            t = bar["open_server_s"]
            closed = self._roll(t)
            if closed is not None:
                self._on_m5_close(closed)
            if self.position and self.trade_until_s is not None and t >= self.trade_until_s:
                self._close(bar, D(bar["open"]) if self.position["side"] == "BUY" else D(self.ask(bar["open"], bar)), "window_end", t)
            if self.pending is not None:
                signal, self.pending = self.pending, None
                if self.position is None:
                    self._enter(signal, bar)
                else:
                    self.signal_stats["ignored_in_position"] += 1
            if self.position:
                self._manage(bar)
            self._aggregate(bar)
        closed = self._roll(None)
        if closed is not None:
            self._on_m5_close(closed)
        if self.pending is not None:
            self.signal_stats["no_next_bar"] += 1
            self.pending = None
        if self.position:
            last = self.history.m1[-1]
            price = last["close"] if self.position["side"] == "BUY" else self.ask(last["close"], last)
            self._close(last, D(price), "end_of_data", last["open_server_s"] + 60)
        return self.result()

    def _roll(self, t):
        """Finalize buckets that M1 time ``t`` (None = end of data) has left."""
        closed_m5 = None
        for name, cur in self.current.items():
            if cur is None or (t is not None and t // FRAMES[name] * FRAMES[name] == cur["open_server_s"]):
                continue
            self.current[name] = None
            if name == "M5":
                closed_m5 = cur
            else:
                self.queued[name].append(cur)
        return closed_m5

    def _aggregate(self, bar):
        for name, cur in self.current.items():
            period = FRAMES[name]
            if cur is None:
                self.current[name] = {"open_server_s": bar["open_server_s"] // period * period, "period_s": period,
                                      "open": bar["open"], "high": bar["high"], "low": bar["low"], "close": bar["close"],
                                      "tick_volume": bar["tick_volume"], "spread_points": bar["spread_points"],
                                      "spread_max_points": bar["spread_points"]}
            else:
                cur["high"] = max(cur["high"], bar["high"])
                cur["low"] = min(cur["low"], bar["low"])
                cur["close"] = bar["close"]
                cur["tick_volume"] += bar["tick_volume"]
                cur["spread_points"] = bar["spread_points"]
                cur["spread_max_points"] = max(cur["spread_max_points"], bar["spread_points"])

    def _on_m5_close(self, bar):
        time_s = bar["open_server_s"] + 300
        new = {"M5": [bar]}
        for name, queue in self.queued.items():
            ready = [b for b in queue if b["open_server_s"] + FRAMES[name] <= time_s]
            self.queued[name] = [b for b in queue if b["open_server_s"] + FRAMES[name] > time_s]
            self.frames[name].extend(ready)
            new[name] = ready
        self.m5.append(bar)
        ctx = Context(time_s, bar, len(self.m5) - 1, self.m5, self.frames, new,
                      None if self.position is None else dict(self.position), float(self.balance),
                      self.history.point, self.history.digits, float(self.tick))
        signal = self.strategy.on_bar(ctx)
        if not signal:
            return
        self.signal_stats["emitted"] += 1
        if not self.in_window(time_s):
            self.signal_stats["outside_window"] += 1
            return
        if signal.get("side") not in ("BUY", "SELL") or signal.get("sl") is None or signal.get("tp") is None:
            raise ValueError(f"Strategy returned an invalid signal: {signal}")
        self.pending = {**signal, "signal_time_s": time_s, "signal_close": bar["close"],
                        "signal_spread": round(bar["spread_points"] * self.history.point, self.history.digits)}

    # ------------------------------------------------------------------ entry
    def _risk_reasons(self, s, entry, stop, target, fill_bid, fill_ask):
        """Live Risk/Paper strategy filters (reported always, enforced with ``live_risk_gate``)."""
        p, v = self.p, D(self.p.usd_per_price_unit_per_quantity)
        reasons = []
        signal_entry = tick_round(s["signal_close"], self.tick)
        spread_used = max(D(p.spread_price), D(s["signal_spread"]))
        costs = (spread_used + 2 * D(p.slippage_price_per_side)) * v + D(p.commission_usd_per_unit_round_trip)
        loss = abs(signal_entry - stop) * v + costs
        reward = abs(target - signal_entry) * v - costs
        if reward / loss < D(p.min_net_reward_risk):
            reasons.append("net_reward_risk_too_low")
        loss_fill = (abs(entry - stop) + D(p.slippage_price_per_side)) * v + D(p.commission_usd_per_unit_round_trip)
        reward_fill = (abs(target - entry) - D(p.slippage_price_per_side)) * v - D(p.commission_usd_per_unit_round_trip)
        if reward_fill / loss_fill < D(p.min_net_reward_risk):
            reasons.append("fill_net_reward_risk")
        if s.get("atr"):
            atr = D(s["atr"])
            if not D(p.min_stop_atr) <= abs(signal_entry - stop) / atr <= D(p.max_stop_atr):
                reasons.append("stop_atr_out_of_range")
            if spread_used / atr > D(p.max_spread_atr):
                reasons.append("spread_atr_too_high")
            if abs(entry - signal_entry) > atr * D(self.paper.max_entry_drift_atr):
                reasons.append("entry_price_drift")
            if (D(fill_ask) - D(fill_bid)) / atr > D(p.max_spread_atr) and "spread_atr_too_high" not in reasons:
                reasons.append("spread_atr_too_high")
        return reasons, signal_entry, spread_used

    def _enter(self, s, bar):
        p, v = self.p, D(self.p.usd_per_price_unit_per_quantity)
        t = bar["open_server_s"]
        if t - s["signal_time_s"] > p.max_signal_age_seconds:
            self.signal_stats["expired_before_fill"] += 1
            return
        side = s["side"]
        direction = 1 if side == "BUY" else -1
        bid, ask = bar["open"], self.ask(bar["open"], bar)
        slip = D(p.slippage_price_per_side)
        quote = D(ask if direction == 1 else bid)
        entry = to_tick(quote + direction * slip, self.tick, ROUND_CEILING if direction == 1 else ROUND_FLOOR)
        stop, target = tick_round(s["sl"], self.tick), tick_round(s["tp"], self.tick)
        if not (stop < entry < target if direction == 1 else target < entry < stop):
            self.signal_stats["fill_outside_sl_tp"] += 1
            return
        reasons, signal_entry, spread_used = self._risk_reasons(s, entry, stop, target, bid, ask)
        if reasons and self.live_risk_gate:
            self.signal_stats["live_risk_rejected"] += 1
            for code in reasons:
                self.signal_stats["live_risk:" + code] += 1
            return
        commission = D(p.commission_usd_per_unit_round_trip)
        loss = (abs(entry - stop) + slip) * v + commission
        reward = (abs(target - entry) - slip) * v - commission
        # Risk-approved cap (policy equity, signal entry) then Paper sizing on current equity.
        risk_costs = (spread_used + 2 * slip) * v + commission
        risk_loss = abs(signal_entry - stop) * v + risk_costs
        budget = D(p.equity_usd) * D(p.risk_percent) / 100
        approved = min(budget / risk_loss, D(p.max_quantity),
                       D(p.equity_usd) * D(p.max_notional_equity_multiple) / (signal_entry * v))
        approved = to_tick(approved, D(p.quantity_step), ROUND_FLOOR)
        equity = self.balance
        fraction, mult = D(p.risk_percent) / 100, D(p.max_notional_equity_multiple)
        liquidation = D(bid if direction == 1 else ask)
        friction = (entry - liquidation) * direction * v + commission / 2
        raw = min(approved, D(p.max_quantity), equity * fraction / (loss + fraction * friction),
                  equity * mult / (entry * v + mult * friction))
        quantity = to_tick(raw, D(p.quantity_step), ROUND_FLOOR)
        if equity <= 0 or quantity < D(p.min_quantity):
            self.signal_stats["risk_budget_below_minimum"] += 1
            return
        entry_fee = quantity * commission / 2
        self.balance -= entry_fee
        exits = {k: s[k] for k in EXIT_KEYS if s.get(k) is not None}
        self.position = {
            "side": side, "direction": direction, "signal_time_s": s["signal_time_s"], "entry_time_s": t,
            "entry": entry, "signal_entry": signal_entry, "sl": stop, "initial_sl": stop, "tp": target,
            "quantity": quantity, "entry_fee": entry_fee, "loss_per_unit": loss, "reward_per_unit": reward,
            "balance_before": equity, "equity_low": equity - entry_fee, "mfe": Decimal(0), "mae": Decimal(0),
            "stop_kind": "SL", "exits": exits, "live_risk_reasons": reasons,
            "tag": s.get("tag"), "atr": s.get("atr"),
        }
        self.signal_stats["filled"] += 1

    # ------------------------------------------------------------------ position
    def _manage(self, bar):
        pos = self.position
        direction, t = pos["direction"], bar["open_server_s"]
        stop_bars = pos["exits"].get("time_stop_bars")
        if stop_bars and t >= pos["signal_time_s"] + int(stop_bars) * 300:
            price = bar["open"] if direction == 1 else self.ask(bar["open"], bar)
            self._close(bar, D(price), "TIME", t)
            return
        if direction == 1:
            o, h, l = bar["open"], bar["high"], bar["low"]
        else:
            o, h, l = (self.ask(bar[k], bar) for k in ("open", "high", "low"))
        sl, tp = float(pos["sl"]), float(pos["tp"])
        hit = None
        if direction == 1:
            hit = (o, pos["stop_kind"]) if o <= sl else (sl, pos["stop_kind"]) if l <= sl else (o, "TP") if o >= tp else (tp, "TP") if h >= tp else None
        else:
            hit = (o, pos["stop_kind"]) if o >= sl else (sl, pos["stop_kind"]) if h >= sl else (o, "TP") if o <= tp else (tp, "TP") if l <= tp else None
        # Track excursions on this bar's path (only reached when no level was hit above, or for MAE).
        favourable = D(h) - pos["entry"] if direction == 1 else pos["entry"] - D(l)
        adverse = pos["entry"] - D(l) if direction == 1 else D(h) - pos["entry"]
        if hit:
            level = D(hit[0])
            adverse = max(Decimal(0), (pos["entry"] - level) * direction) if hit[1] != "TP" else adverse
            favourable = (level - pos["entry"]) * direction if hit[1] == "TP" else favourable
        pos["mfe"], pos["mae"] = max(pos["mfe"], favourable), max(pos["mae"], adverse)
        worst = pos["balance_before"] - pos["entry_fee"] - pos["mae"] * pos["quantity"] * D(self.p.usd_per_price_unit_per_quantity)
        pos["equity_low"] = min(pos["equity_low"], worst)
        if hit:
            self._close(bar, D(hit[0]), hit[1], t + 60)
            return
        self._adjust_stop(pos, favourable)

    def _adjust_stop(self, pos, favourable):
        """Breakeven/trailing from this bar's extreme; the new stop applies from the next M1 bar."""
        e = pos["exits"]
        direction, entry = pos["direction"], pos["entry"]
        risk = abs(entry - pos["initial_sl"])
        candidates = []
        if e.get("breakeven_r") is not None and favourable >= D(e["breakeven_r"]) * risk:
            candidates.append((entry + direction * D(e.get("breakeven_lock_r", 0)) * risk, "BE"))
        if e.get("trail_r") is not None and favourable >= D(e.get("trail_start_r", e["trail_r"])) * risk:
            extreme = entry + direction * favourable
            candidates.append((extreme - direction * D(e["trail_r"]) * risk, "TRAIL"))
        for level, kind in candidates:
            level = to_tick(level, self.tick, ROUND_FLOOR if direction == 1 else ROUND_CEILING)
            if (level - pos["sl"]) * direction > 0:
                pos["sl"], pos["stop_kind"] = level, kind

    def _close(self, bar, quote: Decimal, reason, at_s):
        pos, p = self.position, self.p
        direction, v = pos["direction"], D(p.usd_per_price_unit_per_quantity)
        raw = quote - direction * D(p.slippage_price_per_side)
        exit_price = to_tick(raw, self.tick, ROUND_FLOOR if direction == 1 else ROUND_CEILING)
        quantity = pos["quantity"]
        gross = (exit_price - pos["entry"]) * quantity * direction * v
        exit_fee = quantity * D(p.commission_usd_per_unit_round_trip) / 2
        net = gross - pos["entry_fee"] - exit_fee
        self.balance += gross - exit_fee
        planned_loss = pos["loss_per_unit"] * quantity
        # Worst marked equity during the trade is measured against the peak before it.
        low = min(pos["equity_low"], self.balance)
        dd = (self.peak - low) / self.peak * 100 if self.peak > 0 else Decimal(0)
        self.max_dd = max(self.max_dd, dd)
        self.peak = max(self.peak, self.balance)
        risk = abs(pos["entry"] - pos["initial_sl"])
        self.trades.append({
            "id": len(self.trades) + 1, "side": pos["side"], "tag": pos["tag"],
            "signal_time_s": pos["signal_time_s"], "entry_time_s": pos["entry_time_s"], "exit_time_s": at_s,
            "entry": float(pos["entry"]), "signal_entry": float(pos["signal_entry"]), "exit": float(exit_price),
            "sl_initial": float(pos["initial_sl"]), "sl_final": float(pos["sl"]), "tp": float(pos["tp"]),
            "quantity": float(quantity), "gross_pnl_usd": float(gross), "fees_usd": float(pos["entry_fee"] + exit_fee),
            "net_pnl_usd": float(net), "r": float(net / planned_loss) if planned_loss else 0.0,
            "planned_net_rr": float(pos["reward_per_unit"] / pos["loss_per_unit"]),
            "planned_gross_rr": float(abs(pos["tp"] - pos["entry"]) / risk) if risk else 0.0,
            "exit_reason": reason, "bars_held_m5": max(0, (at_s - pos["entry_time_s"]) // 300),
            "mfe_r": float(pos["mfe"] / risk) if risk else 0.0, "mae_r": float(pos["mae"] / risk) if risk else 0.0,
            "live_risk_reasons": pos["live_risk_reasons"], "balance_after": float(self.balance),
            "equity_low": float(low), "drawdown_pct_at_close": float(dd),
        })
        self.position = None

    def result(self):
        return {"trades": self.trades, "signal_stats": dict(self.signal_stats),
                "final_balance_usd": float(self.balance), "initial_balance_usd": float(self.paper.initial_balance_usd),
                "max_drawdown_pct": float(self.max_dd), "m5_bars": len(self.m5),
                "first_m1_s": self.history.m1[0]["open_server_s"], "last_m1_s": self.history.m1[-1]["open_server_s"],
                "trade_from_s": self.trade_from_s, "trade_until_s": self.trade_until_s,
                "live_risk_gate": self.live_risk_gate}


def policy_snapshot(p: RiskPolicy, paper: PaperPolicy):
    return {"risk": p.model_dump(mode="json", exclude_none=True), "paper": paper.model_dump(mode="json")}


def load_policies(risk_path: Path = RISK_POLICY, paper_path: Path = PAPER_POLICY):
    return RiskPolicy.load(Path(risk_path)), PaperPolicy.load(Path(paper_path))
