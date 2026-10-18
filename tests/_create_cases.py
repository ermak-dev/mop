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

    def ready(self, *, bootstrap=True, image=True, executor=True, approve=True):
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
        self.issuer, self.scheduler = Issuer(self.grants), Scheduler()
        from unittest.mock import patch
        from mop.server.controller.createbinding import service
        self.addCleanup(patch.stopall)
        patch('mop.server.controller.operationviews.create_api',
              lambda: service(grants=self.grants, issuer=self.issuer, scheduler=self.scheduler)).start()

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
