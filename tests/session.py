#!/usr/bin/env python3
"""Stdlib формы CLI и live identity #641, без собственного turn/model store.

Прежние файловые проверки #222/#453 заменены реальным IPC/no-created-files
в session_telemetry.py и PG model-causality в _telemetry_cases.py. Внешние
CLI session JSON/JSONL и public envelope/history API сохраняются.
STATUS: FIXED — see #641
"""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile

import hermetic  # noqa: F401
from _lib import Checks, patched
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from mop import session

NOW = 1_790_000_000
HOOKS = json.loads((ROOT/'tests/session_hooks.json').read_text())
STATUS = json.loads((ROOT/'tests/statusline_hooks.json').read_text())


def main():
    c = Checks()
    for name in ('SessionStart', 'UserPromptSubmit', 'Stop', 'PostModelSwitch'):
        payload = dict(HOOKS['ok']['Stop'], hook_event_name=name)
        c.expect('turn record '+name, session.turn_record(payload, NOW),
                 dict(event=name, at=NOW, error=None, detail=None))
    failure = HOOKS['fail']['StopFailure']
    c.expect('failure retains bounded pure diagnostic', session.turn_record(failure, NOW),
             dict(event='StopFailure', at=NOW, error='model_not_found', detail=failure['last_assistant_message']))
    c.expect('statusline pure projection', session.model_record(STATUS['statusline'], NOW),
             dict(model='claude-opus-5-5', at=NOW))
    c.expect('switch pure projection', session.model_record(STATUS['switch'], NOW),
             dict(model='glm-5.3[1m]', at=NOW))
    for payload in (None, [], {}, dict(STATUS['statusline'], session_id='../../etc'),
                    dict(STATUS['statusline'], model={'id':'a'*129}), dict(STATUS['statusline'], model=42)):
        c.expect('invalid model projection', session.model_record(payload, NOW), None)
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        sessions, cwd = root/'sessions', root/'work'
        sessions.mkdir()
        cwd.mkdir()
        with patched(session, SESSIONS=str(sessions)):
            c.expect('no CLI session', session.state(str(cwd)), dict(status=None, waitingFor=None,
                alive=False, listen=False, turn=None, sessionId=None, model_at=None, model=None))
            def put(pid, sid, at, **more):
                (sessions/(str(pid)+'.json')).write_text(json.dumps(dict(pid=pid, sessionId=sid,
                    cwd=str(cwd), statusUpdatedAt=at, status='idle', messagingSocketPath=str(root/'absent'), **more)))
            put(999999, 'old', 1)
            put(999998, 'new', 2, waitingFor='dialog')
            c.expect('freshest CLI diagnostic survives', session.state(str(cwd))['waitingFor'], 'dialog')
            put(os.getpid(), 'current-process', 3)
            c.check('alive API survives', session.state(str(cwd))['alive'])
            with socket.socket(socket.AF_UNIX) as server:
                server.bind(str(root/'live.sock'))
                server.listen(8)
                (sessions/'live.json').write_text(json.dumps(dict(pid=os.getpid(), sessionId='live',
                    cwd=str(cwd), statusUpdatedAt=4, status='busy', messagingSocketPath=str(root/'live.sock'))))
                # Свежайший corpse не скрывает freshest live session identity.
                put(999997, 'corpse', 5)
                got = session.state(str(cwd))
                c.expect('model binds live CLI identity', got['sessionId'], 'live')
                c.expect('no controller observation in standalone probe',
                         (got['turn'], got['model'], got['model_at']), (None, None, None))
        for verb in ('hook', 'statusline'):
            for body in ('', 'not-json', '[1]', json.dumps(failure), 'x'*8192):
                result = subprocess.run([sys.executable, '-I', str(ROOT/'mop/session.py'), verb],
                    input=body, text=True, capture_output=True,
                    env=dict(os.environ, HOME=str(root), MOP_TELEMETRY_SOCKET='', MOP_TELEMETRY_TOKEN=''), timeout=2)
                c.expect('quiet no-storage hook', (result.returncode, result.stdout, result.stderr), (0, '', ''))
        c.check('no owned local state files', not (root/'.local').exists())
    return c.report('session')


if __name__ == '__main__':
    sys.exit(main())
