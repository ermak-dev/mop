"""Parity-матрица cutover #689: одни права — одно решение на всех входах.

HYPOTHESIS: авторизатор читает узел сервиса из CredentialBinding, а не из
авторитетного ServiceEnrollment (#687 note 2); parity human/agent/service
по входам не зафиксирован проверкой.
SPEC: одинаковый набор прав и фактов даёт одинаковый вердикт и на прямом
вызове сервиса, и через машинный протокол; смена источника факта узла
(удаление enrollment при живом binding) закрывает доступ; агент с правами
человека проходит тот же pipeline без видовых веток.
RED: node-fact тест зелёный на базе — binding-копия в authorize и fence в
_check_binding уже закрывали повторный допуск; parity-случаи ошибкались на
фикстуре. После выравнивания фикстуры источник факта переведён на
ServiceEnrollment в самом authorize (289) — отношение теперь авторитетно
в обеих точках. RESULT: 71/71 (cutover + access + enrollment).
STATUS: FIXED — see #689
"""
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.test import Client, TransactionTestCase, override_settings
from django.utils import timezone
from datetime import timedelta

from mop.server.access import services as auth
from _restart_vertical_cases import RestartVertical
from mop.server.access import models as a
from mop.server.operations import models as m


@override_settings(ALLOWED_HOSTS=['testserver'])
class CutoverParity(TransactionTestCase):
    serialized_rollback = True
    setUp = RestartVertical.setUp
    fixture = RestartVertical.fixture
    body = RestartVertical.body

    def enroll(self, *, node_id=None, actions):
        """Enrollment сам создаёт service Actor; use_case задаёт узловой контекст."""
        kwargs = dict(use_case='node-executor' if node_id is not None else 'controller-worker')
        key = auth.enroll_service('svc-' + uuid4().hex[:8], node_id,
            project_ids=(), permissions=actions, **kwargs)
        return get_user_model().objects.get(auth_token__key=key), key

    def test_service_node_fact_comes_from_the_enrollment_relation(self):
        self.fixture()
        user, key = self.enroll(node_id=self.node.pk, actions=('execution.poll_work',))
        actor = auth.token_context(key)
        from mop.server.access.services import Access, Denied
        # Живой enrollment: допуск в границах узла.
        Access().authorize(actor, 'execution.poll_work', node_ids=(self.node.pk,))
        # Факт узла исчез (enrollment удалён), а binding и token живы:
        # источник факта — отношение, поэтому доступ должен закрыться. Текущий
        # авторизатор читает bound_node из binding и пропустит дальше — RED.
        from mop.server.access.models import CredentialBinding
        binding = CredentialBinding.objects.get(token_user=user)
        a.ServiceEnrollment.objects.filter(actor=user).delete()
        self.assertEqual(binding.bound_node_id, self.node.pk,
                         'binding keeps its denormalized node copy')
        # Авторизатор обязан читать факт узла из отношения enrollment, не из
        # копии в binding: повторный допуск по уже построенному контексту
        # закрывается вместе с отношением.
        with self.assertRaises(Denied):
            Access().authorize(actor, 'execution.poll_work', node_ids=(self.node.pk,))
        # Новый контекст по тому же живому token тоже закрыт.
        with self.assertRaises(Denied):
            auth.token_context(key)

    def test_agent_and_human_with_the_same_permissions_get_the_same_verdicts(self):
        self.fixture()
        from _grant_cases import permit_actions
        agent = get_user_model().objects.create_user('agent-parity', password='p-only-fixture')
        agent.kind = 'agent'
        agent.save(update_fields=['kind'])
        source = a.IdentitySource.objects.get(name='local')
        a.LoginIdentity.objects.create(user=agent, source=source, external_subject='agent-parity')
        a.ProjectMembership.objects.create(user=agent, project=self.project, source=source)
        for target in (self.user, agent):
            permit_actions(target, 'catalog.view_project')
        from mop.server.access.services import Access, Denied, NotVisible
        human_actor = auth.token_context(auth.issue_token(self.user.pk))
        agent_actor = auth.token_context(auth.issue_token(agent.pk))
        access = Access()
        for actor in (human_actor, agent_actor):
            access.authorize(actor, 'catalog.view_project', project_ids=(self.project.pk,))
            with self.assertRaises(NotVisible):
                access.authorize(actor, 'catalog.view_project', project_ids=(uuid4(),))
        stranger = get_user_model().objects.create_user('stranger-parity', password='p-only-fixture')
        a.LoginIdentity.objects.create(user=stranger, source=source, external_subject='stranger-parity')
        stranger_actor = auth.token_context(auth.issue_token(stranger.pk))
        with self.assertRaises((Denied, NotVisible)):
            access.authorize(stranger_actor, 'catalog.view_project', project_ids=(self.project.pk,))

    def test_node_free_service_is_denied_machine_actions_until_use_case_admission(self):
        self.fixture()
        user, key = self.enroll(node_id=None, actions=('catalog.observe_node',))
        actor = auth.token_context(key)
        from mop.server.access.services import Access, Denied
        with self.assertRaises(Denied):
            Access().authorize(actor, 'catalog.observe_node', node_ids=(self.node.pk,))


@override_settings(ALLOWED_HOSTS=['testserver'])
class GatewayCutover(TransactionTestCase):
    """Панель прокси решает тот же авторизатор, что и остальные входы (#689).

    RED: gateway держал собственное право gateway.manage_proxy и читал его
    напрямую через has_perm мимо Access.authorize и пересечения делегирования;
    в словаре #685 то же понятие зарегистрировано как controller.manage_proxy.
    """
    serialized_rollback = True

    def test_proxy_management_has_one_registered_permission(self):
        from django.contrib.auth.models import Permission
        owners = sorted(Permission.objects.filter(codename='manage_proxy')
                        .values_list('content_type__app_label', flat=True))
        self.assertEqual(owners, ['controller'], 'one owner per action (#685): no gateway duplicate')

    def test_gateway_authority_decides_through_the_single_authorizer(self):
        from unittest.mock import patch
        from mop.server.access.services import Access, Denied
        from mop.server.gateway.authority import HumanAuthority
        from mop.server.controller.runtime import initialize
        initialize()
        user = get_user_model().objects.create_user('panel-cutover', password='fixture-only')
        source = a.IdentitySource.objects.create(name='local', kind='local')
        a.LoginIdentity.objects.create(user=user, source=source, external_subject='panel-cutover')
        a.AllProjectMembership.objects.create(user=user, source=source)
        actor = auth.token_context(auth.issue_token(user.pk))
        calls = []
        original = Access.authorize

        def spy(self, actor, action, **scope):
            calls.append(action)
            return original(self, actor, action, **scope)
        with patch.object(Access, 'authorize', spy):
            with self.assertRaises(Denied):
                HumanAuthority().authorize(actor)
        self.assertEqual(calls, ['controller.manage_proxy'], 'gateway must ask the single authorizer')


@override_settings(ALLOWED_HOSTS=['testserver'])
class NodeExecutorFact(TransactionTestCase):
    """Машинные входы требуют факт enrollment, а не вид Actor (#687/#689)."""
    serialized_rollback = True
    setUp = RestartVertical.setUp
    fixture = RestartVertical.fixture

    MODULES = ('mop/server/sessions/services.py', 'mop/server/operations/service.py',
               'mop/server/controller/projectionviews.py', 'mop/server/sessions/telemetry.py')

    def test_machine_modules_compare_no_actor_kind_for_the_executor(self):
        from pathlib import Path
        root = Path(__file__).resolve().parents[1]
        for name in self.MODULES:
            source = (root / name).read_text()
            self.assertFalse('ActorKind.SERVICE' in source,
                             f'{name}: the node executor is an enrollment fact, not a kind branch')

    def test_privileged_human_and_foreign_node_service_cannot_take_machine_work(self):
        self.fixture()
        from mop.server.access.services import Denied
        from mop.server.catalog.models import Node, NodeProject
        from mop.server.sessions.services import machine
        # Суперпользователь со всеми правами — не исполнитель узла.
        with self.assertRaises(Denied):
            machine(self.actor, self.node.pk, 'poll_work')
        other = Node.objects.create(name='other', driver='host', ssh_host='other.invalid',
            control_origin='https://other.invalid', lan_address='other.invalid', bootstrap_address='other.invalid')
        NodeProject.objects.create(node=other, project=self.project)
        key = auth.enroll_service('foreign-executor', other.pk, project_ids=(self.project.pk,),
            permissions=('execution.poll_work',))
        foreign = auth.token_context(key)
        with self.assertRaises(Denied):
            machine(foreign, self.node.pk, 'poll_work')
        # Свой исполнитель своего узла проходит.
        machine(self.machine_actor, self.node.pk, 'poll_work')
