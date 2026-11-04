"""Bounded, isolated Codex CLI inference using existing ChatGPT authentication."""
import asyncio
import json
import os
from pathlib import Path
import shutil
import signal as process_signal
import tempfile

from pydantic import ValidationError

from .ai_analyzer import AnalysisError,AnalysisResult,check_analysis
from .config import ROOT
from .structure import confirmation_blockers
from .webhook import reject_constant,unique_keys


def resolve_cli(settings):
    if settings.codex_cli_path:
        path=Path(settings.codex_cli_path)
        return str(path) if path.is_file() and os.access(path,os.X_OK) else None
    command=shutil.which('codex')
    if command:
        return command
    # GUI-started services often don't inherit the shell's nvm PATH.
    versions=Path.home()/'.nvm/versions/node'
    def version(path):
        try:return tuple(int(p) for p in path.parent.parent.name.lstrip('v').split('.'))
        except ValueError:return (0,)
    for path in sorted(versions.glob('*/bin/codex'),key=version,reverse=True):
        if path.is_file() and os.access(path,os.X_OK):return str(path)
    return None


def cli_environment(command):
    # Do not pass the trading server's .env tokens, API keys or agent runtime.
    allowed=('HOME','TMPDIR','LANG','LC_ALL','CODEX_HOME','SSL_CERT_FILE','SSL_CERT_DIR')
    env={key:os.environ[key] for key in allowed if key in os.environ}
    env['PATH']=str(Path(command).parent)+os.pathsep+os.defpath
    return env


async def stop_process(proc):
    # Each CLI runs in its own process group; never signal another service.
    try:os.killpg(proc.pid,process_signal.SIGTERM)
    except ProcessLookupError:pass
    try:await asyncio.wait_for(proc.wait(),1)
    except TimeoutError:
        try:os.killpg(proc.pid,process_signal.SIGKILL)
        except ProcessLookupError:pass
        await proc.wait()


async def run_cli(command,args,*,cwd,timeout,input_bytes=b'',limit=524288):
    proc=None
    async def read(pipe,maximum):
        data=bytearray()
        while chunk:=await pipe.read(8192):
            if len(data)+len(chunk)>maximum:raise AnalysisError('response_too_large')
            data.extend(chunk)
        return bytes(data)
    try:
        async with asyncio.timeout(timeout):
            proc=await asyncio.create_subprocess_exec(command,*args,cwd=cwd,env=cli_environment(command),
                stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE,
                start_new_session=True)
            async def write():
                try:
                    proc.stdin.write(input_bytes);await proc.stdin.drain()
                except (BrokenPipeError,ConnectionResetError):pass
                finally:proc.stdin.close()
            # TaskGroup cancels pipe readers on a timeout/error before cleanup.
            async with asyncio.TaskGroup() as group:
                out=group.create_task(read(proc.stdout,limit))
                err=group.create_task(read(proc.stderr,131072))
                group.create_task(write())
                group.create_task(proc.wait())
            return proc.returncode,out.result(),err.result()
    except BaseException as exc:
        if proc is not None:await stop_process(proc)
        if isinstance(exc,TimeoutError):raise AnalysisError('model_timeout',True) from None
        if isinstance(exc,OSError):raise AnalysisError('codex_cli_unavailable') from None
        if isinstance(exc,BaseExceptionGroup):
            errors=exc.subgroup(AnalysisError)
            if errors:raise errors.exceptions[0] from None
            raise AnalysisError('codex_process_error') from None
        raise


async def codex_login_status(settings):
    command=resolve_cli(settings)
    result={'installed':command is not None,'authenticated':False,'ready':False,'available':False,
            'error':'codex_cli_unavailable' if command is None else 'codex_auth_required'}
    if command is None:return result
    try:
        with tempfile.TemporaryDirectory(prefix='xm-codex-check-') as directory:
            code,out,err=await run_cli(command,['-c','forced_login_method="chatgpt"','login','status'],cwd=directory,timeout=5,limit=8192)
        # Do not read, copy or expose the authentication file itself.
        logged=code==0 and b'logged in using chatgpt' in (out+err).lower()
        result.update(authenticated=logged,available=logged,ready=logged,error=None if logged else 'codex_auth_required')
    except AnalysisError as exc:result['error']=exc.code
    return result


def cli_error(raw):
    text=raw.lower()
    if any(x in text for x in (b'usage limit',b'usage_limit',b'quota',b'rate limit',b'rate_limit')):
        return AnalysisError('codex_usage_limit')
    if any(x in text for x in (b'unauthorized',b'not logged in',b'authentication',b'401')):
        return AnalysisError('codex_auth_required')
    if b'model' in text and any(x in text for x in (b'not supported',b'not found',b'unavailable')):
        return AnalysisError('codex_model_unavailable')
    return AnalysisError('codex_request_failed',True)


def parse_events(raw,signal):
    final=None;completed=False
    try:
        for line in raw.splitlines():
            if not line.strip():continue
            event=json.loads(line,parse_constant=reject_constant,object_pairs_hook=unique_keys)
            if not isinstance(event,dict):raise ValueError('Invalid event')
            kind=event.get('type')
            if kind in ('error','turn.failed'):raise cli_error(line)
            if kind=='turn.completed':completed=True
            if kind in ('item.started','item.completed','item.updated'):
                item=event['item']
                if not isinstance(item,dict):raise ValueError('Invalid item')
                if item.get('type') not in ('agent_message','reasoning'):
                    raise AnalysisError('unexpected_tool_call')
                if kind=='item.completed' and item['type']=='agent_message':final=item['text']
        if not completed or not isinstance(final,str):raise AnalysisError('incomplete_response')
        data=json.loads(final,parse_constant=reject_constant,object_pairs_hook=unique_keys)
        result=AnalysisResult.model_validate(data)
    except (KeyError,IndexError,TypeError,ValueError,RecursionError,ValidationError):
        raise AnalysisError('invalid_output') from None
    return check_analysis(result,signal)


class CodexAnalyzer:
    def __init__(self,settings):
        self.settings=settings
        self.prompt=(ROOT/'prompts/trading_analysis.txt').read_text()
        self.lock=asyncio.Lock()

    async def analyze(self,signal):
        command=resolve_cli(self.settings)
        if command is None:raise AnalysisError('codex_cli_unavailable')
        evidence={'signal':signal.model_dump(),'confirmation_blockers':confirmation_blockers(signal)}
        instruction=self.prompt+'\nYou are a data-only signal filter in this run. Use only the supplied JSON evidence. Do not call tools, inspect files, execute commands, browse or modify anything. Return only the requested JSON object.\n'+json.dumps(evidence,allow_nan=False)
        try:
            async with asyncio.timeout(self.settings.ai_timeout_seconds):
                async with self.lock:
                    with tempfile.TemporaryDirectory(prefix='xm-codex-') as directory:
                        schema=Path(directory)/'result-schema.json'
                        schema.write_text(json.dumps(AnalysisResult.model_json_schema()))
                        args=['exec','--ignore-user-config','--ephemeral','--skip-git-repo-check','--sandbox','read-only',
                              '--model',self.settings.codex_model,'--color','never','--json','--output-schema',str(schema)]
                        values={'approval_policy':'"never"','forced_login_method':'"chatgpt"',
                                'model_reasoning_effort':'"low"','web_search':'"disabled"',
                                'project_doc_max_bytes':'0','skills.bundled.enabled':'false'}
                        for feature in ('shell_tool','apps','plugins','memories','multi_agent','multi_agent_v2',
                                        'browser_use','computer_use','in_app_browser','image_generation','view_image',
                                        'request_permissions_tool','code_mode'):
                            values['features.'+feature]='false'
                        for key,value in values.items():args+=['-c',key+'='+value]
                        args+=['-']
                        code,out,err=await run_cli(command,args,cwd=directory,timeout=self.settings.ai_timeout_seconds,
                            input_bytes=instruction.encode())
                        if code!=0:raise cli_error(out+err)
                        return parse_events(out,signal)
        except TimeoutError:raise AnalysisError('model_timeout',True) from None
