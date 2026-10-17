"""#687: технический enrollment service Actor и общий after-I/O readmission.

Enrollment — минимальное отношение контекста (use case и, для узловых
сервисов, узел), а не вторая ролевая модель: операционные права остаются
штатным Django RBAC (#685). Переоформление переиспользует тот же Actor,
смена узла не даёт старому ключу усыновить чужой enrollment, отозванный
во время crypto wait enrollment не получает plaintext. Повторный допуск
после I/O (worker fact reads и приватная адресная выдача) идёт через один
общий механизм: короткая SQL-only секция, сеть/crypto вне её.
"""
import ast
import unittest
from pathlib import Path

from django.apps import apps
from django.contrib.auth import get_user_model
from django.db import connection, transaction
from django.test import TransactionTestCase, override_settings

import _credential_cases as socket_fixtures
import _credential_binding_cases as binding_fixtures
import _execution_cases as execution_fixtures


ROOT = Path(__file__).resolve().parents[1]


# FEATURE: #687 — enrollment service Actor как минимальное отношение контекста.
# HYPOTHESIS: без отношения enrollment держится на allowlist и kind-ветках;
# переоформление плодит новых Actor, подделанный bound_node в credential
# неотличим от настоящего, а controller/credential/builder контексты
# вынуждены врать про узел.
# SOLUTION: ServiceEnrollment (use case + узел) проверяется в credential
# resolution; enroll_service переиспользует Actor при переоформлении и
# принимает явные безузловые контексты, остающиеся fail-closed в authorize.
# RED: PG17 — схемы ServiceEnrollment нет; повторный enroll_service даёт
# ValueError('service username already exists'); подделанный binding
# усыновляет чужой узел; безузловой enroll падает на get(pk=None).
# RESULT: один Actor на контекст, подделка и отзыв закрывают credential.
# STATUS: FIXED — see #687
@override_settings(ALLOWED_HOSTS=['testserver'])
class ServiceEnrollmentCases(TransactionTestCase):
    serialized_rollback = True

    def setUp(self):
        # Отсутствующий app — собственный проверяемый red, а не ошибка импорта.
        self.assertTrue(apps.is_installed('mop.server.access'), 'Access schema must be installed')
        from mop.server.access import models as m, services as s
        from mop.server.catalog.models import Project, Node, NodeProject
        from mop.server.controller.runtime import initialize
        initialize()
        self.m, self.s = m, s
        self.project = Project.objects.create(name='enroll-one', origin='https://example.invalid/enroll-one.git')

        def node(name):
            return Node.objects.create(name=name, driver='host', ssh_host=name + '.invalid', ssh_port=22,
                control_origin='https://controller.invalid', lan_address=name + '.invalid',
                bootstrap_address=name + '.invalid')

        self.node_a, self.node_b = node('enroll-a'), node('enroll-b')
        for row in (self.node_a, self.node_b):
            NodeProject.objects.create(node=row, project=self.project)

    def test_enrollment_records_the_technical_context_next_to_the_actor(self):
        key = self.s.enroll_service('executor-687', self.node_a.pk, project_ids=(self.project.pk,),
            permissions=('catalog.observe_node',))
        user = get_user_model().objects.get(username='executor-687')
        row = self.m.ServiceEnrollment.objects.get(actor=user)
        # Контекст — факт отношения, не признак kind: узловой исполнитель
        # несёт и use case, и узел в одной строке рядом с Actor.
        self.assertEqual((row.use_case, row.node_id), ('node-executor', self.node_a.pk))
        actor = self.s.token_context(key)
        self.assertEqual((actor.kind.value, actor.node_id), ('service', self.node_a.pk))

    def test_reenrollment_keeps_one_actor_and_kills_the_old_credential_on_move(self):
        first = self.s.enroll_service('stable-service', self.node_a.pk, project_ids=(self.project.pk,),
            permissions=('catalog.observe_node',))
        user_id = get_user_model().objects.get(username='stable-service').pk
        # Переоформление в том же контексте — та же identity и тот же ключ.
        same = self.s.enroll_service('stable-service', self.node_a.pk, project_ids=(self.project.pk,),
            permissions=('catalog.observe_node',), renew=True)
        self.assertEqual(same, first)
        self.assertEqual(get_user_model().objects.filter(username='stable-service').count(), 1)
        old_context = self.s.token_context(first)
        # Переоформление на другой узел: Actor один, старый ключ и сохранённый
        # контекст мертвы и не усыновляют чужой узел.
        moved = self.s.enroll_service('stable-service', self.node_b.pk, project_ids=(self.project.pk,),
            permissions=('catalog.observe_node',), renew=True)
        actor = self.s.token_context(moved)
        self.assertEqual((actor.user_id, actor.node_id), (user_id, self.node_b.pk))
        with self.assertRaises(self.s.Denied):
            self.s.token_context(first)
        with self.assertRaises(self.s.Denied):
            self.s.current_actor(old_context)
        self.assertEqual(get_user_model().objects.filter(username='stable-service').count(), 1)

    def test_reenrollment_renews_only_a_service_actor_of_its_own_enrollment(self):
        # Неизвестное имя и чужая (не сервисная) учётная запись этим hook
        # не переоформляются.
        with self.assertRaises(ValueError):
            self.s.enroll_service('ghost-service', self.node_a.pk, project_ids=(),
                permissions=('catalog.observe_node',), renew=True)
        get_user_model().objects.create_user('plain-human', password='test-only')
        with self.assertRaises(ValueError):
            self.s.enroll_service('plain-human', self.node_a.pk, project_ids=(),
                permissions=('catalog.observe_node',), renew=True)

    def test_reenrollment_narrows_permissions_to_the_requested_set(self):
        self.s.enroll_service('narrow-service', self.node_a.pk, project_ids=(self.project.pk,),
            permissions=('catalog.observe_node', 'catalog.record_node_application'))
        user = get_user_model().objects.get(username='narrow-service')
        self.assertTrue(user.has_perm('catalog.record_node_application'))
        self.s.enroll_service('narrow-service', self.node_a.pk, project_ids=(self.project.pk,),
            permissions=('catalog.observe_node',), renew=True)
        fresh = get_user_model().objects.get(pk=user.pk)
        self.assertFalse(fresh.has_perm('catalog.record_node_application'))
        self.assertTrue(fresh.has_perm('catalog.observe_node'))

    def test_spoofed_binding_cannot_adopt_a_foreign_node(self):
        from rest_framework.authtoken.models import Token
        key = self.s.enroll_service('guard-service', self.node_a.pk, project_ids=(self.project.pk,),
            permissions=('catalog.observe_node',))
        user = get_user_model().objects.get(username='guard-service')
        token = Token.objects.get(user=user)
        # Подделка строки binding под чужой узел: digest и токен настоящие,
        # enrollment — нет. Такой credential обязан умирать, а не переезжать.
        self.m.CredentialBinding.objects.filter(token_user=user).delete()
        self.m.CredentialBinding.objects.create(token_user=user, token_digest=self.s._digest(token.key),
            bound_node=self.node_b)
        with self.assertRaises(self.s.Denied):
            self.s.token_context(key)

    def test_controller_context_enrolls_without_node_and_stays_fail_closed(self):
        key = self.s.enroll_service('controller-machine', None, project_ids=(),
            permissions=('catalog.record_settings_application',), use_case='controller-worker')
        actor = self.s.token_context(key)
        self.assertIsNone(actor.node_id)
        self.assertIsNone(actor.human_owner_id)
        self.assertIsNone(actor.grant_id)
        row = self.m.ServiceEnrollment.objects.get(actor_id=actor.user_id)
        self.assertEqual((row.use_case, row.node_id), ('controller-worker', None))
        # Безузловой контекст аутентифицируется, но машинный допуск без
        # собственного факта (узел/assignment/OS peer) — отказ; cutover
        # прав остаётся за #685/#689, а не за видом Actor.
        self.assertIsNotNone(self.s.current_actor(actor))
        with self.assertRaises(self.s.Denied):
            self.s.Access().authorize(actor, 'catalog.record_settings_application')
        with self.assertRaises(self.s.Denied):
            self.s.Access().authorize(actor, 'catalog.observe_node', node_ids=(self.node_a.pk,))
        # Отзыв enrollment убивает и ключ, и сохранённый контекст.
        self.m.ServiceEnrollment.objects.filter(actor_id=actor.user_id).delete()
        with self.assertRaises(self.s.Denied):
            self.s.token_context(key)
        with self.assertRaises(self.s.Denied):
            self.s.current_actor(actor)

    def test_use_case_vocabulary_is_enforced(self):
        with self.assertRaises(ValueError):
            self.s.enroll_service('bogus-context', self.node_a.pk, project_ids=(),
                permissions=('catalog.observe_node',), use_case='bogus')
        with self.assertRaises(ValueError):
            # Узловой use case без узла — не безузловой контекст.
            self.s.enroll_service('loose-executor', None, project_ids=(),
                permissions=('catalog.observe_node',))
        with self.assertRaises(ValueError):
            # Безузловой контекст не привязывается к узлу.
            self.s.enroll_service('mixed-context', self.node_a.pk, project_ids=(),
                permissions=('catalog.record_settings_application',), use_case='credential-runtime')
        with self.assertRaises(ValueError):
            # Безузловой контекст не несёт проектной области.
            self.s.enroll_service('scoped-controller', None, project_ids=(self.project.pk,),
                permissions=('catalog.record_settings_application',), use_case='controller-worker')
        self.assertFalse(get_user_model().objects.filter(username__in=(
            'bogus-context', 'loose-executor', 'mixed-context', 'scoped-controller')).exists())

    def test_hand_made_node_free_service_stays_denied(self):
        # Ограждение от избыточного доверия к kind при появлении безузловых
        # контекстов: «просто service» без enrollment и без grant — отказ.
        from rest_framework.authtoken.models import Token
        user = get_user_model().objects.create_user('hand-made', password=None)
        get_user_model().objects.filter(pk=user.pk).update(kind='service')
        token = Token.objects.create(user=user)
        self.m.CredentialBinding.objects.create(token_user=user, token_digest=self.s._digest(token.key))
        with self.assertRaises(self.s.Denied):
            self.s.token_context(token.key)


# FEATURE: #687 — один общий повторный допуск после I/O.
# HYPOTHESIS: worker fact reads повторяют admission вне всякой транзакции;
# приватная адресная выдача держит собственную вторую секцию; scheduler
# prepare_effect — третью. Общего механизма нет, и «после I/O» не значит
# «в одной короткой SQL секции».
# SOLUTION: catalog.services.readmission — одна короткая SQL-only секция
# под locks с явным отказом на вход из активной транзакции; facts и
# issue_addressed идут через неё (catalog — общий предмет для access,
# secrets и sessions: secrets не вправе импортировать access).
# RED: PG17 — повторный допуск facts виден вне транзакции; в исходниках
# sessions/secrets нет общего вызова readmission.
# RESULT: facts и issue_addressed идут через catalog.services.readmission.
# STATUS: FIXED — see #687
class FactReadmissionCases(TransactionTestCase):
    serialized_rollback = True
    setUp = execution_fixtures.MachineExecution.setUp

    def test_fact_readmission_repeats_inside_one_sql_section_after_reader_io(self):
        import mop.server.sessions.services as svc
        from unittest.mock import patch
        page = self.p.FactPageV1(self.node.id, 'states', True, ())
        observed = []

        class Reader:
            def read(inner, **scope):
                # I/O читателя — всегда вне транзакции.
                observed.append(('read', connection.in_atomic_block, connection.get_autocommit()))
                return page

        self.service.reader = Reader()
        real_machine = svc.machine

        def spy(actor, node_id, action, project_ids=()):
            observed.append((action, connection.in_atomic_block))
            return real_machine(actor, node_id, action, project_ids=project_ids)

        with patch.object(svc, 'machine', spy):
            self.service.facts(self.actor, self.node.id, kind='states', limit=10)
        # Первый допуск — до I/O и вне транзакции; повторный — внутри одной
        # короткой SQL-only секции общего readmission, теми же словами.
        self.assertEqual(observed, [('read_facts', False), ('read', False, True), ('read_facts', True)])


# FEATURE: #687 — отзыв enrollment во время crypto wait не выдаёт plaintext.
# RED: PG17 — отношения ServiceEnrollment нет, отзыв нечем проверять.
# RESULT: отзыв enrollment во время crypto wait — отказ, plaintext умер.
# STATUS: FIXED — see #687
class EnrollmentAwaitCases(TransactionTestCase):
    serialized_rollback = True
    setUp = socket_fixtures.CredentialSocketCases.setUp
    exercise = socket_fixtures.CredentialSocketCases.exercise
    run_socket = socket_fixtures.CredentialSocketCases.run_socket
    reserved_grant = socket_fixtures.CredentialSocketCases.reserved_grant
    grant_assignment = socket_fixtures.CredentialSocketCases.grant_assignment
    enroll = binding_fixtures.AddressedCredentialCases.enroll
    delivery = binding_fixtures.AddressedCredentialCases.delivery
    issue = binding_fixtures.AddressedCredentialCases.issue
    wait_change = binding_fixtures.AddressedCredentialCases.wait_change

    def test_enrollment_revoked_during_crypto_discards_plaintext(self):
        from mop.server.access import models as access_models
        self.wait_change(lambda: access_models.ServiceEnrollment.objects
            .filter(actor=self.machine_user).delete())


class ReadmissionSourceContract(unittest.TestCase):
    """Один общий механизм повторного допуска в машинных конвейерах."""

    def test_after_io_rechecks_share_one_readmission_boundary(self):
        # sessions facts и secrets issue_addressed обязаны идти через один
        # helper catalog.services.readmission: «после I/O» — свойство состава,
        # а не каждого вызова по отдельности. Catalog — единственный предмет,
        # который вправе импортировать и access, и secrets, и sessions.
        tree = ast.parse((ROOT / 'mop/server/catalog/services.py').read_bytes())
        names = [node.name for node in ast.walk(tree)
                 if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
        self.assertIn('readmission', names)
        for relative in ('mop/server/sessions/services.py', 'mop/server/secrets/services.py'):
            with self.subTest(module=relative):
                source = (ROOT / relative).read_text()
                self.assertIn('readmission(', source)

    def test_readmission_refuses_entry_from_an_active_transaction(self):
        # Вход из активной транзакции — ошибка состава (не savepoint), и
        # отказ доходит до границы как PermissionError: слой субъектов не
        # вправе «улучшать» его до собственного класса.
        from mop.server.catalog.services import readmission
        with self.assertRaises(PermissionError) as raised:
            with transaction.atomic():
                readmission(lambda: 'never')
        self.assertIn('transaction-free boundary', str(raised.exception))
