import asyncio
import json

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.ai_providers import ProviderAnalyzer
from app.bypass import BypassStore
from app.config import ROOT, Settings
from app.control_center import BypassRequest, install_control_routes
from app.models import Signal
from app.paper_trading import D
from app.risk_manager import RiskPolicy
from app.worker import AnalysisWorker
from test_control_center import center
from test_paper_trading import rig, step
from test_receiver import payload
from test_xm_engine import complete_xm


@pytest.fixture(autouse=True)
def structure_required_policy(monkeypatch):
    """Waivers concern pivot-structure blockers, so pin the policy variant that still requires them.

    The operational XM policy sets ``require_market_structure: false`` (xm-0.2); autouse runs
    before the ``rig`` fixture loads it.
    """
    original = RiskPolicy.load.__func__
    monkeypatch.setattr(RiskPolicy, 'load', classmethod(
        lambda cls, path: original(cls, path).model_copy(update={'require_market_structure': None})))

def config(rig):
    return Settings(webhook_token='w'*43, api_token='r'*43, database_path=rig['db'].path, ai_enabled=True,
                    allowed_symbols=('XM:XAUUSD',), risk_policy_path=ROOT/'config/risk-xm-paper.json')


def blocked_signal(rig, side='BUY'):
    data = complete_xm(rig['payload'], side)
    # A broken pivot is a structure blocker; normally AI must WAIT and Risk rejects.
    data['context']['market_structure']['recent_high']['active'] = False
    signal = Signal.model_validate(data)
    record_id, _ = rig['db'].insert(signal)
    return signal, record_id


def run_worker(rig, monkeypatch, at):
    monkeypatch.setattr('time.time', lambda: at)
    called = []
    async def scenario():
        async with httpx.AsyncClient() as client:
            router = ProviderAnalyzer(rig['db'], config(rig), client)
            async def forbidden(signal):
                called.append(signal.signal_id)
                raise AssertionError('AI must not be called while bypassed')
            for analyzer in router.analyzers.values():
                monkeypatch.setattr(analyzer, 'analyze', forbidden)
            return await AnalysisWorker(rig['db'], router, config(rig), rig['risk']).process_once()
    assert asyncio.run(scenario())
    return called


def approve_with(rig, side, bypass, at):
    data = complete_xm(rig['payload'], side)
    signal = Signal.model_validate(data)
    record_id, _ = rig['db'].insert(signal)
    result = {k: data[k] for k in ('entry', 'sl', 'tp', 'risk_reward')} | {'decision': side, 'reasons': ['Synthetic test']}
    report = rig['risk'].evaluate(signal, result, now=at, bypass=bypass)
    assert report['status'] == 'passed'
    with rig['db'].connect() as db:
        db.execute("UPDATE analyses SET status='completed',result_json=?,updated_at=? WHERE signal_record_id=?", (json.dumps(result), at, record_id))
        db.execute('INSERT OR REPLACE INTO risk_evaluations VALUES(?,?)', (record_id, json.dumps(report)))
    return record_id


def test_bypass_is_off_by_default_and_persists(rig):
    store = BypassStore(rig['db'])
    assert {k: store.get()[k] for k in ('ai', 'risk')} == {'ai': False, 'risk': False}
    assert store.set(ai=True)['changed'] == ['ai']
    assert store.set(ai=True)['changed'] == []
    assert BypassStore(rig['db']).get()['ai'] is True and BypassStore(rig['db']).get()['risk'] is False


def test_without_bypass_structure_blocker_still_rejects(rig):
    signal, _ = blocked_signal(rig)
    result = {k: getattr(signal, k) for k in ('entry', 'sl', 'tp', 'risk_reward')} | {'decision': 'BUY'}
    report = rig['risk'].evaluate(signal, result, now=rig['base']+2)
    assert report['status'] == 'rejected' and 'broken_pivot_level' in report['reason_codes']
    assert report['waived_reason_codes'] == [] and report['bypass'] == {'ai': False, 'risk': False}


def test_ai_bypass_skips_model_and_structure_then_opens(rig, monkeypatch):
    BypassStore(rig['db']).set(ai=True)
    _, record_id = blocked_signal(rig)
    assert run_worker(rig, monkeypatch, rig['base']+2) == []
    analysis = rig['db'].get_analysis(record_id, rig['risk'].policy_hash)
    assert (analysis['status'], analysis['provider'], analysis['model']) == ('completed', 'bypass', 'none')
    assert analysis['ai_decision'] == 'BUY' and analysis['effective_decision'] == 'BUY'
    report = rig['db'].get_risk(record_id)
    assert report['bypass'] == {'ai': True, 'risk': False}
    assert report['waived_reason_codes'] == ['broken_pivot_level']
    events, _ = step(rig, [(3, 2500, 2500.2)])
    assert events[0]['event'] == 'paper_open'
    position = rig['paper'].positions('open')[0]
    assert position['bypass'] == {'ai': True, 'risk': False}
    assert position['waived_reason_codes'] == ['broken_pivot_level']


def test_turning_bypass_off_cancels_pending_bypassed_approval(rig, monkeypatch):
    store = BypassStore(rig['db'])
    store.set(ai=True)
    _, record_id = blocked_signal(rig)
    run_worker(rig, monkeypatch, rig['base']+2)
    store.set(ai=False)
    assert step(rig, [(3, 2500, 2500.2)])[0] == []
    decision = rig['paper'].decisions()[0]
    assert (decision['signal_record_id'], decision['status'], decision['reason']) == (record_id, 'rejected', 'bypass_disabled')
    assert not rig['paper'].positions('open')


def test_risk_bypass_never_waives_integrity_checks(rig):
    signal, _ = blocked_signal(rig)
    result = {k: getattr(signal, k) for k in ('entry', 'sl', 'tp', 'risk_reward')} | {'decision': 'BUY'}
    stale = rig['risk'].evaluate(signal, result, now=rig['base']+400, bypass={'ai': True, 'risk': True})
    assert stale['status'] == 'rejected'
    assert 'stale_signal' in stale['reason_codes'] and 'broken_pivot_level' in stale['waived_reason_codes']
    changed = rig['risk'].evaluate(signal, result | {'tp': signal.tp+1}, now=rig['base']+2, bypass={'risk': True})
    assert 'ai_changed_prices_or_rr' in changed['reason_codes']


def test_risk_bypass_drops_notional_cap_but_keeps_trade_risk_budget(rig):
    data = complete_xm(rig['payload'])
    signal = Signal.model_validate(data)
    result = {k: data[k] for k in ('entry', 'sl', 'tp', 'risk_reward')} | {'decision': 'BUY'}
    normal = rig['risk'].evaluate(signal, result, now=rig['base']+2)
    relaxed = rig['risk'].evaluate(signal, result, now=rig['base']+2, bypass={'risk': True})
    assert D(relaxed['approved_quantity']) > D(normal['approved_quantity']), (normal['approved_quantity'], relaxed['approved_quantity'])
    assert D(relaxed['calculations']['planned_loss_usd']) <= D(relaxed['calculations']['risk_budget_usd'])


def test_risk_bypass_ignores_max_positions_and_daily_lock(rig):
    BypassStore(rig['db']).set(risk=True)
    approve_with(rig, 'BUY', {'risk': True}, rig['base']+2)
    step(rig, [(3, 2500, 2500.2)])
    with rig['db'].connect() as db:
        db.execute('UPDATE paper_days SET locked=1')
    assert rig['paper'].summary(now=rig['base']+3.1)['entries_enabled']
    approve_with(rig, 'SELL', {'risk': True}, rig['base']+4)
    events, _ = step(rig, [(5, 2500, 2500.2)])
    assert events[0]['event'] == 'paper_open'
    opened = rig['paper'].positions('open')
    assert len(opened) == 2 and all(p['bypass']['risk'] for p in opened)
    for p in opened:
        assert D(p['planned_risk_usd']) <= D('50')


def test_normal_approval_is_not_loosened_by_bypass(rig):
    approve_with(rig, 'BUY', None, rig['base']+2)
    step(rig, [(3, 2500, 2500.2)])
    BypassStore(rig['db']).set(risk=True)
    approve_with(rig, 'SELL', None, rig['base']+4)
    step(rig, [(5, 2500, 2500.2)])
    assert len(rig['paper'].positions('open')) == 1
    assert rig['paper'].decisions()[-1]['reason'] == 'max_positions'


def test_bypass_api_requires_token_validates_and_records_events(rig, monkeypatch):
    ui = center(rig, monkeypatch)
    app = FastAPI()
    app.state.control = ui
    install_control_routes(app, config(rig))
    client = TestClient(app)
    headers = {'Authorization': 'Bearer ' + 'r'*43}
    assert client.post('/dashboard/bypass', json={'ai': True}).status_code == 401
    assert client.post('/dashboard/bypass', json={}, headers=headers).status_code == 422
    assert client.post('/dashboard/bypass', json={'ai': 'yes'}, headers=headers).status_code == 422
    assert client.post('/dashboard/bypass', json={'ai': True, 'extra': 1}, headers=headers).status_code == 422
    body = client.post('/dashboard/bypass', json={'ai': True, 'risk': True}, headers=headers).json()
    assert (body['ai'], body['risk'], body['changed']) == (True, True, ['ai', 'risk'])
    assert client.post('/dashboard/bypass', json={'risk': False}, headers=headers).json()['changed'] == ['risk']
    with rig['db'].connect() as db:
        kinds = [(r[0], r[1]) for r in db.execute("SELECT kind,level FROM control_events WHERE kind='bypass_changed' ORDER BY id")]
    assert kinds == [('bypass_changed', 'warning'), ('bypass_changed', 'warning'), ('bypass_changed', 'info')]
    with pytest.raises(ValidationError):
        BypassRequest.model_validate({})
