"""XM research pipeline: local feed -> persistent candidates -> existing AI/risk worker."""
import asyncio
import os
import json
import logging
import sqlite3
from contextlib import asynccontextmanager, suppress
from dataclasses import replace
from pathlib import Path
from typing import Literal

from fastapi import Request, Query
from starlette.concurrency import run_in_threadpool

from .config import ROOT, Settings
from .main import create_app
from .mt5_bridge import BridgeSettings, MarketStore, Snapshot
from .webhook import authenticate, bearer
from .xm_engine import XMEngine
from .paper_trading import PaperEngine, PaperPolicy
from .tick_feed import TickStore
from .control_center import ControlCenter,install_control_routes


def xm_settings():
    original = Settings.from_env()
    path = Path(os.getenv("XM_DATABASE_PATH", "data/xm-trading.db"))
    return replace(original, database_path=path if path.is_absolute() else ROOT / path,
                   allowed_symbols=("XM:XAUUSD",), risk_policy_path=ROOT / "config/risk-xm-paper.json")


def create_xm_app(settings=None, bridge_settings=None,paper_policy=None):
    config = settings or xm_settings()
    feed_config = bridge_settings or BridgeSettings.from_env()
    app = create_app(config)
    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        async with original_lifespan(application):
            policy = application.state.risk_manager.policy
            if policy.source_server != feed_config.server or policy.source_broker_symbol not in feed_config.symbols:
                raise ValueError("XM policy source does not match bridge configuration")
            engine = XMEngine(application.state.db, server=feed_config.server,
                              symbol=policy.source_broker_symbol, price_tick=policy.price_tick)
            market = MarketStore(feed_config.database_path)
            application.state.xm_engine = engine
            paper=PaperEngine(application.state.db,paper_policy or PaperPolicy.load(ROOT/'config/paper-xm.json'),application.state.risk_manager)
            application.state.paper=paper
            ticks=TickStore(feed_config.database_path)
            log=logging.getLogger('uvicorn.error')
            log.info('PAPER account ready: balance=%s USD; simulated_ounce',paper.summary()['balance_usd'])

            async def poll():
                while True:
                    try:
                        latest = await run_in_threadpool(market.latest, policy.source_broker_symbol)
                        if latest is None:
                            await run_in_threadpool(engine.note, "waiting_for_feed")
                        else:
                            data = Snapshot.model_validate(latest["payload"])
                            await run_in_threadpool(engine.process, data)
                    except sqlite3.Error:
                        with suppress(sqlite3.Error):
                            await run_in_threadpool(engine.note, "feed_database_unavailable")
                    except Exception:
                        # Fail closed; a malformed source must never advance the checkpoint.
                        with suppress(sqlite3.Error):
                            await run_in_threadpool(engine.note, "engine_error")
                    await asyncio.sleep(5)

            task = asyncio.create_task(poll())
            application.state.xm_task = task

            async def paper_poll():
                previous_status=None
                while True:
                    try:
                        events=await run_in_threadpool(paper.step,ticks)
                        for event in events:
                            log.info('PAPER %s',json.dumps(event,ensure_ascii=False))
                        status=(await run_in_threadpool(paper.summary))['status']
                        if status!=previous_status:
                            log.info('PAPER status=%s',status)
                            previous_status=status
                    except sqlite3.Error:
                        with suppress(sqlite3.Error):
                            await run_in_threadpool(paper.note,'tick_feed_unavailable')
                    except Exception:
                        log.warning('paper_processing_error')
                        with suppress(sqlite3.Error):
                            await run_in_threadpool(paper.note,'processing_error')
                    await asyncio.sleep(1)

            paper_task=asyncio.create_task(paper_poll())
            application.state.paper_task=paper_task
            control=ControlCenter(application,feed_config)
            application.state.control=control
            control_task=asyncio.create_task(control.monitor())
            try:
                yield
            finally:
                control_task.cancel()
                with suppress(asyncio.CancelledError):
                    await control_task
                paper_task.cancel()
                with suppress(asyncio.CancelledError):
                    await paper_task
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    app.router.lifespan_context = lifespan

    @app.get("/xm/status")
    def status(request: Request):
        authenticate(bearer(request), config.api_token)
        return {"engine": request.app.state.xm_engine.status(),
                "engine_worker": "running" if not request.app.state.xm_task.done() else "stopped",
                "paper_worker":"running" if not request.app.state.paper_task.done() else "stopped",
                "mode": "paper_simulation", "symbol": "XM:XAUUSD"}

    def authorize(request):
        authenticate(bearer(request),config.api_token)

    @app.get('/paper/account')
    def account(request:Request):
        authorize(request)
        return request.app.state.paper.summary()

    @app.get('/paper/positions')
    def positions(request:Request,status:Literal['open','closed']='open',after_id:int=Query(0,ge=0,le=2**63-1),limit:int=Query(50,ge=1,le=100)):
        authorize(request)
        items=request.app.state.paper.positions(status,after_id,limit)
        return {'items':items,'next_after_id':items[-1]['id'] if items else after_id}

    @app.get('/paper/ledger')
    def ledger(request:Request,after_id:int=Query(0,ge=0,le=2**63-1),limit:int=Query(50,ge=1,le=100)):
        authorize(request)
        items=request.app.state.paper.ledger(after_id,limit)
        return {'items':items,'next_after_id':items[-1]['id'] if items else after_id}

    @app.get('/paper/decisions')
    def decisions(request:Request,after_signal_id:int=Query(0,ge=0,le=2**63-1),limit:int=Query(50,ge=1,le=100)):
        authorize(request)
        items=request.app.state.paper.decisions(after_signal_id,limit)
        return {'items':items,'next_after_signal_id':items[-1]['signal_record_id'] if items else after_signal_id}

    install_control_routes(app,config)
    return app


# Uvicorn factory prevents reading credentials/config on import in unit tests.
