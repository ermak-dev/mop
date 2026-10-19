"""Общий owned stdio/TLS harness для body standby #748, не authority fixture."""
from datetime import datetime, timedelta, timezone
import http.server
import json
import os
from pathlib import Path
import queue
import socket
import subprocess
import sys
import tempfile
import threading

from _control_fixture import EPOCH, NODE, PROJECT, KEY, PUPPET, INCARNATION

ROOT = Path(__file__).resolve().parents[1]
TOKEN = 'd'*40


def expiry(seconds=60):
    return (datetime.now(timezone.utc)+timedelta(seconds=seconds)).isoformat()


def request():
    return dict(schema_version=1, generation='2', scope=dict(epoch=EPOCH,
        target_id=PUPPET, incarnation_id=INCARNATION, node_id=NODE,
        executor_epoch=PROJECT, session_id='body-native-session', producer_epoch=KEY))


def context():
    return dict(schema_version=1, request=request(), installation_id='owned-install',
        restore_generation=KEY, project_id=PROJECT, grant_id=KEY, subject_id=7,
        credential_id=PUPPET, assignment_revision='3', expires_at=expiry(), token=TOKEN)


def bootstrap(origin, pem):
    return dict(schema_version=1, request=request(), installation_id='owned-install',
        restore_generation=KEY, project_id=PROJECT, grant_id=KEY, expires_at=expiry(),
        origin=origin, ca_pem=pem, body_uid=os.geteuid())


def publish(path, value):
    # Root публикует только завершённый private inode, не world-readable draft.
    fd, staging = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream)
        os.replace(staging, path)
    finally:
        if os.path.exists(staging): os.unlink(staging)


def catalogue():
    return dict(schema_version=1, catalogue_version=1, epoch=EPOCH,
        actor=dict(user_id=7, kind='agent', human_owner_id=4, node_id=None, grant_id=KEY),
        scope=dict(project_id=PROJECT, node_id=None, client_id=None), actions=['catalog.view_project'])


class TLSCatalogue(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_GET(self):
        self.server.calls.append((self.path, self.headers.get('Authorization')))
        if self.headers.get('Authorization') != 'Token '+TOKEN:
            self.send_error(403); return
        if self.path.startswith('/api/v1/capabilities?'):
            data = self.server.catalogue
        elif self.path.startswith('/api/v1/projects?'):
            data = dict(schema_version=1, count=0, next_cursor=None, results=[])
        else:
            self.send_error(404); return
        raw = json.dumps(data).encode()
        self.send_response(200); self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw))); self.end_headers(); self.wfile.write(raw)


class LostReply(TLSCatalogue):
    """POST дошёл до owned listener; только ответ потерян, retry запрещён."""
    def do_POST(self):
        self.server.calls.append((self.path, self.headers.get('Authorization')))
        self.rfile.read(int(self.headers.get('Content-Length', '0')))
        if hasattr(self.server, 'post_entered'):
            self.server.post_entered.set()
            self.server.release.wait(12)
        self.close_connection = True
        self.connection.shutdown(socket.SHUT_RDWR)
        self.connection.close()

    def do_GET(self):
        if not self.path.startswith('/api/v1/operations'):
            return super().do_GET()
        self.server.calls.append((self.path, self.headers.get('Authorization')))
        raw = json.dumps(dict(schema_version=1, epoch=EPOCH,
            error=dict(code='not_found', message='operation not visible', retryable=False))).encode()
        self.send_response(404); self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw))); self.end_headers(); self.wfile.write(raw)


def stop_arguments():
    return dict(puppet_id=PUPPET, epoch=EPOCH, incarnation=INCARNATION,
        expected_revision='1', request_id=KEY, not_after=expiry())


class Stdio:
    def __init__(self, case, directory, *, deadline=None, token=TOKEN):
        self.case, self.directory, self.token = case, Path(directory), token
        self.boot, self.secret = self.directory/'bootstrap.json', self.directory/'context.json'
        env = {**os.environ, 'PYTHONPATH': str(ROOT), 'MOP_API_ORIGIN': 'http://ambient.invalid',
            'MOP_API_CA': '/never-read', 'MOP_API_TOKEN': 'ambient-not-authority'}
        self.process = subprocess.Popen([sys.executable, '-P', '-m', 'mop.cli', 'mcp',
            '--body-standby', '--body-bootstrap', str(self.boot), '--body-context', str(self.secret),
            '--body-deadline', deadline or expiry(30)], cwd=directory, env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.lines, self.notifications = queue.Queue(), []
        def read():
            for line in self.process.stdout: self.lines.put(line)
            self.lines.put(None)
        self.reader = threading.Thread(target=read); self.reader.start()
        case.addCleanup(self.close)

    def send(self, method, params=None, number=None):
        frame = dict(jsonrpc='2.0', method=method, params=params or {})
        if number is not None: frame['id'] = number
        self.process.stdin.write(json.dumps(frame)+'\n'); self.process.stdin.flush()

    def frame(self, timeout=15):
        try: line = self.lines.get(timeout=timeout)
        except queue.Empty: self.case.fail('body MCP did not answer within its bound')
        self.case.assertIsNotNone(line, 'body MCP exited before response')
        self.case.assertNotIn(self.token, line)
        self.case.assertNotIn('ambient-not-authority', line)
        return json.loads(line)

    def rpc(self, number, method, params=None):
        self.send(method, params, number)
        while True:
            value = self.frame()
            if value.get('method'):
                self.notifications.append(value); continue
            self.case.assertEqual(value.get('id'), number, value)
            self.case.assertNotIn('error', value, value)
            return value['result']

    def initialize(self):
        result = self.rpc(1, 'initialize', dict(protocolVersion='2025-11-25', capabilities={},
            clientInfo=dict(name='owned-body-client', version='1')))
        self.case.assertTrue(result['capabilities']['tools']['listChanged'])
        self.case.assertFalse(result['capabilities']['experimental']['private_inbox']['available'])
        self.send('notifications/initialized')
        return result

    def changed(self):
        value = self.frame()
        self.case.assertEqual(value, dict(jsonrpc='2.0', method='notifications/tools/list_changed'))

    def close(self):
        if self.process.stdin.closed: return
        self.process.stdin.close()
        try: self.process.wait(timeout=12)
        except subprocess.TimeoutExpired:
            self.process.kill(); self.process.wait(timeout=3)
            self.case.fail('body MCP left I/O running after EOF')
        errors = self.process.stderr.read()
        self.reader.join(2)
        self.process.stdout.close(); self.process.stderr.close()
        self.case.assertNotIn(self.token, errors)
        self.case.assertNotIn('ambient-not-authority', errors)
        self.case.assertEqual(self.process.returncode, 0, errors)
