"""Реальные PostgreSQL/AEAD/protected UDS для private application commands.

HYPOTHESIS: default endpoint root не имеет cipher; web не должен получать root.
SPEC: только send report / take current receiver, SQL authority из real credential;
после crypto заново receiver/sender/installation/restore/lease, без replay.
Ни caller ActorContext, ни произвольных ciphertext/AAD, ни I/O под SQL lock.
SOLUTION: credential daemon связывает Sessions owner с AEAD; rootless frontend
передаёт credential и locators, но не конструирует cipher/provider.
RESULT: raw commands/default binding дали red; HTTP input/domain verdicts и
raw provider leak получили отдельные red. Итоговый PG17 прогон: 322 проверки
и отдельно четыре UID случая green, fixture завершилась с кодом 0.
STATUS: FIXED — see #669 (finite private ports; WS/native production binding отдельно).
"""
import asyncio
from datetime import timedelta
import os
from pathlib import Path
import tempfile
from threading import Event
from unittest.mock import patch
from uuid import uuid4

from asgiref.sync import async_to_sync
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.db import connection, transaction
from django.test import TransactionTestCase, Client, override_settings
from django.utils import timezone

from mop.common import credentials as p
from mop.common.credential_client import CredentialClient
from mop.server.access import models as a, services as access
from mop.server.catalog.models import Project, ClientRegistration, NodeProject
from mop.server.controller import credentials as service, endpointviews
from mop.server.controller.models import Installation
from mop.server.controller.runtime import Runtime, initialize
from mop.server.secrets.crypto import XChaCha20Provider, Keyring
from mop.server.sessions import endpoint_models as m


class EndpointCredentialFixture(TransactionTestCase):
    serialized_rollback = True

    def setUp(self):
        initialize()
        Installation.objects.update(effects_enabled=True)
        self.installation = Installation.objects.get(pk=1)
        self.source = a.IdentitySource.objects.create(name='local', kind='local')
        self.project = Project.objects.create(name='private', origin='https://example.invalid/private.git')
        self.foreign_project = Project.objects.create(name='foreign', origin='https://example.invalid/foreign.git')
        self.user, self.actor, self.auth, self.client = self.human('receiver')
        self.sender_user, self.sender_actor, self.sender_auth, self.sender_client = self.human('sender')
        self.metadata = endpointviews.default_endpoints()
        self.endpoint = self.register(self.actor, self.client, 'receiver-conversation')
        self.sender_endpoint = self.register(self.sender_actor, self.sender_client, 'sender-conversation')
        self.context = self.metadata.connect(self.actor, self.endpoint.id, gateway_id=uuid4())
        self.sender_context = self.metadata.connect(self.sender_actor, self.sender_endpoint.id, gateway_id=uuid4())
        self.directory = tempfile.TemporaryDirectory(prefix='mop-epcred-', dir='/tmp')
        self.addCleanup(self.directory.cleanup)
        self.path = str(Path(self.directory.name) / 'service.sock')
        self.real = XChaCha20Provider(Keyring('fixture', {'fixture': b'k' * 32}))
        self.after_encrypt = self.after_decrypt = lambda: None
        test = self
        class Provider:
            def encrypt(inner, value, context):
                test.assertFalse(connection.in_atomic_block, 'encryption under SQL transaction')
                result = test.real.encrypt(value, context)
                test.after_encrypt()
                return result
            def decrypt(inner, envelope, context):
                test.assertFalse(connection.in_atomic_block, 'decryption under SQL transaction')
                result = test.real.decrypt(envelope, context)
                test.after_decrypt()
                return result
        self.provider = Provider()
        self.values = dict(expected_epoch=self.installation.epoch, address=self.endpoint.address,
            message_id=uuid4(), text='protected-private-report',
            not_after=timezone.now()+timedelta(minutes=1), operation_request_id=uuid4(),
            notify_when_idle=True, priority='now', sender_endpoint_id=self.sender_endpoint.id,
            sender_connection_nonce=self.sender_context.nonce)

    def human(self, name):
        user = get_user_model().objects.create_user(name)
        a.LoginIdentity.objects.create(user=user, source=self.source, external_subject=name)
        a.ProjectMembership.objects.create(user=user, project=self.project, source=self.source)
        user.user_permissions.add(Permission.objects.get_by_natural_key('view_project', 'catalog', 'project'))
        for code in ('register_endpoint', 'view_delivery', 'deliver_report'):
            user.user_permissions.add(Permission.objects.get_by_natural_key(code, 'execution', 'assignment'))
        token = access.issue_token(user.pk)
        client = ClientRegistration.objects.create(installation=self.installation, label=name)
        a.ClientOwnership.objects.create(user=user, client=client)
        return user, access.token_context(token), p.Credential('token', token), client

    def register(self, actor, client, external):
        return self.metadata.register(actor, client_id=client.pk, project_id=self.project.pk,
            external_session_id=external, display_name='not-an-identity')

    async def exercise(self, body, *, app, timeout=5, client_timeout=5):
        runtime = Runtime(database_limit=2, queue_limit=0)
        server = service.CredentialService(self.path, application=app, runtime=runtime,
            allowed_uids=(os.geteuid(),), timeout=timeout)
        await server.start()
        try:
            await body(CredentialClient(self.path, server_uid=os.geteuid(), timeout=client_timeout), server)
        finally:
            await server.aclose()
            await runtime.aclose()
        self.assertFalse(Path(self.path).exists())
        self.assertEqual(runtime.pending, 0)

    def run_socket(self, body, **options):
        with override_settings(BOOTSTRAP={**settings.BOOTSTRAP, 'role': 'credential'}):
            app = service.application(provider=self.provider)
        async_to_sync(self.exercise)(body, app=app, **options)

    def send(self, client, **changes):
        self.assertTrue(callable(getattr(client, 'send_private_report', None)),
            'finite protected send_private_report client is missing')
        return client.send_private_report(self.sender_auth, **{**self.values, **changes})

    def take(self, client, *, auth=None, endpoint=None, nonce=None):
        self.assertTrue(callable(getattr(client, 'take_private_delivery', None)),
            'finite protected take_private_delivery client is missing')
        return client.take_private_delivery(auth or self.auth,
            endpoint_id=endpoint or self.endpoint.id, connection_nonce=nonce or self.context.nonce)

    def request(self, command, args, **changes):
        return dict(version=1, command=command, args=args,
            auth=dict(kind='token', value=self.sender_auth.value), **changes)

    def send_args(self):
        return {key: (value.isoformat() if key == 'not_after' else str(value)
                      if key.endswith('_id') or key in ('expected_epoch', 'sender_connection_nonce') else value)
                for key, value in self.values.items()}

    async def raw(self, request):
        reader, writer = await asyncio.open_unix_connection(self.path)
        try:
            writer.write(p.frame(request))
            await writer.drain()
            async with asyncio.timeout(5):
                return p.loads(await reader.readexactly(p.frame_size(await reader.readexactly(4))))
        finally:
            writer.close()
            await writer.wait_closed()


class EndpointCredentialCases(EndpointCredentialFixture):
    def test_real_socket_accepts_only_application_send_and_addressed_take(self):
        async def body(client, server):
            sent = await self.raw(self.request('send_private_report', self.send_args()))
            self.assertTrue(sent['ok'], sent)
            result = sent['result']
            taken = await self.raw(dict(version=1, command='take_private_delivery',
                args=dict(endpoint_id=str(self.endpoint.id), connection_nonce=str(self.context.nonce)),
                auth=dict(kind='token', value=self.auth.value)))
            self.assertTrue(taken['ok'], taken)
            self.assertEqual(taken['result']['id'], result['id'])
            self.assertEqual(taken['result']['text'], self.values['text'])
        self.run_socket(body)

    def test_typed_commands_preserve_exact_fields_encrypt_sql_and_never_replay_take(self):
        async def body(client, server):
            result = await asyncio.to_thread(self.send, client)
            self.assertEqual(result.message_id, self.values['message_id'])
            self.assertEqual(result.operation_request_id, self.values['operation_request_id'])
            self.assertEqual(result.state, 'queued')
            row = await server.runtime.database(lambda: m.MessageDelivery.objects.values(
                'payload', 'not_after', 'reply_endpoint_id').get(pk=result.id))
            self.assertNotIn(self.values['text'].encode(), bytes(row['payload']))
            self.assertEqual(row['not_after'], self.values['not_after'])
            self.assertEqual(row['reply_endpoint_id'], self.sender_endpoint.id)
            frame = await asyncio.to_thread(self.take, client)
            self.assertEqual((frame.id, frame.message_id, frame.text, frame.priority, frame.notify_when_idle,
                frame.external_session_id, frame.reply_address),
                (result.id, self.values['message_id'], self.values['text'], 'now', True,
                 'receiver-conversation', self.sender_endpoint.address))
            self.assertNotIn(frame.text, repr(frame))
            self.assertNotIn(self.auth.value, repr(client))
            self.assertIsNone(await asyncio.to_thread(self.take, client))
            again = await asyncio.to_thread(self.send, client)
            self.assertEqual(again.id, result.id)
            self.assertEqual(again.state, 'attempting')
            self.assertIsNone(await asyncio.to_thread(self.take, client))
            await server.runtime.database(self.metadata.before_dispatch, self.context, frame.id)
        self.run_socket(body)

    def test_protocol_refuses_other_purpose_version_and_all_caller_authority(self):
        async def body(client, server):
            valid = self.request('send_private_report', self.send_args())
            bad = [dict(valid, version=2), dict(valid, version=True), dict(valid, command='decrypt'),
                   dict(valid, command='encrypt'), dict(valid, command='take_private_delivery')]
            for name in ('actor', 'owner_id', 'user_id', 'node_id', 'project_id', 'context', 'sender',
                         'ciphertext', 'aad', 'purpose', 'credential_id', 'generation', 'gateway_id'):
                bad.append(dict(valid, args={**valid['args'], name: 'untrusted-private-marker'}))
            for request in bad:
                response = await self.raw(request)
                self.assertFalse(response['ok'])
                self.assertNotIn(self.sender_auth.value, str(response))
                self.assertNotIn('untrusted-private-marker', str(response))
            await asyncio.to_thread(self.send, client)
        self.run_socket(body)

    def test_sender_pair_is_optional_but_never_accepts_foreign_connection(self):
        async def body(client, server):
            for changes in (dict(sender_endpoint_id=self.endpoint.id),
                            dict(sender_connection_nonce=self.context.nonce),
                            dict(sender_endpoint_id=None), dict(sender_connection_nonce=None)):
                with self.assertRaises(p.CredentialError):
                    await asyncio.to_thread(self.send, client, **changes)
            result = await asyncio.to_thread(self.send, client,
                sender_endpoint_id=None, sender_connection_nonce=None)
            self.assertEqual(result.state, 'queued')
            self.assertIsNone((await asyncio.to_thread(self.take, client)).reply_address)
        self.run_socket(body)

    # HYPOTHESIS: поиск project через presence ошибочно добавляет sender-у
    # view_delivery, хотя доверенная connection требует register_endpoint,
    # а сам report -- deliver_report. Metadata permission не должна стать
    # новой предпосылкой существующего send с optional conversation.
    # RED: socket вернул CredentialRefused: endpoint action permission required.
    # SOLUTION: публичный trusted_context вместо presence + holder_context;
    # project equality по-прежнему проверяет send после provider wait.
    # RESULT: проверка green в итоговом PG17 прогоне; STATUS: FIXED — see #669.
    def test_sender_connection_does_not_require_metadata_read_permission(self):
        permission = Permission.objects.get_by_natural_key('view_delivery', 'execution', 'assignment')
        self.sender_user.user_permissions.remove(permission)
        with self.assertRaises(access.Denied):
            self.metadata.presence(self.sender_actor, self.sender_endpoint.id)
        async def body(client, server):
            result = await asyncio.to_thread(self.send, client)
            frame = await asyncio.to_thread(self.take, client)
            self.assertEqual(frame.id, result.id)
            self.assertEqual(frame.reply_address, self.sender_endpoint.address)
        self.run_socket(body)

    def test_receiver_exact_token_endpoint_and_nonce_not_os_peer_authority(self):
        replacement_token = access.issue_token(self.user.pk)
        # issue_token возвращает текущий Token; второй credential -- настоящий session.
        from django.contrib.auth import SESSION_KEY, BACKEND_SESSION_KEY, HASH_SESSION_KEY
        from django.contrib.sessions.backends.db import SessionStore
        session = SessionStore()
        session[SESSION_KEY] = str(self.user.pk)
        session[BACKEND_SESSION_KEY] = 'mop.server.controller.authbackend.ControllerBackend'
        session[HASH_SESSION_KEY] = self.user.get_session_auth_hash()
        session.save()
        access.bind_session(self.user.pk, session.session_key)
        different = p.Credential('session', session.session_key)
        self.assertEqual(replacement_token, self.auth.value)
        async def body(client, server):
            await asyncio.to_thread(self.send, client)
            for changes in (dict(auth=self.sender_auth), dict(auth=different),
                            dict(endpoint=self.sender_endpoint.id), dict(nonce=uuid4())):
                with self.assertRaises(p.CredentialError):
                    await asyncio.to_thread(self.take, client, **changes)
            self.assertEqual((await asyncio.to_thread(self.take, client)).text, self.values['text'])
        self.run_socket(body)

    def test_foreign_project_client_and_service_tokens_cannot_read_or_send(self):
        from mop.server.catalog.models import Node
        node = Node.objects.create(name='foreign-node', driver='host')
        NodeProject.objects.create(node=node, project=self.project)
        machine = p.Credential('token', access.enroll_service('node-service', node.pk,
            project_ids=(self.project.pk,), permissions=('execution.poll_work',)))
        async def body(client, server):
            with self.assertRaises(p.CredentialError):
                await asyncio.to_thread(client.send_private_report, machine, **self.values)
            await asyncio.to_thread(self.send, client)
            with self.assertRaises(p.CredentialError):
                await asyncio.to_thread(self.take, client, auth=machine)
            await server.runtime.database(a.ClientOwnership.objects.filter(client=self.client).delete)
            with self.assertRaises(p.CredentialError):
                await asyncio.to_thread(self.take, client)
        self.run_socket(body)

    def crypto_refusal(self, mutation, *, stage='decrypt'):
        async def body(client, server):
            if stage == 'decrypt':
                result = await asyncio.to_thread(self.send, client)
                self.after_decrypt = mutation
                with self.assertRaises(p.CredentialError):
                    await asyncio.to_thread(self.take, client)
                state = await server.runtime.database(lambda: m.MessageDelivery.objects.values_list(
                    'state', flat=True).get(pk=result.id))
                self.assertEqual(state, 'attempting')
            else:
                self.after_encrypt = mutation
                with self.assertRaises(p.CredentialError):
                    await asyncio.to_thread(self.send, client)
                self.assertFalse(await server.runtime.database(m.MessageDelivery.objects.exists))
        self.run_socket(body)

    def test_same_user_other_conversation_cannot_take_addressed_payload(self):
        other = self.register(self.actor, self.client, 'second-receiver-conversation')
        context = self.metadata.connect(self.actor, other.id, gateway_id=uuid4())
        async def body(client, server):
            await asyncio.to_thread(self.send, client)
            self.assertIsNone(await asyncio.to_thread(self.take, client, endpoint=other.id, nonce=context.nonce))
            with self.assertRaises(p.CredentialError):
                await asyncio.to_thread(self.take, client, nonce=context.nonce)
            self.assertEqual((await asyncio.to_thread(self.take, client)).text, self.values['text'])
        self.run_socket(body)

    def test_optional_sender_from_other_project_cannot_attribute_report(self):
        a.ProjectMembership.objects.create(user=self.sender_user, project=self.foreign_project, source=self.source)
        other = self.metadata.register(self.sender_actor, client_id=self.sender_client.pk,
            project_id=self.foreign_project.pk, external_session_id='foreign-project', display_name='same')
        context = self.metadata.connect(self.sender_actor, other.id, gateway_id=uuid4())
        async def body(client, server):
            with self.assertRaises(p.CredentialError):
                await asyncio.to_thread(self.send, client, sender_endpoint_id=other.id,
                    sender_connection_nonce=context.nonce)
            self.assertFalse(await server.runtime.database(m.MessageDelivery.objects.exists))
        self.run_socket(body)

    def test_receiver_revocation_during_crypto_discards_plaintext(self):
        self.crypto_refusal(lambda: access.revoke_token(self.actor))

    def test_sender_revocation_during_crypto_discards_plaintext(self):
        self.crypto_refusal(lambda: access.revoke_token(self.sender_actor))

    def test_receiver_client_removed_during_crypto_discards_plaintext(self):
        self.crypto_refusal(lambda: a.ClientOwnership.objects.filter(client=self.client).delete())

    def test_sender_client_removed_during_crypto_discards_plaintext(self):
        self.crypto_refusal(lambda: a.ClientOwnership.objects.filter(client=self.sender_client).delete())

    def test_receiver_project_removed_during_crypto_discards_plaintext(self):
        self.crypto_refusal(lambda: a.ProjectMembership.objects.filter(user=self.user).delete())

    def test_sender_project_removed_during_crypto_discards_plaintext(self):
        self.crypto_refusal(lambda: a.ProjectMembership.objects.filter(user=self.sender_user).delete())

    def test_receiver_lease_changed_during_crypto_discards_plaintext(self):
        self.crypto_refusal(lambda: m.ConnectionLease.objects.filter(endpoint_id=self.endpoint.id).update(nonce=uuid4()))

    def test_lease_expiry_during_crypto_discards_plaintext(self):
        self.crypto_refusal(lambda: m.ConnectionLease.objects.filter(endpoint_id=self.endpoint.id).update(
            expires_at=timezone.now()-timedelta(seconds=1)))

    def test_receiver_endpoint_retired_during_crypto_discards_plaintext(self):
        self.crypto_refusal(lambda: m.Endpoint.objects.filter(pk=self.endpoint.id).update(retired=True))

    def test_receiver_permission_revoked_during_crypto_discards_plaintext(self):
        self.crypto_refusal(lambda: self.user.user_permissions.clear())

    def test_sender_permission_revoked_during_crypto_discards_plaintext(self):
        self.crypto_refusal(lambda: self.sender_user.user_permissions.clear())

    def test_message_deadline_expired_during_crypto_discards_plaintext(self):
        self.crypto_refusal(lambda: m.MessageDelivery.objects.update(not_after=timezone.now()-timedelta(seconds=1)))

    def test_installation_changed_during_crypto_discards_plaintext(self):
        self.crypto_refusal(lambda: Installation.objects.update(epoch=uuid4()))

    def test_root_trust_changed_during_crypto_discards_plaintext(self):
        self.crypto_refusal(lambda: Installation.objects.update(root_digest='a'*64))

    def test_effects_disabled_during_crypto_discards_plaintext(self):
        self.crypto_refusal(lambda: Installation.objects.update(effects_enabled=False))

    def test_restore_changed_during_crypto_discards_plaintext(self):
        from mop.server.controller.models import RestoreGuard
        self.crypto_refusal(lambda: RestoreGuard.objects.update(generation=uuid4()))

    def test_send_rechecks_sender_revocation_before_commit(self):
        self.crypto_refusal(lambda: access.revoke_token(self.sender_actor), stage='encrypt')

    def test_send_rechecks_root_trust_before_commit(self):
        self.crypto_refusal(lambda: Installation.objects.update(root_digest='a'*64), stage='encrypt')

    def test_provider_failure_redacts_credential_and_plaintext(self):
        def broken():
            raise RuntimeError(self.auth.value+' '+self.values['text'])
        self.after_encrypt = broken
        async def body(client, server):
            with self.assertRaises(p.CredentialError) as caught:
                await asyncio.to_thread(self.send, client)
            self.assertNotIn(self.auth.value, str(caught.exception))
            self.assertNotIn(self.values['text'], str(caught.exception))
        self.run_socket(body)

    def test_raw_provider_permission_failure_never_echoes_private_bytes(self):
        def broken():
            raise PermissionError(self.auth.value+' '+self.values['text'])
        async def body(client, server):
            self.after_encrypt = broken
            with self.assertRaises(p.CredentialError) as caught:
                await asyncio.to_thread(self.send, client)
            self.assertNotIn(self.auth.value, str(caught.exception))
            self.assertNotIn(self.values['text'], str(caught.exception))
            self.after_encrypt = lambda: None
            await asyncio.to_thread(self.send, client)
            self.after_decrypt = broken
            with self.assertRaises(p.CredentialError) as caught:
                await asyncio.to_thread(self.take, client)
            self.assertNotIn(self.auth.value, str(caught.exception))
            self.assertNotIn(self.values['text'], str(caught.exception))
        self.run_socket(body)

    def lost_reply(self, *, take):
        entered, release, completed = Event(), Event(), Event()
        def hold():
            entered.set()
            if not release.wait(3):
                raise AssertionError('owned crypto wait was not released')
            completed.set()
        async def body(client, server):
            if take:
                result = await asyncio.to_thread(self.send, client)
                self.after_decrypt = hold
            else:
                self.after_encrypt = hold
            client.timeout = .5
            task = asyncio.create_task(asyncio.to_thread(self.take if take else self.send, client))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                with self.assertRaisesRegex(p.CredentialError, 'outcome unknown.*do not replay'):
                    await task
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)
            self.assertTrue(await asyncio.to_thread(completed.wait, 2))
            # Runtime владеет SQL completion после timeout клиента: дождаться
            # реального execute, не считать crypto completion доказательством COMMIT.
            for owned in tuple(server._tasks):
                await owned
            client.timeout = 5
            if not take:
                result = await asyncio.to_thread(self.send, client)
                self.assertEqual(await server.runtime.database(m.MessageDelivery.objects.count), 1)
                self.after_encrypt = lambda: None
                self.assertEqual((await asyncio.to_thread(self.take, client)).id, result.id)
            self.assertIsNone(await asyncio.to_thread(self.take, client))
            await server.runtime.database(self.metadata.disconnect, self.context)
            new = await server.runtime.database(self.metadata.connect, self.actor,
                self.endpoint.id, gateway_id=uuid4())
            self.assertIsNone(await asyncio.to_thread(self.take, client, nonce=new.nonce))
            state = await server.runtime.database(lambda: m.MessageDelivery.objects.values_list(
                'state', flat=True).get(pk=result.id))
            self.assertEqual(state, 'unknown')
        self.run_socket(body)

    def test_lost_send_reply_retains_one_identity_without_second_dispatch(self):
        self.lost_reply(take=False)

    def test_lost_take_reply_never_releases_plaintext_to_same_or_new_receiver(self):
        self.lost_reply(take=True)

    def test_report_http_uses_default_protected_port_and_preserves_strict_input_verdict(self):
        async def body(client, server):
            boot = {**settings.BOOTSTRAP, 'role': 'web',
                'credential_service': dict(path=self.path, server_uid=os.geteuid())}
            with override_settings(BOOTSTRAP=boot, ALLOWED_HOSTS=['testserver']), \
                    patch('mop.server.controller.bootstrap.private_bytes', side_effect=AssertionError('web root read')), \
                    patch('mop.server.controller.bootstrap.secret_provider', side_effect=AssertionError('web provider')):
                http = Client(HTTP_AUTHORIZATION='Token '+self.sender_auth.value)
                args = dict(schema_version=1, epoch=str(self.values['expected_epoch']),
                    address=self.values['address'], message_id=str(self.values['message_id']), text=self.values['text'],
                    not_after=self.values['not_after'].isoformat(),
                    operation_request_id=str(self.values['operation_request_id']), priority='now', notify_when_idle=True)
                def post(http, path, args, **headers):
                    response = http.post(path, args, content_type='application/json', secure=True, **headers)
                    return dict(status=response.status_code, body=response.json(), cache=response['Cache-Control'])
                for changes in (dict(text=None), dict(notify_when_idle='true'), dict(priority=[]),
                                dict(text='   '), dict(text='я'*32769),
                                dict(not_after=(timezone.now()-timedelta(seconds=1)).isoformat())):
                    response = await server.runtime.database(lambda: post(http, '/api/v1/reports', {**args, **changes}))
                    self.assertEqual(response['status'], 400, response['body'])
                response = await server.runtime.database(lambda: post(http, '/api/v1/reports', args,
                    HTTP_X_MOP_ENDPOINT=str(self.sender_endpoint.id),
                    HTTP_X_MOP_CONNECTION=str(self.sender_context.nonce)))
                self.assertEqual(response['status'], 202, response['body'])
                self.assertEqual(response['cache'], 'no-store')
                report = response['body']['delivery']
                frame = await asyncio.to_thread(self.take, client)
                self.assertEqual(str(frame.id), report['id'])
                self.assertEqual(frame.reply_address, self.sender_endpoint.address)
                self.assertEqual(frame.text, self.values['text'])
                await server.runtime.database(self.metadata.before_dispatch, self.context, frame.id)
                receiver = Client(HTTP_AUTHORIZATION='Token '+self.auth.value)
                ack = await server.runtime.database(lambda: post(receiver, '/api/v1/deliveries/'+report['id']+'/ack',
                    dict(schema_version=1, outcome='accepted'), HTTP_X_MOP_ENDPOINT=str(self.endpoint.id),
                    HTTP_X_MOP_CONNECTION=str(self.context.nonce)))
                self.assertEqual(ack['status'], 200, ack['body'])
        self.run_socket(body)

    def test_rootless_port_preserves_domain_conflict_visibility_and_capacity(self):
        from mop.server.sessions.endpoints import Conflict, Capacity, Unavailable
        from mop.server.access.services import NotVisible
        async def body(client, server):
            boot = {**settings.BOOTSTRAP, 'role': 'web',
                    'credential_service': dict(path=self.path, server_uid=os.geteuid())}
            with override_settings(BOOTSTRAP=boot):
                api = endpointviews.default_endpoints()
                await asyncio.to_thread(api.send_private_report, self.sender_auth, **self.values)
                with self.assertRaises(Conflict):
                    await asyncio.to_thread(api.send_private_report, self.sender_auth,
                        **{**self.values, 'text': 'different'})
                with self.assertRaises(NotVisible):
                    await asyncio.to_thread(api.send_private_report, self.sender_auth,
                        **{**self.values, 'message_id': uuid4(), 'address': 'private:'+str(uuid4())})
                # Сам публичный owner решает admission; boundary сохраняет его
                # тип/фразу, а не дублирует policy или переводит всё в 403.
                with patch.object(server.application.private.endpoints, 'send',
                        side_effect=Capacity('private delivery admission capacity exhausted')):
                    with self.assertRaisesRegex(Capacity, 'admission capacity exhausted'):
                        await asyncio.to_thread(api.send_private_report, self.sender_auth, **self.values)
                with patch.object(server.application.private.endpoints, 'send',
                        side_effect=Unavailable('private delivery effects are disabled')):
                    with self.assertRaisesRegex(Unavailable, 'effects are disabled'):
                        await asyncio.to_thread(api.send_private_report, self.sender_auth, **self.values)
        self.run_socket(body)

    def test_default_rootless_composition_uses_socket_and_keeps_metadata_sql_only(self):
        async def body(client, server):
            boot = {**settings.BOOTSTRAP, 'role': 'web',
                    'credential_service': dict(path=self.path, server_uid=os.geteuid())}
            with override_settings(BOOTSTRAP=boot), \
                    patch('mop.server.controller.bootstrap.private_bytes', side_effect=AssertionError('web root read')), \
                    patch('mop.server.controller.bootstrap.secret_provider', side_effect=AssertionError('web provider')):
                api = endpointviews.default_endpoints()
                self.assertTrue(callable(getattr(api, 'send_private_report', None)),
                    'default endpoint composition has no protected application port')
                await server.runtime.database(api.presence, self.actor, self.endpoint.id)
                sent = await asyncio.to_thread(api.send_private_report, self.sender_auth, **self.values)
                # TrustedEndpointContext нельзя превратить в credential: legacy
                # gateway port обязан отказать до durable attempting/crypto.
                from mop.server.sessions.endpoints import Unavailable
                with self.assertRaises(Unavailable):
                    await server.runtime.database(api.take, self.context)
                state = await server.runtime.database(lambda: m.MessageDelivery.objects.values_list(
                    'state', flat=True).get(pk=sent.id))
                self.assertEqual(state, 'queued')
                frame = await asyncio.to_thread(api.take_private_delivery, self.auth,
                    endpoint_id=self.endpoint.id, connection_nonce=self.context.nonce)
                self.assertEqual(frame.id, sent.id)
                def under_sql():
                    with transaction.atomic():
                        with self.assertRaisesRegex(RuntimeError, 'outside.*transaction'):
                            api.send_private_report(self.sender_auth, **self.values)
                        with self.assertRaisesRegex(RuntimeError, 'outside.*transaction'):
                            api.take_private_delivery(self.auth, endpoint_id=self.endpoint.id,
                                connection_nonce=self.context.nonce)
                await server.runtime.database(under_sql)
        self.run_socket(body)
