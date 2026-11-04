#!/usr/bin/env python3
"""Fixed local service controls used by XM Control. Never manages MT5 or LM Studio."""
import fcntl
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
RUNTIME = ROOT / '.runtime'
SERVICES = {'receiver': (8001, 'app.mt5_bridge:app'), 'xm': (8002, 'app.xm_main:create_xm_app')}


def command(args):
    return subprocess.run(args, capture_output=True, text=True, timeout=4).stdout.strip()


def inspect_service(name):
    port, module = SERVICES[name]
    pids = command(['/usr/sbin/lsof', '-t', '-nP', f'-iTCP:{port}', '-sTCP:LISTEN']).splitlines()
    if not pids:
        return {'running': False, 'managed': False}
    if len(set(pids)) != 1:
        return {'running': True, 'managed': False}
    pid = int(pids[0])
    uid = command(['/bin/ps', '-p', str(pid), '-o', 'uid='])
    args = command(['/bin/ps', '-p', str(pid), '-o', 'command=']).split()
    cwd = command(['/usr/sbin/lsof', '-a', '-p', str(pid), '-d', 'cwd', '-Fn'])
    expected = module in args and 'uvicorn' in args and '--host' in args and '127.0.0.1' in args
    managed = uid == str(os.getuid()) and expected and ('n' + str(ROOT)) in cwd.splitlines()
    return {'running': True, 'managed': managed, 'pid': pid}


def status():
    return {name: inspect_service(name) for name in SERVICES}


def start():
    results = {}
    for name, (port, module) in SERVICES.items():
        state = inspect_service(name)
        if state['running']:
            if not state['managed']:
                raise RuntimeError(f'Port {port} is occupied by another process; no action taken')
            results[name] = state
            continue
        args = [str(ROOT / '.venv/bin/python'), '-m', 'uvicorn', module]
        if name == 'xm':
            args += ['--factory']
        args += ['--host', '127.0.0.1', '--port', str(port), '--workers', '1', '--no-access-log', '--no-proxy-headers']
        fd = os.open(RUNTIME / (name + '.log'), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, 'ab') as log:
            child = subprocess.Popen(args, cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log,
                                     stderr=log, start_new_session=True)
        for _ in range(50):
            state = inspect_service(name)
            if state['running'] or child.poll() is not None:
                break
            time.sleep(.1)
        if not state['running'] or not state['managed']:
            raise RuntimeError(f'{name} did not start; inspect .runtime/{name}.log')
        results[name] = state
    return results


def pause_and_check_empty():
    from app.xm_main import xm_settings
    path = xm_settings().database_path
    if not path.exists():
        return
    with sqlite3.connect(path, timeout=5) as db:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'paper_runtime' not in tables:
            return
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT state_json FROM paper_runtime WHERE id=1').fetchone()
        if row:
            state = json.loads(row[0])
            state['operator_paused'] = True
            db.execute('UPDATE paper_runtime SET state_json=? WHERE id=1', (json.dumps(state),))
        opened = db.execute("SELECT COUNT(*) FROM paper_positions WHERE status='open'").fetchone()[0]
    # Persist pause even when stopping is refused, so existing exits keep working.
    if opened:
        raise RuntimeError('ยังมีสถานะเปิดอยู่: พักการเปิดไม้ใหม่แล้ว และคงบริการติดตาม SL/TP ไว้')


def stop():
    current = status()
    if any(x['running'] and not x['managed'] for x in current.values()):
        raise RuntimeError('พบโปรแกรมอื่นบนพอร์ตที่ใช้งาน จึงไม่หยุดโปรแกรมนั้น')
    pause_and_check_empty()
    for name in ('xm', 'receiver'):
        state = inspect_service(name)
        if not state['running']:
            continue
        if not state['managed']:
            raise RuntimeError('Process identity changed; no signal sent')
        os.kill(state['pid'], signal.SIGTERM)
        for _ in range(60):
            if not inspect_service(name)['running']:
                break
            time.sleep(.1)
        else:
            raise RuntimeError(f'{name} is still shutting down; no force kill sent')
    return status()


def main():
    RUNTIME.mkdir(mode=0o700, exist_ok=True)
    lock = os.open(RUNTIME / 'control.lock', os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(lock, 'w') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        action = sys.argv[1] if len(sys.argv) == 2 else ''
        if action not in ('status', 'start', 'stop'):
            raise ValueError('Unknown action')
        return {'ok': True, 'services': {'status': status, 'start': start, 'stop': stop}[action]()}


if __name__ == '__main__':
    try:
        print(json.dumps(main(), ensure_ascii=False))
    except Exception as exc:
        # Only controlled messages; never include environment, process arguments or credentials.
        message = str(exc) if isinstance(exc, (RuntimeError, ValueError)) else 'ควบคุมบริการไม่สำเร็จ กรุณาตรวจไฟล์ log ใน .runtime'
        print(json.dumps({'ok': False, 'error': message}, ensure_ascii=False))
        sys.exit(1)
