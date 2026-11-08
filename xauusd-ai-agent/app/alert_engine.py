"""Alert-only XM engine: closed MT5 bars -> RSI(2) strategy -> Risk level checks -> alerts.

No AI, no Paper account and no orders. The strategy object is the same class the backtest
and walk-forward studies used (``backtest/strategies/rsi2_trend_pullback_r2.py``) and is fed
bars with the backtest's visibility rules, so a live alert corresponds to a backtest signal:

* a strategy sees a closed M5 bar at its close; an H1 bar is visible once its close <= that time;
* after a signal that passes the Risk level checks, a hypothetical trade blocks new signals
  until its SL/TP is touched on a later M5 bar or its time stop elapses (as the backtest's
  one-position rule does). It is never sized, filled or booked.

State is a JSON checkpoint in its own database, separate from the Paper account.
"""
import hashlib
import json
import time
from collections import Counter
from datetime import datetime
from decimal import Decimal, localcontext
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backtest import indicators
from backtest.engine import Context
from backtest.strategies import load as load_strategy

from .config import ROOT
from .mt5_bridge import Snapshot
from .risk_manager import RiskPolicy, level_metrics
from .xm_engine import tick_round

STATE_VERSION = 1
SERIALIZABLE = {cls.__name__: cls for cls in (indicators.EMA, indicators.RMA, indicators.RSI, indicators.ATR)}
DIAGNOSTIC_FIELDS = ("params", "cost_filtered_times")


class AlertPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: Literal["xm-alert-v1"]
    mode: Literal["alert_only"]
    strategy: Literal["rsi2_trend_pullback_r2"]
    strategy_version: Annotated[str, Field(pattern=r"^[a-z0-9.-]{1,40}$")]
    params: dict
    risk_policy: Annotated[str, Field(min_length=1, max_length=200)]
    max_entry_drift_atr: Annotated[Decimal, Field(gt=0, le=5, allow_inf_nan=False)]
    warmup_h1_bars: Annotated[int, Field(ge=200, le=5000, strict=True)]
    warmup_m5_bars: Annotated[int, Field(ge=200, le=5000, strict=True)]

    @model_validator(mode="after")
    def supported_exits(self):
        params = load_strategy(self.strategy)(self.params).params  # rejects unknown parameters
        exits = params.get("exits") or {}
        if any(exits.get(key) is not None for key in ("breakeven_r", "breakeven_lock_r", "trail_start_r", "trail_r")):
            raise ValueError("Alert mode tracks fixed SL/TP only; breakeven and trailing exits are not supported")
        if not isinstance(exits.get("time_stop_bars"), int) or not 1 <= exits["time_stop_bars"] <= 2016:
            raise ValueError("Alert mode requires an integer time_stop_bars")
        return self

    @classmethod
    def load(cls, path: Path):
        return cls.model_validate_json(Path(path).read_text())

    def risk_path(self) -> Path:
        path = Path(self.risk_policy)
        return path if path.is_absolute() else ROOT / path

    @property
    def effective_params(self) -> dict:
        return load_strategy(self.strategy)(self.params).params

    @property
    def time_stop_bars(self) -> int:
        return self.effective_params["exits"]["time_stop_bars"]


def dump_value(value):
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Counter):
        return {"counter": dict(value)}
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
    if "dict" in value:
        return {key: load_value(item) for key, item in value["dict"].items()}
    if "list" in value:
        return [load_value(item) for item in value["list"]]
    cls = SERIALIZABLE[value["indicator"]]
    obj = cls.__new__(cls)
    obj.__dict__.update({key: load_value(item) for key, item in value["fields"].items()})
    return obj


class AlertCore:
    """Strategy plus the hypothetical one-trade blocker, stepped once per closed M5 bar."""

    def __init__(self, policy: AlertPolicy, state: dict | None = None):
        self.policy = policy
        self.strategy = load_strategy(policy.strategy)(policy.params)
        self.position, self.m5_count, self.h1_count = None, 0, 0
        if state:
            for key, value in state["strategy"].items():
                setattr(self.strategy, key, load_value(value))
            self.position, self.m5_count, self.h1_count = state["position"], state["m5_count"], state["h1_count"]

    def dump(self) -> dict:
        fields = {key: dump_value(value) for key, value in vars(self.strategy).items() if key not in DIAGNOSTIC_FIELDS}
        return {"strategy": fields, "position": self.position, "m5_count": self.m5_count, "h1_count": self.h1_count}

    @property
    def warm(self) -> bool:
        return self.h1_count >= self.policy.warmup_h1_bars and self.m5_count >= self.policy.warmup_m5_bars

    def manage(self, bar, point, digits):
        """Close the hypothetical trade on time stop (at bar open) or a touched SL/TP."""
        pos = self.position
        if pos is None:
            return None
        if bar["open_server_s"] >= pos["time_stop_server_s"]:
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
        exit_ = self.manage(bar, point, digits)
        ctx = Context(bar["open_server_s"] + 300, bar, self.m5_count, [], {"H1": [], "H4": [], "D1": []},
                      {"M5": [bar], "H1": list(new_h1)}, None if self.position is None else dict(self.position),
                      0.0, point, digits, float(point))
        trigger_rsi = self.strategy.prev_rsi  # the confirm rule fires on the previous bar's RSI(2)
        signal = self.strategy.on_bar(ctx)
        if signal:
            signal = {**signal, "rsi2_previous": trigger_rsi}
        self.m5_count += 1
        self.h1_count += len(new_h1)
        return signal, exit_

    def open(self, side, sl, tp, signal_close_server_s):
        self.position = {"side": side, "sl": format(sl, "f"), "tp": format(tp, "f"),
                         "signal_close_server_s": signal_close_server_s,
                         "time_stop_server_s": signal_close_server_s + self.policy.time_stop_bars * 300}

    def indicators(self) -> dict:
        s = self.strategy
        fast, slow, close = s.h1_fast.value, s.h1_slow.value, s.h1_close
        bias = None
        if None not in (fast, slow, close, s.ema200.value) and s.prev is not None:
            m5_close = s.prev["close"]
            if close > slow and fast > slow and m5_close > s.ema200.value:
                bias = "BUY"
            elif close < slow and fast < slow and m5_close < s.ema200.value:
                bias = "SELL"
        return {"rsi2": s.rsi.value, "m5_ema200": s.ema200.value, "atr14": s.atr.value,
                "h1_close": close, "h1_ema50": fast, "h1_ema200": slow, "bias": bias,
                "armed": dict(s.armed), "m5_close": None if s.prev is None else s.prev["close"]}


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


def alert_text(payload) -> tuple[str, str]:
    d, side = payload["digits"], payload["side"]
    limit = ("Ask ≤ " if side == "BUY" else "Bid ≥ ") + price(payload["entry_limit"], d)
    title = f"เข้า {side} ได้ · {payload['source_symbol']}"
    message = (f"ราคาอ้างอิง {price(payload['entry'], d)} · SL {price(payload['sl'], d)} · TP {price(payload['tp'], d)}"
               f" · RR สุทธิ {Decimal(payload['net_reward_risk']):.2f}\n"
               f"เข้าภายใน {local_time(payload['valid_until_utc_s'])} เมื่อ {limit}"
               f" · ถ้ายังไม่ถึง SL/TP ให้ปิดเวลา {local_time(payload['time_stop_utc_s'])}")
    return title, message


class AlertEngine:
    def __init__(self, db, policy: AlertPolicy, risk: RiskPolicy):
        self.db, self.policy, self.risk = db, policy, risk
        if risk.source_server is None or risk.source_broker_symbol is None:
            raise ValueError("Alert mode requires a Risk policy pinned to an MT5 server and symbol")
        material = {"alert": policy.model_dump(mode="json"), "params": policy.effective_params,
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
            current = {**{k: v for k, v in previous.items() if k in ("warmup", "position", "indicators", "last_m5_server_s")},
                       "status": status}
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
                core = AlertCore(self.policy)
                state = {"version": STATE_VERSION, "config_hash": self.config_hash, "last_m5": 0, "last_h1": 0,
                         "bootstrapped_at": now, "reset_reason": reset}
                live = False
            else:
                core = AlertCore(self.policy, state["core"])
                live = True
            status = self._run(conn, core, state, incoming, snapshot, now, live)
            state["core"], state["history"], state["offset"] = core.dump(), incoming, snapshot.observed_utc_offset_s
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

    def _run(self, conn, core, state, incoming, snapshot, now, live):
        h1, offset = incoming["h1"], snapshot.observed_utc_offset_s
        h1_opens = {b["open_server_s"] for b in h1}
        latest_open = incoming["m5"][-1]["open_server_s"]
        status_kind, last_alert, waiting = None, None, False
        processed = 0
        for bar in incoming["m5"]:
            open_s = bar["open_server_s"]
            if open_s <= state["last_m5"]:
                continue
            close_s = open_s + 300
            if close_s % 3600 == 0 and close_s - 3600 not in h1_opens:
                waiting = True  # the H1 bar closing with this M5 bar is not in this snapshot yet
                break
            new_h1 = [b for b in h1 if b["open_server_s"] > state["last_h1"] and b["open_server_s"] + 3600 <= close_s]
            signal, exit_ = core.step(bar, new_h1, snapshot.point, snapshot.digits)
            if new_h1:
                state["last_h1"] = new_h1[-1]["open_server_s"]
            state["last_m5"] = open_s
            processed += 1
            if exit_ is not None:
                state["last_virtual_exit"] = exit_
            if not signal:
                continue
            latest = open_s == latest_open
            spreads = [round(bar["spread_points"] * snapshot.point, snapshot.digits)]
            if live and latest:
                spreads.append(round(snapshot.quote.ask - snapshot.quote.bid, snapshot.digits))
            entry, stop, target, reasons, values = gate(self.risk, signal["side"], bar["close"], signal["sl"],
                                                        signal["tp"], signal["atr"], spreads)
            if not reasons:
                core.open(signal["side"], stop, target, close_s)
            if not live:
                continue
            close_utc_s = close_s - offset
            fresh = latest and 0 <= now - close_utc_s <= self.risk.max_signal_age_seconds
            kind = "rejected" if reasons else "warmup" if not core.warm else "alert" if fresh else "missed"
            payload = self._payload(signal, bar, entry, stop, target, reasons, values, snapshot, close_s, close_utc_s, core)
            cursor = conn.execute("INSERT OR IGNORE INTO trade_alerts(alert_key,status,side,bar_close_utc_ms,created_at,payload_json) VALUES(?,?,?,?,?,?)",
                                  (payload["alert_key"], kind, signal["side"], close_utc_s * 1000, now, json.dumps(payload, allow_nan=False)))
            if kind == "alert" and cursor.rowcount and not self.muted(conn):
                title, message = alert_text(payload)
                event(conn, "alert:" + payload["alert_key"], "trade_alert", "success", title, message, now)
            status_kind = kind
            last_alert = cursor.lastrowid if cursor.rowcount else None
        status = {"status": ("bootstrapped" if not live else "waiting_for_h1" if waiting and not processed
                             else status_kind if status_kind == "alert" else "watching" if core.warm else "warming_up"),
                  "mode": "alert_only", "engine_version": self.policy.strategy_version, "strategy": self.policy.strategy,
                  "symbol": "XM:XAUUSD", "source_symbol": snapshot.broker_symbol,
                  "last_m5_server_s": state["last_m5"],
                  "last_bar_close_utc_s": state["last_m5"] + 300 - offset if state["last_m5"] else None,
                  "new_bars": processed, "snapshot_utc_s": snapshot.captured_at_utc_s,
                  "warmup": {"h1": core.h1_count, "m5": core.m5_count, "required_h1": self.policy.warmup_h1_bars,
                             "required_m5": self.policy.warmup_m5_bars, "ready": core.warm},
                  "position": None if core.position is None else {
                      **core.position, "time_stop_utc_s": core.position["time_stop_server_s"] - offset},
                  "last_virtual_exit": state.get("last_virtual_exit"),
                  "indicators": core.indicators()}
        if last_alert:
            status["last_alert_id"] = last_alert
        elif not processed and state.get("last_status"):
            # A quote-only poll keeps the last bar evaluation.
            kept = {k: v for k, v in state["last_status"].items() if k in ("last_alert_id",)}
            status.update(kept)
            if status["status"] == "alert":
                status["status"] = "watching"
        return status

    def _payload(self, signal, bar, entry, stop, target, reasons, values, snapshot, close_s, close_utc_s, core):
        side, atr = signal["side"], Decimal(str(signal["atr"]))
        drift = atr * self.policy.max_entry_drift_atr
        limit = tick_round(entry + drift if side == "BUY" else entry - drift, self.risk.price_tick)
        version = self.policy.strategy_version
        return {"alert_key": f"XM:XAUUSD:5:{close_utc_s * 1000}:{side}:{version}", "side": side,
                "strategy": self.policy.strategy, "strategy_version": version, "config_sha256": self.config_hash,
                "source_server": snapshot.server, "source_symbol": snapshot.broker_symbol, "digits": snapshot.digits,
                "bar_open_server_s": bar["open_server_s"], "bar_close_utc_s": close_utc_s,
                "entry": format(entry, "f"), "sl": format(stop, "f"), "tp": format(target, "f"),
                "entry_limit": format(limit, "f"), "atr": float(atr),
                "net_reward_risk": format(values["net_reward_risk"].quantize(Decimal("0.0001")), "f"),
                "stop_atr": format(values["stop_atr"].quantize(Decimal("0.0001")), "f"),
                "spread_atr": format(values["spread_atr"].quantize(Decimal("0.0001")), "f"),
                "spread_price_used": format(values["spread_price_used"], "f"),
                "reason_codes": reasons,
                "valid_until_utc_s": close_utc_s + self.risk.max_signal_age_seconds,
                "time_stop_utc_s": close_utc_s + self.policy.time_stop_bars * 300,
                "quote": {"bid": snapshot.quote.bid, "ask": snapshot.quote.ask, "time_server_ms": snapshot.quote.time_server_ms},
                "indicators": {**core.indicators(), "rsi2_previous": signal.get("rsi2_previous")},
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
