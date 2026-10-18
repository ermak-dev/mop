"""Создание папета (#643): admission и SQL-композиция до внешних эффектов.

HYPOTHESIS: у журнала нет create -- ни маршрута, ни handler: назначение,
агентский Actor, грант, инкарнацию и пины запуска заводят вручную фикстуры
restart-вертикали, а production создать папета не может вовсе.
SPEC: POST /api/v1/projects/<id>/commands/create-puppet {node_id,
definition_id} -- человек с execution.create_puppet и assign_work. Сервер сам
назначает target, инкарнацию (эпоха -- заявленная executor узла) и имя
pu-<project>-<n>; пины -- одобренный bootstrap владельца и готовый образ узла.
Агент регистрируется, грант резервируется и выдаётся через порт
credential-сервиса от имени того же человека. Operation create со шагами
scheduler_register, apply_identity, confirm_ready, activate_grant. Без привязок
-- controller_unavailable до записей; повтор запроса -- тот же результат без
второго назначения; отказ любой проверки -- без строк.
STATUS: FIXED — see #643 (admission; шаги регистрации, узла и гранта -- далее)
"""
import hashlib
import json
from datetime import timedelta
from uuid import UUID, uuid4

from django.contrib.auth.models import Permission
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from _restart_vertical_cases import RestartVertical


class Issuer:
    """Порт credential-сервиса: reserve/provision от имени credential человека.

    Как настоящий сервис: actor берётся из переданного credential, не из
    вызывающего; ответ -- GrantReceipt протокола, не ORM-строка."""
    def __init__(self, grants):
        self.grants, self.calls = grants, []

    def _receipt(self, row):
        from mop.common import credentials as p
        return p.GrantReceipt(row.id, row.project_id, row.target_id, row.incarnation,
            row.role, row.digest, row.state, row.revision, row.credential_version_id)

    def reserve_grant(self, auth, **values):
        from mop.server.access import services
        self.calls.append(('reserve', auth.kind))
        return self._receipt(self.grants.reserve(services.token_context(auth.value), **values))

    def provision_grant(self, auth, grant_id, *, expected_revision):
        from mop.server.access import services
        self.calls.append(('provision', auth.kind))
        return self._receipt(self.grants.provision(services.token_context(auth.value), grant_id,
                                                   expected_revision=expected_revision))


class Scheduler:
    """Только capability и identity порта: admission не пишет в планировщик."""
    def __init__(self):
        self.scheduler_id = uuid4()
        self.capabilities = frozenset({'conditional_register_job', 'inspect_register_job'})


@override_settings(ALLOWED_HOSTS=['testserver'])
class CreateVertical(TransactionTestCase):
    serialized_rollback = True
    setUp = RestartVertical.setUp

    def ready(self, *, bootstrap=True, image=True, executor=True, approve=True, enroll=True, scheduler=None):
        from _grant_cases import manifest
        from mop.server.access import services as auth
        from mop.server.access.grants import Grants
        from mop.server.controller.machinebinding import AssignmentBindings
        from mop.server.catalog.services import Catalog
        from mop.server.preparation.services import Preparation
        from mop.server.secrets.crypto import Keyring, XChaCha20Provider
        provider = XChaCha20Provider(Keyring('fixture', {'fixture': b'c'*32}))
        self.preparation = Preparation(provider=provider, authorizer=auth.Access())
        self.grants = Grants(provider=provider, approvals=self.preparation.approval_query(),
                             bindings=AssignmentBindings())
        self.definition = self.grants.publish_definition(self.actor, project_id=self.project.pk,
            manifest=json.dumps(manifest()).encode(), prompt=b'fixture role', provenance='controlled fixture')
        if approve:
            self.grants.approve_definition(self.actor, self.definition.id)
        if bootstrap:
            self.revision = self.preparation.publish(self.actor, project_id=self.project.pk, tasks=b'- debug: msg=hello\n',
                variables=b'answer: 42\n', provenance='git:example.invalid/vertical', commit='b'*40, secret_refs=())
            self.bootstrap_approval = self.preparation.approve(self.actor, self.revision.id)
        if image:
            catalog = Catalog(authorizer=auth.Access())
            from mop.server.catalog.models import Project
            snap = catalog.pin_project_policy(self.actor, self.project.pk, b'role: worker\n', source_commit='c'*40,
                source_ref='refs/heads/main', provenance='git:test', cpu=100, memory_mb=256, disk_mb=1024,
                expected_revision=Project.objects.get(pk=self.project.pk).revision)
            self.image = catalog.create_image_revision(self.actor, self.project.pk, snap.id, source='builder:fixture')
            state = catalog.set_image_intent(self.actor, self.node.pk, self.project.pk, self.image.id, expected_revision=0)
            catalog.observe_image(self.actor, self.node.pk, self.project.pk, self.image.id, available=True,
                source='scheduler:meta', observed_at=timezone.now(), receipt='fixture-1', expected_revision=state.revision)
        self.boot = uuid4()
        if executor:
            # Executor пустого узла заявляет эпоху тем же машинным маршрутом, что в бою.
            from django.test import Client
            key = auth.enroll_service('fixture-executor', self.node.pk, project_ids=(self.project.pk,),
                permissions=('execution.poll_work', 'execution.claim_work', 'execution.report_result', 'execution.read_facts'))
            declared = Client(HTTP_AUTHORIZATION='Token '+key).post('/api/v1/machine/executor/restarted',
                dict(schema_version=1, node_id=str(self.node.pk), previous_epoch=str(uuid4()), new_epoch=str(self.boot)),
                content_type='application/json', secure=True)
            self.assertEqual(declared.status_code, 200, declared.content)
        self.issuer, self.scheduler = Issuer(self.grants), scheduler or Scheduler()
        if enroll:
            # Настройки пула и доверенная привязка узла к планировщику (#643):
            # рендер спеки берёт home/user/datacenter и id узла в Nomad отсюда.
            from mop.server.controller.createbinding import enroll_node
            from mop.server.spec import API_LAUNCH_CAPABILITIES
            catalog = Catalog(authorizer=auth.Access())
            catalog.set_installation_settings(self.actor, {'pool_home': '/srv/mop', 'pool_user': 'mop',
                'pool_datacenter': 'owned'}, expected_revision=catalog.get_installation_settings(self.actor).revision)
            self.nomad_node = uuid4()
            enroll_node(self.node.pk, scheduler_id=self.scheduler.scheduler_id, nomad_node_id=self.nomad_node,
                        capabilities=API_LAUNCH_CAPABILITIES)
        from unittest.mock import patch
        from mop.server.controller.createbinding import service
        self.addCleanup(patch.stopall)
        self.ops = service(grants=self.grants, issuer=self.issuer, scheduler=self.scheduler)
        patch('mop.server.controller.operationviews.create_api', lambda: self.ops).start()

    def body(self, **changes):
        value = dict(schema_version=1, epoch=str(self.epoch), request_id=str(uuid4()),
            not_after=(timezone.now()+timedelta(minutes=2)).isoformat(),
            params={'node_id': str(self.node.pk), 'definition_id': str(self.definition.id)})
        value.update(changes)
        return value

    def submit(self, body, client=None):
        return (client or self.client).post('/api/v1/projects/'+str(self.project.pk)+'/commands/create-puppet',
            body, content_type='application/json', secure=True)

    def rows(self):
        from mop.server.access import models as a
        from mop.server.sessions.models import Assignment
        from mop.server.operations.models import Operation
        return (Assignment.objects.count(), a.Grant.objects.count(), a.AgentProfile.objects.count(),
                Operation.objects.filter(action='create').count())

    def test_create_route_is_admitted(self):
        self.ready()
        response = self.submit(self.body())
        self.assertNotEqual(response.status_code, 404, 'typed create admission must be routed')

    def test_create_assigns_target_incarnation_name_pins_and_grant(self):
        self.ready()
        body = self.body()
        response = self.submit(body)
        self.assertEqual(response.status_code, 202, response.content)
        operation = response.json()['operation']
        from mop.server.access import models as a
        from mop.server.operations import models as om
        self.assertEqual((om.Operation.objects.get(pk=operation['id']).action, operation['state']), ('create', 'accepted'))
        from mop.server.sessions.models import Assignment, ExecutionIncarnation, IncarnationLaunch
        target_id = UUID(operation['target']['id'])
        row = Assignment.objects.get(pk=target_id)
        self.assertEqual((row.owner_id, row.project_id, row.node_id), (self.user.pk, self.project.pk, self.node.pk))
        self.assertEqual((row.role, row.bundle_digest), (self.definition.role, self.definition.digest))
        self.assertEqual(row.display_name, 'pu-vertical-1')
        incarnation = ExecutionIncarnation.objects.get(pk=row.current_incarnation_id)
        self.assertEqual(str(incarnation.pk), operation['target']['incarnation'])
        self.assertEqual(incarnation.executor_epoch, self.boot)
        launch = IncarnationLaunch.objects.get(incarnation=incarnation)
        self.assertEqual((launch.bootstrap_revision, launch.bootstrap_digest), (self.revision.id, self.revision.digest))
        self.assertEqual(launch.image_digest,
            hashlib.sha256(('image-revision:'+str(self.image.id)).encode()).hexdigest())
        grant = a.Grant.objects.select_related('intent').get(pk=row.grant_id)
        self.assertEqual((grant.intent.target_id, grant.intent.incarnation), (target_id, incarnation.pk))
        self.assertEqual(grant.state, 'reserved')
        self.assertIsNotNone(grant.credential_version_id)
        self.assertEqual(a.AgentProfile.objects.get(target_id=target_id).agent_id, grant.subject_id)
        self.assertEqual([kind for _, kind in self.issuer.calls], ['token', 'token'])
        steps = list(om.Step.objects.filter(operation_id=operation['id']).order_by('ordinal').values_list('name', flat=True))
        self.assertEqual(steps, ['scheduler_register', 'apply_identity', 'confirm_ready', 'activate_grant'])
        binding = om.CreateBinding.objects.get(operation_id=operation['id'])
        self.assertEqual((binding.scheduler_id, binding.job_id), (self.scheduler.scheduler_id, str(target_id)))
        # Повтор того же запроса -- тот же Operation, без второго назначения и гранта.
        again = self.submit(body)
        self.assertEqual(again.json()['operation']['id'], operation['id'])
        self.assertEqual(self.rows(), (1, 1, 1, 1))
        # Следующий create того же проекта -- следующее имя.
        second = self.submit(self.body())
        self.assertEqual(second.status_code, 202, second.content)
        self.assertEqual(Assignment.objects.get(pk=second.json()['operation']['target']['id']).display_name,
                         'pu-vertical-2')

    # HYPOTHESIS (#704): expires_at пересчитывается после committed reserve,
    # пока Operation ещё нет; неизменный envelope перестаёт быть повтором.
    # Digest всего create должен сохраняться до первого credential-вызова:
    # иначе другой node/deadline примет тот же грант после потери ответа.
    # SOLUTION: CreateReservation фиксирует digest/expiry в независимом COMMIT
    # до I/O; SQL unique actor/request и общий admission lock сводят повторы.
    # RESULT: PG17 RED -- потеря ответа/падение/гонка дают Conflict,
    # чужой digest принят (202 вместо 409). GREEN -- 27 проверок create,
    # schema и reserve; точное равенство credential intent не менялось.
    # STATUS: FIXED — see #704
    def reservation_rows(self):
        from django.contrib.auth import get_user_model
        from rest_framework.authtoken.models import Token
        from mop.server.access import models as a
        return (get_user_model().objects.count(), a.AgentProfile.objects.count(), Token.objects.count(),
                a.CredentialBinding.objects.count(), a.CreationIntent.objects.count(), a.Grant.objects.count())

    def lose_reservation_reply(self, body):
        from unittest.mock import patch
        reserve = self.issuer.reserve_grant

        def lose(*args, **kwargs):
            reserve(*args, **kwargs)
            raise OSError('reservation reply lost after commit')

        with patch.object(self.issuer, 'reserve_grant', side_effect=lose), \
                self.assertRaisesRegex(OSError, 'reservation reply lost after commit'):
            self.submit(body)
        from mop.server.access.models import Grant
        from mop.server.operations.models import Operation
        self.assertFalse(Operation.objects.exists())
        return Grant.objects.select_related('intent').get()

    def test_lost_reservation_reply_retries_the_same_immutable_create(self):
        from unittest.mock import patch
        from mop.server.controller.createbinding import service
        from mop.server.sessions.models import Assignment
        self.ready()
        body, now = self.body(), timezone.now()
        with patch('mop.server.operations.launch.timezone.now', return_value=now):
            grant = self.lose_reservation_reply(body)
        before = self.reservation_rows()
        # Новый handler после падения процесса, не cache прежнего экземпляра.
        self.ops = service(grants=self.grants, issuer=self.issuer, scheduler=self.scheduler)
        with patch('mop.server.operations.launch.timezone.now', return_value=now + timedelta(seconds=45)):
            response = self.submit(body)
        self.assertEqual(response.status_code, 202, response.content)
        operation = response.json()['operation']
        row = Assignment.objects.get(pk=operation['target']['id'])
        self.assertEqual((row.grant_id, row.id, row.current_incarnation_id, row.expires_at),
            (grant.id, grant.intent.target_id, grant.intent.incarnation, grant.intent.expires_at))
        self.assertEqual(self.reservation_rows(), before)
        self.assertEqual(self.rows(), (1, 1, 1, 1))
        from mop.server.operations.models import CreateReservation
        pinned = CreateReservation.objects.get()
        self.assertEqual((pinned.request_id, pinned.expires_at), (UUID(body['request_id']), grant.intent.expires_at))
        self.assertEqual(self.submit(body).json()['operation']['id'], operation['id'])
        self.assertEqual(self.reservation_rows(), before)

    def test_changed_create_envelope_after_lost_reservation_reply_refuses(self):
        from unittest.mock import patch
        from mop.server.catalog.models import Node, NodeProject
        self.ready()
        body, now = self.body(), timezone.now()
        other = Node.objects.create(name='another', driver='host', ssh_host='example.invalid',
            control_origin='https://example.invalid', lan_address='example.invalid', bootstrap_address='example.invalid')
        NodeProject.objects.create(node=other, project=self.project)
        # Оба узла допустимы: отказ должен быть по digest, не из-за пинов.
        from mop.server.controller.createbinding import enroll_node
        from mop.server.spec import API_LAUNCH_CAPABILITIES
        from mop.server.catalog.models import NodeImage
        from mop.server.sessions.models import NodeExecutor
        original = NodeImage.objects.get(node=self.node, project=self.project)
        NodeImage.objects.create(node=other, project=self.project, desired_image_id=original.desired_image_id,
            observed_image_id=original.observed_image_id, available=True)
        NodeExecutor.objects.create(node=other, executor_epoch=self.boot)
        enroll_node(other.pk, scheduler_id=self.scheduler.scheduler_id, nomad_node_id=uuid4(),
                    capabilities=API_LAUNCH_CAPABILITIES)
        with patch('mop.server.operations.launch.timezone.now', return_value=now):
            self.lose_reservation_reply(body)
        before, calls = self.reservation_rows(), list(self.issuer.calls)
        changed = (dict(body, not_after=(now + timedelta(minutes=3)).isoformat()),
                   dict(body, params=dict(body['params'], node_id=str(other.pk))))
        for envelope in changed:
            # Тот же clock: один лишь другой digest обязан отказать даже
            # при точном совпадении срока существующего credential intent.
            with self.subTest(envelope=envelope), \
                    patch('mop.server.operations.launch.timezone.now', return_value=now):
                response = self.submit(envelope)
                self.assertEqual(response.status_code, 409, response.content)
                self.assertEqual(response.json()['error']['code'], 'request_conflict')
        self.assertEqual(self.reservation_rows(), before)
        self.assertEqual(self.issuer.calls, calls)
        self.assertEqual(self.rows(), (0, 1, 1, 0))

    def test_crash_before_compose_retries_the_original_reservation(self):
        from unittest.mock import patch
        from mop.server.access.models import Grant
        from mop.server.sessions.models import Assignment
        self.ready()
        body, now = self.body(), timezone.now()
        with patch('mop.server.operations.launch.timezone.now', return_value=now), \
                patch.object(self.ops.creates, '_compose', side_effect=OSError('crash before compose')), \
                self.assertRaisesRegex(OSError, 'crash before compose'):
            self.submit(body)
        grant = Grant.objects.select_related('intent').get()
        self.assertIsNotNone(grant.credential_version_id)
        before = self.reservation_rows()
        with patch('mop.server.operations.launch.timezone.now', return_value=now + timedelta(seconds=30)):
            response = self.submit(body)
        self.assertEqual(response.status_code, 202, response.content)
        row = Assignment.objects.get(pk=response.json()['operation']['target']['id'])
        self.assertEqual((row.grant_id, row.expires_at), (grant.id, grant.intent.expires_at))
        self.assertEqual(self.reservation_rows(), before)
        self.assertEqual(self.rows(), (1, 1, 1, 1))

    def test_overlapping_retry_recovers_a_committed_reservation_without_operation(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier, Event, local
        from unittest.mock import patch
        from django.db import connections
        from mop.common.credentials import Credential
        from mop.server.operations.commands import parse_create
        from mop.server.sessions.models import Assignment
        self.ready()
        body, now = self.body(), timezone.now()
        command = parse_create(self.project.pk, body)
        reserve = self.issuer.reserve_grant
        reached, committed, first_reached, clock = Barrier(2), Event(), Event(), local()

        def overlap(*args, **kwargs):
            if clock.first:
                first_reached.set()
            reached.wait(timeout=5)
            if clock.first:
                receipt = reserve(*args, **kwargs)
                committed.set()
                raise OSError('reservation reply lost after commit')
            self.assertTrue(committed.wait(timeout=5))
            return reserve(*args, **kwargs)

        def submit(first):
            clock.first, clock.now = first, now + timedelta(seconds=0 if first else 20)
            try:
                try:
                    return self.ops.creates.submit(self.actor, command, credential=Credential('token', self.key))
                except OSError as error:
                    self.assertTrue(first)
                    self.assertEqual(str(error), 'reservation reply lost after commit')
                    return None
            finally:
                connections.close_all()

        with patch('mop.server.operations.launch.timezone.now', side_effect=lambda: getattr(clock, 'now', now)), \
                patch.object(self.issuer, 'reserve_grant', side_effect=overlap), \
                ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(submit, True)
            self.assertTrue(first_reached.wait(timeout=5))
            second = pool.submit(submit, False)
            self.assertIsNone(first.result(timeout=15))
            operation = second.result(timeout=15)
        self.assertEqual(self.rows(), (1, 1, 1, 1))
        from mop.server.access import models as a
        from rest_framework.authtoken.models import Token
        grant = a.Grant.objects.select_related('intent').get()
        row = Assignment.objects.get(pk=operation['target']['id'])
        self.assertEqual((row.grant_id, row.expires_at), (grant.id, grant.intent.expires_at))
        self.assertEqual(a.CreationIntent.objects.count(), 1)
        self.assertEqual(Token.objects.filter(user__kind='agent').count(), 1)
        self.assertEqual(a.GrantEvent.objects.filter(action='reserve').count(), 1)

    def test_create_requires_its_own_permission(self):
        self.ready()
        from mop.server.access import models as a
        self.user.is_superuser = False
        self.user.save(update_fields=['is_superuser'])
        a.ProjectMembership.objects.get_or_create(user=self.user, project=self.project,
            source=a.IdentitySource.objects.get(name='local'))
        for code in ('assign_work', 'view_operation', 'issue_grant', 'view_role', 'approve_role'):
            self.user.user_permissions.add(*Permission.objects.filter(codename=code))
        response = self.submit(self.body())
        self.assertEqual(response.status_code, 403, response.content)
        self.assertEqual(self.rows(), (0, 0, 0, 0))

    def test_unapproved_definition_creates_nothing(self):
        self.ready(approve=False)
        response = self.submit(self.body())
        self.assertIn(response.status_code, (403, 409), response.content)
        self.assertEqual(self.rows(), (0, 0, 0, 0))

    def _refused_without(self, missing):
        self.ready(**{missing: False})
        response = self.submit(self.body())
        self.assertIn(response.status_code, (409, 503), response.content)
        self.assertEqual(self.rows(), (0, 0, 0, 0))

    def test_no_approved_bootstrap_refuses_before_any_row(self):
        self._refused_without('bootstrap')

    def test_no_ready_node_image_refuses_before_any_row(self):
        self._refused_without('image')

    def test_no_declared_executor_refuses_before_any_row(self):
        self._refused_without('executor')

    def test_node_without_scheduler_enrollment_refuses_before_any_row(self):
        self._refused_without('enroll')

    def test_node_outside_the_project_creates_nothing(self):
        self.ready()
        from mop.server.catalog.models import Node
        other = Node.objects.create(name='other', driver='host', ssh_host='example.invalid',
            control_origin='https://example.invalid', lan_address='example.invalid', bootstrap_address='example.invalid')
        response = self.submit(self.body(params={'node_id': str(other.pk), 'definition_id': str(self.definition.id)}))
        self.assertEqual(response.status_code, 403, response.content)
        self.assertEqual(self.rows(), (0, 0, 0, 0))

    def test_production_default_refuses_before_any_row(self):
        self.ready()
        from unittest.mock import patch
        patch.stopall()
        response = self.submit(self.body())
        self.assertEqual(response.status_code, 503, response.content)
        self.assertEqual(self.rows(), (0, 0, 0, 0))


# HYPOTHESIS (#643): шаг scheduler_register некому исполнить: у порта нет
# полосы регистрации, воркер знает только однo-шаговые операции, и job нового
# тела в планировщик не попадает.
# SOLUTION: полоса LaunchJobs -- claim, рендер спеки из доверенных фактов
# (узел, привязка к планировщику, пины инкарнации, пин определения), одна
# условная запись с readback точной Meta, enrollment ScheduledPuppet; потеря
# ответа -- unknown и сверка только чтением; существующий job не трогается.
# STATUS: FIXED — see #643 (шаг регистрации; узел и грант -- далее)
@override_settings(ALLOWED_HOSTS=['testserver'])
class CreateRegistration(TransactionTestCase):
    serialized_rollback = True
    setUp = RestartVertical.setUp
    ready, body, submit = CreateVertical.ready, CreateVertical.body, CreateVertical.submit

    def launch(self):
        from _register_peer import RegisterPeer
        from mop.server.operations.scheduler import NomadStopScheduler
        self.peer = RegisterPeer()
        self.addCleanup(self.peer.close)
        self.adapter = NomadStopScheduler(origin=self.peer.origin, scheduler_id=uuid4(),
                                          token='fixture-only', timeout=2)
        self.ready(scheduler=self.adapter)
        response = self.submit(self.body())
        self.assertEqual(response.status_code, 202, response.content)
        value = response.json()['operation']
        return UUID(value['id']), value['target']['id'], value['target']['incarnation']

    def step(self, operation):
        from mop.server.operations.models import Step
        return Step.objects.get(operation_id=operation, name='scheduler_register')

    # HYPOTHESIS (#708): digest образа закреплён на admission, ресурсы же
    # рендер брал из текущего NodeImage -- после I1 -> I2 spec смешивала их.
    # SOLUTION: immutable ImageRevision FK рядом с digest, policy берётся
    # только из неё; образ уже не доступен на узле -- отказ до POST.
    # RESULT: RED -- Refused not raised; GREEN -- 20/20 с migration drift.
    # STATUS: FIXED — see #708
    def test_replaced_node_image_cannot_supply_resources_for_the_old_pin(self):
        from mop.server.access import services as auth
        from mop.server.catalog.models import NodeImage, Project
        from mop.server.catalog.services import Catalog
        from mop.server.operations.service import Refused
        operation, target, incarnation = self.launch()
        catalog = Catalog(authorizer=auth.Access())
        policy = catalog.pin_project_policy(self.actor, self.project.pk, b'role: replacement\n',
            source_commit='d'*40, source_ref='refs/heads/main', provenance='git:replacement',
            cpu=400, memory_mb=1024, disk_mb=1024,
            expected_revision=Project.objects.get(pk=self.project.pk).revision)
        image = catalog.create_image_revision(self.actor, self.project.pk, policy.id, source='builder:replacement')
        current = NodeImage.objects.get(node=self.node, project=self.project)
        state = catalog.set_image_intent(self.actor, self.node.pk, self.project.pk, image.id,
                                         expected_revision=current.revision)
        catalog.observe_image(self.actor, self.node.pk, self.project.pk, image.id, available=True,
            source='scheduler:meta', observed_at=timezone.now(), receipt='replacement-1',
            expected_revision=state.revision)
        with self.assertRaises(Refused):
            self.ops.launches.run(operation)
        self.assertEqual(self.peer.posts, [])
        self.assertNotIn(target, self.peer.jobs)

    def test_register_puts_the_rendered_job_once_and_enrolls_it(self):
        operation, target, incarnation = self.launch()
        self.assertIn(operation, self.ops.pending(limit=8, handlers=('scheduler_register',)))
        # Серверный шаг узлу не раздаётся: его machine work -- только действия узла.
        self.assertEqual(self.ops.pending_machine(node_id=self.node.pk, executor_epoch=self.boot,
                                                  project_ids=(self.project.pk,), limit=8), ())
        self.ops.launches.run(operation)
        self.assertEqual([(p['EnforceIndex'], p['JobModifyIndex']) for p in self.peer.posts], [(True, 0)])
        job = self.peer.jobs[target]
        task = job['TaskGroups'][0]['Tasks'][0]
        self.assertEqual((job['Meta']['mop.target_id'], job['Meta']['mop.incarnation_id']), (target, incarnation))
        self.assertEqual(job['Meta']['mop.bootstrap_digest'], self.revision.digest)
        self.assertEqual(job['Meta']['mop.image_digest'],
            hashlib.sha256(('image-revision:'+str(self.image.id)).encode()).hexdigest())
        self.assertEqual(job['Constraints'][0]['RTarget'], str(self.nomad_node))
        self.assertEqual((job['Datacenters'], task['User'], task['Env']['MOP_TARGET_ID']), (['owned'], 'mop', target))
        self.assertTrue(task['Env']['MOP_REGISTERED_LAUNCHER'].startswith('/srv/mop/runtime/'+incarnation))
        step = self.step(operation)
        self.assertEqual((step.outcome, step.evidence), ('succeeded', 'job-registered'))
        from mop.server.operations.models import CreateBinding, Operation, ScheduledPuppet
        self.assertEqual(CreateBinding.objects.get(operation_id=operation).applied_index, 1)
        enrolled = ScheduledPuppet.objects.get(target_id=target)
        self.assertEqual((str(enrolled.incarnation_id), enrolled.job_id, enrolled.modify_index), (incarnation, target, 1))
        self.assertEqual(Operation.objects.get(pk=operation).state, 'running')
        self.assertNotIn(operation, self.ops.pending(limit=8, handlers=('scheduler_register',)))
        self.ops.launches.run(operation)
        self.assertEqual(len(self.peer.posts), 1)

    # BUG (#706): reserved обходил живое pinned approval на обеих полосах.
    # HYPOTHESIS: scheduler проверял только state/target, а authorize_launch
    # не вызывал SQL-only проверки expiry, approval и creation authority.
    # SOLUTION: обе полосы используют Grants.authorize_launch с реальным
    # Preparation.approval_query; последняя SQL-проверка повторяет authority.
    # RESULT: RED -- registration после revoke и prepare=True; GREEN -- PG17.
    # STATUS: FIXED — see #706
    def test_revoked_pinned_bootstrap_blocks_registration_after_admission(self):
        from mop.server.operations.models import Operation
        operation, _, _ = self.launch()
        self.preparation.revoke(self.actor, self.bootstrap_approval.id)
        self.ops.launches.run(operation)
        self.assertEqual(self.peer.posts, [], 'registration ran after pinned approval revocation')
        row = Operation.objects.get(pk=operation)
        self.assertEqual(row.state, 'refused')
        self.assertIn('revoked', row.reason)
        self.assertIsNone(self.step(operation).token)

    def test_revoked_pinned_bootstrap_blocks_registration_after_claim(self):
        operation, _, _ = self.launch()
        ticket = self.ops.launches.claim(operation)
        self.assertIsNotNone(ticket)
        self.preparation.revoke(self.actor, self.bootstrap_approval.id)
        self.assertFalse(self.ops.launches.prepare(ticket), 'last SQL gate ignored approval revocation')
        self.assertEqual(self.peer.posts, [])
        self.assertIsNone(self.step(operation).dispatched_at)

    def test_default_scheduler_authority_is_bound_without_a_provider(self):
        from mop.server.controller.createbinding import service
        operation, _, _ = self.launch()
        # Production composition без подставленного Grants/crypto provider.
        default = service(issuer=self.issuer, scheduler=self.adapter)
        ticket = default.launches.claim(operation)
        self.assertIsNotNone(ticket)
        self.preparation.revoke(self.actor, self.bootstrap_approval.id)
        self.assertFalse(default.launches.prepare(ticket))
        self.assertEqual(self.peer.posts, [])

    def test_lost_register_reply_is_unknown_and_reconciles_by_reading(self):
        operation, target, _ = self.launch()
        self.peer.lose_post = True
        self.ops.launches.run(operation)
        from mop.server.operations.models import Operation, ScheduledPuppet
        self.assertEqual(Operation.objects.get(pk=operation).state, 'unknown')
        self.assertEqual(self.step(operation).outcome, 'unknown')
        self.ops.launches.run(operation)
        self.assertEqual(len(self.peer.posts), 1)
        self.ops.launches.reconcile(self.actor, operation)
        step = self.step(operation)
        self.assertEqual((step.outcome, step.evidence), ('succeeded', 'job-registered'))
        # Как у restart: сверка промежуточного шага не выдаёт оставшиеся эффекты
        # автоматически -- операция остаётся unknown и держит цель.
        self.assertEqual(Operation.objects.get(pk=operation).state, 'unknown')
        self.assertEqual(ScheduledPuppet.objects.get(target_id=target).modify_index, 1)
        self.assertEqual(len(self.peer.posts), 1)

    # HYPOTHESIS (#707): recovery заново рендерит spec текущим wrapper,
    # а сверять нужно байты исходного dispatch, не новую версию контроллера.
    # SOLUTION: точная несекретная spec сохраняется вместе с claim до POST;
    # recovery только читает её, отсутствие evidence -- отказ без освобождения.
    # RESULT: RED -- failed вместо succeeded; GREEN -- 5/5 с migration check.
    # STATUS: FIXED — see #707
    def test_register_recovery_uses_dispatched_spec_after_template_change(self):
        from unittest.mock import patch
        from mop.server import spec
        from mop.server.operations.models import Operation, ScheduledPuppet
        operation, target, _ = self.launch()
        self.peer.lose_post = True
        self.ops.launches.run(operation)
        self.assertEqual(Operation.objects.get(pk=operation).state, 'unknown')
        original = self.peer.jobs[target]['Meta'].copy()
        with patch.object(spec, 'API_WRAPPER', spec.API_WRAPPER + '\n# updated controller template\n'):
            self.ops.launches.reconcile(self.actor, operation)
        self.assertEqual((self.step(operation).outcome, self.step(operation).evidence),
                         ('succeeded', 'job-registered'))
        self.assertEqual(self.peer.jobs[target]['Meta'], original)
        self.assertEqual(len(self.peer.posts), 1)
        self.assertEqual(ScheduledPuppet.objects.get(target_id=target).modify_index, 1)
        self.assertEqual(Operation.objects.get(pk=operation).state, 'unknown')

    def test_an_existing_job_is_never_overwritten(self):
        operation, target, _ = self.launch()
        self.peer.jobs[target] = {'ID': target, 'Namespace': 'default', 'JobModifyIndex': 4,
                                  'Meta': {'owner': 'someone else'}, 'Stop': False}
        self.ops.launches.run(operation)
        from mop.server.operations.models import Operation, ScheduledPuppet
        step = self.step(operation)
        self.assertEqual((step.outcome, step.evidence), ('failed', 'conditional-refusal'),
                         Operation.objects.values_list('state', 'code', 'reason').get(pk=operation))
        self.assertEqual(Operation.objects.get(pk=operation).state, 'failed')
        self.assertEqual(self.peer.posts, [])
        self.assertEqual(self.peer.jobs[target]['Meta'], {'owner': 'someone else'})
        self.assertFalse(ScheduledPuppet.objects.filter(target_id=target).exists())


# HYPOTHESIS (#643): после регистрации узел не может исполнить шаги create:
# машинная полоса не знает действия create, а claim требует авторизации
# исполнения -- активного гранта с credential агента, которого до готовности
# нет. Активация же ждёт квитанций готовности: круг.
# SOLUTION: идентичность и готовность новой цели исполняются по выданному, но
# ещё не активному гранту (authorize_launch); всё прочее -- только по активному.
# STATUS: FIXED — see #643 (узловые шаги; активация гранта -- далее)
class CreateMachineSteps(TransactionTestCase):
    serialized_rollback = True
    setUp = RestartVertical.setUp
    ready, body, submit = CreateVertical.ready, CreateVertical.body, CreateVertical.submit
    launch, step = CreateRegistration.launch, CreateRegistration.step

    def machine(self):
        from mop.server.access import services as auth
        from mop.server.controller.createbinding import LaunchSupport
        from mop.server.controller.machinebinding import compose
        if getattr(self, '_executor_key', None) is None:
            self._executor_key = auth.enroll_service('create-executor', self.node.pk, project_ids=(self.project.pk,),
                permissions=('execution.poll_work', 'execution.claim_work', 'execution.report_result',
                             'execution.read_facts'))
        return compose(grants=self.grants, launch_support=LaunchSupport()), auth.token_context(self._executor_key)

    def test_node_confirms_identity_and_readiness_under_the_provisioned_grant(self):
        operation, target, _ = self.launch()
        service, actor = self.machine()
        # До регистрации job у узла нет работы этой цели.
        self.assertEqual(service.poll(actor, self.node.pk, executor_epoch=self.boot, limit=2), ())
        self.ops.launches.run(operation)
        for action, evidence in (('apply_identity', 'identity-applied'), ('confirm_ready', 'ready')):
            work, = service.poll(actor, self.node.pk, executor_epoch=self.boot, limit=2)
            self.assertEqual((work.operation_id, work.action), (operation, action))
            attempt = service.claim(actor, work, request_id=uuid4())
            service.result(actor, attempt.id, expected_revision=attempt.revision, receipt_id=uuid4(),
                           outcome='succeeded', evidence=evidence)
        from mop.server.operations.models import Operation, Step
        self.assertEqual(service.poll(actor, self.node.pk, executor_epoch=self.boot, limit=2), ())
        steps = dict(Step.objects.filter(operation_id=operation).values_list('name', 'outcome'))
        self.assertEqual(steps, {'scheduler_register': 'succeeded', 'apply_identity': 'succeeded',
                                 'confirm_ready': 'succeeded', 'activate_grant': ''})
        self.assertEqual(Operation.objects.get(pk=operation).state, 'running')

    # HYPOTHESIS (#705): успешный scheduler_register снимал target fence,
    # а машинные шаги её не возвращали -- release менял живое назначение.
    # SOLUTION: target claim остаётся за Operation; передаются только claims
    # полосы. Терминал освобождает всё, unknown по-прежнему держит цель.
    # RESULT: RED -- Conflict not raised; GREEN -- 11/11 create cases.
    # STATUS: FIXED — see #705
    def test_create_holds_target_fence_across_server_and_machine_stages(self):
        from mop.server.controller.runtime import assignments_service
        from mop.server.sessions.models import Assignment
        from mop.server.sessions.services import Conflict
        operation, target, _ = self.launch()
        self.ops.launches.run(operation)
        before = Assignment.objects.values().get(pk=target)

        def cannot_release():
            with self.assertRaises(Conflict):
                assignments_service().release(self.actor, UUID(target), expected_revision=before['revision'])
            self.assertEqual(Assignment.objects.values().get(pk=target), before)

        cannot_release()
        service, actor = self.machine()
        work, = service.poll(actor, self.node.pk, executor_epoch=self.boot, limit=2)
        attempt = service.claim(actor, work, request_id=uuid4())
        service.result(actor, attempt.id, expected_revision=attempt.revision, receipt_id=uuid4(),
                       outcome='succeeded', evidence='identity-applied')
        cannot_release()
        work, = service.poll(actor, self.node.pk, executor_epoch=self.boot, limit=2)
        attempt = service.claim(actor, work, request_id=uuid4())
        service.result(actor, attempt.id, expected_revision=attempt.revision, receipt_id=uuid4(),
                       outcome='unknown', evidence='connection-lost')
        cannot_release()

    def test_a_provisioned_grant_authorizes_launch_steps_only(self):
        operation, target, incarnation = self.launch()
        from django.db import transaction
        from mop.server.access.delegation import RoleBinding
        from mop.server.access.services import Denied
        from mop.server.controller.machinebinding import ReceivingGrant
        from mop.server.sessions.models import Assignment
        row = Assignment.objects.get(pk=target)
        values = dict(owner_id=row.owner_id, project_id=row.project_id, target_id=row.id, grant_id=row.grant_id,
                      role=row.role, bundle_digest=row.bundle_digest, incarnation_id=row.current_incarnation_id)
        receiving = ReceivingGrant(self.grants)
        for action in ('apply_identity', 'confirm_ready'):
            with transaction.atomic():
                receiving.authorize(**values, action=action)
        for action in ('send_message', 'restart_session', 'wipe_clone'):
            with self.subTest(action=action), self.assertRaises(Denied), transaction.atomic():
                receiving.authorize(**values, action=action)
        with self.assertRaises(Denied), transaction.atomic():
            self.grants.authorize_launch(RoleBinding(row.project_id, row.id, uuid4(), row.role,
                                                     row.bundle_digest, row.grant_id, row.owner_id))

    # BUG (#706): readiness исключала active, но вместе с ним теряла authority.
    # HYPOTHESIS: отдельная reserved-проверка должна повторять все общие
    # локальные guards; она не даёт reserved гранту активных действий.
    # SOLUTION: общий SQL-only gate проверяет точный binding, expiry/seal,
    # retained references, pinned approval и credential/права создателя.
    # RESULT: RED -- claim после revoke и PermissionError not raised;
    # GREEN -- 223/223 create/grant/preparation/execution/restart/send/wipe PG17.
    # STATUS: FIXED — see #706
    def launch_binding(self, target):
        from mop.server.access.delegation import RoleBinding
        from mop.server.sessions.models import Assignment
        row = Assignment.objects.get(pk=target)
        return RoleBinding(row.project_id, row.id, row.current_incarnation_id, row.role,
                           row.bundle_digest, row.grant_id, row.owner_id)

    def test_revoked_pinned_bootstrap_blocks_next_machine_claim(self):
        from mop.server.sessions.models import AssignmentAttempt
        operation, _, _ = self.launch()
        self.ops.launches.run(operation)
        service, actor = self.machine()
        work, = service.poll(actor, self.node.pk, executor_epoch=self.boot, limit=2)
        self.preparation.revoke(self.actor, self.bootstrap_approval.id)
        with self.assertRaisesRegex(PermissionError, 'revoked'):
            service.claim(actor, work, request_id=uuid4())
        self.assertFalse(AssignmentAttempt.objects.filter(operation_id=operation).exists())

    def test_default_machine_authority_is_bound_without_a_provider(self):
        from mop.server.controller.createbinding import LaunchSupport
        from mop.server.controller.machinebinding import compose
        operation, _, _ = self.launch()
        self.ops.launches.run(operation)
        _, actor = self.machine()
        service = compose(launch_support=LaunchSupport())
        work, = service.poll(actor, self.node.pk, executor_epoch=self.boot, limit=2)
        attempt = service.claim(actor, work, request_id=uuid4())
        service.result(actor, attempt.id, expected_revision=attempt.revision, receipt_id=uuid4(),
                       outcome='succeeded', evidence='identity-applied')
        work, = service.poll(actor, self.node.pk, executor_epoch=self.boot, limit=2)
        self.preparation.revoke(self.actor, self.bootstrap_approval.id)
        with self.assertRaisesRegex(PermissionError, 'revoked'):
            service.claim(actor, work, request_id=uuid4())

    def test_revoked_pinned_bootstrap_blocks_readiness_after_identity(self):
        from mop.server.sessions.models import AssignmentAttempt
        operation, _, _ = self.launch()
        self.ops.launches.run(operation)
        service, actor = self.machine()
        work, = service.poll(actor, self.node.pk, executor_epoch=self.boot, limit=2)
        attempt = service.claim(actor, work, request_id=uuid4())
        service.result(actor, attempt.id, expected_revision=attempt.revision, receipt_id=uuid4(),
                       outcome='succeeded', evidence='identity-applied')
        work, = service.poll(actor, self.node.pk, executor_epoch=self.boot, limit=2)
        self.assertEqual(work.action, 'confirm_ready')
        self.preparation.revoke(self.actor, self.bootstrap_approval.id)
        with self.assertRaisesRegex(PermissionError, 'revoked'):
            service.claim(actor, work, request_id=uuid4())
        self.assertFalse(AssignmentAttempt.objects.filter(operation_id=operation, action='confirm_ready').exists())

    def test_expired_reserved_grant_cannot_authorize_launch(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from django.db import transaction
        from mop.server.access.models import Grant
        _, target, _ = self.launch()
        binding = self.launch_binding(target)
        expires = Grant.objects.get(pk=binding.grant_id).intent.expires_at
        with patch('mop.server.access.grants.timezone', SimpleNamespace(now=lambda: expires)), \
                self.assertRaises(PermissionError), transaction.atomic():
            self.grants.authorize_launch(binding)
        self.assertEqual(Grant.objects.get(pk=binding.grant_id).state, 'reserved')

    def test_unsealed_reserved_definition_cannot_authorize_launch(self):
        from django.db import connections, transaction
        from django.db.migrations.loader import MigrationLoader
        _, target, _ = self.launch()
        # Состояние до seal migration, не разрешённый mutation публичного ORM.
        historical = MigrationLoader(connections['default']).project_state().apps
        historical.get_model('access', 'AgentDefinitionSeal').objects.filter(definition_id=self.definition.id).delete()
        with self.assertRaisesRegex(PermissionError, 'sealed'), transaction.atomic():
            self.grants.authorize_launch(self.launch_binding(target))

    def test_launch_rechecks_exact_creator_credential_not_another_owner_token(self):
        from django.db import transaction
        from mop.server.access import services as auth
        _, target, _ = self.launch()
        key = auth.rotate_token(self.actor)
        current = auth.token_context(key)
        self.assertNotEqual(current.credential_id, self.actor.credential_id)
        with self.assertRaises(PermissionError), transaction.atomic():
            self.grants.authorize_launch(self.launch_binding(target))

    def test_launch_rechecks_owner_issue_permission(self):
        from django.db import transaction
        _, target, _ = self.launch()
        self.user.is_superuser = False
        self.user.save(update_fields=['is_superuser'])
        from _grant_cases import permit_actions
        from mop.server.access import models as a
        a.ProjectMembership.objects.get_or_create(user=self.user, project=self.project,
            source=a.IdentitySource.objects.get(name='local'))
        permit_actions(self.user, 'execution.create_puppet', 'access.view_role', 'preparation.approve_bootstrap')
        with self.assertRaises(PermissionError), transaction.atomic():
            self.grants.authorize_launch(self.launch_binding(target))

    def test_launch_keeps_exact_old_policy_and_retained_references_without_provider_io(self):
        from unittest.mock import patch
        from django.db import transaction
        from mop.server.access.models import Grant
        _, target, _ = self.launch()
        binding = self.launch_binding(target)
        pinned = Grant.objects.get(pk=binding.grant_id).intent.policy_version_id
        newer = self.preparation.publish(self.actor, project_id=self.project.pk, tasks=b'- debug: msg=new\n',
            variables=b'answer: 43\n', provenance='git:example.invalid/new', commit='c'*40, secret_refs=())
        self.preparation.approve(self.actor, newer.id)
        with patch.object(type(self.grants.secrets.provider), 'decrypt', side_effect=AssertionError('provider under SQL')), \
                patch.object(type(self.grants.secrets.provider), 'encrypt', side_effect=AssertionError('provider under SQL')), \
                transaction.atomic():
            self.grants.authorize_launch(binding)
        self.assertEqual(Grant.objects.get(pk=binding.grant_id).intent.policy_version_id, pinned)
        # Новый latest не подменяет отозванный exact proof старого намерения.
        self.preparation.revoke(self.actor, self.bootstrap_approval.id)
        with self.assertRaisesRegex(PermissionError, 'revoked'), transaction.atomic():
            self.grants.authorize_launch(binding)

    def test_revoked_retained_launch_credential_blocks_authority(self):
        from django.db import transaction
        from mop.server.access.models import Grant
        from mop.server.secrets.models import Secret
        from mop.server.secrets.services import SecretStore
        _, target, _ = self.launch()
        binding = self.launch_binding(target)
        version = Grant.objects.get(pk=binding.grant_id).credential_version_id
        secret = Secret.objects.get(secretversion__id=version)
        from mop.server.access.services import Access
        SecretStore(provider=self.grants.secrets.provider, authorizer=Access()).revoke(
            self.actor, secret.pk, expected_revision=secret.revision)
        with self.assertRaises(PermissionError), transaction.atomic():
            self.grants.authorize_launch(binding)

    def test_revoked_retained_definition_blocks_launch_authority(self):
        from django.db import transaction
        from mop.server.access.models import AgentDefinitionVersion
        from mop.server.access.services import Access
        from mop.server.secrets.models import Secret
        from mop.server.secrets.services import SecretStore
        _, target, _ = self.launch()
        version = AgentDefinitionVersion.objects.get(pk=self.definition.id).content_version_id
        secret = Secret.objects.get(secretversion__id=version)
        SecretStore(provider=self.grants.secrets.provider, authorizer=Access()).revoke(
            self.actor, secret.pk, expected_revision=secret.revision)
        with self.assertRaises(PermissionError), transaction.atomic():
            self.grants.authorize_launch(self.launch_binding(target))


class Activator:
    """Порт активации credential-сервиса: grants с провайдером и доверенной
    квитанцией готовности (CurrentBindings), как в его процессе."""
    def __init__(self, grants, *, lose=False):
        self.grants, self.lose, self.calls = grants, lose, 0

    def activate_grant(self, grant_id, *, expected_revision):
        self.calls += 1
        row = self.grants.activate(grant_id, expected_revision=expected_revision)
        if self.lose:
            raise OSError('credential service reply lost after commit')
        return row


# HYPOTHESIS (#643): после готовности create не завершается: activate_grant
# некому исполнить, квитанция активации в credential-сервисе не связана, а у
# агента-субъекта нет своего credential -- активация отказала бы всегда.
# SOLUTION: квитанция активации -- подтверждённые identity и готовность текущей
# инкарнации; полоса GrantActivations зовёт активацию credential-сервиса только
# после успешных шагов узла; потеря ответа -- unknown, сверка читает грант.
# STATUS: FIXED — see #643 (боевой активатор credential-сервиса -- далее)
class CreateActivation(TransactionTestCase):
    serialized_rollback = True
    setUp = RestartVertical.setUp
    ready, body, submit = CreateVertical.ready, CreateVertical.body, CreateVertical.submit
    launch, step = CreateRegistration.launch, CreateRegistration.step
    machine = CreateMachineSteps.machine

    def through_readiness(self, operation, *, ready=True):
        self.ops.launches.run(operation)
        service, actor = self.machine()
        steps = (('apply_identity', 'identity-applied'), ('confirm_ready', 'ready'))
        for action, evidence in steps[:2 if ready else 1]:
            work, = service.poll(actor, self.node.pk, executor_epoch=self.boot, limit=2)
            attempt = service.claim(actor, work, request_id=uuid4())
            service.result(actor, attempt.id, expected_revision=attempt.revision, receipt_id=uuid4(),
                           outcome='succeeded', evidence=evidence)

    def activator(self, **options):
        from mop.server.access.grants import Grants
        from mop.server.controller.credentials import CurrentBindings
        # Как CredentialApplication: провайдер, порт одобрений и CurrentBindings.
        grants = Grants(provider=self.grants.secrets.provider, approvals=self.grants.approvals,
                        bindings=CurrentBindings())
        return Activator(grants, **options)

    def test_ready_target_activates_its_grant_and_completes_create(self):
        operation, target, _ = self.launch()
        self.ops.activations.activator = self.activator()
        self.through_readiness(operation, ready=False)
        # Без квитанции готовности активация не зовётся вовсе.
        self.assertNotIn(operation, self.ops.pending(limit=8, handlers=('activate_grant',)))
        self.assertIsNone(self.ops.activations.run(operation))
        self.assertEqual(self.ops.activations.activator.calls, 0)
        service, actor = self.machine()
        work, = service.poll(actor, self.node.pk, executor_epoch=self.boot, limit=2)
        attempt = service.claim(actor, work, request_id=uuid4())
        service.result(actor, attempt.id, expected_revision=attempt.revision, receipt_id=uuid4(),
                       outcome='succeeded', evidence='ready')
        self.assertIn(operation, self.ops.pending(limit=8, handlers=('activate_grant',)))
        self.ops.activations.run(operation)
        from mop.server.access.models import Grant
        from mop.server.operations.models import Operation
        from mop.server.sessions.models import Assignment
        grant = Grant.objects.get(pk=Assignment.objects.get(pk=target).grant_id)
        self.assertEqual(grant.state, 'active', Operation.objects.values_list('state', 'reason').get(pk=operation))
        step = self.step(operation)
        activation = type(step).objects.get(operation_id=operation, name='activate_grant')
        self.assertEqual((activation.outcome, activation.evidence), ('succeeded', 'grant-activated'))
        row = Operation.objects.get(pk=operation)
        self.assertEqual(row.state, 'succeeded')
        status = self.client.get('/api/v1/operations/'+str(operation), secure=True).json()['operation']
        self.assertFalse(status['resources_held'])
        self.assertNotIn(operation, self.ops.pending(limit=8, handlers=('activate_grant',)))
        self.ops.activations.run(operation)
        self.assertEqual(self.ops.activations.activator.calls, 1)

    def test_lost_activation_reply_is_unknown_and_reconciles_by_reading_the_grant(self):
        operation, target, _ = self.launch()
        self.ops.activations.activator = self.activator(lose=True)
        self.through_readiness(operation)
        self.ops.activations.run(operation)
        from mop.server.operations.models import Operation, Step
        self.assertEqual(Operation.objects.get(pk=operation).state, 'unknown')
        self.ops.activations.run(operation)
        self.assertEqual(self.ops.activations.activator.calls, 1)
        self.ops.activations.reconcile(self.actor, operation)
        activation = Step.objects.get(operation_id=operation, name='activate_grant')
        self.assertEqual((activation.outcome, activation.evidence), ('succeeded', 'grant-activated'))
        self.assertEqual(Operation.objects.get(pk=operation).state, 'succeeded')
        self.assertEqual(self.ops.activations.activator.calls, 1)

    # HYPOTHESIS (#703): готовность не отменяет not_after -- новый шаг
    # активации не должен начинаться после истечения intent.
    # SOLUTION: проверка на claim и непосредственно перед внешним вызовом;
    # lease ограничен тем же сроком, сверка прежнего результата не меняется.
    # RESULT: оба RED дали calls=1; после правки calls=0, activation 5/5.
    # STATUS: FIXED — see #703
    def test_expired_create_never_calls_activation(self):
        from unittest.mock import patch
        from mop.server.access.models import Grant
        from mop.server.operations.models import Operation
        from mop.server.sessions.models import Assignment
        operation, target, _ = self.launch()
        self.ops.activations.activator = self.activator()
        self.through_readiness(operation)
        deadline = Operation.objects.get(pk=operation).not_after
        with patch('mop.server.operations.launch.timezone.now', return_value=deadline + timedelta(seconds=1)):
            self.ops.activations.run(operation)
        self.assertEqual(self.ops.activations.activator.calls, 0)
        self.assertEqual(Grant.objects.get(pk=Assignment.objects.get(pk=target).grant_id).state, 'reserved')
        self.assertNotEqual(Operation.objects.get(pk=operation).state, 'succeeded')

    def test_deadline_crossed_after_claim_never_calls_activation(self):
        from unittest.mock import patch
        from mop.server.access.models import Grant
        from mop.server.operations.models import Operation
        from mop.server.sessions.models import Assignment
        operation, target, _ = self.launch()
        self.ops.activations.activator = self.activator()
        self.through_readiness(operation)
        deadline = Operation.objects.get(pk=operation).not_after
        claim = self.ops.activations.claim
        clock = [deadline - timedelta(seconds=1)]

        def cross_deadline(value):
            ticket = claim(value)
            clock[0] = deadline + timedelta(seconds=1)
            return ticket

        with patch('mop.server.operations.launch.timezone.now', side_effect=lambda: clock[0]), \
                patch.object(self.ops.activations, 'claim', side_effect=cross_deadline):
            self.ops.activations.run(operation)
        self.assertEqual(self.ops.activations.activator.calls, 0)
        self.assertEqual(Grant.objects.get(pk=Assignment.objects.get(pk=target).grant_id).state, 'reserved')
        self.assertNotEqual(Operation.objects.get(pk=operation).state, 'succeeded')

    def test_activation_receipt_needs_current_readiness(self):
        operation, target, _ = self.launch()
        self.through_readiness(operation, ready=False)
        from mop.server.controller.credentials import CurrentBindings
        from mop.server.sessions.models import Assignment
        self.assertIsNone(CurrentBindings().activation(Assignment.objects.get(pk=target).grant_id))
