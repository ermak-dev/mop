"""LDAP boundary: поддельный провод python-ldap (то, что зовёт django-auth-ldap),
настоящие ORM identity и PostgreSQL."""
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import patch

import ldap
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.db import close_old_connections
from django.test import SimpleTestCase, TransactionTestCase, override_settings

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
#
# BUG (#693): Django-сторона LDAP держала второй собственный ldap3-стек и
# повторяла механику bind/поиска/групп стандартного backend Django Auth.
# HYPOTHESIS: тонкий адаптер на django-auth-ldap держит все контракты #633
# (immutable subject, fail-closed 51/52/53, неоднозначный логин, секрет раньше
# сети, TLS/доверие, nested/AD групповыми типами библиотеки), а ORM остаётся
# единственным источником настроек: статические AUTH_LDAP_* не читаются,
# MIRROR_GROUPS/FIND_GROUP_PERMS выключены, проекция -- общие Group #685.
# RED: провод python-ldap не вызывается (адаптер на ldap3), backend_settings нет:
# 1 fail + 24 errors из 80 (_ldap_cases + _access_cases, PG17/py3.12).
# SOLUTION: LDAPBackend одного вызова, LDAPSearch и групповые типы библиотеки
# (nested AD -- NestedActiveDirectoryGroupType), ldap_error -- сбой поиска
# логина не «нет записи»; настройки из ORM выставлены по всем именам.
# RESULT: 80/80 зелёные; мутации (без ldap_error, без лимита групп, без явных
# настроек, без UTF-8 GUID, без переспроса логина, без вложенного типа) --
# 15 красных проверок. Реальный каталог не проверялся.
# STATUS: FIXED — see #693
UID_A = '00112233-4455-6677-8899-aabbccddeeff'
UID_B = '11223344-5566-7788-99aa-bbccddeeff00'
DN = 'uid=person,dc=example'
GROUP = 'cn=allowed,dc=example'
PARENT = 'cn=parent,dc=example'
USERS, GROUPS = 'ou=users,dc=example', 'ou=groups,dc=example'
CA_PEM = b'-----BEGIN CERTIFICATE-----directory-test-----END CERTIFICATE-----\n'
MEMBER_TERM = re.compile(r'\((member|uniqueMember|memberUid)=((?:[^()\\]|\\.)*)\)')


class Secrets:
    def bind_password(self, source_id, reference):
        return b'test-service-secret'

    def ca_pem(self, source_id, reference):
        return CA_PEM


class Wire:
    """Провод python-ldap: initialize/set_option/start_tls_s/simple_bind_s/
    search_s/unbind_s -- ровно то, что зовёт django-auth-ldap. Значения
    атрибутов -- bytes, как у python-ldap; размерный лимит соединения
    исполняется так, как его исполнил бы сервер (sizeLimitExceeded)."""

    def __init__(self):
        self.subjects = [UID_A]
        self.dn = DN
        self.attribute = 'entryUUID'
        self.code = 0
        self.service_code = 0
        self.failure = None
        self.user_failure = None
        self.group_failure = None
        self.cleanup_failure = None
        self.searches = []
        self.binds = []
        self.options = []
        self.ca_files = []
        self.ca_contents = []
        self.uris = []
        self.started_tls = 0
        self.unbound = 0
        self.duplicates = False
        self.login_entries = 1
        # Членство сверх основной группы: DN группы -> {(атрибут, значение)}.
        self.groups = {}

    def membership(self):
        groups = {GROUP: {('member', self.dn)}}
        for dn, members in self.groups.items():
            groups.setdefault(dn, set()).update(members)
        return groups

    def entry(self, dn):
        values = [value.encode() if isinstance(value, str) else value for value in self.subjects]
        return (dn, {self.attribute: values, 'uid': [b'person']})

    def initialize(self, uri, **kwargs):
        self.uris.append(uri)
        wire = self

        class Connection:
            limit = 0

            def set_option(self, option, value):
                wire.options.append((option, value))
                if option == ldap.OPT_SIZELIMIT:
                    self.limit = value
                if option == ldap.OPT_X_TLS_CACERTFILE:
                    wire.ca_files.append(value)
                    with open(value) as f:
                        wire.ca_contents.append(f.read())

            def start_tls_s(self):
                wire.started_tls += 1

            def simple_bind_s(self, dn, password):
                wire.binds.append(dn)
                code = wire.service_code if dn == 'cn=reader' else wire.code
                if dn != 'cn=reader' and wire.failure:
                    raise wire.failure
                if code:
                    rejection = {49: ldap.INVALID_CREDENTIALS, 51: ldap.BUSY,
                                 52: ldap.UNAVAILABLE, 53: ldap.UNWILLING_TO_PERFORM,
                                 1: ldap.OPERATIONS_ERROR}[code]
                    raise rejection({'desc': 'private-context', 'info': 'test-password'})

            def search_s(self, base, scope, filterstr, attrlist=None):
                wire.searches.append((base, scope, filterstr, tuple(attrlist or ())))
                if base == GROUPS:
                    if wire.group_failure:
                        raise wire.group_failure
                    terms = {(name, value.casefold()) for name, value in MEMBER_TERM.findall(filterstr)}
                    results = [(dn, {'cn': [dn.split(',')[0].split('=')[1].encode()]})
                               for dn, members in sorted(wire.membership().items())
                               if {(a, v.casefold()) for a, v in members} & terms]
                else:
                    if wire.user_failure:
                        # Сбой разовый: повторный поиск уже ответил бы «нет».
                        failure, wire.user_failure = wire.user_failure, None
                        raise failure
                    if wire.attribute + '=' in filterstr:
                        results = [wire.entry(wire.dn)]
                        if wire.duplicates:
                            results.append(wire.entry('uid=other,dc=example'))
                    else:
                        results = [wire.entry(dn) for dn in
                                   (wire.dn, 'uid=second,dc=example', 'uid=third,dc=example')[:wire.login_entries]]
                if self.limit and len(results) > self.limit:
                    raise ldap.SIZELIMIT_EXCEEDED({'desc': 'Size limit exceeded'})
                return results

            def unbind_s(self):
                wire.unbound += 1
                if wire.cleanup_failure:
                    raise wire.cleanup_failure
        return Connection()


def configuration(url='ldaps://directory.invalid', starttls=False, ca='', nested=False,
                  login='uid', subject='entryUUID'):
    return Configuration('test-source', 1, url, 'cn=reader', 'protected:test',
        USERS, GROUPS, login, nested, starttls, ca, subject)


class AdapterContracts(SimpleTestCase):
    def setUp(self):
        self.wire = Wire()
        self.adapter = LdapDirectory(secrets=Secrets())
        self.patch = patch('ldap.initialize', side_effect=self.wire.initialize)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.network = patch('socket.create_connection', side_effect=AssertionError('real LDAP network'))
        self.network.start()
        self.addCleanup(self.network.stop)

    def test_subject_is_immutable_and_dn_is_only_bind_target(self):
        account, = self.adapter.lookup(configuration(), 'person')
        self.assertEqual(account.subject, UID_A)
        self.assertEqual(account.bind_dn, DN)
        self.assertIn('entryUUID', self.wire.searches[0][3])
        self.wire.subjects = [UID_B]
        replacement, = self.adapter.lookup(configuration(), 'person')
        self.assertNotEqual(account.subject, replacement.subject)

    def test_absent_login_is_empty_not_unavailable(self):
        # Отсутствие -- ответ каталога; Router по нему переходит к следующему источнику.
        self.wire.login_entries = 0
        self.assertEqual(self.adapter.lookup(configuration(), 'person'), ())

    def test_missing_multivalued_and_duplicate_subjects_fail_closed(self):
        cases = [([], False, 1), ([UID_A, UID_B], False, 1), (['not-an-immutable-uuid'], False, 1),
                 ([UID_A], True, 1), ([UID_A], False, 2), ([UID_A], False, 3)]
        for subjects, duplicate, entries in cases:
            with self.subTest(subjects=subjects, duplicate=duplicate, entries=entries):
                self.wire.subjects, self.wire.duplicates = subjects, duplicate
                self.wire.login_entries = entries
                with self.assertRaises(DirectoryUnavailable):
                    self.adapter.lookup(configuration(), 'person')

    def test_ad_guid_uses_little_endian_wire_uuid(self):
        cfg = configuration(nested=True, subject='objectGUID')
        self.wire.attribute = 'objectGUID'
        # Третий GUID целиком из ASCII: библиотека декодирует валидный UTF-8
        # в str, и subject обязан остаться тем же UUID из тех же 16 байт.
        for text, wire_hex in ((UID_A, '33221100554477668899aabbccddeeff'),
                               (UID_B, '443322116655887799aabbccddeeff00'),
                               ('33323130-3534-3736-3839-616263646566', '30313233343536373839616263646566')):
            with self.subTest(subject=text):
                self.wire.subjects = [bytes.fromhex(wire_hex)]
                account, = self.adapter.lookup(cfg, 'person')
                self.assertEqual(account.subject, text)
                encoded = ''.join('\\' + wire_hex[i:i + 2] for i in range(0, 32, 2))
                self.assertIn('(objectGUID=' + encoded + ')', [s[2] for s in self.wire.searches])

    def test_nested_groups_are_resolved_transitively_only_when_configured(self):
        # Вложенность -- библиотечный групповой тип: родитель основной группы
        # виден только при nested_ad, прямые группы -- всегда.
        self.wire.groups = {PARENT: {('member', GROUP)}}
        nested, = self.adapter.lookup(configuration(nested=True), 'person')
        self.assertEqual(set(nested.groups), {GROUP, PARENT})
        flat, = self.adapter.lookup(configuration(), 'person')
        self.assertEqual(set(flat.groups), {GROUP})

    def test_flat_directory_maps_member_unique_member_and_member_uid(self):
        # Три членства -- как у прежнего фильтра; групп больше лимита поиска
        # пользователя: лимит 2 не режет поиск групп.
        self.wire.groups = {'cn=unique,dc=example': {('uniqueMember', DN)},
                            'cn=posix,dc=example': {('memberUid', 'person')},
                            'cn=second,dc=example': {('member', DN)},
                            'cn=third,dc=example': {('member', DN)}}
        account, = self.adapter.lookup(configuration(), 'person')
        self.assertEqual(set(account.groups), {GROUP, 'cn=unique,dc=example', 'cn=posix,dc=example',
                                               'cn=second,dc=example', 'cn=third,dc=example'})

    def test_search_failures_are_unavailable_not_absent_or_fewer_groups(self):
        # Поиск логина библиотеки глотает LDAPError и отвечает «не найден»;
        # для authoritative source разовый outage -- не отсутствие записи.
        for side in ('user', 'group'):
            with self.subTest(side=side):
                self.wire.user_failure = self.wire.group_failure = None
                self.wire.login_entries = 0 if side == 'user' else 1
                setattr(self.wire, side + '_failure', ldap.BUSY({'desc': 'private-context', 'info': 'test-password'}))
                with self.assertRaises(DirectoryUnavailable) as raised:
                    self.adapter.lookup(configuration(), 'person')
                self.assertNotIn('private-context', str(raised.exception))
                self.assertNotIn('test-password', str(raised.exception))

    def test_service_bind_rejection_is_unavailable(self):
        for code in (49, 51, 52, 53):
            with self.subTest(code=code):
                self.wire.service_code = code
                with self.assertRaises(DirectoryUnavailable) as raised:
                    self.adapter.lookup(configuration(), 'person')
                self.assertNotIn('private-context', str(raised.exception))

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
        # Пустой пароль -- unauthenticated bind по RFC 4513: в провод не идёт.
        self.wire.binds.clear()
        self.assertFalse(self.adapter.check_password(configuration(), DN, ''))
        self.assertEqual(self.wire.binds, [])

    def test_connection_cleanup_transport_failure_is_sanitized(self):
        self.adapter.lookup(configuration(), 'person')
        self.adapter.check_password(configuration(), DN, 'test-password')
        self.assertEqual(self.wire.unbound, len(self.wire.uris))
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
        uris = list(self.wire.uris)
        with self.assertRaises(DirectoryUnavailable):
            LdapDirectory().lookup(configuration(), 'person')
        self.assertEqual(self.wire.binds, [DN])
        self.assertEqual(self.wire.uris, uris)

    def test_invalid_configuration_is_refused_before_network(self):
        for cfg in (configuration(login='uid)(objectClass=*'), configuration(login='uid%(user)s'),
                    configuration(subject='cn'), configuration(url='ldap://directory.invalid'),
                    configuration(url='ldaps://reader:secret@directory.invalid'),
                    configuration(url='http://directory.invalid')):
            with self.subTest(cfg=cfg):
                with self.assertRaises(DirectoryUnavailable):
                    self.adapter.lookup(cfg, 'person')
        self.assertEqual(self.wire.uris, [])

    def test_standard_backend_wire_is_the_only_ldap_stack(self):
        # import ldap3 запрещён заглушкой модуля: если адаптер всё ещё на
        # собственном стеке, он падает, не доходя до провода python-ldap.
        with patch.dict(sys.modules, {'ldap3': None}):
            account, = self.adapter.lookup(configuration(), 'person')
            self.assertTrue(self.adapter.check_password(configuration(), account.bind_dn, 'test-password'))
        self.assertEqual(account.subject, UID_A)

    def test_orm_configuration_reaches_backend_per_call_without_static_copy(self):
        from mop.server.controller import settings as controller_settings
        self.assertFalse([name for name in vars(controller_settings) if name.startswith('AUTH_LDAP')])
        # Даже появившаяся статическая копия не читается: источник -- ORM.
        with override_settings(AUTH_LDAP_SERVER_URI='ldaps://static.invalid', AUTH_LDAP_MIRROR_GROUPS=True,
                               AUTH_LDAP_USER_SEARCH=None, AUTH_LDAP_BIND_PASSWORD='static-secret',
                               AUTH_LDAP_GLOBAL_OPTIONS={ldap.OPT_X_TLS_REQUIRE_CERT: ldap.OPT_X_TLS_NEVER}):
            self.adapter.lookup(configuration(url='ldaps://first.invalid'), 'person')
            self.adapter.lookup(configuration(url='ldaps://second.invalid'), 'person')
        self.assertEqual(self.wire.uris, ['ldaps://first.invalid', 'ldaps://second.invalid'])

    def test_backend_settings_never_mirror_groups_or_permissions(self):
        from mop.server.access.ldap import backend_settings
        with override_settings(AUTH_LDAP_MIRROR_GROUPS=True, AUTH_LDAP_FIND_GROUP_PERMS=True,
                               AUTH_LDAP_CACHE_TIMEOUT=3600, AUTH_LDAP_ALWAYS_UPDATE_USER=True):
            settings = backend_settings(configuration(), bind_password=b'service')
        self.assertIsNone(settings.MIRROR_GROUPS)
        self.assertIsNone(settings.MIRROR_GROUPS_EXCEPT)
        self.assertFalse(settings.FIND_GROUP_PERMS)
        self.assertFalse(settings.AUTHORIZE_ALL_USERS)
        self.assertFalse(settings.ALWAYS_UPDATE_USER)
        self.assertTrue(settings.NO_NEW_USERS)
        self.assertEqual(settings.CACHE_TIMEOUT, 0)
        self.assertIsNone(settings.REQUIRE_GROUP)
        self.assertIsNone(settings.DENY_GROUP)
        self.assertEqual(settings.USER_FLAGS_BY_GROUP, {})
        self.assertEqual(settings.USER_ATTR_MAP, {})
        self.assertIsNone(settings.USER_DN_TEMPLATE)
        self.assertEqual(settings.SERVER_URI, 'ldaps://directory.invalid')
        self.assertEqual((settings.USER_SEARCH.base_dn, settings.USER_SEARCH.scope), (USERS, ldap.SCOPE_SUBTREE))
        self.assertEqual((settings.GROUP_SEARCH.base_dn, settings.GROUP_SEARCH.scope), (GROUPS, ldap.SCOPE_SUBTREE))

    def test_tls_trust_and_search_limits_reach_the_wire(self):
        for cfg in (configuration(url='ldap://directory.invalid', starttls=True, ca='protected:ca'),
                    configuration(ca='protected:ca')):
            with self.subTest(url=cfg.url):
                self.wire.options.clear()
                self.wire.started_tls = 0
                self.adapter.lookup(cfg, 'person')
                self.assertEqual(self.wire.started_tls, 1 if cfg.starttls else 0)
                names = [option for option, _ in self.wire.options]
                self.assertIn((ldap.OPT_X_TLS_REQUIRE_CERT, ldap.OPT_X_TLS_DEMAND), self.wire.options)
                # NEWCTX последним: иначе libldap не применяет TLS-опции соединения.
                self.assertGreater(names.index(ldap.OPT_X_TLS_NEWCTX), names.index(ldap.OPT_X_TLS_CACERTFILE))
                self.assertGreater(names.index(ldap.OPT_X_TLS_NEWCTX), names.index(ldap.OPT_X_TLS_REQUIRE_CERT))
                self.assertEqual(self.wire.ca_contents[-1], CA_PEM.decode('ascii'))
                self.assertFalse(os.path.exists(self.wire.ca_files[-1]))
                self.assertIn((ldap.OPT_SIZELIMIT, 2), self.wire.options)
                self.assertIn((ldap.OPT_SIZELIMIT, 500), self.wire.options)
                self.assertIn((ldap.OPT_REFERRALS, 0), self.wire.options)


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
        p = patch('ldap.initialize', side_effect=self.wire.initialize)
        p.start()
        self.addCleanup(p.stop)
        p = patch('socket.create_connection', side_effect=AssertionError('real LDAP network'))
        p.start()
        self.addCleanup(p.stop)

    def login(self, username='person'):
        return self.router.authenticate(None, username=username, password='test-password')

    # HYPOTHESIS: app.codename — логическое право Django, не уникальная строка
    # Permission; ровно один канонический ContentType не даёт неоднозначности.
    # RED: PG17 — MultipleObjectsReturned и сырой DoesNotExist до исправления.
    # SOLUTION: один канонический владелец UsageSource (#685), отсутствие строки
    # даёт DirectoryUnavailable; дублей регистрации больше нет.
    # RESULT: PG16/17 — по 13/13 LDAP checks green, локальные права не затронуты.
    # STATUS: FIXED — see #670; канонический владелец один с #685.
    def test_duplicate_permission_codenames_preserve_authority_and_source_revocation(self):
        from django.contrib.auth.models import Permission
        choices = Permission.objects.filter(content_type__app_label='execution', codename='view_usage')
        self.assertEqual(choices.count(), 1)
        self.assertEqual(choices.get().content_type.model, 'usagesource')
        rule = self.m.DirectoryGroupRule.objects.get(source=self.source, group_dn=GROUP)
        rule.permissions.add(choices.get())
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

    # BUG (#693): группы теперь ищут групповые типы библиотеки; проекция обязана
    # остаться общими Group правил #685 (участие add/remove), без MIRROR_GROUPS,
    # и не трогать ручные права и права других источников.
    def test_nested_rule_projects_shared_group_and_keeps_manual_rights(self):
        from django.contrib.auth.models import Group, Permission
        self.m.DirectoryConfiguration.objects.filter(source=self.source).update(nested_ad=True)
        view = Permission.objects.get(content_type__app_label='catalog', codename='view_project')
        usage = Permission.objects.get(content_type__app_label='execution', codename='view_usage')
        rule = self.m.DirectoryGroupRule.objects.create(source=self.source, group_dn=PARENT, all_projects=True)
        rule.permissions.add(usage)
        self.wire.groups = {PARENT: {('member', GROUP)}}
        groups = Group.objects.count()
        user = self.login()
        rule.refresh_from_db()
        self.assertTrue(user.groups.filter(pk=rule.group_id).exists())
        self.assertTrue(get_user_model().objects.get(pk=user.pk).has_perm('execution.view_usage'))
        self.assertTrue(self.m.AllProjectMembership.objects.filter(user=user, source=self.source).exists())
        manual = Group.objects.create(name='manual')
        user.groups.add(manual)
        user.user_permissions.add(view)
        local = self.m.IdentitySource.objects.create(name='local', kind='local')
        other = self.m.ProjectMembership.objects.create(user=user, source=local, project=self.project)
        self.wire.subjects, self.wire.dn = [UID_B], 'uid=other,dc=example'
        second = self.login('other')
        self.assertNotEqual(second.pk, user.pk)
        self.assertTrue(second.groups.filter(pk=rule.group_id).exists())
        # Две Group правил и одна ручная: контейнеров на пользователя нет.
        self.assertEqual(Group.objects.count(), groups + 3)
        self.wire.subjects, self.wire.dn, self.wire.groups = [UID_A], DN, {}
        self.assertEqual(self.login().pk, user.pk)
        current = get_user_model().objects.get(pk=user.pk)
        self.assertFalse(current.groups.filter(pk=rule.group_id).exists())
        self.assertFalse(current.has_perm('execution.view_usage'))
        self.assertFalse(self.m.AllProjectMembership.objects.filter(user=user, source=self.source).exists())
        self.assertTrue(current.groups.filter(pk=manual.pk).exists())
        self.assertTrue(current.user_permissions.filter(pk=view.pk).exists())
        self.assertTrue(self.m.ProjectMembership.objects.filter(pk=other.pk, user=user).exists())
        self.assertTrue(self.m.ProjectMembership.objects.filter(user=user, source=self.source,
                                                               project=self.project).exists())
        self.assertTrue(second.groups.filter(pk=rule.group_id).exists())
        self.assertTrue(Group.objects.get(pk=rule.group_id).permissions.filter(pk=usage.pk).exists())
