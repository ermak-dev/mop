"""Тонкий адаптер каталога поверх django-auth-ldap (#693).

Механику -- соединение, STARTTLS и проверку сертификата, bind, поиск
пользователя и групп, вложенность AD -- исполняет backend Django Auth и его
групповые типы. Здесь только три вещи:

- ORM Configuration -> объект настроек backend на один вызов. Router отдаёт
  снимок конфигурации и сверяет его под CAS после I/O (revision-aware); каждое
  имя настроек выставлено явно, поэтому AUTH_LDAP_* из django.conf не читаются
  даже если появятся: второй статической копии нет.
- Секреты -- через существующий port (BindSecrets); служебный пароль --
  первым, до любой сети (#637), и нигде не хранится.
- Прикладные инварианты: immutable subject, неоднозначный логин, fail-closed
  для всего, кроме invalidCredentials (49) на bind самого пользователя.

Backend не стоит в AUTHENTICATION_BACKENDS и не трогает ORM пользователей:
решение и проекция -- Router (directory.py), группы -- общие Group правил #685
(участие add/remove). MIRROR_GROUPS/FIND_GROUP_PERMS выключены: зеркалирование
библиотеки делает groups.set по имени cn и снесло бы ручные группы.
"""
from contextlib import ExitStack, contextmanager
import re
from tempfile import NamedTemporaryFile
from typing import Protocol
from urllib.parse import urlsplit
from uuid import UUID

import ldap
import ldap.filter
from django.views.decorators.debug import sensitive_variables
from django_auth_ldap.backend import LDAPBackend, _LDAPUser, ldap_error
from django_auth_ldap.config import (GroupOfNamesType, GroupOfUniqueNamesType, LDAPGroupType,
    LDAPSearch, LDAPSettings, NestedActiveDirectoryGroupType, PosixGroupType)

from .directory import DirectoryAccount, DirectoryUnavailable

# Сбои провода: python-ldap бросает LDAPError, сокет -- OSError, битые
# значения -- UnicodeError/ValueError. Ни один из них не «неверный пароль».
WIRE_ERRORS = (ldap.LDAPError, OSError, UnicodeError, ValueError)
TIMEOUT = 3
# Поиск логина и subject: больше двух записей -- sizeLimitExceeded, отказ.
USER_LIMIT = 2
GROUP_LIMIT = 500


class BindSecrets(Protocol):
    def bind_password(self, source_id, reference: str) -> bytes: ...
    def ca_pem(self, source_id, reference: str) -> bytes: ...


class UnboundSecrets:
    def bind_password(self, source_id, reference):
        raise DirectoryUnavailable('directory bind secret resolver is not bound')

    def ca_pem(self, source_id, reference):
        raise DirectoryUnavailable('directory trust resolver is not bound')


def _reason(error):
    """Безопасная причина: имя result code python-ldap, без desc/info сервера."""
    return type(error).__name__ if isinstance(error, ldap.LDAPError) else 'transport'


def _check(cfg):
    parsed = urlsplit(cfg.url)
    if (parsed.scheme not in ('ldap', 'ldaps') or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment or parsed.path not in ('', '/')
            or (parsed.scheme == 'ldap' and not cfg.starttls)
            # Атрибут логина входит в шаблон фильтра библиотеки как есть.
            or not re.fullmatch('[A-Za-z][A-Za-z0-9-]*', cfg.login_attribute)
            or cfg.subject_attribute not in ('entryUUID', 'objectGUID')):
        raise DirectoryUnavailable('invalid secure directory configuration')


class _Groups(LDAPGroupType):
    """Объединение групповых типов библиотеки по DN группы.

    Фильтры строят сами типы; это прежнее (|(member)(uniqueMember)(memberUid)),
    где member -- прямой или вложенный AD. Сбой любого поиска поднимается:
    меньше групп из-за outage -- не ответ каталога.
    """

    def __init__(self, *kinds):
        super().__init__(name_attr='cn')
        self.kinds = kinds

    def user_groups(self, ldap_user, group_search):
        found = {}
        for kind in self.kinds:
            found.update((info[0], info) for info in kind.user_groups(ldap_user, group_search))
        return list(found.values())


@sensitive_variables('bind_password', 'values')
def backend_settings(cfg, *, bind_password=None, ca_file=None):
    """ORM Configuration -> LDAPSettings одного вызова, все имена явно."""
    _check(cfg)
    options = {ldap.OPT_PROTOCOL_VERSION: ldap.VERSION3, ldap.OPT_REFERRALS: 0,
               ldap.OPT_NETWORK_TIMEOUT: TIMEOUT, ldap.OPT_TIMEOUT: TIMEOUT,
               ldap.OPT_SIZELIMIT: USER_LIMIT,
               ldap.OPT_X_TLS_REQUIRE_CERT: ldap.OPT_X_TLS_DEMAND}
    if ca_file:
        options[ldap.OPT_X_TLS_CACERTFILE] = ca_file
    # Последним: libldap собирает TLS-контекст соединения из опций выше.
    options[ldap.OPT_X_TLS_NEWCTX] = 0
    attributes = list(dict.fromkeys((cfg.login_attribute, cfg.subject_attribute, 'uid')))
    member = NestedActiveDirectoryGroupType() if cfg.nested_ad else GroupOfNamesType()
    values = dict(LDAPSettings.defaults,
        SERVER_URI=cfg.url, START_TLS=cfg.starttls, CONNECTION_OPTIONS=options,
        BIND_DN=cfg.bind_dn, BIND_PASSWORD=bind_password.decode('utf-8') if bind_password else '',
        USER_SEARCH=LDAPSearch(cfg.user_base, ldap.SCOPE_SUBTREE,
                               '(' + cfg.login_attribute + '=%(user)s)', attributes),
        USER_ATTRLIST=attributes,
        GROUP_SEARCH=LDAPSearch(cfg.group_base, ldap.SCOPE_SUBTREE, '(objectClass=*)', ['cn']),
        GROUP_TYPE=_Groups(member, GroupOfUniqueNamesType(), PosixGroupType()),
        NO_NEW_USERS=True, ALWAYS_UPDATE_USER=False, CACHE_TIMEOUT=0, PERMIT_EMPTY_PASSWORD=False)
    settings = LDAPSettings(defaults=values)
    for name, value in values.items():
        setattr(settings, name, value)
    return settings


class _Wire:
    """Модуль python-ldap для backend: учитывает открытые соединения для unbind."""

    def __init__(self, connections):
        self._connections = connections

    def __getattr__(self, name):
        return getattr(ldap, name)

    def initialize(self, uri, **kwargs):
        connection = ldap.initialize(uri, **kwargs)
        self._connections.append(connection)
        return connection


class _Backend(LDAPBackend):
    """LDAPBackend одного вызова: настройки -- из ORM, глобальные опции
    AUTH_LDAP_GLOBAL_OPTIONS не применяются, соединения закрываются явно."""

    def __init__(self, settings):
        self.settings = settings
        self.connections = []
        self._ldap = _Wire(self.connections)

    @property
    def ldap(self):
        return self._ldap

    def close(self):
        failed = False
        for connection in self.connections:
            try:
                connection.unbind_s()
            except WIRE_ERRORS:
                failed = True
        self.connections.clear()
        if failed:
            raise DirectoryUnavailable('directory transport cleanup failed')


def _fail_closed(sender, exception=None, **kwargs):
    """Поиск логина библиотеки глотает LDAPError и отвечает «не найден»;
    для authoritative source сбой -- не отсутствие (ldap_error пропускает исключение)."""
    raise DirectoryUnavailable('directory search is unavailable: ' + _reason(exception)) from None


ldap_error.connect(_fail_closed, sender=_Backend, dispatch_uid='mop.access.ldap.fail-closed')


def _subject(cfg, attrs):
    values = (attrs or {}).get(cfg.subject_attribute)
    if not isinstance(values, (list, tuple)) or len(values) != 1:
        raise DirectoryUnavailable('directory immutable subject is missing or ambiguous')
    value = values[0]
    try:
        # Библиотека декодирует валидный UTF-8 в str: encode возвращает те же байты.
        raw = value.encode('utf-8') if isinstance(value, str) else value
        if not isinstance(raw, bytes):
            raise TypeError
        # AD хранит первые три компонента GUID в little-endian порядке.
        if cfg.subject_attribute == 'objectGUID' and len(raw) == 16:
            return str(UUID(bytes_le=raw))
        return str(UUID(raw.decode('ascii')))
    except (ValueError, UnicodeError, TypeError):
        raise DirectoryUnavailable('directory immutable subject is invalid') from None


def _assertion(cfg, subject):
    if cfg.subject_attribute == 'objectGUID':
        return ''.join('\\' + format(byte, '02x') for byte in UUID(subject).bytes_le)
    return ldap.filter.escape_filter_chars(subject)


class LdapDirectory:
    def __init__(self, *, secrets: BindSecrets | None = None):
        self.secrets = secrets if secrets is not None else UnboundSecrets()

    @contextmanager
    def _backend(self, cfg, *, service):
        _check(cfg)
        password = None
        if service:
            # Секрет запрашивается первым: без #637 не происходит даже network connect.
            password = self.secrets.bind_password(cfg.source_id, cfg.bind_secret_ref)
            if not password:
                raise DirectoryUnavailable('directory bind credential is unavailable')
        with ExitStack() as stack:
            ca_file = None
            if cfg.ca_secret_ref:
                pem = self.secrets.ca_pem(cfg.source_id, cfg.ca_secret_ref)
                if not pem:
                    raise DirectoryUnavailable('directory trust is unavailable')
                # python-ldap принимает доверие только файлом: временный 0600
                # живёт ровно один вызов.
                trust = stack.enter_context(NamedTemporaryFile(suffix='.pem'))
                trust.write(pem)
                trust.flush()
                ca_file = trust.name
            try:
                backend = _Backend(backend_settings(cfg, bind_password=password, ca_file=ca_file))
            except UnicodeError:
                raise DirectoryUnavailable('directory bind credential is invalid') from None
            try:
                yield backend
            except WIRE_ERRORS as error:
                raise DirectoryUnavailable('directory transport or trust is unavailable: '
                                           + _reason(error)) from None
            finally:
                backend.close()

    def lookup(self, cfg, username):
        with self._backend(cfg, service=True) as backend:
            user = _LDAPUser(backend, username=username)
            # Служебный bind и USER_SEARCH библиотеки; сбой -- _fail_closed.
            dn = user.dn
            if dn is None:
                # Библиотека не различает «нет» и «больше одной»: переспрашиваем.
                if backend.settings.USER_SEARCH.execute(user.connection, {'user': username}):
                    raise DirectoryUnavailable('directory login is ambiguous')
                return ()
            subject = _subject(cfg, user.attrs)
            # Одинаковый immutable id у разных DN -- коллизия, не rename.
            unique = LDAPSearch(cfg.user_base, ldap.SCOPE_SUBTREE,
                '(' + cfg.subject_attribute + '=' + _assertion(cfg, subject) + ')',
                backend.settings.USER_ATTRLIST)
            matches = unique.execute(user.connection)
            if len(matches) != 1 or matches[0][0] != dn or _subject(cfg, matches[0][1]) != subject:
                raise DirectoryUnavailable('directory immutable subject is ambiguous')
            user.connection.set_option(ldap.OPT_SIZELIMIT, GROUP_LIMIT)
            groups = tuple(sorted(user.group_dns))
        return (DirectoryAccount(subject, username, groups, bind_dn=dn),)

    @sensitive_variables('password')
    def check_password(self, cfg, subject, password):
        # Пустой пароль -- unauthenticated bind (RFC 4513), в провод не идёт.
        if not password:
            return False
        with self._backend(cfg, service=False) as backend:
            user = _LDAPUser(backend, username=subject)
            # DN найден lookup: шаг bind библиотеки без повторного поиска.
            # Только invalidCredentials (49) -- AuthenticationFailed; остальное
            # (busy/unavailable/unwillingToPerform) поднимается как outage.
            user._user_dn = subject
            try:
                user._authenticate_user_dn(password)
            except _LDAPUser.AuthenticationFailed:
                return False
            return True
