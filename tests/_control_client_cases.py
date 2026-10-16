"""#642: HTTPS client -> настоящий Django/DRF Auth -> PostgreSQL admission/result.

HYPOTHESIS: header double не доказывает standard Token/auth или durable lookup.
SOLUTION: свой TLS listener перед Django test handler с независимыми DB connections;
ответ теряется только после настоящего COMMIT. Effect заменён контролируемой
распиской существующего node handler, не операцией на живом пуле.
RESULT: четыре native TLS/DRF checks прошли на собственных PG16 и PG17 fixtures.
STATUS: FIXED — see #642; эти cases не принимают отсутствующие consumers.
"""
import contextlib
import http.server
import io
import json
import socket
from uuid import uuid4

from django.db import connections
from django.test import Client, TransactionTestCase, override_settings
from django.utils import timezone
from datetime import timedelta

import _operation_cases as operation_fixture
import _build_cases as build_fixture
from control_tls import own_tls
from mop import cli
from mop.cli import _control
from mop.client import control as api


class DjangoTLS(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *args):
        pass

    def serve_django(self):
        self.server.calls.append((self.command, self.path))
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 <= size <= 65536:
                raise AssertionError('native client exceeded request bound')
            body = self.rfile.read(size)
            response = Client(enforce_csrf_checks=True).generic(self.command, self.path, body,
                content_type='application/json', secure=True,
                HTTP_HOST='localhost', HTTP_AUTHORIZATION=self.headers.get('Authorization', ''))
            self.server.after(self.command, response)
            drop = (self.command == 'POST' and self.server.drop_post or
                    self.command == 'GET' and self.server.drop_get)
            if drop:
                self.server.drop_post = self.server.drop_get = False
                self.close_connection = True
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return
            self.send_response(response.status_code)
            self.send_header('Content-Type', response.get('Content-Type', 'application/json'))
            self.send_header('Content-Length', str(len(response.content)))
            self.send_header('Cache-Control', response.get('Cache-Control', 'no-store'))
            self.send_header('Connection', 'close')
            self.end_headers()
            self.wfile.write(response.content)
            self.close_connection = True
        finally:
            connections.close_all()

    do_GET = serve_django
    do_POST = serve_django


@override_settings(ALLOWED_HOSTS=['localhost', 'testserver'])
class NativeControlClient(TransactionTestCase):
    serialized_rollback = True

    def setUp(self):
        operation_fixture.DurableOperations.setUp(self)

    @contextlib.contextmanager
    def https(self):
        with own_tls(DjangoTLS) as (server, origin, cert, directory):
            server.calls = []
            server.drop_post = server.drop_get = False
            server.after = lambda method, response: None
            with _control.use(api.ServerContext(origin, self.token, str(cert))):
                yield server, api.Client(api.ServerContext(origin, self.token, str(cert)))

    def args(self, key):
        return [str(self.node.pk), '--epoch', str(self.epoch), '--expected-revision', '1',
            '--request-id', key, '--not-after', (timezone.now()+timedelta(minutes=2)).isoformat()]

    def complete_once(self, method, response):
        if method == 'POST' and response.status_code == 202:
            operation = json.loads(response.content)['operation']
            claim = self.api.claim(operation['id'])
            if claim is None:
                raise AssertionError('controlled effect did not claim admission')
            self.api.complete(claim, expected_revision=claim.revision, applied=True)

    def test_real_token_tls_lost_accepted_reply_recovers_one_committed_effect(self):
        key = str(uuid4())
        out, err = io.StringIO(), io.StringIO()
        with self.https() as (server, client):
            server.after, server.drop_post = self.complete_once, True
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                self.assertEqual(cli.main(['node', 'drain', *self.args(key)]), 0, err.getvalue())
            result = json.loads(out.getvalue())
            self.assertEqual(result['operation']['state'], 'succeeded')
            self.assertEqual(result['operation']['request_id'], key)
            self.assertEqual([method for method, _ in server.calls], ['POST', 'GET'])
            self.assertEqual(self.m.Operation.objects.filter(request_id=key).count(), 1)
            self.assertEqual(self.m.Step.objects.count(), 1)
            self.assertEqual(self.m.EffectReceipt.objects.count(), 1)
            self.assertEqual(client.lookup_operation(key, epoch=str(self.epoch)).id, result['operation']['id'])
        self.assertNotIn(self.token, out.getvalue()+err.getvalue())

    def test_lost_result_after_accepted_keeps_key_and_get_only_recovery(self):
        key = str(uuid4())
        out, err = io.StringIO(), io.StringIO()
        with self.https() as (server, client):
            server.after, server.drop_get = self.complete_once, True
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                self.assertEqual(cli.main(['node', 'drain', *self.args(key), '--wait', '1']), 3)
            self.assertIn(key, err.getvalue())
            self.assertIn(str(self.epoch), err.getvalue())
            result = client.lookup_operation(key, epoch=str(self.epoch))
            self.assertEqual(result.state, 'succeeded')
            self.assertEqual([method for method, _ in server.calls], ['POST', 'GET', 'GET'])
            self.assertEqual(self.m.Operation.objects.count(), 1)
            self.assertEqual(self.m.EffectReceipt.objects.count(), 1)
        self.assertNotIn(self.token, out.getvalue()+err.getvalue())

    def test_real_build_acceptance_pins_existing_policy_and_recovers_by_get(self):
        build_fixture.ImageBuilds.permission(self)
        policy = build_fixture.ImageBuilds.policy(self)
        key = str(uuid4())
        out, err = io.StringIO(), io.StringIO()
        with self.https() as (server, client):
            server.drop_post = True
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = cli.main(['build', str(self.project.pk), '--policy-id', str(policy.pk),
                    '--target-node', str(self.node.pk), '--epoch', str(self.epoch),
                    '--expected-revision', '1', '--request-id', key, '--mode', 'rebuild'])
            self.assertEqual(code, 3, err.getvalue())
            result = json.loads(out.getvalue())['operation']
            self.assertEqual(result['state'], 'accepted')
            self.assertEqual(result['build']['policy_id'], str(policy.pk))
            self.assertEqual(result['build']['source_commit'], 'a'*40)
            self.assertEqual(client.lookup_operation(key, epoch=str(self.epoch)).id, result['id'])
            self.assertEqual([method for method, _ in server.calls], ['POST', 'GET', 'GET'])
            self.assertEqual(self.m.Operation.objects.count(), 1)
            self.assertEqual(self.m.EffectReceipt.objects.count(), 0)
        self.assertNotIn(self.token, out.getvalue()+err.getvalue())
        self.assertNotIn('manifest', result['build'])

    def test_real_auth_permissions_and_key_conflict_are_typed_not_retried(self):
        key = str(uuid4())
        with self.https() as (server, client):
            context = client._context
            invalid = api.Client(api.ServerContext(context.origin, '0'*40, context.ca_file))
            with self.assertRaises(api.APIError) as caught:
                invalid.health()
            self.assertEqual(caught.exception.error.http_status, 401)
            deadline = (timezone.now()+timedelta(minutes=2)).isoformat()
            command = client.prepare_node_drain(str(self.node.pk), epoch=str(self.epoch),
                expected_revision='1', not_after=deadline, request_id=key)
            self.assertEqual(client.submit(command).operation.state, 'accepted')
            changed = client.prepare_node_enable(str(self.node.pk), epoch=str(self.epoch),
                expected_revision='1', not_after=deadline, request_id=key)
            with self.assertRaises(api.Conflict) as caught:
                client.submit(changed)
            self.assertEqual(caught.exception.error.code, 'request_conflict')
            self.user.user_permissions.clear()
            with self.assertRaises(api.APIError) as caught:
                client.submit(client.prepare_node_drain(str(self.node.pk), epoch=str(self.epoch),
                    expected_revision='1', not_after=deadline))
            self.assertEqual(caught.exception.error.http_status, 403)
            self.assertEqual(self.m.Operation.objects.count(), 1)
            self.assertEqual(self.m.EffectReceipt.objects.count(), 0)
            self.assertEqual(len(server.calls), 4)
