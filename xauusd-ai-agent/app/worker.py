import asyncio
import logging
import sqlite3
import time

from starlette.concurrency import run_in_threadpool

from .ai_analyzer import AIAnalyzer, AnalysisError, PROMPT_VERSION
from .config import Settings
from .database import Database
from .models import Signal
from .risk_manager import RiskManager, RiskPolicy
from .ai_providers import ProviderAnalyzer

logger = logging.getLogger(__name__)


class AnalysisWorker:
    def __init__(self, db: Database, analyzer: AIAnalyzer, settings: Settings, risk_manager: RiskManager | None = None):
        self.db, self.analyzer, self.settings = db, analyzer, settings
        self.risk_manager = risk_manager if risk_manager is not None else RiskManager(RiskPolicy.load(settings.risk_policy_path))

    async def process_once(self):
        config = self.settings
        provider,model,analyzer='lm_studio',config.lm_studio_model,self.analyzer
        if isinstance(self.analyzer,ProviderAnalyzer):
            provider,model,analyzer=await run_in_threadpool(self.analyzer.choose)
        job = await run_in_threadpool(self.db.claim_analysis, model, PROMPT_VERSION,
                                     config.ai_timeout_seconds, config.ai_max_attempts, config.ai_max_signal_age_seconds,provider)
        if job is None:
            return False
        result, error, status, delay, risk = None, None, "completed", 0, None
        try:
            signal = Signal.model_validate(job["payload"])
            result = (await analyzer.analyze(signal)).model_dump()
            risk = self.risk_manager.evaluate(signal, result)
        except AnalysisError as exc:
            error = exc.code
            status = "pending" if exc.retryable and job["attempt"] < config.ai_max_attempts else "failed"
            delay = min(60, 5 * 2 ** (job["attempt"] - 1))
        except Exception:
            # No exception text: it could contain a remote response or private data.
            result, risk, error, status = None, None, "internal_analysis_error", "failed"
        if time.time() - job["payload"]["bar_close_ms"] / 1000 > config.ai_max_signal_age_seconds:
            result, risk, error, status = None, None, "stale_signal", "expired"
        await run_in_threadpool(self.db.finish_analysis, job, status, result, error, delay, risk)
        return True

    async def run(self):
        while True:
            try:
                processed = await self.process_once()
            except sqlite3.Error:
                logger.warning("analysis_database_unavailable")
                processed = False
            if not processed:
                await asyncio.sleep(0.5)
