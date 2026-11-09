"""Alert-only XM engine: closed MT5 bars -> selected strategies -> Risk level checks -> alerts.

No AI, no Paper account and no orders. Every strategy listed in ``config/alert-xm.json`` is
computed on every closed M5 bar by its own core (``app/alert_strategies.py``) with the backtest's
visibility rules, and keeps its own hypothetical trade, so each strategy's alerts correspond to
its standalone backtest:

* a strategy sees a closed M5 bar at its close; an H1 bar is visible once its close <= that time;
* after a signal that passes the Risk level checks, a hypothetical trade blocks that strategy's
  new signals until its SL/TP is touched on a later M5 bar or its time stop elapses. It is never
  sized, filled or booked.

Which strategies notify is a runtime selection stored in ``control_meta`` (XM Control or
``POST /strategies``); all configured strategies keep computing, so switching needs no warm-up.
With ``conflict="skip"`` a strategy's new signal is rejected while another selected strategy holds
a hypothetical trade; with ``"warn"`` (default) the alert carries a note about the other trade.

State is a JSON checkpoint in its own database, separate from the Paper account.
"""
import hashlib
import json
import time
from datetime import datetime
from decimal import Decimal, localcontext
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .alert_strategies import StrategySpec, make_core
from .config import ROOT
from .mt5_bridge import Snapshot
from .risk_manager import RiskPolicy, level_metrics
from .xm_engine import tick_round

STATE_VERSION = 2
SELECTION_KEY = "alert_strategies"


class AlertPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: Literal["xm-alert-v2"]
    mode: Literal["alert_only"]
    strategies: Annotated[tuple[StrategySpec, ...], Field(min_length=1, max_length=8)]
    conflict: Literal["warn", "skip"] = "warn"
    risk_policy: Annotated[str, Field(min_length=1, max_length=200)]
    max_entry_drift_atr: Annotated[Decimal, Field(gt=0, le=5, allow_inf_nan=False)]
    warmup_h1_bars: Annotated[int, Field(ge=200, le=5000, strict=True)]
    warmup_m5_bars: Annotated[int, Field(ge=200, le=5000, strict=True)]

    @model_validator(mode="after")
    def unique(self):
        ids = [s.id for s in self.strategies]
        versions = [s.strategy_version for s in self.strategies]
        if len(set(ids)) != len(ids) or len(set(versions)) != len(versions):
            raise ValueError("Strategy ids and strategy_version values must be unique")
        return self

    @classmethod
    def load(cls, path: Path):
        return cls.model_validate_json(Path(path).read_text())

    def risk_path(self) -> Path:
        path = Path(self.risk_policy)
        return path if path.is_absolute() else ROOT / path

    def spec(self, strategy_id) -> StrategySpec:
        return next(s for s in self.strategies if s.id == strategy_id)

    @property
    def default_selection(self) -> list[str]:
        chosen = [s.id for s in self.strategies if s.enabled]
        return chosen or [self.strategies[0].id]


def gate(risk: RiskPolicy, side, close, sl, tp, atr, spreads):
    """Tick-rounded levels and the shared Risk level checks for one strategy signal."""
    tick = risk.price_tick
    entry, stop, target = (tick_round(value, tick) for value in (close, sl, tp))
    with localcontext() as ctx:
        ctx.prec = 40
        spread = max([risk.spread_price, *(Decimal(str(value)) for value in spreads)])
        reasons, values = level_metrics(risk, entry, stop, target, Decimal(str(atr)), spread)
    if not (stop < entry < target if side == "BUY" else target < entry < stop):
        reasons.insert(0, "levels_inconsistent")
    return entry, stop, target, reasons, values


def local_time(utc_s) -> str:
    return datetime.fromtimestamp(utc_s).strftime("%H:%M")


def price(value, digits) -> str:
    return f"{Decimal(str(value)):,.{digits}f}"


def side_name(side) -> str:
    return "Buy" if side == "BUY" else "Sell"


def alert_text(payload, conflicts=()) -> tuple[str, str]:
    """Title and message for a market-entry alert."""
    d, side = payload["digits"], payload["side"]
    label = payload.get("strategy_label")
    limit = ("Ask ≤ " if side == "BUY" else "Bid ≥ ") + price(payload["entry_limit"], d)
    title = f"เข้า {side} ได้ · {payload['source_symbol']}" + (f" · {label}" if label else "")
    exit_at = (f"ถ้ายังไม่ถึง SL/TP ให้ปิดเวลา {local_time(payload['time_stop_utc_s'])}"
               if payload.get("time_stop_utc_s") else "ไม่มีปิดตามเวลา ถือจนถึง SL/TP")
    message = (f"ราคาอ้างอิง {price(payload['entry'], d)} · SL {price(payload['sl'], d)} · TP {price(payload['tp'], d)}"
               f" · RR สุทธิ {Decimal(payload['net_reward_risk']):.2f}\n"
               f"เข้าภายใน {local_time(payload['valid_until_utc_s'])} เมื่อ {limit} · {exit_at}")
    return title, message + conflict_note(conflicts)


def setup_text(payloads, conflicts=()) -> tuple[str, str]:
    """Title and message for a pending-order (TPSL) setup; ``payloads`` are the accepted sides."""
    first = payloads[0]
    d, label = first["digits"], first.get("strategy_label", "")
    names = " / ".join(f"{side_name(p['side'])} Stop" for p in payloads)
    title = f"ตั้ง {names} · {first['source_symbol']}" + (f" · {label}" if label else "")
    lines = [f"{side_name(p['side'])} Stop {price(p['entry'], d)} · SL {price(p['sl'], d)} · TP {price(p['tp'], d)}"
             f" · RR สุทธิ {Decimal(p['net_reward_risk']):.2f}" for p in payloads]
    rules = [f"ตั้งภายใน {local_time(first['valid_until_utc_s'])}"]
    if len(payloads) > 1:
        rules.append("ฝั่งที่ถูกชนก่อนให้ยกเลิกอีกฝั่ง")
    if first.get("breakeven_r"):
        rules.append(f"เลื่อน SL เป็นทุนเมื่อได้ +{first['breakeven_r']:g}R")
    rules.append(f"ยกเลิก/ปิดทั้งหมดเวลา {local_time(first['cancel_at_utc_s'])}")
    return title, "\n".join(lines) + "\n" + " · ".join(rules) + conflict_note(conflicts)


def conflict_note(conflicts) -> str:
    if not conflicts:
        return ""
    return "\n⚠️ ขัดกับไม้สมมติที่ยังเปิดอยู่: " + ", ".join(f"{label} {side}" for label, side in conflicts)


class AlertEngine:
    def __init__(self, db, policy: AlertPolicy, risk: RiskPolicy):
        self.db, self.policy, self.risk = db, policy, risk
        if risk.source_server is None or risk.source_broker_symbol is None:
            raise ValueError("Alert mode requires a Risk policy pinned to an MT5 server and symbol")
        alert = policy.model_dump(mode="json")
        for spec in alert["strategies"]:  # display name and default selection do not change computation
            spec.pop("label"), spec.pop("enabled")
        material = {"alert": alert, "params": {s.id: s.effective_params for s in policy.strategies},
                    "risk": risk.model_dump(mode="json", exclude_none=True), "state_version": STATE_VERSION}
        self.config_hash = hashlib.sha256(json.dumps(material, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        with db.connect() as conn:
            ensure_tables(conn)

    # ------------------------------------------------------------------ reads
    def status(self):
        with self.db.connect() as conn:
            row = conn.execute("SELECT status_json, updated_at FROM alert_engine WHERE id=1").fetchone()
        return {"status": "waiting_for_feed"} if row is None else {**json.loads(row["status_json"]), "updated_at": row["updated_at"]}

    def alerts(self, after_id=0, limit=50, newest_first=False):
        order = "DESC" if newest_first else "ASC"
        where = "" if newest_first else "WHERE id>?"
        args = (limit,) if newest_first else (after_id, limit)
        with self.db.connect() as conn:
            rows = conn.execute(f"SELECT * FROM trade_alerts {where} ORDER BY id {order} LIMIT ?", args).fetchall()
        return [{"id": r["id"], "status": r["status"], "created_at": r["created_at"], **json.loads(r["payload_json"])} for r in rows]

    def note(self, status):
        with self.db.connect() as conn:
            row = conn.execute("SELECT state_json, status_json FROM alert_engine WHERE id=1").fetchone()
            previous = json.loads(row["status_json"]) if row else {}
            keep = ("warmup", "position", "indicators", "last_m5_server_s", "strategies")
            current = {**{k: v for k, v in previous.items() if k in keep}, "status": status}
            conn.execute("""INSERT INTO alert_engine VALUES(1,?,?,?) ON CONFLICT(id) DO UPDATE SET
                status_json=excluded.status_json, updated_at=excluded.updated_at""",
                         (row["state_json"] if row else "{}", json.dumps(current), time.time()))

    @staticmethod
    def muted(conn) -> bool:
        row = conn.execute("SELECT value FROM control_meta WHERE key='alerts_muted'").fetchone()
        return row is not None and row[0] == "1"

    def set_muted(self, muted: bool):
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            changed = self.muted(conn) != muted
            conn.execute("INSERT INTO control_meta VALUES('alerts_muted',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                         ("1" if muted else "0",))
            if changed:
                event(conn, f"mute:{time.time_ns()}", "alerts_muted" if muted else "alerts_unmuted", "info",
                      "ปิดเสียงแจ้งเตือนสัญญาณ" if muted else "เปิดแจ้งเตือนสัญญาณ",
                      "ยังคำนวณและบันทึกสัญญาณต่อ แต่ไม่ส่งการแจ้งเตือน" if muted else "แจ้งเตือนเมื่อมีจังหวะเข้า trade ใหม่")
        return {"muted": muted, "changed": changed}

    # ------------------------------------------------------------------ strategy selection
    def selection(self, conn=None) -> list[str]:
        """Strategy ids that notify, in config order (stored choice, else the config defaults)."""
        if conn is None:
            with self.db.connect() as own:
                return self.selection(own)
        row = conn.execute("SELECT value FROM control_meta WHERE key=?", (SELECTION_KEY,)).fetchone()
        known = [s.id for s in self.policy.strategies]
        if row is None:
            return self.policy.default_selection
        try:
            stored = set(json.loads(row[0]))
        except (ValueError, TypeError):
            return self.policy.default_selection
        chosen = [i for i in known if i in stored]
        return chosen or self.policy.default_selection

    def set_selection(self, ids):
        known = [s.id for s in self.policy.strategies]
        unknown = sorted(set(ids) - set(known))
        if unknown:
            raise ValueError(f"Unknown strategy ids: {unknown}")
        if not ids:
            raise ValueError("Select at least one strategy (use mute to silence alerts)")
        chosen = [i for i in known if i in set(ids)]
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            changed = self.selection(conn) != chosen
            conn.execute("INSERT INTO control_meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                         (SELECTION_KEY, json.dumps(chosen)))
            if changed:
                labels = ", ".join(self.policy.spec(i).label for i in chosen)
                event(conn, f"strategies:{time.time_ns()}", "strategies_changed", "info", "เปลี่ยนกลยุทธ์ที่แจ้งเตือน",
                      f"แจ้งเตือนจาก: {labels}")
        return {"enabled": chosen, "changed": changed}

    # ------------------------------------------------------------------ processing
    def process(self, snapshot: Snapshot, now=None):
        now = time.time() if now is None else now
        risk = self.risk
        if snapshot.server != risk.source_server or snapshot.broker_symbol != risk.source_broker_symbol:
            self.note("source_mismatch")
            return self.status()
        if not -30 <= now - snapshot.captured_at_utc_s <= 120:
            self.note("feed_stale")
            return self.status()
        if Decimal(str(snapshot.tick_size)) != risk.price_tick:
            self.note("tick_policy_mismatch")
            return self.status()
        if len(snapshot.m5) < 200 or len(snapshot.h1) < 200:
            self.note("insufficient_history")
            return self.status()
        incoming = {"m5": [b.model_dump() for b in snapshot.m5], "h1": [b.model_dump() for b in snapshot.h1]}
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT state_json FROM alert_engine WHERE id=1").fetchone()
            state = json.loads(row["state_json"]) if row else {}
            reset = None
            if state.get("version") == STATE_VERSION:
                if state["config_hash"] != self.config_hash:
                    reset = "policy_changed"
                elif incoming["m5"][-1]["open_server_s"] < state["last_m5"]:
                    status = {**state["last_status"], "status": "out_of_order_feed"}
                    self._save(conn, state, status, now)
                    return status
                elif self._revised(state, incoming):
                    reset = "closed_history_revised"
                elif incoming["m5"][0]["open_server_s"] > state["last_m5"]:
                    reset = "missing_overlap"
            elif state:
                reset = "state_version_changed"
            if reset or not state.get("version"):
                if reset:
                    conn.execute("INSERT INTO alert_engine_resets(reset_at, reason, previous_state_json) VALUES(?,?,?)",
                                 (now, reset, row["state_json"]))
                    event(conn, f"reset:{time.time_ns()}", "engine_reset", "warning", "เริ่มคำนวณกลยุทธ์ใหม่",
                          {"policy_changed": "นโยบายหรือพารามิเตอร์กลยุทธ์เปลี่ยน",
                           "closed_history_revised": "แท่งราคาที่ปิดแล้วถูกแก้ไขใน MT5",
                           "missing_overlap": "ข้อมูลราคาขาดช่วงระหว่างที่บริการหยุด",
                           "state_version_changed": "รูปแบบ checkpoint เปลี่ยน"}[reset]
                          + " · ต้องอุ่นเครื่อง indicator ก่อนแจ้งเตือนอีกครั้ง")
                cores = {s.id: make_core(s) for s in self.policy.strategies}
                state = {"version": STATE_VERSION, "config_hash": self.config_hash, "last_m5": 0, "last_h1": 0,
                         "bootstrapped_at": now, "reset_reason": reset}
                live = False
            else:
                cores = {s.id: make_core(s, state["cores"][s.id]) for s in self.policy.strategies}
                live = True
            status = self._run(conn, cores, state, incoming, snapshot, now, live)
            state["cores"] = {key: core.dump() for key, core in cores.items()}
            state["history"], state["offset"] = incoming, snapshot.observed_utc_offset_s
            state["last_status"] = status
            self._save(conn, state, status, now)
        return status

    @staticmethod
    def _revised(state, incoming):
        for frame in ("m5", "h1"):
            previous = {b["open_server_s"]: b for b in state["history"][frame]}
            if any(b["open_server_s"] in previous and b != previous[b["open_server_s"]] for b in incoming[frame]):
                return True
        return False

    def _save(self, conn, state, status, now):
        conn.execute("""INSERT INTO alert_engine VALUES(1,?,?,?) ON CONFLICT(id) DO UPDATE SET
            state_json=excluded.state_json, status_json=excluded.status_json, updated_at=excluded.updated_at""",
                     (json.dumps(state, allow_nan=False), json.dumps(status, allow_nan=False), now))

    def warm(self, core) -> bool:
        return core.h1_count >= self.policy.warmup_h1_bars and core.m5_count >= self.policy.warmup_m5_bars

    def _run(self, conn, cores, state, incoming, snapshot, now, live):
        h1, offset = incoming["h1"], snapshot.observed_utc_offset_s
        h1_opens = {b["open_server_s"] for b in h1}
        latest_open = incoming["m5"][-1]["open_server_s"]
        selected, muted = set(self.selection(conn)), self.muted(conn)
        exits = state.setdefault("last_virtual_exit", {})
        if not isinstance(exits, dict) or "exit_reason" in exits:
            exits = state["last_virtual_exit"] = {}
        status_kind, last_alert, waiting, processed = None, None, False, 0
        for bar in incoming["m5"]:
            open_s = bar["open_server_s"]
            if open_s <= state["last_m5"]:
                continue
            close_s = open_s + 300
            if close_s % 3600 == 0 and close_s - 3600 not in h1_opens:
                waiting = True  # the H1 bar closing with this M5 bar is not in this snapshot yet
                break
            new_h1 = [b for b in h1 if b["open_server_s"] > state["last_h1"] and b["open_server_s"] + 3600 <= close_s]
            latest = open_s == latest_open
            close_utc_s = close_s - offset
            spreads = [round(bar["spread_points"] * snapshot.point, snapshot.digits)]
            if live and latest:
                spreads.append(round(snapshot.quote.ask - snapshot.quote.bid, snapshot.digits))
            bar_ctx = {"bar": bar, "close_s": close_s, "close_utc_s": close_utc_s, "spreads": spreads, "live": live,
                       "fresh": latest and 0 <= now - close_utc_s <= self.risk.max_signal_age_seconds,
                       "selected": selected, "muted": muted, "now": now, "snapshot": snapshot, "cores": cores}
            for spec in self.policy.strategies:
                core = cores[spec.id]
                signals, exit_ = core.step(bar, new_h1, snapshot.point, snapshot.digits)
                if exit_ is not None:
                    exits[spec.id] = exit_
                for signal in signals:
                    handler = {"entry": self._entry, "setup": self._setup}.get(signal["type"], self._update)
                    kind, row_id = handler(conn, spec, core, signal, bar_ctx)
                    if kind:
                        status_kind = kind if status_kind != "alert" else status_kind
                    if row_id:
                        last_alert = row_id
            if new_h1:
                state["last_h1"] = new_h1[-1]["open_server_s"]
            state["last_m5"] = open_s
            processed += 1
        warm = all(self.warm(c) for c in cores.values())
        strategies = {}
        for spec in self.policy.strategies:
            core = cores[spec.id]
            position = core.position
            if position is not None and position.get("time_stop_server_s") is not None:
                position = {**position, "time_stop_utc_s": position["time_stop_server_s"] - offset}
            strategies[spec.id] = {"label": spec.label, "strategy": spec.strategy, "version": spec.strategy_version,
                                   "kind": spec.kind, "enabled": spec.id in selected, "position": position,
                                   "indicators": core.indicators(), "summary": core.summary(),
                                   "last_virtual_exit": exits.get(spec.id)}
        primary = next((i for i in strategies if strategies[i]["enabled"]), next(iter(strategies)))
        status = {"status": ("bootstrapped" if not live else "waiting_for_h1" if waiting and not processed
                             else status_kind if status_kind == "alert" else "watching" if warm else "warming_up"),
                  "mode": "alert_only", "engine_version": "+".join(s.strategy_version for s in self.policy.strategies),
                  "strategy": primary, "symbol": "XM:XAUUSD", "source_symbol": snapshot.broker_symbol,
                  "last_m5_server_s": state["last_m5"],
                  "last_bar_close_utc_s": state["last_m5"] + 300 - offset if state["last_m5"] else None,
                  "new_bars": processed, "snapshot_utc_s": snapshot.captured_at_utc_s,
                  "warmup": {"h1": min(c.h1_count for c in cores.values()), "m5": min(c.m5_count for c in cores.values()),
                             "required_h1": self.policy.warmup_h1_bars, "required_m5": self.policy.warmup_m5_bars,
                             "ready": warm},
                  "position": strategies[primary]["position"], "indicators": strategies[primary]["indicators"],
                  "strategies": strategies, "selected": sorted(selected, key=list(strategies).index),
                  "conflict": self.policy.conflict}
        if last_alert:
            status["last_alert_id"] = last_alert
        elif not processed and state.get("last_status"):
            # A quote-only poll keeps the last bar evaluation.
            kept = {k: v for k, v in state["last_status"].items() if k in ("last_alert_id",)}
            status.update(kept)
            if status["status"] == "alert":
                status["status"] = "watching"
        return status

    # ------------------------------------------------------------------ signal handlers
    def _conflicts(self, spec, ctx, side=None):
        """Other selected strategies holding a hypothetical trade: [(label, side)]."""
        out = []
        for other in self.policy.strategies:
            pos = ctx["cores"][other.id].position
            if other.id != spec.id and other.id in ctx["selected"] and pos and (side is None or pos["side"] != side):
                out.append((other.label, pos["side"]))
        return out

    def _kind(self, core, reasons, ctx):
        return "rejected" if reasons else "warmup" if not self.warm(core) else "alert" if ctx["fresh"] else "missed"

    def _insert(self, conn, payload, kind, ctx):
        cursor = conn.execute("INSERT OR IGNORE INTO trade_alerts(alert_key,status,side,bar_close_utc_ms,created_at,payload_json) VALUES(?,?,?,?,?,?)",
                              (payload["alert_key"], kind, payload["side"], ctx["close_utc_s"] * 1000, ctx["now"],
                               json.dumps(payload, allow_nan=False)))
        return cursor.rowcount, (cursor.lastrowid if cursor.rowcount else None)

    def _entry(self, conn, spec, core, signal, ctx):
        bar, side = ctx["bar"], signal["side"]
        skip = self.policy.conflict == "skip" and self._conflicts(spec, ctx)
        entry, stop, target, reasons, values = gate(self.risk, side, bar["close"], signal["sl"], signal["tp"],
                                                    signal["atr"], ctx["spreads"])
        if skip:
            reasons.insert(0, "other_strategy_open")
        if not reasons:
            core.open(side, stop, target, ctx["close_s"])
        if not ctx["live"] or spec.id not in ctx["selected"]:
            return None, None
        kind = self._kind(core, reasons, ctx)
        payload = self._payload(spec, core, signal, side, entry, stop, target, reasons, values, ctx)
        payload["entry_limit"] = format(self._limit(side, entry, signal["atr"]), "f")
        inserted, row_id = self._insert(conn, payload, kind, ctx)
        if kind == "alert" and inserted and not ctx["muted"]:
            title, message = alert_text(payload, self._conflicts(spec, ctx, side))
            event(conn, "alert:" + payload["alert_key"], "trade_alert", "success", title, message, ctx["now"])
        return kind, row_id

    def _setup(self, conn, spec, core, signal, ctx):
        skip = self.policy.conflict == "skip" and self._conflicts(spec, ctx)
        sides, accepted = {}, {}
        for side, order in signal["orders"].items():
            entry, stop, target, reasons, values = gate(self.risk, side, order["level"], order["sl"], order["tp"],
                                                        signal["atr"], ctx["spreads"])
            if skip:
                reasons.insert(0, "other_strategy_open")
            sides[side] = (entry, stop, target, reasons, values)
            if not reasons:
                accepted[side] = {"level": entry, "sl": stop, "tp": target}
        show = ctx["live"] and spec.id in ctx["selected"]
        kind = self._kind(core, [] if accepted else ["rejected"], ctx) if show else None
        core.arm(accepted, alerted=kind == "alert")
        if not show:
            return None, None
        offset, rows, last_id = ctx["snapshot"].observed_utc_offset_s, [], None
        for side, (entry, stop, target, reasons, values) in sides.items():
            payload = self._payload(spec, core, signal, side, entry, stop, target, reasons, values, ctx)
            payload.update(order_type="buy_stop" if side == "BUY" else "sell_stop", entry_limit=None,
                           cancel_at_utc_s=signal["cancel_server_s"] - offset, time_stop_utc_s=signal["cancel_server_s"] - offset,
                           range_high=signal["range_high"], range_low=signal["range_low"],
                           range_width_pct=round(signal["width_pct"], 4), breakeven_r=core.p.breakeven_r)
            side_kind = "rejected" if reasons else kind
            inserted, row_id = self._insert(conn, payload, side_kind, ctx)
            last_id = row_id or last_id
            if side_kind == "alert" and inserted:
                rows.append(payload)
        if rows and not ctx["muted"]:
            title, message = setup_text(rows, self._conflicts(spec, ctx))
            event(conn, "alert:" + rows[0]["alert_key"], "trade_alert", "success", title, message, ctx["now"])
        return kind, last_id

    def _update(self, conn, spec, core, signal, ctx):
        """TPSL follow-ups (fill, breakeven, 21:00) for a setup that was notified."""
        kind = signal["type"]
        notify = (ctx["live"] and ctx["fresh"] and core.alerted and spec.id in ctx["selected"] and not ctx["muted"])
        if kind == "expire":
            core.alerted = False
        if not notify:
            return None, None
        d, symbol, label = ctx["snapshot"].digits, ctx["snapshot"].broker_symbol, spec.label
        offset = ctx["snapshot"].observed_utc_offset_s
        close_at = local_time(core.day * 86400 + core.p.close_h * 3600 - offset) if core.day is not None else "—"
        if kind == "fill":
            side, risk = signal["side"], abs(float(signal["level"]) - float(signal["sl"]))
            title = f"{label}: {side_name(side)} Stop ถูกชน · {symbol}"
            parts = [f"ยกเลิก {side_name(s)} Stop ที่ค้างอยู่" for s in signal["cancelled"]]
            parts.append(f"SL {price(signal['sl'], d)} · TP {price(signal['tp'], d)}")
            if core.p.breakeven_r:
                be = float(signal["level"]) + (1 if side == "BUY" else -1) * core.p.breakeven_r * risk
                parts.append(f"เลื่อน SL เป็นทุนเมื่อราคาถึง {price(round(be, d), d)} (+{core.p.breakeven_r:g}R)")
            parts.append(f"ปิดเวลา {close_at}")
            message = " · ".join(parts) + "\nตรวจจากแท่ง M5 ที่ปิดแล้ว ราคาที่คุณได้จริงอาจต่างไป"
        elif kind == "breakeven":
            title = f"{label}: เลื่อน SL เป็นทุน · {symbol}"
            message = (f"ไม้ {signal['side']} ได้ +{core.p.breakeven_r:g}R แล้ว · ย้าย SL ไปที่ {price(signal['sl'], d)}"
                       f" · TP {price(signal['tp'], d)} เท่าเดิม")
        else:
            if not (signal["had_orders"] or signal["had_position"]):
                return None, None
            title = f"{label}: ถึงเวลา {close_at} · {symbol}"
            message = " · ".join(([f"ปิดไม้ที่ยังเปิดอยู่"] if signal["had_position"] else [])
                                 + (["ยกเลิก Buy/Sell Stop ที่ยังค้าง"] if signal["had_orders"] else []))
        event(conn, f"update:{spec.id}:{kind}:{ctx['close_utc_s']}", "trade_update", "info", title, message, ctx["now"])
        return None, None

    def _limit(self, side, entry, atr):
        drift = Decimal(str(atr)) * self.policy.max_entry_drift_atr
        return tick_round(entry + drift if side == "BUY" else entry - drift, self.risk.price_tick)

    def _payload(self, spec, core, signal, side, entry, stop, target, reasons, values, ctx):
        snapshot, bar, close_utc_s = ctx["snapshot"], ctx["bar"], ctx["close_utc_s"]
        version, atr = spec.strategy_version, Decimal(str(signal["atr"]))
        bars = spec.time_stop_bars
        indicators = core.indicators()
        if signal.get("rsi2_previous") is not None:
            indicators["rsi2_previous"] = signal["rsi2_previous"]
        return {"alert_key": f"XM:XAUUSD:5:{close_utc_s * 1000}:{side}:{version}", "side": side,
                "strategy_id": spec.id, "strategy_label": spec.label,
                "strategy": spec.strategy, "strategy_version": version, "config_sha256": self.config_hash,
                "order_type": "market",
                "source_server": snapshot.server, "source_symbol": snapshot.broker_symbol, "digits": snapshot.digits,
                "bar_open_server_s": bar["open_server_s"], "bar_close_utc_s": close_utc_s,
                "entry": format(entry, "f"), "sl": format(stop, "f"), "tp": format(target, "f"),
                "atr": float(atr),
                "net_reward_risk": format(values["net_reward_risk"].quantize(Decimal("0.0001")), "f"),
                "stop_atr": format(values["stop_atr"].quantize(Decimal("0.0001")), "f"),
                "spread_atr": format(values["spread_atr"].quantize(Decimal("0.0001")), "f"),
                "spread_price_used": format(values["spread_price_used"], "f"),
                "reason_codes": reasons,
                "valid_until_utc_s": close_utc_s + self.risk.max_signal_age_seconds,
                "time_stop_utc_s": None if bars is None else close_utc_s + bars * 300,
                "quote": {"bid": snapshot.quote.bid, "ask": snapshot.quote.ask, "time_server_ms": snapshot.quote.time_server_ms},
                "indicators": indicators,
                "bar": {k: bar[k] for k in ("open", "high", "low", "close", "spread_points")},
                "snapshot_sha256": hashlib.sha256(snapshot.model_dump_json().encode()).hexdigest(),
                "not_checked": ["fill_price", "fill_time_spread", "broker_contract_specifications", "account_equity"]}


def ensure_tables(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS alert_engine (
        id INTEGER PRIMARY KEY CHECK(id=1), state_json TEXT NOT NULL,
        status_json TEXT NOT NULL, updated_at REAL NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS alert_engine_resets (
        id INTEGER PRIMARY KEY, reset_at REAL NOT NULL, reason TEXT NOT NULL, previous_state_json TEXT NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS trade_alerts (
        id INTEGER PRIMARY KEY, alert_key TEXT UNIQUE NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('alert','missed','warmup','rejected')),
        side TEXT NOT NULL CHECK(side IN ('BUY','SELL')), bar_close_utc_ms INTEGER NOT NULL,
        created_at REAL NOT NULL, payload_json TEXT NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS control_events (
        id INTEGER PRIMARY KEY,event_key TEXT UNIQUE NOT NULL,kind TEXT NOT NULL,
        level TEXT NOT NULL,title TEXT NOT NULL,message TEXT NOT NULL,at REAL NOT NULL)""")
    conn.execute("CREATE TABLE IF NOT EXISTS control_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)")


def event(conn, key, kind, level, title, message, at=None):
    conn.execute("INSERT OR IGNORE INTO control_events(event_key,kind,level,title,message,at) VALUES(?,?,?,?,?,?)",
                 (key, kind, level, title, message, time.time() if at is None else at))
