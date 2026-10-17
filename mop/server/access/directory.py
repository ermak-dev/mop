"""Authoritative Django backend: один знающий source, отдельный LDAP projection."""
from dataclasses import dataclass
from typing import Protocol
from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend
from django.contrib.auth.models import Group, Permission
from django.core.exceptions import PermissionDenied
from django.db import connection, transaction
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables
from . import models as m
from .permissions import ACTIONS
from .policy import directory_dn_key as _norm
from .services import Denied, _identity


class DirectoryUnavailable(PermissionDenied):
    """Безопасная причина отказа port; Django не пробует следующий backend."""


@dataclass(frozen=True)
class DirectoryAccount:
    subject: str
    username: str
    groups: tuple[str, ...]
    active: bool = True
    bind_dn: str = ''


@dataclass(frozen=True)
class DirectoryResult:
    subject: str
    username: str
    groups: tuple[str, ...]
    project_ids: tuple
    all_projects: bool
    permissions: tuple[str, ...]
    allowed: bool = True


@dataclass(frozen=True)
class Configuration:
    source_id: object
    revision: int
    url: str
    bind_dn: str
    bind_secret_ref: str
    user_base: str
    group_base: str
    login_attribute: str
    nested_ad: bool
    starttls: bool
    ca_secret_ref: str
    subject_attribute: str = 'entryUUID'


class DirectoryProvider(Protocol):
    def lookup(self, configuration: Configuration, username: str) -> tuple[DirectoryAccount, ...]: ...
    def check_password(self, configuration: Configuration, subject: str, password: str) -> bool: ...


class UnboundDirectory:
    def lookup(self, configuration, username):
        raise DirectoryUnavailable('directory bind secret resolver is not bound')


def _configuration(source, *, lock=False):
    rows = m.DirectoryConfiguration.objects
    if lock:
        rows = rows.select_for_update()
    row = rows.filter(source=source).first()
    if row is None:
        raise DirectoryUnavailable('directory configuration is unavailable')
    return Configuration(source.pk, source.revision, row.url, row.bind_dn, row.bind_secret_ref,
        row.user_base, row.group_base or row.user_base, row.login_attribute,
        row.nested_ad, row.starttls, row.ca_secret_ref, row.subject_attribute)


def _mapping(source, account):
    groups = {_norm(dn) for dn in account.groups}
    rules = tuple(m.DirectoryGroupRule.objects.filter(source=source).prefetch_related('permissions__content_type'))
    matched = tuple(rule for rule in rules if _norm(rule.group_dn) in groups)
    return DirectoryResult(account.subject, account.username, tuple(account.groups),
        tuple(sorted({rule.project_id for rule in matched if rule.project_id is not None}, key=str)),
        any(rule.all_projects for rule in matched), tuple(sorted({
            p.content_type.app_label + '.' + p.codename for rule in matched for p in rule.permissions.all()})),
        bool(matched) and account.active)


def _revoke_owned(user, source):
    """Source-owned revoke: снимает только membership/scope этого источника.

    Права общей группы правила и участия других пользователей не трогаем;
    ручные группы, прямые права и права других источников остаются.
    """
    m.ProjectMembership.objects.filter(user=user, source=source).delete()
    m.AllProjectMembership.objects.filter(user=user, source=source).delete()
    for rule in m.DirectoryGroupRule.objects.select_related('group').filter(source=source, group__user=user):
        user.groups.remove(rule.group)


def _rule_group(rule):
    """Одна общая Group на правило; имя устойчиво и не зависит от пользователя."""
    if rule.group_id is None:
        # Уникальность даёт pk правила: DN длиннее 64 символов мог бы
        # столкнуться после усечения имени.
        group = Group.objects.create(name='directory-' + str(rule.pk))
        m.DirectoryGroupRule.objects.filter(pk=rule.pk).update(group=group)
        rule.group = group
    return rule.group


def apply_directory(source_id, revision, result, *, configuration=None, expected_user_id=None):
    """Только результат trusted mapping, не JSON. CAS после внешнего I/O."""
    if type(result) is not DirectoryResult or not set(result.permissions) <= ACTIONS:
        raise ValueError('invalid directory projection')
    with transaction.atomic():
        source = m.IdentitySource.objects.select_for_update().get(pk=source_id)
        if not source.enabled or source.kind != 'ldap' or source.revision != revision:
            raise DirectoryUnavailable('directory policy changed during validation')
        identity = m.LoginIdentity.objects.filter(source=source, external_subject=result.subject).first()
        if configuration is not None:
            if _configuration(source, lock=True) != configuration:
                raise DirectoryUnavailable('directory configuration changed during validation')
            # Найденный под другим именем subject не присваивает существующий User.
            # Bound login также не создаёт replacement, если связь исчезла за I/O.
            if (identity is not None and identity.user_id != expected_user_id
                    or identity is None and expected_user_id is not None):
                raise PermissionDenied('directory identity belongs to another login')
        if identity is None:
            if not result.allowed:
                raise PermissionDenied('directory account is not authorized')
            # Существующий User не становится LDAP identity по совпадению имени.
            if get_user_model().objects.filter(username=result.username).exists():
                raise PermissionDenied('username belongs to another identity')
            user = get_user_model().objects.create_user(result.username, password=None)
            identity = m.LoginIdentity.objects.create(user=user, source=source, external_subject=result.subject)
        user = get_user_model().objects.select_for_update().get(pk=identity.user_id)
        # Вид Actor читается напрямую; human без привязки узла (#684).
        if user.kind not in ('human', 'agent'):
            raise PermissionDenied('directory account is not a human principal')
        if user.username != result.username:
            if get_user_model().objects.exclude(pk=user.pk).filter(username=result.username).exists():
                raise PermissionDenied('username belongs to another identity')
            user.username = result.username
        if user.has_usable_password():
            user.set_unusable_password()
        user.save(update_fields=['username', 'password'])
        _revoke_owned(user, source)
        identity.authorized = result.allowed
        identity.validated_at, identity.validated_revision = timezone.now(), source.revision
        identity.save(update_fields=['authorized', 'validated_at', 'validated_revision'])
        if result.allowed:
            from ..catalog.models import Project
            ids = set(result.project_ids)
            if Project.objects.filter(pk__in=ids).count() != len(ids):
                raise ValueError('directory mapping references an unknown project')
            # Каталог права мог потерять строку; проекция отказывает целиком,
            # а не принимает пользователя с усечённым набором (#670).
            for action in result.permissions:
                app, code = action.split('.')
                if not Permission.objects.filter(content_type__app_label=app, codename=code).exists():
                    raise DirectoryUnavailable('directory permission catalogue is unavailable')
            for project_id in ids:
                m.ProjectMembership.objects.create(user=user, source=source, project_id=project_id)
            if result.all_projects:
                m.AllProjectMembership.objects.create(user=user, source=source)
            # Проекция права — общая Group правила, не контейнер на пользователя:
            # участие только add/remove, права группы зеркалит каталог правила.
            for rule in m.DirectoryGroupRule.objects.filter(source=source).prefetch_related('permissions'):
                if _norm(rule.group_dn) in {_norm(dn) for dn in result.groups}:
                    group = _rule_group(rule)
                    rule_permissions = set(rule.permissions.all())
                    if rule_permissions != set(group.permissions.all()):
                        group.permissions.set(rule_permissions)
                    user.groups.add(group)
        m.CredentialEvent.objects.create(user=user, action='directory_validated' if result.allowed else 'directory_denied')
        return user.pk


def _deny_bound(identity, revision):
    apply_directory(identity.source_id, revision, DirectoryResult(identity.external_subject,
        identity.user.username, (), (), False, (), allowed=False))


class Router(ModelBackend):
    """ModelBackend сохраняет обычные Django permissions; auth routing иной."""
    def __init__(self, *, provider: DirectoryProvider | None = None):
        self.provider = provider if provider is not None else UnboundDirectory()

    @sensitive_variables('password')
    def authenticate(self, request, username=None, password=None, **kwargs):
        if not username or not password:
            return None
        bound = m.LoginIdentity.objects.select_related('source', 'user').filter(user__username=username).first()
        sources = (bound.source,) if bound else tuple(m.IdentitySource.objects.filter(enabled=True).order_by('priority', 'id'))
        for source in sources:
            if not source.enabled:
                raise PermissionDenied('identity source is disabled')
            if source.kind == 'local':
                if bound is None or bound.source_id != source.pk:
                    continue
                user = bound.user
                try:
                    _identity(user)
                except Denied as error:
                    raise PermissionDenied(str(error)) from None
                if not user.check_password(password):
                    raise PermissionDenied('authentication refused')
                return user
            if source.kind != 'ldap':
                raise PermissionDenied('unknown identity source')
            if connection.in_atomic_block or not connection.get_autocommit():
                raise DirectoryUnavailable('directory I/O cannot run inside SQL transaction')
            configuration = _configuration(source)
            accounts = tuple(self.provider.lookup(configuration, username))
            if not accounts and bound is None:
                continue
            if len(accounts) != 1:
                if bound:
                    _deny_bound(bound, source.revision)
                raise PermissionDenied('directory login is absent or ambiguous')
            account = accounts[0]
            if bound and account.subject != bound.external_subject:
                _deny_bound(bound, source.revision)
                raise PermissionDenied('directory identity changed')
            result = _mapping(source, account)
            if not result.allowed:
                apply_directory(source.pk, source.revision, result, configuration=configuration,
                    expected_user_id=bound.user_id if bound else None)
                raise PermissionDenied('directory account is not authorized')
            if not self.provider.check_password(configuration, account.bind_dn or account.subject, password):
                raise PermissionDenied('authentication refused')
            user_id = apply_directory(source.pk, source.revision, result, configuration=configuration,
                expected_user_id=bound.user_id if bound else None)
            user = get_user_model().objects.get(pk=user_id)
            if not user.is_active:
                raise PermissionDenied('identity is inactive')
            return user
        # Как у стандартного backend: неизвестный логин всё равно хеширует пароль.
        get_user_model()().set_password(password)
        return None

    def get_user(self, user_id):
        user = get_user_model().objects.filter(pk=user_id).first()
        if user is None:
            return None
        try:
            _identity(user)
        except Denied:
            return None
        return user if user.kind == 'human' else None
