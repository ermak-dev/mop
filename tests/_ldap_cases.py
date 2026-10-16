"""LDAP boundary: поддельный transport, настоящие ORM identity и PostgreSQL."""
from threading import Event
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.db import close_old_connections
from django.test import SimpleTestCase, TransactionTestCase

from mop.server.access.directory import Configuration, DirectoryUnavailable, Router
from mop.server.access.ldap import LdapDirectory


# BUG: DN повторно используется другой записью, bind outage выглядит неверным паролем.
# HYPOTHESIS: adapter не читает immutable attribute и игнорирует result code bind.
# RED: DN вместо UUID; replacement принят; 51/52/53 не поднимают unavailable.
# RED: configuration edit за blocked bind принят; unbind timeout вышел наружу.
# SOLUTION: явный immutable attribute, UUID byte order, uniqueness search, bound CAS;
# snapshot configuration под lock и только result 49 как credential rejection.
# RESULT: 11 LDAP contracts зелёные на изолированных PostgreSQL 16 и 17.
# STATUS: FIXED — see #633
UID_A = '00112233-4455-6677-8899-aabbccddeeff'
UID_B = '11223344-5566-7788-99aa-bbccddeeff00'
DN = 'uid=person,dc=example'
GROUP = 'cn=allowed,dc=example'


class Secrets:
    def bind_password(self, source_id, reference):
        return b'test-service-secret'


class Wire:
    def __init__(self):
        self.subjects = [UID_A]
        self.dn = DN
        self.attribute = 'entryUUID'
        self.code = 0
        self.failure = None
        self.cleanup_failure = None
        self.searches = []
        self.binds = []
        self.duplicates = False

    def connection(self, server, **kwargs):
        wire = self
        class Connection:
            result = {}
            response = []
            def open(self):
                pass
            def start_tls(self):
                return True
            def bind(self):
                wire.binds.append(kwargs['user'])
                if kwargs['user'] != 'cn=reader':
                    if wire.failure:
                        raise wire.failure
                    self.result = {'result': wire.code, 'message': 'private-context test-password'}
                    return wire.code == 0
                return True
            def search(self, base, search_filter, **options):
                wire.searches.append((base, search_filter, options))
                self.result = {'result': 0}
                if base == 'ou=groups,dc=example':
                    self.response = [{'type': 'searchResEntry', 'dn': GROUP}]
                else:
                    entry = {'type': 'searchResEntry', 'dn': wire.dn,
                             'attributes': {wire.attribute: wire.subjects},
                             'raw_attributes': {wire.attribute: wire.subjects}}
                    self.response = [entry]
                    if wire.duplicates and 'entryUUID=' in search_filter:
                        self.response.append(dict(entry, dn='uid=other,dc=example'))
                return True
            def unbind(self):
                if wire.cleanup_failure:
                    raise wire.cleanup_failure
        return Connection()


def configuration():
    return Configuration('test-source', 1, 'ldaps://directory.invalid', 'cn=reader',
        'protected:test', 'ou=users,dc=example', 'ou=groups,dc=example', 'uid', False, False, '')


class AdapterContracts(SimpleTestCase):
    def setUp(self):
        self.wire = Wire()
        self.adapter = LdapDirectory(secrets=Secrets())
        self.patch = patch('ldap3.Connection', side_effect=self.wire.connection)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.network = patch('socket.create_connection', side_effect=AssertionError('real LDAP network'))
        self.network.start()
        self.addCleanup(self.network.stop)

    def test_subject_is_immutable_and_dn_is_only_bind_target(self):
        account, = self.adapter.lookup(configuration(), 'person')
        self.assertEqual(account.subject, UID_A)
        self.assertEqual(account.bind_dn, DN)
        self.assertIn('entryUUID', self.wire.searches[0][2]['attributes'])
        self.wire.subjects = [UID_B]
        replacement, = self.adapter.lookup(configuration(), 'person')
        self.assertNotEqual(account.subject, replacement.subject)

    def test_missing_multivalued_and_duplicate_subjects_fail_closed(self):
        for subjects, duplicate in (([], False), ([UID_A, UID_B], False), (['not-an-immutable-uuid'], False), ([UID_A], True)):
            with self.subTest(subjects=subjects, duplicate=duplicate):
                self.wire.subjects, self.wire.duplicates = subjects, duplicate
                with self.assertRaises(DirectoryUnavailable):
                    self.adapter.lookup(configuration(), 'person')

    def test_ad_guid_uses_little_endian_wire_uuid_and_nested_filter(self):
        cfg = SimpleNamespace(**(vars(configuration()) | {'subject_attribute': 'objectGUID'}))
        cfg.nested_ad = True
        self.wire.attribute = 'objectGUID'
        for text, wire_hex in ((UID_A, '33221100554477668899aabbccddeeff'),
                               (UID_B, '443322116655887799aabbccddeeff00')):
            self.wire.subjects = [bytes.fromhex(wire_hex)]
            account, = self.adapter.lookup(cfg, 'person')
            self.assertEqual(account.subject, text)
            encoded = ''.join('\\' + wire_hex[i:i + 2] for i in range(0, 32, 2))
            self.assertIn('(objectGUID=' + encoded + ')', [s[1] for s in self.wire.searches])
        group_search = next(s for s in self.wire.searches if s[0] == cfg.group_base)
        self.assertIn('(member:1.2.840.113556.1.4.1941:=' + DN + ')', group_search[1])
        self.assertIn('(uniqueMember=' + DN + ')', group_search[1])

    def test_only_invalid_credentials_is_wrong_password(self):
        for code in (49, 51, 52, 53, 1, 0):
            with self.subTest(code=code):
                self.wire.code = code
                if code in (49, 0):
                    self.assertEqual(self.adapter.check_password(configuration(), DN, 'test-password'), code == 0)
                else:
                    with self.assertRaises(DirectoryUnavailable) as raised:
                        self.adapter.check_password(configuration(), DN, 'test-password')
                    self.assertNotIn('private-context', str(raised.exception))
                    self.assertNotIn('test-password', str(raised.exception))

    def test_connection_cleanup_transport_failure_is_sanitized(self):
        self.wire.cleanup_failure = TimeoutError('private-context test-password')
        with self.assertRaises(DirectoryUnavailable) as raised:
            self.adapter.check_password(configuration(), DN, 'test-password')
        self.assertNotIn('private-context', str(raised.exception))
        self.assertNotIn('test-password', str(raised.exception))

    def test_timeout_is_sanitized_and_unbound_secret_precedes_network(self):
        self.wire.failure = TimeoutError('private-context test-password')
        with self.assertRaises(DirectoryUnavailable) as raised:
            self.adapter.check_password(configuration(), DN, 'test-password')
        self.assertNotIn('private-context', str(raised.exception))
        with self.assertRaises(DirectoryUnavailable):
            LdapDirectory().lookup(configuration(), 'person')
        self.assertEqual(self.wire.binds, [DN])


class DirectoryIdentityContracts(TransactionTestCase):
    serialized_rollback = True

    def setUp(self):
        from mop.server.access import models as m
        from mop.server.catalog.models import Project
        self.m = m
        self.source = m.IdentitySource.objects.create(name='directory', kind='ldap', priority=-1)
        m.DirectoryConfiguration.objects.create(source=self.source, url='ldaps://directory.invalid',
            bind_dn='cn=reader', bind_secret_ref='protected:test', user_base='ou=users,dc=example',
            group_base='ou=groups,dc=example')
        self.project = Project.objects.create(name='one', origin='https://example.invalid/one.git')
        m.DirectoryGroupRule.objects.create(source=self.source, group_dn=GROUP, project=self.project)
        self.wire = Wire()
        self.router = Router(provider=LdapDirectory(secrets=Secrets()))
        p = patch('ldap3.Connection', side_effect=self.wire.connection)
        p.start()
        self.addCleanup(p.stop)
        p = patch('socket.create_connection', side_effect=AssertionError('real LDAP network'))
        p.start()
        self.addCleanup(p.stop)

    def login(self, username='person'):
        return self.router.authenticate(None, username=username, password='test-password')

    # HYPOTHESIS: app.codename — логическое право Django, не уникальная строка
    # Permission; несколько ContentTypes не должны срывать directory projection.
    # RED: PG17 — MultipleObjectsReturned и сырой DoesNotExist до исправления.
    # SOLUTION: все эквивалентные Django rows; отсутствие даёт DirectoryUnavailable.
    # RESULT: PG16/17 — по 13/13 LDAP checks green, локальные права не затронуты.
    # STATUS: FIXED — see #670
    def test_duplicate_permission_codenames_preserve_authority_and_source_revocation(self):
        from django.contrib.auth.models import Permission
        choices = Permission.objects.filter(content_type__app_label='execution', codename='view_usage')
        self.assertGreater(choices.count(), 1)
        rule = self.m.DirectoryGroupRule.objects.get(source=self.source, group_dn=GROUP)
        rule.permissions.add(choices.get(content_type__model='usagesource'))
        user = self.login()
        self.assertTrue(user.has_perm('execution.view_usage'))
        self.assertFalse(user.has_perm('execution.assign_work'))
        local = Permission.objects.get(content_type__app_label='catalog', codename='view_project')
        user.user_permissions.add(local)
        self.assertEqual(self.login().pk, user.pk)
        self.assertEqual(get_user_model().objects.count(), 1)
        rule.permissions.clear()
        self.assertEqual(self.login().pk, user.pk)
        current = get_user_model().objects.get(pk=user.pk)
        self.assertFalse(current.has_perm('execution.view_usage'))
        self.assertTrue(current.has_perm('catalog.view_project'))
        self.assertTrue(self.m.ProjectMembership.objects.filter(user=user, source=self.source,
                                                               project=self.project).exists())

    def test_missing_permission_is_unavailable_not_partial_directory_acceptance(self):
        from django.contrib.auth.models import Permission
        from mop.server.access.directory import DirectoryResult, apply_directory
        Permission.objects.filter(content_type__app_label='execution', codename='view_usage').delete()
        result = DirectoryResult(UID_A, 'person', (GROUP,), (self.project.pk,), False,
                                 ('execution.view_usage',))
        with self.assertRaises(DirectoryUnavailable):
            apply_directory(self.source.pk, self.source.revision, result)
        self.assertFalse(get_user_model().objects.exists())
        self.assertFalse(self.m.LoginIdentity.objects.exists())
        self.assertFalse(self.m.ProjectMembership.objects.exists())

    def test_delete_recreate_same_dn_cannot_inherit_local_grants_or_ownership(self):
        from django.contrib.auth.models import Permission
        from mop.server.catalog.models import ClientRegistration
        user = self.login()
        local = self.m.IdentitySource.objects.create(name='local', kind='local')
        grant = self.m.ProjectMembership.objects.create(user=user, source=local, project=self.project)
        permission = Permission.objects.get(content_type__app_label='catalog', codename='view_project')
        user.user_permissions.add(permission)
        from mop.server.controller.runtime import initialize
        initialize()
        client = ClientRegistration.objects.create(label='owned', installation_id=1)
        owner = self.m.ClientOwnership.objects.create(user=user, client=client)
        self.wire.subjects = [UID_B]
        with self.assertRaises(PermissionDenied):
            self.login()
        identity = self.m.LoginIdentity.objects.get(user=user)
        self.assertEqual(identity.external_subject, UID_A)
        self.assertFalse(identity.authorized)
        self.assertTrue(self.m.ProjectMembership.objects.filter(pk=grant.pk, user=user).exists())
        self.assertTrue(self.m.ClientOwnership.objects.filter(pk=owner.pk, user=user).exists())
        self.assertTrue(get_user_model().objects.get(pk=user.pk).user_permissions.filter(pk=permission.pk).exists())
        self.assertEqual(get_user_model().objects.count(), 1)
        self.assertFalse(self.m.ProjectMembership.objects.filter(user=user, source=self.source).exists())

    def test_same_subject_dn_rename_keeps_identity(self):
        user = self.login()
        self.wire.dn = 'uid=renamed,dc=example'
        self.assertEqual(self.login().pk, user.pk)
        self.assertEqual(self.wire.binds[-1], self.wire.dn)

    def test_reused_subject_under_different_login_cannot_claim_existing_user(self):
        user = self.login()
        with self.assertRaises(PermissionDenied):
            self.login('different-person')
        user.refresh_from_db()
        self.assertEqual(user.username, 'person')

    def test_bind_service_failures_escape_django_as_unavailable_without_fallback(self):
        from django.contrib.auth import authenticate
        from mop.server.controller.authbackend import AuthenticationUnavailable
        lower = self.m.IdentitySource.objects.create(name='lower', kind='ldap', priority=1)
        self.m.DirectoryConfiguration.objects.create(source=lower, url='ldaps://directory.invalid')
        with patch('mop.server.controller.authbackend.LdapDirectory', return_value=self.router.provider):
            for code in (51, 52, 53):
                self.wire.code = code
                with self.subTest(code=code):
                    with self.assertRaises(AuthenticationUnavailable) as raised:
                        authenticate(username='person', password='test-password')
                    self.assertNotIn('private-context', str(raised.exception))
            self.wire.failure = TimeoutError('private-context test-password')
            with self.assertRaises(AuthenticationUnavailable) as raised:
                authenticate(username='person', password='test-password')
            self.assertNotIn('test-password', str(raised.exception))
            self.wire.failure = None
            self.wire.code = 49
            self.assertIsNone(authenticate(username='person', password='test-password'))
        self.assertEqual(sum(dn == 'cn=reader' for dn in self.wire.binds), 5)

    def test_source_revision_and_deactivation_during_blocked_bind_reject_acceptance(self):
        from mop.server.access.directory import DirectoryAccount
        for change in ('revision', 'configuration', 'inactive'):
            with self.subTest(change=change):
                user = self.login()
                subject = self.m.LoginIdentity.objects.get(user=user).external_subject
                pending, release = Event(), Event()
                class Blocked:
                    def lookup(inner, cfg, username):
                        return (DirectoryAccount(subject, username, (GROUP,), bind_dn=DN),)
                    def check_password(inner, cfg, subject, password):
                        pending.set()
                        if not release.wait(10):
                            raise AssertionError('blocked bind not released')
                        return True
                def login():
                    close_old_connections()
                    try:
                        return Router(provider=Blocked()).authenticate(None, username='person', password='test-password')
                    finally:
                        close_old_connections()
                with ThreadPoolExecutor(max_workers=1) as pool:
                    future = pool.submit(login)
                    self.assertTrue(pending.wait(10))
                    if change == 'revision':
                        self.m.IdentitySource.objects.filter(pk=self.source.pk).update(revision=2)
                    elif change == 'configuration':
                        self.m.DirectoryConfiguration.objects.filter(source=self.source).update(login_attribute='cn')
                    else:
                        get_user_model().objects.filter(pk=user.pk).update(is_active=False)
                    release.set()
                    with self.assertRaises(PermissionDenied):
                        future.result(15)
                self.m.IdentitySource.objects.filter(pk=self.source.pk).update(revision=1)
                self.m.DirectoryConfiguration.objects.filter(source=self.source).update(login_attribute='uid')
