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
# RED: PG16 — 19 behavioral failures (absent ClientSession/routes); отдельно был
#      fixture flush timeout, который не считается доказательством endpoint defect.
# RED: retry пытался шифровать снова; idle_message/root/receiver loop отсутствовали.
# RED: stale queue держала cap, revoked sender client проходил take, reconnect
#      удерживал finished task; epoch отсутствовал, disabled effects не запрещали send.
# SOLUTION: portable ORM identity, bounded async gateway, AEAD retention и own UDS.
# RESULT: PG16 — 24/24 green до добавления autonomous receiver-loop acceptance.
# STATUS: FIXED — see #649; итоговые PG16/17 и exact-SHA CI указаны в отчёте.

@override_settings(ALLOWED_HOSTS=['testserver'])
class EndpointBoundary(TransactionTestCase):
    serialized_rollback = True

    def test_presence_and_send_are_authenticated_routes_not_missing_routes(self):
        for method, path in [('get', '/api/v1/addresses'), ('post', '/api/v1/reports'),
                             ('post', '/api/v1/endpoints')]:
            result = getattr(Client(), method)(path, {}, content_type='application/json', secure=True)
            with self.subTest(path=path):
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
        Installation.objects.update(effects_enabled=True)
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
        from mop.server.controller.endpointviews import authority
        from mop.server.controller.endpointcrypto import EndpointCipher
        self.api = s.Endpoints(authority=authority,
            cipher=EndpointCipher(XChaCha20Provider(Keyring('test', {'test': b'k'*32})), 'isolated-ci'))
        from unittest.mock import patch
        binding = patch('mop.server.controller.endpointviews.endpoints', self.api)
        binding.start()
        self.addCleanup(binding.stop)
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
        values = dict(expected_epoch=self.installation.epoch,
                      address=self.endpoint.address, message_id=uuid4(), text='private message',
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

    def test_independent_restart_gateways_fence_one_old_attempt_without_replay(self):
        old = self.connect()
        report = self.send()
        self.api.take(old)
        self.m.ConnectionLease.objects.filter(endpoint_id=old.endpoint_id).update(
            expires_at=timezone.now()-timedelta(seconds=1))
        barrier = Barrier(2)
        def restart(_):
            connections.close_all()
            try:
                barrier.wait(timeout=10)
                try:
                    return self.connect(gateway=uuid4())
                except self.s.Conflict:
                    return None
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as workers:
            contexts = [value for value in workers.map(restart, range(2)) if value is not None]
        self.assertEqual(len(contexts), 1)
        self.assertGreater(contexts[0].generation, old.generation)
        self.assertIsNone(self.api.take(contexts[0]))
        self.assertEqual(self.api.delivery(self.actor, report.id).state, 'unknown')
        with self.assertRaises(PermissionError):
            self.api.acknowledge(old, report.id, outcome='accepted')

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

    def test_stale_sender_epoch_cannot_recreate_a_message_lost_by_database_restore(self):
        from mop.server.controller.models import Installation
        from unittest.mock import patch
        old_epoch = self.installation.epoch
        Installation.objects.update(epoch=uuid4())
        with patch.object(self.api.cipher, 'seal', side_effect=AssertionError('stale epoch must not reach cipher')):
            with self.assertRaises(self.s.Conflict):
                self.api.send(self.actor, address=self.endpoint.address, message_id=uuid4(),
                    expected_epoch=old_epoch, text='old retry', not_after=timezone.now()+timedelta(minutes=1))
        self.assertEqual(self.m.MessageDelivery.objects.count(), 0)

    def test_effect_gate_blocks_new_reports_and_rechecks_before_actual_dispatch(self):
        from mop.server.controller.models import Installation
        context = self.connect()
        queued = self.send()
        Installation.objects.update(effects_enabled=False)
        with self.assertRaises(self.s.Unavailable):
            self.send()
        with self.assertRaises(self.s.Unavailable):
            self.api.take(context)
        Installation.objects.update(effects_enabled=True)
        self.api.take(context)
        Installation.objects.update(effects_enabled=False)
        with self.assertRaises(self.s.Unavailable):
            self.api.before_dispatch(context, queued.id)

    def test_expired_queue_does_not_hold_global_admission_or_retain_message_bytes(self):
        self.connect()
        for _ in range(64):
            self.send()
        self.m.MessageDelivery.objects.filter(state='queued').update(not_after=timezone.now()-timedelta(seconds=1))
        report = self.send()
        self.assertEqual(report.state, 'queued')
        self.assertEqual(self.m.MessageDelivery.objects.filter(state='queued').count(), 1)
        self.assertFalse(self.m.MessageDelivery.objects.filter(state='unavailable', payload__isnull=False).exists())

    def test_disconnected_senders_client_cannot_authorize_queued_private_content(self):
        self.connect()
        sender_endpoint = self.api.register(self.other_actor, client_id=self.other_client.pk,
            project_id=self.project.pk, external_session_id='other-cli', display_name='same')
        sender = self.connect(sender_endpoint, self.other_actor)
        report = self.api.send(self.other_actor, expected_epoch=self.installation.epoch,
            address=self.endpoint.address, message_id=uuid4(),
            text='addressed report', not_after=timezone.now()+timedelta(minutes=1), sender=sender)
        self.a.ClientOwnership.objects.filter(client=self.other_client).delete()
        receiver = self.api.trusted_context(self.actor, self.endpoint.id,
            self.m.ConnectionLease.objects.get(endpoint_id=self.endpoint.id).nonce)
        self.assertIsNone(self.api.take(receiver))
        self.assertEqual(self.api.delivery(self.actor, report.id).state, 'unavailable')

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
        response = http.post('/api/v1/reports', {'schema_version': 1, 'epoch': str(self.installation.epoch),
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
                        line = await reader.readline()
                        if not line:
                            return
                        frame = json.loads(line)
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
                    Path(path).unlink(missing_ok=True)
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
                    try:
                        line = await reader.readline()
                        if line:
                            frames.append(json.loads(line))
                    finally:
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
                        line = await reader.readline()
                        if not line:
                            return
                        frame = json.loads(line)
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

    def test_root_receiver_loop_delivers_real_socket_and_correlated_idle(self):
        from mop.client.local_bridge import OwnSession, LocalBridge
        from mop.server.controller.endpointroot import create_endpoint_runtime
        async def scenario():
            with tempfile.TemporaryDirectory(prefix='mop-endpoint-') as directory:
                os.chmod(directory, 0o700)
                path = str(Path(directory)/'external.sock')
                frames = []
                async def external(reader, writer):
                    try:
                        line = await reader.readline()
                        if not line:
                            return
                        frame = json.loads(line)
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
                runtime, gateway = create_endpoint_runtime(self.api)
                await runtime.start()
                try:
                    bridge = LocalBridge(OwnSession(path, os.getpid(), self.external), timeout=.3)
                    context = await gateway.attach(self.actor, self.endpoint.id, bridge)
                    task = gateway.start_receiver(context)
                    report = await runtime.database(self.send, notify_when_idle=True)
                    async def acknowledged():
                        while True:
                            status = await runtime.database(self.api.delivery, self.actor, report.id)
                            if status.state == 'accepted' and status.idle:
                                return status
                            await asyncio.sleep(.01)
                    await asyncio.wait_for(acknowledged(), 3)
                    self.assertEqual(frames[0]['msg_id'], str(report.message_id))
                    self.assertEqual(frames[1]['action'], 'notify_when_idle')
                    await runtime.aclose()
                    self.assertTrue(task.done())
                    self.assertEqual(runtime.pending, 0)
                finally:
                    await runtime.aclose()
                    server.close()
                    await server.wait_closed()
        asyncio.run(scenario())

    def test_duplicate_identity_survives_cipher_outage_and_expired_presence(self):
        from unittest.mock import patch
        self.connect()
        message_id, deadline = uuid4(), timezone.now()+timedelta(minutes=1)
        first = self.send(message_id=message_id, not_after=deadline)
        self.m.Endpoint.objects.filter(pk=self.endpoint.id).update(expires_at=timezone.now()-timedelta(seconds=1))
        with patch.object(self.api.cipher, 'seal', side_effect=AssertionError('retry must not encrypt again')):
            self.assertEqual(self.send(message_id=message_id, not_after=deadline), first)

    def test_slow_delivery_does_not_hold_sql_transaction_and_rechecks_revocation(self):
        from mop.common.endpoint import ApplicationAcceptance
        from mop.server.sessions.delivery import DeliveryGateway
        from mop.server.controller.runtime import Runtime
        async def scenario():
            runtime = Runtime()
            entered, release = asyncio.Event(), asyncio.Event()
            class Peer:
                external_session_id = self.external
                async def start(self): pass
                async def ready(self): return self.external_session_id
                async def aclose(self): pass
                async def deliver(self, frame):
                    entered.set()
                    await release.wait()
                    return ApplicationAcceptance('accepted')
            gateway = DeliveryGateway(runtime, self.api)
            await runtime.start()
            try:
                context = await gateway.attach(self.actor, self.endpoint.id, Peer())
                report = await runtime.database(self.send)
                task = asyncio.create_task(gateway.deliver_one(context))
                await asyncio.wait_for(entered.wait(), 2)
                # Независимый SQL worker получает тот же ресурс пока socket ждёт.
                await asyncio.wait_for(runtime.database(self.a.ProjectMembership.objects.filter(user=self.user).delete), 2)
                release.set()
                with self.assertRaises(PermissionError):
                    await task
                row = await runtime.database(lambda: self.m.MessageDelivery.objects.get(pk=report.id).state)
                self.assertEqual(row, 'attempting')
                await gateway.detach(context)
                self.assertEqual(await runtime.database(lambda: self.m.MessageDelivery.objects.get(pk=report.id).state), 'unknown')
            finally:
                release.set()
                await gateway.aclose()
                await runtime.aclose()
        asyncio.run(scenario())

    def test_composition_root_owns_gateway_clients_and_shutdown_leases(self):
        from mop.server.controller.endpointroot import create_endpoint_runtime
        # ORM assertion идёт после остановки event loop, не через async unsafe.
        async def wrapped():
            runtime, gateway = create_endpoint_runtime(self.api)
            closed = []
            class Peer:
                external_session_id = self.external
                async def start(self): pass
                async def ready(self): return self.external_session_id
                async def aclose(self): closed.append(True)
            await runtime.start()
            context = await gateway.attach(self.actor, self.endpoint.id, Peer())
            await runtime.aclose()
            self.assertEqual(closed, [True])
            self.assertEqual(runtime.pending, 0)
            return context
        context = asyncio.run(wrapped())
        self.assertFalse(self.m.ConnectionLease.objects.get(endpoint_id=context.endpoint_id).connected)

    def test_receiver_reconnects_do_not_accumulate_finished_tasks(self):
        from mop.server.controller.endpointroot import create_endpoint_runtime
        async def scenario():
            runtime, gateway = create_endpoint_runtime(self.api)
            class Peer:
                external_session_id = self.external
                async def start(self): pass
                async def ready(self): return self.external_session_id
                async def aclose(self): pass
            await runtime.start()
            try:
                for _ in range(3):
                    context = await gateway.attach(self.actor, self.endpoint.id, Peer())
                    task = gateway.start_receiver(context)
                    await gateway.detach(context)
                    await task
                    self.assertEqual(len(gateway._loops), 0, 'finished receiver tasks must leave the owned set')
            finally:
                await runtime.aclose()
        asyncio.run(scenario())

    def test_idle_lookup_cannot_pair_another_messages_notice(self):
        first = self.connect()
        report = self.send(notify_when_idle=True)
        self.api.take(first)
        self.api.acknowledge(first, report.id, outcome='accepted')
        self.assertEqual(self.api.idle_message(first, report.id), report.message_id)
        plain = self.send()
        self.api.take(first)
        self.api.acknowledge(first, plain.id, outcome='accepted')
        with self.assertRaises(self.s.Conflict):
            self.api.idle_message(first, plain.id)

    def test_bridge_and_common_wire_have_no_framework_or_legacy_discovery_import(self):
        for module in ('mop.client.local_bridge', 'mop.common.endpoint'):
            spec = importlib.util.find_spec(module)
            self.assertIsNotNone(spec)
            source = Path(spec.origin).read_text()
            for forbidden in ('from django', 'import django', 'mop.server', 'session.resolve', 'session.registry', 'Nomad'):
                self.assertNotIn(forbidden, source)
