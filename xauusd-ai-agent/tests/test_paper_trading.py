import copy
import json
import time
from dataclasses import replace
from decimal import Decimal

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config import ROOT
from app.database import Database
from app.models import Signal
from app.paper_trading import D,PaperEngine,PaperPolicy
from app.risk_manager import RiskManager,RiskPolicy
from app.tick_feed import TickBatch,TickStore
from test_receiver import payload
from test_xm_engine import complete_xm


@pytest.fixture
def rig(tmp_path,payload):
    base=payload['bar_close_ms']/1000
    db=Database(tmp_path/'paper.db');db.initialize()
    risk=RiskManager(RiskPolicy.load(ROOT/'config/risk-xm-paper.json'))
    policy=PaperPolicy.load(ROOT/'config/paper-xm.json')
    paper=PaperEngine(db,policy,risk,now=base+1)
    ticks=TickStore(tmp_path/'ticks.db');ticks.initialize()
    return {'db':db,'risk':risk,'policy':policy,'paper':paper,'ticks':ticks,'base':base,'payload':payload}


def approve(rig,side='BUY',decision=None,at=None):
    at=rig['base']+2 if at is None else at
    data=complete_xm(rig['payload'],side)
    signal=Signal.model_validate(data)
    record_id,created=rig['db'].insert(signal)
    result={k:data[k] for k in ('entry','sl','tp','risk_reward')}|{'decision':decision or side,'reasons':['Synthetic test']}
    risk=rig['risk'].evaluate(signal,result,now=at)
    assert risk['status']=='passed'
    with rig['db'].connect() as db:
        db.execute("UPDATE analyses SET status='completed',result_json=?,updated_at=? WHERE signal_record_id=?",(json.dumps(result),at,record_id))
        db.execute('INSERT OR REPLACE INTO risk_evaluations VALUES(?,?)',(record_id,json.dumps(risk)))
    return record_id


def feed(rig,values,now=None):
    # values: [(seconds after base,bid,ask), ...]
    now=rig['base']+values[-1][0]+0.1 if now is None else now
    cursor=rig['ticks'].cursor('XMGlobal-MT5 7','GOLD')
    entries=[]
    previous=-1;ordinal=-1
    for seconds,bid,ask in values:
        stamp=int((rig['base']+seconds+7200)*1000)
        ordinal=ordinal+1 if stamp==previous else 0;previous=stamp
        entries.append({'time_server_ms':stamp,'ordinal':ordinal,'bid':bid,'ask':ask})
    batch=TickBatch.model_validate({'schema_version':1,'source':'mt5','account_mode':'demo',
        'server':'XMGlobal-MT5 7','broker_symbol':'GOLD','captured_at_utc_s':int(now),
        'server_time_s':int(now)+7200,'observed_utc_offset_s':7200,
        'from_server_ms':cursor+1 if cursor else entries[0]['time_server_ms']-1,
        'through_server_ms':entries[-1]['time_server_ms'],'ticks':entries})
    rig['ticks'].insert(batch,now=now)
    return batch,now


def step(rig,values):
    _,now=feed(rig,values)
    return rig['paper'].step(rig['ticks'],now=now),now


def test_funding_is_once_and_restart_is_idempotent(rig):
    PaperEngine(rig['db'],rig['policy'],rig['risk'],now=rig['base']+10)
    assert rig['paper'].summary()['balance_usd']=='10000'
    assert len(rig['paper'].ledger())==1


def test_buy_open_mark_tp_account_and_ledger(rig):
    record=approve(rig)
    events,now=step(rig,[(3,2500,2500.2)])
    assert events[0]['event']=='paper_open'
    position=rig['paper'].positions('open')[0]
    assert position['signal_record_id']==record
    assert D(position['entry'])==D('2500.25')
    assert D(position['quantity'])==D('3.99')
    summary=rig['paper'].summary(now=now)
    assert D(summary['balance_usd'])==D('9999.8005')
    assert D(summary['unrealized_pnl_usd'])==D('-0.9975')
    assert D(summary['equity_usd'])==D('9998.8030')
    events,now=step(rig,[(4,2510,2510.2)])
    assert events[0]['reason']=='TP'
    closed=rig['paper'].positions('closed')[0]
    assert D(closed['exit_price'])==D('2509.95')
    assert D(closed['net_pnl'])==D('38.3040')
    assert D(rig['paper'].summary(now=now)['balance_usd'])==D('10038.3040')
    assert sum(D(x['amount']) for x in rig['paper'].ledger())==D('10038.3040')
    rig['paper'].step(rig['ticks'],now=now)
    assert len(rig['paper'].ledger())==3
    assert len(rig['paper'].positions('closed'))==1


def test_sell_uses_ask_to_close_and_adverse_slippage(rig):
    approve(rig,'SELL')
    step(rig,[(3,2500,2500.2)])
    position=rig['paper'].positions('open')[0]
    assert D(position['entry'])==D('2499.95')
    assert D(position['quantity'])==D('3.99')
    step(rig,[(4,2489.9,2490.1)])
    assert len(rig['paper'].positions('open'))==1
    events,now=step(rig,[(5,2489.7,2489.9)])
    assert events[0]['reason']=='TP'
    assert D(rig['paper'].positions('closed')[0]['net_pnl'])==D('39.501')


def test_stop_gap_fills_observed_price_not_stop(rig):
    approve(rig)
    step(rig,[(3,2500,2500.2)])
    events,now=step(rig,[(4,2490,2490.2)])
    closed=rig['paper'].positions('closed')[0]
    assert events[0]['reason']=='SL'
    assert D(closed['exit_price'])==D('2489.95')
    assert -D(closed['net_pnl'])>D(closed['planned_risk_usd'])


def test_intraperiod_path_uses_ticks_not_last_snapshot(rig):
    approve(rig)
    step(rig,[(3,2500,2500.2)])
    # Price touches SL and recovers before the next 15s batch arrives.
    events,now=step(rig,[(4,2494.9,2495.1),(5,2500,2500.2),(6,2511,2511.2)])
    assert len(events)==1 and events[0]['reason']=='SL'
    assert len(rig['paper'].positions('closed'))==1


def test_restart_keeps_open_position_and_pnl_exact(rig):
    approve(rig);step(rig,[(3,2500,2500.2)])
    rig['paper']=PaperEngine(rig['db'],rig['policy'],rig['risk'],now=rig['base']+4)
    assert len(rig['paper'].positions('open'))==1
    step(rig,[(5,2510,2510.2)])
    assert D(rig['paper'].summary()['balance_usd'])==D('10038.3040')
    assert len(rig['paper'].ledger())==3


@pytest.mark.parametrize('decision,reason',[('WAIT','ai_wait'),('BUY','before_paper_activation')])
def test_no_wait_or_historical_approval_replay(rig,decision,reason):
    at=rig['base']+2 if decision=='WAIT' else rig['base']+0.5
    approve(rig,decision=decision,at=at)
    step(rig,[(3,2500,2500.2)])
    assert rig['paper'].positions('open')==[]
    assert rig['paper'].decisions()[0]['reason']==reason


def test_only_post_approval_ticks_can_open(rig):
    approve(rig,at=rig['base']+10)
    step(rig,[(3,2500,2500.2),(9,2500,2500.2)])
    assert rig['paper'].positions('open')==[]
    step(rig,[(11,2500,2500.2)])
    assert len(rig['paper'].positions('open'))==1


def test_stale_quote_never_opens_and_expired_pending_is_rejected(rig):
    approve(rig)
    batch,_=feed(rig,[(3,2500,2500.2)])
    rig['paper'].step(rig['ticks'],now=rig['base']+40)
    assert rig['paper'].positions('open')==[]
    assert rig['paper'].summary(now=rig['base']+40)['valuation_fresh'] is False
    rig['paper'].step(rig['ticks'],now=rig['base']+130)
    assert rig['paper'].decisions()[0]['reason']=='approval_expired'


@pytest.mark.parametrize('bid,ask,reason',[(2502,2502.2,'entry_price_drift'),(2500,2501,'entry_price_drift'),(2511,2511.2,'fill_outside_sl_tp')])
def test_fill_time_checks_reject(rig,bid,ask,reason):
    approve(rig)
    step(rig,[(3,bid,ask)])
    assert rig['paper'].positions('open')==[]
    assert rig['paper'].decisions()[0]['reason']==reason


def test_missing_ticks_halts_without_inventing_close(rig):
    approve(rig);step(rig,[(3,2500,2500.2)])
    batch,now=feed(rig,[(4,2490,2490.2),(5,2500,2500.2)])
    with rig['ticks'].connect() as conn:
        conn.execute('DELETE FROM mt5_ticks WHERE seq=2')
    assert rig['paper'].step(rig['ticks'],now=now)==[]
    assert rig['paper'].summary(now=now)['status']=='tick_history_gap_review_required'
    assert len(rig['paper'].positions('open'))==1


def test_atomic_trade_and_cursor_roll_back_on_failure(rig,monkeypatch):
    approve(rig)
    _,now=feed(rig,[(3,2500,2500.2)])
    original=rig['paper']._cash
    def fail(*args,**kwargs):
        original(*args,**kwargs)
        raise RuntimeError('Simulated crash')
    monkeypatch.setattr(rig['paper'],'_cash',fail)
    with pytest.raises(RuntimeError):
        rig['paper'].step(rig['ticks'],now=now)
    assert rig['paper'].positions('open')==[] and len(rig['paper'].ledger())==1
    monkeypatch.setattr(rig['paper'],'_cash',original)
    rig['paper'].step(rig['ticks'],now=now)
    assert len(rig['paper'].positions('open'))==1 and len(rig['paper'].ledger())==2


def test_daily_loss_lock_and_policy_change_restrict_entries(rig):
    approve(rig)
    with rig['db'].connect() as db:
        db.execute('INSERT INTO paper_days VALUES(?,?,1)',
                   (time.strftime('%Y-%m-%d',time.gmtime(rig['base']+7*3600)),'10000'))
    step(rig,[(3,2500,2500.2)])
    assert rig['paper'].decisions()[0]['reason']=='daily_loss_limit'
    revised=rig['policy'].model_copy(update={'max_positions':2})
    paper=PaperEngine(rig['db'],revised,rig['risk'])
    assert paper.summary()['policy_matches'] is False


def test_actual_loss_locks_day_and_next_day_gets_new_baseline(rig):
    approve(rig);step(rig,[(3,2500,2500.2)])
    step(rig,[(4,2440,2440.2)])
    assert rig['paper'].summary()['day']['locked']==1
    approve(rig,'SELL',at=rig['base']+5)
    step(rig,[(6,2500,2500.2)])
    assert rig['paper'].decisions()[1]['reason']=='daily_loss_limit'
    step(rig,[(86403,2500,2500.2)])
    assert rig['paper'].summary()['day']['locked']==0


def test_max_position_limit(rig):
    approve(rig);step(rig,[(3,2500,2500.2)])
    approve(rig,'SELL',at=rig['base']+4)
    step(rig,[(5,2500,2500.2)])
    assert rig['paper'].decisions()[1]['reason']=='max_positions'
    assert len(rig['paper'].positions('open'))==1


def test_quantity_shrinks_after_realized_loss(rig):
    approve(rig);step(rig,[(3,2500,2500.2)])
    step(rig,[(4,2490,2490.2)])
    approve(rig,'SELL',at=rig['base']+5)
    step(rig,[(6,2500,2500.2)])
    assert D(rig['paper'].positions('open')[0]['quantity'])==D('3.98')


def test_aggregate_risk_and_notional_include_opening_costs(rig,tmp_path):
    policy=PaperPolicy.model_validate({**rig['policy'].model_dump(),'initial_balance_usd':'100000',
                                      'max_positions':2,'max_open_risk_percent':'0.025'})
    db=Database(tmp_path/'aggregate.db');db.initialize()
    rig.update(db=db,policy=policy,paper=PaperEngine(db,policy,rig['risk'],now=rig['base']+1))
    approve(rig);step(rig,[(3,2500,2500.2)])
    approve(rig,'SELL',at=rig['base']+4)
    step(rig,[(5,2500,2500.2)])
    positions=rig['paper'].positions('open')
    assert len(positions)==2
    summary=rig['paper'].summary()
    assert D(summary['open_planned_risk_usd'])<=D(summary['equity_usd'])*D('0.025')/100
    assert sum(D(p['notional_usd']) for p in positions)<=D(summary['equity_usd'])


def test_frozen_costs_close_existing_position_after_policy_change(rig):
    approve(rig);step(rig,[(3,2500,2500.2)])
    policy=PaperPolicy.model_validate({**rig['policy'].model_dump(),'max_positions':2})
    rig['paper']=PaperEngine(rig['db'],policy,rig['risk'],now=rig['base']+4)
    assert rig['paper'].summary()['policy_matches'] is False
    step(rig,[(5,2510,2510.2)])
    assert len(rig['paper'].positions('closed'))==1
    assert D(rig['paper'].summary()['balance_usd'])==D('10038.3040')


def test_recovered_old_ticks_can_close_but_cannot_open(rig):
    approve(rig);step(rig,[(3,2500,2500.2)])
    _,now=feed(rig,[(4,2494.9,2495.1),(5,2500,2500.2)])
    events=rig['paper'].step(rig['ticks'],now=rig['base']+200)
    assert events[0]['reason']=='SL'
    assert rig['paper'].summary(now=rig['base']+200)['valuation_fresh'] is False


def test_overnight_gap_counts_against_new_day_loss_limit(rig):
    approve(rig);step(rig,[(3,2500,2500.2)])
    previous_equity=D(rig['paper'].summary()['equity_usd'])
    step(rig,[(86403,2440,2440.2)])
    summary=rig['paper'].summary()
    assert D(summary['day']['start_equity'])==previous_equity
    assert summary['day']['locked']==1


def test_tick_batches_idempotent_and_contiguous(rig):
    batch,now=feed(rig,[(3,2500,2500.2),(3,2500.1,2500.3)])
    assert rig['ticks'].insert(batch,now=now) is False
    raw=batch.model_dump();raw['ticks'][0]['bid']=2499
    with pytest.raises(HTTPException) as caught:
        rig['ticks'].insert(TickBatch.model_validate(raw),now=now)
    assert caught.value.status_code==409
    raw=batch.model_dump();raw['from_server_ms']=batch.through_server_ms+2;raw['through_server_ms']+=10
    raw['ticks']=[{'time_server_ms':raw['through_server_ms'],'ordinal':0,'bid':2500,'ask':2500.2}]
    with pytest.raises(HTTPException) as caught:
        rig['ticks'].insert(TickBatch.model_validate(raw),now=now)
    assert caught.value.status_code==409


def test_receiving_old_ticks_is_catchup_not_fresh_and_does_not_enable_entries(rig):
    approve(rig)
    _,now=feed(rig,[(3,2500,2500.2)])
    assert rig['ticks'].status('XMGlobal-MT5 7','GOLD',now=now)['status']=='live'
    assert rig['ticks'].status('XMGlobal-MT5 7','GOLD',now=now+100)['status']=='stale'
    feed(rig,[(4,2500,2500.2)],now=rig['base']+600)
    status=rig['ticks'].status('XMGlobal-MT5 7','GOLD',now=rig['base']+600)
    assert status['status']=='catching_up'
    assert status['received_age_seconds']==0
    assert status['coverage_age_seconds']==596
    assert rig['paper'].step(rig['ticks'],now=rig['base']+600)==[]
    assert not rig['paper'].summary(now=rig['base']+600)['entries_enabled']
    feed(rig,[(601,2500,2500.2)])
    assert rig['ticks'].status('XMGlobal-MT5 7','GOLD',now=rig['base']+601.1)['status']=='live'


@pytest.mark.parametrize('change',['reverse','ordinal','inverted','outside'])
def test_bad_tick_schema(change,rig):
    batch,_=feed(rig,[(3,2500,2500.2),(4,2500,2500.2)])
    raw=batch.model_dump()
    if change=='reverse':raw['ticks'].reverse()
    elif change=='ordinal':raw['ticks'][0]['ordinal']=1
    elif change=='inverted':raw['ticks'][0]['ask']=2499
    else:raw['through_server_ms']-=1000
    with pytest.raises(ValidationError):TickBatch.model_validate(raw)


def test_http_pipeline_approval_tick_open_close_and_auth(tmp_path,payload,monkeypatch):
    from app.ai_analyzer import AIAnalyzer,AnalysisResult
    from app.config import Settings
    from app.mt5_bridge import BridgeSettings,create_bridge
    from app.xm_main import create_xm_app
    base=payload['bar_close_ms']/1000
    clock=[base+1]
    monkeypatch.setattr('time.time',lambda:clock[0])
    async def analyze(self,signal):
        return AnalysisResult(decision=signal.side,entry=signal.entry,sl=signal.sl,tp=signal.tp,
                              risk_reward=signal.risk_reward,reasons=['Synthetic integration test'])
    monkeypatch.setattr(AIAnalyzer,'analyze',analyze)
    bridge=BridgeSettings(token='m'*43,database_path=tmp_path/'feed.db')
    config=Settings(webhook_token='w'*43,api_token='r'*43,database_path=tmp_path/'xm.db',
                    ai_enabled=True,allowed_symbols=('XM:XAUUSD',),risk_policy_path=ROOT/'config/risk-xm-paper.json')
    headers={'Authorization':'Bearer '+config.api_token}
    tick_headers={'Authorization':'Bearer '+bridge.token}
    with TestClient(create_bridge(bridge)) as market,TestClient(create_xm_app(config,bridge)) as app:
        for path in ('/paper/account','/paper/positions','/paper/ledger','/paper/decisions'):
            assert app.get(path).status_code==401
        assert app.get('/paper/positions?status=invalid',headers=headers).status_code==422
        assert market.get('/mt5/ticks/cursor?symbol=GOLD').status_code==401
        assert market.get('/mt5/ticks/cursor?symbol=GOLD',headers=tick_headers).text=='0'
        clock[0]=base+2
        response=app.post('/webhook',json=complete_xm(payload),headers={'Authorization':'Bearer '+config.webhook_token})
        assert response.status_code==201
        record_id=response.json()['id']
        def wait_until(predicate):
            deadline=time.monotonic()+5
            while time.monotonic()<deadline:
                if predicate():return
                time.sleep(0.02)
            pytest.fail('Background paper pipeline did not reach expected state')
        wait_until(lambda:app.get(f'/signals/{record_id}/analysis',headers=headers).json()['status']=='completed')
        previous=0
        for seconds,bid,ask,expected in [(3,2500,2500.2,'open'),(4,2510,2510.2,'closed')]:
            clock[0]=base+seconds
            stamp=int((clock[0]+7200)*1000)
            batch={'schema_version':1,'source':'mt5','account_mode':'demo','server':bridge.server,'broker_symbol':'GOLD',
                   'captured_at_utc_s':int(clock[0]),'server_time_s':int(clock[0])+7200,'observed_utc_offset_s':7200,
                   'from_server_ms':previous+1 if previous else stamp-1,'through_server_ms':stamp,
                   'ticks':[{'time_server_ms':stamp,'ordinal':0,'bid':bid,'ask':ask}]}
            assert market.post('/mt5/ticks',json=batch).status_code==401
            assert market.post('/mt5/ticks',json=batch,headers=tick_headers).status_code==201
            assert market.post('/mt5/ticks',json=batch,headers=tick_headers).status_code==200
            previous=stamp
            wait_until(lambda:len(app.get('/paper/positions?status='+expected,headers=headers).json()['items'])==1)
        account=app.get('/paper/account',headers=headers).json()
        assert D(account['balance_usd'])==D('10038.3040')
        assert account['open_positions']==0 and account['closed_trades']==1
        assert len(app.get('/paper/ledger',headers=headers).json()['items'])==3
