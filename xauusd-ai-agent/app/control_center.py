"""Local control UI data and durable events. All endpoints require API_TOKEN."""
import asyncio
import json
import logging
import sqlite3
import time
from typing import Literal

import httpx
from fastapi import Request,Query
from pydantic import model_validator

from .bypass import BypassStore
from .models import StrictModel
from .mt5_bridge import MarketStore
from .webhook import authenticate,bearer
from .paper_trading import D
from .tick_feed import TickStore
from .ai_providers import AIProviderStore,LABELS
from .codex_analyzer import codex_login_status


class PauseRequest(StrictModel):
    paused:bool


class ProviderRequest(StrictModel):
    provider:Literal['lm_studio','codex']


class BypassRequest(StrictModel):
    ai:bool|None=None
    risk:bool|None=None

    @model_validator(mode='after')
    def one_flag(self):
        if self.ai is None and self.risk is None:
            raise ValueError('Provide ai and/or risk')
        return self


BYPASS_TEXT={
    ('ai',True):('ยกเว้น AI แล้ว','สัญญาณใหม่ไม่เรียก AI และไม่ตรวจโครงสร้างตลาด · ใช้ฝั่งของสัญญาณโดยตรง'),
    ('ai',False):('กลับมาใช้ AI','สัญญาณใหม่ต้องผ่าน AI และโครงสร้างตลาด · ไม้ที่อนุมัติแบบยกเว้นและยังไม่เปิดจะถูกยกเลิก'),
    ('risk',True):('ยกเว้น Risk Manager แล้ว','ข้ามตัวกรองกลยุทธ์ เพดานขาดทุนรายวัน จำนวนไม้สูงสุด และเพดาน notional · ยังคุมความเสี่ยงต่อไม้'),
    ('risk',False):('กลับมาใช้ Risk Manager','สัญญาณใหม่ต้องผ่านกฎ Risk และกฎบัญชี · ไม้ที่อนุมัติแบบยกเว้นและยังไม่เปิดจะถูกยกเลิก'),
}


class ControlCenter:
    def __init__(self,app,feed_config):
        self.app,self.config,self.feed_config=app,app.state.settings,feed_config
        self.db=app.state.db
        self.health={}
        self.bad_counts={}
        self.incidents=set()
        self.ai={'available':False,'model_loaded':False,'checked_at':None,'error':'checking'}
        self.codex={'installed':False,'authenticated':False,'available':False,'ready':False,'checked_at':None,'error':'checking'}
        self.receiver={'available':False,'checked_at':None}
        self.market=MarketStore(feed_config.database_path)
        self.ticks=TickStore(feed_config.database_path)
        self.provider_store=AIProviderStore(self.db,self.config)
        self.bypass_store=BypassStore(self.db)
        with self.db.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS control_events (
                id INTEGER PRIMARY KEY,event_key TEXT UNIQUE NOT NULL,kind TEXT NOT NULL,
                level TEXT NOT NULL,title TEXT NOT NULL,message TEXT NOT NULL,at REAL NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS control_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS control_equity (
                at INTEGER PRIMARY KEY,balance TEXT NOT NULL,equity TEXT NOT NULL,fresh INTEGER NOT NULL)""")
            maximum=db.execute('SELECT COALESCE(MAX(id),0) FROM paper_ledger').fetchone()[0]
            db.execute("INSERT OR IGNORE INTO control_meta VALUES('ledger_cursor',?)",(str(maximum),))
            db.execute("INSERT OR IGNORE INTO control_meta VALUES('analysis_cursor',?)",(str(time.time()),))

    @staticmethod
    def event(db,key,kind,level,title,message,at=None):
        db.execute('INSERT OR IGNORE INTO control_events(event_key,kind,level,title,message,at) VALUES(?,?,?,?,?,?)',
                   (key,kind,level,title,message,time.time() if at is None else at))

    def set_paused(self,paused):
        with self.db.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            state=json.loads(db.execute('SELECT state_json FROM paper_runtime WHERE id=1').fetchone()[0])
            changed=bool(state.get('operator_paused',False))!=paused
            state['operator_paused']=paused
            db.execute('UPDATE paper_runtime SET state_json=? WHERE id=1',(json.dumps(state),))
            if changed:
                self.event(db,'operator:'+str(time.time_ns()),'paper_paused' if paused else 'paper_resumed','info',
                           'พักการเปิดไม้ใหม่' if paused else 'เปิดรับสัญญาณใหม่',
                           'ยังติดตามและปิดสถานะเดิมตาม SL/TP' if paused else 'เปิดได้เมื่อผ่าน AI, Risk และกฎบัญชี')
        return {'paused':paused,'changed':changed}

    def set_provider(self,provider):
        result=self.provider_store.set(provider)
        if result['changed']:
            with self.db.connect() as db:
                self.event(db,'provider:'+str(time.time_ns()),'ai_provider_changed','info','เลือก AI · '+LABELS[provider],
                           'ใช้กับงานวิเคราะห์ถัดไป งานที่เริ่มแล้วใช้ AI เดิมจนจบ')
        return result

    def set_bypass(self,ai=None,risk=None):
        result=self.bypass_store.set(ai=ai,risk=risk)
        if result['changed']:
            with self.db.connect() as db:
                for name in result['changed']:
                    title,message=BYPASS_TEXT[(name,result[name])]
                    self.event(db,f'bypass:{name}:{time.time_ns()}','bypass_changed','warning' if result[name] else 'info',title,message)
        return result

    def record_health(self,name,healthy,label,*,failure_title=None,failure_message=None):
        self.bad_counts[name]=0 if healthy else self.bad_counts.get(name,0)+1
        if not healthy and self.bad_counts[name]>=3 and name not in self.incidents:
            self.incidents.add(name)
            with self.db.connect() as db:
                self.event(db,'down:'+name+':'+str(time.time_ns()),'health_down','warning',failure_title or label+' ขัดข้อง',failure_message or 'ตรวจพบต่อเนื่อง 3 รอบ กรุณาตรวจการเชื่อมต่อ')
        elif healthy and name in self.incidents:
            self.incidents.remove(name)
            with self.db.connect() as db:
                self.event(db,'up:'+name+':'+str(time.time_ns()),'health_recovered','success',label+' กลับมาทำงานแล้ว','รับข้อมูลหรือเชื่อมต่อได้อีกครั้ง')
        self.health[name]=healthy

    def collect_events(self):
        with self.db.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            cursor=int(db.execute("SELECT value FROM control_meta WHERE key='ledger_cursor'").fetchone()[0])
            for row in db.execute('SELECT * FROM paper_ledger WHERE id>? ORDER BY id',(cursor,)).fetchall():
                position=db.execute('SELECT payload_json FROM paper_positions WHERE id=?',(row['position_id'],)).fetchone()
                p=json.loads(position[0]) if position else {}
                if row['kind']=='entry_commission':
                    self.event(db,'ledger:'+str(row['id']),'paper_open','success','เปิด '+p.get('side','')+' จำลอง',
                               f"{p.get('quantity','')} หน่วย ที่ {p.get('entry','')} · SL {p.get('sl','')} / TP {p.get('tp','')}",row['at_utc_ms']/1000)
                elif row['kind']=='exit_settlement':
                    self.event(db,'ledger:'+str(row['id']),'paper_close','info','ปิดสถานะจำลอง · '+p.get('exit_reason',''),
                               f"กำไรขาดทุนสุทธิ {p.get('net_pnl','')} USD · Balance {row['balance_after']} USD",row['at_utc_ms']/1000)
                cursor=row['id']
            db.execute("UPDATE control_meta SET value=? WHERE key='ledger_cursor'",(str(cursor),))
            since=float(db.execute("SELECT value FROM control_meta WHERE key='analysis_cursor'").fetchone()[0])
            rows=db.execute("SELECT * FROM analyses WHERE updated_at>=? AND status IN ('completed','failed','expired') ORDER BY updated_at",(since,)).fetchall()
            for row in rows:
                result=json.loads(row['result_json']) if row['result_json'] else {}
                decision=result.get('decision','WAIT')
                self.event(db,f"analysis:{row['signal_record_id']}:{row['status']}",'analysis','info' if row['status']=='completed' else 'warning',
                           LABELS.get(row['provider'],'AI')+' · '+decision if row['status']=='completed' else LABELS.get(row['provider'],'AI')+' · วิเคราะห์ไม่สำเร็จ',
                           f"สัญญาณ #{row['signal_record_id']} · {row['status']}"+((' · '+row['error_code']) if row['error_code'] else ' · การเปิดไม้ขึ้นกับ Risk และเงื่อนไขบัญชี'),row['updated_at'])
                since=max(since,row['updated_at'])
            db.execute("UPDATE control_meta SET value=? WHERE key='analysis_cursor'",(str(since),))
            db.execute('DELETE FROM control_events WHERE id NOT IN (SELECT id FROM control_events ORDER BY id DESC LIMIT 2000)')

    def snapshot(self,after_event_id=0):
        now=time.time()
        try:
            market=self.market.latest(self.app.state.risk_manager.policy.source_broker_symbol)
        except Exception:
            market=None
        feed={'available':market is not None and market['status']=='fresh','age_seconds':None,'bid':None,'ask':None}
        if market:
            p=market['payload']
            feed.update(age_seconds=market['age_seconds'],bid=p['quote']['bid'],ask=p['quote']['ask'],
                        captured_at=p['captured_at_utc_s'],m5_bars=len(p['m5']),h1_bars=len(p['h1']),symbol=p['broker_symbol'])
        paper=self.app.state.paper.summary(now=now)
        try:
            ticks=self.ticks.status(self.feed_config.server,self.app.state.risk_manager.policy.source_broker_symbol,
                                    self.app.state.paper.policy.max_entry_quote_age_seconds,now=now)
        except sqlite3.Error:
            ticks={'status':'unavailable','received_age_seconds':None,'coverage_age_seconds':None,'last_sequence':0}
        engine=self.app.state.xm_engine.status()
        choice=self.provider_store.get()
        selected_ai={**(self.codex if choice['provider']=='codex' else self.ai),'provider':choice['provider']}
        selected_ai['ready']=bool(selected_ai.get('ready',selected_ai.get('available') and selected_ai.get('model_loaded')))
        last_check=self.provider_store.check(choice['provider'])
        if choice['provider']=='codex' and last_check and last_check['last_error'] in ('codex_usage_limit','codex_model_unavailable','codex_auth_required'):
            selected_ai.update(ready=False,error=last_check['last_error'])
        with self.db.connect() as db:
            events=[dict(x) for x in db.execute('SELECT * FROM control_events WHERE id>? ORDER BY id LIMIT 100',(after_event_id,))]
            recent=[dict(x) for x in db.execute('SELECT * FROM control_events ORDER BY id DESC LIMIT 30')]
            latest=db.execute('SELECT COALESCE(MAX(id),0) FROM control_events').fetchone()[0]
            equity=[dict(x) for x in db.execute('SELECT * FROM control_equity ORDER BY at DESC LIMIT 120')][::-1]
            candidate=db.execute('SELECT id FROM signals ORDER BY id DESC LIMIT 1').fetchone()
            total=db.execute('SELECT COUNT(*) FROM signals').fetchone()[0]
            active=db.execute("SELECT signal_record_id,provider,model FROM analyses WHERE status='processing' AND lease_until>? ORDER BY signal_record_id LIMIT 1",(now,)).fetchone()
            closed=[{'id':r['id'],'status':'closed',**json.loads(r['payload_json'])} for r in db.execute("SELECT * FROM paper_positions WHERE status='closed' ORDER BY id DESC LIMIT 100")][::-1]
        positions=self.app.state.paper.positions('open',0,100)
        for p in positions:
            mark=D(p['mark_bid'] if p['side']=='BUY' else p['mark_ask'])
            p['unrealized_pnl']=format((mark-D(p['entry']))*D(p['quantity'])*(1 if p['side']=='BUY' else -1)*D(p['usd_per_price_unit']),'f')
        analysis=None if candidate is None else self.db.get_analysis(candidate['id'],self.app.state.risk_manager.policy_hash)
        return {'connected':True,'checked_at':now,'mode':'paper_simulation','feed':feed,'ticks':ticks,'ai':selected_ai,'receiver':self.receiver,
                'workers':{'engine':not self.app.state.xm_task.done(),'paper':not self.app.state.paper_task.done(),
                           'ai':self.app.state.worker_task is not None and not self.app.state.worker_task.done()},
                'account':paper,'engine':engine,'analysis':analysis,'candidate_count':total,
                'positions':positions,'closed':closed,
                'events':events,'recent_events':recent,'latest_event_id':latest,'equity_history':equity,
                'policy':{'risk_percent':str(self.app.state.risk_manager.policy.risk_percent),
                          'daily_loss_percent':str(self.app.state.paper.policy.daily_loss_limit_percent),
                          'max_positions':self.app.state.paper.policy.max_positions},
                'bypass':{k:v for k,v in self.bypass_store.get().items() if k in ('ai','risk','updated_at')},
                'model':choice['model'],'model_url':None if choice['provider']=='codex' else self.config.lm_studio_url,
                'ai_selection':{**choice,'label':LABELS[choice['provider']],
                    'in_flight':None if active is None else dict(active),
                    'last_check':last_check},
                'ai_options':{'lm_studio':{**self.ai,'model':self.config.lm_studio_model},
                              'codex':{**self.codex,'model':self.config.codex_model}}}

    async def monitor(self):
        async with httpx.AsyncClient(timeout=3,trust_env=False,follow_redirects=False) as client:
            while True:
                async def probe_model():
                    checked=time.time()
                    try:
                        start=time.monotonic()
                        response=await client.get(self.config.lm_studio_url+'/v1/models')
                        loaded=response.status_code==200 and any(x.get('id')==self.config.lm_studio_model for x in response.json().get('data',[]))
                        self.ai={'available':response.status_code==200,'model_loaded':loaded,'checked_at':checked,
                                 'latency_ms':round((time.monotonic()-start)*1000),'error':None if loaded else 'model_unavailable'}
                    except Exception:
                        self.ai={'available':False,'model_loaded':False,'checked_at':checked,'error':'connection_failed'}
                async def probe_receiver():
                    try:
                        response=await client.get('http://127.0.0.1:8001/health')
                        self.receiver={'available':response.status_code==200,'checked_at':time.time()}
                    except httpx.HTTPError:
                        self.receiver={'available':False,'checked_at':time.time()}
                async def probe_codex():
                    self.codex={**await codex_login_status(self.config),'checked_at':time.time()}
                await asyncio.gather(probe_model(),probe_receiver(),probe_codex())
                try:
                    from starlette.concurrency import run_in_threadpool
                    await run_in_threadpool(self.collect_events)
                    data=await run_in_threadpool(self.snapshot)
                    await run_in_threadpool(self.record_health,'model:'+data['ai_selection']['provider'],data['ai']['ready'],data['ai_selection']['label'])
                    await run_in_threadpool(self.record_health,'receiver',self.receiver['available'],'ตัวรับราคาในเครื่อง')
                    await run_in_threadpool(self.record_health,'feed',data['feed']['available'],'ราคาจาก MT5')
                    recovering=data['ticks']['status']=='catching_up'
                    await run_in_threadpool(self.record_health,'ticks',data['account']['valuation_fresh'],'Tick / Paper Trading',
                        failure_title='กำลังรับ Tick ย้อนหลัง' if recovering else None,
                        failure_message='EA ยังส่งข้อมูลอยู่ รอให้ตามทันราคาปัจจุบันก่อนเปิดไม้ใหม่' if recovering else None)
                    if data['account']['day'] and data['account']['day']['locked']:
                        with self.db.connect() as db:
                            self.event(db,'daily:'+data['account']['day']['day'],'daily_limit','warning','ถึงเพดานขาดทุนรายวัน','หยุดเปิดไม้ใหม่ ยังติดตามสถานะเดิมต่อ')
                    bucket=int(time.time()//30)*30
                    with self.db.connect() as db:
                        db.execute('INSERT OR IGNORE INTO control_equity VALUES(?,?,?,?)',(bucket,data['account']['balance_usd'],data['account']['equity_usd'],int(data['account']['valuation_fresh'])))
                        db.execute('DELETE FROM control_equity WHERE at<?',(bucket-172800,))
                except Exception:
                    logging.getLogger('uvicorn.error').warning('control_monitor_error')
                await asyncio.sleep(15)


def install_control_routes(app,config):
    @app.get('/dashboard/state')
    def dashboard_state(request:Request,after_event_id:int=Query(0,ge=0,le=2**63-1)):
        authenticate(bearer(request),config.api_token)
        return request.app.state.control.snapshot(after_event_id)

    @app.post('/dashboard/pause')
    def pause(request:Request,body:PauseRequest):
        authenticate(bearer(request),config.api_token)
        return request.app.state.control.set_paused(body.paused)

    @app.post('/dashboard/ai-provider')
    def provider(request:Request,body:ProviderRequest):
        authenticate(bearer(request),config.api_token)
        return request.app.state.control.set_provider(body.provider)

    @app.post('/dashboard/bypass')
    def bypass(request:Request,body:BypassRequest):
        authenticate(bearer(request),config.api_token)
        return request.app.state.control.set_bypass(ai=body.ai,risk=body.risk)
