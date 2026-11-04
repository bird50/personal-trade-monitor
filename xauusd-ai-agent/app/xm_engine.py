"""Persistent, causal XM candidate stream. No fills, quantities or execution."""
import hashlib
import json
import time
from decimal import Decimal, ROUND_HALF_UP

from .database import Database
from .models import MarketStructure, Signal
from .mt5_bridge import Snapshot

VERSION = "xm-0.1"


def smooth(state: dict, key: str, value: float | None, length: int, ema=False):
    item = state.setdefault(key, {"count": 0, "sum": 0.0, "value": None})
    if value is None:
        return item["value"]
    item["count"] += 1
    if item["value"] is None:
        item["sum"] += value
        if ema:
            item["value"] = value
        elif item["count"] >= length:
            item["value"] = item["sum"] / length
    else:
        alpha = 2 / (length + 1) if ema else 1 / length
        item["value"] += alpha * (value - item["value"])
    return item["value"]


def update_m5(state: dict, bar: dict):
    previous = state.get("previous_bar")
    previous_ema = state.get("ema20", {}).get("value")
    close = bar["close"]
    ema = smooth(state, "ema20", close, 20, ema=True)
    tr = bar["high"] - bar["low"]
    plus = minus = change = None
    if previous:
        tr = max(tr, abs(bar["high"] - previous["close"]), abs(bar["low"] - previous["close"]))
        change = close - previous["close"]
        up, down = bar["high"] - previous["high"], previous["low"] - bar["low"]
        plus = up if up > down and up > 0 else 0.0
        minus = down if down > up and down > 0 else 0.0
    atr = smooth(state, "atr14", tr, 14)
    gain = smooth(state, "gain14", max(change, 0) if change is not None else None, 14)
    loss = smooth(state, "loss14", max(-change, 0) if change is not None else None, 14)
    rsi = None if gain is None or loss is None else 50.0 if gain == loss == 0 else 100.0 if loss == 0 else 100 - 100 / (1 + gain / loss)
    dtr = smooth(state, "dmi_tr14", tr if previous else None, 14)
    dp, dm = smooth(state, "plus14", plus, 14), smooth(state, "minus14", minus, 14)
    pdi = None if dp is None else 100 * dp / dtr if dtr else 0.0
    mdi = None if dm is None else 100 * dm / dtr if dtr else 0.0
    dx = None if pdi is None or mdi is None else 100 * abs(pdi - mdi) / (pdi + mdi) if pdi + mdi else 0.0
    adx = smooth(state, "adx14", dx, 14)
    cross_up = bool(previous and previous_ema is not None and previous["close"] <= previous_ema and close > ema)
    cross_down = bool(previous and previous_ema is not None and previous["close"] >= previous_ema and close < ema)
    state["previous_bar"] = bar
    return {"ema": ema, "rsi": rsi, "adx": adx, "atr": atr,
            "plus_di": pdi, "minus_di": mdi, "cross_up": cross_up, "cross_down": cross_down}


def choose_side(metrics: dict, h1: dict | None):
    if h1 is None or any(metrics[k] is None for k in ("rsi", "adx", "atr", "plus_di", "minus_di")):
        return None
    if metrics["atr"] <= 0 or metrics["adx"] < 20:
        return None
    if h1["close"] > h1["ema_fast"] > h1["ema_slow"] and metrics["cross_up"] and 50 <= metrics["rsi"] < 70 and metrics["plus_di"] > metrics["minus_di"]:
        return "BUY"
    if h1["close"] < h1["ema_fast"] < h1["ema_slow"] and metrics["cross_down"] and 30 < metrics["rsi"] <= 50 and metrics["minus_di"] > metrics["plus_di"]:
        return "SELL"
    return None


def update_pivots(state: dict, bar: dict, index: int):
    window = state.setdefault("pivot_window", [])
    window.append(bar)
    del window[:-7]
    points = state.setdefault("pivots", {k: None for k in ("recent_high", "previous_high", "recent_low", "previous_low")})
    if len(window) == 7:
        pivot = window[3]
        others = window[:3] + window[4:]
        # Strict ties are intentionally conservative in this independent XM version.
        for kind, field, test in (("high", "high", lambda a, b: a > b), ("low", "low", lambda a, b: a < b)):
            if all(test(pivot[field], other[field]) for other in others):
                points["previous_" + kind] = points["recent_" + kind]
                points["recent_" + kind] = {"price": pivot[field],
                    "pivot_close_ms": (pivot["open_server_s"] + 300) * 1000,
                    "confirmed_at_ms": (bar["open_server_s"] + 300) * 1000,
                    "confirmed_index": index, "active": True}
    for key, broken in (("recent_high", lambda p: bar["close"] > p), ("recent_low", lambda p: bar["close"] < p)):
        if points[key] and broken(points[key]["price"]):
            points[key]["active"] = False
    result = {"timeframe": "5", "method": "confirmed_pivots", "left_bars": 3,
              "right_bars": 3, "max_age_bars": 120, "state": "insufficient"}
    for key, point in points.items():
        result[key] = None if point is None else {k: v for k, v in point.items() if k != "confirmed_index"}
        if point is not None:
            result[key]["age_bars"] = index - point["confirmed_index"]
    model = MarketStructure.model_validate(result | {"state": derived_structure(result)})
    return model.model_dump()


def derived_structure(points):
    if any(points[k] is None or points[k]["age_bars"] > 120 for k in ("recent_high", "previous_high", "recent_low", "previous_low")):
        return "insufficient"
    highs = points["recent_high"]["price"] - points["previous_high"]["price"]
    lows = points["recent_low"]["price"] - points["previous_low"]["price"]
    return "bullish" if highs > 0 and lows > 0 else "bearish" if highs < 0 and lows < 0 else "ranging"


def tick_round(value, tick):
    return (Decimal(str(value)) / tick).to_integral_value(rounding=ROUND_HALF_UP) * tick


def levels(side, close, atr, tick):
    entry = tick_round(close, tick)
    distance = max(tick, tick_round(Decimal(str(atr)) * Decimal("1.5"), tick))
    direction = 1 if side == "BUY" else -1
    return float(entry), float(entry - direction * distance), float(entry + direction * distance * 2)


class XMEngine:
    def __init__(self, database: Database, server="XMGlobal-MT5 7", symbol="GOLD", price_tick=Decimal("0.01")):
        self.db, self.server, self.symbol, self.tick = database, server, symbol, price_tick
        with self.db.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS xm_engine (
                id INTEGER PRIMARY KEY CHECK(id=1), state_json TEXT NOT NULL,
                status_json TEXT NOT NULL, updated_at REAL NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS xm_candidate_inputs (
                signal_record_id INTEGER PRIMARY KEY REFERENCES signals(id),
                snapshot_json TEXT NOT NULL, state_before_snapshot_json TEXT NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS xm_bar_evaluations (
                open_server_s INTEGER PRIMARY KEY, evaluation_json TEXT NOT NULL)""")

    def status(self):
        with self.db.connect() as db:
            row = db.execute("SELECT status_json, updated_at FROM xm_engine WHERE id=1").fetchone()
        return {"status": "waiting_for_feed"} if row is None else {**json.loads(row["status_json"]), "updated_at": row["updated_at"]}

    def note(self, status):
        with self.db.connect() as db:
            row = db.execute("SELECT state_json FROM xm_engine WHERE id=1").fetchone()
            db.execute("INSERT INTO xm_engine VALUES(1, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET status_json=excluded.status_json, updated_at=excluded.updated_at",
                       (row["state_json"] if row else "{}", json.dumps({"status": status}), time.time()))

    def process(self, snapshot: Snapshot, now=None):
        now = time.time() if now is None else now
        if snapshot.server != self.server or snapshot.broker_symbol != self.symbol:
            self.note("source_mismatch")
            return self.status()
        if not -30 <= now - snapshot.captured_at_utc_s <= 120:
            self.note("feed_stale")
            return self.status()
        if Decimal(str(snapshot.tick_size)) != self.tick:
            self.note("tick_policy_mismatch")
            return self.status()
        if len(snapshot.m5) < 200 or len(snapshot.h1) < 200:
            self.note("insufficient_history")
            return self.status()
        incoming = {"m5": [b.model_dump() for b in snapshot.m5], "h1": [b.model_dump() for b in snapshot.h1]}
        with self.db.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT state_json FROM xm_engine WHERE id=1").fetchone()
            state = json.loads(row["state_json"]) if row else {}
            state_before = json.dumps(state, allow_nan=False)
            initialized = bool(state.get("version"))
            error = None
            if initialized:
                if state["version"] != VERSION or state["offset"] != snapshot.observed_utc_offset_s:
                    error = "version_or_offset_changed_reset_required"
                for frame in ("m5", "h1"):
                    previous = {b["open_server_s"]: b for b in state["history"][frame]}
                    if any(b["open_server_s"] in previous and b != previous[b["open_server_s"]] for b in incoming[frame]):
                        error = "closed_history_revised_reset_required"
                last = state["last_m5"]
                if incoming["m5"][-1]["open_server_s"] < last:
                    error = "out_of_order_feed"
                elif incoming["m5"][0]["open_server_s"] > last:
                    error = "missing_overlap_reset_required"
            if error:
                status = {"status": error, "last_m5_server_s": state.get("last_m5")}
            else:
                if not initialized:
                    state = {"version": VERSION, "offset": snapshot.observed_utc_offset_s,
                             "last_m5": 0, "last_h1": 0, "index": -1, "m5_indicators": {},
                             "h1_indicators": {}, "h1": None, "recent_range": [], "candidate_lock": None}
                status = {"status": "no_new_bar", "engine_version": VERSION, "symbol": "XM:XAUUSD",
                          "source_symbol": self.symbol, "last_m5_server_s": state["last_m5"]}
                for bar in incoming["m5"]:
                    if bar["open_server_s"] <= state["last_m5"]:
                        continue
                    # Pine's H1 [1] is the H1 closed at/before the M5 OPEN, not its close.
                    for hbar in incoming["h1"]:
                        if hbar["open_server_s"] > state["last_h1"] and hbar["open_server_s"] + 3600 <= bar["open_server_s"]:
                            fast = smooth(state["h1_indicators"], "fast", hbar["close"], 50, ema=True)
                            slow = smooth(state["h1_indicators"], "slow", hbar["close"], 200, ema=True)
                            state["last_h1"] = hbar["open_server_s"]
                            if state["h1_indicators"]["slow"]["count"] >= 200:
                                state["h1"] = {"confirmed": True, "bar_close_ms": (hbar["open_server_s"] + 3600) * 1000,
                                               "close": hbar["close"], "ema_fast": fast, "ema_slow": slow}
                    state["index"] += 1
                    metrics = update_m5(state["m5_indicators"], bar)
                    structure = update_pivots(state, bar, state["index"])
                    lock = state["candidate_lock"]
                    if lock and (bar["low"] <= lock["sl"] if lock["side"] == "BUY" else bar["high"] >= lock["sl"]):
                        state["candidate_lock"] = None
                    if lock and (bar["high"] >= lock["tp"] if lock["side"] == "BUY" else bar["low"] <= lock["tp"]):
                        state["candidate_lock"] = None
                    h1_fresh = state["h1"] is not None and 0 <= bar["open_server_s"] - state["h1"]["bar_close_ms"] / 1000 < 3600
                    side = choose_side(metrics, state["h1"]) if h1_fresh else None
                    prior = state["recent_range"]
                    candidate = side if state["candidate_lock"] is None and len(prior) == 20 else None
                    status = {"status": "evaluated", "engine_version": VERSION, "symbol": "XM:XAUUSD",
                              "source_symbol": self.symbol, "last_m5_server_s": bar["open_server_s"],
                              "metrics": metrics, "h1": state["h1"], "structure_state": structure["state"],
                              "h1_ready": h1_fresh, "context_time_basis": "broker_server_wall_clock",
                              "candidate_side": candidate, "candidate_locked": state["candidate_lock"] is not None,
                              "signal_record_id": None}
                    if candidate:
                        entry, sl, tp = levels(candidate, bar["close"], metrics["atr"], self.tick)
                        state["candidate_lock"] = {"side": candidate, "sl": sl, "tp": tp}
                        close_ms = (bar["open_server_s"] + 300 - snapshot.observed_utc_offset_s) * 1000
                        if initialized and bar is incoming["m5"][-1] and 0 <= now - close_ms / 1000 <= 300:
                            signal = self.make_signal(snapshot, bar, metrics, structure, state["h1"], prior, candidate, entry, sl, tp, close_ms)
                            record_id, _ = self.db.insert_in_transaction(db, signal)
                            db.execute("INSERT OR IGNORE INTO xm_candidate_inputs VALUES (?, ?, ?)",
                                       (record_id, snapshot.model_dump_json(), state_before))
                            status.update(status="signal_queued", signal_record_id=record_id)
                    if initialized:
                        db.execute("INSERT INTO xm_bar_evaluations VALUES (?, ?)",
                                   (bar["open_server_s"], json.dumps(status, allow_nan=False)))
                    prior.append(bar)
                    del prior[:-20]
                    state["last_m5"] = bar["open_server_s"]
                    state["last_evaluation"] = status
                state["history"] = incoming
                if not initialized:
                    status["status"] = "bootstrapped_no_historical_signals"
                elif status["status"] == "no_new_bar" and state.get("last_evaluation"):
                    # Preserve the last actual bar evaluation across quote-only polls.
                    status = state["last_evaluation"]
                status["snapshot_utc_s"] = snapshot.captured_at_utc_s
                status["last_m5_server_s"] = state["last_m5"]
                state["last_evaluation"] = status
                db.execute("DELETE FROM xm_bar_evaluations WHERE open_server_s NOT IN (SELECT open_server_s FROM xm_bar_evaluations ORDER BY open_server_s DESC LIMIT 5000)")
            db.execute("INSERT INTO xm_engine VALUES(1, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET state_json=excluded.state_json, status_json=excluded.status_json, updated_at=excluded.updated_at",
                       (json.dumps(state, allow_nan=False), json.dumps(status, allow_nan=False), now))
        return status

    @staticmethod
    def make_signal(snapshot, bar, metrics, structure, h1, prior, side, entry, sl, tp, close_ms):
        raw = snapshot.model_dump_json()
        return Signal.model_validate({
            "schema_version": 3, "strategy_version": VERSION, "event": "signal",
            "signal_id": f"XM:XAUUSD:5:{close_ms}:{side}:{VERSION}", "symbol": "XM:XAUUSD",
            "timeframe": "5", "bar_close_ms": close_ms, "candle_confirmed": True, "side": side,
            "entry_basis": "signal_close", "entry": entry, "sl": sl, "tp": tp,
            "risk_reward": abs(tp - entry) / abs(entry - sl),
            "h1": {**h1, "trend": "bullish" if side == "BUY" else "bearish"},
            "m5": {k: bar[k] for k in ("open", "high", "low", "close")} | {k: metrics[k] for k in ("ema", "rsi", "adx", "atr", "plus_di", "minus_di")},
            "context": {"prior_range_low": min(b["low"] for b in prior),
                        "prior_range_high": max(b["high"] for b in prior), "range_bars": 20,
                        "market_structure": structure},
            "source": {"provider": "xm_mt5_demo", "server": snapshot.server,
                "broker_symbol": snapshot.broker_symbol, "engine_version": VERSION,
                "context_time_basis": "broker_server_wall_clock", "bar_close_server_ms": (bar["open_server_s"] + 300) * 1000,
                "snapshot_utc_s": snapshot.captured_at_utc_s, "observed_utc_offset_s": snapshot.observed_utc_offset_s,
                "quote_server_ms": snapshot.quote.time_server_ms,
                "bid": snapshot.quote.bid, "ask": snapshot.quote.ask, "price_tick": snapshot.tick_size,
                "input_sha256": hashlib.sha256(raw.encode()).hexdigest()}})
