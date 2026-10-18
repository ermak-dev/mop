"""Адресные байты запуска через DRF и защищённый credential socket (#643).

HYPOTHESIS: AddressedLaunchSource не имеет production route; read_definition
требует owner credential и не годится как machine launch authority.
SPEC: конечный read_launch для собственного claimed create/native_exec;
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

HYPOTHESIS #721: planned purpose нельзя провести через прежний native response;
новые bytes требуют отдельной shape и повторного отбора после provider wait.
SOLUTION: strict read_initial_launch и отдельный POST; old role-only port native-only.
RESULT: private socket + verified HTTPS roundtrip green под прежним production
5s budget; owner-profile change после настоящего decrypt даёт отказ без bytes.
STATUS: FIXED — see #721; runtime timeouts не повышались, native не запускался.
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


def selected_fixture(case, reserved):
    """Точные SQL-пины seeded authority fixtures; lifecycle отдельно real HTTP."""
    import hashlib
    from mop.server.catalog import models as cm
    from mop.server.operations import models as om
    from mop.server.sessions import models as em
    from mop.server.preparation.models import BootstrapRevision
    from mop.server.controller.initialselection import configure
    from mop.server.controller.machinebinding import owner_identity
    from mop.server.operations.launch import image_pin
    from mop.server.access.models import AllProjectMembership, IdentitySource
    # Конфигурирование каталога installation-scoped; только owner этой fixture.
    AllProjectMembership.objects.get_or_create(user=case.user, source=IdentitySource.objects.get(name='local'))
    actor = case.access.token_context(case.token)
    case.user.user_permissions.add(Permission.objects.get(content_type__app_label='catalog', codename='configure_project'))
    revision = BootstrapRevision.objects.get(project=case.project)
    pin = em.IncarnationLaunch.objects.filter(incarnation_id=reserved.incarnation).first()
    if pin is None:
        policy = cm.ProjectPolicySnapshot.objects.create(project=case.project, sequence=1, content=b'fixture',
            digest='a'*64, source_commit='a'*40, source_ref='refs/heads/image', provenance='fixture',
            cpu=100, memory_mb=256, disk_mb=1024)
        image = cm.ImageRevision.objects.create(policy=policy, source='fixture')
        pin = em.IncarnationLaunch.objects.create(incarnation_id=reserved.incarnation,
            bootstrap_revision=revision.id, bootstrap_digest=revision.digest,
            image_revision=image, image_digest=image_pin(image.id))
    cm.NodeImage.objects.update_or_create(node_id=case.assignment.node_id, project=case.project,
        defaults=dict(desired_image_id=pin.image_revision_id, observed_image_id=pin.image_revision_id, available=True))
    config = case.app.store.create(actor, project_id=case.project.pk, name='native-config',
        kind='file', purpose='project', value=b'{}')
    selected = configure(actor, project_id=case.project.pk, definition_id=reserved.definition_id,
        bootstrap_revision=revision.id, image_id=pin.image_revision_id, source_origin=case.project.origin,
        source_commit='d'*40, source_ref='refs/heads/exact-source', desired_model='fixture-model',
        model_config_digest=hashlib.sha256(b'{}').hexdigest(), consumers=(('model_config', config.active_version_id),))
    identity = owner_identity(case.user.pk)
    reservation = om.CreateReservation.objects.create(actor=case.user, request_id=case.operation.request_id,
        digest=case.operation.digest, expires_at=case.operation.not_after, epoch=case.operation.epoch,
        project=case.project, policy_id=selected.policy_id, policy_digest=selected.digest,
        bootstrap_approval_id=selected.bootstrap_policy.approval_id,
        bootstrap_version_id=selected.bootstrap_policy.version_id, bootstrap_digest=selected.bootstrap_policy.digest,
        git_name=identity.name, git_email=identity.email)
    binding, _ = om.CreateBinding.objects.get_or_create(operation=case.operation, defaults=dict(
        definition_id=reserved.definition_id, scheduler_id=uuid4(), job_id=str(case.assignment.id), namespace='default'))
    from mop.server.launch_spec import API_LAUNCH_CAPABILITIES
    # Trusted SQL fixture -- заявленный owned anchor, не physical acceptance.
    scheduler_node, _ = om.ScheduledNode.objects.get_or_create(node_id=case.assignment.node_id,
        defaults=dict(scheduler_id=binding.scheduler_id, nomad_node_id=uuid4(),
            capabilities=sorted(API_LAUNCH_CAPABILITIES),
            anchor_executable='/installed/sha256-'+'a'*64+'/anchor', anchor_digest='a'*64))
    binding.reservation, binding.applied_index = reservation, 1
    binding.dispatched_job = dict(Constraints=[dict(LTarget='${node.unique.id}', Operand='=', RTarget=str(scheduler_node.nomad_node_id))])
    binding.save()
    om.ScheduledPuppet.objects.update_or_create(target_id=case.assignment.id, defaults=dict(
        incarnation_id=reserved.incarnation, scheduler_id=binding.scheduler_id, job_id=binding.job_id,
        namespace=binding.namespace, epoch=case.operation.epoch,
        restore_generation=case.operation.restore_generation, modify_index=1))
    em.NodeExecutor.objects.get_or_create(node_id=case.assignment.node_id,
        defaults=dict(executor_epoch=case.assignment.current_incarnation.executor_epoch))
    case.pin = pin


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
            action='create', state='running', revision=6,
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
        from mop.common.initialcreate import PHASES
        for ordinal, action in enumerate(PHASES, 1):
            step = om.Step.objects.create(operation=self.operation, ordinal=ordinal, name=action)
            if ordinal <= 4:
                step.token = step.attempt_id = uuid4()
                step.started_at = step.dispatched_at = step.quiescent_at = timezone.now()
                step.outcome = 'succeeded'
                step.save()
                om.EffectReceipt.objects.create(step=step, token=step.token, epoch=self.operation.epoch,
                    node_id=self.assignment.node_id, source_revision=self.operation.expected_revision,
                    decision_revision=ordinal, applied=True)
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
        selected_fixture(self, self.reserved)
        # #721: все четыре fences остаются у первого prepare, а не scheduler.
        prepare = om.Step.objects.get(operation=self.operation, name='prepare_body')
        for key in ('target:'+str(self.assignment.id), 'node:'+str(self.assignment.node_id),
                    'project:'+str(self.project.id), 'puppet:'+str(self.assignment.id)):
            om.Resource.objects.get_or_create(key=key)
            om.ResourceClaim.objects.create(resource_id=key, operation=self.operation, step=prepare, token=prepare.token)
        work = self.execution.poll(self.machine, self.assignment.node_id,
            executor_epoch=self.assignment.current_incarnation.executor_epoch)[0]
        self.attempt = self.execution.claim(self.machine, work, request_id=uuid4())
        from mop.server.sessions.protocol import work_to_wire
        self.metadata = work_to_wire(work)
        return permission

    def post(self, *, auth=None, metadata=None, path='/api/v1/machine/work/launch'):
        from django.db import connections
        try:
            return Client(HTTP_AUTHORIZATION='Token '+(auth or self.machine_auth).value).post(
                path, metadata or self.metadata,
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
        # Полный phase proof с provider rechecks проверяется в production 5s
        # socket budget, не в искусственном 2s deadline коротких wire-тестов.
        async_to_sync(self.exercise)(exercise, application=app, timeout=5, client_timeout=5)

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

    def test_native_initial_delivery_roundtrips_strict_socket_and_http_without_widening_old_response(self):
        from mop.common.initialcreate import parse_delivery
        from mop.common import credentials as p
        self.fixture()
        async def body(client, server):
            self.assertEqual((client.timeout, server.timeout), (5, 5))
            direct = await asyncio.to_thread(client.read_initial_launch, self.machine_auth, self.metadata)
            response = await asyncio.to_thread(self.post, path='/api/v1/machine/work/initial-launch')
            self.assertEqual(response.status_code, 200, response.content)
            self.assertEqual(response['Cache-Control'], 'no-store')
            value = parse_delivery(response.json(), self.metadata)
            self.assertEqual(value, direct)
            self.assertEqual(p.binary(value['role']['prompt']), b'private')
            self.assertIsNone(value['bootstrap'])
            self.assertEqual(value['selection']['source']['commit'], 'd'*40)
            self.assertEqual(value['selection']['desired_model'], 'fixture-model')
            self.assertEqual([item['purpose'] for item in value['consumers']], ['model_config'])
            self.assertEqual(p.binary(value['consumers'][0]['value']), b'{}')
            for field in ('secret_id', 'secret_name', 'owner_id', 'policy_id'):
                denied = await asyncio.to_thread(self.post, path='/api/v1/machine/work/initial-launch',
                    metadata={**self.metadata, field: str(uuid4())})
                self.assertEqual(denied.status_code, 400, denied.content)
                self.assertNotIn(base64.b64encode(b'private'), denied.content)
        self.run_delivery(body)

    def initial_metadata(self):
        policy = self.pin.image_revision.policy
        return dict(display_name=self.assignment.display_name, image_revision=str(self.pin.image_revision_id),
            image_policy=dict(policy_id=str(policy.pk), cpu=policy.cpu, memory_mb=policy.memory_mb, disk_mb=policy.disk_mb))

    # HYPOTHESIS #738: layout/resources отсутствуют в exact response либо
    # меняются во время provider; observed/latest image не заменяет admitted pin.
    # SPEC: настоящие PostgreSQL + socket + client, immutable ImageRevision.policy,
    # прежние Work/purpose и 5s deadlines; никакого physical acceptance.
    # RESULT (RED): 7/10 отказов — metadata отсутствовали, смена name/policy/
    # resources возвращала 200; отсутствие name не останавливало provider.
    # SOLUTION: уже выбранные IncarnationLaunch/ImageRevision.policy/Assignment
    # включены в прежний proof до/после provider, без новой authority-петли.
    # RESULT: Python 3.12/PostgreSQL 17 — 171/171 delivery/create/producer checks,
    # включая verified HTTPS; logical DTO, не native/physical acceptance.
    # STATUS: FIXED — see #738.
    def test_initial_metadata_survives_new_latest_policy_over_socket_and_http(self):
        from mop.server.catalog import models as cm
        self.fixture()
        policy = cm.ProjectPolicySnapshot.objects.create(project=self.project, sequence=2, content=b'new',
            digest='f'*64, source_commit='f'*40, source_ref='refs/heads/new', provenance='fixture',
            cpu=900, memory_mb=4096, disk_mb=8192)
        image = cm.ImageRevision.objects.create(policy=policy, source='new-latest-not-admitted')
        from mop.server.controller.initialselection import configure
        admitted = cm.InitialCreateSelection.objects.select_related('policy').get(project=self.project,
            owner=self.user, definition_id=self.reserved.definition_id).policy
        refs = tuple(cm.InitialCreateConsumer.objects.filter(policy=admitted)
            .values_list('purpose', 'version_id'))
        configure(self.access.token_context(self.token), project_id=self.project.pk,
            definition_id=self.reserved.definition_id, bootstrap_revision=self.pin.bootstrap_revision,
            image_id=image.pk, source_origin=self.project.origin, source_commit='f'*40,
            source_ref='refs/heads/new', desired_model=admitted.desired_model,
            model_config_digest=admitted.model_config_digest, consumers=refs)
        metadata = self.initial_metadata()
        async def body(client, server):
            direct = await asyncio.to_thread(client.read_initial_launch, self.machine_auth, self.metadata)
            for field, expected in metadata.items():
                self.assertEqual(direct.get(field), expected)
            for field in ('image_source', 'installed', 'receipt', 'capabilities', 'path', 'argv'):
                self.assertNotIn(field, direct)
            response = await asyncio.to_thread(self.post, path='/api/v1/machine/work/initial-launch')
            self.assertEqual(response.status_code, 200, response.content)
            self.assertEqual(response.json(), direct)
            self.assertNotEqual(direct['image_revision'], str(image.pk))
            self.assertNotEqual(direct['image_policy']['policy_id'], str(policy.pk))
            self.assertEqual(direct['work'], self.metadata)
        self.run_delivery(body)

    def initial_during_provider(self, change):
        from mop.server.controller import bootstrap
        from mop.server.controller.models import Installation
        row = Installation.objects.get(pk=1)
        real = bootstrap.secret_provider(self.bootstrap, installation_id=row.identity,
            expected_root_digest=row.root_digest, key_id='root-v1')
        changed = []
        class Changing:
            def decrypt(inner, envelope, context):
                self.assertFalse(connection.in_atomic_block)
                value = real.decrypt(envelope, context)
                if not changed:
                    change()
                    changed.append(True)
                return value
        async def body(client, server):
            response = await asyncio.to_thread(self.post, path='/api/v1/machine/work/initial-launch')
            self.assertEqual(response.status_code, 403, response.content)
            for private in (b'private', b'{}'):
                self.assertNotIn(base64.b64encode(private), response.content)
            for field in (b'image_policy', b'display_name', b'image_revision'):
                self.assertNotIn(field, response.content)
        self.run_delivery(body, provider=Changing())
        self.assertTrue(changed, 'must change authority after actual decrypt outside SQL')

    def test_initial_display_name_change_after_provider_refuses_without_metadata(self):
        from mop.server.sessions.models import Assignment
        self.fixture()
        self.initial_during_provider(lambda: Assignment.objects.filter(pk=self.assignment.pk)
            .update(display_name='pu-socket-project-2'))

    def test_initial_image_policy_change_after_provider_refuses_without_metadata(self):
        from mop.server.catalog.models import ImageRevision, ProjectPolicySnapshot
        self.fixture()
        policy = ProjectPolicySnapshot.objects.create(project=self.project, sequence=2, content=b'new',
            digest='f'*64, source_commit='f'*40, source_ref='refs/heads/new', provenance='fixture',
            cpu=900, memory_mb=4096, disk_mb=8192)
        # Fault injection: обычный ImmutableRecord API такое изменение запрещает.
        self.initial_during_provider(lambda: ImageRevision._base_manager.filter(pk=self.pin.image_revision_id)
            .update(policy=policy))

    def test_initial_resource_change_after_provider_refuses_without_metadata(self):
        from mop.server.catalog.models import ProjectPolicySnapshot
        self.fixture()
        self.initial_during_provider(lambda: ProjectPolicySnapshot._base_manager
            .filter(pk=self.pin.image_revision.policy_id).update(cpu=900))

    def test_initial_token_revocation_after_provider_refuses_without_metadata(self):
        from rest_framework.authtoken.models import Token
        self.fixture()
        self.initial_during_provider(lambda: Token.objects.filter(user_id=self.machine.user_id).delete())

    def initial_before_provider(self, change):
        from mop.server.controller import bootstrap
        from mop.server.controller.models import Installation
        change()
        row = Installation.objects.get(pk=1)
        real = bootstrap.secret_provider(self.bootstrap, installation_id=row.identity,
            expected_root_digest=row.root_digest, key_id='root-v1')
        class Guard:
            calls = 0
            def decrypt(inner, envelope, context):
                inner.calls += 1
                return real.decrypt(envelope, context)
        guard = Guard()
        async def body(client, server):
            response = await asyncio.to_thread(self.post, path='/api/v1/machine/work/initial-launch')
            self.assertEqual(response.status_code, 403, response.content)
            self.assertEqual(guard.calls, 0)
        self.run_delivery(body, provider=guard)

    def test_initial_missing_name_refuses_before_provider(self):
        from mop.server.sessions.models import Assignment
        self.fixture()
        self.initial_before_provider(lambda: Assignment.objects.filter(pk=self.assignment.pk).update(display_name=None))

    def test_initial_invalid_name_refuses_before_provider(self):
        from mop.server.sessions.models import Assignment
        self.fixture()
        self.initial_before_provider(lambda: Assignment.objects.filter(pk=self.assignment.pk).update(display_name='../clone'))

    def test_initial_different_policy_image_refuses_before_provider(self):
        from mop.server.sessions.models import IncarnationLaunch
        from mop.server.catalog.models import ProjectPolicySnapshot, ImageRevision
        from mop.server.operations.launch import image_pin
        self.fixture()
        policy = ProjectPolicySnapshot.objects.create(project=self.project, sequence=2, content=b'new',
            digest='f'*64, source_commit='f'*40, source_ref='refs/heads/new', provenance='fixture',
            cpu=900, memory_mb=4096, disk_mb=8192)
        image = ImageRevision.objects.create(policy=policy, source='different-policy')
        self.initial_before_provider(lambda: IncarnationLaunch.objects.filter(pk=self.pin.pk)
            .update(image_revision=image, image_digest=image_pin(image.pk)))

    def test_initial_delivery_rechecks_selection_after_real_provider_wait(self):
        from mop.server.controller import bootstrap
        from mop.server.controller.models import Installation
        self.fixture()
        row = Installation.objects.get(pk=1)
        real = bootstrap.secret_provider(self.bootstrap, installation_id=row.identity,
            expected_root_digest=row.root_digest, key_id='root-v1')
        changed = []
        class Changing:
            def decrypt(inner, envelope, context):
                self.assertFalse(connection.in_atomic_block)
                value = real.decrypt(envelope, context)
                from django.contrib.auth import get_user_model
                get_user_model().objects.filter(pk=self.user.pk).update(email='changed@example.invalid')
                changed.append(True)
                return value
        async def body(client, server):
            response = await asyncio.to_thread(self.post, path='/api/v1/machine/work/initial-launch')
            self.assertEqual(response.status_code, 403, response.content)
            self.assertNotIn(base64.b64encode(b'private'), response.content)
            self.assertNotIn(base64.b64encode(b'{}'), response.content)
        self.run_delivery(body, provider=Changing())
        self.assertTrue(changed, 'must execute real decrypt outside SQL before authority changes')

    def test_real_verified_https_transport_binds_addressed_launch_source(self):
        self.fixture()
        metadata = self.initial_metadata()
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
                    from mop.common.initialcreate import parse_delivery
                    selected = parse_delivery(await machine.transport.request('POST', 'work/initial-launch',
                        payload=work.wire()), work.wire())
                    self.assertEqual(base64.b64decode(selected['role']['prompt']), b'private')
                    self.assertEqual(selected['phase'], 'native_exec')
                    for field, expected in metadata.items():
                        self.assertEqual(selected[field], expected)
                    self.assertEqual(selected['selection']['source']['ref'], 'refs/heads/exact-source')
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
