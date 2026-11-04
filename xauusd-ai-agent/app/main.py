import sqlite3
import asyncio
from contextlib import asynccontextmanager, suppress

import httpx

from fastapi import Depends, FastAPI, HTTPException, Path, Query, Request
from fastapi.responses import JSONResponse

from .config import Settings
from .database import Database
from .webhook import RateLimiter, authenticate, bearer, router
from .ai_analyzer import AIAnalyzer
from .ai_providers import ProviderAnalyzer
from .worker import AnalysisWorker
from .risk_manager import RiskPolicy, RiskManager


def create_app(settings: Settings | None = None):
    @asynccontextmanager
    async def lifespan(application: FastAPI):
        config = settings if settings is not None else Settings.from_env()
        # Fail at startup for a missing/invalid risk policy even in receive-only mode.
        risk_manager = RiskManager(RiskPolicy.load(config.risk_policy_path))
        db = Database(config.database_path)
        db.initialize()
        application.state.settings = config
        application.state.db = db
        application.state.limiter = RateLimiter(config.webhook_rate_per_minute)
        application.state.worker_task = None
        application.state.risk_manager = risk_manager
        async with httpx.AsyncClient(timeout=config.ai_timeout_seconds, trust_env=False, follow_redirects=False) as client:
            providers=ProviderAnalyzer(db,config,client)
            application.state.ai_providers=providers
            if config.ai_enabled:
                worker = AnalysisWorker(db, providers, config, risk_manager)
                application.state.worker_task = asyncio.create_task(worker.run())
            try:
                yield
            finally:
                if application.state.worker_task is not None:
                    application.state.worker_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await application.state.worker_task

    application = FastAPI(title="XAUUSD Signal Receiver", version="0.1.0", lifespan=lifespan,
                          docs_url=None, redoc_url=None, openapi_url=None)
    application.include_router(router)

    @application.exception_handler(sqlite3.Error)
    async def database_error(request: Request, exc: sqlite3.Error):
        return JSONResponse(status_code=503, content={"detail": "Database unavailable"},
                            headers={"Retry-After": "1"})

    def authorize_read(request: Request):
        authenticate(bearer(request), request.app.state.settings.api_token)

    @application.get("/health")
    def health(request: Request):
        request.app.state.db.health()
        task = request.app.state.worker_task
        running = task is not None and not task.done()
        return {"status": "ok" if not request.app.state.settings.ai_enabled or running else "degraded",
                "database": "ok", "mode": "receive_and_analyze" if request.app.state.settings.ai_enabled else "receive_only",
                "ai_worker": "running" if running else "disabled" if task is None else "stopped"}

    @application.get("/signals", dependencies=[Depends(authorize_read)])
    def signals(request: Request, after_id: int = Query(0, ge=0, le=2**63 - 1), limit: int = Query(50, ge=1, le=100)):
        items = request.app.state.db.list_signals(after_id, limit)
        return {"items": items, "next_after_id": items[-1]["id"] if items else after_id}

    @application.get("/signals/{record_id}", dependencies=[Depends(authorize_read)])
    def signal(request: Request, record_id: int = Path(ge=1, le=2**63 - 1)):
        item = request.app.state.db.get_signal(record_id)
        if item is None:
            raise HTTPException(404, "Signal not found")
        return item

    @application.get("/signals/{record_id}/analysis", dependencies=[Depends(authorize_read)])
    def analysis(request: Request, record_id: int = Path(ge=1, le=2**63 - 1)):
        item = request.app.state.db.get_analysis(record_id, request.app.state.risk_manager.policy_hash)
        if item is None:
            raise HTTPException(404, "Signal not found")
        return item

    @application.get("/signals/{record_id}/risk", dependencies=[Depends(authorize_read)])
    def risk(request: Request, record_id: int = Path(ge=1, le=2**63 - 1)):
        if request.app.state.db.get_signal(record_id) is None:
            raise HTTPException(404, "Signal not found")
        report = request.app.state.db.get_risk(record_id)
        if report is None:
            return {"status": "not_evaluated", "signal_record_id": record_id}
        state = request.app.state.db.get_analysis(record_id, request.app.state.risk_manager.policy_hash)
        return {**report, "current_status": state["risk_status"], "effective_decision": state["effective_decision"]}

    return application


app = create_app()
