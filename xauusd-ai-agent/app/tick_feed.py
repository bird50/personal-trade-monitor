"""Authenticated, contiguous tick batches for deterministic paper accounting."""
import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager
from typing import Annotated, Literal

from fastapi import HTTPException
from pydantic import Field, model_validator

from .models import Price, StrictModel, Timestamp


class Tick(StrictModel):
    time_server_ms: Timestamp
    ordinal: Annotated[int, Field(ge=0, le=2048)]
    bid: Price
    ask: Price

    @model_validator(mode="after")
    def coherent(self):
        if self.ask < self.bid:
            raise ValueError("Inverted quote")
        return self


class TickBatch(StrictModel):
    schema_version: Annotated[int, Field(ge=1, le=1)]
    source: Literal["mt5"]
    account_mode: Literal["demo"]
    server: Annotated[str, Field(min_length=1, max_length=80)]
    broker_symbol: Annotated[str, Field(pattern=r"^[A-Za-z0-9_.#-]{1,40}$")]
    captured_at_utc_s: Timestamp
    server_time_s: Timestamp
    observed_utc_offset_s: Annotated[int, Field(ge=-50400, le=50400)]
    from_server_ms: Timestamp
    through_server_ms: Timestamp
    ticks: Annotated[list[Tick], Field(min_length=1, max_length=2048)]

    @model_validator(mode="after")
    def coherent(self):
        if self.observed_utc_offset_s % 900 or abs(self.server_time_s-self.captured_at_utc_s-self.observed_utc_offset_s)>60:
            raise ValueError("Invalid server clock")
        if not self.from_server_ms <= self.through_server_ms <= self.server_time_s*1000+1000:
            raise ValueError("Invalid coverage interval")
        previous, ordinal = 0, -1
        for tick in self.ticks:
            if not self.from_server_ms <= tick.time_server_ms <= self.through_server_ms:
                raise ValueError("Tick outside coverage")
            ordinal = ordinal+1 if tick.time_server_ms==previous else 0
            if tick.time_server_ms < previous or tick.ordinal!=ordinal:
                raise ValueError("Ticks must be ordered with per-millisecond ordinals")
            previous=tick.time_server_ms
        return self


class TickStore:
    def __init__(self,path):
        self.path=path

    @contextmanager
    def connect(self):
        db=sqlite3.connect(self.path,timeout=1)
        db.row_factory=sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def initialize(self):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""CREATE TABLE IF NOT EXISTS mt5_tick_streams (
                server TEXT NOT NULL, symbol TEXT NOT NULL, through_server_ms INTEGER NOT NULL,
                offset_s INTEGER NOT NULL, last_seq INTEGER NOT NULL, updated_at REAL NOT NULL,
                PRIMARY KEY(server,symbol))""")
            db.execute("""CREATE TABLE IF NOT EXISTS mt5_ticks (
                server TEXT NOT NULL, symbol TEXT NOT NULL, seq INTEGER NOT NULL,
                time_server_ms INTEGER NOT NULL, ordinal INTEGER NOT NULL, utc_ms INTEGER NOT NULL,
                bid TEXT NOT NULL, ask TEXT NOT NULL, received_at REAL NOT NULL,
                PRIMARY KEY(server,symbol,seq), UNIQUE(server,symbol,time_server_ms,ordinal))""")
            db.execute("CREATE INDEX IF NOT EXISTS mt5_ticks_retention ON mt5_ticks(utc_ms)")
            db.execute("""CREATE TABLE IF NOT EXISTS mt5_tick_batches (
                server TEXT NOT NULL,symbol TEXT NOT NULL,from_ms INTEGER NOT NULL,through_ms INTEGER NOT NULL,
                digest TEXT NOT NULL,received_at REAL NOT NULL,PRIMARY KEY(server,symbol,from_ms,through_ms))""")

    def cursor(self,server,symbol):
        with self.connect() as db:
            row=db.execute("SELECT through_server_ms FROM mt5_tick_streams WHERE server=? AND symbol=?",(server,symbol)).fetchone()
        return 0 if row is None else row[0]

    def status(self,server,symbol,quote_age_limit=30,now=None):
        """Distinguish a recovering exporter from a stopped/stale tick feed."""
        now=time.time() if now is None else now
        with self.connect() as db:
            row=db.execute('SELECT * FROM mt5_tick_streams WHERE server=? AND symbol=?',(server,symbol)).fetchone()
        result={'status':'waiting_for_tick_stream','received_age_seconds':None,
                'coverage_age_seconds':None,'last_sequence':0}
        if row is None:
            return result
        received=now-row['updated_at']
        coverage=now-(row['through_server_ms']/1000-row['offset_s'])
        receiving=0<=received<=60
        live=receiving and 0<=coverage<=quote_age_limit
        result.update(status='live' if live else 'catching_up' if receiving and coverage>quote_age_limit else 'stale',
                      received_age_seconds=round(received,1),coverage_age_seconds=round(coverage,1),
                      last_sequence=row['last_seq'])
        return result

    def insert(self,batch:TickBatch,now=None):
        now=time.time() if now is None else now
        if not -30<=now-batch.captured_at_utc_s<=120:
            raise HTTPException(422,"Tick batch capture is stale or in the future")
        # Capture time changes on retry; coverage and tick content remain the identity.
        content=batch.model_dump(exclude={"captured_at_utc_s","server_time_s"})
        digest=hashlib.sha256(json.dumps(content,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        key=(batch.server,batch.broker_symbol)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old=db.execute("SELECT digest FROM mt5_tick_batches WHERE server=? AND symbol=? AND from_ms=? AND through_ms=?",(*key,batch.from_server_ms,batch.through_server_ms)).fetchone()
            if old:
                if old[0]!=digest:
                    raise HTTPException(409,"Tick coverage already has different data")
                return False
            stream=db.execute("SELECT * FROM mt5_tick_streams WHERE server=? AND symbol=?",key).fetchone()
            if stream:
                if batch.observed_utc_offset_s!=stream['offset_s']:
                    raise HTTPException(409,"Tick stream offset changed; review required")
                if batch.from_server_ms!=stream['through_server_ms']+1:
                    raise HTTPException(409,"Tick cursor mismatch; fetch current cursor")
                seq=stream['last_seq']
            else:
                first_utc=batch.from_server_ms/1000-batch.observed_utc_offset_s
                if not -30<=now-first_utc<=120:
                    raise HTTPException(422,"New tick streams must start with recent coverage")
                seq=0
            for tick in batch.ticks:
                seq+=1
                db.execute("INSERT INTO mt5_ticks VALUES(?,?,?,?,?,?,?,?,?)",(*key,seq,tick.time_server_ms,tick.ordinal,
                    tick.time_server_ms-batch.observed_utc_offset_s*1000,str(tick.bid),str(tick.ask),now))
            db.execute("INSERT INTO mt5_tick_batches VALUES(?,?,?,?,?,?)",(*key,batch.from_server_ms,batch.through_server_ms,digest,now))
            db.execute("INSERT INTO mt5_tick_streams VALUES(?,?,?,?,?,?) ON CONFLICT(server,symbol) DO UPDATE SET through_server_ms=excluded.through_server_ms,last_seq=excluded.last_seq,updated_at=excluded.updated_at",
                (*key,batch.through_server_ms,batch.observed_utc_offset_s,seq,now))
            db.execute("DELETE FROM mt5_ticks WHERE utc_ms<?",(int((now-172800)*1000),))
            db.execute("DELETE FROM mt5_tick_batches WHERE received_at<?",(now-172800,))
        return True

    def read(self,server,symbol,after_seq,limit=5000):
        with self.connect() as db:
            db.execute("BEGIN")
            stream=db.execute("SELECT * FROM mt5_tick_streams WHERE server=? AND symbol=?",(server,symbol)).fetchone()
            if stream is None:
                return {"stream":None,"ticks":[],"min_seq":None}
            minimum=db.execute("SELECT MIN(seq) FROM mt5_ticks WHERE server=? AND symbol=?",(server,symbol)).fetchone()[0]
            ticks=db.execute("SELECT * FROM mt5_ticks WHERE server=? AND symbol=? AND seq>? ORDER BY seq LIMIT ?",(server,symbol,after_seq,limit)).fetchall()
        return {"stream":dict(stream),"ticks":[dict(x) for x in ticks],"min_seq":minimum}
