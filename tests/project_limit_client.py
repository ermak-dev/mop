#!/usr/bin/env python3
"""Конечный catalogue CAS не выдумывает durable Operation receipt (#761)."""
import json
import os
import sys
import unittest
from unittest.mock import patch

import hermetic  # noqa: F401
from _lib import run_command
from _control_fixture import Fixture, Response, PROJECT, TOKEN
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
from mop import cli
from mop.cli import _control
from mop.client import control as api


# HYPOTHESIS: готовый Project PUT недоступен новому CLI/MCP.
# SOLUTION: immutable limit-only intent из точного authorized GET, один CAS PUT.
# RESULT: RED missing consumer/unsupported CLI; GREEN — 10 pure cases,
# 10 real PostgreSQL17/TLS checks + UID5, без physical операций.
# STATUS: FIXED — see #761
class ProjectLimitClient(unittest.TestCase):
    def setUp(self):
        self.fixture = Fixture()
        self.patcher = patch.object(api, '_DeadlineHTTPSConnection', self.fixture.factory)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.context = api.ServerContext('https://controller.example', TOKEN)
        self.client = api.Client(self.context)
        self.project = api.Project(PROJECT, 'fixture', 'https://example.invalid/fixture.git', '7', 2)

    def response(self, revision='8', limit=3):
        return {'schema_version': 1, 'id': PROJECT, 'name': self.project.name,
            'origin': self.project.origin, 'revision': revision, 'puppet_limit': limit}

    def test_finite_limit_intent_and_exact_catalogue_put(self):
        self.assertTrue(hasattr(self.client, 'prepare_project_limit'), 'missing finite project-limit intent')
        intent = self.client.prepare_project_limit(self.project, 3)
        self.assertEqual(self.fixture.calls, [])
        self.fixture.responses.append(Response(200, self.response()))
        value = self.client.set_project_limit(intent)
        self.assertEqual((value.id, value.revision, value.puppet_limit), (PROJECT, '8', 3))
        self.assertEqual(self.fixture.calls[0][:2], ('PUT', f'/api/v1/projects/{PROJECT}'))
        self.assertEqual(json.loads(self.fixture.calls[0][2]), {'origin': self.project.origin,
            'expected_revision': '7', 'puppet_limit': 3})

    def test_lost_put_and_unknown_commit_never_replay_or_claim_success(self):
        self.assertTrue(hasattr(self.client, 'prepare_project_limit'), 'missing finite project-limit intent')
        self.assertTrue(hasattr(api, 'UnknownProjectLimit'), 'missing keyless mutation outcome')
        for failure in (OSError(TOKEN), Response(503, {'schema_version': 1, 'error': {
                'code': 'database_outcome_unknown', 'message': 'database outcome unknown', 'retryable': False}})):
            self.fixture.responses.append(failure)
            intent = self.client.prepare_project_limit(self.project, 3)
            with self.assertRaises(api.UnknownProjectLimit) as caught:
                self.client.set_project_limit(intent)
            self.assertIs(caught.exception.intent, intent)
            self.assertNotIn(TOKEN, str(caught.exception))
        self.assertEqual([call[0] for call in self.fixture.calls], ['PUT', 'PUT'])

    def test_known_catalogue_conflict_is_not_unknown_or_retried(self):
        self.assertTrue(hasattr(self.client, 'prepare_project_limit'), 'missing finite project-limit intent')
        self.fixture.responses.append(Response(409, {'schema_version': 1, 'error': {
            'code': 'catalog_conflict', 'message': 'catalog revision changed', 'retryable': False}}))
        with self.assertRaises(api.Conflict) as caught:
            self.client.set_project_limit(self.client.prepare_project_limit(self.project, 3))
        self.assertEqual(caught.exception.error.code, 'catalog_conflict')
        self.assertEqual([call[0] for call in self.fixture.calls], ['PUT'])

    def test_invalid_or_misbound_success_is_unknown_not_a_receipt(self):
        self.assertTrue(hasattr(self.client, 'prepare_project_limit'), 'missing finite project-limit intent')
        for change in ({'id': 'not-a-uuid'}, {'revision': '9'}, {'puppet_limit': 4},
                {'origin': 'https://other.invalid/fixture.git'}, {'schema_version': True}):
            self.fixture.responses.append(Response(200, {**self.response(), **change}))
            with self.assertRaises(api.UnknownProjectLimit):
                self.client.set_project_limit(self.client.prepare_project_limit(self.project, 3))
        self.assertEqual([call[0] for call in self.fixture.calls], ['PUT']*5)

    def test_cli_uses_authorized_origin_and_revision_without_legacy_inputs(self):
        self.fixture.responses.extend((Response(200, self.response('7', 2)), Response(200, self.response())))
        with _control.use(self.context), patch.object(cli, 'environment',
                side_effect=AssertionError('legacy installation context')):
            out, err, code = run_command(cli.main, ['project', 'limit', PROJECT, '3'])
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)['puppet_limit'], 3)
        self.assertEqual([call[0] for call in self.fixture.calls], ['GET', 'PUT'])
        self.assertEqual(json.loads(self.fixture.calls[-1][2])['expected_revision'], '7')
        self.assertNotIn(TOKEN, out+err)

    def test_cli_unknown_after_put_is_exit_three_and_no_automatic_observation(self):
        self.fixture.responses.extend((Response(200, self.response('7', 2)), OSError(TOKEN)))
        with _control.use(self.context):
            out, err, code = run_command(cli.main, ['project', 'limit', PROJECT, 'none'])
        self.assertEqual(code, 3, err)
        self.assertEqual(out, '')
        self.assertIn('unknown', err)
        self.assertIn(PROJECT, err)
        self.assertIn('7', err)
        self.assertNotIn(TOKEN, err)
        self.assertEqual([call[0] for call in self.fixture.calls], ['GET', 'PUT'])

    def test_invalid_limit_and_explicit_revision_refuse_before_context(self):
        bad = [[], ['named-project', '3'], [PROJECT, '-1'], [PROJECT, 'true'],
            [PROJECT, '2147483648'], [PROJECT, '03'], [PROJECT, '3', '--force'],
            [PROJECT, '3', '--expected-revision', '0'], [PROJECT, '3', '--expected-revision', '01'],
            [PROJECT, '3', '--expected-revision', '9223372036854775808']]
        with patch.object(_control, 'context', side_effect=AssertionError('context before validation')):
            for argv in bad:
                with self.subTest(argv=argv):
                    out, err, code = run_command(cli.main, ['project', 'limit', *argv])
                    self.assertIn(code, (1, 2))
                    self.assertEqual(out, '')
        self.assertEqual(self.fixture.calls, [])

    def test_explicit_stale_revision_is_not_replaced_by_fresh_detail(self):
        self.fixture.responses.append(Response(200, self.response('8', 2)))
        with _control.use(self.context):
            out, err, code = run_command(cli.main, ['project', 'limit', PROJECT, '3', '--expected-revision', '7'])
        self.assertEqual(code, 1, err)
        self.assertEqual([call[0] for call in self.fixture.calls], ['GET'])
        self.assertIn('revision', err)
        self.assertEqual(out, '')

    def test_mcp_requires_exact_actions_and_explicit_revision_with_same_handler(self):
        declaration = cli.declared(os.path.join(cli.PACKAGE, 'project', 'limit.py'))
        actions = frozenset(('catalog.view_project', 'catalog.change_project', 'catalog.set_project_limit'))
        self.assertEqual(frozenset(declaration.get('actions', [])), actions)
        from mop.cli import _mcp
        self.assertEqual(set(_mcp.schema(declaration)['required']), {'project_id', 'limit', 'expected_revision'})
        for absent in actions:
            self.assertFalse(_control.allowed(declaration,
                api.Identity(1, 'human', 1, None, None, actions-{absent})))
        self.fixture.responses.extend((Response(200, self.response('7', 2)), Response(200, self.response())))
        with _control.use(self.context):
            result = _control.tool_call(['project', 'limit'], declaration,
                {'project_id': PROJECT, 'limit': '3', 'expected_revision': '7'})
        self.assertEqual(json.loads(result)['puppet_limit'], 3)
        self.assertEqual([call[0] for call in self.fixture.calls], ['GET', 'PUT'])

    def test_typed_limits_are_finite_and_noop_accepts_original_revision(self):
        self.assertTrue(hasattr(self.client, 'prepare_project_limit'), 'missing finite project-limit intent')
        for value in (True, -1, 2147483648, '3'):
            with self.subTest(value=value), self.assertRaises(api.ClientError):
                self.client.prepare_project_limit(self.project, value)
        for value in (0, 2147483647, None):
            intent = self.client.prepare_project_limit(self.project, value)
            self.assertEqual(json.loads(intent.body)['puppet_limit'], value)
        self.fixture.responses.append(Response(200, self.response('7', 2)))
        result = self.client.set_project_limit(self.client.prepare_project_limit(self.project, 2))
        self.assertEqual(result.revision, '7')


if __name__ == '__main__':
    unittest.main()
