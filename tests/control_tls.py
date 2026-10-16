#!/usr/bin/env python3
"""HYPOTHESIS: transport doubles не доказывают TLS/auth и потерю HTTP ответа.

SOLUTION: собственный HTTPS fixture с отдельным CA, без old pool и credentials.
Он хранит один admission по ключу, теряет ответ POST и отдаёт итог через GET.
Это настоящий TLS/CLI, но не Django/PostgreSQL и не доказательство executor.
STATUS: INCOMPLETE — see #642; production controller acceptance отдельна.
"""
import contextlib
import http.server
import io
import json
import os
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
from urllib.parse import parse_qs, urlsplit

import hermetic  # noqa: F401
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
from mop import cli
from mop.cli import _control
from mop.client import control as api
from control_consumers import KEY, EPOCH, NODE, DEADLINE, TOKEN, node_operation


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *args):
        pass

    def reply(self, status, value):
        raw = json.dumps(value).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Connection', 'close')
        self.end_headers()
        self.wfile.write(raw)
        self.close_connection = True

    def authorized(self):
        self.server.calls.append((self.command, self.path))
        if self.headers.get('Authorization') != 'Token '+TOKEN:
            self.reply(401, {'schema_version': 1, 'error': {'code': 'authentication_required',
                'message': 'authentication required', 'retryable': False}})
            return False
        return True

    def do_GET(self):
        if not self.authorized():
            return
        path = urlsplit(self.path)
        if path.path == '/api/v1/health':
            self.reply(200, {'schema_version': 1, 'epoch': EPOCH, 'state': 'ready', 'effects_enabled': True})
        elif path.path == '/api/v1/usage':
            from usage_query import page
            self.reply(200, page())
        elif path.path == '/api/v1/operations':
            query = parse_qs(path.query)
            if query != {'request_id': [KEY], 'epoch': [EPOCH]}:
                self.reply(409, {'schema_version': 1, 'error': {'code': 'epoch_conflict',
                    'message': 'installation epoch changed', 'retryable': False}})
            elif self.server.admissions:
                self.reply(200, node_operation('succeeded'))
            else:
                self.reply(404, {'schema_version': 1, 'error': {'code': 'not_found',
                    'message': 'resource not found', 'retryable': False}})
        else:
            self.reply(403, {'schema_version': 1, 'error': {'code': 'access_denied',
                'message': 'action permission denied', 'retryable': False}})

    def do_POST(self):
        if not self.authorized():
            return
        raw = self.rfile.read(int(self.headers['Content-Length']))
        body = json.loads(raw)
        expected = {'schema_version': 1, 'epoch': EPOCH, 'request_id': KEY,
                    'expected_revision': '1', 'not_after': DEADLINE, 'params': {}}
        if (self.path != f'/api/v1/nodes/{NODE}/commands/drain' or body != expected):
            self.reply(409, {'schema_version': 1, 'error': {'code': 'request_conflict',
                'message': 'request key is bound to different intent', 'retryable': False}})
            return
        self.server.admissions.append(KEY)
        # COMMIT в модели fixture завершён, ответ потерян. Ни retry, ни второго key.
        self.close_connection = True
        self.connection.shutdown(socket.SHUT_RDWR)
        self.connection.close()


class TLSConsumers(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if shutil.which('openssl') is None:
            raise AssertionError('declared test environment needs openssl for own TLS fixture')
        cls.directory = tempfile.TemporaryDirectory(prefix='mop-642-own-tls-')
        root = Path(cls.directory.name)
        cls.cert, cls.key = root/'ca.pem', root/'key.pem'
        result = subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
            '-keyout', str(cls.key), '-out', str(cls.cert), '-days', '1', '-subj', '/CN=localhost',
            '-addext', 'subjectAltName=DNS:localhost'], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if result.returncode:
            cls.directory.cleanup()
            raise AssertionError('could not create isolated TLS certificate')
        cls.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.server.daemon_threads = True
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(cls.cert, cls.key)
        cls.server.socket = tls.wrap_socket(cls.server.socket, server_side=True)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.origin = 'https://localhost:'+str(cls.server.server_port)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(5)
        cls.directory.cleanup()

    def setUp(self):
        self.server.calls, self.server.admissions = [], []

    def context(self, token=TOKEN, trust=True):
        return api.ServerContext(self.origin, token, str(self.cert) if trust else None)

    def test_real_tls_checks_ca_and_django_token_no_proxy_environment(self):
        with self.assertRaises(api.TransportError):
            api.Client(self.context(trust=False)).health()
        self.assertEqual(self.server.calls, [])
        self.assertEqual(api.Client(self.context()).health().epoch, EPOCH)
        with self.assertRaises(api.APIError) as caught:
            api.Client(self.context(token='wrong-fixture-token')).health()
        self.assertEqual(caught.exception.error.http_status, 401)
        self.assertEqual(caught.exception.error.reason, 'authentication required')
        with self.assertRaises(api.APIError) as caught:
            api.Client(self.context()).operation(NODE)
        self.assertEqual(caught.exception.error.http_status, 403)
        with self.assertRaises(api.ResetRequired) as caught:
            api.Client(self.context()).lookup_operation(KEY, epoch=NODE)
        self.assertEqual(caught.exception.error.http_status, 409)

    def test_cli_real_tls_recovers_lost_accepted_reply_without_second_post(self):
        out, err = io.StringIO(), io.StringIO()
        with _control.use(self.context()), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(['node', 'drain', NODE, '--epoch', EPOCH, '--expected-revision', '1',
                             '--request-id', KEY, '--not-after', DEADLINE])
        self.assertEqual(code, 0, err.getvalue())
        self.assertEqual(json.loads(out.getvalue())['operation']['state'], 'succeeded')
        self.assertEqual(self.server.admissions, [KEY])
        self.assertEqual([method for method, _ in self.server.calls], ['POST', 'GET'])
        self.assertNotIn(TOKEN, out.getvalue()+err.getvalue())

    def test_installed_wheel_client_without_dependencies_or_server_extras(self):
        root = Path(self.directory.name)/'install-fixture'
        root.mkdir()
        source = root/'source'
        source.mkdir()
        checkout = Path(__file__).resolve().parents[1]
        shutil.copytree(checkout/'mop', source/'mop', ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        for name in ('setup.py', 'pyproject.toml'):
            shutil.copyfile(checkout/name, source/name)
        wheels = root/'wheels'
        # Собираем собственный пакет offline; никаких pip install зависимостей.
        build = subprocess.run([sys.executable, '-m', 'pip', 'wheel', '--no-deps', '--no-build-isolation',
            '--no-index', '--wheel-dir', str(wheels), str(source)], cwd=root,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=60)
        self.assertEqual(build.returncode, 0, build.stdout)
        environment = root/'venv'
        create = subprocess.run([sys.executable, '-m', 'venv', '--without-pip', str(environment)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=30)
        self.assertEqual(create.returncode, 0, create.stdout)
        python = environment/'bin'/'python'
        site = subprocess.run([str(python), '-I', '-c', 'import sysconfig; print(sysconfig.get_path("purelib"))'],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10)
        self.assertEqual(site.returncode, 0, site.stderr)
        wheel, = wheels.glob('*.whl')
        install = subprocess.run([sys.executable, '-m', 'pip', 'install', '--no-deps', '--no-index',
            '--target', site.stdout.strip(), str(wheel)], cwd=root,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=30)
        self.assertEqual(install.returncode, 0, install.stdout)
        script = '''import importlib.util, json, sys
from importlib.metadata import distribution
assert importlib.util.find_spec('django') is None
assert importlib.util.find_spec('nats') is None
from mop import cli
from mop.client.control import Client
assert not any(x == 'django' or x.startswith(('django.', 'mop.server.')) for x in sys.modules)
entry, = distribution('mop').entry_points.select(name='mop', group='console_scripts')
assert entry.value == 'mop.cli:console'
assert cli.main(['health']) == 0
import contextlib, io
from mop.cli.core import stat
private = io.StringIO()
with contextlib.redirect_stdout(private):
    assert stat.main(['--start', '2026-10-16', '--end', '2026-10-17']) == 0
result = json.loads(private.getvalue())
assert result['source'] == 'cliproxy' and result['source_complete'] is False
assert result['attribution']['puppet'] == 'unavailable'
assert result['groups'][0]['counters']['cache_read']['value'] is None
assert not any(x == 'django' or x.startswith(('django.', 'mop.server.', 'nats.')) for x in sys.modules)
'''
        env = {key: value for key, value in os.environ.items() if not key.startswith(('MOP_', 'PYTHON'))}
        env.update(MOP_API_ORIGIN=self.origin, MOP_API_TOKEN=TOKEN, MOP_API_CA=str(self.cert))
        run = subprocess.run([str(python), '-I', '-c', script], env=env, cwd=root,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=15)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(json.loads(run.stdout)['state'], 'ready')
        self.assertNotIn(TOKEN, run.stdout+run.stderr)

    def test_package_client_runs_without_site_packages_or_server_imports(self):
        # Установленный wheel проверяется отдельно; этот check честно проверяет
        # stdlib-only import path из source snapshot, не выдаёт его за install.
        script = '''import sys
sys.path.insert(0, sys.argv[1])
from mop import cli
from mop.cli import _control
from mop.client.control import Client, ServerContext
assert not any(x == 'django' or x.startswith(('django.', 'mop.server.')) for x in sys.modules)
assert cli.main(['health']) == 0
'''
        env = {key: value for key, value in os.environ.items() if not key.startswith('MOP_')}
        env.update(MOP_API_ORIGIN=self.origin, MOP_API_TOKEN=TOKEN, MOP_API_CA=str(self.cert))
        run = subprocess.run([sys.executable, '-I', '-S', '-c', script,
            str(Path(__file__).resolve().parents[1])], env=env, cwd=self.directory.name,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=15)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(json.loads(run.stdout)['state'], 'ready')
        self.assertNotIn(TOKEN, run.stdout+run.stderr)


if __name__ == '__main__':
    unittest.main()
