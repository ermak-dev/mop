#!/usr/bin/env python3
"""Конечный dispatch-клиент не подменяет отдельное серверное право (#757)."""
import io
import json
import os
import sys
import unittest
from unittest.mock import patch

import hermetic  # noqa: F401
from _lib import run_command
from _control_fixture import (Fixture, Response, KEY, EPOCH, DEADLINE, TOKEN,
    PUPPET, INCARNATION, send_operation, puppet_row)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
from mop import cli
from mop.cli import _control
from mop.client import control as api


# HYPOTHESIS: готовый DispatchCommand недоступен canonical client/CLI/MCP.
# SOLUTION: отдельный finite dispatch intent/route, общий transport и GET recovery.
# RESULT: RED — missing consumer/commandlet и unsupported CLI; GREEN — 7 cases,
# 26 real PostgreSQL17/TLS checks + UID5; native delivery здесь не запускалась.
# STATUS: FIXED — see #757
class DispatchClient(unittest.TestCase):
    def setUp(self):
        self.fixture = Fixture()
        self.patcher = patch.object(api, '_DeadlineHTTPSConnection', self.fixture.factory)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.context = api.ServerContext('https://controller.example', TOKEN)
        self.client = api.Client(self.context)

    def test_typed_dispatch_has_separate_route_and_exact_server_command(self):
        self.assertTrue(hasattr(self.client, 'prepare_dispatch'), 'missing finite dispatch consumer')
        command = self.client.prepare_dispatch(api.Target(PUPPET, INCARNATION, '7'),
            epoch=EPOCH, request_id=KEY, not_after=DEADLINE, message='private-dispatch',
            priority='later', wait_seconds=2)
        self.assertEqual(self.fixture.calls, [])
        from mop.server.operations.commands import parse_dispatch, DispatchCommand
        parsed = parse_dispatch(json.loads(command.body))
        self.assertIs(type(parsed), DispatchCommand)
        self.assertEqual(str(parsed.intent.target_id), PUPPET)
        self.assertEqual(parsed.message, 'private-dispatch')
        self.assertNotIn('private-dispatch', repr(command))
        self.fixture.responses.append(Response(202, send_operation()))
        self.assertEqual(self.client.submit(command).operation.state, 'accepted')
        self.assertEqual(self.fixture.calls[0][0:2], ('POST', '/api/v1/dispatches'))
        self.assertEqual(json.loads(self.fixture.calls[0][2]), json.loads(command.body))

    def test_cli_dispatch_explicit_binding_and_no_legacy_context(self):
        self.fixture.responses.append(Response(202, send_operation()))
        with _control.use(self.context), patch.object(cli, 'environment',
                side_effect=AssertionError('legacy installation context read')):
            out, err, code = run_command(cli.main, ['dispatch', PUPPET, 'private-dispatch',
                '--epoch', EPOCH, '--incarnation', INCARNATION, '--expected-revision', '7',
                '--request-id', KEY, '--not-after', DEADLINE])
        self.assertEqual(code, 3, err)
        self.assertEqual(json.loads(out)['operation']['state'], 'accepted')
        self.assertEqual([call[:2] for call in self.fixture.calls], [('POST', '/api/v1/dispatches')])
        self.assertNotIn('private-dispatch', out+err)
        self.assertNotIn(TOKEN, out+err)

    def test_generated_key_and_deadline_are_fixed_before_detail_io(self):
        self.fixture.responses.append(Response(200,
            {'schema_version': 1, 'epoch': EPOCH, 'puppet': puppet_row()}))
        self.fixture.responses.append(Response(202, send_operation()))
        def context(*args):
            self.assertTrue(key.called, 'request identity must precede context/detail I/O')
            self.assertTrue(deadline.called, 'validated deadline must precede context/detail I/O')
            return self.context
        with patch.object(api, 'new_request_id', return_value=KEY) as key, \
                patch.object(api, 'admission_deadline', wraps=api.admission_deadline) as deadline, \
                patch.object(_control, 'context', side_effect=context):
            out, err, code = run_command(cli.main, ['dispatch', PUPPET, 'private-dispatch'])
        self.assertEqual(code, 3, err)
        self.assertEqual([call[0] for call in self.fixture.calls], ['GET', 'POST'])
        body = json.loads(self.fixture.calls[-1][2])
        self.assertEqual((body['request_id'], body['epoch'], body['incarnation'],
            body['expected_revision']), (KEY, EPOCH, INCARNATION, '7'))
        api.admission_deadline(body['not_after'])
        self.assertNotIn('private-dispatch', out+err)

    def test_lost_response_recovers_only_get_without_second_dispatch(self):
        self.fixture.responses.extend((OSError('private-dispatch'), Response(200, send_operation('succeeded'))))
        with _control.use(self.context):
            out, err, code = run_command(cli.main, ['dispatch', PUPPET, 'private-dispatch',
                '--epoch', EPOCH, '--incarnation', INCARNATION, '--expected-revision', '7',
                '--request-id', KEY, '--not-after', DEADLINE])
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)['operation']['state'], 'succeeded')
        self.assertEqual([call[:2] for call in self.fixture.calls], [
            ('POST', '/api/v1/dispatches'), ('GET', f'/api/v1/operations?request_id={KEY}&epoch={EPOCH}')])
        self.assertNotIn('private-dispatch', out+err)

    def test_mcp_declaration_requires_dispatch_not_send_and_uses_same_handler(self):
        path = os.path.join(cli.PACKAGE, 'core', 'dispatch.py')
        self.assertTrue(os.path.isfile(path), 'missing declared dispatch commandlet')
        declaration = cli.declared(path)
        actions = frozenset(('execution.dispatch_task', 'operations.view_operation'))
        self.assertEqual(frozenset(declaration.get('actions', [])), actions)
        self.assertTrue(_control.allowed(declaration, api.Identity(1, 'human', 1, None, None, actions)))
        self.assertFalse(_control.allowed(declaration, api.Identity(1, 'human', 1, None, None,
            frozenset(('execution.send_message', 'operations.view_operation')))))
        self.fixture.responses.append(Response(202, send_operation()))
        with _control.use(self.context):
            result = _control.tool_call(['dispatch'], declaration, {'puppet_id': PUPPET,
                'message': 'private-dispatch', 'epoch': EPOCH, 'incarnation': INCARNATION,
                'expected_revision': '7', 'request_id': KEY, 'not_after': DEADLINE})
        self.assertEqual(json.loads(result)['operation']['state'], 'accepted')
        self.assertEqual(self.fixture.calls[0][1], '/api/v1/dispatches')

    def test_invalid_or_unbound_options_do_not_read_private_stdin_or_context(self):
        class RefusedInput(io.StringIO):
            def read(self, size=-1):
                raise AssertionError('invalid routing read private stdin')
        bad = [[], ['pu-project-1', '-'], [PUPPET, '-', '--force'],
            [PUPPET, '-', '--notify-when-idle'], [PUPPET, '-', '--reply-to', 'other'],
            [PUPPET, '-', '--epoch', EPOCH], [PUPPET, '-', '--wait-seconds', '61'],
            [PUPPET, '-', '--wait', 'nan'], [PUPPET, '-', '--request-id', 'invalid']]
        with patch.object(_control, 'context', side_effect=AssertionError('context before validation')), \
                patch.object(sys, 'stdin', RefusedInput()):
            for argv in bad:
                with self.subTest(argv=argv):
                    out, err, code = run_command(cli.main, ['dispatch', *argv])
                    self.assertIn(code, (1, 2))
                    self.assertEqual(out, '')
        self.assertEqual(self.fixture.calls, [])

    def test_typed_dispatch_rejects_bad_private_values_before_io(self):
        self.assertTrue(hasattr(self.client, 'prepare_dispatch'), 'missing finite dispatch consumer')
        for fields in ({'message': ''}, {'message': '  '}, {'message': '\x00'},
                {'message': 'é'*8193}, {'message': 'ok', 'priority': 'shell'},
                {'message': 'ok', 'wait_seconds': True}, {'message': 'ok', 'wait_seconds': 61}):
            with self.subTest(fields=fields), self.assertRaises(api.ClientError):
                self.client.prepare_dispatch(api.Target(PUPPET, INCARNATION, '7'), epoch=EPOCH,
                    request_id=KEY, not_after=DEADLINE, **fields)
        self.assertEqual(self.fixture.calls, [])


if __name__ == '__main__':
    unittest.main()
