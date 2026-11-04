"""Persistent provider selection. Each claimed job pins its provider and model."""
import time

from .ai_analyzer import AIAnalyzer,AnalysisError
from .codex_analyzer import CodexAnalyzer

PROVIDERS=('lm_studio','codex')
LABELS={'lm_studio':'LM Studio','codex':'Codex'}


class AIProviderStore:
    def __init__(self,db,settings):
        self.db,self.settings=db,settings
        with db.connect() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS ai_provider_runtime (
                id INTEGER PRIMARY KEY CHECK(id=1),provider TEXT NOT NULL CHECK(provider IN ('lm_studio','codex')),
                updated_at REAL NOT NULL)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS ai_provider_checks (
                provider TEXT NOT NULL,model TEXT NOT NULL,last_success_at REAL,last_error TEXT,
                finished_at REAL NOT NULL,duration_ms INTEGER NOT NULL,PRIMARY KEY(provider,model))""")
            conn.execute('INSERT OR IGNORE INTO ai_provider_runtime VALUES(1,?,?)',(settings.ai_provider,time.time()))

    def model(self,provider):
        return self.settings.codex_model if provider=='codex' else self.settings.lm_studio_model

    def get(self):
        with self.db.connect() as conn:
            row=conn.execute('SELECT provider,updated_at FROM ai_provider_runtime WHERE id=1').fetchone()
        return {**dict(row),'model':self.model(row['provider'])}

    def set(self,provider):
        if provider not in PROVIDERS:raise ValueError('Invalid provider')
        with self.db.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            previous=conn.execute('SELECT provider FROM ai_provider_runtime WHERE id=1').fetchone()[0]
            if previous!=provider:
                conn.execute('UPDATE ai_provider_runtime SET provider=?,updated_at=? WHERE id=1',(provider,time.time()))
        return {**self.get(),'changed':previous!=provider}

    def record(self,provider,model,error,duration_ms):
        now=time.time()
        with self.db.connect() as conn:
            conn.execute("""INSERT INTO ai_provider_checks VALUES(?,?,?,?,?,?)
                ON CONFLICT(provider,model) DO UPDATE SET last_success_at=COALESCE(excluded.last_success_at,last_success_at),
                last_error=excluded.last_error,finished_at=excluded.finished_at,duration_ms=excluded.duration_ms""",
                (provider,model,now if error is None else None,error,now,duration_ms))

    def check(self,provider):
        with self.db.connect() as conn:
            row=conn.execute('SELECT * FROM ai_provider_checks WHERE provider=? AND model=?',(provider,self.model(provider))).fetchone()
        return None if row is None else dict(row)


class TrackedAnalyzer:
    def __init__(self,analyzer,store,provider,model):
        self.analyzer,self.store,self.provider,self.model=analyzer,store,provider,model

    async def analyze(self,signal):
        from starlette.concurrency import run_in_threadpool
        start=time.monotonic()
        try:result=await self.analyzer.analyze(signal)
        except AnalysisError as exc:
            await run_in_threadpool(self.store.record,self.provider,self.model,exc.code,round((time.monotonic()-start)*1000))
            raise
        await run_in_threadpool(self.store.record,self.provider,self.model,None,round((time.monotonic()-start)*1000))
        return result


class ProviderAnalyzer:
    def __init__(self,db,settings,client):
        self.store=AIProviderStore(db,settings)
        self.analyzers={'lm_studio':AIAnalyzer(settings,client),'codex':CodexAnalyzer(settings)}

    def choose(self):
        choice=self.store.get();provider=choice['provider'];model=choice['model']
        return provider,model,TrackedAnalyzer(self.analyzers[provider],self.store,provider,model)
