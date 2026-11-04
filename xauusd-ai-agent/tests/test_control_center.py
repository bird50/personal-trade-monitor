import asyncio
import json
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from app.config import ROOT, Settings
from app.control_center import ControlCenter
from app.mt5_bridge import BridgeSettings, MarketStore
from app.paper_trading import PaperEngine
from app.xm_main import create_xm_app
from test_paper_trading import rig, approve, step
from test_receiver import payload


def center(rig, monkeypatch):
    monkeypatch.setattr('time.time', lambda: rig['base']+1)
    config=Settings(webhook_token='w'*43,api_token='r'*43,database_path=rig['db'].path,
                    allowed_symbols=('XM:XAUUSD',),risk_policy_path=ROOT/'config/risk-xm-paper.json')
    app=SimpleNamespace(state=SimpleNamespace(settings=config,db=rig['db'],paper=rig['paper'],risk_manager=rig['risk']))
    return ControlCenter(app,BridgeSettings(token='m'*43,database_path=rig['ticks'].path))


def test_pause_persists_and_blocks_entries_without_rejecting_approval(rig,monkeypatch):
    ui=center(rig,monkeypatch)
    record=approve(rig)
    assert ui.set_paused(True)['changed']
    assert not ui.set_paused(True)['changed']
    events,now=step(rig,[(3,2500,2500.2)])
    assert events==[]
    assert not rig['paper'].positions('open')
    restored=PaperEngine(rig['db'],rig['policy'],rig['risk'],now=now)
    assert restored.summary(now=now)['operator_paused']
    assert not restored.summary(now=now)['entries_enabled']
    ui.set_paused(False)
    events,now=step(rig,[(4,2500,2500.2)])
    assert events[0]['event']=='paper_open'
    assert rig['paper'].positions('open')[0]['signal_record_id']==record


def test_pause_keeps_sl_tp_monitoring_and_events_are_durable_once(rig,monkeypatch):
    ui=center(rig,monkeypatch)
    approve(rig)
    step(rig,[(3,2500,2500.2)])
    ui.set_paused(True)
    events,_=step(rig,[(4,2510,2510.2)])
    assert events[0]['event']=='paper_close'
    ui.collect_events();ui.collect_events()
    with rig['db'].connect() as db:
        kinds=[r[0] for r in db.execute('SELECT kind FROM control_events')]
    assert kinds.count('paper_open')==1
    assert kinds.count('paper_close')==1
    assert kinds.count('analysis')==1
    restarted=center(rig,monkeypatch)
    restarted.collect_events()
    with rig['db'].connect() as db:
        assert db.execute('SELECT COUNT(*) FROM control_events').fetchone()[0]==4


def test_health_debounce_and_recovery_once(rig,monkeypatch):
    ui=center(rig,monkeypatch)
    for i in range(2):ui.record_health('ai',False,'AI')
    with rig['db'].connect() as db:assert db.execute('SELECT COUNT(*) FROM control_events').fetchone()[0]==0
    for i in range(5):ui.record_health('ai',False,'AI')
    ui.record_health('ai',True,'AI');ui.record_health('ai',True,'AI')
    with rig['db'].connect() as db:
        assert [r[0] for r in db.execute('SELECT kind FROM control_events ORDER BY id')]==['health_down','health_recovered']


def test_dashboard_routes_auth_schema_and_safe_snapshot(tmp_path,monkeypatch):
    async def idle(self):await asyncio.Event().wait()
    monkeypatch.setattr(ControlCenter,'monitor',idle)
    bridge=BridgeSettings(token='m'*43,database_path=tmp_path/'feed.db')
    MarketStore(bridge.database_path).initialize()
    config=Settings(webhook_token='w'*43,api_token='r'*43,database_path=tmp_path/'xm.db',ai_enabled=False,
                    allowed_symbols=('XM:XAUUSD',),risk_policy_path=ROOT/'config/risk-xm-paper.json')
    headers={'Authorization':'Bearer '+config.api_token}
    with TestClient(create_xm_app(config,bridge)) as app:
        assert app.get('/dashboard/state').status_code==401
        assert app.post('/dashboard/pause',json={'paused':True}).status_code==401
        assert app.post('/dashboard/ai-provider',json={'provider':'codex'}).status_code==401
        assert app.post('/dashboard/ai-provider',headers=headers,json={'provider':'arbitrary-command'}).status_code==422
        assert app.post('/dashboard/pause',headers=headers,json={'paused':'yes'}).status_code==422
        assert app.get('/dashboard/state?after_event_id=-1',headers=headers).status_code==422
        response=app.get('/dashboard/state',headers=headers)
        assert response.status_code==200
        data=response.json()
        assert data['account']['balance_usd']=='10000'
        assert not data['feed']['available']
        assert config.api_token not in response.text and bridge.token not in response.text
        assert data['ai_selection']['provider']=='lm_studio'
        assert app.post('/dashboard/ai-provider',headers=headers,json={'provider':'codex'}).json()['changed']
        assert not app.post('/dashboard/ai-provider',headers=headers,json={'provider':'codex'}).json()['changed']
        current=app.get('/dashboard/state',headers=headers).json()
        assert current['ai_selection']['provider']=='codex'
        assert current['model']==config.codex_model
        assert app.post('/dashboard/pause',headers=headers,json={'paused':True}).json()['paused']
        assert app.get('/dashboard/state',headers=headers).json()['account']['operator_paused']


def test_stop_refuses_open_positions_and_leaves_exit_service_running(rig,monkeypatch):
    import scripts.control_system as controls
    approve(rig);step(rig,[(3,2500,2500.2)])
    monkeypatch.setattr('app.xm_main.xm_settings',lambda:SimpleNamespace(database_path=rig['db'].path))
    monkeypatch.setattr(controls,'status',lambda:{'xm':{'running':True,'managed':True},'receiver':{'running':True,'managed':True}})
    signals=[]
    monkeypatch.setattr(controls.os,'kill',lambda *args:signals.append(args))
    with pytest.raises(RuntimeError,match='สถานะเปิด'):controls.stop()
    assert not signals
    assert rig['paper'].summary()['operator_paused']
    events,_=step(rig,[(4,2510,2510.2)])
    assert events[0]['event']=='paper_close'


def test_stop_never_signals_unrecognized_process(monkeypatch):
    import scripts.control_system as controls
    monkeypatch.setattr(controls,'status',lambda:{'receiver':{'running':True,'managed':False}})
    signals=[]
    monkeypatch.setattr(controls.os,'kill',lambda *args:signals.append(args))
    with pytest.raises(RuntimeError):controls.stop()
    assert not signals
