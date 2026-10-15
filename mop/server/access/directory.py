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
from .services import Denied, _principal


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


class DirectoryProvider(Protocol):
    def lookup(self, configuration: Configuration, username: str) -> tuple[DirectoryAccount, ...]: ...
    def check_password(self, configuration: Configuration, subject: str, password: str) -> bool: ...


class UnboundDirectory:
    def lookup(self, configuration, username):
        raise DirectoryUnavailable('directory bind secret resolver is not bound')


def _configuration(source):
    row = m.DirectoryConfiguration.objects.filter(source=source).first()
    if row is None:
        raise DirectoryUnavailable('directory configuration is unavailable')
    return Configuration(source.pk, source.revision, row.url, row.bind_dn, row.bind_secret_ref,
        row.user_base, row.group_base or row.user_base, row.login_attribute,
        row.nested_ad, row.starttls, row.ca_secret_ref)


from .policy import directory_dn_key as _norm

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
    m.ProjectMembership.objects.filter(user=user, source=source).delete()
    m.AllProjectMembership.objects.filter(user=user, source=source).delete()
    ownership = m.DirectoryGroupOwnership.objects.select_related('group').filter(user=user, source=source).first()
    if ownership:
        ownership.group.permissions.clear()
        user.groups.remove(ownership.group)


def apply_directory(source_id, revision, result):
    """Только результат trusted mapping, не JSON. CAS после внешнего I/O."""
    if type(result) is not DirectoryResult or not set(result.permissions) <= ACTIONS:
        raise ValueError('invalid directory projection')
    with transaction.atomic():
        source = m.IdentitySource.objects.select_for_update().get(pk=source_id)
        if not source.enabled or source.kind != 'ldap' or source.revision != revision:
            raise DirectoryUnavailable('directory policy changed during validation')
        identity = m.LoginIdentity.objects.filter(source=source, external_subject=result.subject).first()
        if identity is None:
            if not result.allowed:
                raise PermissionDenied('directory account is not authorized')
            # Существующий User не становится LDAP identity по совпадению имени.
            if get_user_model().objects.filter(username=result.username).exists():
                raise PermissionDenied('username belongs to another identity')
            user = get_user_model().objects.create_user(result.username, password=None)
            m.Principal.objects.create(user=user, kind='human')
            identity = m.LoginIdentity.objects.create(user=user, source=source, external_subject=result.subject)
        user = get_user_model().objects.select_for_update().get(pk=identity.user_id)
        principal = m.Principal.objects.filter(user=user, kind='human', bound_node__isnull=True).first()
        if principal is None:
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
            for project_id in ids:
                m.ProjectMembership.objects.create(user=user, source=source, project_id=project_id)
            if result.all_projects:
                m.AllProjectMembership.objects.create(user=user, source=source)
            ownership = m.DirectoryGroupOwnership.objects.select_related('group').filter(user=user, source=source).first()
            if ownership is None:
                group = Group.objects.create(name='directory-' + source.pk.hex + '-' + str(user.pk))
                ownership = m.DirectoryGroupOwnership.objects.create(user=user, source=source, group=group)
            permissions = []
            for action in result.permissions:
                app, code = action.split('.')
                permissions.append(Permission.objects.get(content_type__app_label=app, codename=code))
            ownership.group.permissions.set(permissions)
            user.groups.add(ownership.group)
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
                    _principal(user)
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
                apply_directory(source.pk, source.revision, result)
                raise PermissionDenied('directory account is not authorized')
            if not self.provider.check_password(configuration, account.bind_dn or account.subject, password):
                raise PermissionDenied('authentication refused')
            user_id = apply_directory(source.pk, source.revision, result)
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
            principal, _ = _principal(user)
        except Denied:
            return None
        return user if principal.kind == 'human' else None
