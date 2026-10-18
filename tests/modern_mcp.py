"""#642: native stdio management по явному authenticated catalogue.

HYPOTHESIS: неподключённый startup и старый kind filter исключают canonical agents.
SPEC: finite typed scope, refresh до вызова, общий dispatcher, inbox отсутствует.
SOLUTION: SDK публикует только bound declarations; catalogue — не источник grants.
RESULT: исходный RED подтвердил отказ startup/agent; 12 strict checks зелёные.
Дополнительный RED: safe codec reason терялся в loud; узкая граница его сохраняет.
STATUS: FIXED — see #642
"""
import http.server
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch

import hermetic  # noqa: F401
from _control_fixture import (Fixture, Response, PROJECT, NODE, EPOCH, TOKEN, KEY, own_tls)
from _lib import run_command
from mop.client import control as api
from mop.cli import _control

ROOT = Path(__file__).resolve().parents[1]


def catalogue(actions=('catalog.view_pool',), kind='human'):
    return {'schema_version': 1, 'catalogue_version': 1, 'epoch': EPOCH,
        'actor': {'user_id': 7, 'kind': kind, 'human_owner_id': 7, 'node_id': None, 'grant_id': None},
        'scope': {'project_id': PROJECT, 'node_id': None, 'client_id': None}, 'actions': list(actions)}


class CatalogueClient(unittest.TestCase):
    def setUp(self):
        self.fixture = Fixture()
        self.patcher = patch.object(api, '_DeadlineHTTPSConnection', self.fixture.factory)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.client = api.Client(api.ServerContext('https://controller.example', TOKEN))

    def query(self, data):
        self.assertTrue(hasattr(api, 'CapabilityScope'), 'typed explicit catalogue scope is missing')
        self.assertTrue(hasattr(self.client, 'capabilities'), 'authenticated catalogue parser is missing')
        self.fixture.responses.append(Response(data=data))
        return self.client.capabilities(api.CapabilityScope(project_id=PROJECT))

    def test_typed_scoped_catalogue_and_exact_query(self):
        result = self.query(catalogue())
        self.assertEqual(result.epoch, EPOCH)
        self.assertEqual(result.identity.action_permissions, frozenset(('catalog.view_pool',)))
        self.assertEqual(result.scope.project_id, PROJECT)
        self.assertEqual(self.fixture.calls[-1][1], '/api/v1/capabilities?schema_version=1&project_id='+PROJECT)
        self.assertNotIn(TOKEN, repr(result))

    def test_unknown_versions_actions_context_and_duplicates_fail_closed(self):
        variants = []
        for key, value in (('catalogue_version', 2), ('catalogue_version', True), ('schema_version', 2),
                ('actions', ['catalog.future']), ('actions', ['catalog.view_pool']*2),
                ('actions', ['execution.restart_puppet', 'catalog.view_pool']), ('actions', 'catalog.view_pool'),
                ('epoch', 'not-uuid'), ('extra', True)):
            data = catalogue(); data[key] = value; variants.append(data)
        data = catalogue(); data['scope']['project_id'] = NODE; variants.append(data)
        data = catalogue(); data['actor']['kind'] = 'service'; data['actor']['grant_id'] = KEY; variants.append(data)
        data = catalogue(); del data['actor']['user_id']; variants.append(data)
        for data in variants:
            with self.subTest(data=data):
                with self.assertRaises(api.ProtocolError):
                    self.query(data)

    def test_canonical_actor_filter_not_kind_permission_grants(self):
        declaration = {'actions': ['execution.send_message']}
        permissions = frozenset(declaration['actions'])
        ordinary = api.Identity(7, 'agent', 7, None, None, permissions)
        delegated = api.Identity(7, 'agent', 4, NODE, KEY, permissions)
        service = api.Identity(7, 'service', None, NODE, None, permissions)
        legacy = api.Identity(7, 'service', 4, NODE, KEY, permissions)
        for identity in (ordinary, delegated, service):
            self.assertTrue(_control.allowed(declaration, identity), identity)
            self.assertFalse(_control.allowed({'actions': ['execution.delete_puppet']}, identity))
        self.assertFalse(_control.allowed(declaration, legacy))
        self.assertFalse(_control.allowed(declaration, api.Identity(7, 'human', 7, None, None)))
        self.assertFalse(_control.allowed({'actions': []}, ordinary))
        self.assertFalse(_control.allowed({**declaration, 'human_only': True}, delegated))


class ManagementContracts(unittest.TestCase):
    def test_only_business_contracts_are_human_only(self):
        from mop import cli
        declarations = {name: cli.declared(path) for name, words, path in cli.tool_commands(cli.scan(), cli.verbs())
            if _control.SURFACES.get(' '.join(words)) == 'bound'}
        for name in ('build', 'stat', 'node_drain', 'node_up'):
            self.assertIsNotNone(declarations[name])
            self.assertFalse(declarations[name].get('human_only', False), name)
        for name in ('add', 'addresses'):
            self.assertTrue(declarations[name]['human_only'], name)

    def test_discovery_imports_no_legacy_runtime_or_role_claims(self):
        script = '''import sys
from mop.cli import _mcp
_mcp.commandlets()
for name in ('mop.common.bus', 'mop.client.channel', 'mop.common.context',
             'mop.common.soul', 'mop.common.busnames', 'mop.session'):
    assert name not in sys.modules, name
'''
        result = subprocess.run([sys.executable, '-P', '-c', script], cwd=ROOT,
            capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, '')

    def test_finite_wire_vocabulary_matches_public_canonical_schema(self):
        from mop.client.action_catalogue import ACTIONS_V1
        import yaml
        document = yaml.safe_load((ROOT/'docs/API.schema.yaml').read_text())
        self.assertEqual(ACTIONS_V1, frozenset(document['components']['schemas']['ActionsEnum']['enum']))

    def test_loud_redacts_credentials_even_from_local_client_refusal(self):
        from mop.cli import _mcp
        fixture = Fixture(Response(data=catalogue()), Response(data=catalogue()))
        with patch.object(api, '_DeadlineHTTPSConnection', fixture.factory):
            manager = _mcp.Management(api.ServerContext('https://controller.example', TOKEN),
                api.CapabilityScope(project_id=PROJECT))
            with patch.object(_control, 'tool_call', side_effect=api.ClientError(TOKEN)):
                reply = manager.call('list', {})
        self.assertTrue(reply.model_dump(by_alias=True)['isError'])
        self.assertNotIn(TOKEN, reply.content[0].text)

    def test_safe_argument_codec_refusal_keeps_its_reason_without_private_value(self):
        from mop.cli import _mcp
        data = catalogue(('execution.restart_puppet', 'operations.view_operation'))
        fixture = Fixture(Response(data=data), Response(data=data))
        with patch.object(api, '_DeadlineHTTPSConnection', fixture.factory):
            manager = _mcp.Management(api.ServerContext('https://controller.example', TOKEN),
                api.CapabilityScope(project_id=PROJECT))
            with patch('mop.cli.main', side_effect=AssertionError('must not execute')):
                reply = manager.call('restart', {'puppet_id': '-'+TOKEN, 'epoch': EPOCH,
                    'incarnation': KEY, 'expected_revision': '1'})
        self.assertTrue(reply.model_dump(by_alias=True)['isError'])
        self.assertIn('puppet_id: value would read as a flag', reply.content[0].text)
        self.assertNotIn(TOKEN, reply.content[0].text)

    def test_epoch_or_actor_change_requires_restart_not_new_authority(self):
        from mop.cli import _mcp
        for field in ('epoch', 'actor'):
            changed = catalogue()
            if field == 'epoch': changed['epoch'] = KEY
            else: changed['actor']['user_id'] = changed['actor']['human_owner_id'] = 8
            fixture = Fixture(Response(data=catalogue()), Response(data=changed))
            with patch.object(api, '_DeadlineHTTPSConnection', fixture.factory), \
                    patch.object(_control, 'tool_call', side_effect=AssertionError('must not execute')):
                manager = _mcp.Management(api.ServerContext('https://controller.example', TOKEN),
                    api.CapabilityScope(project_id=PROJECT))
                reply = manager.call('list', {})
                self.assertTrue(reply.model_dump(by_alias=True)['isError'])
                self.assertIn('changed', reply.content[0].text)

    def test_tool_dispatch_status_context_scope_and_private_input(self):
        from mop.cli import _mcp
        fixture = Fixture(*[Response(data=catalogue()) for _ in range(5)])
        context = api.ServerContext('https://controller.example', TOKEN)
        with patch.object(api, '_DeadlineHTTPSConnection', fixture.factory):
            manager = _mcp.Management(context, api.CapabilityScope(project_id=PROJECT))
            def command(argv):
                self.assertEqual(_control.context(), context)
                print(TOKEN)
                return 1
            # Тот же dispatcher получает argv, не параллельный HTTP adapter.
            with patch('mop.cli.main', side_effect=command) as dispatch:
                reply = manager.call('list', {})
                self.assertTrue(reply.model_dump(by_alias=True)['isError'], reply)
                self.assertNotIn(TOKEN, reply.content[0].text)
                self.assertEqual(dispatch.call_args.args[0], ['list', '--project-id', PROJECT])
            with patch.object(_control, 'tool_call', side_effect=AssertionError('must not execute')):
                reply = manager.call('list', {'project_id': NODE})
                self.assertTrue(reply.model_dump(by_alias=True)['isError'])
                self.assertIn('scope differs', reply.content[0].text)
                reply = manager.call('list', {'unexpected': TOKEN})
                self.assertTrue(reply.model_dump(by_alias=True)['isError'])
                self.assertNotIn(TOKEN, reply.content[0].text)


class NativeStdio(unittest.TestCase):
    def test_real_initialize_list_call_refresh_safe_errors_and_no_inbox(self):
        class Handler(http.server.BaseHTTPRequestHandler):
            actions = ['catalog.view_project']
            requests = []
            def log_message(self, *args): pass
            def do_GET(self):
                type(self).requests.append(self.path)
                if self.headers.get('Authorization') != 'Token '+TOKEN:
                    self.send_error(403); return
                if self.path.startswith('/api/v1/capabilities?'):
                    data = catalogue(tuple(type(self).actions))
                elif self.path == '/api/v1/health':
                    data = {'schema_version': 1, 'epoch': EPOCH, 'state': 'ready', 'effects_enabled': False}
                elif self.path.startswith('/api/v1/projects?'):
                    data = {'schema_version': 1, 'count': 0, 'next_cursor': None, 'results': []}
                else:
                    data = {'schema_version': 1, 'error': {'code': 'denied', 'message': TOKEN, 'retryable': False}}
                    self.send_response(403); self.send_header('Content-Type', 'application/json')
                    raw = json.dumps(data).encode(); self.send_header('Content-Length', str(len(raw)))
                    self.end_headers(); self.wfile.write(raw); return
                raw = json.dumps(data).encode(); self.send_response(200)
                self.send_header('Content-Type', 'application/json'); self.send_header('Cache-Control', 'no-store')
                self.send_header('Content-Length', str(len(raw))); self.end_headers(); self.wfile.write(raw)
        with own_tls(Handler) as (_, origin, cert, directory):
            env = {**os.environ, 'MOP_API_ORIGIN': origin, 'MOP_API_CA': str(cert), 'MOP_API_TOKEN': TOKEN,
                'PYTHONPATH': str(ROOT)+os.pathsep+os.environ.get('PYTHONPATH', '')}
            process = subprocess.Popen([sys.executable, '-P', '-m', 'mop.cli', 'mcp', '--project-id', PROJECT],
                cwd=directory, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True)
            lines = queue.Queue()
            def reader():
                for line in process.stdout:
                    lines.put(line)
                lines.put(None)
            thread = threading.Thread(target=reader, daemon=True); thread.start()
            def rpc(number, method, params=None):
                process.stdin.write(json.dumps({'jsonrpc': '2.0', 'id': number, 'method': method,
                    'params': params or {}})+'\n'); process.stdin.flush()
                try: line = lines.get(timeout=20)
                except queue.Empty: self.fail('native MCP did not answer')
                self.assertIsNotNone(line, 'native MCP exited before response')
                reply = json.loads(line)
                self.assertEqual(reply.get('id'), number)
                self.assertNotIn(TOKEN, line)
                return reply
            try:
                init = rpc(1, 'initialize', {'protocolVersion': '2025-11-25',
                    'capabilities': {}, 'clientInfo': {'name': 'owned-fixture', 'version': '1'}})
                self.assertNotIn('error', init, init)
                self.assertIn('private inbox unavailable', init['result']['instructions'])
                self.assertEqual(init['result']['capabilities']['experimental']['private_inbox']['available'], False)
                process.stdin.write('{"jsonrpc":"2.0","method":"notifications/initialized"}\n'); process.stdin.flush()
                tools = rpc(2, 'tools/list')['result']['tools']
                names = {tool['name'] for tool in tools}
                self.assertIn('health', names)
                self.assertIn('project_list', names)
                self.assertNotIn('send', names)
                self.assertFalse(names & {'agents', 'report', 'idle', 'dispatch', 'session_terminal', 'update'})
                result = rpc(3, 'tools/call', {'name': 'health', 'arguments': {}})['result']
                self.assertIn('ready', result['content'][0]['text'])
                Handler.actions = []
                result = rpc(4, 'tools/call', {'name': 'project_list', 'arguments': {}})['result']
                self.assertTrue(result['isError'])
                self.assertIn('not authorized', result['content'][0]['text'])
                self.assertFalse(any(path.startswith('/api/v1/projects') for path in Handler.requests))
                result = rpc(5, 'tools/call', {'name': 'send', 'arguments': {'message': TOKEN}})['result']
                self.assertTrue(result['isError'])
                self.assertGreaterEqual(sum('/capabilities?' in path for path in Handler.requests), 4)
            finally:
                process.stdin.close()
                try: process.wait(timeout=10)
                except subprocess.TimeoutExpired: process.kill(); process.wait(timeout=5)
                errors = process.stderr.read(); thread.join(3)
                process.stdout.close(); process.stderr.close()
                self.assertNotIn(TOKEN, errors)
                self.assertEqual(process.returncode, 0, errors)

    def test_check_explicitly_reports_unavailable_private_inbox(self):
        fixture = Fixture(Response(data=catalogue()))
        with patch.object(api, '_DeadlineHTTPSConnection', fixture.factory), \
                _control.use(api.ServerContext('https://controller.example', TOKEN)):
            out, err, code = run_command(lambda argv: _control.invoke('mcp', argv), ['--check', '--project-id', PROJECT])
        self.assertEqual(code, 0, err)
        body = json.loads(out)
        self.assertEqual(body['private_inbox']['available'], False)
        self.assertEqual(body['private_inbox']['reason'], 'not_bound')
        self.assertEqual(body['scope']['project_id'], PROJECT)
        self.assertNotIn(TOKEN, out+err)


if __name__ == '__main__':
    unittest.main()
