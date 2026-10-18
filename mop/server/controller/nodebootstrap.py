"""Finite private transport export #722; не authority и не node runtime."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from mop.server.install_files import private_directory


# Это профиль конкретного реализованного node executor, не вся MACHINE_ACTIONS
# и не бизнес-политика файла. Каждое действие повторно проверяет живой RBAC.
NODE_ACTIONS = frozenset('execution.' + name for name in (
    'poll_work', 'claim_work', 'report_result', 'read_facts', 'read_launch',
    'register_producer', 'ingest_telemetry', 'ingest_observations'))


class Transport(dict):
    def __repr__(self):
        return '<node transport bootstrap: redacted>'

    __str__ = __repr__


def canonical_path(value):
    """OS-ссылка без чтения файла: CA и destination принадлежат другой машине."""
    if not isinstance(value, (str, os.PathLike)):
        raise RuntimeError('node export requires canonical absolute paths')
    raw = os.fspath(value)
    if (not isinstance(raw, str) or not raw.startswith('/') or raw == '/'
            or any(ord(char) < 32 or ord(char) == 127 or 0xd800 <= ord(char) <= 0xdfff for char in raw)
            or any(part in ('', '.', '..') for part in raw.split('/')[1:])):
        raise RuntimeError('node export requires canonical absolute paths')
    return Path(raw)


def transport(*, installation_id, origin, ca_file, node_id, token):
    """Замороженная v1 transport-схема #624: никаких epoch/policy/trust roots."""
    if type(installation_id) is not str or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}', installation_id):
        raise RuntimeError('node bootstrap installation identity is invalid')
    if type(origin) is not str or not origin.isascii() or any(ord(char) <= 32 or ord(char) == 127 for char in origin):
        raise RuntimeError('node bootstrap requires a canonical HTTPS origin')
    try:
        parts = urlsplit(origin)
        valid = (parts.scheme == 'https' and parts.hostname and parts.username is None and parts.password is None
                 and not parts.path and not parts.query and not parts.fragment
                 and parts.port != 0 and '?' not in origin and '#' not in origin
                 and not parts.netloc.endswith(':') and '\\' not in origin)
    except ValueError:
        valid = False
    if not valid:
        raise RuntimeError('node bootstrap requires a canonical HTTPS origin')
    if type(node_id) is not UUID or node_id.int == 0:
        raise RuntimeError('node bootstrap requires a node UUID')
    if type(token) is not str or not re.fullmatch('[0-9a-f]{40}', token):
        raise RuntimeError('node bootstrap requires a standard service credential')
    return Transport(schema_version=1, installation_id=installation_id, origin=origin,
                     ca_file=str(canonical_path(ca_file)), node_id=str(node_id), token=token)


class ExportFiles:
    """Descriptor нового private каталога, не bearer store и не delivery driver."""
    def __init__(self, directory, file, node_file):
        self.directory, self.file, self.node_file = directory, file, node_file

    def __repr__(self):
        return '<private node export files>'

    def _write(self, name, data):
        descriptor = os.open(name, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
                             0o600, dir_fd=self.directory)
        with os.fdopen(descriptor, 'wb') as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())

    def write(self, data, *, node_id, enrollment_id):
        if type(data) is not bytes:
            raise TypeError('node bootstrap export requires bytes')
        self._write(self.file.name, data)
        delivery = dict(schema_version=1, node_id=str(node_id), enrollment_id=str(enrollment_id),
                        controller_file=str(self.file), node_file=str(self.node_file),
                        mode='0600', directory_mode='0700', no_log=True)
        self._write('delivery.json', (json.dumps(delivery, sort_keys=True) + '\n').encode())
        os.fsync(self.directory)


@contextmanager
def export_files(file, *, node_file):
    """Новый каталог в private owned parent; существующее не открываем на запись.

    Dirfd держит проверенных родителей до конца экспорта. Ошибка/rollback удаляет
    лишь наши новые артефакты; предыдущий export не используется как authority.
    """
    file, destination = canonical_path(file), canonical_path(node_file)
    if file.name == 'delivery.json':
        raise RuntimeError('node bootstrap filename collides with delivery metadata')
    with private_directory(file.parent.parent) as parent:
        directory = None
        created = complete = False
        try:
            os.mkdir(file.parent.name, mode=0o700, dir_fd=parent)
            created = True
            # Private trusted parent исключает чужую замену; umask=0777 мог
            # сделать mkdir-каталог mode 000 ещё до первого open.
            os.chmod(file.parent.name, 0o700, dir_fd=parent, follow_symlinks=False)
            directory = os.open(file.parent.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            # Устанавливаем точные modes независимо от umask процесса.
            os.fchmod(directory, 0o700)
            # Никакого fallible artifact I/O после SQL commit: parent durable
            # до enrollment, file/directory fsync выполняет write внутри него.
            os.fsync(parent)
            yield ExportFiles(directory, file, destination)
            complete = True
        except OSError:
            raise RuntimeError('node export requires a new private folder and writable files; no replacement') from None
        finally:
            if directory is not None:
                if not complete:
                    for name in (file.name, 'delivery.json'):
                        try:
                            os.unlink(name, dir_fd=directory)
                        except FileNotFoundError:
                            pass
                os.close(directory)
            if created and not complete:
                os.rmdir(file.parent.name, dir_fd=parent)


def _permissions(user):
    return frozenset(user.user_permissions.values_list('content_type__app_label', 'codename'))


def _live(enrollment, projects):
    """Не repair: тот же exact enrollment, Actor, membership, RBAC и credential."""
    from django.contrib.auth import get_user_model
    from django.views.decorators.debug import sensitive_variables
    from rest_framework.authtoken.models import Token
    from mop.server.access import models as access, services as auth
    user = get_user_model().objects.select_for_update().get(pk=enrollment.actor_id)
    if (user.kind != 'service' or not user.is_active or user.is_staff or user.is_superuser
            or user.has_usable_password() or user.groups.exists()
            or access.LoginIdentity.objects.filter(user=user).exists()
            or access.AllProjectMembership.objects.filter(user=user).exists()
            or access.ClientOwnership.objects.filter(user=user).exists()
            or _permissions(user) != frozenset(tuple(action.split('.', 1)) for action in NODE_ACTIONS)):
        raise RuntimeError('node enrollment actor or permissions changed; explicit maintenance recovery required')
    memberships = list(access.ProjectMembership.objects.filter(user=user)
                       .values_list('project_id', 'source__name', 'source__kind', 'source__enabled'))
    if (len(memberships) != len(projects) or {row[0] for row in memberships} != projects
            or any(row[1:] != ('local', 'local', True) for row in memberships)):
        raise RuntimeError('node enrollment projects changed; explicit maintenance recovery required')
    # Authentication проверяет digest, active Actor, purpose/node и binding,
    # а не существование username или даже одного enrollment ID.
    @sensitive_variables('token')
    def credential():
        token = Token.objects.filter(user=user).first()
        if token is None:
            raise RuntimeError('node enrollment credential is unavailable; explicit maintenance recovery required')
        try:
            actor = auth.token_context(token.key)
            auth.node_executor(actor, enrollment.node_id)
            for action in NODE_ACTIONS:
                auth.Access().authorize(actor, action, node_ids=(enrollment.node_id,), project_ids=projects)
        except (auth.Denied, PermissionError):
            raise RuntimeError('node enrollment credential is not current; explicit maintenance recovery required') from None
        return token.key
    return credential()


def issue(*, node_id, origin, ca_file, file, node_file, enrollment_id=None):
    """SQL authority плюс private export; вызывается только maintenance root."""
    from django.contrib.auth import get_user_model
    from django.db import DatabaseError, transaction
    from django.views.decorators.debug import sensitive_variables
    from mop.server.access import models as access, services as auth
    from mop.server.catalog.models import Node, NodeProject, Project
    from mop.server.sessions.models import Assignment, NodeExecutor
    from . import runtime
    from .models import Installation

    runtime._maintenance_role()
    try:
        state = runtime.verify_installation()
    except DatabaseError as error:
        raise RuntimeError('node enrollment database operation failed: ' + type(error).__name__) from None
    if type(node_id) is not UUID or (enrollment_id is not None and type(enrollment_id) is not UUID):
        raise RuntimeError('node enrollment requires explicit UUID coordinates')
    # Validation до первого файлового/SQL изменения; фиктивный ключ не выходит
    # наружу, только проверяет transport coordinates без обращения к Node OS.
    transport(installation_id=state['identity'], node_id=node_id, origin=origin,
              ca_file=ca_file, token='0' * 40)
    if transaction.get_connection().in_atomic_block:
        raise RuntimeError('node export requires its own committed maintenance transaction')

    @sensitive_variables('token', 'document')
    def enroll(bundle):
        with runtime.short_transaction():
            runtime.require_restore_ready(lock=True)
            Installation.objects.select_for_update().get(pk=1)
            runtime.verify_installation()
            # Порядок общих locks как у enroll_service: projects, затем node.
            projects = set(Project.objects.select_for_update().filter(
                pk__in=NodeProject.objects.filter(node_id=node_id).values('project_id'),
                retired_at__isnull=True).order_by('pk').values_list('pk', flat=True))
            node = Node.objects.select_for_update().filter(pk=node_id, retired_at__isnull=True).first()
            if node is None:
                raise RuntimeError('node enrollment requires a current installation node')
            locked_projects = set(NodeProject.objects.filter(node_id=node_id, project__retired_at__isnull=True)
                                  .values_list('project_id', flat=True))
            if projects != locked_projects:
                raise RuntimeError('node project scope changed during enrollment; retry explicit export')
            current = list(access.ServiceEnrollment.objects.select_for_update()
                           .filter(node_id=node_id, use_case='node-executor').order_by('pk'))
            if enrollment_id is None:
                if current:
                    raise RuntimeError('node enrollment already exists; reexport requires its exact enrollment UUID')
                username = 'node-executor-' + node_id.hex
                if get_user_model().objects.filter(username=username).exists():
                    raise RuntimeError('node enrollment username collision; no actor was changed')
                if Assignment.objects.filter(node_id=node_id).exists():
                    raise RuntimeError('node enrollment requires fresh execution state; explicit reconciliation required')
                source = access.IdentitySource.objects.filter(name='local').first()
                if source is not None and (source.kind != 'local' or not source.enabled):
                    raise RuntimeError('node enrollment local membership source is unavailable')
                auth.enroll_service(username, node_id, project_ids=projects, permissions=NODE_ACTIONS)
                enrollment = access.ServiceEnrollment.objects.get(actor__username=username)
                NodeExecutor.objects.get_or_create(node_id=node_id, defaults={'executor_epoch': uuid4()})
            else:
                if len(current) != 1 or current[0].pk != enrollment_id:
                    raise RuntimeError('node export requires the exact current node-executor enrollment')
                enrollment = current[0]
                if not NodeExecutor.objects.filter(node_id=node_id).exists():
                    raise RuntimeError('node executor epoch is unavailable; explicit reconciliation required')
            token = _live(enrollment, projects)
            document = transport(installation_id=state['identity'], node_id=node_id, origin=origin,
                                 ca_file=ca_file, token=token)
            bundle.write((json.dumps(document, sort_keys=True) + '\n').encode(),
                         node_id=node_id, enrollment_id=enrollment.pk)
            return {'enrollment_id': str(enrollment.pk), 'actor_id': enrollment.actor_id,
                    'node_id': str(node_id), 'file': str(bundle.file)}

    try:
        with export_files(file, node_file=node_file) as bundle:
            result = enroll(bundle)
        return result
    except DatabaseError as error:
        # Driver errors могут содержать SQL/credential: не отражаем raw exception.
        raise RuntimeError('node enrollment database operation failed: ' + type(error).__name__) from None
    except (auth.Denied, ValueError):
        raise RuntimeError('node enrollment refused; current service authority required') from None
