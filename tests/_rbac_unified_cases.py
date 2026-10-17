"""#685 контракты единого Django RBAC: один словарь, один pipeline, общие Groups.

RED писался до реализации: action-словари были разнесены по ACTIONS/
SERVICE_ACTIONS/GLOBAL_ACTIONS/permission_names-алиасам, допуск
маршрутизировался видом Actor, LDAP заводил персональный Group-контейнер на
пользователя, а права role/grant были зарегистрированы на deprecated Principal.
Каждый случай падался поведением (Denied/assertion), не ошибкой импорта.
RESULT: 9/9 green на изолированном PG17 — шесть duplicate-пар закрыты одним
каноническим владельцем, pipeline допускает агента теми же правами/scope,
общая группа правила делится между пользователями, отзыв — только membership.
STATUS: FIXED — see #685
"""
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.test import TransactionTestCase

from mop.server.access import models as m, services as s
from mop.server.catalog.models import Project


class RbacBase(TransactionTestCase):
    serialized_rollback = True

    def setUp(self):
        from mop.server.controller.runtime import initialize
        initialize()
        self.source = m.IdentitySource.objects.create(name='local', kind='local', priority=0)
        self.human = get_user_model().objects.create_user('rbac-human', password='test-password-only')
        m.LoginIdentity.objects.create(user=self.human, source=self.source, external_subject='rbac-human')
        self.project = Project.objects.create(name='rbac-one', origin='https://example.invalid/one.git')
        self.foreign = Project.objects.create(name='rbac-two', origin='https://example.invalid/two.git')
        m.ProjectMembership.objects.create(user=self.human, source=self.source, project=self.project)
        self.permit(self.human, 'catalog.view_project')
        self.token = s.issue_token(self.human.pk)
        self.actor = s.token_context(self.token)

    def permit(self, user, *names):
        # auth.* — логический namespace CONTROL: строки лежат в actor.
        for name in names:
            app, code = name.split('.')
            if app == 'auth':
                app, code = 'actor', code.replace('_user', '_actor', 1)
            user.user_permissions.add(Permission.objects.get(content_type__app_label=app, codename=code))

    def node(self, name):
        from mop.server.catalog.models import Node, NodeProject
        row = Node.objects.create(name=name, driver='host', ssh_host='node.invalid', ssh_port=22,
            control_origin='https://controller.invalid', lan_address='node.invalid', bootstrap_address='node.invalid')
        NodeProject.objects.create(node=row, project=self.project)
        return row

    def agent_actor(self, username, *permissions):
        agent = get_user_model().objects.create_user(username, password=None)
        get_user_model().objects.filter(pk=agent.pk).update(kind='agent')
        agent.refresh_from_db()
        m.LoginIdentity.objects.create(user=agent, source=self.source, external_subject=username)
        m.ProjectMembership.objects.create(user=agent, source=self.source, project=self.project)
        if permissions:
            self.permit(agent, *permissions)
        return agent, s.token_context(s.issue_token(agent.pk))


# FEATURE: #685 — один словарь permission-codenames, один владелец регистрации.
# HYPOTHESIS: повторные регистрации одного codename на разных ContentTypes и
# логический sessions-namespace делают словарь действий неоднозначным.
# RED: шесть duplicate-пар давали count=2; execution.* напрямую не зарегистрирован.
# RESULT: у каждого действия ровно одна registration-строка, владелец документирован.
class UnifiedActionDictionaryCases(RbacBase):

    def test_each_action_permission_has_exactly_one_canonical_owner(self):
        # Канонический владелец каждой строки: Installation, а не unmanaged
        # DomainPermission; Assignment для endpoint-прав; UsageSource для
        # view_usage (#670 фиксировал usagesource как authority).
        canonical = (
            ('controller', 'manage_proxy', 'installation'),
            ('controller', 'view_models', 'installation'),
            ('execution', 'view_usage', 'usagesource'),
            ('execution', 'register_endpoint', 'assignment'),
            ('execution', 'view_delivery', 'assignment'),
            ('execution', 'deliver_report', 'assignment'),
        )
        for app, code, model in canonical:
            with self.subTest(permission=app + '.' + code):
                rows = Permission.objects.filter(content_type__app_label=app, codename=code)
                self.assertEqual(rows.count(), 1, 'duplicate codename registrations: see #670')
                self.assertEqual(rows.get().content_type.model, model)

    def test_execution_actions_are_registered_directly_without_sessions_alias(self):
        self.permit(self.human, 'execution.restart_puppet')
        # Логического sessions-namespace больше нет: прямое имя app label.
        with self.assertRaises(s.Denied):
            s.Access().authorize(self.actor, 'sessions.restart_puppet', project_ids=(self.project.pk,))
        s.Access().authorize(self.actor, 'execution.restart_puppet', project_ids=(self.project.pk,))

    def test_one_dictionary_declares_every_action_once(self):
        import importlib
        permissions = importlib.import_module('mop.server.access.permissions')
        self.assertTrue(hasattr(permissions, 'ACTION_PERMISSIONS'), 'single action dictionary is missing')
        dictionary = permissions.ACTION_PERMISSIONS
        self.assertNotIn('sessions.create_puppet', dictionary)
        for action in ('execution.create_puppet', 'execution.poll_work', 'execution.view_usage',
                       'catalog.view_pool', 'catalog.view_project', 'auth.view_user', 'auth.view_group',
                       'access.view_role', 'access.issue_grant', 'operations.view_operation',
                       'operations.cancel_operation', 'secrets.deliver_secret', 'secrets.rotate_proxy_key',
                       'controller.manage_proxy', 'controller.view_models', 'builds.force_build',
                       'preparation.approve_bootstrap', 'execution.ingest_observations'):
            self.assertIn(action, dictionary)
        self.assertEqual(permissions.ACTIONS, frozenset(dictionary))
        self.assertLessEqual(permissions.GLOBAL_ACTIONS, permissions.ACTIONS)
        self.assertLessEqual(permissions.MACHINE_ACTIONS, permissions.ACTIONS)
        delegation = importlib.import_module('mop.server.access.delegation')
        matrix = getattr(delegation, 'ACTION_CAPABILITIES', None)
        self.assertIsNotNone(matrix, 'delegated capability matrix is missing')
        self.assertFalse(any(action.startswith('sessions.') for action in matrix),
            'capability matrix still uses the sessions alias')


# FEATURE: #685 — один pipeline для human/agent/service; kind не маршрут.
# HYPOTHESIS: вид Actor выбирает ветку проверки и проектный scope человека
# не вычисляется для агента; unknown action отказывает до scope-проверки.
# RED: агент с правом и membership получал 'human project query required';
# unknown action у агента/сервиса тонул в kind-ветке.
# RESULT: один pipeline — решение агента совпадает с человеком; unknown action
# отказывает 'action is not registered' до всяких видовых веток.
class OnePipelineCases(RbacBase):

    def test_agent_gets_the_same_decision_as_human(self):
        agent, actor = self.agent_actor('rbac-agent', 'catalog.view_project')
        s.Access().authorize(actor, 'catalog.view_project', project_ids=(self.project.pk,))
        # Чужой проект — тот же 404-семантика NotVisible, что у человека.
        with self.assertRaises(s.NotVisible):
            s.Access().authorize(actor, 'catalog.view_project', project_ids=(self.foreign.pk,))
        # Отозванное право отказывает тем же отказом, что у человека.
        agent.user_permissions.clear()
        with self.assertRaises(s.Denied):
            s.Access().authorize(actor, 'catalog.view_project', project_ids=(self.project.pk,))

    def test_unknown_action_is_refused_as_unregistered_for_every_kind(self):
        _, agent_context = self.agent_actor('rbac-agent-unknown')
        key = s.enroll_service('rbac-machine-unknown', self.node('rbac-node-unknown').pk,
            project_ids=(self.project.pk,), permissions=('catalog.observe_node',))
        machine = s.token_context(key)
        for actor in (self.actor, agent_context, machine):
            with self.subTest(user_id=actor.user_id):
                with self.assertRaisesRegex(s.Denied, 'action is not registered'):
                    s.Access().authorize(actor, 'catalog.not_a_real_action', project_ids=(self.project.pk,))

    def test_enrollment_still_admits_only_machine_actions(self):
        # Узловой enrollment — именованное предусловие, не второй RBAC:
        # за пределами машинного набора прав hook отказывает как раньше.
        with self.assertRaises(ValueError):
            s.enroll_service('rbac-loud-service', self.node('rbac-node-loud').pk,
                project_ids=(self.project.pk,), permissions=('access.view_role',))
        self.assertFalse(get_user_model().objects.filter(username='rbac-loud-service').exists())


# FEATURE: #685 — общие Django Groups по правилу group-dn, membership add/remove.
# HYPOTHESIS: персональный Group-контейнер на пользователя/источник дублирует
# каталог прав; управляемая общая группа должна быть одна на правило.
# RED: два пользователя одного правила получали два разных Group.
# RESULT: одна общая Group на правило; отзыв снимает только membership отозванного,
# права общей группы, ручные группы/права и чужие источники не затронуты.
class SharedDirectoryGroupsCases(RbacBase):

    def directory_source(self, name, dn, permissions):
        source = m.IdentitySource.objects.create(name=name, kind='ldap', priority=1)
        rule = m.DirectoryGroupRule.objects.create(source=source, group_dn=dn, project=self.project)
        for permission in permissions:
            app, code = permission.split('.')
            if app == 'auth':
                app, code = 'actor', code.replace('_user', '_actor', 1)
            rule.permissions.add(Permission.objects.get(content_type__app_label=app, codename=code))
        return source, rule

    def apply(self, source, subject, username, groups, permissions, *, allowed=True, project_ids=None):
        from mop.server.access.directory import DirectoryResult, apply_directory
        return apply_directory(source.pk, source.revision, DirectoryResult(subject, username, groups,
            tuple(project_ids if project_ids is not None else (self.project.pk,)), False, permissions, allowed=allowed))

    def test_directory_users_share_one_group_per_rule(self):
        source, rule = self.directory_source('rbac-dir', 'cn=rbac-team,dc=example', ('catalog.view_project',))
        first = self.apply(source, 'uid=one', 'dir-one', ('cn=rbac-team,dc=example',), ('catalog.view_project',))
        second = self.apply(source, 'uid=two', 'dir-two', ('cn=rbac-team,dc=example',), ('catalog.view_project',))
        users = get_user_model().objects.filter(pk__in=(first, second)).order_by('pk')
        shared = set(users[0].groups.all()) & set(users[1].groups.all())
        self.assertEqual(len(shared), 1, 'directory group must be shared per rule, not per user')
        group = shared.pop()
        self.assertTrue(group.permissions.filter(content_type__app_label='catalog',
            codename='view_project').exists())
        # Общая группа не ломает живой credential обоих пользователей.
        for user in users:
            actor = s.token_context(s.issue_token(user.pk))
            s.Access().authorize(actor, 'catalog.view_project', project_ids=(self.project.pk,))

    def test_revoke_is_membership_only_and_preserves_shared_group_and_manual_rights(self):
        source, rule = self.directory_source('rbac-dir', 'cn=rbac-team,dc=example', ('catalog.view_project',))
        first = self.apply(source, 'uid=one', 'dir-one', ('cn=rbac-team,dc=example',), ('catalog.view_project',))
        second = self.apply(source, 'uid=two', 'dir-two', ('cn=rbac-team,dc=example',), ('catalog.view_project',))
        revoked = get_user_model().objects.get(pk=first)
        kept = get_user_model().objects.get(pk=second)
        manual = Group.objects.create(name='rbac-manual')
        revoked.groups.add(manual)
        self.permit(revoked, 'catalog.view_node')
        m.ProjectMembership.objects.create(user=revoked, source=self.source, project=self.foreign)
        self.assertTrue(revoked.has_perm('catalog.view_project'))
        # Токен до отзыва: directory freshness проверяется живым credential.
        actor = s.token_context(s.issue_token(revoked.pk))
        # Отзыв одного пользователя снимает только его membership и scope;
        # права общей группы и второго её участника не затронуты.
        self.apply(source, 'uid=one', 'dir-one', (), (), allowed=False, project_ids=())
        # Свежий экземпляр: у User кэш прав переживает refresh_from_db.
        revoked = get_user_model().objects.get(pk=revoked.pk)
        kept = get_user_model().objects.get(pk=kept.pk)
        self.assertFalse(revoked.has_perm('catalog.view_project'))
        self.assertTrue(kept.has_perm('catalog.view_project'), 'shared group must keep its permissions')
        self.assertTrue(revoked.groups.filter(pk=manual.pk).exists())
        self.assertTrue(revoked.has_perm('catalog.view_node'), 'manual direct permission survives revoke')
        self.assertTrue(m.ProjectMembership.objects.filter(
            user=revoked, source=self.source, project=self.foreign).exists())
        # Живой credential отозванного не приобретает прав из общей группы.
        with self.assertRaises(s.Denied):
            s.Access().authorize(actor, 'catalog.view_project', project_ids=(self.project.pk,))


# FEATURE: #685 — права role/grant регистрирует owning-модель, не Principal.
# HYPOTHESIS: Principal — deprecated metadata (#684), владение строками прав
# должно перейти к живой модели жизненного цикла делегирования.
# RED: content_type строк — principal.
# RESULT: четыре access-права регистрирует Grant; Principal не держит прав.
class PermissionOwnershipCases(RbacBase):

    def test_role_and_grant_permissions_are_owned_by_the_grant_model(self):
        for code in ('view_role', 'approve_role', 'issue_grant', 'revoke_grant'):
            with self.subTest(code=code):
                row = Permission.objects.get(content_type__app_label='access', codename=code)
                self.assertEqual(row.content_type.model, 'grant',
                    'Principal no longer registers action permissions')
        self.permit(self.human, 'access.view_role')
        s.Access().authorize(self.actor, 'access.view_role', project_ids=(self.project.pk,))
