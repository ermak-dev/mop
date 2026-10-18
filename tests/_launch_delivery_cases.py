"""Адресные байты запуска через DRF и защищённый credential socket (#643).

HYPOTHESIS: AddressedLaunchSource не имеет production route; read_definition
требует owner credential и не годится как machine launch authority.
SPEC: конечный read_launch для собственного claimed create/apply_identity;
RBAC + node-executor enrollment, exact Work/pins, current approvals/grant.
Provider вне SQL; после его ожидания все authority/fences проверяются заново.
Фикстура регистрирует durable scheduler receipt, не запускает живой пул.
SOLUTION: конечные socket/DRF/HTTPS ports, каноническое execution.read_launch,
Access-owned retained scope и повторная SQL-проверка request-local proof.
RED: PG17 -- нет route/permission; затем 200 при подмене durable claim и
освобождённом target fence. Отдельно обнаружен private prompt в repr ответа.
RESULT: 24/24 PG17, включая настоящий verified HTTPS + private socket + AEAD;
отзывы во время provider wait, exact Work, claim/fences и no-store проверены.
STATUS: FIXED -- see #643 (адресная выдача, не startup/SessionApi/live pool).
"""
import asyncio
import base64
from datetime import timedelta
import json
from unittest.mock import patch
from uuid import uuid4

from asgiref.sync import async_to_sync
from django.contrib.auth.models import Permission
from django.db import connection
from django.test import Client, TransactionTestCase, override_settings
from django.utils import timezone

import _credential_cases as credential_cases


@override_settings(ALLOWED_HOSTS=['testserver'])
class LaunchDelivery(TransactionTestCase):
    serialized_rollback = True
    setUp = credential_cases.CredentialSocketCases.setUp
    exercise = credential_cases.CredentialSocketCases.exercise
    reserved_grant = credential_cases.CredentialSocketCases.reserved_grant
    grant_assignment = credential_cases.CredentialSocketCases.grant_assignment

    def fixture(self):
        from mop.server.controller.models import Installation, RestoreGuard
        from mop.server.operations import models as om
        from mop.server.operations.service import _binding
        from mop.server.sessions import models as em
        from mop.server.preparation.models import BootstrapRevision
        from mop.server.controller.machinebinding import compose
        from mop.server.controller.createbinding import LaunchSupport
        permission = Permission.objects.filter(content_type__app_label='execution', codename='read_launch').first()
        self.assertIsNotNone(permission, 'canonical addressed launch permission is missing')
        self.user.email = 'owner@example.invalid'
        self.user.save(update_fields=['email'])
        self.user.user_permissions.add(Permission.objects.get(content_type__app_label='operations', codename='view_operation'))
        Installation.objects.filter(pk=1).update(effects_enabled=True)
        self.reserved = self.reserved_grant()
        self.assignment = self.grant_assignment(self.reserved)
        self.assignment.display_name = 'pu-socket-project-1'
        self.assignment.save(update_fields=['display_name'])
        with override_settings(BOOTSTRAP=self.bootstrap):
            self.app = self.service.application()
        self.app.grants.provision(self.access.token_context(self.token), self.reserved.id,
            expected_revision=self.reserved.revision)
        self.machine_key = self.access.enroll_service('launch-executor', self.assignment.node_id,
            project_ids=(self.project.id,), permissions=('execution.poll_work', 'execution.claim_work',
                'execution.report_result', 'execution.read_launch'))
        self.machine_auth = self.Auth('token', self.machine_key)
        self.machine = self.access.token_context(self.machine_key)
        self.execution = compose(grants=self.app.grants, launch_support=LaunchSupport())
        installation = Installation.objects.get(pk=1)
        self.operation = om.Operation.objects.create(actor=self.user,
            actor_binding=_binding(self.access.token_context(self.token)), request_id=uuid4(), digest='a'*64,
            epoch=installation.epoch, restore_generation=RestoreGuard.objects.get(pk=1).generation,
            action='create', state='running', revision=3,
            node_id=self.assignment.node_id, target_kind='puppet', target_id=self.assignment.id,
            incarnation_id=self.reserved.incarnation, expected_revision=self.assignment.revision,
            not_after=timezone.now()+timedelta(minutes=2))
        self.operation.projects.set((self.project.id,))
        om.RestartBinding.objects.create(operation=self.operation, project=self.project,
            executor_epoch=self.assignment.current_incarnation.executor_epoch, owner_id=self.user.pk,
            generation=self.assignment.generation, role=self.reserved.role,
            bundle_digest=self.reserved.digest, grant_id=self.reserved.id)
        om.CreateBinding.objects.create(operation=self.operation, definition_id=self.reserved.definition_id,
            scheduler_id=uuid4(), job_id=str(self.assignment.id), namespace='default')
        for ordinal, action in enumerate(('scheduler_register', 'apply_identity', 'confirm_ready', 'activate_grant'), 1):
            om.Step.objects.create(operation=self.operation, ordinal=ordinal, name=action,
                outcome='succeeded' if ordinal == 1 else '', quiescent_at=timezone.now() if ordinal == 1 else None)
        om.ScheduledPuppet.objects.create(target_id=self.assignment.id, incarnation_id=self.reserved.incarnation,
            scheduler_id=uuid4(), job_id=str(self.assignment.id), namespace='default',
            epoch=installation.epoch, restore_generation=self.operation.restore_generation, modify_index=1)
        revision = BootstrapRevision.objects.get(project=self.project)
        from mop.server.catalog.models import ProjectPolicySnapshot, ImageRevision
        from mop.server.operations.launch import image_pin
        policy = ProjectPolicySnapshot.objects.create(project=self.project, sequence=1, content=b'fixture',
            digest='a'*64, source_commit='a'*40, source_ref='refs/heads/main', provenance='fixture',
            cpu=100, memory_mb=256, disk_mb=1024)
        image = ImageRevision.objects.create(policy=policy, source='fixture')
        self.pin = em.IncarnationLaunch.objects.create(incarnation_id=self.reserved.incarnation,
            bootstrap_revision=revision.id, bootstrap_digest=revision.digest,
            image_revision=image, image_digest=image_pin(image.id))
        # После scheduler receipt остаётся только target fence; три machine
        # claims создаёт настоящий claim ниже. Реальную HTTP-передачу без seeded
        # scheduler строк проверяет CreateProducerHandoff (#716).
        scheduler = om.Step.objects.get(operation=self.operation, name='scheduler_register')
        scheduler.token = scheduler.attempt_id = uuid4()
        scheduler.started_at = scheduler.dispatched_at = scheduler.quiescent_at
        scheduler.save(update_fields=['token', 'attempt_id', 'started_at', 'dispatched_at'])
        om.EffectReceipt.objects.create(step=scheduler, token=scheduler.token,
            epoch=self.operation.epoch, node_id=self.assignment.node_id,
            source_revision=self.operation.expected_revision, decision_revision=2, applied=True)
        key = 'target:'+str(self.assignment.id)
        om.Resource.objects.get_or_create(key=key)
        om.ResourceClaim.objects.create(resource_id=key, operation=self.operation,
            step=scheduler, token=scheduler.token)
        work = self.execution.poll(self.machine, self.assignment.node_id,
            executor_epoch=self.assignment.current_incarnation.executor_epoch)[0]
        self.attempt = self.execution.claim(self.machine, work, request_id=uuid4())
        from mop.server.sessions.protocol import work_to_wire
        self.metadata = work_to_wire(work)
        return permission

    def post(self, *, auth=None, metadata=None):
        from django.db import connections
        try:
            return Client(HTTP_AUTHORIZATION='Token '+(auth or self.machine_auth).value).post(
                '/api/v1/machine/work/launch', metadata or self.metadata,
                content_type='application/json', secure=True)
        finally:
            # Django Client в owned asyncio worker не имеет request_finished
            # серверного lifecycle; закрываем checkout именно этого потока.
            connections.close_all()

    def run_delivery(self, body, *, provider=None):
        with override_settings(BOOTSTRAP=self.bootstrap):
            app = self.service.application(provider=provider)
        async def exercise(client, server):
            with patch('mop.server.controller.adminservices.credential_client', return_value=client):
                await body(client, server)
        async_to_sync(self.exercise)(exercise, application=app)

    def test_route_exists_and_unbound_root_refuses_without_provider(self):
        response = Client().post('/api/v1/machine/work/launch', {}, content_type='application/json', secure=True)
        self.assertEqual(response.status_code, 401, response.content)

    def test_own_current_create_delivers_real_approved_bytes_over_http_and_socket(self):
        self.fixture()
        async def body(client, server):
            response = await asyncio.to_thread(self.post)
            self.assertEqual(response.status_code, 200, response.content)
            self.assertEqual(response['Cache-Control'], 'no-store')
            value = response.json()
            self.assertEqual(value['work'], self.metadata)
            self.assertEqual(base64.b64decode(value['prompt'], validate=True), b'private')
            self.assertEqual(value['manifest']['soul'], self.reserved.role)
            self.assertEqual(value['bootstrap_revision'], str(self.pin.bootstrap_revision))
            self.assertEqual(value['bootstrap_digest'], self.pin.bootstrap_digest)
            self.assertEqual(value['image_digest'], self.pin.image_digest)
            self.assertEqual((value['git_name'], value['git_email']), ('credential-user', 'owner@example.invalid'))
            self.assertEqual((value['message'], value['peer_token']), ('', None))
            from mop.driver.launch import LaunchResponse, Registration
            from mop.node.http_execution import Work
            from mop.node.execution_adapters import ActionInput
            registration = Registration(Work.parse(self.metadata), str(self.pin.bootstrap_revision),
                self.pin.bootstrap_digest, self.pin.image_digest, self.assignment.display_name)
            parsed = LaunchResponse.parse(value, registration, input_factory=ActionInput)
            self.assertEqual(parsed.bundle.digest, self.reserved.digest)
            self.assertNotIn('private', repr(parsed))
            for secret in (self.token, self.machine_key):
                self.assertNotIn(secret, response.content.decode())
        self.run_delivery(body)

    # BUG/HYPOTHESIS (#732): read_launch_definition повторяет полный SQL gate
    # вокруг RetainedSecrets.read, хотя тот уже проверяет его с двух сторон AEAD.
    # Это четыре обхода authority вместо двух (всего шесть selection в delivery):
    # 2642 SQL и ~2.19 s, server закрывает fixture socket на 2 s до успешного result.
    # Контракт -- реальные проверки до/после provider, не кэш authority и не
    # больший deadline. Проверяем настоящий retained read, все SQL gates и AEAD.
    # RED: authority, authority, provider, authority, authority вместо трёх фаз;
    # отдельно оба положительных DRF/HTTPS запроса дали исходный 503 и EOF.
    # SOLUTION: убрать два внешних дубликата, оставив pre/post-provider gates
    # RetainedSecrets и начальную/финальную selection контроллера неизменными.
    # RESULT: 27/27 launch cases PG17; 1698 SQL, ~1.51/1.54 s, прежние 2/3 s.
    # STATUS: FIXED — see #732.
    def test_launch_definition_checks_authority_at_provider_boundaries_without_duplicate_round_trips(self):
        self.fixture()
        from mop.server.controller.launchdelivery import LaunchAuthority
        from mop.server.sessions.protocol import work_from_wire
        gate = LaunchAuthority(self.app, self.machine, work_from_wire(self.metadata))
        role, definition, _, _ = gate.selection()
        events = []
        check = gate.check
        provider = self.app.grants.secrets.provider
        def checked(actor, scope):
            events.append('authority')
            return check(actor, scope)
        class ObservedProvider:
            def decrypt(inner, envelope, context):
                self.assertFalse(connection.in_atomic_block)
                events.append('provider')
                return provider.decrypt(envelope, context)
        with patch.object(gate, 'check', new=checked), \
                patch.object(self.app.grants.secrets, 'provider', new=ObservedProvider()):
            content = self.app.grants.read_launch_definition(self.machine, role, definition, authority=gate)
        self.assertEqual(content.prompt, b'private')
        self.assertEqual(events, ['authority', 'provider', 'authority'])

    def test_real_verified_https_transport_binds_addressed_launch_source(self):
        self.fixture()
        import socket
        import ssl
        import subprocess
        import tempfile
        from pathlib import Path
        from django.core.asgi import get_asgi_application
        import uvicorn
        from mop.driver.launch import Registration
        from mop.node.http_execution import Work, MachineClient, HttpsTransport
        from mop.node.body_launch import AddressedLaunchSource
        async def body(client, server):
            with tempfile.TemporaryDirectory(prefix='mop-launch-tls-') as directory:
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
                machine = MachineClient(HttpsTransport(origin, self.machine_key,
                    tls=ssl.create_default_context(cafile=str(cert))))
                try:
                    async with asyncio.timeout(5):
                        while not https.started:
                            if task.done(): await task
                            await asyncio.sleep(.01)
                    work = Work.parse(self.metadata)
                    registration = Registration(work, str(self.pin.bootstrap_revision),
                        self.pin.bootstrap_digest, self.pin.image_digest, self.assignment.display_name)
                    source = AddressedLaunchSource(registration, query=machine)
                    result = await source.fetch(work)
                    self.assertEqual(result.bundle.prompt, b'private')
                    self.assertEqual(result.registration, registration)
                finally:
                    await machine.aclose()
                    https.should_exit = True
                    await asyncio.wait_for(task, 5)
                    listener.close()
        with override_settings(ALLOWED_HOSTS=['127.0.0.1']):
            self.run_delivery(body)

    def test_generic_definition_read_is_not_machine_launch_authority(self):
        self.fixture()
        async def body(client, server):
            with self.assertRaises(self.Error):
                await asyncio.to_thread(client.read_definition, self.machine_auth, self.reserved.definition_id)
        self.run_delivery(body)

    def test_human_unrelated_node_and_project_are_denied(self):
        self.fixture()
        from mop.server.catalog.models import Node, NodeProject, Project
        foreign = Node.objects.create(name='foreign', driver='host', ssh_host='foreign.invalid',
            control_origin='https://foreign.invalid', lan_address='foreign.invalid', bootstrap_address='foreign.invalid')
        NodeProject.objects.create(node=foreign, project=self.project)
        other = self.access.enroll_service('foreign-executor', foreign.id, project_ids=(self.project.id,),
            permissions=('execution.claim_work', 'execution.read_launch'))
        project = Project.objects.create(name='foreign', origin='https://example.invalid/foreign.git')
        NodeProject.objects.create(node_id=self.assignment.node_id, project=project)
        other_project = self.access.enroll_service('foreign-project', self.assignment.node_id,
            project_ids=(project.id,), permissions=('execution.claim_work', 'execution.read_launch'))
        async def body(client, server):
            for auth in (self.auth, self.Auth('token', other), self.Auth('token', other_project)):
                response = await asyncio.to_thread(self.post, auth=auth)
                self.assertIn(response.status_code, (403, 404), response.content)
                self.assertNotIn(base64.b64encode(b'private'), response.content)
        self.run_delivery(body)

    def test_launch_requires_its_canonical_permission_not_kind_only(self):
        permission = self.fixture()
        from django.contrib.auth import get_user_model
        get_user_model().objects.get(pk=self.machine.user_id).user_permissions.remove(permission)
        async def body(client, server):
            response = await asyncio.to_thread(self.post)
            self.assertEqual(response.status_code, 403, response.content)
        self.run_delivery(body)

    def test_exact_work_unknown_fields_and_definition_uuid_oracle_are_rejected(self):
        self.fixture()
        variants = [{**self.metadata, 'definition_id': str(self.reserved.definition_id)}]
        for name in ('node_id', 'target_id', 'incarnation_id', 'executor_epoch', 'grant_id', 'epoch', 'operation_id', 'step_id'):
            variants.append({**self.metadata, name: str(uuid4())})
        for name in ('revision', 'generation', 'owner_id'):
            variants.append({**self.metadata, name: str(int(self.metadata[name])+1)})
        variants.extend(({**self.metadata, 'action': 'confirm_ready'},
            {**self.metadata, 'role': 'foreign'}, {**self.metadata, 'bundle_digest': 'b'*64},
            {**self.metadata, 'not_after': (timezone.now()+timedelta(minutes=1)).isoformat()},
            {**self.metadata, 'schema_version': 2}))
        async def body(client, server):
            for metadata in variants:
                response = await asyncio.to_thread(self.post, metadata=metadata)
                self.assertIn(response.status_code, (400, 403, 404, 409), response.content)
                self.assertNotIn(base64.b64encode(b'private'), response.content)
        self.run_delivery(body)

    def test_expired_admission_is_denied(self):
        self.fixture()
        from mop.server.sessions.models import AssignmentAttempt
        AssignmentAttempt.objects.filter(pk=self.attempt.id).update(lease_until=timezone.now()-timedelta(seconds=1))
        async def body(client, server):
            response = await asyncio.to_thread(self.post)
            self.assertIn(response.status_code, (403, 409), response.content)
        self.run_delivery(body)

    def test_durable_claim_must_match_the_own_execution_attempt(self):
        self.fixture()
        from mop.server.operations.models import Step
        wrong = uuid4()
        Step.objects.filter(pk=self.metadata['step_id']).update(token=wrong, attempt_id=wrong)
        async def body(client, server):
            response = await asyncio.to_thread(self.post)
            self.assertEqual(response.status_code, 403, response.content)
            self.assertNotIn(base64.b64encode(b'private'), response.content)
        self.run_delivery(body)

    # BUG/HYPOTHESIS (#716): один operation UUID не доказывает token/stage
    # retained claim. Общий launch locator должен защищать также bytes delivery.
    # SOLUTION: тот же create-only proof проверяет stage/token/receipt до provider.
    # RESULT: RED -- 200 вместо 403; GREEN -- 99/99 producer/launch/create PG17.
    # STATUS: FIXED — see #716
    def test_launch_rejects_a_retained_scheduler_claim_with_the_wrong_token(self):
        self.fixture()
        from mop.server.operations.models import ResourceClaim
        ResourceClaim.objects.filter(operation=self.operation,
            resource_id='target:'+str(self.assignment.id)).update(token=uuid4())
        async def body(client, server):
            response = await asyncio.to_thread(self.post)
            self.assertEqual(response.status_code, 403, response.content)
            self.assertNotIn(base64.b64encode(b'private'), response.content)
        self.run_delivery(body)

    def test_launch_rejects_a_current_machine_claim_with_the_wrong_token(self):
        self.fixture()
        from mop.server.operations.models import ResourceClaim
        ResourceClaim.objects.filter(operation=self.operation,
            resource_id='node:'+str(self.assignment.node_id)).update(token=uuid4())
        async def body(client, server):
            response = await asyncio.to_thread(self.post)
            self.assertEqual(response.status_code, 403, response.content)
            self.assertNotIn(base64.b64encode(b'private'), response.content)
        self.run_delivery(body)

    def test_released_create_resource_fence_cannot_deliver_launch(self):
        self.fixture()
        from mop.server.operations.models import ResourceClaim
        ResourceClaim.objects.filter(operation=self.operation,
            resource_id='target:'+str(self.assignment.id)).update(released_at=timezone.now())
        async def body(client, server):
            response = await asyncio.to_thread(self.post)
            self.assertEqual(response.status_code, 403, response.content)
            self.assertNotIn(base64.b64encode(b'private'), response.content)
        self.run_delivery(body)

    def test_unbound_credential_composition_is_unavailable(self):
        self.fixture()
        response = self.post()
        self.assertEqual(response.status_code, 503, response.content)

    def test_socket_launch_does_not_accept_arbitrary_definition_uuid(self):
        self.fixture()
        from mop.common import credentials as p
        async def body(client, server):
            reader, writer = await asyncio.open_unix_connection(self.path)
            writer.write(p.frame({'version': 1, 'auth': {'kind': 'token', 'value': self.machine_key},
                'command': 'read_launch', 'args': {'definition_id': str(self.reserved.definition_id)}}))
            await writer.drain()
            result = p.loads(await reader.readexactly(p.frame_size(await reader.readexactly(4))))
            writer.close()
            await writer.wait_closed()
            self.assertFalse(result['ok'])
            self.assertEqual(result['error']['message'], 'invalid credential command fields')
        self.run_delivery(body)

    def during_provider(self, change):
        from mop.server.controller import bootstrap
        from mop.server.controller.models import Installation
        row = Installation.objects.get(pk=1)
        real = bootstrap.secret_provider(self.bootstrap, installation_id=row.identity,
            expected_root_digest=row.root_digest, key_id='root-v1')
        changed = []
        class Revoking:
            def decrypt(inner, envelope, context):
                self.assertFalse(connection.in_atomic_block)
                result = real.decrypt(envelope, context)
                change()
                changed.append(True)
                return result
        async def body(client, server):
            response = await asyncio.to_thread(self.post)
            self.assertEqual(response.status_code, 403, response.content)
            self.assertNotIn(base64.b64encode(b'private'), response.content)
        self.run_delivery(body, provider=Revoking())
        self.assertTrue(changed, 'refusal must exercise actual provider wait')

    def test_authority_changes_during_provider_wait_prevent_disclosure(self):
        self.fixture()
        from rest_framework.authtoken.models import Token
        self.during_provider(lambda: Token.objects.filter(user_id=self.machine.user_id).delete())

    def test_rbac_removal_during_provider_wait_prevents_disclosure(self):
        permission = self.fixture()
        from django.contrib.auth import get_user_model
        self.during_provider(lambda: get_user_model().objects.get(pk=self.machine.user_id)
            .user_permissions.remove(permission))

    def test_other_service_purpose_with_same_rbac_is_denied(self):
        self.fixture()
        from mop.server.access.models import ServiceEnrollment
        ServiceEnrollment.objects.filter(actor_id=self.machine.user_id).update(use_case='grant-activator')
        async def body(client, server):
            response = await asyncio.to_thread(self.post)
            self.assertEqual(response.status_code, 403, response.content)
        self.run_delivery(body)

    def test_enrollment_change_during_provider_wait_prevents_disclosure(self):
        self.fixture()
        from mop.server.access.models import ServiceEnrollment
        self.during_provider(lambda: ServiceEnrollment.objects.filter(actor_id=self.machine.user_id)
            .update(use_case='grant-activator'))

    def test_grant_revocation_during_provider_wait_prevents_disclosure(self):
        self.fixture()
        from mop.server.access.models import Grant
        self.user.user_permissions.add(Permission.objects.get(content_type__app_label='access', codename='revoke_grant'))
        revision = Grant.objects.get(pk=self.reserved.id).revision
        self.during_provider(lambda: self.app.grants.revoke(self.access.token_context(self.token),
            self.reserved.id, expected_revision=revision))

    def test_grant_expiration_during_provider_wait_prevents_disclosure(self):
        self.fixture()
        from mop.server.access.models import CreationIntent
        # Fault injection в immutable evidence: моделируем истечение без сна.
        self.during_provider(lambda: CreationIntent._base_manager.filter(grant__id=self.reserved.id)
            .update(expires_at=timezone.now()-timedelta(seconds=1)))

    def test_bootstrap_revocation_during_provider_wait_prevents_disclosure(self):
        self.fixture()
        from mop.server.preparation.models import BootstrapApproval, ApprovalRevocation
        self.during_provider(lambda: ApprovalRevocation.objects.create(approval=
            BootstrapApproval.objects.get(revision_id=self.pin.bootstrap_revision)))

    def test_assignment_change_during_provider_wait_prevents_disclosure(self):
        self.fixture()
        from mop.server.sessions.models import Assignment
        self.during_provider(lambda: Assignment.objects.filter(pk=self.assignment.id)
            .update(generation=self.assignment.generation+1))

    def test_executor_epoch_change_during_provider_wait_prevents_disclosure(self):
        self.fixture()
        from mop.server.sessions.models import ExecutionIncarnation
        self.during_provider(lambda: ExecutionIncarnation.objects.filter(pk=self.reserved.incarnation)
            .update(executor_epoch=uuid4()))

    def test_launch_pin_change_during_provider_wait_prevents_disclosure(self):
        self.fixture()
        from mop.server.sessions.models import IncarnationLaunch
        self.during_provider(lambda: IncarnationLaunch.objects.filter(pk=self.pin.pk).update(image_digest='b'*64))

    def test_finished_work_during_provider_wait_prevents_disclosure(self):
        self.fixture()
        from mop.server.sessions.models import AssignmentAttempt
        self.during_provider(lambda: AssignmentAttempt.objects.filter(pk=self.attempt.id).update(state='succeeded'))

    def test_released_fence_during_provider_wait_prevents_disclosure(self):
        self.fixture()
        from mop.server.operations.models import ResourceClaim
        self.during_provider(lambda: ResourceClaim.objects.filter(operation=self.operation)
            .update(released_at=timezone.now()))
