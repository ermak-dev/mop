"""Адресное read-only чтение canonical Actor Token после activation #742.

HYPOTHESIS: standard Actor Token уже создан, но finite command/HTTPS query
не передают его даже текущему enrolled executor после readiness/activation.
SPEC: только active exact grant/assignment/current producer и committed model;
никаких Work leases, issuance, caller actor IDs или угадываемого origin.
Fixture SQL receipts — consumption persisted proof, не physical HOST acceptance.
SOLUTION: отдельное permission, public Sessions proof + Access canonical read;
весь допуск повторяется после реального provider decrypt, без issuance/repair.
RESULT: RED — missing permission/unsupported command; GREEN — PG17/socket/HTTPS,
реальная create/producer/model/ready/activate цепочка, late revoke и пять actual UID.
STATUS: FIXED — see #742 (не physical HOST/MCP/body startup acceptance).
"""
import asyncio
import importlib
import json
from datetime import timedelta
from uuid import uuid4
from unittest.mock import patch

from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.db import close_old_connections
from django.test import Client, TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.authtoken.models import Token

import _credential_binding_cases as addressed
import _launch_delivery_cases as launch


class ManagementContext(TransactionTestCase):
    serialized_rollback = True
    setUp = addressed.AddressedCredentialCases.setUp
    exercise = addressed.AddressedCredentialCases.exercise
    reserved_grant = addressed.AddressedCredentialCases.reserved_grant
    grant_assignment = addressed.AddressedCredentialCases.grant_assignment
    enroll = addressed.AddressedCredentialCases.enroll
    delivery = addressed.AddressedCredentialCases.delivery
    run_delivery = launch.LaunchDelivery.run_delivery

    def fixture(self):
        from mop.server.access.models import Grant, CredentialBinding
        from mop.server.controller.models import Installation
        from mop.server.sessions import models as execution
        from mop.server.sessions.producer_models import SessionProducer, CurrentProducer
        from mop.server.sessions.telemetry_models import SessionObservation, ModelObservation, TelemetryReceipt
        self.delivery()
        permission = Permission.objects.filter(content_type__app_label='access', codename='read_management_context').first()
        self.assertIsNotNone(permission, 'dedicated management-context permission is missing')
        self.machine_user.user_permissions.add(permission)
        assignment = self.assignment
        epoch = Installation.objects.get(pk=1).epoch
        self.scope = dict(epoch=str(epoch), target_id=str(assignment.id),
            incarnation_id=str(assignment.current_incarnation_id), node_id=str(assignment.node_id),
            executor_epoch=str(assignment.current_incarnation.executor_epoch), session_id='exact-native-session',
            producer_epoch=str(uuid4()))
        self.request = dict(schema_version=1, scope=self.scope, generation=str(assignment.generation))
        ready = self.attempts[-1]
        self.native = execution.AssignmentAttempt.objects.create(assignment=assignment,
            incarnation_id=assignment.current_incarnation_id, executor_user_id=self.machine.user_id,
            request_id=uuid4(), epoch=epoch, operation_id=ready.operation_id, step_id=uuid4(),
            executor_epoch=ready.executor_epoch, owner=self.user, generation=assignment.generation,
            assignment_revision=assignment.revision, role=assignment.role, bundle_digest=assignment.bundle_digest,
            grant_id=self.grant_id, action='native_exec', not_after=ready.not_after,
            lease_until=timezone.now()-timedelta(seconds=1), state='succeeded', revision=2)
        execution.ResultReceipt.objects.create(attempt=self.native, expected_revision=1,
            resulting_revision=2, outcome='succeeded', evidence='native-started')
        self.producer = SessionProducer.objects.create(assignment=assignment,
            incarnation_id=assignment.current_incarnation_id, node_id=assignment.node_id, epoch=epoch,
            executor_epoch=ready.executor_epoch, producer_epoch=self.scope['producer_epoch'],
            session_id=self.scope['session_id'], executor_user_id=self.machine.user_id, owner_id=self.user.id,
            generation=assignment.generation, assignment_revision=assignment.revision, role=assignment.role,
            bundle_digest=assignment.bundle_digest, grant_id=self.grant_id, operation_id=ready.operation_id,
            step_id=self.native.step_id, attempt_id=self.native.id, proof_digest='a'*64, request_digest='b'*64,
            accepted_revision=1, pane='%1', pid=1234, process_start='123')
        CurrentProducer.objects.create(incarnation_id=assignment.current_incarnation_id, producer=self.producer)
        execution.NodeExecutor.objects.create(node_id=assignment.node_id, executor_epoch=ready.executor_epoch)
        self.observation = SessionObservation.objects.create(assignment=assignment,
            incarnation_id=assignment.current_incarnation_id, node_id=assignment.node_id, epoch=epoch,
            executor_epoch=ready.executor_epoch, producer_epoch=self.producer.producer_epoch,
            session_id=self.producer.session_id, sequence=1, event='PostModelSwitch', confirmed_model='fixture-model',
            confirmed_sequence=1, model_hint='fixture-model', model_proven=True, model_received_at=timezone.now())
        self.model = ModelObservation.objects.create(observation=self.observation, sequence=1,
            model='fixture-model', kind='PostModelSwitch')
        TelemetryReceipt.objects.create(observation=self.observation, sequence=1, digest='c'*64)
        self.subject = Grant.objects.get(pk=self.grant_id).subject
        self.subject_token = Token.objects.get(user=self.subject).key
        self.subject_binding = CredentialBinding.objects.get(token_user=self.subject)
        return self.request

    def post(self, payload=None, *, token=None):
        close_old_connections()
        try:
            return Client().post('/api/v1/machine/management-context',
                data=json.dumps(self.request if payload is None else payload), content_type='application/json',
                HTTP_AUTHORIZATION='Token '+(self.machine_auth.value if token is None else token), secure=True)
        finally:
            close_old_connections()

    def test_exact_existing_agent_token_is_idempotent_no_store_and_not_a_new_effect(self):
        self.fixture()
        counts = get_user_model().objects.count(), Token.objects.count()
        from mop.common import credentials as p
        direct = self.app.execute(self.machine_auth, p.Command.MANAGEMENT_CONTEXT, {'request': self.request})
        self.assertEqual(direct['token'], self.subject_token)
        async def body(client, server):
            values = []
            for _ in range(2):
                response = await asyncio.to_thread(self.post)
                self.assertEqual(response.status_code, 200, response.content)
                self.assertEqual(response['Cache-Control'], 'no-store')
                values.append(response.json())
            self.assertEqual(values[0], values[1])
            value = values[0]
            self.assertEqual(value['token'], self.subject_token)
            self.assertNotEqual(value['token'], self.machine_auth.value)
            self.assertEqual(value['subject_id'], self.subject.id)
            self.assertEqual(value['credential_id'], str(self.subject_binding.id))
            self.assertEqual(value['request'], self.request)
            self.assertEqual(value['grant_id'], str(self.grant_id))
            self.assertNotIn('origin', value)
            dto = importlib.import_module('mop.common.management_context').ManagementContext.parse(value, self.request)
            self.assertNotIn(self.subject_token, repr(dto))
            self.assertNotIn(self.subject_token, repr([dto]))
        self.run_delivery(body)
        self.assertEqual((get_user_model().objects.count(), Token.objects.count()), counts)

    def refused(self, change, *, payload=None, token=None):
        self.fixture()
        change()
        counts = get_user_model().objects.count(), Token.objects.count()
        async def body(client, server):
            response = await asyncio.to_thread(self.post, payload() if payload else None,
                token=token() if token else None)
            self.assertEqual(response.status_code, 403, response.content)
            self.assertEqual(response['Cache-Control'], 'no-store')
            self.assertNotIn(self.subject_token, response.content.decode())
            self.assertNotIn(self.machine_auth.value, response.content.decode())
        self.run_delivery(body)
        self.assertEqual((get_user_model().objects.count(), Token.objects.count()), counts)

    def test_reserved_grant_never_gets_active_context_or_catalogue(self):
        from mop.server.access.models import Grant
        from mop.server.access.services import Denied
        self.refused(lambda: Grant._base_manager.filter(pk=self.grant_id).update(state='reserved'))
        with self.assertRaises(Denied):
            self.access.token_context(self.subject_token, grants=self.app.grants.local_authority())

    def test_revoked_grant(self):
        from mop.server.access.models import Grant
        self.refused(lambda: Grant._base_manager.filter(pk=self.grant_id).update(state='revoked'))

    def test_missing_readiness(self):
        from mop.server.sessions.models import ReadinessReceipt
        self.refused(lambda: ReadinessReceipt._base_manager.all().delete())

    def test_missing_identity(self):
        from mop.server.sessions.models import IdentityApplication
        self.refused(lambda: IdentityApplication._base_manager.all().delete())

    def test_statusline_is_not_committed_model_switch(self):
        self.refused(lambda: type(self.model).objects.filter(pk=self.model.pk).update(kind='Statusline'))

    def test_model_receipt_missing(self):
        from mop.server.sessions.telemetry_models import TelemetryReceipt
        self.refused(lambda: TelemetryReceipt.objects.all().delete())

    def test_model_proof_lost(self):
        self.refused(lambda: type(self.observation).objects.filter(pk=self.observation.pk).update(model_proven=False))

    def test_old_producer(self):
        self.refused(lambda: None, payload=lambda: {**self.request,
            'scope': {**self.scope, 'producer_epoch': str(uuid4())}})

    def test_old_incarnation(self):
        self.refused(lambda: None, payload=lambda: {**self.request,
            'scope': {**self.scope, 'incarnation_id': str(uuid4())}})

    def test_old_executor_epoch(self):
        self.refused(lambda: None, payload=lambda: {**self.request,
            'scope': {**self.scope, 'executor_epoch': str(uuid4())}})

    def test_old_installation_epoch(self):
        self.refused(lambda: None, payload=lambda: {**self.request,
            'scope': {**self.scope, 'epoch': str(uuid4())}})

    def test_old_generation(self):
        self.refused(lambda: None, payload=lambda: {**self.request,
            'generation': str(self.assignment.generation+1)})

    def test_not_accepting(self):
        from mop.server.sessions.models import ExecutionIncarnation
        self.refused(lambda: ExecutionIncarnation.objects.filter(pk=self.assignment.current_incarnation_id).update(accepting=False))

    def test_foreign_node(self):
        from mop.server.catalog.models import Node
        def change():
            node = Node.objects.create(name='foreign-node', driver='host', ssh_host='node.invalid',
                control_origin='https://node.invalid', lan_address='node.invalid', bootstrap_address='node.invalid')
            self.foreign_key = self.access.enroll_service('foreign-node', node.pk,
                project_ids=(), permissions=('access.read_management_context',))
        self.refused(change, token=lambda: self.foreign_key)

    def test_project_membership_removed(self):
        from mop.server.access.models import ProjectMembership
        self.refused(lambda: ProjectMembership.objects.filter(user=self.machine_user, project=self.project).delete())

    def test_node_project_removed(self):
        from mop.server.catalog.models import NodeProject
        self.refused(lambda: NodeProject.objects.filter(node_id=self.assignment.node_id, project=self.project).delete())

    def test_node_enrollment_removed(self):
        from mop.server.access.models import ServiceEnrollment
        self.refused(lambda: ServiceEnrollment.objects.filter(actor=self.machine_user).delete())

    def test_permission_removed(self):
        self.refused(lambda: self.machine_user.user_permissions.remove(Permission.objects.get(
            content_type__app_label='access', codename='read_management_context')))

    def test_human_token_is_not_node_authority(self):
        self.refused(lambda: None, token=lambda: self.token)

    def test_subject_human(self):
        self.refused(lambda: get_user_model().objects.filter(pk=self.subject.id).update(kind='human'))

    def test_subject_service(self):
        self.refused(lambda: get_user_model().objects.filter(pk=self.subject.id).update(kind='service'))

    def test_creator_token_revoked(self):
        self.refused(lambda: Token.objects.filter(user=self.user).delete())

    def test_subject_token_missing_is_not_repaired(self):
        self.refused(lambda: Token.objects.filter(user=self.subject).delete())

    def test_subject_binding_revoked_is_not_repaired(self):
        from mop.server.access.models import CredentialBinding
        self.refused(lambda: CredentialBinding.objects.filter(pk=self.subject_binding.pk).delete())

    def test_subject_token_generation_changed(self):
        def change():
            Token.objects.filter(user=self.subject).delete()
            Token.objects.create(user=self.subject)
        self.refused(change)

    def test_expired_assignment(self):
        self.refused(lambda: type(self.assignment).objects.filter(pk=self.assignment.pk).update(
            expires_at=timezone.now()-timedelta(seconds=1)))

    def test_expired_grant(self):
        from mop.server.access.models import CreationIntent
        self.refused(lambda: CreationIntent._base_manager.filter(grant__id=self.grant_id).update(
            expires_at=timezone.now()-timedelta(seconds=1)))

    def test_new_producer_with_old_ready_attempt(self):
        self.refused(lambda: type(self.producer)._base_manager.filter(pk=self.producer.pk).update(operation_id=uuid4()))

    def late_refused(self, change):
        self.fixture()
        provider = self.app.store.provider
        decrypt, changed = provider.decrypt, []
        def after_decrypt(*args, **kwargs):
            value = decrypt(*args, **kwargs)
            if not changed:
                change()
                changed.append(True)
            return value
        async def body(client, server):
            response = await asyncio.to_thread(self.post)
            self.assertEqual(response.status_code, 403, response.content)
            self.assertNotIn(self.subject_token, response.content.decode())
        with patch.object(type(provider), 'decrypt', side_effect=after_decrypt):
            self.run_delivery(body, provider=provider)
        self.assertTrue(changed, 'must cross the real provider boundary before recheck')

    def test_node_token_revoked_during_crypto_wait(self):
        self.late_refused(lambda: Token.objects.filter(user=self.machine_user).delete())

    def test_node_permission_revoked_during_crypto_wait(self):
        self.late_refused(lambda: self.machine_user.user_permissions.remove(Permission.objects.get(
            content_type__app_label='access', codename='read_management_context')))

    def test_subject_token_revoked_during_crypto_wait(self):
        self.late_refused(lambda: Token.objects.filter(user=self.subject).delete())

    def test_subject_binding_revoked_during_crypto_wait(self):
        from mop.server.access.models import CredentialBinding
        self.late_refused(lambda: CredentialBinding.objects.filter(pk=self.subject_binding.pk).delete())

    def test_generation_changed_during_crypto_wait(self):
        self.late_refused(lambda: type(self.assignment).objects.filter(pk=self.assignment.pk).update(
            generation=self.assignment.generation+1))

    def test_producer_changed_during_crypto_wait(self):
        from mop.server.sessions.producer_models import CurrentProducer
        self.late_refused(lambda: CurrentProducer.objects.all().delete())

    def test_readiness_removed_during_crypto_wait(self):
        from mop.server.sessions.models import ReadinessReceipt
        self.late_refused(lambda: ReadinessReceipt._base_manager.all().delete())

    def test_node_project_changed_during_crypto_wait(self):
        from mop.server.catalog.models import NodeProject
        self.late_refused(lambda: NodeProject.objects.filter(node_id=self.assignment.node_id, project=self.project).delete())

    def test_grant_revoked_during_crypto_wait(self):
        from mop.server.access.models import Grant
        self.late_refused(lambda: Grant._base_manager.filter(pk=self.grant_id).update(state='revoked'))

    def test_model_proof_lost_during_crypto_wait(self):
        self.late_refused(lambda: type(self.observation).objects.filter(pk=self.observation.pk).update(model_proven=False))

    def test_verified_https_positive_and_addressed_negative_cases(self):
        self.fixture()
        import socket
        import ssl
        import subprocess
        import tempfile
        from pathlib import Path
        from django.core.asgi import get_asgi_application
        import uvicorn
        from mop.node.http_execution import MachineClient, HttpsTransport, Refused
        async def body(client, server):
            with tempfile.TemporaryDirectory(prefix='mop-management-tls-') as directory:
                cert, key = Path(directory)/'cert.pem', Path(directory)/'key.pem'
                subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
                    '-keyout', str(key), '-out', str(cert), '-subj', '/CN=localhost',
                    '-addext', 'subjectAltName=DNS:localhost,IP:127.0.0.1'], check=True,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                listener = socket.socket()
                listener.bind(('127.0.0.1', 0))
                listener.listen(8)
                origin = 'https://127.0.0.1:'+str(listener.getsockname()[1])
                https = uvicorn.Server(uvicorn.Config(get_asgi_application(), lifespan='off',
                    log_level='error', access_log=False, ssl_certfile=str(cert), ssl_keyfile=str(key)))
                task = asyncio.create_task(https.serve(sockets=[listener]))
                machine = MachineClient(HttpsTransport(origin, self.machine_auth.value,
                    tls=ssl.create_default_context(cafile=str(cert))))
                human = MachineClient(HttpsTransport(origin, self.token,
                    tls=ssl.create_default_context(cafile=str(cert))))
                try:
                    async with asyncio.timeout(5):
                        while not https.started:
                            if task.done(): await task
                            await asyncio.sleep(.01)
                    value = await machine.read_management_context(self.request)
                    self.assertEqual(value.token, self.subject_token)
                    self.assertNotIn(self.subject_token, repr(value))
                    self.assertEqual(value.request.wire(), self.request)
                    for name in ('epoch', 'target_id', 'incarnation_id', 'node_id', 'executor_epoch', 'producer_epoch'):
                        with self.subTest(name=name), self.assertRaises(Refused):
                            await machine.read_management_context({**self.request,
                                'scope': {**self.scope, name: str(uuid4())}})
                    with self.assertRaises(Refused):
                        await human.read_management_context(self.request)
                finally:
                    await machine.aclose()
                    await human.aclose()
                    https.should_exit = True
                    await asyncio.wait_for(task, 5)
                    listener.close()
        with override_settings(ALLOWED_HOSTS=['127.0.0.1']):
            self.run_delivery(body)

    def test_restore_changes_during_crypto_wait(self):
        from mop.server.controller.models import RestoreGuard
        self.fixture()
        provider = self.app.store.provider
        decrypt = provider.decrypt
        changed = []
        def change(*args, **kwargs):
            value = decrypt(*args, **kwargs)
            if not changed:
                RestoreGuard.objects.filter(pk=1).update(generation=uuid4())
                changed.append(True)
            return value
        async def body(client, server):
            response = await asyncio.to_thread(self.post)
            self.assertEqual(response.status_code, 403, response.content)
            self.assertNotIn(self.subject_token, response.content.decode())
        with patch.object(type(provider), 'decrypt', side_effect=change):
            self.run_delivery(body, provider=provider)
        self.assertTrue(changed, 'test must cross real retained-definition decrypt')

    def test_creator_revoked_during_crypto_wait(self):
        self.fixture()
        provider = self.app.store.provider
        decrypt = provider.decrypt
        changed = []
        def change(*args, **kwargs):
            value = decrypt(*args, **kwargs)
            if not changed:
                Token.objects.filter(user=self.user).delete()
                changed.append(True)
            return value
        async def body(client, server):
            response = await asyncio.to_thread(self.post)
            self.assertEqual(response.status_code, 403, response.content)
            self.assertNotIn(self.subject_token, response.content.decode())
        with patch.object(type(provider), 'decrypt', side_effect=change):
            self.run_delivery(body, provider=provider)
        self.assertTrue(changed)


class ActivatedManagementContext(TransactionTestCase):
    """Настоящие create -> producer ACK -> telemetry COMMIT -> readiness -> activation.

    Node results/reference scheduler -- owned adapters, не physical HOST proof.
    """
    serialized_rollback = True
    import _credential_cases as sockets
    import _create_cases as create
    setUp = sockets.CreateSocketActivationCases.setUp
    ready = sockets.CreateSocketActivationCases.ready
    body = sockets.CreateSocketActivationCases.body
    submit = sockets.CreateSocketActivationCases.submit
    exercise = sockets.CredentialSocketCases.exercise
    launch = create.CreateRegistration.launch
    machine = create.CreateActivation.machine
    post = ManagementContext.post
    run_delivery = ManagementContext.run_delivery

    def test_actual_post_activation_read_uses_existing_stable_actor_without_work_lease(self):
        from mop.common.credentials import Credential
        from mop.common.credential_client import GrantActivator
        from mop.common.telemetry import SessionScope, Observation
        from mop.server.access import services as auth
        from mop.server.access.models import Grant
        from mop.server.sessions.models import Assignment, AssignmentAttempt
        from mop.server.sessions.producer_models import SessionProducer
        operation, target, _ = self.launch()
        self.create.CreateActivation.through_readiness(self, operation, ready=False)
        self.assignment = Assignment.objects.get(pk=target)
        producer = SessionProducer.objects.get(currentproducer__incarnation_id=self.assignment.current_incarnation_id)
        self.scope = SessionScope(producer.epoch, producer.assignment_id, producer.incarnation_id, producer.node_id,
            producer.executor_epoch, producer.session_id, producer.producer_epoch).wire()
        self.request = dict(schema_version=1, scope=self.scope, generation=str(self.assignment.generation))
        machine_user = Token.objects.get(key=self._executor_key).user
        for code in ('access.read_management_context', 'execution.ingest_telemetry'):
            app_label, codename = code.split('.')
            machine_user.user_permissions.add(Permission.objects.get(content_type__app_label=app_label, codename=codename))
        self.machine_auth = Credential('token', self._executor_key)
        observed = Observation(SessionScope.parse(self.scope), 1, 'PostModelSwitch', None, 'fixture-native-model', None, None, 0)
        response = Client(HTTP_AUTHORIZATION='Token '+self._executor_key).post('/api/v1/machine/telemetry',
            observed.wire(), content_type='application/json', secure=True)
        self.assertEqual(response.status_code, 200, response.content)
        service, actor = self.machine()
        work, = service.poll(actor, self.node.pk, executor_epoch=self.boot)
        self.assertEqual(work.action, 'confirm_ready')
        attempt = service.claim(actor, work, request_id=uuid4())
        service.result(actor, attempt.id, expected_revision=attempt.revision, receipt_id=uuid4(),
            outcome='succeeded', evidence='ready')
        grant = Grant.objects.get(pk=self.assignment.grant_id)
        canonical = Token.objects.get(user_id=grant.subject_id).key
        activator = auth.enroll_service('management-activation-worker', self.node.pk,
            project_ids=(self.project.pk,), permissions=('access.activate_grant',), use_case='grant-activator')
        counts = get_user_model().objects.count(), Token.objects.count()
        async def exercise(client, server):
            denied = await asyncio.to_thread(self.post)
            self.assertEqual(denied.status_code, 403, denied.content)
            self.ops.activations.activator = GrantActivator(client, Credential('token', activator))
            result = await server.runtime.database(self.ops.activations.run, operation)
            self.assertEqual(result['state'], 'succeeded', result['reason'])
            # Завершённые leases и снятые resources больше не authority этого read.
            await server.runtime.database(lambda: AssignmentAttempt.objects.filter(assignment_id=target).update(
                lease_until=timezone.now()-timedelta(seconds=1)))
            response = await asyncio.to_thread(self.post)
            self.assertEqual(response.status_code, 200, response.content)
            self.assertEqual(response.json()['token'], canonical)
            self.assertEqual(response.json()['subject_id'], grant.subject_id)
        self.run_delivery(exercise, provider=self.grants.secrets.provider)
        self.assertEqual((get_user_model().objects.count(), Token.objects.count()), counts)
