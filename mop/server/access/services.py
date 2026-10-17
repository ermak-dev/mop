"""Live credential и единый action/scope evaluator; HTTP сюда не проходит."""
from datetime import timedelta
import re
from django.conf import settings
from django.contrib.auth import get_user_model, SESSION_KEY, BACKEND_SESSION_KEY, HASH_SESSION_KEY
from django.contrib.auth.models import Permission
from django.contrib.sessions.models import Session
from django.db import transaction
from django.utils import timezone
from django.utils.crypto import constant_time_compare, salted_hmac
from django.views.decorators.debug import sensitive_variables
from rest_framework.authtoken.models import Token
from ..catalog import models as catalog
from ..catalog.services import Catalog
from . import models as m
from .context import ActorContext, ActorKind
from .permissions import ACTIONS, GLOBAL_ACTIONS, MACHINE_ACTIONS, permission_names


class Denied(PermissionError):
    """Нет текущей authority; сообщение уже безопасно для boundary."""


class NotVisible(Denied):
    """Отсутствующая/чужая ссылка имеет один 404."""


def _identity(user, node_id=None):
    """Живой Actor по его виду; вид не выбирает систему проверок (#684).

    Человеку и агенту нужна живая source identity, сервису — привязка
    credential к текущему узлу без пароля/staff. Машина делегирования —
    сервисный Actor: его проверяет резолвер делегирования, не токен-парсер.
    """
    if not user.is_active:
        raise Denied('identity is inactive')
    if user.kind == 'service':
        if user.has_usable_password() or user.is_staff or user.is_superuser:
            raise Denied('invalid service identity')
        if node_id is None:
            # Машина делегирования не привязана к узлу: её закрытый жизненный
            # цикл проверяет резолвер делегирования, не enrollment fence.
            if not _delegated(user.pk):
                raise Denied('invalid service identity')
        elif not Catalog.node_is_current(node_id):
            raise Denied('service node is unavailable')
        return None
    if user.kind not in ('human', 'agent'):
        raise Denied('invalid identity kind')
    identity = m.LoginIdentity.objects.select_related('source').filter(user=user).first()
    if identity is None or not identity.source.enabled or not identity.authorized:
        raise Denied('identity source is unavailable')
    source = identity.source
    if source.kind == 'ldap':
        if (user.has_usable_password() or identity.validated_revision != source.revision
                or identity.validated_at is None
                or not timezone.now() - timedelta(seconds=source.freshness_seconds) <= identity.validated_at <= timezone.now()):
            raise Denied('directory identity requires current validation')
    elif source.kind != 'local':
        raise Denied('unknown identity source')
    return identity


def _binding_values(user, identity, node_id):
    """Только proven metadata, которых нет в стандартных Django credentials.

    Узел enrollment, ссылки identity и subject-change HMAC. Атрибуты Actor
    (kind) не дублируются: credential не становится второй identity, вид
    всегда читается заново с живого Actor.
    """
    return dict(identity_id=identity.pk if identity else None,
        identity_source_id=identity.source_id if identity else None,
        identity_digest=salted_hmac('mop.access.identity-binding', ':'.join((str(identity.pk),
            str(identity.source_id), identity.source.kind, identity.external_subject)), algorithm='sha256').hexdigest() if identity else '',
        bound_node_id=node_id)


def _check_binding(binding, user):
    identity = _identity(user, binding.bound_node_id)
    if any(getattr(binding, key) != value for key, value in _binding_values(user, identity, binding.bound_node_id).items()):
        raise Denied('credential identity binding changed')


def _context(binding, user):
    """Контекст только из проверенного credential: факты, не права."""
    grant_id = m.Grant.objects.filter(actor=user).values_list('id', flat=True).first()
    return ActorContext(user.pk, ActorKind(user.kind), binding.pk,
        user.pk if user.kind in ('human', 'agent') else None, binding.bound_node_id, grant_id)


@sensitive_variables('key')
def _digest(key):
    return salted_hmac('mop.access.token-generation', key, algorithm='sha256').hexdigest()


def _session_user(session, user):
    if session is None or session.expire_date <= timezone.now():
        raise Denied('session expired or logged out')
    data = session.get_decoded()
    if (str(data.get(SESSION_KEY)) != str(user.pk)
            or data.get(BACKEND_SESSION_KEY) not in settings.AUTHENTICATION_BACKENDS
            or not constant_time_compare(data.get(HASH_SESSION_KEY, ''), user.get_session_auth_hash())):
        raise Denied('session authentication changed')


def _delegated(user_id):
    return m.Grant.objects.filter(actor_id=user_id).exists()


@sensitive_variables('key')
def token_context(key, *, grants=None):
    if not isinstance(key, str) or not re.fullmatch('[0-9a-f]{40}', key):
        raise Denied('token is unavailable')
    user_id = Token.objects.filter(key=key).values_list('user_id', flat=True).first()
    if user_id is None:
        raise Denied('token revoked or rotated')
    if _delegated(user_id):
        if grants is None:
            raise Denied('delegation resolver is not bound')
        # Проверенный resolver делает provider I/O вне SQL. Human/service path
        # остаётся штатным; сессия браузера не становится grant credential.
        return grants.token_context(key)
    with transaction.atomic():
        user = get_user_model().objects.select_for_update().get(pk=user_id)
        if not Token.objects.filter(user=user, key=key).exists():
            raise Denied('token revoked or rotated')
        binding = m.CredentialBinding.objects.filter(token_user=user).first()
        if binding is None:
            raise Denied('credential binding is unavailable')
        _check_binding(binding, user)
        if not constant_time_compare(binding.token_digest, _digest(key)):
            raise Denied('token generation changed')
        return _context(binding, user)


@sensitive_variables('session_key')
def session_context(session_key):
    session = Session.objects.filter(pk=session_key, expire_date__gt=timezone.now()).first()
    if session is None:
        raise Denied('session expired or logged out')
    user_id = session.get_decoded().get(SESSION_KEY)
    try:
        user_id = int(user_id)
    except (TypeError, ValueError):
        raise Denied('session authentication is unavailable') from None
    with transaction.atomic():
        user = get_user_model().objects.select_for_update().filter(pk=user_id).first()
        if user is None:
            raise Denied('session identity is unavailable')
        session = Session.objects.filter(pk=session_key).first()
        _session_user(session, user)
        binding = m.CredentialBinding.objects.filter(session=session).first()
        if binding is None:
            raise Denied('credential binding is unavailable')
        _check_binding(binding, user)
        return _context(binding, user)


@sensitive_variables('session_key')
def bind_session(user_id, session_key):
    """Trusted login hook: session и binding сохраняются в общей transaction boundary."""
    with transaction.atomic():
        user = get_user_model().objects.select_for_update().get(pk=user_id)
        if user.kind not in ('human', 'agent'):
            raise Denied('browser login requires a human identity')
        identity = _identity(user)
        session = Session.objects.filter(pk=session_key).first()
        _session_user(session, user)
        # Login создаёт новую session generation; существующий credential не чинится.
        m.CredentialBinding.objects.create(session=session, **_binding_values(user, identity, None))


def _resolve(actor):
    if type(actor) is not ActorContext:
        raise Denied('trusted actor context required')
    binding = m.CredentialBinding.objects.filter(pk=actor.credential_id).first()
    user = get_user_model().objects.filter(pk=actor.user_id).first()
    if binding is None or user is None:
        raise Denied('credential revoked')
    _check_binding(binding, user)
    if actor != _context(binding, user):
        raise Denied('actor does not match credential')
    if binding.token_user_id is not None:
        token = Token.objects.filter(user=user).first()
        if (binding.token_user_id != user.pk or token is None
                or not constant_time_compare(binding.token_digest, _digest(token.key))):
            raise Denied('token revoked or rotated')
    else:
        _session_user(Session.objects.filter(pk=binding.session_id).first(), user)
    return user


def current_actor(actor, *, grants=None):
    if type(actor) is ActorContext and actor.grant_id is not None:
        if grants is None:
            raise Denied('delegation resolver is not bound')
        return grants.resolve_actor(actor)
    _resolve(actor)
    return actor


def _global(user):
    return user.is_superuser or m.AllProjectMembership.objects.filter(user=user, source__enabled=True).exists()


def _require_permissions(user, action, reason):
    """Одна permission matrix для текущего actor и закреплённого human owner."""
    if action not in ACTIONS:
        raise Denied('action is not registered')
    for permission in permission_names(action):
        app, code = permission.split('.', 1)
        if (not Permission.objects.filter(content_type__app_label=app, codename=code).exists()
                or not user.has_perm(permission)):
            raise Denied(reason)


def visible_project_ids(actor):
    # Вид Actor не выбирает систему проверок (#685): проектная область для
    # человека, агента и сервиса — одна и та же membership-таблица.
    user = _resolve(actor)
    if _global(user):
        return Catalog.current_project_ids()
    memberships = m.ProjectMembership.objects.filter(user=user, source__enabled=True).values_list('project_id', flat=True)
    return Catalog.current_project_ids(memberships)


class Access:
    """Реализация Catalog.Authorizer; никаких callback из request/payload."""
    def __init__(self, *, grants=None):
        self.grants = grants

    def authorize_owner(self, owner_id, action, *, project_ids=()):
        """Локальная проверка текущего human owner для закреплённого server proof.

        Не создаёт ActorContext и не допускает команду от имени владельца.
        Вызывается только trusted прикладным слоем, не HTTP по owner JSON.
        """
        user = get_user_model().objects.filter(pk=owner_id).first()
        if user is None:
            raise Denied('human owner is unavailable')
        if user.kind != 'human':
            raise Denied('human owner required')
        identity = _identity(user)
        projects = set(project_ids)
        if projects != set(Catalog.current_project_ids(projects)):
            raise Denied('human owner project is unavailable')
        if not _global(user):
            allowed = set(m.ProjectMembership.objects.filter(user=user, source__enabled=True).values_list('project_id', flat=True))
            if not projects <= allowed:
                raise Denied('human owner no longer belongs to project')
        _require_permissions(user, action, 'current human owner permission required')

    def authorize(self, actor, action, *, project_ids=(), node_ids=(), client_ids=()):
        if type(actor) is ActorContext and actor.grant_id is not None:
            if self.grants is None:
                raise Denied('delegation resolver is not bound')
            return self.grants.authorize(actor, action, project_ids=project_ids,
                                         node_ids=node_ids, client_ids=client_ids)
        # Один pipeline для human/agent/service (#685): неизвестное действие
        # отказывает одинаково до всяких видовых веток.
        if action not in ACTIONS:
            raise Denied('action is not registered')
        user = _resolve(actor)
        projects, nodes, clients = set(project_ids), set(node_ids), set(client_ids)
        if user.kind == 'service':
            # Узловой enrollment — именованное доменное предусловие сервиса,
            # не второй RBAC: права проверяет общий has_perm ниже.
            node_id = m.CredentialBinding.objects.filter(token_user=user).values_list('bound_node_id', flat=True).first()
            if node_id is None or nodes != {node_id} or clients:
                raise Denied('service action or node is outside enrollment')
            node_projects = set(catalog.NodeProject.objects.filter(node_id=node_id).values_list('project_id', flat=True))
            allowed = set(m.ProjectMembership.objects.filter(user=user, source__enabled=True).values_list('project_id', flat=True))
            if not projects <= node_projects & allowed or projects != set(Catalog.current_project_ids(projects)):
                raise Denied('service project is outside enrollment')
        else:
            if nodes != set(Catalog.current_node_ids(nodes)):
                raise NotVisible('resource not found')
            node_projects = catalog.NodeProject.objects.filter(node_id__in=nodes).values_list('project_id', flat=True)
            projects.update(Catalog.current_project_ids(node_projects))
            if clients != set(m.ClientOwnership.objects.filter(client_id__in=clients, user=user).values_list('client_id', flat=True)):
                raise NotVisible('resource not found')
            allowed = set(visible_project_ids(actor))
            if not projects <= allowed:
                raise NotVisible('resource not found')
            if action in GLOBAL_ACTIONS and not _global(user):
                raise Denied('installation-wide scope required')
        _require_permissions(user, action, 'action permission required')


class AuthorizedCatalog(Catalog):
    """Access-composition: client и ownership принадлежат одной transaction."""
    def __init__(self):
        super().__init__(authorizer=Access())

    def register_client(self, actor, *, label):
        with transaction.atomic():
            # Владелец берётся только из живого credential, не из caller payload.
            if type(actor) is not ActorContext:
                raise Denied('trusted actor context required')
            if not get_user_model().objects.select_for_update().filter(pk=actor.user_id).exists():
                raise Denied('credential revoked')
            user = _resolve(actor)
            if user.kind not in ('human', 'agent'):
                raise Denied('client registration requires a human identity')
            client = super().register_client(actor, label=label)
            m.ClientOwnership.objects.create(client_id=client.id, user=user)
            return client


def bind_local_identity(user_id):
    """Явный maintenance hook; повтор не меняет пароль, source или credential."""
    with transaction.atomic():
        source, _ = m.IdentitySource.objects.get_or_create(name='local', defaults={'kind': 'local'})
        if source.kind != 'local' or not source.enabled:
            raise Denied('local source requires authorized repair')
        user = get_user_model().objects.select_for_update().get(pk=user_id)
        if user.kind not in ('human', 'agent'):
            raise Denied('identity belongs to another actor kind')
        identity, created = m.LoginIdentity.objects.get_or_create(user=user,
            defaults={'source': source, 'external_subject': str(user.pk)})
        if identity.source_id != source.pk:
            raise Denied('identity belongs to another source or principal kind')
        if created:
            Token.objects.filter(user=user).delete()
            m.CredentialEvent.objects.create(user=user, action='local_identity_bound')
        return user_id


@sensitive_variables('token')
def _bind_token(token, identity, node_id):
    m.CredentialBinding.objects.create(token_user_id=token.user_id,
        token_digest=_digest(token.key), **_binding_values(token.user, identity, node_id))


@sensitive_variables('token')
def issue_token(user_id):
    """Только после password authentication на trusted boundary, не userid из JSON."""
    with transaction.atomic():
        user = get_user_model().objects.select_for_update().get(pk=user_id)
        if user.kind not in ('human', 'agent'):
            raise Denied('password token issuance requires a human identity')
        identity = _identity(user)
        token, created = Token.objects.get_or_create(user=user)
        if created:
            _bind_token(token, identity, None)
            m.CredentialEvent.objects.create(user=user, action='token_issued')
        else:
            # Существующий credential не усыновляет текущую identity при repair.
            binding = m.CredentialBinding.objects.filter(token_user=user).first()
            if binding is None:
                raise Denied('credential binding is unavailable')
            _check_binding(binding, user)
            if not constant_time_compare(binding.token_digest, _digest(token.key)):
                raise Denied('token generation changed')
        return token.key


@sensitive_variables('token')
def rotate_token(actor):
    with transaction.atomic():
        get_user_model().objects.select_for_update().get(pk=actor.user_id)
        user = _resolve(actor)
        # Enrollment-узел переживает ротацию: credential остаётся тем же machines.
        binding = m.CredentialBinding.objects.filter(pk=actor.credential_id).first()
        node_id = binding.bound_node_id if binding else None
        _identity(user, node_id)
        Token.objects.filter(user=user).delete()
        m.CredentialBinding.objects.filter(token_user=user).delete()
        token = Token.objects.create(user=user)
        # Сервис не имеет LoginIdentity; его proven values -- узел enrollment.
        _bind_token(token, None if user.kind == 'service' else _identity(user), node_id)
        m.CredentialEvent.objects.create(user=user, action='token_rotated')
        return token.key


def revoke_token(actor):
    with transaction.atomic():
        get_user_model().objects.select_for_update().get(pk=actor.user_id)
        user = _resolve(actor)
        deleted, _ = Token.objects.filter(user=user).delete()
        m.CredentialBinding.objects.filter(token_user=user).delete()
        if deleted:
            m.CredentialEvent.objects.create(user=user, action='token_revoked')


@sensitive_variables('token')
def enroll_service(username, node_id, *, project_ids, permissions):
    """Привилегированный local enrollment hook; root проверяет maintenance principal.

    Права сервиса — обычные Django permissions; MACHINE_ACTIONS остаётся
    предусловием enrollment-хука, а не вторым runtime RBAC.
    """
    if not permissions or not set(permissions) <= MACHINE_ACTIONS:
        raise ValueError('service permissions exceed enrollment allowlist')
    with transaction.atomic():
        projects = set(catalog.Project.objects.select_for_update().filter(pk__in=project_ids)
                       .order_by('pk').values_list('pk', flat=True))
        catalog.Node.objects.select_for_update().get(pk=node_id)
        if not Catalog.node_is_current(node_id):
            raise Denied('service node is unavailable')
        assigned = set(catalog.NodeProject.objects.filter(node_id=node_id).values_list('project_id', flat=True))
        if projects != set(project_ids) or projects != set(Catalog.current_project_ids(projects)) or not projects <= assigned:
            raise ValueError('service projects must be assigned to its node')
        source, _ = m.IdentitySource.objects.get_or_create(name='local', defaults={'kind': 'local'})
        if get_user_model().objects.filter(username=username).exists():
            raise ValueError('service username already exists')
        user = get_user_model().objects.create_user(username, password=None, is_staff=False, is_superuser=False)
        # Классификация живёт на самом Actor; узел enrollment — факт credential.
        user.kind = 'service'
        user.save(update_fields=['kind'])
        for action in permissions:
            app, code = action.split('.')
            user.user_permissions.add(Permission.objects.get(content_type__app_label=app, codename=code))
        for project_id in projects:
            m.ProjectMembership.objects.create(user=user, project_id=project_id, source=source)
        token = Token.objects.create(user=user)
        _bind_token(token, None, node_id)
        m.CredentialEvent.objects.create(user=user, action='service_enrolled')
        return token.key


def note_session_logout(session_key):
    """Вызывает browser boundary в одной transaction со стандартным logout."""
    session = Session.objects.filter(pk=session_key).first()
    if session is None:
        return
    user_id = session.get_decoded().get(SESSION_KEY)
    if user_id is not None:
        user = get_user_model().objects.select_for_update().filter(pk=user_id).first()
        if user is not None:
            m.CredentialEvent.objects.create(user=user, action='session_logged_out')


def consume_attempt(username, remote_address='local'):
    """Общий DB rate limit: ключи не содержат пароль, логин или адрес в plaintext."""
    source = m.LoginIdentity.objects.filter(user__username=username).values_list('source_id', flat=True).first()
    keys = sorted((_digest('account:' + str(source) + ':' + username), _digest('origin:' + remote_address)))
    allowed = True
    with transaction.atomic():
        for key in keys:
            m.AuthAttempt.objects.get_or_create(key=key)
            row = m.AuthAttempt.objects.select_for_update().get(pk=key)
            if row.window_started < timezone.now() - timedelta(minutes=5):
                row.count, row.window_started = 0, timezone.now()
            row.count += 1
            row.save(update_fields=['count', 'window_started'])
            allowed = allowed and row.count <= 10
    return allowed


def attempts_blocked(username, remote_address='local'):
    source = m.LoginIdentity.objects.filter(user__username=username).values_list('source_id', flat=True).first()
    keys = (_digest('account:' + str(source) + ':' + username), _digest('origin:' + remote_address))
    return m.AuthAttempt.objects.filter(key__in=keys, count__gt=10,
        window_started__gte=timezone.now() - timedelta(minutes=5)).exists()
