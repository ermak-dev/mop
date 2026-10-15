"""Контракты Access через настоящий Django runner и PostgreSQL."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

from django.apps import apps
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.db import close_old_connections, transaction
from django.test import Client, TransactionTestCase, override_settings
from django.utils import timezone


# FEATURE: #633 — единая identity, live credential и scope без HTTP в use case.
# HYPOTHESIS: у foundation нет Access и auth endpoints.
# SOLUTION: стандартный Django Auth + source provenance + единый evaluator.
@override_settings(ALLOWED_HOSTS=['testserver'])
class AuthenticationBoundary(TransactionTestCase):
    def test_login_requires_csrf_even_for_anonymous_browser(self):
        response = Client(enforce_csrf_checks=True).post('/api/v1/auth/login',
            {'username': 'human', 'password': 'not-a-real-secret'}, secure=True)
        self.assertEqual(response.status_code, 403)

    def test_password_token_endpoint_returns_standard_token_only_explicitly(self):
        get_user_model().objects.create_user('unbound', password='test-password-only')
        response = Client().post('/api/v1/auth/token',
            {'username': 'unbound', 'password': 'test-password-only'}, secure=True)
        self.assertEqual(response.status_code, 403)
        self.assertNotIn('test-password-only', response.content.decode())

    def test_logout_is_post_only_and_credential_responses_are_no_store(self):
        response = Client().get('/api/v1/auth/logout', secure=True)
        self.assertEqual(response.status_code, 405)
        self.assertIn('no-store', response.headers.get('Cache-Control', ''))


class AccessContracts(TransactionTestCase):
    def setUp(self):
        # Отсутствующий app — собственный проверяемый red, а не ошибка импорта.
        self.assertTrue(apps.is_installed('mop.server.access'), 'Access schema must be installed')
        from mop.server.access import models as m, services as s
        from mop.server.catalog.models import Project
        self.m, self.s = m, s
        self.source = m.IdentitySource.objects.create(name='local', kind='local', priority=0)
        self.user = get_user_model().objects.create_user('human', password='test-password-only')
        m.Principal.objects.create(user=self.user, kind='human')
        m.LoginIdentity.objects.create(user=self.user, source=self.source, external_subject='human')
        self.project = Project.objects.create(name='one', origin='https://example.invalid/one.git')
        self.foreign = Project.objects.create(name='two', origin='https://example.invalid/two.git')
        m.ProjectMembership.objects.create(user=self.user, source=self.source, project=self.project)
        self.permission('catalog.view_project')
        self.token = s.issue_token(self.user.pk)
        self.actor = s.token_context(self.token)

    def permission(self, name):
        app, codename = name.split('.')
        permission = Permission.objects.get(content_type__app_label=app, codename=codename)
        self.user.user_permissions.add(permission)

    def test_action_and_scope_are_independent_and_fresh(self):
        access = self.s.Access()
        access.authorize(self.actor, 'catalog.view_project', project_ids=(self.project.pk,))
        with self.assertRaises(self.s.NotVisible):
            access.authorize(self.actor, 'catalog.view_project', project_ids=(self.foreign.pk,))
        self.user.user_permissions.clear()
        with self.assertRaises(self.s.Denied):
            access.authorize(self.actor, 'catalog.view_project', project_ids=(self.project.pk,))
        self.user.is_staff = True
        self.user.save()
        with self.assertRaises(self.s.Denied):
            access.authorize(self.actor, 'catalog.view_project', project_ids=(self.project.pk,))

    def test_catalog_port_list_detail_and_multi_reference_use_same_scope(self):
        from mop.server.catalog.services import Catalog
        catalog = Catalog(authorizer=self.s.Access())
        self.assertEqual(tuple(row.id for row in catalog.list_projects(self.actor)), (self.project.pk,))
        self.assertEqual(catalog.get_project(self.actor, self.project.pk).id, self.project.pk)
        with self.assertRaises(self.s.NotVisible):
            catalog.get_project(self.actor, self.foreign.pk)
        with self.assertRaises(self.s.NotVisible):
            self.s.Access().authorize(self.actor, 'catalog.view_project',
                project_ids=(self.project.pk, self.foreign.pk))
        with self.assertRaises(self.s.Denied):
            catalog.register_project(self.actor, 'https://example.invalid/three.git')

    def test_all_project_membership_is_not_an_action_grant(self):
        self.m.AllProjectMembership.objects.create(user=self.user, source=self.source)
        self.s.Access().authorize(self.actor, 'catalog.view_project', project_ids=(self.foreign.pk,))
        with self.assertRaises(self.s.Denied):
            self.s.Access().authorize(self.actor, 'access.manage_proxy')

    def test_context_is_immutable_credential_bound_and_contains_no_secrets(self):
        from dataclasses import FrozenInstanceError, replace
        self.assertNotIn(self.token, repr(self.actor))
        self.assertNotIn(self.user.password, repr(self.actor))
        with self.assertRaises(FrozenInstanceError):
            self.actor.user_id = 99
        other = get_user_model().objects.create_user('other')
        with self.assertRaises(self.s.Denied):
            self.s.Access().authorize(replace(self.actor, user_id=other.pk), 'catalog.view_project')
        with self.assertRaises(self.s.Denied):
            self.s.Access().authorize({'user_id': self.user.pk, 'kind': 'human'}, 'catalog.view_project')

    def test_stale_token_rotation_deactivation_and_permission_revoke(self):
        replacement = self.s.rotate_token(self.actor)
        self.assertNotEqual(replacement, self.token)
        with self.assertRaises(self.s.Denied):
            self.s.Access().authorize(self.actor, 'catalog.view_project')
        fresh = self.s.token_context(replacement)
        self.user.is_active = False
        self.user.save()
        with self.assertRaises(self.s.Denied):
            self.s.Access().authorize(fresh, 'catalog.view_project')

    @override_settings(ALLOWED_HOSTS=['testserver'])
    def test_session_logout_and_password_change_invalidate_existing_context(self):
        client = Client()
        self.assertTrue(client.login(username='human', password='test-password-only'))
        actor = self.s.session_context(client.session.session_key)
        self.assertNotIn(client.session.session_key, repr(actor))
        self.s.Access().authorize(actor, 'catalog.view_project')
        client.logout()
        with self.assertRaises(self.s.Denied):
            self.s.Access().authorize(actor, 'catalog.view_project')
        client.login(username='human', password='test-password-only')
        actor = self.s.session_context(client.session.session_key)
        self.user.set_password('replacement-password-only')
        self.user.save()
        with self.assertRaises(self.s.Denied):
            self.s.Access().authorize(actor, 'catalog.view_project')

    def test_service_flags_do_not_turn_service_into_human_or_global_admin(self):
        self.user.set_unusable_password()
        self.user.is_staff = self.user.is_superuser = True
        self.user.save()
        self.m.Principal.objects.filter(user=self.user).update(kind='service')
        actor = self.s.token_context(self.token)
        with self.assertRaises(self.s.Denied):
            self.s.Access().authorize(actor, 'catalog.view_project', project_ids=(self.project.pk,))
        with self.assertRaises(self.s.Denied):
            self.s.issue_token(self.user.pk)

    def test_token_issue_serializes_one_user_and_rotation_rolls_back(self):
        from rest_framework.authtoken.models import Token
        barrier = Barrier(2)
        def issue():
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                return self.s.issue_token(self.user.pk)
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = tuple(pool.map(lambda _: issue(), range(2)))
        self.assertEqual(results, (self.token, self.token))
        self.assertEqual(Token.objects.filter(user=self.user).count(), 1)
        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                self.s.rotate_token(self.actor)
                raise RuntimeError('test rollback')
        self.s.Access().authorize(self.actor, 'catalog.view_project')

    def test_source_binding_and_wrong_first_password_never_fall_through(self):
        from django.contrib.auth import authenticate
        self.m.IdentitySource.objects.create(name='directory', kind='ldap', priority=1)
        self.assertIsNone(authenticate(username='human', password='wrong-password'))
        self.assertEqual(authenticate(username='human', password='test-password-only').pk, self.user.pk)
        self.source.enabled = False
        self.source.save()
        self.assertIsNone(authenticate(username='human', password='test-password-only'))
        with self.assertRaises(self.s.Denied):
            self.s.Access().authorize(self.actor, 'catalog.view_project')

    def test_ldap_refresh_revokes_only_source_owned_scope_and_groups(self):
        from mop.server.access.directory import DirectoryResult, apply_directory
        from django.contrib.auth.models import Group
        source = self.m.IdentitySource.objects.create(name='directory', kind='ldap', priority=1)
        user = get_user_model().objects.create_user('ldap-human')
        self.m.Principal.objects.create(user=user, kind='human')
        identity = self.m.LoginIdentity.objects.create(user=user, source=source, external_subject='uid=human')
        independent = Group.objects.create(name='local-manual')
        user.groups.add(independent)
        self.m.ProjectMembership.objects.create(user=user, source=self.source, project=self.project)
        apply_directory(source.pk, source.revision, DirectoryResult(
            subject='uid=human', username='ldap-human', groups=('direct', 'nested'),
            project_ids=(self.foreign.pk,), all_projects=True, permissions=('catalog.view_project',)))
        user.refresh_from_db()
        self.assertFalse(user.has_usable_password())
        self.assertTrue(self.m.AllProjectMembership.objects.filter(user=user, source=source).exists())
        apply_directory(source.pk, source.revision, DirectoryResult(
            subject='uid=human', username='ldap-human', groups=(), project_ids=(),
            all_projects=False, permissions=(), allowed=False))
        self.assertFalse(self.m.ProjectMembership.objects.filter(user=user, source=source).exists())
        self.assertTrue(self.m.ProjectMembership.objects.filter(user=user, source=self.source).exists())
        self.assertTrue(user.groups.filter(pk=independent.pk).exists())
        identity.refresh_from_db()
        self.assertFalse(identity.authorized)

    def test_ldap_freshness_and_revision_fail_closed(self):
        self.source.kind = 'ldap'
        self.source.save()
        self.m.LoginIdentity.objects.filter(user=self.user).update(
            validated_revision=self.source.revision, validated_at=timezone.now() - timedelta(days=1))
        with self.assertRaises(self.s.Denied):
            self.s.Access().authorize(self.actor, 'catalog.view_project')

    def test_directory_router_first_known_denial_and_mapping_are_authoritative(self):
        from mop.server.access.directory import DirectoryAccount, Router
        source = self.m.IdentitySource.objects.create(name='directory', kind='ldap', priority=1)
        self.m.DirectoryConfiguration.objects.create(source=source, url='ldaps://directory.invalid',
            bind_dn='cn=reader', bind_secret_ref='protected:test', user_base='dc=example')
        rule = self.m.DirectoryGroupRule.objects.create(source=source, group_dn='cn=nested',
            project=self.foreign, all_projects=True)
        rule.permissions.add(Permission.objects.get(content_type__app_label='catalog', codename='view_project'))
        class Provider:
            def __init__(self):
                self.calls = []
                self.password_ok = True
                self.groups = ('cn=nested',)
            def lookup(self, configuration, username):
                from django.db import connection
                if connection.in_atomic_block:
                    raise AssertionError('LDAP I/O inside SQL')
                self.calls.append(username)
                return (DirectoryAccount(subject='uid=ldap', username='ldap', groups=self.groups),)
            def check_password(self, configuration, subject, password):
                return self.password_ok
        provider = Provider()
        router = Router(provider=provider)
        from django.core.exceptions import PermissionDenied
        with self.assertRaises(PermissionDenied):
            router.authenticate(None, username='human', password='wrong')
        self.assertEqual(provider.calls, [])
        user = router.authenticate(None, username='ldap', password='test-password-only')
        self.assertIsNotNone(user)
        self.assertFalse(user.has_usable_password())
        self.assertTrue(user.has_perm('catalog.view_project'))
        self.assertTrue(self.m.AllProjectMembership.objects.filter(user=user, source=source).exists())
        provider.groups = ()
        from django.core.exceptions import PermissionDenied
        with self.assertRaises(PermissionDenied):
            router.authenticate(None, username='ldap', password='test-password-only')
        self.assertFalse(self.m.AllProjectMembership.objects.filter(user=user, source=source).exists())
        self.assertFalse(get_user_model().objects.get(pk=user.pk).has_perm('catalog.view_project'))

    def test_directory_outage_ambiguous_and_unbound_secret_do_not_fall_through(self):
        from mop.server.access.directory import DirectoryAccount, Router, DirectoryUnavailable
        from django.core.exceptions import PermissionDenied
        source = self.m.IdentitySource.objects.create(name='directory', kind='ldap', priority=-1)
        self.m.DirectoryConfiguration.objects.create(source=source, url='ldaps://directory.invalid',
            bind_dn='cn=reader', bind_secret_ref='protected:test', user_base='dc=example')
        class Outage:
            def lookup(self, configuration, username):
                raise DirectoryUnavailable('directory unavailable')
        with self.assertRaises(PermissionDenied):
            Router(provider=Outage()).authenticate(None, username='unknown', password='anything')
        class Ambiguous:
            def lookup(self, configuration, username):
                return (DirectoryAccount(subject='a', username='unknown', groups=()),
                        DirectoryAccount(subject='b', username='unknown', groups=()))
        with self.assertRaises(PermissionDenied):
            Router(provider=Ambiguous()).authenticate(None, username='unknown', password='anything')
        with self.assertRaises(PermissionDenied):
            Router().authenticate(None, username='unknown', password='anything')

    @override_settings(ALLOWED_HOSTS=['testserver'])
    def test_browser_flow_csrf_and_password_token_rate_limit(self):
        client = Client(enforce_csrf_checks=True)
        response = client.get('/api/v1/auth/csrf', secure=True)
        self.assertEqual(response.status_code, 200)
        csrf = client.cookies['csrftoken'].value
        response = client.post('/api/v1/auth/login',
            {'username': 'human', 'password': 'test-password-only'}, secure=True, HTTP_X_CSRFTOKEN=csrf)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(client.cookies['sessionid']['secure'])
        self.assertTrue(client.cookies['sessionid']['httponly'])
        response = client.post('/api/v1/auth/token', secure=True)
        self.assertEqual(response.status_code, 403)
        response = client.post('/api/v1/auth/token', secure=True,
            HTTP_X_CSRFTOKEN=client.cookies['csrftoken'].value)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['token'], self.token)
        response = client.post('/api/v1/auth/logout', secure=True,
            HTTP_X_CSRFTOKEN=client.cookies['csrftoken'].value)
        self.assertEqual(response.status_code, 200)
        password_client = Client()
        for _ in range(12):
            response = password_client.post('/api/v1/auth/token',
                {'username': 'unknown', 'password': 'not-the-password'}, secure=True)
        self.assertEqual(response.status_code, 429)
        self.assertNotIn('not-the-password', response.content.decode())

    def test_read_after_wait_rechecks_scope_and_revoke(self):
        self.s.Access().authorize(self.actor, 'catalog.view_project', project_ids=(self.project.pk,))
        self.m.ProjectMembership.objects.filter(user=self.user).delete()
        with self.assertRaises(self.s.NotVisible):
            self.s.Access().authorize(self.actor, 'catalog.view_project', project_ids=(self.project.pk,))
        self.s.revoke_token(self.actor)
        with self.assertRaises(self.s.Denied):
            self.s.current_actor(self.actor)

    @override_settings(ALLOWED_HOSTS=['testserver'])
    def test_drf_and_admin_share_filtered_list_and_in_scope_denial(self):
        client = Client(HTTP_AUTHORIZATION='Token ' + self.token)
        response = client.get('/api/v1/projects', secure=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['count'], 1)
        self.assertEqual(response.json()['results'][0]['id'], str(self.project.pk))
        self.assertEqual(client.get('/api/v1/projects/' + str(self.foreign.pk), secure=True).status_code, 404)
        self.user.is_staff = True
        self.user.save()
        browser = Client()
        browser.login(username='human', password='test-password-only')
        response = browser.get('/admin/catalog/project/', secure=True)
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, self.foreign.origin)
        self.user.user_permissions.clear()
        self.assertEqual(client.get('/api/v1/projects/' + str(self.project.pk), secure=True).status_code, 403)
        self.assertEqual(browser.get('/admin/catalog/project/', secure=True).status_code, 403)

    def test_client_fk_cannot_move_binding_to_foreign_project_or_client(self):
        from mop.server.catalog.models import ClientRegistration
        from mop.server.catalog.services import Catalog
        from mop.server.controller.runtime import initialize
        from mop.server.controller.models import Installation
        initialize()
        client = ClientRegistration.objects.create(installation=Installation.objects.get(pk=1), label='owned')
        foreign_client = ClientRegistration.objects.create(installation=Installation.objects.get(pk=1), label='foreign')
        self.m.ClientOwnership.objects.create(client=client, user=self.user)
        self.permission('catalog.change_workspacebinding')
        catalog = Catalog(authorizer=self.s.Access())
        result = catalog.bind_workspace(self.actor, client.pk, 'work', self.project.pk,
            integration_branch='main', expected_revision=0)
        self.assertEqual(result.project_id, self.project.pk)
        with self.assertRaises(self.s.NotVisible):
            catalog.bind_workspace(self.actor, client.pk, 'work', self.foreign.pk,
                integration_branch='main', expected_revision=1)
        with self.assertRaises(self.s.NotVisible):
            catalog.bind_workspace(self.actor, foreign_client.pk, 'work', self.project.pk,
                integration_branch='main', expected_revision=0)

    @override_settings(ALLOWED_HOSTS=['testserver'])
    def test_json_cannot_choose_source_actor_owner_node_or_grant(self):
        for name in ('source', 'actor', 'owner', 'node', 'grant', 'kind'):
            response = Client().post('/api/v1/auth/token', {
                'username': 'human', 'password': 'test-password-only', name: 'forged'},
                content_type='application/json', secure=True)
            self.assertEqual(response.status_code, 400, name)
            self.assertNotIn(self.token, response.content.decode())
        from django.contrib.admin.models import LogEntry
        self.assertFalse(LogEntry.objects.filter(change_message__contains=self.token).exists())

    def test_two_concurrent_rotations_accept_only_one_old_credential(self):
        barrier = Barrier(2)
        def rotate(_):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                try:
                    self.s.rotate_token(self.actor)
                    return 'rotated'
                except self.s.Denied:
                    return 'denied'
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertCountEqual(tuple(pool.map(rotate, range(2))), ('rotated', 'denied'))

    def test_token_admin_is_not_registered(self):
        from django.contrib import admin
        from rest_framework.authtoken.models import Token, TokenProxy
        self.assertFalse(admin.site.is_registered(Token))
        self.assertFalse(admin.site.is_registered(TokenProxy))
