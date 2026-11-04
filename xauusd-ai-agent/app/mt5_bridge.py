"""Loopback-only MT5 market data collector. No account or execution API."""
import json
import os
import re
import sqlite3
import time
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Literal

from dotenv import dotenv_values
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from fastapi.responses import PlainTextResponse
from pydantic import Field, ValidationError, model_validator
from starlette.concurrency import run_in_threadpool

from .config import ROOT
from .models import Price, StrictModel, Timestamp
from .webhook import RateLimiter, authenticate, bearer, read_json
from .tick_feed import TickBatch, TickStore


class Bar(StrictModel):
    # MT5 chart times are broker wall-clock timestamps, not Unix UTC instants.
    open_server_s: Timestamp
    open: Price
    high: Price
    low: Price
    close: Price
    tick_volume: Annotated[int, Field(ge=0)]
    spread_points: Annotated[int, Field(ge=0)]

    @model_validator(mode="after")
    def coherent(self):
        if not self.low <= min(self.open, self.close) <= max(self.open, self.close) <= self.high:
            raise ValueError("Inconsistent OHLC")
        return self


class Quote(StrictModel):
    time_server_ms: Timestamp
    bid: Price
    ask: Price

    @model_validator(mode="after")
    def coherent(self):
        if self.ask < self.bid:
            raise ValueError("Inverted quote")
        return self


class Snapshot(StrictModel):
    schema_version: Annotated[int, Field(ge=1, le=1)]
    source: Literal["mt5"]
    account_mode: Literal["demo"]
    server: Annotated[str, Field(min_length=1, max_length=80)]
    broker_symbol: Annotated[str, Field(pattern=r"^[A-Za-z0-9_.#-]{1,40}$")]
    captured_at_utc_s: Timestamp
    server_time_s: Timestamp
    observed_utc_offset_s: Annotated[int, Field(ge=-50400, le=50400)]
    digits: Annotated[int, Field(ge=0, le=8)]
    point: Price
    tick_size: Price
    quote: Quote
    m5: Annotated[list[Bar], Field(min_length=2, max_length=1000)]
    h1: Annotated[list[Bar], Field(min_length=2, max_length=1000)]

    @model_validator(mode="after")
    def coherent(self):
        if self.observed_utc_offset_s % 900:
            raise ValueError("Offset must be a multiple of 15 minutes")
        if abs(self.server_time_s - self.captured_at_utc_s - self.observed_utc_offset_s) > 60:
            raise ValueError("Server clock and UTC offset disagree")
        if not -30 <= self.server_time_s - self.quote.time_server_ms / 1000 <= 120:
            raise ValueError("Quote is stale or in the future")
        for bars, period in ((self.m5, 300), (self.h1, 3600)):
            previous = 0
            for bar in bars:
                if bar.open_server_s % period or bar.open_server_s <= previous:
                    raise ValueError("Bars must be aligned, unique and oldest first")
                if bar.open_server_s + period > self.server_time_s:
                    raise ValueError("Open candles are not accepted")
                previous = bar.open_server_s
            # Detect a live quote paired with an out-of-date chart cache.
            # Historical gaps are retained; latest M5 must be recent.
            if period == 300 and self.server_time_s - (bars[-1].open_server_s + period) > 600:
                raise ValueError("Latest M5 is stale")
        return self


@dataclass(frozen=True)
class BridgeSettings:
    token: str = field(repr=False)
    database_path: Path = ROOT / "data/mt5-market.db"
    server: str = "XMGlobal-MT5 7"
    # XM gold names vary by account. Pin exact chart names after login.
    symbols: tuple[str, ...] = ("GOLD", "GOLD#", "GOLDmicro", "XAUUSD")

    def __post_init__(self):
        if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", self.token):
            raise ValueError("Create .env.mt5 using scripts/init_mt5.py")
        if not self.symbols or not self.server:
            raise ValueError("Configure MT5 server and symbol allowlist")

    @classmethod
    def from_env(cls):
        config = {**dotenv_values(ROOT / ".env.mt5"), **os.environ}
        path = Path(config.get("MT5_DATABASE_PATH", "data/mt5-market.db"))
        return cls(token=config.get("MT5_BRIDGE_TOKEN", ""),
                   database_path=path if path.is_absolute() else ROOT / path,
                   server=config.get("MT5_SERVER", "XMGlobal-MT5 7"),
                   symbols=tuple(x.strip() for x in config.get("MT5_SYMBOLS", "GOLD,GOLD#,GOLDmicro,XAUUSD").split(",") if x.strip()))


class MarketStore:
    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=1)
        try:
            with db:
                yield db
        finally:
            db.close()

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""CREATE TABLE IF NOT EXISTS mt5_snapshots (
                server TEXT NOT NULL, broker_symbol TEXT NOT NULL,
                captured_at_utc_s INTEGER NOT NULL, received_at_utc_s REAL NOT NULL,
                payload_json TEXT NOT NULL,
                PRIMARY KEY(server, broker_symbol, captured_at_utc_s))""")

    def insert(self, snapshot: Snapshot):
        raw = snapshot.model_dump_json()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload_json FROM mt5_snapshots WHERE server=? AND broker_symbol=? AND captured_at_utc_s=?",
                             (snapshot.server, snapshot.broker_symbol, snapshot.captured_at_utc_s)).fetchone()
            if row:
                if row[0] != raw:
                    raise HTTPException(409, "Snapshot timestamp already has different data")
                return False
            db.execute("INSERT INTO mt5_snapshots VALUES (?, ?, ?, ?, ?)",
                       (snapshot.server, snapshot.broker_symbol, snapshot.captured_at_utc_s, time.time(), raw))
            # Bound duplicated history: keep at most 240 snapshots per server/symbol.
            db.execute("""DELETE FROM mt5_snapshots WHERE server=? AND broker_symbol=?
                AND captured_at_utc_s NOT IN (SELECT captured_at_utc_s FROM mt5_snapshots
                WHERE server=? AND broker_symbol=? ORDER BY captured_at_utc_s DESC LIMIT 240)""",
                (snapshot.server, snapshot.broker_symbol, snapshot.server, snapshot.broker_symbol))
            return True

    def latest(self, symbol: str):
        with self.connect() as db:
            row = db.execute("SELECT received_at_utc_s, payload_json FROM mt5_snapshots WHERE broker_symbol=? ORDER BY captured_at_utc_s DESC LIMIT 1", (symbol,)).fetchone()
        if row is None:
            return None
        payload = json.loads(row[1])
        age = time.time() - payload["captured_at_utc_s"]
        return {"status": "fresh" if age <= 120 else "stale", "age_seconds": round(age, 1),
                "received_at_utc_s": row[0], "mode": "market_data_only", "payload": payload}


def create_bridge(settings: BridgeSettings | None = None):
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.settings = settings or BridgeSettings.from_env()
        app.state.store = MarketStore(app.state.settings.database_path)
        app.state.store.initialize()
        app.state.ticks = TickStore(app.state.settings.database_path)
        app.state.ticks.initialize()
        app.state.limiter = RateLimiter(20)
        # Keep snapshot traffic isolated from bounded historical tick bursts.
        app.state.tick_limiter = RateLimiter(64)
        app.state.last_attempt = None
        yield

    app = FastAPI(title="MT5 Local Market Data", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)

    @app.exception_handler(sqlite3.Error)
    async def db_error(request: Request, exc: sqlite3.Error):
        return JSONResponse(status_code=503, content={"detail": "Market database unavailable"})

    @app.get("/health")
    def health(request: Request):
        with request.app.state.store.connect() as db:
            db.execute("SELECT 1 FROM mt5_snapshots LIMIT 1").fetchone()
        return {"status": "ok", "mode": "market_data_only"}

    @app.post("/mt5/snapshot")
    async def snapshot(request: Request):
        config = request.app.state.settings
        authenticate(bearer(request), config.token)
        request.app.state.limiter.check()
        payload = await read_json(request, 512 * 1024)
        try:
            data = Snapshot.model_validate(payload)
        except ValidationError as exc:
            codes = {
                "Inconsistent OHLC": "inconsistent_ohlc",
                "Inverted quote": "inverted_quote",
                "Offset must be a multiple of 15 minutes": "invalid_offset",
                "Server clock and UTC offset disagree": "server_clock_offset_mismatch",
                "Quote is stale or in the future": "quote_time_invalid",
                "Bars must be aligned, unique and oldest first": "bar_order_invalid",
                "Open candles are not accepted": "open_candle",
                "Latest M5 is stale": "m5_cache_stale",
            }
            errors = []
            for error in exc.errors()[:8]:
                loc = error["loc"]
                field = str(loc[0]) if loc and loc[0] in Snapshot.model_fields else "snapshot"
                message = str(error.get("ctx", {}).get("error", ""))
                errors.append({"field": field, "code": codes.get(message, error["type"])})
            request.app.state.last_attempt = {"status": "rejected", "reason": "schema_validation",
                                              "errors": errors, "received_at_utc_s": time.time()}
            raise HTTPException(422, "Invalid MT5 snapshot") from None
        if data.server != config.server or data.broker_symbol not in config.symbols:
            request.app.state.last_attempt = {"status": "rejected", "reason": "source_not_allowed",
                                              "broker_symbol": data.broker_symbol,
                                              "server_matches": data.server == config.server,
                                              "received_at_utc_s": time.time()}
            raise HTTPException(422, "Server or symbol is not allowed")
        age = time.time() - data.captured_at_utc_s
        if not -30 <= age <= 120:
            request.app.state.last_attempt = {"status": "rejected", "reason": "snapshot_time_invalid",
                                              "received_at_utc_s": time.time()}
            raise HTTPException(422, "Snapshot timestamp is stale or in the future")
        created = await run_in_threadpool(request.app.state.store.insert, data)
        request.app.state.last_attempt = {"status": "accepted", "broker_symbol": data.broker_symbol,
                                          "received_at_utc_s": time.time()}
        return JSONResponse(status_code=201 if created else 200,
                            content={"status": "stored", "duplicate": not created,
                                     "m5_bars": len(data.m5), "h1_bars": len(data.h1)})

    @app.get("/mt5/latest")
    def latest(request: Request, symbol: str = Query(min_length=1, max_length=40)):
        authenticate(bearer(request), request.app.state.settings.token)
        if symbol not in request.app.state.settings.symbols:
            raise HTTPException(422, "Symbol is not allowed")
        result = request.app.state.store.latest(symbol)
        if result is None:
            raise HTTPException(404, "No MT5 data received")
        return result

    @app.get("/mt5/status")
    def status(request: Request):
        authenticate(bearer(request), request.app.state.settings.token)
        return {"last_attempt": request.app.state.last_attempt}

    @app.get("/mt5/ticks/cursor",response_class=PlainTextResponse)
    def tick_cursor(request:Request,symbol:str=Query(min_length=1,max_length=40)):
        config=request.app.state.settings
        authenticate(bearer(request),config.token)
        if symbol not in config.symbols:
            raise HTTPException(422,"Symbol is not allowed")
        return str(request.app.state.ticks.cursor(config.server,symbol))

    @app.post("/mt5/ticks")
    async def ticks(request:Request):
        config=request.app.state.settings
        authenticate(bearer(request),config.token)
        request.app.state.tick_limiter.check()
        payload=await read_json(request,512*1024)
        try:
            batch=TickBatch.model_validate(payload)
        except ValidationError:
            raise HTTPException(422,"Invalid tick batch") from None
        if batch.server!=config.server or batch.broker_symbol not in config.symbols:
            raise HTTPException(422,"Server or symbol is not allowed")
        created=await run_in_threadpool(request.app.state.ticks.insert,batch)
        return JSONResponse(status_code=201 if created else 200,content={"status":"stored","duplicate":not created,"through_server_ms":batch.through_server_ms})

    return app


app = create_bridge()
