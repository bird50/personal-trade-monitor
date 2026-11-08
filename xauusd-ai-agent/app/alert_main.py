"""Alert-only XM service: MT5 snapshots -> RSI(2) strategy -> Risk level checks -> Mac alerts.

No AI worker, no Paper account and no order execution. Runs on loopback port 8002 in place
of the Paper service (``app.xm_main``), with its own database.
"""
import asyncio
import logging
import os
import sqlite3
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path

import httpx
from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from .alert_engine import AlertEngine, AlertPolicy, event
from .config import ROOT, Settings
from .database import Database
from .models import StrictModel
from .mt5_bridge import BridgeSettings, MarketStore, Snapshot
from .risk_manager import RiskPolicy
from .webhook import authenticate, bearer

ALERT_POLICY = ROOT / "config/alert-xm.json"


class MuteRequest(StrictModel):
    paused: bool


def alert_database_path() -> Path:
    path = Path(os.getenv("XM_ALERT_DATABASE_PATH", "data/xm-alerts.db"))
    return path if path.is_absolute() else ROOT / path


class AlertMonitor:
    """Receiver/feed health with debounced durable events, and the dashboard snapshot."""

    def __init__(self, app, feed_config, receiver_url="http://127.0.0.1:8001/health"):
        self.app, self.feed_config, self.receiver_url = app, feed_config, receiver_url
        self.market = MarketStore(feed_config.database_path)
        self.receiver = {"available": False, "checked_at": None}
        self.bad_counts, self.incidents = {}, set()

    @property
    def engine(self) -> AlertEngine:
        return self.app.state.alert_engine

    def record_health(self, name, healthy, label):
        self.bad_counts[name] = 0 if healthy else self.bad_counts.get(name, 0) + 1
        if not healthy and self.bad_counts[name] >= 3 and name not in self.incidents:
            self.incidents.add(name)
            with self.engine.db.connect() as conn:
                event(conn, f"down:{name}:{time.time_ns()}", "health_down", "warning", label + " ขัดข้อง",
                      "ตรวจพบต่อเนื่อง 3 รอบ ระหว่างนี้จะไม่มีการแจ้งเตือนสัญญาณ")
        elif healthy and name in self.incidents:
            self.incidents.remove(name)
            with self.engine.db.connect() as conn:
                event(conn, f"up:{name}:{time.time_ns()}", "health_recovered", "success", label + " กลับมาทำงานแล้ว",
                      "รับข้อมูลได้อีกครั้ง")

    def feed(self):
        try:
            market = self.market.latest(self.engine.risk.source_broker_symbol)
        except Exception:
            market = None
        feed = {"available": market is not None and market["status"] == "fresh", "age_seconds": None,
                "bid": None, "ask": None}
        if market:
            p = market["payload"]
            feed.update(age_seconds=market["age_seconds"], bid=p["quote"]["bid"], ask=p["quote"]["ask"],
                        captured_at=p["captured_at_utc_s"], m5_bars=len(p["m5"]), h1_bars=len(p["h1"]),
                        symbol=p["broker_symbol"], digits=p["digits"])
        return feed

    def snapshot(self, after_event_id=0):
        now = time.time()
        engine, policy, risk = self.engine, self.engine.policy, self.engine.risk
        feed, status = self.feed(), engine.status()
        workers = {"engine": not self.app.state.alert_task.done(), "monitor": not self.app.state.monitor_task.done()}
        with engine.db.connect() as conn:
            events = [dict(x) for x in conn.execute("SELECT * FROM control_events WHERE id>? ORDER BY id LIMIT 100", (after_event_id,))]
            recent = [dict(x) for x in conn.execute("SELECT * FROM control_events ORDER BY id DESC LIMIT 30")]
            latest = conn.execute("SELECT COALESCE(MAX(id),0) FROM control_events").fetchone()[0]
            muted = engine.muted(conn)
            counts = dict(conn.execute("SELECT status, COUNT(*) FROM trade_alerts GROUP BY status").fetchall())
        alerts = engine.alerts(limit=50, newest_first=True)
        latest_alert = next((a for a in alerts if a["status"] == "alert"), None)
        warm = bool((status.get("warmup") or {}).get("ready"))
        healthy_status = status.get("status") in ("watching", "alert", "warming_up", "bootstrapped", "waiting_for_h1")
        params = policy.effective_params
        return {
            "connected": True, "checked_at": now, "mode": "alert_only",
            "ready": feed["available"] and warm and healthy_status and all(workers.values()),
            "feed": feed, "receiver": self.receiver, "engine": status, "workers": workers, "muted": muted,
            "alerts": alerts, "latest_alert": latest_alert, "alert_counts": counts,
            "events": events, "recent_events": recent, "latest_event_id": latest,
            "strategy": {"name": policy.strategy, "version": policy.strategy_version,
                         "rsi_low": params["rsi_low"], "sl_atr": params["sl_atr"], "rr": params["rr"],
                         "cost_r_max": params["cost_r_max"], "start_hour": params["start_hour"],
                         "end_hour": params["end_hour"], "time_stop_bars": params["exits"]["time_stop_bars"]},
            "policy": {"version": policy.version, "min_net_reward_risk": str(risk.min_net_reward_risk),
                       "min_stop_atr": str(risk.min_stop_atr), "max_stop_atr": str(risk.max_stop_atr),
                       "max_spread_atr": str(risk.max_spread_atr), "spread_floor": str(risk.spread_price),
                       "max_signal_age_seconds": risk.max_signal_age_seconds,
                       "max_entry_drift_atr": str(policy.max_entry_drift_atr)},
        }

    async def run(self):
        log = logging.getLogger("uvicorn.error")
        async with httpx.AsyncClient(timeout=3, trust_env=False, follow_redirects=False) as client:
            while True:
                try:
                    response = await client.get(self.receiver_url)
                    self.receiver = {"available": response.status_code == 200, "checked_at": time.time()}
                except httpx.HTTPError:
                    self.receiver = {"available": False, "checked_at": time.time()}
                try:
                    feed = await run_in_threadpool(self.feed)
                    await run_in_threadpool(self.record_health, "receiver", self.receiver["available"], "ตัวรับราคาในเครื่อง")
                    await run_in_threadpool(self.record_health, "feed", feed["available"], "ราคาจาก MT5")
                    with self.engine.db.connect() as conn:
                        conn.execute("DELETE FROM control_events WHERE id NOT IN (SELECT id FROM control_events ORDER BY id DESC LIMIT 2000)")
                except Exception:
                    log.warning("alert_monitor_error")
                await asyncio.sleep(15)


def create_alert_app(settings=None, bridge_settings=None, alert_policy=None, database_path=None):
    config = settings or Settings.from_env()
    feed_config = bridge_settings or BridgeSettings.from_env()
    policy = alert_policy or AlertPolicy.load(ALERT_POLICY)

    @asynccontextmanager
    async def lifespan(application):
        risk = RiskPolicy.load(policy.risk_path())
        if risk.source_server != feed_config.server or risk.source_broker_symbol not in feed_config.symbols:
            raise ValueError("Alert policy source does not match bridge configuration")
        db = Database(database_path or alert_database_path())
        db.path.parent.mkdir(parents=True, exist_ok=True)
        with db.connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
        engine = AlertEngine(db, policy, risk)
        market = MarketStore(feed_config.database_path)
        application.state.settings, application.state.alert_engine = config, engine
        log = logging.getLogger("uvicorn.error")
        log.info("ALERT mode: %s %s; no AI, no Paper account", policy.strategy, policy.strategy_version)

        async def poll():
            previous = None
            while True:
                try:
                    latest = await run_in_threadpool(market.latest, risk.source_broker_symbol)
                    if latest is None:
                        await run_in_threadpool(engine.note, "waiting_for_feed")
                    else:
                        status = await run_in_threadpool(engine.process, Snapshot.model_validate(latest["payload"]))
                        if status.get("status") != previous:
                            log.info("ALERT engine status=%s", status.get("status"))
                            previous = status.get("status")
                except sqlite3.Error:
                    with suppress(sqlite3.Error):
                        await run_in_threadpool(engine.note, "feed_database_unavailable")
                except Exception:
                    # Fail closed; a malformed source must never advance the checkpoint.
                    log.warning("alert_engine_error")
                    with suppress(sqlite3.Error):
                        await run_in_threadpool(engine.note, "engine_error")
                await asyncio.sleep(5)

        application.state.alert_task = asyncio.create_task(poll())
        monitor = AlertMonitor(application, feed_config)
        application.state.monitor = monitor
        application.state.monitor_task = asyncio.create_task(monitor.run())
        try:
            yield
        finally:
            for task in (application.state.monitor_task, application.state.alert_task):
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    app = FastAPI(title="XM Trade Alerts", version="1.0.0", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)

    @app.exception_handler(sqlite3.Error)
    async def database_error(request: Request, exc: sqlite3.Error):
        return JSONResponse(status_code=503, content={"detail": "Database unavailable"}, headers={"Retry-After": "1"})

    def authorize(request):
        authenticate(bearer(request), config.api_token)

    @app.get("/health")
    def health(request: Request):
        with request.app.state.alert_engine.db.connect() as conn:
            conn.execute("SELECT 1").fetchone()
        running = not request.app.state.alert_task.done()
        return {"status": "ok" if running else "degraded", "database": "ok", "mode": "alert_only",
                "engine_worker": "running" if running else "stopped"}

    @app.get("/xm/status")
    def status(request: Request):
        authorize(request)
        return {"engine": request.app.state.alert_engine.status(),
                "engine_worker": "running" if not request.app.state.alert_task.done() else "stopped",
                "mode": "alert_only", "symbol": "XM:XAUUSD"}

    @app.get("/alerts")
    def alerts(request: Request, after_id: int = Query(0, ge=0, le=2**63 - 1), limit: int = Query(50, ge=1, le=100)):
        authorize(request)
        items = request.app.state.alert_engine.alerts(after_id, limit)
        return {"items": items, "next_after_id": items[-1]["id"] if items else after_id}

    @app.get("/dashboard/state")
    def dashboard_state(request: Request, after_event_id: int = Query(0, ge=0, le=2**63 - 1)):
        authorize(request)
        return request.app.state.monitor.snapshot(after_event_id)

    @app.post("/dashboard/pause")
    def mute(request: Request, body: MuteRequest):
        authorize(request)
        result = request.app.state.alert_engine.set_muted(body.paused)
        return {"paused": result["muted"], "changed": result["changed"]}

    return app
