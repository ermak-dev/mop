#!/usr/bin/env python3
"""HYPOTHESIS: потребители должны понимать опубликованный DRF, не старый RPC.

SPEC: #625 node eligibility не имеет incarnation; lookup требует epoch;
#633/#649 query/error envelopes имеют schema_version без epoch.
RESULT: сначала проверяется meaningful red отсутствующих consumer bindings.
STATUS: INCOMPLETE — see #642; это не приёмка всей миграции.
"""
import contextlib
import io
import json
import os
import sys
import unittest
from unittest.mock import patch

import hermetic  # noqa: F401
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
from mop import cli
from mop.client import control as api
from control_client import Fixture, Response

KEY = '8e6449b9-65db-4148-aa68-a8b4020bd8b5'
EPOCH = 'cb739f05-87ef-457d-a5a2-e205ed97cf49'
NODE = 'e3089889-7246-4de1-93c3-2e1854151700'
OP = 'a4948f9b-8bad-491a-a0cc-160d4da18828'
DEADLINE = '2026-10-16T10:05:00Z'
TOKEN = 'private-consumer-token'


def node_operation(state='accepted'):
    return {'schema_version': 1, 'epoch': EPOCH, 'operation': {
        'id': OP, 'epoch': EPOCH, 'request_id': KEY, 'state': state, 'revision': '1',
        'target': {'id': NODE, 'kind': 'node', 'revision': '2', 'source_revision': '1'},
        'code': 'admitted', 'reason': 'command admitted', 'resources_held': True, 'evidence': None}}


class Consumers(unittest.TestCase):
    def setUp(self):
        self.fixture = Fixture()
        self.patcher = patch.object(api, '_DeadlineHTTPSConnection', self.fixture.factory)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.client = api.Client(api.ServerContext('https://controller.example', TOKEN))

    def response(self, data, status=200):
        self.fixture.responses.append(Response(status, data))

    def test_node_admission_uses_published_schema_and_get_only_recovery(self):
        self.assertTrue(hasattr(self.client, 'prepare_node_drain'), '#642 missing typed node drain consumer')
        command = self.client.prepare_node_drain(NODE, epoch=EPOCH, expected_revision='1',
            not_after=DEADLINE, request_id=KEY)
        self.assertEqual(self.fixture.calls, [])
        self.response(node_operation(), 202)
        admitted = self.client.submit(command)
        self.assertEqual(admitted.operation.target_id, NODE)
        body = json.loads(self.fixture.calls[-1][2])
        self.assertEqual(body, {'schema_version': 1, 'epoch': EPOCH, 'request_id': KEY,
            'expected_revision': '1', 'not_after': DEADLINE, 'params': {}})
        self.assertEqual(self.fixture.calls[-1][1], f'/api/v1/nodes/{NODE}/commands/drain')
        self.response(node_operation('unknown'))
        result = self.client.lookup_operation(KEY, epoch=EPOCH)
        self.assertEqual(result.state, 'unknown')
        self.assertEqual(self.fixture.calls[-1][1], f'/api/v1/operations?request_id={KEY}&epoch={EPOCH}')
        self.assertEqual(sum(call[0] == 'POST' for call in self.fixture.calls), 1)

    def test_empty_explicit_request_key_is_not_replaced_with_a_new_identity(self):
        with self.assertRaises(api.ClientError):
            self.client.prepare_node_drain(NODE, epoch=EPOCH, expected_revision='1',
                not_after=DEADLINE, request_id='')
        self.assertEqual(self.fixture.calls, [])

    def test_post_database_outcome_unknown_preserves_command_identity(self):
        self.assertTrue(hasattr(self.client, 'prepare_node_drain'), '#642 missing typed node drain consumer')
        command = self.client.prepare_node_drain(NODE, epoch=EPOCH, expected_revision='1',
            not_after=DEADLINE, request_id=KEY)
        self.response({'schema_version': 1, 'error': {'code': 'database_outcome_unknown',
            'message': 'database commit outcome unknown; reconcile before retry', 'retryable': False}}, 503)
        with self.assertRaises(api.UnknownSubmission) as caught:
            self.client.submit(command)
        self.assertIs(caught.exception.command, command)

    def test_cli_unknown_lookup_failure_keeps_key_epoch_and_never_repeats_post(self):
        from mop.cli import _control
        self.fixture.responses.extend((OSError('private low-level detail'),
            Response(404, {'schema_version': 1, 'error': {'code': 'not_found',
                'message': 'resource not found', 'retryable': False}})))
        out, err = io.StringIO(), io.StringIO()
        with _control.use(api.ServerContext('https://controller.example', TOKEN)), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = _control.invoke('node drain', [NODE, '--epoch', EPOCH,
                '--request-id', KEY, '--expected-revision', '1', '--not-after', DEADLINE])
        self.assertEqual(code, 3)
        self.assertIn(KEY, err.getvalue())
        self.assertIn(EPOCH, err.getvalue())
        self.assertIn('unknown', err.getvalue())
        self.assertNotIn('private low-level detail', err.getvalue())
        self.assertEqual([call[0] for call in self.fixture.calls], ['POST', 'GET'])

    def test_lost_result_after_accepted_keeps_admitted_operation_and_request_key(self):
        from mop.cli import _control
        self.response(node_operation(), 202)
        self.fixture.responses.append(OSError('private result read failed'))
        out, err = io.StringIO(), io.StringIO()
        with _control.use(api.ServerContext('https://controller.example', TOKEN)), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = _control.invoke('node drain', [NODE, '--epoch', EPOCH,
                '--request-id', KEY, '--expected-revision', '1', '--not-after', DEADLINE, '--wait', '1'])
        self.assertEqual(code, 3)
        self.assertIn(KEY, err.getvalue())
        self.assertIn(OP, err.getvalue())
        self.assertIn(EPOCH, err.getvalue())
        self.assertEqual([call[0] for call in self.fixture.calls], ['POST', 'GET'])
        self.assertNotIn('private result read failed', err.getvalue())

    def test_wait_is_bound_to_already_admitted_epoch_and_request(self):
        self.response(node_operation())
        admitted = self.client.operation(OP)
        changed = node_operation('succeeded')
        changed['operation']['request_id'] = EPOCH
        self.response(changed)
        with self.assertRaises(api.ProtocolError):
            self.client.wait_operation(OP, initial=admitted)

    def test_cli_accepted_is_not_success_and_recovery_is_get_only(self):
        from mop.cli import _control
        self.response(node_operation(), 202)
        out, err = io.StringIO(), io.StringIO()
        with _control.use(api.ServerContext('https://controller.example', TOKEN)), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = _control.invoke('node up', [NODE, '--epoch', EPOCH,
                '--request-id', KEY, '--expected-revision', '1', '--not-after', DEADLINE])
        self.assertEqual(code, 3)
        self.assertEqual(json.loads(out.getvalue())['operation']['state'], 'accepted')
        self.assertEqual(self.fixture.calls[-1][1], f'/api/v1/nodes/{NODE}/commands/enable')
        self.response(node_operation('succeeded'))
        with _control.use(api.ServerContext('https://controller.example', TOKEN)), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            self.assertEqual(_control.invoke('operation', ['--request-id', KEY, '--epoch', EPOCH]), 0)
        self.assertEqual([call[0] for call in self.fixture.calls], ['POST', 'GET'])

    def test_published_identity_is_typed_and_not_a_live_endpoint_or_permission_list(self):
        self.assertTrue(hasattr(self.client, 'identity'), '#642 missing typed identity consumer')
        self.response({'schema_version': 1, 'user_id': 2, 'principal_id': NODE,
            'kind': 'human', 'human_owner_id': 2, 'node_id': None, 'grant_id': None})
        result = self.client.identity()
        self.assertEqual(result.user_id, 2)
        self.assertEqual(result.kind, 'human')
        self.assertIsNone(result.action_permissions)
        self.assertFalse(hasattr(result, 'address'))
        self.assertNotIn(TOKEN, repr(result))

    def test_published_error_keeps_reason_without_requiring_epoch(self):
        self.response({'schema_version': 1, 'error': {'code': 'access_denied',
            'message': 'node action permission denied', 'retryable': False}}, 403)
        with self.assertRaises(api.APIError) as caught:
            self.client.operation(OP)
        self.assertEqual(caught.exception.error.reason, 'node action permission denied')
        self.assertEqual(caught.exception.error.http_status, 403)

    def test_dispatcher_enters_transient_https_without_legacy_resolution(self):
        from mop.common import context
        from mop.cli import _control
        out, err = io.StringIO(), io.StringIO()
        self.response({'schema_version': 1, 'state': 'ready', 'epoch': EPOCH,
            'effects_enabled': True})
        with patch.object(context, 'here', side_effect=AssertionError('legacy context read')), \
             patch.object(cli, 'environment', side_effect=AssertionError('installation environment read')), \
             _control.use(api.ServerContext('https://controller.example', TOKEN)), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = cli.main(['health'])
            except SystemExit as error:
                code = error.code
        self.assertEqual(code, 0, err.getvalue())
        self.assertEqual(json.loads(out.getvalue())['state'], 'ready')
        self.assertNotIn(TOKEN, out.getvalue()+err.getvalue())
        self.assertEqual([call[:2] for call in self.fixture.calls], [('GET', '/api/v1/health')])

    def test_mcp_never_starts_legacy_channel_or_invents_action_permissions(self):
        from mop.common import context
        from mop.cli import _control
        self.response({'schema_version': 1, 'user_id': 2, 'principal_id': NODE,
            'kind': 'human', 'human_owner_id': 2, 'node_id': None, 'grant_id': None})
        out, err = io.StringIO(), io.StringIO()
        with _control.use(api.ServerContext('https://controller.example', TOKEN)), \
             patch.object(context, 'here', side_effect=AssertionError('legacy MCP started')), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(['mcp', '--check']), 0)
            self.assertIsNone(json.loads(out.getvalue())['action_permissions'])
            self.assertEqual(cli.main(['mcp']), 2)
        self.assertIn('unsupported', err.getvalue())
        self.assertNotIn(TOKEN, out.getvalue()+err.getvalue())
        self.assertEqual([call[0] for call in self.fixture.calls], ['GET'])

    def test_default_project_query_and_offline_diagnose_do_not_read_old_login_store(self):
        from mop.common import context
        from mop.cli import _control
        self.response({'schema_version': 1, 'count': 0, 'results': [], 'next_cursor': None})
        out, err = io.StringIO(), io.StringIO()
        with _control.use(api.ServerContext('https://controller.example', TOKEN)), \
             patch.object(context, 'here', side_effect=AssertionError('old login/config read')), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(['project']), 0)
            self.assertEqual(json.loads(out.getvalue())['results'], [])
            out.seek(0)
            out.truncate()
            self.assertEqual(cli.main(['diagnose']), 0)
            self.assertEqual(json.loads(out.getvalue())['origin'], 'https://controller.example')
        self.assertEqual([call[0] for call in self.fixture.calls], ['GET'])
        self.assertNotIn(TOKEN, out.getvalue()+err.getvalue())

    def test_role_launcher_cannot_fall_back_to_legacy_context_or_ssh(self):
        from mop.common import context
        with patch.object(context, 'here', side_effect=AssertionError('legacy role launch')), \
             contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(['fixture-role']), 2)

    def test_mcp_adapter_refuses_local_commands_before_execution(self):
        from mop.cli import _control
        with patch.object(cli, 'main', side_effect=AssertionError('local command executed')):
            with self.assertRaises(api.ClientError):
                _control.tool_call(['server', 'deploy'], {'args': []}, {})

    def test_invalid_wait_does_not_admit_effect(self):
        from mop.cli import _control
        self.response(node_operation(), 202)
        with _control.use(api.ServerContext('https://controller.example', TOKEN)), \
             contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(_control.invoke('node drain', [NODE, '--epoch', EPOCH,
                '--expected-revision', '1', '--request-id', KEY, '--wait', '-1']), 1)
        self.assertEqual(self.fixture.calls, [])

    def test_cli_never_prints_token_even_in_safe_error_reason(self):
        from mop.cli import _control
        self.response({'schema_version': 1, 'error': {'code': 'authentication_required',
            'message': 'denied '+TOKEN, 'retryable': False}}, 401)
        out, err = io.StringIO(), io.StringIO()
        with _control.use(api.ServerContext('https://controller.example', TOKEN)), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(_control.invoke('health', []), 1)
        self.assertNotIn(TOKEN, out.getvalue()+err.getvalue())
        self.assertIn('denied', err.getvalue())
        response = node_operation()
        response['operation']['reason'] = 'controller reason '+TOKEN
        self.response(response, 202)
        out, err = io.StringIO(), io.StringIO()
        with _control.use(api.ServerContext('https://controller.example', TOKEN)), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(_control.invoke('node drain', [NODE, '--epoch', EPOCH,
                '--expected-revision', '1', '--request-id', KEY, '--not-after', DEADLINE]), 3)
        self.assertNotIn(TOKEN, out.getvalue()+err.getvalue())
        self.assertIn('controller reason', json.loads(out.getvalue())['operation']['reason'])

    def test_help_does_not_become_an_unsupported_role_launch(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(['help']), 0)
        self.assertIn('mop', out.getvalue())
        self.assertEqual(err.getvalue(), '')

    def test_all_management_surfaces_have_explicit_conversion_inventory(self):
        from mop.cli import _control
        required = {'add', 'update', 'restart', 'recycle', 'delete', 'wipe', 'stop',
            'send', 'dispatch', 'list', 'detail', 'model', 'models', 'profile', 'agents',
            'project add', 'project delete', 'project list', 'project limit', 'node',
            'node drain', 'node up', 'node forget', 'secret file', 'secret var', 'build',
            'stat', 'session history', 'session terminal', 'server doctor', 'server gc',
            'server sweep', 'server disk', 'health', 'attach', 'master', 'code', 'landing'}
        self.assertLessEqual(required, set(_control.SURFACES))
        self.assertEqual(set(_control.SURFACES.values())-{'bound', 'unsupported'}, set())

    def test_live_addresses_are_server_endpoint_records_not_user_presence(self):
        self.assertTrue(hasattr(self.client, 'addresses'), '#642 missing #649 presence query')
        endpoint = {'id': NODE, 'session_id': OP, 'address': 'private:'+NODE,
            'project_id': EPOCH, 'external_session_id': 'conversation-1', 'display_name': 'Own session',
            'expires_at': '2026-10-16T10:05:00+00:00', 'state': 'registered', 'live': False}
        self.response({'schema_version': 1, 'addresses': [endpoint]})
        page = self.client.addresses(EPOCH, limit=20)
        self.assertEqual(len(page.items), 1)
        self.assertEqual(page.items[0].address, 'private:'+NODE)
        self.assertFalse(page.items[0].live)
        self.assertEqual(page.items[0].external_session_id, 'conversation-1')
        self.assertEqual(self.fixture.calls[-1][1],
            f'/api/v1/addresses?schema_version=1&project_id={EPOCH}&limit=20')
        from mop.cli import _control
        self.response({'schema_version': 1, 'addresses': [endpoint]})
        out = io.StringIO()
        with _control.use(api.ServerContext('https://controller.example', TOKEN)), contextlib.redirect_stdout(out):
            code = _control.invoke('addresses', [EPOCH, '--limit', '20'])
        self.assertEqual(code, 0)
        self.assertFalse(json.loads(out.getvalue())['addresses'][0]['live'])

    def test_project_pages_preserve_server_cursor_and_never_follow_url(self):
        row = {'id': NODE, 'name': 'fixture', 'origin': 'https://git.example/fixture.git',
            'revision': '9007199254740993', 'puppet_limit': None}
        self.response({'schema_version': 1, 'count': 2, 'next_cursor': 'opaque:next', 'results': [row]})
        page = self.client.projects_page(page_size=1)
        self.assertEqual(page.items[0].revision, row['revision'])
        self.assertEqual(page.next_cursor, 'opaque:next')
        self.response({'schema_version': 1, 'count': 2, 'next_cursor': None, 'results': [row]})
        self.client.projects_page(cursor=page.next_cursor, page_size=1)
        self.assertEqual(self.fixture.calls[-1][1], '/api/v1/projects?page_size=1&cursor=opaque%3Anext')

    def test_bound_commandlets_use_the_same_https_handler_when_called_directly(self):
        from mop.cli.core import profile
        from mop.cli.project import list as projects
        from mop.cli.node import drain
        from mop.cli import _control
        from mop.common import bus
        self.response({'schema_version': 1, 'user_id': 2, 'principal_id': NODE,
            'kind': 'human', 'human_owner_id': 2, 'node_id': None, 'grant_id': None})
        self.response({'schema_version': 1, 'count': 0, 'results': [], 'next_cursor': None})
        self.response(node_operation('succeeded'), 200)
        with _control.use(api.ServerContext('https://controller.example', TOKEN)), \
             patch.object(bus, 'config', side_effect=AssertionError('legacy commandlet authority')), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(profile.main([]), 0)
            self.assertEqual(projects.main([]), 0)
            self.assertEqual(drain.main([NODE, '--epoch', EPOCH, '--expected-revision', '1',
                '--request-id', KEY, '--not-after', DEADLINE]), 0)
        self.assertEqual([call[0] for call in self.fixture.calls], ['GET', 'GET', 'POST'])

    def test_new_mcp_declarations_name_server_actions_not_legacy_verbs(self):
        path = os.path.join(cli.PACKAGE, 'core', 'operation.py')
        declaration = cli.declared(path)
        self.assertEqual(declaration.get('actions'), ['operations.view_operation'])
        self.assertFalse({'cluster_verbs', 'node_verbs', 'dispatch'} & declaration.keys())
        address = cli.declared(os.path.join(cli.PACKAGE, 'core', 'addresses.py'))
        self.assertEqual(address.get('actions'), ['sessions.view_delivery', 'catalog.view_project'])
        self.assertIs(address.get('human_only'), True)

    def test_mcp_action_filter_fails_closed_and_invokes_same_commandlet(self):
        from mop.cli import _control
        identity = api.Identity(1, NODE, 'grant', 1, None, OP, frozenset({'sessions.restart_puppet',
            'operations.override_assignment'}))
        self.assertTrue(hasattr(_control, 'allowed'), '#642 missing server action permission filter')
        self.assertTrue(_control.allowed({'actions': ['sessions.restart_puppet']}, identity))
        self.assertFalse(_control.allowed({'actions': ['catalog.drain_node']}, identity))
        self.assertFalse(_control.allowed({'actions': ['operations.override_assignment']}, identity))
        self.assertFalse(_control.allowed({'cluster_verbs': ['restart']}, identity))
        self.assertFalse(_control.allowed({'actions': ['catalog.drain_node']},
            api.Identity(1, NODE, 'human', 1, None, None)))
        out, err = io.StringIO(), io.StringIO()
        decl = {'args': [{'name': 'cursor', 'type': 'string', 'flag': '--cursor'}]}
        self.response({'schema_version': 1, 'count': 0, 'results': [], 'next_cursor': None})
        with _control.use(api.ServerContext('https://controller.example', TOKEN)), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            result = _control.tool_call(['project', 'list'], decl, {'cursor': 'safe-cursor'})
        self.assertEqual(json.loads(result)['results'], [])
        self.assertEqual(out.getvalue(), '')
        self.assertEqual(err.getvalue(), '')
        self.assertNotIn(TOKEN, result)
        self.assertEqual(self.fixture.calls[-1][1], '/api/v1/projects?page_size=100&cursor=safe-cursor')

    def test_absent_backend_refuses_before_any_io_and_never_ssh_or_nats(self):
        from mop.cli import _control
        for words in ('add', 'update', 'recycle', 'delete', 'wipe', 'stop', 'model',
                      'secret file', 'secret var', 'server gc', 'attach', 'master'):
            out, err = io.StringIO(), io.StringIO()
            with self.subTest(words=words), _control.use(api.ServerContext('https://controller.example', TOKEN)), \
                 contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = _control.invoke(words, [])
                self.assertEqual(code, 2)
                self.assertIn('unsupported', err.getvalue())
                self.assertNotIn('accepted', out.getvalue())
        self.assertEqual(self.fixture.calls, [])


if __name__ == '__main__':
    unittest.main()
