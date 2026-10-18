#!/usr/bin/env python3
"""Landing #642: finite client, human owner и read-only recovery через свой TLS."""
import http.server
import json
import os
import socket
import sys
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

import hermetic  # noqa: F401
from _lib import run_command
from _control_fixture import Fixture, Response, own_tls, EPOCH, PROJECT, TOKEN
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
from mop.client import control as api
from mop.cli import _control
from mop import cli


# HYPOTHESIS: landing всё ещё unsupported/legacy, а operation recovery неприменим
# к SQL CAS без request_id. Совпавшее чтение не является historical receipt.
# SPEC: UUID project+epoch, canonical decimal revision, actual human; один POST,
# затем только GET с точным successor/owner, неопределённость не становится grant.
# RED: отсутствующие методы/DTO, legacy MCP и unsupported CLI; затем
# execution_conflict ошибочно превращался в unknown вместо типизированного 409.
# SOLUTION: отдельные finite Landing DTO/SQL CAS path, actual identity до POST;
# только GET recovery с явным unknown, canonical execution_conflict без retry.
# RESULT: 7 pure + 3 собственных HTTPS checks green; соседний native client gate
# (6 TLS/wheel и 6 Django/PostgreSQL 17 checks) также green, не live pool.
# STATUS: FIXED — see #642
def envelope(state='unmaterialized', revision='0', owner=None, generation=None):
    return {'schema_version': 1, 'epoch': EPOCH, 'landing': {'project_id': PROJECT,
        'state': state, 'revision': revision, 'generation': generation, 'owner_id': owner}}


def identity(kind='human', owner=7, grant=None):
    return {'schema_version': 1, 'user_id': 7, 'kind': kind, 'human_owner_id': owner,
        'node_id': None, 'grant_id': grant, 'action_permissions': [
            'execution.view_landing', 'execution.acquire_landing', 'execution.release_landing']}


class LandingClient(unittest.TestCase):
    def client(self, *responses):
        fixture = Fixture(*responses)
        self.addCleanup(patch.stopall)
        patch.object(api, '_DeadlineHTTPSConnection', fixture.factory).start()
        return api.Client(api.ServerContext('https://fixture.invalid', TOKEN)), fixture

    def test_absent_is_explicit_and_query_cannot_materialize(self):
        client, fixture = self.client(Response(data=envelope()))
        value = client.read_landing(PROJECT)
        self.assertEqual(value.envelope(), envelope())
        self.assertEqual([(m,p) for m,p,_,_ in fixture.calls], [('GET', f'/api/v1/projects/{PROJECT}/landing')])

    def test_mutation_has_exact_body_and_actual_current_human_not_owner_alias(self):
        client, fixture = self.client(Response(data=identity()),
            Response(data=envelope('held', '1', 7, '1')))
        value = client.change_landing('take', PROJECT, epoch=EPOCH, expected_revision='0')
        self.assertEqual((value.owner_id, value.revision), (7, '1'))
        self.assertEqual([m for m,_,_,_ in fixture.calls], ['GET', 'POST'])
        self.assertEqual(fixture.calls[-1][1], f'/api/v1/projects/{PROJECT}/landing/commands/take')
        self.assertEqual(json.loads(fixture.calls[-1][2]),
            {'schema_version': 1, 'epoch': EPOCH, 'expected_revision': '0'})
        for kind, grant in (('agent', None), ('service', PROJECT), ('human', PROJECT)):
            client, fixture = self.client(Response(data=identity(kind, owner=99, grant=grant)))
            with self.assertRaises(api.ClientError):
                client.change_landing('take', PROJECT, epoch=EPOCH, expected_revision='0')
            self.assertEqual([m for m,_,_,_ in fixture.calls], ['GET'])

    def test_invalid_binding_refuses_before_io(self):
        client, fixture = self.client()
        for values in (('take', 'pu-one-1', EPOCH, '0'), ('force', PROJECT, EPOCH, '0'),
                       ('take', PROJECT, EPOCH.upper(), '0')):
            with self.assertRaises(api.ClientError):
                client.change_landing(values[0], values[1], epoch=values[2], expected_revision=values[3])
        for revision in (True, 0, '00', '-1', '9223372036854775808'):
            with self.assertRaises(api.ClientError):
                client.change_landing('take', PROJECT, epoch=EPOCH, expected_revision=revision)
        self.assertEqual(fixture.calls, [])

    def test_malformed_or_misbound_reply_never_invents_landing_owner(self):
        bad = [envelope('held', '1', None, '1'), envelope('free', '0'),
               envelope('unmaterialized', '0', 7), envelope('free', '2', 7, '2')]
        for mutate in ('epoch', 'project_id', 'schema_version', 'unknown'):
            value = envelope()
            if mutate == 'epoch': value['epoch'] = PROJECT
            elif mutate == 'project_id': value['landing']['project_id'] = EPOCH
            elif mutate == 'schema_version': value['schema_version'] = True
            else: value['landing']['unknown'] = True
            bad.append(value)
        for value in bad:
            client, _ = self.client(Response(data=value))
            with self.assertRaises(api.ProtocolError):
                client.read_landing(PROJECT, epoch=EPOCH)

    def test_lost_response_and_uncertain_commit_recover_only_exact_revision_owner(self):
        for failure in (OSError('private-token'), Response(status=503, data={
            'schema_version': 1, 'error': {'code': 'database_outcome_unknown',
                'message': 'database outcome unknown', 'retryable': False}})):
            client, fixture = self.client(Response(data=identity()), failure,
                Response(data=envelope('held', '1', 7, '1')))
            with self.assertRaises(api.UnknownLanding) as caught:
                client.change_landing('take', PROJECT, epoch=EPOCH, expected_revision='0')
            recovery = client.recover_landing(caught.exception.intent)
            self.assertTrue(recovery.matched)
            self.assertEqual(recovery.snapshot.owner_id, 7)
            self.assertEqual([m for m,_,_,_ in fixture.calls], ['GET', 'POST', 'GET'])
            self.assertEqual(fixture.calls[-1][1], f'/api/v1/projects/{PROJECT}/landing?epoch={EPOCH}')
        intent = api.LandingIntent('take', PROJECT, EPOCH, '0', 7)
        for value in (envelope(), envelope('held', '3', 7, '3'), envelope('held', '1', 8, '1')):
            client, fixture = self.client(Response(data=value))
            self.assertFalse(client.recover_landing(intent).matched)
            self.assertEqual([m for m,_,_,_ in fixture.calls], ['GET'])
        client, fixture = self.client(Response(data=envelope('free', '2', None, '2')))
        self.assertTrue(client.recover_landing(api.LandingIntent('release', PROJECT, EPOCH, '1', 7)).matched)
        self.assertEqual([m for m,_,_,_ in fixture.calls], ['GET'])

    def test_wrong_success_reply_is_unknown_and_api_refusal_is_not_retried(self):
        client, fixture = self.client(Response(data=identity()), Response(data=envelope('held', '3', 7, '3')))
        with self.assertRaises(api.UnknownLanding):
            client.change_landing('take', PROJECT, epoch=EPOCH, expected_revision='0')
        client, fixture = self.client(Response(data=identity()), Response(status=409, data={
            'schema_version': 1, 'error': {'code': 'execution_conflict',
                'message': 'landing token is already held', 'retryable': False}}))
        with self.assertRaises(api.Conflict):
            client.change_landing('take', PROJECT, epoch=EPOCH, expected_revision='0')
        self.assertEqual([m for m,_,_,_ in fixture.calls], ['GET', 'POST'])

    def test_mcp_uses_canonical_permissions_and_no_force_puppet_or_legacy_verbs(self):
        from mop.cli.core.landing import MCP
        actions = ['execution.view_landing', 'execution.acquire_landing', 'execution.release_landing']
        self.assertEqual(MCP.get('actions'), actions)
        self.assertIs(MCP.get('human_only'), True)
        self.assertNotIn('cluster_verbs', MCP)
        self.assertFalse({'puppet', 'force'} & {arg['name'] for arg in MCP['args']})
        self.assertTrue(_control.allowed(MCP, api.Identity(7, 'human', 7, None, None, frozenset(actions))))
        self.assertFalse(_control.allowed(MCP, api.Identity(7, 'agent', 7, None, None, frozenset(actions))))
        for argv in (['show'], ['take', PROJECT], ['take', PROJECT, '--force'], ['take', 'pu-one-1']):
            client, fixture = self.client()
            with _control.use(client._context):
                _, _, code = run_command(cli.main, ['landing', *argv])
            self.assertNotEqual(code, 0)
            self.assertEqual(fixture.calls, [])


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, value):
        raw = json.dumps(value).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def authorized(self):
        self.server.calls.append((self.command, self.path))
        assert self.headers.get('Authorization') == 'Token ' + TOKEN

    def do_GET(self):
        self.authorized()
        if self.path == '/api/v1/identity':
            self.reply(identity())
        else:
            assert urlsplit(self.path).path == f'/api/v1/projects/{PROJECT}/landing'
            self.reply(self.server.value)

    def do_POST(self):
        self.authorized()
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.server.bodies.append(body)
        assert body == {'schema_version': 1, 'epoch': EPOCH, 'expected_revision': self.server.expected_revision}
        assert self.path == f'/api/v1/projects/{PROJECT}/landing/commands/{self.server.action}'
        # Это только CAS-модель HTTPS fixture: real COMMIT проверен отдельно PG17.
        self.server.value = self.server.after
        self.close_connection = True
        self.connection.shutdown(socket.SHUT_RDWR)
        self.connection.close()


class LandingTLS(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = own_tls(Handler)
        cls.server, cls.origin, cls.cert, _ = cls.fixture.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.fixture.__exit__(None, None, None)

    def run_cli(self, words):
        with _control.use(api.ServerContext(self.origin, TOKEN, str(self.cert))):
            return run_command(cli.main, ['landing', *words])

    def setUp(self):
        self.server.calls, self.server.bodies = [], []
        self.server.action, self.server.expected_revision = 'take', '0'
        self.server.value = envelope()
        self.server.after = envelope('held', '1', 7, '1')

    def test_real_https_lost_reply_is_observation_not_false_grant_or_repost(self):
        out, err, code = self.run_cli(['take', PROJECT, '--epoch', EPOCH, '--expected-revision', '0'])
        self.assertEqual(code, 3, err)
        value = json.loads(out)
        self.assertTrue(value['recovery']['matched'])
        self.assertEqual(value['recovery']['command_outcome'], 'unknown')
        self.assertEqual(value['landing']['owner_id'], 7)
        self.assertIn('not a command receipt', err)
        self.assertEqual(self.server.calls, [('GET', '/api/v1/identity'),
            ('POST', f'/api/v1/projects/{PROJECT}/landing/commands/take'),
            ('GET', f'/api/v1/projects/{PROJECT}/landing?epoch={EPOCH}')])
        self.assertEqual(len(self.server.bodies), 1)
        self.assertNotIn(TOKEN, out+err)

    def test_later_same_owner_is_uncertain_and_explicit_recovery_never_posts(self):
        self.server.after = envelope('held', '3', 7, '3')
        out, err, code = self.run_cli(['take', PROJECT, '--epoch', EPOCH, '--expected-revision', '0'])
        self.assertEqual(code, 3, err)
        self.assertFalse(json.loads(out)['recovery']['matched'])
        self.assertEqual(len(self.server.bodies), 1)
        self.server.calls = []
        out, err, code = self.run_cli(['recover-take', PROJECT, '--epoch', EPOCH,
            '--expected-revision', '0', '--owner-id', '7'])
        self.assertEqual(code, 3, err)
        self.assertFalse(json.loads(out)['recovery']['matched'])
        self.assertEqual(self.server.calls, [('GET', f'/api/v1/projects/{PROJECT}/landing?epoch={EPOCH}')])

    def test_release_lost_reply_and_show_are_read_only_observations(self):
        self.server.action, self.server.expected_revision = 'release', '1'
        self.server.after = envelope('free', '2', None, '2')
        out, err, code = self.run_cli(['give', PROJECT, '--epoch', EPOCH, '--expected-revision', '1'])
        self.assertEqual(code, 3, err)
        self.assertTrue(json.loads(out)['recovery']['matched'])
        self.assertEqual(len(self.server.bodies), 1)
        self.server.calls = []
        out, err, code = self.run_cli(['show', PROJECT])
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out), self.server.after)
        self.assertEqual([m for m,_ in self.server.calls], ['GET'])


if __name__ == '__main__':
    unittest.main()
