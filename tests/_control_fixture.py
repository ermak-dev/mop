"""Общие fixture'ы контрольного HTTPS-клиента (#675): транспортные двойники,
опубликованные конверты и собственный TLS listener.

Не проверка: `mop dev test` и hermetic пропускают `_*.py`. До #675 эти
fixture'ы жили в самих проверках (control_client, control_consumers,
control_tls, usage_query), и проверка импортировала проверку -- tests_lib
это запрещает (#269). Общее живёт здесь и не тянет назад в проверки:
хелпер не импортирует ни одну проверку и не трогает mop на импорте.
"""
import contextlib
import http.server
import json
import shutil
import ssl
import subprocess
import socket
import tempfile
import threading
from pathlib import Path

# Маркеры приватности транспортного двойника: проверка control_client
# сверяет свои диагностики именно с этими строками (assertNotIn), поэтому
# значение живёт здесь одним источником, а проверка импортирует его.
TRANSPORT_TOKEN = "private-test-token"
TRANSPORT_MESSAGE = "private-message-never-in-diagnostics"


class Response:
    def __init__(self, status=200, data=None, raw=None, headers=None):
        self.status = status
        self.body = raw if raw is not None else json.dumps(data or {}).encode()
        self.headers = {"Content-Type": "application/json", **(headers or {})}
        self.closed = False
        self.read_sizes = []

    def getheader(self, name, default=None):
        return next((v for k, v in self.headers.items() if k.lower() == name.lower()), default)

    def read1(self, size):
        self.read_sizes.append(size)
        part, self.body = self.body[:size], self.body[size:]
        return part

    def close(self):
        self.closed = True


class Socket:
    def settimeout(self, value):
        self.timeout = value


class Connection:
    def __init__(self, fixture, *args, **kwargs):
        self.fixture, self.args, self.kwargs = fixture, args, kwargs
        self.closed = False
        self.sock = Socket()
        self.debuglevel = 1
        fixture.connections.append(self)

    def connect(self):
        if self.fixture.connect_failure:
            raise OSError(TRANSPORT_TOKEN)

    def request(self, method, path, body=None, headers=None):
        self.fixture.calls.append((method, path, body, headers))
        if self.fixture.send_failure:
            raise OSError(TRANSPORT_TOKEN + TRANSPORT_MESSAGE)

    def getresponse(self):
        value = self.fixture.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    def close(self):
        self.closed = True


class Fixture:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls, self.connections = [], []
        self.connect_failure = self.send_failure = False

    def factory(self, *args, **kwargs):
        return Connection(self, *args, **kwargs)


# ── опубликованные конверты control_consumers ────────────────────────────
KEY = '8e6449b9-65db-4148-aa68-a8b4020bd8b5'
EPOCH = 'cb739f05-87ef-457d-a5a2-e205ed97cf49'
NODE = 'e3089889-7246-4de1-93c3-2e1854151700'
OP = 'a4948f9b-8bad-491a-a0cc-160d4da18828'
DEADLINE = '2026-10-16T10:05:00Z'
TOKEN = 'private-consumer-token'
PROJECT = '5ee23d6c-04a4-43ae-9b91-409485c535fc'
POLICY = 'b5600b0d-15cc-4fa3-ab0e-7c59d7e98939'
PUPPET = '9db85243-4ae1-4511-8acc-bf6c8140cf2c'
INCARNATION = 'f6db1446-0358-4dfd-8605-85ec12367ebc'
DEFINITION = '24807b60-72aa-44d1-985d-7073f4053dbb'


def create_operation(state='accepted'):
    value = restart_operation(state)
    value['operation']['target']['revision'] = '1'
    value['operation']['target']['source_revision'] = '1'
    value['operation']['steps'] = [{'name': name, 'outcome': '', 'evidence': ''}
        for name in ('scheduler_register', 'apply_identity', 'confirm_ready', 'activate_grant')]
    return value


def puppet_row():
    unavailable = {'value': None, 'freshness': 'unavailable', 'reason': 'not_observed',
        'sequence': None, 'producer_epoch': None, 'incarnation': None,
        'observed_at': None, 'received_at': None, 'expires_at': None}
    return {'kind': 'puppet', 'id': PUPPET, 'incarnation': INCARNATION,
        'revision': '9007199254740993', 'deleted': False, 'name': None, 'aliases': [],
        'project_id': PROJECT, 'project_name': 'owned', 'node_id': NODE, 'node_name': 'fixture-node',
        'desired': {'revision': '7', 'role': 'worker', 'bundle_digest': 'a'*64,
            'generation': '2', 'owner_id': '2', 'expires_at': DEADLINE},
        'applied': {'identity': False, 'ready': False},
        'observed': {kind: dict(unavailable) for kind in ('state', 'clone', 'size')},
        'state': {'kind': 'unknown', 'detail': 'no current session observation', 'branch': None},
        'node': {'desired': {'eligible': True}, 'applied': {'applied_revision': None, 'at': None},
                 'observed': {'eligible': None, 'at': None, 'freshness': 'unverified',
                              'reason': 'source_version_unavailable'}},
        'usage': {'source': 'proxy', 'freshness': 'unavailable', 'reason': 'usage_query_not_bound'}}


def pool_page():
    return {'schema_version': 1, 'epoch': EPOCH, 'snapshot_id': OP,
        'cursor': 'opaque-event-position', 'next_cursor': None, 'results': [puppet_row()],
        'coverage': {'catalog': True, 'assignments': True, 'observations': True,
            'operation_publication': False, 'pending_operations': False,
            'endpoints': False, 'telemetry': False, 'usage': False}, 'handshake_complete': False}


def build_operation(state='accepted'):
    value = node_operation(state)
    value['operation']['target'] = {'id': PROJECT, 'kind': 'project', 'source_revision': '5'}
    value['operation']['build'] = {'policy_id': POLICY, 'source_commit': 'c'*40, 'source_ref': 'main',
        'manifest_digest': 'a'*64, 'source_digest': 'b'*64, 'mode': 'update', 'target_nodes': [NODE],
        'planned': False, 'skipped': False, 'affected_targets': [], 'unresolved_stages': [],
        'stages': [], 'original_failure': None, 'compensation_failures': []}
    return value


def restart_operation(state='accepted'):
    value = node_operation(state)
    value['operation']['target'] = {'id': PUPPET, 'kind': 'puppet', 'incarnation': INCARNATION,
                                  'revision': '7', 'source_revision': '7'}
    value['operation']['steps'] = [{'name': name, 'outcome': '', 'evidence': ''}
        for name in ('apply_identity', 'restart_session', 'confirm_ready')]
    return value


def node_operation(state='accepted'):
    return {'schema_version': 1, 'epoch': EPOCH, 'operation': {
        'id': OP, 'epoch': EPOCH, 'request_id': KEY, 'state': state, 'revision': '1',
        'target': {'id': NODE, 'kind': 'node', 'revision': '2', 'source_revision': '1'},
        'code': 'admitted', 'reason': 'command admitted', 'resources_held': True, 'evidence': None}}


# ── опубликованная страница usage (#656) ─────────────────────────────────
def page():
    return {'schema_version': 1, 'source': 'cliproxy', 'source_complete': False,
        'period': {'start': '2026-10-16', 'end': '2026-10-17', 'timezone': 'UTC', 'end_exclusive': True},
        'attribution': {'user': 'unavailable', 'project': 'unavailable', 'puppet': 'unavailable'},
        'sources': [], 'next': None,
        'items': [{'id': '0195305d-152b-42c0-80c6-edbce57a4ef4', 'source_id': '9195305d-152b-42c0-80c6-edbce57a4ef4',
            'date': '2026-10-16', 'key_id': None, 'provider': 'provider', 'model': 'model', 'alias': 'alias',
            'requests': 1, 'failed_requests': 0, 'partial_events': 1,
            'counters': {name: {'value': '41' if name == 'total' else None,
                'known_events': 1 if name == 'total' else 0, 'unknown_events': 0 if name == 'total' else 1}
                for name in ('total', 'unclassified', 'input', 'input_uncached', 'cache_read', 'cache_write',
                             'output', 'output_non_reasoning', 'reasoning')}}]}


# ─── собственный TLS listener (#642/#675) ────────────────────────────────
@contextlib.contextmanager
def own_tls(handler):
    """Один TLS fixture для stdlib и настоящего Django/PostgreSQL клиента."""
    if shutil.which('openssl') is None:
        raise AssertionError('declared test environment needs openssl for own TLS fixture')
    with tempfile.TemporaryDirectory(prefix='mop-642-own-tls-') as directory:
        root = Path(directory)
        cert, key = root/'ca.pem', root/'key.pem'
        result = subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
            '-keyout', str(key), '-out', str(cert), '-days', '1', '-subj', '/CN=localhost',
            '-addext', 'subjectAltName=DNS:localhost'], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if result.returncode:
            raise AssertionError('could not create isolated TLS certificate')
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler)
        server.daemon_threads = True
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(cert, key)
        server.socket = tls.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server, 'https://localhost:'+str(server.server_port), cert, directory
        finally:
            server.shutdown()
            server.server_close()
            thread.join(5)
            if thread.is_alive():
                raise AssertionError('own TLS fixture did not shut down')
