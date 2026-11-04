import asyncio
import json
import os
from dataclasses import replace
from pathlib import Path
import sys

import httpx
import pytest

from app.ai_analyzer import AnalysisError
from app.ai_providers import AIProviderStore,ProviderAnalyzer
from app.codex_analyzer import CodexAnalyzer,cli_environment,parse_events,run_cli
from app.database import Database
from app.models import Signal
from app.worker import AnalysisWorker
from test_ai import output
from test_receiver import settings,payload


def events(result,extra=None):
    records=[{'type':'thread.started','thread_id':'synthetic'},{'type':'turn.started'}]
    if extra:records.append(extra)
    records += [{'type':'item.completed','item':{'type':'agent_message','text':json.dumps(result)}},{'type':'turn.completed','usage':{'input_tokens':1}}]
    return b'\n'.join(json.dumps(x).encode() for x in records)


@pytest.mark.parametrize('change,code',[
    ({'entry':2501},'modified_prices_or_rr'),({'risk_reward':3},'modified_prices_or_rr'),
    ({'decision':'SELL'},'reversed_signal'),({'decision':'BUY'},'unsupported_confirmation'),
    ({'entry':'2500'},'invalid_output'),({'extra':True},'invalid_output'),
])
def test_codex_uses_the_same_decision_guards(payload,change,code):
    with pytest.raises(AnalysisError,match=code):
        parse_events(events(output(payload)|change),Signal.model_validate(payload))


def test_codex_rejects_tools_and_incomplete_output(payload):
    signal=Signal.model_validate(payload)
    for kind in ('command_execution','mcp_tool_call','web_search','file_change','unknown_tool'):
        with pytest.raises(AnalysisError,match='unexpected_tool_call'):
            parse_events(events(output(payload),{'type':'item.started','item':{'type':kind}}),signal)
    with pytest.raises(AnalysisError,match='incomplete_response'):
        parse_events(b'{"type":"turn.started"}',signal)
    with pytest.raises(AnalysisError,match='invalid_output'):parse_events(b'[]',signal)
    with pytest.raises(AnalysisError,match='codex_usage_limit') as caught:
        parse_events(b'{"type":"turn.failed","error":{"message":"usage limit reached"}}',signal)
    assert not caught.value.retryable


def test_cli_environment_does_not_pass_trading_or_api_credentials(monkeypatch):
    for name in ('API_TOKEN','WEBHOOK_TOKEN','MT5_BRIDGE_TOKEN','OPENAI_API_KEY','CODEX_API_KEY','CODEX_ACCESS_TOKEN'):
        monkeypatch.setenv(name,'synthetic-private-marker')
    env=cli_environment('/tmp/node/bin/codex')
    assert 'synthetic-private-marker' not in json.dumps(env)
    assert env['PATH'].startswith('/tmp/node/bin:')


def test_actual_subprocess_protocol_is_isolated_and_schema_validated(tmp_path,settings,payload):
    script=tmp_path/'fake-codex'
    encoded=repr(events(output(payload)))
    script.write_text(f'''#!{sys.executable}
import json,os,sys
from pathlib import Path
args=sys.argv[1:]
assert '--sandbox' in args and args[args.index('--sandbox')+1]=='read-only'
assert '--ignore-user-config' in args and '--ephemeral' in args
assert 'features.shell_tool=false' in args and 'features.plugins=false' in args
assert 'features.multi_agent=false' in args and 'web_search="disabled"' in args
assert 'forced_login_method="chatgpt"' in args
assert 'API_TOKEN' not in os.environ and 'OPENAI_API_KEY' not in os.environ
schema=json.loads(Path(args[args.index('--output-schema')+1]).read_text())
assert schema['additionalProperties']==False
evidence=sys.stdin.read()
assert 'confirmation_blockers' in evidence and 'Do not call tools' in evidence
assert not Path('.env').exists()
sys.stdout.buffer.write({encoded})
''')
    script.chmod(0o700)
    config=replace(settings,codex_cli_path=str(script))
    result=asyncio.run(CodexAnalyzer(config).analyze(Signal.model_validate(payload)))
    assert result.decision=='WAIT'


def test_timeout_kills_only_owned_cli_process_group(tmp_path):
    pidfile=tmp_path/'pid'
    program='import os,time,pathlib;pathlib.Path('+repr(str(pidfile))+').write_text(str(os.getpid()));time.sleep(60)'
    with pytest.raises(AnalysisError,match='model_timeout'):
        asyncio.run(run_cli(sys.executable,['-c',program],cwd=tmp_path,timeout=.4))
    pid=int(pidfile.read_text())
    with pytest.raises(ProcessLookupError):os.kill(pid,0)


def test_oversized_output_is_stopped_without_returning_raw_content(tmp_path):
    with pytest.raises(AnalysisError,match='response_too_large'):
        asyncio.run(run_cli(sys.executable,['-c','print("x"*100000)'],cwd=tmp_path,timeout=3,limit=8192))


def test_provider_persists_and_inflight_job_keeps_original_identity(tmp_path,settings,payload,monkeypatch):
    db=Database(tmp_path/'jobs.db');db.initialize()
    signal=Signal.model_validate(payload);record,_=db.insert(signal)
    config=replace(settings,database_path=db.path,ai_enabled=True)
    async def scenario():
        async with httpx.AsyncClient() as client:
            router=ProviderAnalyzer(db,config,client)
            entered=asyncio.Event();release=asyncio.Event()
            async def analyze(s):
                entered.set();await release.wait()
                from app.ai_analyzer import AnalysisResult
                return AnalysisResult.model_validate(output(payload))
            monkeypatch.setattr(router.analyzers['lm_studio'],'analyze',analyze)
            worker=AnalysisWorker(db,router,config)
            task=asyncio.create_task(worker.process_once());await entered.wait()
            router.store.set('codex');release.set();await task
            completed=db.get_analysis(record)
            assert completed['provider']=='lm_studio'
            assert completed['model']==settings.lm_studio_model
            assert completed['status']=='completed'
            assert AIProviderStore(db,config).get()['provider']=='codex'
            provider,model,_=router.choose()
            assert (provider,model)==('codex',config.codex_model)
    asyncio.run(scenario())


def test_canceled_worker_does_not_record_a_successful_codex_check(tmp_path,settings,payload,monkeypatch):
    db=Database(tmp_path/'cancel.db');db.initialize();db.insert(Signal.model_validate(payload))
    config=replace(settings,database_path=db.path,ai_provider='codex')
    async def scenario():
        async with httpx.AsyncClient() as client:
            router=ProviderAnalyzer(db,config,client)
            entered=asyncio.Event()
            async def analyze(s):entered.set();await asyncio.Event().wait()
            monkeypatch.setattr(router.analyzers['codex'],'analyze',analyze)
            task=asyncio.create_task(AnalysisWorker(db,router,config).process_once())
            await entered.wait();task.cancel()
            with pytest.raises(asyncio.CancelledError):await task
            assert router.store.check('codex') is None
    asyncio.run(scenario())
