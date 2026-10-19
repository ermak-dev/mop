"""#772: sender-key recovery читает только metadata, не повторяет POST.

HYPOTHESIS: после COMMIT клиент знает message_id, но существующий маршрут
принимает лишь server delivery_id; отсутствие ответа не означает noneffect.
SPEC: current Actor + canonical epoch/message_id, ровно DeliveryResult,
restore/current RBAC/delegation fences, без payload/crypto/receipt writes.
SOLUTION: общий sender authorization и SQL-only lookup под restore/epoch fence.
RESULT: Python 3.12/PG17 — 152 scoped и 6 UID проверок прошли; verified HTTPS
теряет весь ответ после COMMIT, затем один GET находит metadata одного POST.
Delivery/lease/receipt/operation snapshots неизменны, crypto/protected calls = 0.
STATUS: FIXED — see #772
"""
import asyncio
from contextlib import contextmanager
import logging
import os
from pathlib import Path
import socket
import ssl
import subprocess
from unittest.mock import patch
from urllib.parse import urlencode
from uuid import uuid4

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.db import connection
from django.test import Client, override_settings
from django.test.utils import CaptureQueriesContext

from _endpoint_credential_cases import EndpointCredentialFixture
from mop.server.access import models as a, services as access
from mop.server.controller import endpointviews
from mop.server.controller.models import Installation, RestoreGuard
from mop.server.operations.models import Operation
from mop.server.sessions import endpoint_models as m


@override_settings(ALLOWED_HOSTS=['testserver', 'localhost', '127.0.0.1'])
class DeliveryLookupCases(EndpointCredentialFixture):
    def query(self, message_id=None, epoch=None):
        return dict(schema_version='1', epoch=str(epoch or self.installation.epoch),
                    message_id=str(message_id or self.values['message_id']))

    def get(self, *, auth=None, query=None):
        return Client(HTTP_AUTHORIZATION='Token '+(auth or self.sender_auth).value).get(
            '/api/v1/deliveries', query or self.query(), secure=True)

    def assert_metadata(self, response, expected):
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response['Cache-Control'], 'no-store')
        self.assertEqual(response.json(), dict(schema_version=1, delivery=expected))

    def assert_hidden(self, response, status):
        self.assertEqual(response.status_code, status, response.content)
        self.assertEqual(response['Cache-Control'], 'no-store')
        self.assertEqual(set(response.json()), {'schema_version', 'error'})
        self.assertNotIn(self.values['text'], response.content.decode())
        self.assertNotIn(str(self.values['message_id']), response.content.decode())

    def assert_project_hidden_like_by_id(self, sent, *, query=None):
        # Project scope в существующем by-id скрывается через private 404,
        # а не permission 403: новый query обязан сохранить этот контракт.
        response = self.get(query=query)
        self.assert_hidden(response, 404)
        by_id = Client(HTTP_AUTHORIZATION='Token '+self.sender_auth.value).get(
            '/api/v1/deliveries/'+str(sent.id), secure=True)
        self.assert_hidden(by_id, 404)
        self.assertEqual(response.json(), by_id.json())

    def snapshot(self):
        return {model.__name__: list(model.objects.order_by('pk').values()) for model in
                (m.ClientSession, m.Endpoint, m.ConnectionLease, m.MessageDelivery, m.DeliveryReceipt, Operation)}

    @contextmanager
    def metadata_only(self):
        before = self.snapshot()
        with patch.object(self.provider, 'encrypt', side_effect=AssertionError('metadata encrypted')) as encrypt, \
                patch.object(self.provider, 'decrypt', side_effect=AssertionError('metadata decrypted')) as decrypt, \
                patch('mop.server.controller.adminservices.credential_client',
                      side_effect=AssertionError('metadata called protected service')) as protected, \
                CaptureQueriesContext(connection) as queries:
            yield
        self.assertEqual(self.snapshot(), before, 'metadata query changed durable delivery state')
        self.assertEqual(m.DeliveryReceipt.objects.count(), len(before['DeliveryReceipt']))
        encrypt.assert_not_called()
        decrypt.assert_not_called()
        protected.assert_not_called()
        self.assertFalse(any(q['sql'].lstrip().split(' ', 1)[0].upper() in
            ('INSERT', 'UPDATE', 'DELETE') for q in queries), 'metadata issued a database write')

    def committed(self, body):
        async def run(client, server):
            sent = await asyncio.to_thread(self.send, client)
            await server.runtime.database(body, sent)
        self.run_socket(run)

    def test_actual_route_requires_current_authentication_not_missing_route(self):
        response = Client().get('/api/v1/deliveries', self.query(), secure=True)
        self.assertIn(response.status_code, (401, 403), response.content)
        self.assertEqual(response['Cache-Control'], 'no-store')

    def test_protected_commit_then_sender_key_read_is_exact_readonly_metadata(self):
        def body(sent):
            expected = endpointviews.result(sent)
            self.assertNotEqual(sent.id, sent.message_id)
            with self.metadata_only():
                self.assert_metadata(self.get(), expected)
                self.assert_metadata(self.get(), expected)
                result = self.metadata.delivery_for_message(self.sender_actor,
                    expected_epoch=self.installation.epoch, message_id=sent.message_id)
                self.assertEqual(result, sent)
        self.committed(body)

    def test_committed_acceptance_idle_and_expired_payload_remain_readonly_evidence(self):
        async def run(client, server):
            sent = await asyncio.to_thread(self.send, client)
            frame = await asyncio.to_thread(self.take, client)
            self.assertEqual(frame.id, sent.id)
            accepted = await server.runtime.database(self.metadata.acknowledge, self.context, sent.id,
                                                     outcome='accepted')
            def check(value):
                with self.metadata_only():
                    self.assert_metadata(self.get(), endpointviews.result(value))
            await server.runtime.database(check, accepted)
            idle = await server.runtime.database(self.metadata.idle, self.context, sent.id)
            self.assertTrue(idle.idle)
            await server.runtime.database(check, idle)
            from django.utils import timezone
            from datetime import timedelta
            pending = await asyncio.to_thread(self.send, client, message_id=uuid4())
            await server.runtime.database(lambda: m.MessageDelivery.objects.filter(pk=pending.id).update(
                not_after=timezone.now()-timedelta(seconds=1)))
            # GET не является retention sweeper: queued и ciphertext не меняются.
            def expired():
                with self.metadata_only():
                    self.assert_metadata(self.get(query=self.query(message_id=pending.message_id)),
                                         endpointviews.result(pending))
            await server.runtime.database(expired)
        self.run_socket(run)

    def test_same_message_key_isolated_by_sender_and_recipient_by_id_is_unchanged(self):
        async def run(client, server):
            first = await asyncio.to_thread(self.send, client)
            second = await asyncio.to_thread(client.send_private_report, self.auth,
                **{**self.values, 'sender_endpoint_id': None, 'sender_connection_nonce': None})
            def check():
                with self.metadata_only():
                    self.assert_metadata(self.get(), endpointviews.result(first))
                    self.assert_metadata(self.get(auth=self.auth), endpointviews.result(second))
                    self.assertNotEqual(first.id, second.id)
                    by_id = Client(HTTP_AUTHORIZATION='Token '+self.auth.value).get(
                        '/api/v1/deliveries/'+str(first.id), secure=True)
                    self.assert_metadata(by_id, endpointviews.result(first))
            await server.runtime.database(check)
        self.run_socket(run)

    def test_recipient_cannot_lookup_sender_key_and_unknown_is_not_commit_evidence(self):
        def body(sent):
            with self.metadata_only():
                self.assert_hidden(self.get(auth=self.auth), 404)
                self.assert_hidden(self.get(query=self.query(message_id=uuid4())), 404)
                self.assertEqual(m.MessageDelivery.objects.get(pk=sent.id).state, sent.state)
        self.committed(body)

    def test_query_is_finite_single_valued_and_canonical(self):
        base = list(self.query().items())
        invalid = []
        for key in ('sender_user_id', 'sender_id', 'user_id', 'actor', 'grant_id', 'credential_id',
                    'project_id', 'delivery_id', 'text', 'not_after'):
            invalid.append(base+[(key, 'forbidden')])
        for key in self.query():
            invalid.extend(([item for item in base if item[0] != key], base+[(key, self.query()[key])]))
        for value in ('true', 'True', 'false', '0', '01', '1.0', '2', 'NaN', 'Infinity', ''):
            invalid.append([(key, value if key == 'schema_version' else original) for key, original in base])
        for field in ('epoch', 'message_id'):
            raw = self.query()[field]
            for value in (raw.upper(), raw.replace('-', ''), '{'+raw+'}', 'urn:uuid:'+raw,
                          ' '+raw, raw+' ', 'true', '1', 'NaN', 'null', ''):
                invalid.append([(key, value if key == field else original) for key, original in base])
        with self.metadata_only():
            for values in invalid:
                with self.subTest(values=values):
                    response = Client(HTTP_AUTHORIZATION='Token '+self.sender_auth.value).get(
                        '/api/v1/deliveries?'+urlencode(values), secure=True)
                    self.assert_hidden(response, 400)

    def test_owner_query_requires_trusted_actor_and_uuid_objects(self):
        from mop.server.sessions.endpoints import Endpoints
        self.assertTrue(callable(getattr(Endpoints, 'delivery_for_message', None)),
                        'Sessions sender-key metadata query is missing')
        for value in (True, 1, None, str(self.values['message_id'])):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.metadata.delivery_for_message(self.sender_actor,
                    expected_epoch=self.installation.epoch, message_id=value)
            with self.subTest(epoch=value), self.assertRaises(ValueError):
                self.metadata.delivery_for_message(self.sender_actor,
                    expected_epoch=value, message_id=self.values['message_id'])
        with self.assertRaises(PermissionError):
            self.metadata.delivery_for_message({'user_id': self.sender_user.pk},
                expected_epoch=self.installation.epoch, message_id=self.values['message_id'])

    def test_current_permissions_and_membership_are_rechecked_without_payload(self):
        def body(sent):
            for app, code in (('execution', 'view_delivery'), ('execution', 'deliver_report'),
                              ('catalog', 'view_project')):
                permission = Permission.objects.get(content_type__app_label=app, codename=code)
                self.assert_metadata(self.get(), endpointviews.result(sent))
                self.sender_user.user_permissions.remove(permission)
                with self.metadata_only():
                    self.assert_hidden(self.get(), 403)
                self.sender_user.user_permissions.add(permission)
            a.ProjectMembership.objects.filter(user=self.sender_user).delete()
            with self.metadata_only():
                self.assert_project_hidden_like_by_id(sent)
        self.committed(body)

    def test_foreign_project_is_not_disclosed_even_for_same_sender(self):
        def body(sent):
            m.Endpoint.objects.filter(pk=self.endpoint.id).update(project=self.foreign_project)
            with self.metadata_only():
                self.assert_project_hidden_like_by_id(sent)
        self.committed(body)

    def test_revoked_credential_and_identity_do_not_disclose_metadata(self):
        def body(sent):
            get_user_model().objects.filter(pk=self.sender_user.pk).update(is_active=False)
            with self.metadata_only():
                self.assert_hidden(self.get(), 401)
            get_user_model().objects.filter(pk=self.sender_user.pk).update(is_active=True)
            access.revoke_token(self.sender_actor)
            with self.metadata_only():
                self.assert_hidden(self.get(), 401)
                with self.assertRaises(PermissionError):
                    self.metadata.delivery_for_message(self.sender_actor,
                        expected_epoch=self.installation.epoch, message_id=sent.message_id)
        self.committed(body)

    def test_current_sender_identity_not_original_credential_selects_the_row(self):
        def body(sent):
            access.revoke_token(self.sender_actor)
            from mop.common.credentials import Credential
            current = Credential('token', access.issue_token(self.sender_user.pk))
            with self.metadata_only():
                self.assert_metadata(self.get(auth=current), endpointviews.result(sent))
        self.committed(body)

    def test_old_epoch_row_still_requires_current_project_metadata_authorization(self):
        def body(sent):
            Installation.objects.update(epoch=uuid4())
            epoch = Installation.objects.get().epoch
            m.Endpoint.objects.filter(pk=self.endpoint.id).update(project=self.foreign_project)
            with self.metadata_only():
                self.assert_project_hidden_like_by_id(sent, query=self.query(epoch=epoch))
        self.committed(body)

    def test_restore_guard_and_request_or_stored_epoch_are_not_empty_results(self):
        def body(sent):
            with self.metadata_only():
                self.assert_hidden(self.get(query=self.query(epoch=uuid4())), 409)
            RestoreGuard.objects.update(state='restoring')
            with self.metadata_only():
                self.assert_hidden(self.get(), 503)
            RestoreGuard.objects.update(state='ready')
            Installation.objects.update(epoch=uuid4())
            current = Installation.objects.get().epoch
            with self.metadata_only():
                self.assert_hidden(self.get(), 409)
                self.assert_hidden(self.get(query=self.query(epoch=current)), 409)
                self.assert_hidden(self.get(query=self.query(epoch=current, message_id=uuid4())), 404)
            # effects gate не стирает уже сохранённое metadata evidence.
            Installation.objects.update(epoch=self.installation.epoch, effects_enabled=False)
            with self.metadata_only():
                self.assert_metadata(self.get(), endpointviews.result(sent))
        self.committed(body)

    def test_verified_https_lost_response_after_commit_recovers_by_one_post_then_get(self):
        import aiohttp
        import uvicorn
        from uvicorn.protocols.http.h11_impl import H11Protocol
        from mop.server.controller.asgi import ControllerApplication
        cert, key = Path(self.directory.name)/'tls.crt', Path(self.directory.name)/'tls.key'
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
            '-keyout', str(key), '-out', str(cert), '-subj', '/CN=localhost',
            '-addext', 'subjectAltName=DNS:localhost,IP:127.0.0.1'], check=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        async def run(client, daemon):
            calls, committed, transports = [], [], {}
            app = ControllerApplication(startup=lambda: None)
            class LostReportProtocol(H11Protocol):
                def connection_made(inner, transport):
                    super().connection_made(transport)
                    transports[inner.client] = transport
                def connection_lost(inner, error):
                    transports.pop(inner.client, None)
                    super().connection_lost(error)
            async def lose_response(scope, receive, send):
                if scope['type'] != 'http' or scope['path'] != '/api/v1/reports':
                    return await app(scope, receive, send)
                calls.append(scope['method'])
                async def discard_response(message):
                    if message['type'] == 'http.response.start':
                        committed.append(message['status'])
                await app(scope, receive, discard_response)
                # Только после завершения POST/COMMIT закрываем настоящий TLS
                # transport: ни body, ни Location с delivery_id клиент не получил.
                # H11 ожидаемо диагностирует отсутствие response.start у injector;
                # это искусственная потеря ответа, не ошибка production приложения.
                transports[scope['client']].close()
            listener = socket.socket()
            listener.bind(('127.0.0.1', 0))
            listener.setblocking(False)
            server = uvicorn.Server(uvicorn.Config(lose_response, lifespan='on', log_level='error',
                http=LostReportProtocol, access_log=False, ssl_certfile=str(cert), ssl_keyfile=str(key)))
            records = []
            class Capture(logging.Handler):
                def emit(inner, record): records.append(record.getMessage())
            capture = Capture()
            logger = logging.getLogger('uvicorn.error')
            logger.addHandler(capture)
            task = asyncio.create_task(server.serve(sockets=[listener]))
            try:
                for _ in range(100):
                    if server.started: break
                    await asyncio.sleep(.02)
                self.assertTrue(server.started)
                tls = ssl.create_default_context(cafile=str(cert))
                origin = 'https://localhost:'+str(listener.getsockname()[1])
                headers = {'Authorization': 'Token '+self.sender_auth.value}
                body = dict(schema_version=1, epoch=str(self.installation.epoch), address=self.endpoint.address,
                    message_id=str(self.values['message_id']), text=self.values['text'],
                    not_after=self.values['not_after'].isoformat(), operation_request_id=str(self.values['operation_request_id']))
                async with aiohttp.ClientSession(trust_env=False, connector=aiohttp.TCPConnector(ssl=tls)) as http:
                    with self.assertRaises(aiohttp.ServerDisconnectedError):
                        async with http.post(origin+'/api/v1/reports', json=body, headers=headers):
                            self.fail('lost POST exposed an HTTP response or delivery Location')
                    self.assertEqual(calls, ['POST'])
                    self.assertEqual(committed, [202])
                    row = await daemon.runtime.database(lambda: m.MessageDelivery.objects.values(
                        'id', 'message_id', 'operation_request_id', 'state').get(
                        sender_user_id=self.sender_user.pk, message_id=self.values['message_id']))
                    self.assertEqual(row['state'], 'queued')
                    self.assertNotEqual(row['id'], row['message_id'])
                    before = await daemon.runtime.database(self.snapshot)
                    self.assertEqual(len(before['MessageDelivery']), 1)
                    self.assertEqual(before['DeliveryReceipt'], [])
                    with patch.object(self.provider, 'encrypt', side_effect=AssertionError('GET encrypted')) as encrypt, \
                            patch.object(self.provider, 'decrypt', side_effect=AssertionError('GET decrypted')) as decrypt, \
                            patch('mop.server.controller.adminservices.credential_client',
                                  side_effect=AssertionError('GET called crypto daemon')) as protected:
                        async with http.get(origin+'/api/v1/deliveries', params=self.query(), headers=headers) as response:
                            self.assertEqual(response.status, 200, await response.text())
                            self.assertEqual(response.headers['Cache-Control'], 'no-store')
                            self.assertEqual(await response.json(), dict(schema_version=1, delivery=dict(
                                id=str(row['id']), message_id=str(row['message_id']),
                                operation_request_id=str(row['operation_request_id']), state='queued', idle=False)))
                        encrypt.assert_not_called(); decrypt.assert_not_called(); protected.assert_not_called()
                    self.assertEqual(await daemon.runtime.database(self.snapshot), before)
                    self.assertEqual(calls, ['POST'])
            finally:
                server.should_exit = True
                await asyncio.wait_for(task, 7)
                listener.close()
                logger.removeHandler(capture)
            for secret in (self.values['text'], self.sender_auth.value):
                self.assertFalse(any(secret in record for record in records), 'listener disclosed private input')
        bootstrap = {**settings.BOOTSTRAP, 'role': 'web',
            'credential_service': dict(path=self.path, server_uid=os.geteuid())}
        with override_settings(BOOTSTRAP=bootstrap):
            self.run_socket(run)
