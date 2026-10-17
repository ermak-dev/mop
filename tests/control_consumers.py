#!/usr/bin/env python3
"""HYPOTHESIS: потребители должны понимать опубликованный DRF, не старый RPC.

SPEC: #625 node eligibility не имеет incarnation; lookup требует epoch;
#633/#649 query/error envelopes имеют schema_version без epoch.
RESULT: meaningful red проверил недостающие bindings, потерю key/reason,
неверный recovery target, scope селектора и admission до проверки wait.
STATUS: FIXED — see #642 (только связанные consumers; вся миграция не завершена).

#675: общие конверты/транспортные двойники -- в _control_fixture, захват
вывода -- run_command из tests/_lib: своих диалектов файл не держит.
"""
import io
import json
import os
import sys
import unittest
from unittest.mock import patch

import hermetic  # noqa: F401
from _lib import run_command
from _control_fixture import (Fixture, Response, KEY, EPOCH, NODE, OP, DEADLINE,
    TOKEN, PROJECT, POLICY, PUPPET, INCARNATION, puppet_row, pool_page,
    build_operation, restart_operation, node_operation, page)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
from mop import cli
from mop.client import control as api


class Consumers(unittest.TestCase):
    def setUp(self):
        self.fixture = Fixture()
        self.patcher = patch.object(api, '_DeadlineHTTPSConnection', self.fixture.factory)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.client = api.Client(api.ServerContext('https://controller.example', TOKEN))

    def response(self, data, status=200):
        self.fixture.responses.append(Response(status, data))

    def test_build_uses_published_finite_parser_and_preserves_pinned_policy(self):
        self.assertTrue(hasattr(self.client, 'prepare_build'), '#642 missing published build consumer')
        command = self.client.prepare_build(PROJECT, policy_id=POLICY, epoch=EPOCH,
            expected_revision='5', not_after=DEADLINE, request_id=KEY,
            target_nodes=(NODE,), mode='update')
        self.assertEqual(self.fixture.calls, [])
        from mop.server.builds.commands import parse
        parsed = parse(PROJECT, json.loads(command.body))
        self.assertEqual(str(parsed.policy_id), POLICY)
        self.assertIs(parsed.force_safety, False)
        self.assertEqual(parsed.mode, 'update')
        self.response(build_operation(), 202)
        result = self.client.submit(command)
        self.assertEqual(result.operation.target_id, PROJECT)
        self.assertEqual(result.operation.data['build']['policy_id'], POLICY)
        self.assertEqual(self.fixture.calls[-1][:2], ('POST', f'/api/v1/projects/{PROJECT}/commands/build'))

    def test_build_cli_is_named_admission_with_no_local_manifest_or_force_fallback(self):
        from mop.cli import _control
        self.response(build_operation(), 202)
        args = [PROJECT, '--policy-id', POLICY, '--target-node', NODE, '--epoch', EPOCH,
            '--expected-revision', '5', '--request-id', KEY, '--not-after', DEADLINE, '--mode', 'update']
        with _control.use(api.ServerContext('https://controller.example', TOKEN)):
            out, err, code = run_command(cli.main, ['build', *args])
        self.assertEqual(code, 3, err)
        self.assertEqual(json.loads(out)['operation']['state'], 'accepted')
        body = json.loads(self.fixture.calls[-1][2])
        self.assertEqual(body['target_nodes'], [NODE])
        self.assertEqual(body['policy_id'], POLICY)
        self.assertNotIn('branch', body)
        self.assertNotIn('manifest', body)
        self.assertIs(body['force_safety'], False)
        self.assertNotIn(TOKEN, out+err)
        for option in ('--force', '--force-safety'):
            self.assertEqual(run_command(cli.main, ['build', *args, option])[2], 2)
        self.assertEqual(len(self.fixture.calls), 1)

    def test_restart_cli_uses_canonical_detail_desired_revision_not_projection_clock(self):
        from mop.cli import _control
        self.response({'schema_version': 1, 'epoch': EPOCH, 'puppet': puppet_row()})
        self.response(restart_operation(), 202)
        with _control.use(api.ServerContext('https://controller.example', TOKEN)):
            out, err, code = run_command(cli.main, ['restart', PUPPET, '--request-id', KEY, '--not-after', DEADLINE])
        self.assertEqual(code, 3, err)
        self.assertEqual([call[0] for call in self.fixture.calls], ['GET', 'POST'])
        self.assertEqual(self.fixture.calls[-1][1], f'/api/v1/puppets/{PUPPET}/commands/restart')
        body = json.loads(self.fixture.calls[-1][2])
        self.assertEqual(body['expected_revision'], '7')
        self.assertEqual(body['incarnation'], INCARNATION)
        self.assertEqual(body['params'], {'fresh': False})
        self.assertEqual(body['request_id'], KEY)
        self.assertEqual(json.loads(out)['operation']['state'], 'accepted')

    def test_restart_explicit_binding_needs_no_roster_read_and_never_enables_override(self):
        from mop.cli import _control
        self.response(restart_operation('succeeded'))
        with _control.use(api.ServerContext('https://controller.example', TOKEN)):
            out, err, code = run_command(cli.main, ['restart', PUPPET, '--epoch', EPOCH,
                '--incarnation', INCARNATION, '--expected-revision', '7',
                '--request-id', KEY, '--not-after', DEADLINE])
        self.assertEqual(code, 0, err)
        self.assertEqual([call[0] for call in self.fixture.calls], ['POST'])
        for flag in ('--fresh', '--force'):
            self.assertEqual(run_command(cli.main, ['restart', PUPPET, flag])[2], 2)
        self.assertEqual(len(self.fixture.calls), 1)

    def test_restart_validates_request_identity_before_its_detail_query(self):
        from mop.cli import _control
        with _control.use(api.ServerContext('https://controller.example', TOKEN)):
            self.assertEqual(run_command(cli.main, ['restart', PUPPET, '--request-id', ''])[2], 1)
        self.assertEqual(self.fixture.calls, [])

    def test_public_request_identity_factory_is_transient_and_rejects_replacement(self):
        self.assertTrue(hasattr(api, 'new_request_id'), '#642 missing pre-I/O request identity factory')
        from uuid import UUID
        generated = api.new_request_id()
        self.assertEqual(str(UUID(generated)), generated)
        self.assertEqual(api.new_request_id(KEY), KEY)
        for bad in ('', True, 'not-a-key'):
            with self.assertRaises(api.ClientError):
                api.new_request_id(bad)
        self.assertEqual(self.fixture.calls, [])

    def test_restart_recovery_rejects_a_changed_source_revision_under_the_same_key(self):
        command = self.client.prepare_restart(api.Target(PUPPET, INCARNATION, '7'), epoch=EPOCH,
            not_after=DEADLINE, request_id=KEY)
        response = restart_operation('succeeded')
        response['operation']['target']['source_revision'] = '8'
        self.response(response)
        with self.assertRaises(api.ProtocolError):
            self.client.recover(command)
        self.assertEqual([call[0] for call in self.fixture.calls], ['GET'])

    def test_recovery_rejects_a_different_target_under_the_same_key(self):
        from mop.cli import _control
        self.fixture.responses.append(OSError('lost admission reply'))
        changed = node_operation('succeeded')
        changed['operation']['target']['id'] = PROJECT
        self.response(changed)
        with _control.use(api.ServerContext('https://controller.example', TOKEN)):
            out, err, code = run_command(lambda a: _control.invoke('node drain', a[0]), [[NODE, '--epoch', EPOCH,
                '--request-id', KEY, '--expected-revision', '1', '--not-after', DEADLINE]])
        self.assertEqual(code, 3)
        self.assertIn(KEY, err)
        self.assertIn('unknown', err)
        self.assertEqual(out, '')

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
        with _control.use(api.ServerContext('https://controller.example', TOKEN)):
            out, err, code = run_command(lambda a: _control.invoke('node drain', a[0]), [[NODE, '--epoch', EPOCH,
                '--request-id', KEY, '--expected-revision', '1', '--not-after', DEADLINE]])
        self.assertEqual(code, 3)
        self.assertIn(KEY, err)
        self.assertIn(EPOCH, err)
        self.assertIn('unknown', err)
        self.assertIn('not_found', err)
        self.assertIn('resource not found', err)
        self.assertNotIn('private low-level detail', err)
        self.assertEqual([call[0] for call in self.fixture.calls], ['POST', 'GET'])

    def test_lost_result_after_accepted_keeps_admitted_operation_and_request_key(self):
        from mop.cli import _control
        self.response(node_operation(), 202)
        self.fixture.responses.append(OSError('private result read failed'))
        with _control.use(api.ServerContext('https://controller.example', TOKEN)):
            out, err, code = run_command(lambda a: _control.invoke('node drain', a[0]), [[NODE, '--epoch', EPOCH,
                '--request-id', KEY, '--expected-revision', '1', '--not-after', DEADLINE, '--wait', '1']])
        self.assertEqual(code, 3)
        self.assertIn(KEY, err)
        self.assertIn(OP, err)
        self.assertIn(EPOCH, err)
        self.assertEqual([call[0] for call in self.fixture.calls], ['POST', 'GET'])
        self.assertNotIn('private result read failed', err)

    def test_wait_permission_refusal_keeps_reason_and_original_operation_identity(self):
        from mop.cli import _control
        self.response(node_operation(), 202)
        self.response({'schema_version': 1, 'error': {'code': 'access_denied',
            'message': 'operation scope was revoked', 'retryable': False}}, 403)
        with _control.use(api.ServerContext('https://controller.example', TOKEN)):
            out, err, code = run_command(lambda a: _control.invoke('node drain', a[0]), [[NODE, '--epoch', EPOCH,
                '--request-id', KEY, '--expected-revision', '1', '--not-after', DEADLINE, '--wait', '1']])
        self.assertEqual(code, 1)
        for value in ('operation scope was revoked', KEY, OP, EPOCH):
            self.assertIn(value, err)
        self.assertEqual(out, '')
        self.assertEqual([call[0] for call in self.fixture.calls], ['POST', 'GET'])

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
        with _control.use(api.ServerContext('https://controller.example', TOKEN)):
            out, err, code = run_command(lambda a: _control.invoke('node up', a[0]), [[NODE, '--epoch', EPOCH,
                '--request-id', KEY, '--expected-revision', '1', '--not-after', DEADLINE]])
        self.assertEqual(code, 3)
        self.assertEqual(json.loads(out)['operation']['state'], 'accepted')
        self.assertEqual(self.fixture.calls[-1][1], f'/api/v1/nodes/{NODE}/commands/enable')
        self.response(node_operation('succeeded'))
        with _control.use(api.ServerContext('https://controller.example', TOKEN)):
            self.assertEqual(run_command(lambda a: _control.invoke('operation', a[0]),
                [['--request-id', KEY, '--epoch', EPOCH]])[2], 0)
        self.assertEqual([call[0] for call in self.fixture.calls], ['POST', 'GET'])

    def test_published_identity_is_typed_and_not_a_live_endpoint_or_permission_list(self):
        self.assertTrue(hasattr(self.client, 'identity'), '#642 missing typed identity consumer')
        self.response({'schema_version': 1, 'user_id': 2,
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

    def test_https_context_options_are_only_a_global_prefix_not_command_data(self):
        from mop.cli import _control
        args = ['dev', 'bug', 'new', '--title', '--api-origin=https://not-a-context.example']
        self.assertEqual(_control.options(args), ({}, args))
        args = ['project', 'list', '--cursor', '--api-origin=https://not-a-context.example']
        self.assertEqual(_control.options(args), ({}, args))
        self.assertEqual(_control.options(['--api-origin', 'https://controller.example',
            '--api-ca', '/explicit/ca.pem', 'health']),
            ({'origin': 'https://controller.example', 'ca_file': '/explicit/ca.pem'}, ['health']))

    def test_legacy_server_prefix_cannot_bypass_converted_command_gate(self):
        from mop.common import context
        with patch.object(context, 'here', side_effect=AssertionError('legacy selector read old stores')):
            out, err, code = run_command(cli.main, ['--server', 'old-selector.example', 'list'])
        self.assertEqual(code, 2)
        self.assertIn('unsupported', err)
        self.assertNotIn('old-selector.example', err)
        self.assertEqual(self.fixture.calls, [])

    def test_dispatcher_enters_transient_https_without_legacy_resolution(self):
        from mop.common import context
        from mop.cli import _control
        self.response({'schema_version': 1, 'state': 'ready', 'epoch': EPOCH,
            'effects_enabled': True})
        with patch.object(context, 'here', side_effect=AssertionError('legacy context read')), \
             patch.object(cli, 'environment', side_effect=AssertionError('installation environment read')), \
             _control.use(api.ServerContext('https://controller.example', TOKEN)):
            out, err, code = run_command(cli.main, ['health'])
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)['state'], 'ready')
        self.assertNotIn(TOKEN, out+err)
        self.assertEqual([call[:2] for call in self.fixture.calls], [('GET', '/api/v1/health')])

    def test_user_actions_are_finite_and_never_edit_the_old_operator_store(self):
        from mop.common import context
        for words in ('server user', 'server user add', 'server user delete',
                      'server user passwd', 'server user import'):
            with self.subTest(words=words), \
                 patch.object(context, 'here', side_effect=AssertionError('old operator-store context')):
                out, err, code = run_command(cli.main, words.split())
            self.assertEqual(code, 2)
            self.assertIn('unsupported', err)
            self.assertEqual(out, '')
        self.assertEqual(self.fixture.calls, [])

    def test_mcp_never_starts_legacy_channel_or_invents_action_permissions(self):
        from mop.common import context
        from mop.cli import _control
        self.response({'schema_version': 1, 'user_id': 2,
            'kind': 'human', 'human_owner_id': 2, 'node_id': None, 'grant_id': None})
        with _control.use(api.ServerContext('https://controller.example', TOKEN)), \
             patch.object(context, 'here', side_effect=AssertionError('legacy MCP started')):
            out, err, code = run_command(cli.main, ['mcp', '--check'])
            self.assertEqual(code, 0)
            self.assertIsNone(json.loads(out)['action_permissions'])
            _, refused, code = run_command(cli.main, ['mcp'])
        self.assertEqual(code, 2)
        self.assertIn('unsupported', refused)
        self.assertNotIn(TOKEN, out+err+refused)
        self.assertEqual([call[0] for call in self.fixture.calls], ['GET'])

    def test_default_project_query_and_offline_diagnose_do_not_read_old_login_store(self):
        from mop.common import context
        from mop.cli import _control
        self.response({'schema_version': 1, 'count': 0, 'results': [], 'next_cursor': None})
        with _control.use(api.ServerContext('https://controller.example', TOKEN)), \
             patch.object(context, 'here', side_effect=AssertionError('old login/config read')):
            _, err, code = run_command(cli.main, ['project'])
            self.assertEqual(code, 0)
            out, err2, code = run_command(cli.main, ['diagnose'])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out)['origin'], 'https://controller.example')
        self.assertEqual([call[0] for call in self.fixture.calls], ['GET'])
        self.assertNotIn(TOKEN, out+err+err2)

    def test_role_launcher_cannot_fall_back_to_legacy_context_or_ssh(self):
        from mop.common import context
        with patch.object(context, 'here', side_effect=AssertionError('legacy role launch')):
            self.assertEqual(run_command(cli.main, ['fixture-role'])[2], 2)

    def test_mcp_adapter_refuses_local_commands_before_execution(self):
        from mop.cli import _control
        with patch.object(cli, 'main', side_effect=AssertionError('local command executed')):
            with self.assertRaises(api.ClientError):
                _control.tool_call(['server', 'deploy'], {'args': []}, {})

    def test_invalid_wait_does_not_admit_effect(self):
        from mop.cli import _control
        self.response(node_operation(), 202)
        with _control.use(api.ServerContext('https://controller.example', TOKEN)):
            self.assertEqual(run_command(lambda a: _control.invoke('node drain', a[0]), [[NODE, '--epoch', EPOCH,
                '--expected-revision', '1', '--request-id', KEY, '--wait', '-1']])[2], 1)
        self.assertEqual(self.fixture.calls, [])

    def test_cli_never_prints_token_even_in_safe_error_reason(self):
        from mop.cli import _control
        self.response({'schema_version': 1, 'error': {'code': 'authentication_required',
            'message': 'denied '+TOKEN, 'retryable': False}}, 401)
        with _control.use(api.ServerContext('https://controller.example', TOKEN)):
            out, err, code = run_command(lambda a: _control.invoke('health', a[0]), [[]])
        self.assertEqual(code, 1)
        self.assertNotIn(TOKEN, out+err)
        self.assertIn('denied', err)
        response = node_operation()
        response['operation']['reason'] = 'controller reason '+TOKEN
        self.response(response, 202)
        with _control.use(api.ServerContext('https://controller.example', TOKEN)):
            out, err, code = run_command(lambda a: _control.invoke('node drain', a[0]), [[NODE, '--epoch', EPOCH,
                '--expected-revision', '1', '--request-id', KEY, '--not-after', DEADLINE]])
        self.assertEqual(code, 3)
        self.assertNotIn(TOKEN, out+err)
        self.assertIn('controller reason', json.loads(out)['operation']['reason'])

    def test_help_does_not_become_an_unsupported_role_launch(self):
        out, err, code = run_command(cli.main, ['help'])
        self.assertEqual(code, 0)
        self.assertIn('mop', out)
        self.assertEqual(err, '')

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

    def test_published_pool_query_keeps_projection_and_command_revisions_separate(self):
        self.assertTrue(hasattr(self.client, 'puppets_page'), '#642 missing #626 pool query binding')
        self.assertTrue(hasattr(self.client, 'read_puppet'), '#642 missing typed detail binding')
        self.response(pool_page())
        page = self.client.puppets_page(project_id=PROJECT, page_size=1)
        self.assertEqual(page.items[0].revision, '9007199254740993')
        self.assertEqual(page.items[0].target.revision, '7')
        self.assertEqual(page.items[0].target.incarnation, INCARNATION)
        self.assertFalse(page.coverage['pending_operations'])
        self.assertFalse(page.handshake_complete)
        self.assertNotIn('pending_operations', page.data)
        self.assertEqual(self.fixture.calls[-1][1], f'/api/v1/puppets?page_size=1&project_id={PROJECT}')
        self.response({'schema_version': 1, 'epoch': EPOCH, 'puppet': puppet_row()})
        detail = self.client.read_puppet(PUPPET)
        self.assertEqual(detail.id, PUPPET)
        self.assertIsNone(detail.data['name'])
        self.assertFalse(detail.data['applied']['ready'])
        self.assertEqual(detail.data['observed']['state']['reason'], 'not_observed')
        self.assertEqual(self.fixture.calls[-1][1], f'/api/v1/puppets/{PUPPET}')

    def test_missing_incarnation_and_wrong_detail_never_create_a_command_binding(self):
        self.assertTrue(hasattr(self.client, 'read_puppet'), '#642 missing typed detail binding')
        row = puppet_row()
        row['incarnation'] = None
        self.response({'schema_version': 1, 'epoch': EPOCH, 'puppet': row})
        view = self.client.read_puppet(PUPPET)
        with self.assertRaises(api.ClientError):
            view.target
        row = puppet_row()
        row['id'] = PROJECT
        self.response({'schema_version': 1, 'epoch': EPOCH, 'puppet': row})
        with self.assertRaises(api.ProtocolError):
            self.client.read_puppet(PUPPET)
        self.assertTrue(all(call[0] == 'GET' for call in self.fixture.calls))

    def test_authority_error_nullable_epoch_is_not_replaced_by_a_protocol_error(self):
        self.response({'schema_version': 1, 'epoch': None, 'error': {'code': 'authority_unavailable',
            'message': 'installation read authority is unavailable', 'retryable': True}}, 503)
        with self.assertRaises(api.APIError) as caught:
            self.client.snapshot()
        self.assertEqual(caught.exception.error.reason, 'installation read authority is unavailable')
        self.assertIsNone(caught.exception.error.epoch)

    def test_success_snapshot_without_epoch_has_a_typed_protocol_refusal(self):
        self.response({'schema_version': 1, 'results': [], 'next_cursor': None})
        with self.assertRaises(api.ProtocolError):
            self.client.snapshot()

    def test_list_and_detail_use_canonical_queries_not_a_client_roster(self):
        from mop.cli import _control
        from mop.common import context, puppets
        self.response(pool_page())
        self.response({'schema_version': 1, 'epoch': EPOCH, 'puppet': puppet_row()})
        with _control.use(api.ServerContext('https://controller.example', TOKEN)), \
             patch.object(context, 'here', side_effect=AssertionError('legacy query context')), \
             patch.object(puppets, 'roster', side_effect=AssertionError('client assembled a roster')):
            listed, err, code = run_command(cli.main, ['list', '--project-id', PROJECT])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(listed)['results'][0], puppet_row())
            out, err2, code = run_command(cli.main, ['detail', PUPPET])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out)['puppet'], puppet_row())
        self.assertEqual(len(self.fixture.calls), 2)
        self.assertNotIn(TOKEN, listed+out+err+err2)

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
        with _control.use(api.ServerContext('https://controller.example', TOKEN)):
            out, _, code = run_command(lambda a: _control.invoke('addresses', a[0]), [[EPOCH, '--limit', '20']])
        self.assertEqual(code, 0)
        self.assertFalse(json.loads(out)['addresses'][0]['live'])

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
        self.response({'schema_version': 1, 'user_id': 2,
            'kind': 'human', 'human_owner_id': 2, 'node_id': None, 'grant_id': None})
        self.response({'schema_version': 1, 'count': 0, 'results': [], 'next_cursor': None})
        self.response(node_operation('succeeded'), 200)
        with _control.use(api.ServerContext('https://controller.example', TOKEN)), \
             patch.object(bus, 'config', side_effect=AssertionError('legacy commandlet authority')):
            self.assertEqual(run_command(profile.main, [])[2], 0)
            self.assertEqual(run_command(projects.main, [])[2], 0)
            self.assertEqual(run_command(drain.main, [NODE, '--epoch', EPOCH,
                '--expected-revision', '1', '--request-id', KEY, '--not-after', DEADLINE])[2], 0)
        self.assertEqual([call[0] for call in self.fixture.calls], ['GET', 'GET', 'POST'])

    def test_new_mcp_declarations_name_server_actions_not_legacy_verbs(self):
        path = os.path.join(cli.PACKAGE, 'core', 'operation.py')
        declaration = cli.declared(path)
        self.assertEqual(declaration.get('actions'), ['operations.view_operation'])
        self.assertFalse({'cluster_verbs', 'node_verbs', 'dispatch'} & declaration.keys())
        address = cli.declared(os.path.join(cli.PACKAGE, 'core', 'addresses.py'))
        self.assertEqual(address.get('actions'), ['execution.view_delivery', 'catalog.view_project'])
        self.assertIs(address.get('human_only'), True)
        drain = cli.declared(os.path.join(cli.PACKAGE, 'node', 'drain.py'))
        self.assertEqual(drain.get('actions'), ['catalog.drain_node', 'operations.view_operation'])
        build = cli.declared(os.path.join(cli.PACKAGE, 'core', 'build.py'))
        self.assertEqual(build.get('actions'), ['catalog.build_project', 'operations.view_operation'])
        self.assertIs(build.get('human_only'), True)

    def test_mutating_declaration_cannot_claim_an_empty_action_requirement(self):
        with patch('builtins.open', return_value=io.StringIO(
                'MCP = {"annotations": "destructive", "actions": [], "args": []}')):
            with self.assertRaises(ValueError):
                cli.declared('own-declaration-fixture.py')

    def test_mcp_action_filter_fails_closed_and_invokes_same_commandlet(self):
        from mop.cli import _control
        identity = api.Identity(1, 'service', 1, None, OP, frozenset({'execution.restart_puppet',
            'operations.override_assignment'}))
        self.assertTrue(hasattr(_control, 'allowed'), '#642 missing server action permission filter')
        self.assertTrue(_control.allowed({'actions': ['execution.restart_puppet']}, identity))
        self.assertFalse(_control.allowed({'actions': ['catalog.drain_node']}, identity))
        self.assertFalse(_control.allowed({'actions': ['operations.override_assignment']}, identity))
        self.assertFalse(_control.allowed({'cluster_verbs': ['restart']}, identity))
        self.assertFalse(_control.allowed({'actions': ['catalog.drain_node']},
            api.Identity(1, 'human', 1, None, None)))
        scoped = api.Identity(1, 'human', 1, None, None, frozenset())
        self.assertTrue(_control.allowed({'annotations': 'readonly', 'actions': []}, scoped))
        self.assertFalse(_control.allowed({'annotations': 'destructive', 'actions': []}, scoped))
        self.assertFalse(_control.allowed({'annotations': 'readonly'}, scoped))
        decl = {'args': [{'name': 'cursor', 'type': 'string', 'flag': '--cursor'}]}
        self.response({'schema_version': 1, 'count': 0, 'results': [], 'next_cursor': None})
        def mcp_tool(argv):
            return _control.tool_call(['project', 'list'], decl, {'cursor': 'safe-cursor'})
        with _control.use(api.ServerContext('https://controller.example', TOKEN)):
            out, err, result = run_command(mcp_tool, [])
        self.assertEqual(json.loads(result)['results'], [])
        self.assertEqual(out, '')
        self.assertEqual(err, '')
        self.assertNotIn(TOKEN, result)
        self.assertEqual(self.fixture.calls[-1][1], '/api/v1/projects?page_size=100&cursor=safe-cursor')

    def test_proxy_stat_uses_published_usage_parser_without_transcript_attribution(self):
        from mop.cli import _control
        self.response(page())
        with _control.use(api.ServerContext('https://controller.example', TOKEN)):
            out, err, code = run_command(cli.main, ['stat', '--start', '2026-10-16', '--end', '2026-10-17'])
        self.assertEqual(code, 0, err)
        result = json.loads(out)
        self.assertEqual(result['source'], 'cliproxy')
        self.assertFalse(result['source_complete'])
        self.assertEqual(result['attribution'], {'user': 'unavailable', 'project': 'unavailable', 'puppet': 'unavailable'})
        self.assertEqual(result['groups'][0]['counters']['total']['value'], '41')
        self.assertIsNone(result['groups'][0]['counters']['cache_read']['value'])
        self.assertEqual(self.fixture.calls[-1][:2], ('GET',
            '/api/v1/usage?schema_version=1&start=2026-10-16&end=2026-10-17&limit=100&after='))
        self.assertNotIn(TOKEN, out+err)

    def test_proxy_stat_unavailable_breakdown_refuses_before_any_query(self):
        from mop.cli import _control
        for args in (['--users'], ['--puppets'], ['--group-by', 'project']):
            out, err, code = run_command(cli.main, ['stat', *args])
            self.assertEqual(code, 2)
            self.assertIn('unavailable', err)
            self.assertIn('proxy', err)
            self.assertEqual(out, '')
        self.assertEqual(self.fixture.calls, [])

    def test_absent_backend_refuses_before_any_io_and_never_ssh_or_nats(self):
        from mop.cli import _control
        for words in ('add', 'update', 'recycle', 'delete', 'wipe', 'stop', 'model',
                      'secret file', 'secret var', 'server gc', 'attach', 'master'):
            with self.subTest(words=words), _control.use(api.ServerContext('https://controller.example', TOKEN)):
                out, err, code = run_command(lambda a: _control.invoke(words, a[0]), [[]])
                self.assertEqual(code, 2)
                self.assertIn('unsupported', err)
                self.assertNotIn('accepted', out)
        self.assertEqual(self.fixture.calls, [])


if __name__ == '__main__':
    unittest.main()
