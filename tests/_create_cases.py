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
        from _grant_cases import ApprovalFixture, BindingFixture, manifest
        from mop.server.access import services as auth
        from mop.server.access.grants import Grants, ApprovedPolicy
        from mop.server.catalog.services import Catalog
        from mop.server.preparation.services import Preparation
        from mop.server.secrets.crypto import Keyring, XChaCha20Provider
        from mop.server.secrets.services import SecretStore
        provider = XChaCha20Provider(Keyring('fixture', {'fixture': b'c'*32}))
        policy = SecretStore(provider=provider, authorizer=auth.Access()).create(self.actor,
            project_id=self.project.pk, name='policy', kind='credential', purpose='policy', value=b'approved fixture policy')
        proof = ApprovedPolicy(uuid4(), policy.active_version_id, 'a'*64, self.user.pk, self.project.pk)
        self.grants = Grants(provider=provider, approvals=ApprovalFixture(proof), bindings=BindingFixture())
        self.definition = self.grants.publish_definition(self.actor, project_id=self.project.pk,
            manifest=json.dumps(manifest()).encode(), prompt=b'fixture role', provenance='controlled fixture')
        if approve:
            self.grants.approve_definition(self.actor, self.definition.id)
        if bootstrap:
            prep = Preparation(provider=provider, authorizer=auth.Access())
            self.revision = prep.publish(self.actor, project_id=self.project.pk, tasks=b'- debug: msg=hello\n',
                variables=b'answer: 42\n', provenance='git:example.invalid/vertical', commit='b'*40, secret_refs=())
            prep.approve(self.actor, self.revision.id)
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
