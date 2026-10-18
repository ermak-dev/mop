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


class BoundedRefusalInput(io.StringIO):
    def read(self, size=-1):
        raise AssertionError('unsupported/invalid routing must not read stdin')


class Consumers(unittest.TestCase):
    def setUp(self):
        self.fixture = Fixture()
        self.patcher = patch.object(api, '_DeadlineHTTPSConnection', self.fixture.factory)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.client = api.Client(api.ServerContext('https://controller.example', TOKEN))

    def response(self, data, status=200):
        self.fixture.responses.append(Response(status, data))

    def create_args(self):
        from _control_fixture import DEFINITION
        return [PROJECT, '--node-id', NODE, '--definition-id', DEFINITION, '--epoch', EPOCH,
            '--request-id', KEY, '--not-after', DEADLINE]

    # HYPOTHESIS: add всё ещё unsupported, а прямой вызов импортирует старую шину.
    # SOLUTION: один transient HTTPS handler для CLI и MCP, без legacy role/origin.
    # RESULT: unsupported/missing-actions red сменился green; invalid ввод не читает context.
    # STATUS: FIXED — see #642
    def test_create_uses_published_parser_without_preexisting_target(self):
        from _control_fixture import DEFINITION, create_operation
        self.assertTrue(hasattr(self.client, 'prepare_create'), '#642 missing published create consumer')
        command = self.client.prepare_create(PROJECT, node_id=NODE, definition_id=DEFINITION,
            epoch=EPOCH, request_id=KEY, not_after=DEADLINE)
        from mop.server.operations.commands import parse_create
        parsed = parse_create(PROJECT, json.loads(command.body))
        self.assertEqual(str(parsed.intent.project_id), PROJECT)
        self.assertEqual(str(parsed.node_id), NODE)
        self.assertEqual(str(parsed.definition_id), DEFINITION)
        self.assertEqual(self.fixture.calls, [])
        self.response(create_operation(), 202)
        self.assertEqual(self.client.submit(command).operation.target_id, PUPPET)

    def test_add_cli_and_direct_commandlet_use_transient_context_only(self):
        from _control_fixture import DEFINITION, create_operation
        from mop.cli import _control
        from mop.cli.core import add
        from mop.common import context
        for entry, args in ((cli.main, ['add', *self.create_args()]), (add.main, self.create_args())):
            self.response(create_operation(), 202)
            with _control.use(api.ServerContext('https://controller.example', TOKEN)), \
                 patch.object(context, 'here', side_effect=AssertionError('legacy context read')), \
                 patch.object(cli, 'environment', side_effect=AssertionError('installation environment read')):
                out, err, code = run_command(entry, args)
            self.assertEqual(code, 3, err)
            self.assertEqual(json.loads(out)['operation']['state'], 'accepted')
            body = json.loads(self.fixture.calls[-1][2])
            self.assertEqual(body, {'schema_version': 1, 'epoch': EPOCH, 'request_id': KEY,
                'not_after': DEADLINE, 'params': {'node_id': NODE, 'definition_id': DEFINITION}})
            self.assertNotIn(TOKEN, out+err)
        self.assertEqual([call[0] for call in self.fixture.calls], ['POST', 'POST'])

    def test_add_invalid_or_legacy_inputs_refuse_before_context_or_io(self):
        from mop.cli import _control
        args = self.create_args()
        bad_inputs = [[], [PROJECT], ['git@example:repo.git', '--role', 'worker'],
            [*args, '--force'], [*args, '--origin', 'git@example:repo.git'],
            [*args, '--shell', 'whoami'], [*args, '--role', 'worker'],
            [*args, '--incarnation', INCARNATION], [*args, '--expected-revision', '1'],
            [*args, '--wait', '-1'], [*args, '--wait', 'nan']]
        for option in ('--node-id', '--definition-id', '--epoch', '--request-id', '--not-after'):
            changed = list(args)
            changed[changed.index(option)+1] = ''
            bad_inputs.append(changed)
        bad_inputs.append(['not-a-uuid', *args[1:]])
        with patch.object(_control, 'context', side_effect=AssertionError('context before validation')):
            for argv in bad_inputs:
                with self.subTest(argv=argv):
                    out, err, code = run_command(cli.main, ['add', *argv])
                    self.assertIn(code, (1, 2))
                    self.assertEqual(out, '')
        self.assertEqual(self.fixture.calls, [])

    def test_add_pins_generated_key_and_deadline_before_context_io(self):
        from _control_fixture import DEFINITION, create_operation
        from mop.cli import _control
        self.assertTrue(hasattr(api, 'CreateCommand'), '#642 missing immutable create intent')
        original = api.CreateCommand
        order = []
        class CheckedCreate(original):
            def __post_init__(self):
                super().__post_init__()
                order.append(self)
        def context(*args):
            self.assertEqual(len(order), 1, 'intent must be pinned before context/TLS I/O')
            return api.ServerContext('https://controller.example', TOKEN)
        def factory(*args, **kwargs):
            reply = create_operation()
            reply['operation']['request_id'] = order[0].request_id
            self.response(reply, 202)
            return self.fixture.factory(*args, **kwargs)
        with patch.object(api, 'CreateCommand', CheckedCreate), \
             patch.object(_control, 'context', side_effect=context), \
             patch.object(api, '_DeadlineHTTPSConnection', side_effect=factory):
            out, err, code = run_command(cli.main, ['add', PROJECT, '--node-id', NODE,
                '--definition-id', DEFINITION, '--epoch', EPOCH])
        self.assertEqual(code, 3, err)
        self.assertEqual(json.loads(self.fixture.calls[-1][2])['request_id'], order[0].request_id)
        self.assertEqual(json.loads(self.fixture.calls[-1][2])['not_after'], order[0].not_after)

    def test_add_unknown_recovers_by_key_and_epoch_without_blind_post(self):
        from _control_fixture import create_operation
        from mop.cli import _control
        self.fixture.responses.append(OSError('lost private admission reply'))
        self.response(create_operation('succeeded'))
        with _control.use(api.ServerContext('https://controller.example', TOKEN)):
            out, err, code = run_command(cli.main, ['add', *self.create_args()])
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)['operation']['target']['id'], PUPPET)
        self.assertEqual([call[0] for call in self.fixture.calls], ['POST', 'GET'])
        self.assertEqual(self.fixture.calls[-1][1], f'/api/v1/operations?request_id={KEY}&epoch={EPOCH}')
        self.fixture.responses.extend((OSError('lost private admission reply'), Response(404,
            {'schema_version': 1, 'error': {'code': 'not_found', 'message': 'resource not found', 'retryable': False}})))
        with _control.use(api.ServerContext('https://controller.example', TOKEN)):
            out, err, code = run_command(cli.main, ['add', *self.create_args()])
        self.assertEqual(code, 3)
        self.assertEqual(out, '')
        for value in (KEY, EPOCH, 'unknown', 'resource not found'):
            self.assertIn(value, err)
        self.assertNotIn('private admission reply', err)
        self.assertEqual([call[0] for call in self.fixture.calls], ['POST', 'GET', 'POST', 'GET'])

    def test_add_mcp_requires_exact_canonical_human_actions_and_same_handler(self):
        from _control_fixture import DEFINITION, create_operation
        from mop.cli import _control
        declaration = cli.declared(os.path.join(cli.PACKAGE, 'core', 'add.py'))
        actions = ['execution.create_puppet', 'execution.assign_work', 'operations.view_operation']
        self.assertEqual(declaration.get('actions'), actions)
        self.assertIs(declaration.get('human_only'), True)
        self.assertFalse({'node_verbs', 'cluster_verbs', 'dispatch'} & declaration.keys())
        for missing in actions:
            self.assertFalse(_control.allowed(declaration, api.Identity(1, 'human', 1, None, None,
                frozenset(set(actions)-{missing}))))
        self.assertTrue(_control.allowed(declaration, api.Identity(1, 'human', 1, None, None, frozenset(actions))))
        self.assertFalse(_control.allowed(declaration, api.Identity(1, 'service', 1, None, OP, frozenset(actions))))
        self.response(create_operation(), 202)
        with _control.use(api.ServerContext('https://controller.example', TOKEN)):
            result = _control.tool_call(['add'], declaration, {'project_id': PROJECT, 'node_id': NODE,
                'definition_id': DEFINITION, 'epoch': EPOCH, 'request_id': KEY, 'not_after': DEADLINE})
        self.assertEqual(json.loads(result)['operation']['state'], 'accepted')
        self.assertEqual(len(self.fixture.calls), 1)

    def send_args(self, text='private-send-input'):
        return [PUPPET, text, '--epoch', EPOCH, '--incarnation', INCARNATION,
            '--expected-revision', '7', '--request-id', KEY, '--not-after', DEADLINE]

    # HYPOTHESIS: send остаётся unsupported, прямой commandlet открывает legacy bus.
    # SOLUTION: один id-only HTTPS consumer; text живёт только в transient POST.
    # RESULT: unsupported/missing-MCP red -> green; native delivery не подменяется.
    # STATUS: FIXED — see #642
    def test_send_client_body_is_the_published_finite_server_command(self):
        from mop.server.operations.commands import parse_send
        command = self.client.prepare_send(api.Target(PUPPET, INCARNATION, '7'), epoch=EPOCH,
            request_id=KEY, not_after=DEADLINE, message='private-send-input', priority='later', wait_seconds=2)
        parsed = parse_send(json.loads(command.body))
        self.assertEqual(str(parsed.intent.target_id), PUPPET)
        self.assertEqual(parsed.intent.expected_revision, 7)
        self.assertEqual(parsed.message, 'private-send-input')
        self.assertEqual(parsed.priority.value, 'later')
        self.assertEqual(parsed.wait_seconds, 2)
        self.assertFalse(parsed.notify_when_idle)
        self.assertNotIn('private-send-input', repr(command))
        self.assertNotIn('private-send-input', repr(parsed))

    def test_send_cli_detail_and_direct_explicit_binding_use_no_legacy_context(self):
        from _control_fixture import send_operation
        from mop.cli import _control
        from mop.cli.core import send
        from mop.common import context
        self.response({'schema_version': 1, 'epoch': EPOCH, 'puppet': puppet_row()})
        self.response(send_operation(), 202)
        self.response(send_operation(), 202)
        with _control.use(api.ServerContext('https://controller.example', TOKEN)), \
             patch.object(context, 'here', side_effect=AssertionError('legacy context read')), \
             patch.object(cli, 'environment', side_effect=AssertionError('installation environment read')):
            out, err, code = run_command(cli.main, ['send', PUPPET, 'private-send-input',
                '--request-id', KEY, '--not-after', DEADLINE, '--priority', 'later', '--wait-seconds', '2'])
            self.assertEqual(code, 3, err)
            self.assertEqual(json.loads(out)['operation']['state'], 'accepted')
            out2, err2, code = run_command(send.main, self.send_args())
        self.assertEqual(code, 3, err2)
        self.assertEqual([call[0] for call in self.fixture.calls], ['GET', 'POST', 'POST'])
        self.assertEqual(self.fixture.calls[0][1], '/api/v1/puppets/'+PUPPET)
        body = json.loads(self.fixture.calls[1][2])
        self.assertEqual(body, {'schema_version': 1, 'epoch': EPOCH, 'request_id': KEY,
            'target_id': PUPPET, 'incarnation': INCARNATION, 'expected_revision': '7',
            'not_after': DEADLINE, 'params': {'message': 'private-send-input', 'priority': 'later',
                'wait_seconds': 2, 'notify_when_idle': False}})
        for method, path, _, _ in self.fixture.calls:
            self.assertNotIn('private-send-input', path)
        for private in (TOKEN, 'private-send-input'):
            self.assertNotIn(private, out+out2+err+err2)

    def test_send_fixes_generated_key_deadline_and_text_before_context_and_detail(self):
        from _control_fixture import send_operation
        from mop.cli import _control
        from datetime import datetime, timezone
        order = []
        class Clock(datetime):
            calls = 0
            @classmethod
            def now(cls, tz=None):
                cls.calls += 1
                return datetime(2026, 10, 18, 12, tzinfo=timezone.utc)
        original = api.new_request_id
        def key(value=None):
            result = original(value)
            order.append(result)
            return result
        def context(*args):
            self.assertEqual(len(order), 1, 'key must be pinned before context I/O')
            self.assertEqual(Clock.calls, 1, 'deadline must be fixed before context I/O')
            return api.ServerContext('https://controller.example', TOKEN)
        def detail(client, puppet_id):
            self.assertEqual(len(order), 1)
            view = api._puppet_view(api._freeze(puppet_row()), EPOCH)
            return view
        def submit(client, command):
            self.assertEqual(command.request_id, order[0])
            self.assertEqual(command.not_after, '2026-10-18T12:02:00+00:00')
            self.assertEqual(Clock.calls, 1)
            self.assertEqual(command.message, 'private-send-input')
            reply = send_operation()
            reply['operation']['request_id'] = order[0]
            return api.Submission(202, api.Client._read_operation(api._freeze(reply)))
        with patch.object(api, 'new_request_id', side_effect=key), \
             patch.object(_control, 'datetime', Clock), \
             patch.object(_control, 'context', side_effect=context), \
             patch.object(api.Client, 'read_puppet', autospec=True, side_effect=detail), \
             patch.object(api.Client, 'submit', autospec=True, side_effect=submit):
            out, err, code = run_command(cli.main, ['send', PUPPET, 'private-send-input'])
        self.assertEqual(code, 3, err)

    def test_send_stdin_is_bounded_preserved_and_validated_before_context(self):
        from _control_fixture import send_operation
        from mop.cli import _control
        class BoundedInput(io.StringIO):
            def read(self, size=-1):
                self.assert_size(size)
                return super().read(size)
            def assert_size(self, size):
                if not 0 < size <= 16385:
                    raise AssertionError('stdin must use a bounded read')
        text = 'private\nmessage\n'
        self.response(send_operation(), 202)
        with _control.use(api.ServerContext('https://controller.example', TOKEN)), \
             patch.object(sys, 'stdin', BoundedInput(text)):
            out, err, code = run_command(cli.main, ['send', *self.send_args('-')])
        self.assertEqual(code, 3, err)
        self.assertEqual(json.loads(self.fixture.calls[-1][2])['params']['message'], text)
        with patch.object(_control, 'context', side_effect=AssertionError('context before validation')):
            for text in ('', '  \n', 'x' * 16385, 'я' * 8193, 'private\x00input', '\ud800'):
                with patch.object(sys, 'stdin', BoundedInput(text)):
                    out, err, code = run_command(cli.main, ['send', *self.send_args('-')])
                self.assertEqual(code, 1, err)
                self.assertNotIn('private\x00input', err)
                self.assertEqual(out, '')
        self.assertEqual(len(self.fixture.calls), 1)

    def test_send_refuses_local_routing_idle_and_invalid_bindings_before_any_io(self):
        from mop.cli import _control
        unsupported = [['--list'], ['list'], [*self.send_args(), '--force'],
            [*self.send_args(), '--mode', 'bypass'], [*self.send_args(), '--notify-when-idle'],
            [*self.send_args(), '--local'], [*self.send_args(), '--reply-to', 'private:other']]
        bad = [['some-session', 'private-send-input'], ['/tmp/inbox.sock', 'private-send-input'],
            ['pu-fixture-1', 'private-send-input'], ['1234', 'private-send-input'],
            [PUPPET, 'private-send-input', '--epoch', EPOCH],
            [*self.send_args(), '--wait-seconds', '-1'], [*self.send_args(), '--wait-seconds', '61'],
            [*self.send_args(), '--wait', 'nan'], [*self.send_args(), '--wait', 'inf']]
        for option in ('--request-id', '--not-after', '--incarnation', '--epoch', '--expected-revision'):
            args = self.send_args()
            args[args.index(option)+1] = 'invalid-private-value'
            bad.append(args)
        with patch.object(_control, 'context', side_effect=AssertionError('context before refusal')), \
             patch.object(sys, 'stdin', BoundedRefusalInput()):
            for args in unsupported:
                out, err, code = run_command(cli.main, ['send', *args])
                self.assertEqual(code, 2, err)
                self.assertIn('unsupported', err)
                self.assertEqual(out, '')
            for args in bad:
                out, err, code = run_command(cli.main, ['send', *args])
                self.assertEqual(code, 1, err)
                self.assertNotIn('invalid-private-value', err)
                self.assertEqual(out, '')
        self.assertEqual(self.fixture.calls, [])

    # HYPOTHESIS: invalid wait читает stdin, а marker -- не отделяет text от options.
    # SOLUTION: wait проверяется до stdin, marker -- завершает options scan.
    # RESULT: два meaningful red -> green, все 62 consumer checks проходят.
    # STATUS: FIXED — see #642
    def test_send_invalid_wait_does_not_consume_stdin(self):
        from mop.cli import _control
        with patch.object(sys, 'stdin', BoundedRefusalInput()), \
             patch.object(_control, 'context', side_effect=AssertionError('context before validation')):
            for value in ('nan', 'inf', '-1', '0', '61'):
                out, err, code = run_command(cli.main, ['send', *self.send_args('-'), '--wait', value])
                self.assertEqual(code, 1, err)
        self.assertEqual(self.fixture.calls, [])

    def test_send_literal_text_after_double_dash_is_not_a_routing_option(self):
        from _control_fixture import send_operation
        from mop.cli import _control
        self.response(send_operation(), 202)
        with _control.use(api.ServerContext('https://controller.example', TOKEN)):
            out, err, code = run_command(cli.main, ['send', *self.send_args()[2:], '--', PUPPET, '--mode'])
        self.assertEqual(code, 3, err)
        self.assertEqual(json.loads(self.fixture.calls[0][2])['params']['message'], '--mode')

    def test_send_unknown_is_get_only_even_if_lookup_missing_or_changes_binding(self):
        from _control_fixture import send_operation
        from mop.cli import _control
        replies = [send_operation('succeeded'),
            {'schema_version': 1, 'error': {'code': 'not_found', 'message': 'resource not found', 'retryable': False}},
            send_operation('succeeded')]
        replies[2]['operation']['target']['source_revision'] = '8'
        for index, reply in enumerate(replies):
            self.fixture.responses.append(OSError('lost private-send-input reply'))
            self.response(reply, 404 if index == 1 else 200)
            with _control.use(api.ServerContext('https://controller.example', TOKEN)):
                out, err, code = run_command(cli.main, ['send', *self.send_args()])
            self.assertEqual(code, 0 if index == 0 else 3, err)
            if index:
                self.assertEqual(out, '')
                self.assertIn(KEY, err)
                self.assertIn(EPOCH, err)
                self.assertIn('unknown', err)
            self.assertNotIn('private-send-input', out+err)
        self.assertEqual([call[0] for call in self.fixture.calls], ['POST', 'GET']*3)
        self.assertTrue(all(call[1] == f'/api/v1/operations?request_id={KEY}&epoch={EPOCH}'
                            for call in self.fixture.calls if call[0] == 'GET'))

    def test_send_operation_wait_timeout_is_not_idle_or_delivery_evidence(self):
        from _control_fixture import send_operation
        from mop.cli import _control
        self.response(send_operation(), 202)
        last = api.Client._read_operation(api._freeze(send_operation('running')))
        with _control.use(api.ServerContext('https://controller.example', TOKEN)), \
             patch.object(api.Client, 'wait_operation', side_effect=api.WaitTimeout(last)) as wait:
            out, err, code = run_command(cli.main, ['send', *self.send_args(), '--wait', '1'])
        self.assertEqual(code, 3)
        self.assertEqual(json.loads(out)['operation']['state'], 'running')
        self.assertIn('not cancelled', err)
        self.assertNotIn('delivered', out+err)
        self.assertNotIn('idle', out+err)
        wait.assert_called_once()
        self.assertEqual([call[0] for call in self.fixture.calls], ['POST'])

    def test_send_mcp_uses_canonical_permissions_full_binding_and_same_handler(self):
        from _control_fixture import send_operation
        from mop.cli import _control
        declaration = cli.declared(os.path.join(cli.PACKAGE, 'core', 'send.py'))
        actions = ['execution.send_message', 'operations.view_operation']
        self.assertIsNotNone(declaration, '#642 send has no canonical MCP declaration')
        self.assertEqual(declaration.get('actions'), actions)
        self.assertFalse({'node_verbs', 'cluster_verbs', 'dispatch'} & declaration.keys())
        # HYPOTHESIS: старый service+grant искажал форму настоящего Actor.
        # SOLUTION: delegated agent имеет owner+grant, service — ни того, ни другого;
        # node nullable у обоих (#720), kind сам не выдаёт ни одного действия.
        # RESULT: исходный RED заменён canonical positive и каждым missing-action negative.
        # STATUS: FIXED — see #642
        bindings = [(1, 'human', 1, None, None), (2, 'agent', 2, None, None)]
        for node in (None, NODE):
            bindings.extend(((2, 'agent', 1, node, OP), (3, 'service', None, node, None)))
        for binding in bindings:
            with self.subTest(binding=binding):
                identity = api.Identity(*binding, frozenset(actions))
                self.assertTrue(api.canonical_identity(identity))
                self.assertTrue(_control.allowed(declaration, identity))
                for missing in actions:
                    self.assertFalse(_control.allowed(declaration,
                        api.Identity(*binding, frozenset(set(actions)-{missing}))))
                self.assertFalse(_control.allowed(declaration, api.Identity(*binding)))
        legacy = api.Identity(3, 'service', 1, None, OP, frozenset(actions))
        self.assertFalse(api.canonical_identity(legacy))
        self.assertFalse(_control.allowed(declaration, legacy))
        required = {arg['name'] for arg in declaration['args'] if arg.get('required')}
        self.assertLessEqual({'puppet_id', 'message', 'epoch', 'incarnation', 'expected_revision'}, required)
        self.response(send_operation(), 202)
        with _control.use(api.ServerContext('https://controller.example', TOKEN)):
            result = _control.tool_call(['send'], declaration, {'puppet_id': PUPPET,
                'message': 'private-send-input', 'epoch': EPOCH, 'incarnation': INCARNATION,
                'expected_revision': '7', 'request_id': KEY, 'not_after': DEADLINE})
        self.assertEqual(json.loads(result)['operation']['state'], 'accepted')
        self.assertNotIn('private-send-input', result)
        self.assertEqual([call[0] for call in self.fixture.calls], ['POST'])

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

    # #642: wipe и stop-job -- тот же путь, что restart: detail-чтение даёт
    # desired revision, POST на свой маршрут с пустыми params, без force.
    def test_wipe_and_stop_cli_post_empty_params_to_their_routes(self):
        from mop.cli import _control
        for words, route in (('wipe', 'wipe'), ('stop', 'stop-job'), ('delete', 'delete')):
            self.fixture.calls.clear()
            self.response({'schema_version': 1, 'epoch': EPOCH, 'puppet': puppet_row()})
            self.response(restart_operation(), 202)
            with _control.use(api.ServerContext('https://controller.example', TOKEN)):
                out, err, code = run_command(cli.main, [words, PUPPET, '--request-id', KEY,
                                                        '--not-after', DEADLINE])
            self.assertEqual(code, 3, err)
            self.assertEqual([call[0] for call in self.fixture.calls], ['GET', 'POST'])
            self.assertEqual(self.fixture.calls[-1][1], f'/api/v1/puppets/{PUPPET}/commands/{route}')
            body = json.loads(self.fixture.calls[-1][2])
            self.assertEqual(body['params'], {})
            self.assertEqual(body['expected_revision'], '7')
            self.assertEqual(json.loads(out)['operation']['state'], 'accepted')
            self.assertEqual(run_command(cli.main, [words, PUPPET, '--force'])[2], 2)

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
        from mop.common import bus, context
        from mop.cli import _control
        # HYPOTHESIS: identity-only ответ ошибочно принимался за каталог полномочий.
        # SOLUTION: --check читает строгий v1 capabilities с explicit scope/actions;
        # stdio без каталога отказывает до чтения протокола, а не как unsupported.
        # RESULT: исходный RED; native initialize/list/call остаются в modern_mcp.
        # STATUS: FIXED — see #642
        actor = {'user_id': 2, 'kind': 'human', 'human_owner_id': 2,
                 'node_id': None, 'grant_id': None}
        scope = {'project_id': PROJECT, 'node_id': None, 'client_id': None}
        catalogue = {'schema_version': 1, 'catalogue_version': 1, 'epoch': EPOCH,
                     'actor': actor, 'scope': scope, 'actions': ['catalog.view_pool']}
        with _control.use(api.ServerContext('https://controller.example', TOKEN)), \
             patch.object(context, 'here', side_effect=AssertionError('legacy MCP started')), \
             patch.object(bus, 'config', side_effect=AssertionError('legacy bus read')), \
             patch.object(cli, 'environment', side_effect=AssertionError('installation environment read')), \
             patch.object(sys, 'stdin', BoundedRefusalInput()):
            self.response(catalogue)
            out, err, code = run_command(cli.main, ['mcp', '--check', '--project-id', PROJECT])
            self.assertEqual(code, 0, err)
            self.assertEqual(json.loads(out), {'schema_version': 1, 'catalogue_version': 1,
                'epoch': EPOCH, 'actor': {**actor, 'action_permissions': ['catalog.view_pool']},
                'scope': scope, 'private_inbox': {'available': False, 'reason': 'not_bound', 'issue': 649}})
            self.assertNotIn(TOKEN, out+err)
            self.response({**catalogue, 'scope': dict.fromkeys(scope), 'actions': []})
            out, err, code = run_command(cli.main, ['mcp', '--check'])
            self.assertEqual(code, 0, err)
            self.assertEqual(json.loads(out)['actor']['action_permissions'], [])
            self.assertNotIn(TOKEN, out+err)
            for args in (['--check'], []):
                self.response({'schema_version': 1, **actor})
                with patch('mcp.server.stdio.stdio_server',
                        side_effect=AssertionError('protocol entered without catalogue')):
                    out, refused, code = run_command(cli.main, ['mcp', *args])
                self.assertEqual(code, 1, refused)
                self.assertEqual(out, '')
                self.assertIn('invalid action catalogue response', refused)
                self.assertNotIn('unsupported', refused)
                self.assertNotIn(TOKEN, out+refused)
            invalid_catalogues = [{key: value for key, value in catalogue.items() if key != 'actions'},
                {**catalogue, 'catalogue_version': 2}, {**catalogue, 'schema_version': 2},
                {**catalogue, 'actions': ['catalog.future']}]
            for invalid in invalid_catalogues:
                self.response(invalid)
                out, refused, code = run_command(cli.main, ['mcp', '--check', '--project-id', PROJECT])
                self.assertEqual(code, 1, refused)
                self.assertEqual(out, '')
                self.assertNotIn(TOKEN, out+refused)
        scoped_query = ('GET', '/api/v1/capabilities?schema_version=1&project_id='+PROJECT)
        self.assertEqual([call[:2] for call in self.fixture.calls], [scoped_query] +
            [('GET', '/api/v1/capabilities?schema_version=1')]*3 + [scoped_query]*4)

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

    # HYPOTHESIS: без MOP_API_ORIGIN/MOP_API_TOKEN любая удалённая команда, в
    # том числе mop diagnose, отвечает глухим «invalid control client input» --
    # причина (чего нет) теряется, а diagnose падает ровно там, где должен
    # объяснять.
    # SOLUTION: контекст называет недостающие переменные; diagnose без контекста
    # -- информационный not-ready с rc 0, как empty у прежней сводки (#536).
    def test_missing_context_is_named_and_diagnose_reports_not_ready(self):
        clean = {k: v for k, v in os.environ.items() if not k.startswith('MOP_API_')}
        with patch.dict(os.environ, clean, clear=True):
            out, err, code = run_command(cli.main, ['diagnose'])
            self.assertEqual(code, 0, err)
            got = json.loads(out)
            self.assertIs(got['ready'], False)
            self.assertEqual(got['missing'], ['MOP_API_ORIGIN', 'MOP_API_TOKEN'])
            self.assertIs(got['network_observed'], False)
            _, err, code = run_command(cli.main, ['health'])
            self.assertEqual(code, 1)
            self.assertIn('MOP_API_ORIGIN', err)
            self.assertIn('MOP_API_TOKEN', err)
            self.assertNotIn('invalid control client input', err)
            with patch.dict(os.environ, {'MOP_API_TOKEN': TOKEN}):
                out, err, code = run_command(cli.main, ['diagnose'])
                self.assertEqual(json.loads(out)['missing'], ['MOP_API_ORIGIN'])
                self.assertNotIn(TOKEN, out + err)
        self.assertEqual(self.fixture.calls, [])

    # HYPOTHESIS (#713): recursive proxykey leaf не находился в SURFACES и
    # падал обратно в legacy context вместо отказа неподключённой операции.
    # SOLUTION: вся recursive группа связана с конечным server proxykey surface.
    # RESULT: RED3 context trap; GREEN consumer/client/layers/helpers4/4.
    # STATUS: FIXED — see #713
    def test_nested_proxykey_commands_refuse_before_legacy_context(self):
        from mop.common import context
        for leaf in ('start', 'resume', 'status'):
            with self.subTest(leaf=leaf), patch.object(context, 'here',
                    side_effect=AssertionError('legacy rotation context reached')):
                out, err, code = run_command(cli.main, ['server', 'proxykey', leaf])
                self.assertEqual(code, 2, (out, err))
                self.assertIn('unsupported', err)
        self.assertEqual(self.fixture.calls, [])

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
        actions = ['catalog.build_project', 'operations.view_operation']
        self.assertEqual(build.get('actions'), actions)
        # HYPOTHESIS: build получил старый human-only fence вместо общего RBAC.
        # SOLUTION: Builds.submit/_authorize проверяют эти же два действия;
        # human-only остаётся бизнес-контрактом addresses/add, не видом authority.
        # RESULT: исходный RED; каждый Actor требует всю конъюнкцию действий.
        # STATUS: FIXED — see #642
        from mop.cli import _control
        self.assertFalse(build.get('human_only', False))
        self.assertFalse({'cluster_verbs', 'node_verbs', 'dispatch'} & build.keys())
        bindings = [(1, 'human', 1, None, None), (2, 'agent', 2, None, None)]
        for node in (None, NODE):
            bindings.extend(((2, 'agent', 1, node, OP), (3, 'service', None, node, None)))
        for binding in bindings:
            with self.subTest(binding=binding):
                self.assertTrue(_control.allowed(build, api.Identity(*binding, frozenset(actions))))
                self.assertFalse(_control.allowed(build, api.Identity(*binding)))
                for missing in actions:
                    self.assertFalse(_control.allowed(build,
                        api.Identity(*binding, frozenset(set(actions)-{missing}))))
                permissions = frozenset(address['actions'])
                self.assertEqual(_control.allowed(address, api.Identity(*binding, permissions)),
                    binding[1] == 'human')
        self.assertFalse(_control.allowed(build,
            api.Identity(3, 'service', 1, None, OP, frozenset(actions))))

    def test_mutating_declaration_cannot_claim_an_empty_action_requirement(self):
        with patch('builtins.open', return_value=io.StringIO(
                'MCP = {"annotations": "destructive", "actions": [], "args": []}')):
            with self.assertRaises(ValueError):
                cli.declared('own-declaration-fixture.py')

    def test_mcp_action_filter_fails_closed_and_invokes_same_commandlet(self):
        from mop.cli import _control
        # HYPOTHESIS: service+grant и actor-kind whitelist подменяли explicit RBAC.
        # SOLUTION: override с правом допустим; реальный human_only fence берётся
        # из add. Серверный Access/Grants заново проверяет action и resource scope.
        # RESULT: исходный RED; missing/unknown catalogue и действия закрыты по умолчанию.
        # STATUS: FIXED — see #642
        self.assertTrue(hasattr(_control, 'allowed'), '#642 missing server action permission filter')
        actions = frozenset({'execution.restart_puppet', 'operations.override_assignment'})
        human_only = cli.declared(os.path.join(cli.PACKAGE, 'core', 'add.py'))
        self.assertIs(human_only.get('human_only'), True)
        bindings = [(1, 'human', 1, None, None), (2, 'agent', 2, None, None)]
        for node in (None, NODE):
            bindings.extend(((2, 'agent', 1, node, OP), (3, 'service', None, node, None)))
        for binding in bindings:
            with self.subTest(binding=binding):
                identity = api.Identity(*binding, actions)
                self.assertTrue(api.canonical_identity(identity))
                for action in actions:
                    declaration = {'actions': [action]}
                    self.assertTrue(_control.allowed(declaration, identity))
                    self.assertFalse(_control.allowed(declaration,
                        api.Identity(*binding, actions-{action})))
                    self.assertFalse(_control.allowed(declaration, api.Identity(*binding)))
                self.assertFalse(_control.allowed({'actions': ['catalog.drain_node']}, identity))
                for declaration in ({'cluster_verbs': ['restart']}, {'annotations': 'readonly'},
                        {'actions': ['catalog.future']}, {'actions': 'execution.restart_puppet'},
                        {'actions': ['execution.restart_puppet', 'catalog.future']}):
                    self.assertFalse(_control.allowed(declaration, identity))
                self.assertFalse(_control.allowed({'actions': ['execution.restart_puppet']},
                    api.Identity(*binding, actions | {'catalog.future'})))
                self.assertFalse(_control.allowed({'actions': ['execution.restart_puppet']},
                    api.Identity(*binding, set(actions))))
                scoped = api.Identity(*binding, frozenset())
                self.assertTrue(_control.allowed({'annotations': 'readonly', 'actions': []}, scoped))
                self.assertFalse(_control.allowed({'annotations': 'destructive', 'actions': []}, scoped))
                self.assertFalse(_control.allowed({'annotations': 'readonly'}, scoped))
                permissions = frozenset(human_only['actions'])
                self.assertEqual(_control.allowed(human_only, api.Identity(*binding, permissions)),
                    binding[1] == 'human')
                for missing in permissions:
                    self.assertFalse(_control.allowed(human_only,
                        api.Identity(*binding, permissions-{missing})))
        for invalid in ((1, 'human', 2, None, None), (1, 'human', 1, NODE, None),
                (1, 'human', 1, None, OP), (2, 'agent', None, None, OP),
                (3, 'service', 1, None, OP), (3, 'service', None, NODE, OP),
                (3, 'service', 1, None, None)):
            identity = api.Identity(*invalid, actions)
            self.assertFalse(api.canonical_identity(identity))
            self.assertFalse(_control.allowed({'actions': ['execution.restart_puppet']}, identity))
        decl = {'args': [{'name': 'cursor', 'type': 'string', 'flag': '--cursor'}]}
        self.response({'schema_version': 1, 'count': 0, 'results': [], 'next_cursor': None})
        def mcp_tool(argv):
            return _control.tool_call(['project', 'list'], decl, {'cursor': 'safe-cursor'})
        with _control.use(api.ServerContext('https://controller.example', TOKEN)), \
             patch.object(cli, 'main', wraps=cli.main) as dispatcher:
            out, err, result = run_command(mcp_tool, [])
        dispatcher.assert_called_once_with(['project', 'list', '--cursor', 'safe-cursor'])
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
        for words in ('update', 'recycle', 'model',
                      'secret file', 'secret var', 'server gc', 'attach', 'master'):
            with self.subTest(words=words), _control.use(api.ServerContext('https://controller.example', TOKEN)):
                out, err, code = run_command(lambda a: _control.invoke(words, a[0]), [[]])
                self.assertEqual(code, 2)
                self.assertIn('unsupported', err)
                self.assertNotIn('accepted', out)
        self.assertEqual(self.fixture.calls, [])


if __name__ == '__main__':
    unittest.main()
