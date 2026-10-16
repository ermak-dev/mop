"""Поведенческий контракт #649: PostgreSQL fences и настоящий собственный UDS."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import importlib.util
import json
import os
from pathlib import Path
import tempfile
from threading import Barrier
from uuid import uuid4

from django.apps import apps
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.db import connections
from django.test import TransactionTestCase, Client, override_settings
from django.utils import timezone

# FEATURE: private endpoint identity, живой lease и подтверждение CLI разделены.
# HYPOTHESIS: presence row/socket publish сейчас не имеют общего durable receipt.
# SPEC: два MCP одного externalSessionID объединяются только внутри actual user/client;
#       timeout/restart не разрешает повтор сообщения; nonce не заменяет Django auth.
# STATUS: RED pending — see #649.

@override_settings(ALLOWED_HOSTS=['testserver'])
class EndpointBoundary(TransactionTestCase):
    def test_presence_and_send_are_authenticated_routes_not_missing_routes(self):
        for method, path in [('get', '/api/v1/addresses'), ('post', '/api/v1/reports'),
                             ('post', '/api/v1/endpoints')]:
            result = getattr(Client(), method)(path, {}, content_type='application/json', secure=True)
            self.assertIn(result.status_code, (401, 403), path)


class Endpoints(TransactionTestCase):
    serialized_rollback = True

    def setUp(self):
        self.assertIsNotNone(apps.get_app_config('execution').get_models())
        self.assertTrue(any(m.__name__ == 'ClientSession' for m in apps.get_app_config('execution').get_models()),
                        'ClientSession/Endpoint delivery schema must exist')
        from mop.server.sessions import endpoints as s, endpoint_models as m
        from mop.server.access import services as auth, models as a
        from mop.server.catalog.models import Project, ClientRegistration
        from mop.server.controller.runtime import initialize
        from mop.server.controller.models import Installation
        from mop.server.secrets.crypto import XChaCha20Provider, Keyring
        initialize()
        self.installation = Installation.objects.get()
        self.m, self.s, self.auth, self.a = m, s, auth, a
        self.project = Project.objects.create(name='one', origin='https://example.invalid/one.git')
        self.other_project = Project.objects.create(name='two', origin='https://example.invalid/two.git')
        self.source = a.IdentitySource.objects.create(name='local', kind='local')
        self.user, self.actor, self.key = self.human('one')
        self.other_user, self.other_actor, self.other_key = self.human('two')
        self.client = ClientRegistration.objects.create(installation=self.installation, label='same')
        self.other_client = ClientRegistration.objects.create(installation=self.installation, label='same')
        a.ClientOwnership.objects.create(client=self.client, user=self.user)
        a.ClientOwnership.objects.create(client=self.other_client, user=self.other_user)
        self.api = s.Endpoints(provider=XChaCha20Provider(Keyring('test', {'test': b'k'*32})))
        self.external = 'external-conversation-1'
        self.endpoint = self.register()

    def human(self, name):
        user = get_user_model().objects.create_user(name)
        self.a.Principal.objects.create(user=user, kind='human')
        self.a.LoginIdentity.objects.create(user=user, source=self.source, external_subject=name)
        self.a.ProjectMembership.objects.create(user=user, project=self.project, source=self.source)
        user.user_permissions.add(*Permission.objects.filter(content_type__app_label='execution',
            codename__in=('register_endpoint', 'view_delivery', 'deliver_report')))
        user.user_permissions.add(Permission.objects.get(content_type__app_label='catalog', codename='view_project'))
        key = self.auth.issue_token(user.pk)
        return user, self.auth.token_context(key), key

    def register(self, **kwargs):
        values = dict(client_id=self.client.pk, project_id=self.project.pk,
                      external_session_id=self.external, display_name='same')
        values.update(kwargs)
        return self.api.register(self.actor, **values)

    def connect(self, endpoint=None, actor=None, gateway=None):
        return self.api.connect(actor or self.actor, (endpoint or self.endpoint).id,
                                gateway_id=gateway or uuid4())

    def send(self, **kwargs):
        values = dict(address=self.endpoint.address, message_id=uuid4(), text='private message',
                      not_after=timezone.now()+timedelta(minutes=1))
        values.update(kwargs)
        return self.api.send(self.actor, **values)

    def test_same_external_conversation_deduplicates_not_display_name(self):
        same = self.register(display_name='changed')
        self.assertEqual(same.id, self.endpoint.id)
        self.assertEqual(self.m.ClientSession.objects.count(), 1)
        separate = self.register(external_session_id='different')
        self.assertNotEqual(separate.id, same.id)
        other = self.api.register(self.other_actor, client_id=self.other_client.pk,
            project_id=self.project.pk, external_session_id=self.external, display_name='same')
        self.assertNotEqual(other.id, same.id)
        self.assertEqual(len(self.api.addresses(self.actor, self.project.pk)), 2)
        self.assertEqual(len(self.api.addresses(self.other_actor, self.project.pk)), 1)

    def test_client_project_and_grant_payload_cannot_choose_human_owner(self):
        from dataclasses import replace
        from mop.server.access.context import PrincipalKind
        with self.assertRaises(PermissionError):
            self.register(client_id=self.other_client.pk)
        with self.assertRaises(PermissionError):
            self.register(project_id=self.other_project.pk)
        with self.assertRaises(PermissionError):
            self.api.register(replace(self.actor, kind=PrincipalKind.GRANT), client_id=self.client.pk,
                project_id=self.project.pk, external_session_id='forged', display_name='same')
        response = Client().post('/api/v1/endpoints', {'schema_version': 1, 'owner': self.other_user.pk},
            HTTP_AUTHORIZATION='Token '+self.key, content_type='application/json', secure=True)
        self.assertEqual(response.status_code, 400)

    def test_database_presence_is_not_live_or_delivered(self):
        presence = self.api.addresses(self.actor, self.project.pk)[0]
        self.assertFalse(presence.live)
        self.assertEqual(presence.state, 'registered')
        result = self.send()
        self.assertEqual(result.state, 'unavailable')
        self.assertNotEqual(result.message_id, result.id)

    def test_private_send_idempotency_and_operation_correlation(self):
        context = self.connect()
        key, operation = uuid4(), uuid4()
        deadline = timezone.now()+timedelta(minutes=1)
        first = self.send(message_id=key, operation_request_id=operation, not_after=deadline)
        again = self.send(message_id=key, operation_request_id=operation, not_after=deadline)
        self.assertEqual(first.id, again.id)
        self.assertEqual(first.state, 'queued')
        self.assertEqual(first.operation_request_id, operation)
        self.assertNotIn(b'private message', bytes(self.m.MessageDelivery.objects.get(pk=first.id).payload))
        with self.assertRaises(self.s.Conflict):
            self.send(message_id=key, text='different', operation_request_id=operation, not_after=deadline)
        with self.assertRaises(PermissionError):
            self.api.delivery(self.other_actor, first.id)
        self.assertEqual(self.api.trusted_context(self.actor, self.endpoint.id, context.nonce), context)

    def test_connection_context_distinguishes_two_same_user_conversations(self):
        from mop.common.endpoint import TrustedEndpointContext
        first = self.connect()
        second_endpoint = self.register(external_session_id='another')
        second = self.connect(second_endpoint)
        self.assertIs(type(first), TrustedEndpointContext)
        self.assertNotEqual(first.session_id, second.session_id)
        self.assertNotEqual(first.endpoint_id, second.endpoint_id)
        with self.assertRaises(PermissionError):
            self.api.trusted_context(self.actor, second_endpoint.id, first.nonce)
        self.assertEqual(first.user_id, second.user_id)

    def test_independent_gateway_claim_race_has_one_receiver(self):
        barrier = Barrier(2)
        def attempt(_):
            connections.close_all()
            try:
                barrier.wait(timeout=10)
                try:
                    return self.connect()
                except self.s.Conflict:
                    return None
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, range(2)))
        self.assertEqual(sum(x is not None for x in results), 1)
        self.assertEqual(self.m.ConnectionLease.objects.count(), 1)

    def test_restart_fences_late_ack_and_never_replays_unknown_attempt(self):
        old = self.connect()
        report = self.send()
        attempt = self.api.take(old)
        self.assertEqual(attempt.id, report.id)
        self.m.ConnectionLease.objects.filter(endpoint_id=old.endpoint_id).update(
            expires_at=timezone.now()-timedelta(seconds=1))
        new = self.connect()
        self.assertGreater(new.generation, old.generation)
        self.assertIsNone(self.api.take(new))
        self.assertEqual(self.api.delivery(self.actor, report.id).state, 'unknown')
        with self.assertRaises(PermissionError):
            self.api.acknowledge(old, report.id, outcome='accepted')
        self.api.disconnect(old)
        self.assertEqual(self.api.trusted_context(self.actor, self.endpoint.id, new.nonce), new)

    def test_revocation_membership_and_client_ownership_are_current(self):
        context = self.connect()
        report = self.send()
        self.a.ClientOwnership.objects.filter(client=self.client).delete()
        with self.assertRaises(PermissionError):
            self.api.take(context)
        with self.assertRaises(PermissionError):
            self.api.renew(context)
        self.a.ClientOwnership.objects.create(client=self.client, user=self.user)
        self.a.ProjectMembership.objects.filter(user=self.user).delete()
        with self.assertRaises(PermissionError):
            self.api.delivery(self.actor, report.id)
        with self.assertRaises(PermissionError):
            self.api.take(context)

    def test_ack_requires_matching_current_attempt_and_idle_is_separate(self):
        context = self.connect()
        report = self.send(notify_when_idle=True)
        with self.assertRaises(self.s.Conflict):
            self.api.acknowledge(context, report.id, outcome='accepted')
        self.api.take(context)
        result = self.api.acknowledge(context, report.id, outcome='accepted')
        self.assertEqual(result.state, 'accepted')
        self.assertFalse(result.idle)
        self.assertEqual(self.api.acknowledge(context, report.id, outcome='accepted'), result)
        self.api.idle(context, report.id)
        self.assertTrue(self.api.delivery(self.actor, report.id).idle)
        with self.assertRaises(self.s.Conflict):
            self.api.acknowledge(context, report.id, outcome='unavailable')

    def test_receipt_and_idle_cannot_cross_connection_or_receiver(self):
        first = self.connect()
        another = self.register(external_session_id='another')
        second = self.connect(another)
        report = self.send(notify_when_idle=True)
        self.api.take(first)
        for operation in (lambda: self.api.acknowledge(second, report.id, outcome='accepted'),
                          lambda: self.api.idle(second, report.id)):
            with self.assertRaises(PermissionError):
                operation()

    def test_bounded_queue_and_payload_limits(self):
        self.connect()
        with self.assertRaises(ValueError):
            self.send(text='x'*65537)
        for _ in range(64):
            self.send()
        with self.assertRaises(self.s.Capacity):
            self.send()

    def test_disconnect_expiry_and_revoked_token_do_not_confirm_delivery(self):
        context = self.connect()
        report = self.send()
        self.api.take(context)
        self.api.disconnect(context)
        self.assertEqual(self.api.delivery(self.actor, report.id).state, 'unknown')
        new = self.connect()
        self.auth.revoke_token(self.actor)
        with self.assertRaises(PermissionError):
            self.api.take(new)
        with self.assertRaises(PermissionError):
            self.api.acknowledge(new, report.id, outcome='accepted')

    def test_rest_private_send_and_ack_have_finite_schemas(self):
        context = self.connect()
        http = Client(HTTP_AUTHORIZATION='Token '+self.key)
        response = http.post('/api/v1/reports', {'schema_version': 1,
            'address': self.endpoint.address, 'message_id': str(uuid4()), 'text': 'private',
            'not_after': (timezone.now()+timedelta(minutes=1)).isoformat(), 'notify_when_idle': True},
            content_type='application/json', secure=True)
        self.assertEqual(response.status_code, 202, response.content)
        self.assertEqual(response['Cache-Control'], 'no-store')
        report = response.json()['delivery']
        self.assertEqual(report['state'], 'queued')
        self.api.take(context)
        path = '/api/v1/deliveries/'+report['id']+'/ack'
        ack = http.post(path, {'schema_version': 1, 'outcome': 'accepted'},
            HTTP_X_MOP_ENDPOINT=str(self.endpoint.id), HTTP_X_MOP_CONNECTION=str(context.nonce),
            content_type='application/json', secure=True)
        self.assertEqual(ack.status_code, 200, ack.content)
        neighbor = Client(HTTP_AUTHORIZATION='Token '+self.other_key).get(
            '/api/v1/deliveries/'+report['id'], secure=True)
        self.assertEqual(neighbor.status_code, 404)

    def test_real_unix_bridge_loop_acceptance_unknown_idle_and_shutdown(self):
        from mop.client.local_bridge import OwnSession, LocalBridge
        from mop.server.sessions.delivery import DeliveryGateway
        from mop.server.controller.runtime import Runtime
        async def scenario():
            with tempfile.TemporaryDirectory(prefix='mop-endpoint-') as directory:
                os.chmod(directory, 0o700)
                path = str(Path(directory)/'external.sock')
                received = []
                async def external(reader, writer):
                    try:
                        frame = json.loads(await reader.readline())
                        received.append(frame)
                        callback = frame['from'][4:]
                        r, w = await asyncio.open_unix_connection(callback)
                        w.write((json.dumps({'msgV': 1, 'type': 'control', 'action': 'message_accepted',
                            'orig_msg_id': frame['msg_id']})+'\n').encode())
                        await w.drain()
                        w.close()
                        await w.wait_closed()
                    finally:
                        writer.close()
                        await writer.wait_closed()
                server = await asyncio.start_unix_server(external, path=path)
                os.chmod(path, 0o600)
                bridge = LocalBridge(OwnSession(path, os.getpid(), self.external), timeout=.3)
                runtime = Runtime(adapters=(bridge,))
                gateway = DeliveryGateway(runtime, self.api)
                await runtime.start()
                try:
                    context = await gateway.attach(self.actor, self.endpoint.id, bridge)
                    report = await runtime.database(self.send)
                    result = await gateway.deliver_one(context)
                    self.assertEqual(result.state, 'accepted')
                    self.assertEqual(received[0]['msg_id'], str(report.message_id))
                    self.assertEqual(received[0]['type'], 'user')
                    self.assertIsNone(await gateway.deliver_one(context))
                    server.close()
                    await server.wait_closed()
                    os.unlink(path)
                    report = await runtime.database(self.send)
                    result = await gateway.deliver_one(context)
                    self.assertEqual(result.state, 'unavailable')
                    self.assertIsNone(await gateway.deliver_one(context))
                    await gateway.detach(context)
                finally:
                    await gateway.aclose()
                    await runtime.aclose()
                    server.close()
                    await server.wait_closed()
                self.assertEqual(runtime.pending, 0)
                self.assertEqual(list(Path(directory).glob('*-*.sock')), [])
        asyncio.run(scenario())

    def test_bridge_publish_without_application_ack_is_unknown_and_not_replayed(self):
        from mop.client.local_bridge import OwnSession, LocalBridge
        from mop.common.endpoint import DeliveryFrame
        async def scenario():
            with tempfile.TemporaryDirectory(prefix='mop-endpoint-') as directory:
                os.chmod(directory, 0o700)
                path = str(Path(directory)/'external.sock')
                frames = []
                async def silent(reader, writer):
                    frames.append(json.loads(await reader.readline()))
                    writer.close()
                    await writer.wait_closed()
                server = await asyncio.start_unix_server(silent, path=path)
                os.chmod(path, 0o600)
                bridge = LocalBridge(OwnSession(path, os.getpid(), self.external), timeout=.05)
                await bridge.start()
                try:
                    frame = DeliveryFrame(uuid4(), uuid4(), self.external, 'hello', 'next', False)
                    self.assertEqual((await bridge.deliver(frame)).outcome, 'unknown')
                    self.assertEqual((await bridge.deliver(frame)).outcome, 'unknown')
                    self.assertEqual(len(frames), 1)
                finally:
                    await bridge.aclose()
                    server.close()
                    await server.wait_closed()
        asyncio.run(scenario())

    def test_real_bridge_idle_notice_is_correlated_to_control_not_user_message(self):
        from mop.client.local_bridge import OwnSession, LocalBridge
        from mop.common.endpoint import DeliveryFrame
        async def scenario():
            with tempfile.TemporaryDirectory(prefix='mop-endpoint-') as directory:
                os.chmod(directory, 0o700)
                path = str(Path(directory)/'external.sock')
                frames = []
                async def external(reader, writer):
                    try:
                        frame = json.loads(await reader.readline())
                        frames.append(frame)
                        _, reply = await asyncio.open_unix_connection(frame['from'][4:])
                        action = 'peer_idle_notice' if frame['type'] == 'control' else 'message_accepted'
                        reply.write((json.dumps({'msgV': 1, 'type': 'control', 'action': action,
                            'orig_msg_id': frame['msg_id']})+'\n').encode())
                        await reply.drain()
                        reply.close()
                        await reply.wait_closed()
                    finally:
                        writer.close()
                        await writer.wait_closed()
                server = await asyncio.start_unix_server(external, path=path)
                os.chmod(path, 0o600)
                bridge = LocalBridge(OwnSession(path, os.getpid(), self.external), timeout=.3)
                await bridge.start()
                try:
                    frame = DeliveryFrame(uuid4(), uuid4(), self.external, 'hello', 'next', True)
                    result = await bridge.deliver(frame)
                    self.assertEqual(result.outcome, 'accepted')
                    self.assertTrue(await bridge.wait_idle(frame.message_id, timeout=.3))
                    self.assertEqual(frames[1]['action'], 'notify_when_idle')
                    self.assertNotEqual(frames[0]['msg_id'], frames[1]['msg_id'])
                finally:
                    await bridge.aclose()
                    server.close()
                    await server.wait_closed()
        asyncio.run(scenario())

    def test_subscription_failure_never_leaves_a_live_database_connection(self):
        from mop.server.sessions.delivery import DeliveryGateway
        from mop.server.controller.runtime import Runtime
        class Refusing:
            external_session_id = self.external
            async def ready(self):
                raise ConnectionError('subscription unavailable')
        async def scenario():
            runtime = Runtime()
            gateway = DeliveryGateway(runtime, self.api)
            await runtime.start()
            try:
                with self.assertRaises(ConnectionError):
                    await gateway.attach(self.actor, self.endpoint.id, Refusing())
                self.assertEqual(await runtime.database(self.m.ConnectionLease.objects.count), 0)
            finally:
                await gateway.aclose()
                await runtime.aclose()
        asyncio.run(scenario())

    def test_bridge_and_common_wire_have_no_framework_or_legacy_discovery_import(self):
        for module in ('mop.client.local_bridge', 'mop.common.endpoint'):
            spec = importlib.util.find_spec(module)
            self.assertIsNotNone(spec)
            source = Path(spec.origin).read_text()
            for forbidden in ('from django', 'import django', 'mop.server', 'session.resolve', 'session.registry', 'Nomad'):
                self.assertNotIn(forbidden, source)
