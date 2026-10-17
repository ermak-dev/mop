"""#684 контракты одной authentication path: Actor + CredentialBinding, без Principal.

RED пишется до реализации: сегодня resolver требует Principal row, дублирует
атрибуты Actor в CredentialBinding и различает делегирование видом 'grant'.
Каждый случай падается поведением (Denied/assertion), не ошибкой импорта.
"""
from datetime import timedelta
from unittest.mock import patch
import uuid

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import Client, TransactionTestCase, override_settings
from django.utils import timezone

from mop.server.access import models as m, services as s
from mop.server.catalog.models import Project


def _grant_scaffolding(test):
    """Минимальный publish/approve/provision, как в _credential_cases."""
    import json
    from mop.common import soul
    from mop.server.access import grants as grants_module
    from mop.server.secrets.crypto import Keyring, XChaCha20Provider
    from mop.server.secrets.services import SecretStore
    provider = XChaCha20Provider(Keyring('test', {'test': b'q' * 32}))
    policy = SecretStore(provider=provider, authorizer=s.Access()).create(test.actor,
        project_id=test.project.pk, name='auth-policy', kind='credential', purpose='policy', value=b'bytes')
    proof = grants_module.ApprovedPolicy(uuid.uuid4(), policy.active_version_id, 'b' * 64,
                                         test.human.pk, test.project.pk)

    class Approvals:
        def pin(self, binding):
            return proof

        def validate(self, proof_value, binding):
            if (proof_value.owner_id, proof_value.project_id) != (binding.owner_id, binding.project_id):
                raise PermissionError('approved policy is unavailable')
            return None

    class Bindings:
        def __init__(self):
            self.bindings, self.receipts = {}, {}

        def current(self, project_id, target_id):
            return self.bindings.get((project_id, target_id))

        def activation(self, grant_id):
            return self.receipts.get(grant_id)

    api = grants_module.Grants(provider=provider, approvals=Approvals(), bindings=Bindings())
    raw = b'# manifest\n' + json.dumps({'soul': 'worker', 'version': 1,
        'runtime': {'driver': 'any', 'receives_dispatch': True},
        'capabilities': {'node_verbs': ['send'], 'cluster_verbs': [], 'dispatch': False, 'spawn': []},
        'prompt': 'soul.md'}, ensure_ascii=False).encode()
    bundle = api.publish_bundle(test.actor, project_id=test.project.pk, manifest=raw,
                                prompt=b'prompt', provenance='test')
    api.approve_bundle(test.actor, bundle.id)
    return api, api.reserve(test.actor, project_id=test.project.pk, bundle_id=bundle.id,
                            target_id=uuid.uuid4(), incarnation=uuid.uuid4(),
                            request_id=uuid.uuid4(), expires_at=timezone.now() + timedelta(hours=1))


# FEATURE: #684 — единый trusted authentication path для human/agent/service.
# HYPOTHESIS: Principal как обязательная строка делает вид Actor выбором
# альтернативной identity; CredentialBinding дублирует атрибуты Actor, а
# делегирование различается видом 'grant' вместо grant_id.
# SOLUTION: resolver читает Actor + CredentialBinding напрямую; выдача
# (login/enrollment/bind/rotation) не создаёт Principal rows; контекст несёт
# user/kind/credential/owner/node/grant ссылки; делегирование — grant_id.
# RED: 10 поведенческих red этого файла (см. ниже по случаям) + offline
# tests/actor_auth.py 4/4.
# RESULT: 11/11 ok; offline 4/4 ok; correlation-колонки execution ссылаются
# user_id (#686 executor_user_id), duplicates убраны вместе с SourceRef
# principal UUID. Access/grant/credential/telemetry/endpoint suites зелёные.
# STATUS: FIXED — see #684
@override_settings(ALLOWED_HOSTS=['testserver'])
class SingleAuthenticationPathCases(TransactionTestCase):
    serialized_rollback = True

    def setUp(self):
        from mop.server.controller.runtime import initialize
        initialize()
        self.source = m.IdentitySource.objects.create(name='local', kind='local', priority=0)
        self.human = get_user_model().objects.create_user('auth-human', password='test-password-only')
        m.LoginIdentity.objects.create(user=self.human, source=self.source, external_subject='auth-human')
        self.project = Project.objects.create(name='authed', origin='https://example.invalid/authed.git')
        m.ProjectMembership.objects.create(user=self.human, project=self.project, source=self.source)
        self.permit('catalog.view_project', 'access.view_role', 'access.approve_role',
                    'access.issue_grant', 'access.revoke_grant', 'secrets.manage_project_secret',
                    'sessions.create_puppet')
        self.token = s.issue_token(self.human.pk)
        self.actor = s.token_context(self.token)

    def permit(self, *actions):
        # Логические CONTROL имена через канонический permission_names; выдача
        # строк идёт одному и тому же Actor.
        from mop.server.access.permissions import permission_names
        target = self.human
        for action in actions:
            for permission in permission_names(action):
                app, code = permission.split('.')
                target.user_permissions.add(Permission.objects.get(content_type__app_label=app, codename=code))

    # RED (до фикса) setUp сам падал 'principal is not enrolled': теперь
    # каждый случай держит собственное поведение.
    def test_human_token_and_session_authentication_require_no_principal_row(self):
        fresh = get_user_model().objects.create_user('no-principal', password='test-password-only')
        m.LoginIdentity.objects.create(user=fresh, source=self.source, external_subject='no-principal')
        m.ProjectMembership.objects.create(user=fresh, project=self.project, source=self.source)
        key = s.issue_token(fresh.pk)
        actor = s.token_context(key)
        self.assertEqual(actor.user_id, fresh.pk)
        fresh.user_permissions.add(Permission.objects.get(content_type__app_label='catalog', codename='view_project'))
        s.Access().authorize(actor, 'catalog.view_project', project_ids=(self.project.pk,))
        client = Client()
        response = client.post('/api/v1/auth/login',
            {'username': 'no-principal', 'password': 'test-password-only'}, secure=True)
        self.assertEqual(response.status_code, 200)
        session_actor = s.session_context(client.session.session_key)
        self.assertEqual(session_actor.user_id, fresh.pk)
        # Выдача не создаёт параллельной identity: ни одной Principal row.
        self.assertEqual(m.Principal.objects.filter(user_id__in=(fresh.pk, self.human.pk)).count(), 0)
        # Явный maintenance hook тоже не создаёт строку.
        s.bind_local_identity(fresh.pk)
        self.assertEqual(m.Principal.objects.filter(user=fresh).count(), 0)

    # RED: удалённая (или никогда не существовавшая) Principal row убивает
    # живой credential, хотя Actor и binding не менялись.
    def test_identity_resolution_survives_principal_row_removal(self):
        legacy = get_user_model().objects.create_user('legacy-row', password='test-password-only')
        m.Principal.objects.create(user=legacy, kind='human')
        m.LoginIdentity.objects.create(user=legacy, source=self.source, external_subject='legacy-row')
        m.ProjectMembership.objects.create(user=legacy, project=self.project, source=self.source)
        self.permit('catalog.view_project')
        key = s.issue_token(legacy.pk)
        actor = s.token_context(key)
        # Удаление deprecated metadata не влияет на аутентификацию Actor:
        # credential держится сам на Actor, а не на Principal row.
        legacy.user_permissions.add(Permission.objects.get(content_type__app_label='catalog', codename='view_project'))
        m.Principal.objects.filter(user=legacy).delete()
        self.assertEqual(s.token_context(key).user_id, legacy.pk)
        s.Access().authorize(actor, 'catalog.view_project', project_ids=(self.project.pk,))
        self.assertIsNotNone(s.current_actor(actor))

    # RED: directory projection заводит human Actor вместе с Principal row.
    def test_directory_projection_creates_no_principal_row(self):
        from mop.server.access.directory import DirectoryResult, Router, apply_directory
        source = m.IdentitySource.objects.create(name='directory', kind='ldap', priority=1)
        user_id = apply_directory(source.pk, source.revision, DirectoryResult(
            subject='uid=fresh', username='fresh', groups=('direct',), project_ids=(self.project.pk,),
            all_projects=False, permissions=('catalog.view_project',)))
        user = get_user_model().objects.get(pk=user_id)
        self.assertEqual(user.kind, 'human')
        self.assertEqual(m.Principal.objects.filter(user=user).count(), 0)
        router = Router()
        self.assertEqual(router.get_user(user_id).pk, user_id)

    # RED: локальный login через authoritative backend требует Principal.
    def test_local_directory_login_authenticates_without_principal_row(self):
        from mop.server.access.directory import Router
        fresh = get_user_model().objects.create_user('router-login', password='test-password-only')
        m.LoginIdentity.objects.create(user=fresh, source=self.source, external_subject='router-login')
        router = Router()
        self.assertEqual(router.authenticate(None, username='router-login',
            password='test-password-only').pk, fresh.pk)
        # Неверный пароль не проваливается в следующий backend: прежний fence.
        from django.core.exceptions import PermissionDenied
        with self.assertRaises(PermissionDenied):
            router.authenticate(None, username='router-login', password='wrong-password')
        self.assertEqual(router.get_user(fresh.pk).pk, fresh.pk)

    # RED: enroll_service создаёт Principal(kind='service'); после #684 узел
    # закрепляется в самом credential, а fences (узел/проект/retirement)
    # сохраняются.
    def test_service_enrollment_creates_no_principal_row_and_keeps_fences(self):
        from mop.server.catalog import models as catalog
        def node(name):
            return catalog.Node.objects.create(name=name, driver='host', ssh_host='n.invalid', ssh_port=22,
                control_origin='https://controller.invalid', lan_address='n.invalid', bootstrap_address='n.invalid')
        enrolled, foreign = node('enrolled-684'), node('foreign-684')
        for row in (enrolled, foreign):
            catalog.NodeProject.objects.create(node=row, project=self.project)
        key = s.enroll_service('bare-service', enrolled.pk, project_ids=(self.project.pk,),
                               permissions=('catalog.observe_node',))
        service = get_user_model().objects.get(username='bare-service')
        self.assertEqual(service.kind, 'service')
        self.assertEqual(m.Principal.objects.filter(user=service).count(), 0)
        actor = s.token_context(key)
        self.assertEqual((actor.user_id, actor.node_id), (service.pk, enrolled.id))
        s.Access().authorize(actor, 'catalog.observe_node', node_ids=(enrolled.id,), project_ids=(self.project.pk,))
        with self.assertRaises(s.Denied):
            s.Access().authorize(actor, 'catalog.observe_node', node_ids=(foreign.id,), project_ids=(self.project.pk,))
        catalog.Node.objects.filter(pk=enrolled.pk).update(retired_at=timezone.now(),
            identity_slot=enrolled.id, deployment_member=False)
        with self.assertRaises(s.Denied):
            s.token_context(key)

    # RED: bind_local_identity/issue_token штампуют Principal(kind='human')
    # и контекст с kind HUMAN независимо от вида самого Actor.
    def test_agent_actor_authenticates_through_the_same_path(self):
        from dataclasses import replace
        agent = get_user_model().objects.create_user('stable-agent', password=None)
        get_user_model().objects.filter(pk=agent.pk).update(kind='agent')
        agent.refresh_from_db()
        self.assertEqual(agent.kind, 'agent')
        s.bind_local_identity(agent.pk)
        m.ProjectMembership.objects.create(user=agent, project=self.project, source=self.source)
        key = s.issue_token(agent.pk)
        actor = s.token_context(key)
        self.assertEqual((actor.user_id, actor.kind.value), (agent.pk, 'agent'))
        self.assertEqual(m.Principal.objects.filter(user=agent).count(), 0)
        # Права агента — вопрос общего авторизатора (#685/#686): здесь
        # аутентификация работает, а authorization по умолчанию закрыт.
        with self.assertRaises(s.Denied):
            s.Access().authorize(actor, 'catalog.view_project', project_ids=(self.project.pk,))
        self.assertIsNotNone(s.current_actor(actor))
        # Подмена вида в сохранённом контексте не становится правом.
        with self.assertRaises(s.Denied):
            s.current_actor(replace(actor, kind=type(actor.kind).SERVICE))

    # Контракт сохранённых fences (green до и после): поддельный контекст,
    # деактивация и поколение ключа отказывают по умолчанию.
    def test_fake_context_deactivation_and_rotation_stay_denied(self):
        from dataclasses import replace
        with self.assertRaises(s.Denied):
            s.current_actor(replace(self.actor, user_id=self.human.pk + 1000))
        with self.assertRaises(s.Denied):
            s.current_actor(replace(self.actor, credential_id=uuid.uuid4()))
        get_user_model().objects.filter(pk=self.human.pk).update(is_active=False)
        with self.assertRaises(s.Denied):
            s.token_context(self.token)
        with self.assertRaises(s.Denied):
            s.current_actor(self.actor)
        get_user_model().objects.filter(pk=self.human.pk).update(is_active=True)
        # Неиспользованный старый ключ не усыновляет новое поколение/subject.
        s.revoke_token(self.actor)
        replacement = s.issue_token(self.human.pk)
        with self.assertRaises(s.Denied):
            s.token_context(self.token)
        m.LoginIdentity.objects.filter(user=self.human).update(external_subject='replaced')
        with self.assertRaises(s.Denied):
            s.token_context(replacement)

    # Контракт (green до и после): logout закрывает ровно одну browser session.
    @override_settings(ALLOWED_HOSTS=['testserver'])
    def test_logout_closes_only_the_requested_browser_session(self):
        first, second = Client(), Client()
        for client in (first, second):
            response = client.post('/api/v1/auth/login',
                {'username': 'auth-human', 'password': 'test-password-only'}, secure=True)
            self.assertEqual(response.status_code, 200)
        kept = second.session.session_key
        response = first.post('/api/v1/auth/logout', secure=True)
        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(s.session_context(kept))
        response = second.get('/api/v1/identity', secure=True)
        self.assertEqual(response.status_code, 200)

    # RED: делегирование различается grant_id, а не видом 'grant'; машина
    # делегирования — обычный service Actor без Principal row.
    def test_delegation_context_is_grant_id_not_an_actor_kind(self):
        api, reserved = _grant_scaffolding(self)
        provisioned = api.provision(self.actor, reserved.id, expected_revision=reserved.revision)
        provisioned = api.get(self.actor, reserved.id)
        api.bindings.bindings[(self.project.pk, provisioned.target_id)] = provisioned.binding
        api.bindings.receipts[provisioned.id] = provisioned.binding
        api.activate(provisioned.id, expected_revision=provisioned.revision)
        machine = get_user_model().objects.get(username='grant-' + reserved.id.hex)
        self.assertEqual(machine.kind, 'service')
        self.assertEqual(m.Principal.objects.filter(user=machine).count(), 0)
        from rest_framework.authtoken.models import Token
        key = Token.objects.get(user=machine).key
        # Резолвер не привязан — отказ по умолчанию; привязан — grant_id контекст.
        with self.assertRaises(s.Denied):
            s.token_context(key)
        actor = s.token_context(key, grants=api.local_authority())
        self.assertEqual(actor.grant_id, provisioned.id)
        self.assertEqual(actor.kind.value, 'service')
        with self.assertRaises(s.Denied):
            s.Access().authorize(actor, 'catalog.view_project', project_ids=(self.project.pk,))

    # RED: Admin fences читают Principal kind; service Actor без строки
    # попадает в changelist и получает password form.
    def test_scoped_admin_fences_read_actor_kind(self):
        from django.contrib import admin as django_admin
        from mop.server.controller.admin import ScopedUserAdmin
        service = get_user_model().objects.create_user('admin-visible-service', password=None)
        get_user_model().objects.filter(pk=service.pk).update(kind='service')
        self.human.is_staff = True
        self.human.save()
        staff = Client()
        staff.post('/api/v1/auth/login',
            {'username': 'auth-human', 'password': 'test-password-only'}, secure=True)
        request = staff.get('/admin/actor/actor/').wsgi_request
        queryset = ScopedUserAdmin(get_user_model(), django_admin.site).get_queryset(request)
        self.assertFalse(queryset.filter(pk=service.pk).exists())
        self.assertTrue(queryset.filter(pk=self.human.pk).exists())
        with self.assertRaises(Exception):
            ScopedUserAdmin(get_user_model(), django_admin.site).user_change_password(
                request, str(service.pk))

    # RED: identity endpoint отдаёт principal_id параллельной identity.
    @override_settings(ALLOWED_HOSTS=['testserver'])
    def test_identity_endpoint_reports_actor_facts_only(self):
        client = Client()
        client.post('/api/v1/auth/login',
            {'username': 'auth-human', 'password': 'test-password-only'}, secure=True)
        response = client.get('/api/v1/identity', secure=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['user_id'], self.human.pk)
        self.assertEqual(response.data['kind'], 'human')
        self.assertNotIn('principal_id', response.data)
        self.assertNotIn(self.token, response.content.decode())
