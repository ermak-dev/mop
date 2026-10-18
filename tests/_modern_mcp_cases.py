"""#642: native SDK stdio -> собственный HTTPS -> DRF/Token/Access -> PostgreSQL 17.

Private inbox/physical delivery не подменяются fixture успехом: их здесь нет.
SOLUTION: настоящий commandlet и текущий RBAC соединены без старой шины.
RESULT: оба native checks зелёные в выборе из 17 PostgreSQL 17 checks.
STATUS: FIXED — see #642
"""
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading

from django.test import override_settings
from mop.client import control as api
from mop.server.access import services as access
from _control_fixture import own_tls
from _control_client_cases import DjangoTLS
from _rbac_unified_cases import RbacBase


@override_settings(ALLOWED_HOSTS=['localhost', 'testserver'])
class NativeManagement(RbacBase):
    def test_typed_https_catalogue_reads_current_human_and_agent_rbac(self):
        agent, _ = self.agent_actor('mcp-agent', 'catalog.view_project')
        with own_tls(DjangoTLS) as (server, origin, cert, directory):
            server.calls = []; server.drop_post = server.drop_get = False
            server.after = lambda method, response: None
            scope = api.CapabilityScope(project_id=str(self.project.pk))
            for user, token in ((self.human, self.token), (agent, access.issue_token(agent.pk))):
                client = api.Client(api.ServerContext(origin, token, str(cert)))
                current = client.capabilities(scope)
                self.assertEqual(current.identity.user_id, user.pk)
                self.assertEqual(current.identity.action_permissions, frozenset(('catalog.view_project',)))
                self.assertEqual(client.capabilities(api.CapabilityScope()).identity.action_permissions, frozenset())
                user.user_permissions.clear()
                self.assertEqual(client.capabilities(scope).identity.action_permissions, frozenset())

    def test_real_stdio_project_list_and_live_permission_revocation(self):
        root = Path(__file__).resolve().parents[1]
        with own_tls(DjangoTLS) as (server, origin, cert, directory):
            server.calls = []; server.drop_post = server.drop_get = False
            server.after = lambda method, response: None
            env = {**os.environ, 'MOP_API_ORIGIN': origin, 'MOP_API_CA': str(cert), 'MOP_API_TOKEN': self.token,
                'PYTHONPATH': str(root)+os.pathsep+os.environ.get('PYTHONPATH', '')}
            process = subprocess.Popen([sys.executable, '-P', '-m', 'mop.cli', 'mcp',
                '--project-id', str(self.project.pk)], cwd=directory, env=env,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            lines = queue.Queue()
            def reader():
                for line in process.stdout: lines.put(line)
                lines.put(None)
            thread = threading.Thread(target=reader, daemon=True); thread.start()
            def rpc(number, method, params):
                process.stdin.write(json.dumps({'jsonrpc': '2.0', 'id': number, 'method': method, 'params': params})+'\n')
                process.stdin.flush()
                line = lines.get(timeout=30)
                self.assertIsNotNone(line, 'native SDK process exited')
                self.assertNotIn(self.token, line)
                value = json.loads(line); self.assertEqual(value['id'], number)
                self.assertNotIn('error', value, value)
                return value['result']
            try:
                initialized = rpc(1, 'initialize', {'protocolVersion': '2025-11-25', 'capabilities': {},
                    'clientInfo': {'name': 'owned-postgres', 'version': '1'}})
                self.assertFalse(initialized['capabilities']['experimental']['private_inbox']['available'])
                process.stdin.write('{"jsonrpc":"2.0","method":"notifications/initialized"}\n'); process.stdin.flush()
                tools = rpc(2, 'tools/list', {})['tools']
                self.assertIn('project_list', {tool['name'] for tool in tools})
                self.assertNotIn('send', {tool['name'] for tool in tools})
                reply = rpc(3, 'tools/call', {'name': 'project_list', 'arguments': {}})
                self.assertFalse(reply.get('isError', False), reply)
                data = json.loads(reply['content'][0]['text'])
                self.assertEqual([row['id'] for row in data['results']], [str(self.project.pk)])
                self.human.user_permissions.clear()
                self.assertNotIn('project_list', {tool['name'] for tool in rpc(4, 'tools/list', {})['tools']})
                reply = rpc(5, 'tools/call', {'name': 'project_list', 'arguments': {}})
                self.assertTrue(reply['isError'])
                self.assertIn('not authorized', reply['content'][0]['text'])
            finally:
                process.stdin.close()
                try: process.wait(timeout=10)
                except subprocess.TimeoutExpired: process.kill(); process.wait(timeout=5)
                errors = process.stderr.read(); thread.join(3)
                process.stdout.close(); process.stderr.close()
                self.assertNotIn(self.token, errors)
                self.assertEqual(process.returncode, 0, errors)
