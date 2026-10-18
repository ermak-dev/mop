"""Отдельная credential OS-служба: root/provider остаются внутри процесса.

Socket принимает конечные application commands, не ciphertext/AAD/actor claims.
Ни HTTP, ни произвольного RPC, ни собственного credential store. Реальные
Preparation approval и current Assignment связаны; непроверенные delivery proofs запрещены.
"""
import asyncio
from dataclasses import fields
import os
import signal
import socket
import stat
import sys
from uuid import UUID

from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.db import transaction

from mop.common import credentials as p
from mop.server.access import services as access
from mop.server.access.grants import Grants, Conflict as GrantConflict, StorageUnavailable as GrantUnavailable
from mop.server.access.delegation import RoleBinding
from mop.server.sessions.services import Assignments
from mop.server.secrets import services as secrets
from mop.server.preparation import services as preparation
from mop.server.secrets.crypto import SecretCryptoError
from . import bootstrap
from .models import Installation
from .runtime import Runtime, Backpressure, require_restore_ready, verify_installation, maintenance, close_pools


class AwaitAssignment:
    """Адресная post-readiness выдача: locator из wire, authority из owner APIs."""
    def __init__(self, application, target_id=None):
        self.application, self.target_id = application, target_id

    def resolve(self, actor):
        app = self.application
        # Только короткие SQL-only queries. Provider вызывается SecretStore уже
        # после выхода; resolve повторяется с нуля после любого crypto wait.
        with transaction.atomic():
            app.fence()
            if not app.snapshot['effects_enabled']:
                raise access.Denied('installation effects are disabled')
            proof = Assignments().credential_proof(actor, self.target_id, epoch=UUID(app.snapshot['epoch']))
            binding = proof.binding
            policy = app.grants.addressed_policy(RoleBinding(binding.project_id, binding.target_id,
                binding.incarnation_id, binding.role, binding.bundle_digest, binding.grant_id, binding.owner_id))
            return app.store.addressed_selection(actor, project_id=binding.project_id, node_id=binding.node_id,
                assignment_id=binding.target_id, incarnation=binding.incarnation_id, role=binding.role,
                digest=binding.bundle_digest, grant_id=binding.grant_id, required_names=policy.required_names,
                current_proof=(proof, policy))


class CurrentBindings:
    """SQL-only public Assignment projection, не activation/transport receipt."""
    def __init__(self):
        self.assignments = Assignments()

    def current(self, project_id, target_id):
        binding = self.assignments.current(project_id=project_id, target_id=target_id)
        if binding is None:
            return None
        return RoleBinding(binding.project_id, binding.target_id, binding.incarnation_id,
            binding.role, binding.bundle_digest, binding.grant_id, binding.owner_id)

    def activation(self, grant_id):
        """Доверенная квитанция активации (#643): текущее назначение этого гранта
        с подтверждёнными identity и готовностью именно текущей инкарнации."""
        from uuid import UUID
        from mop.server.controller.models import Installation
        from mop.server.sessions.models import Assignment
        target_id = Assignment.objects.filter(grant_id=grant_id, deleted_at=None).values_list('id', flat=True).first()
        binding = None if target_id is None else self.assignments.target(target_id)
        if binding is None or binding.grant_id != grant_id:
            return None
        epoch = Installation.objects.values_list('epoch', flat=True).get()
        if self.assignments.readiness(binding, epoch=UUID(str(epoch))) is None:
            return None
        return RoleBinding(binding.project_id, binding.target_id, binding.incarnation_id,
            binding.role, binding.bundle_digest, binding.grant_id, binding.owner_id)

    def retirement(self, grant_id, revision):
        raise access.Denied('transport retirement receipt composition is not bound')


class CreateActivation:
    """Request-local authority worker: не произвольная активация по grant UUID.

    Worker предъявляет только свой token. Узел/проект -- enrollment и текущее
    назначение, допуск эффекта -- claimed create общего журнала. Снимок не
    переживает запрос; каждый callback после crypto заново читает живые факты.
    """
    def __init__(self, application, actor, grant_id):
        self.application, self.actor, self.grant_id = application, actor, grant_id
        self.proof = None

    def require(self):
        from django.utils import timezone
        from mop.server.access.models import ServiceEnrollment
        from mop.server.operations import models as operations
        from mop.server.sessions.models import Assignment
        app, actor = self.application, self.actor
        with transaction.atomic():
            app.fence()
            access.current_actor(actor, grants=app.local_grants)
            enrollment = ServiceEnrollment.objects.filter(actor_id=actor.user_id,
                use_case='grant-activator', node__isnull=False).first()
            if enrollment is None or actor.kind is not access.ActorKind.SERVICE or actor.grant_id is not None:
                raise access.Denied('enrolled grant activator required')
            if not app.snapshot['effects_enabled']:
                raise access.Denied('installation effects are disabled')
            target_id = Assignment.objects.filter(grant_id=self.grant_id, deleted_at=None).values_list('id', flat=True).first()
            binding = None if target_id is None else Assignments().target(target_id, lock=True)
            if binding is None:
                raise access.Denied('current create assignment required for grant activation')
            access.Access().authorize(actor, 'access.activate_grant', project_ids=(binding.project_id,),
                node_ids=(binding.node_id,))
            # Assignment -> Operation -> Step: те же блокировки, что у worker.
            row = operations.Operation.objects.select_for_update().filter(action='create', state='running',
                target_id=binding.target_id, incarnation_id=binding.incarnation_id, node_id=binding.node_id,
                expected_revision=binding.revision, actor_id=binding.owner_id,
                epoch=UUID(app.snapshot['epoch']), restore_generation=UUID(app.generation),
                not_after__gt=timezone.now(), restartbinding__grant_id=self.grant_id,
                restartbinding__project_id=binding.project_id, restartbinding__owner_id=binding.owner_id,
                restartbinding__generation=binding.generation, restartbinding__role=binding.role,
                restartbinding__bundle_digest=binding.bundle_digest,
                restartbinding__executor_epoch=binding.executor_epoch).first()
            if row is None or set(row.projects.values_list('pk', flat=True)) != {binding.project_id}:
                raise access.Denied('matching claimed create required for grant activation')
            stages = tuple(operations.Step.objects.select_for_update().filter(operation=row).order_by('ordinal'))
            if (tuple(step.name for step in stages) != ('scheduler_register', 'apply_identity', 'confirm_ready', 'activate_grant')
                    or any(step.outcome != 'succeeded' or step.quiescent_at is None for step in stages[:3])):
                raise access.Denied('matching claimed create required for grant activation')
            step = stages[-1]
            if (step.token is None or step.attempt_id != step.token or step.dispatched_at is None
                    or step.quiescent_at is not None or step.outcome or step.lease_until is None
                    or step.lease_until <= timezone.now()):
                raise access.Denied('matching claimed create required for grant activation')
            keys = {'node:'+str(binding.node_id), 'project:'+str(binding.project_id),
                    'puppet:'+str(binding.target_id), 'target:'+str(binding.target_id)}
            claims = tuple(operations.ResourceClaim.objects.filter(operation=row, released_at=None,
                resource_id__in=keys).order_by('resource_id').values_list('resource_id', 'step_id', 'token'))
            if {claim[0] for claim in claims} != keys:
                raise access.Denied('matching claimed create resource fences required for grant activation')
            proof = (enrollment.id, binding, row.id, row.revision, step.id, step.token, claims)
            if self.proof is not None and self.proof != proof:
                raise access.Denied('claimed create activation authority changed')
            self.proof = proof


class CreateActivationBindings(CurrentBindings):
    """Финальный callback Grants держит SQL authority до его COMMIT, не crypto."""
    def __init__(self, authority):
        super().__init__()
        self.authority = authority

    def activation(self, grant_id):
        if grant_id != self.authority.grant_id:
            raise access.Denied('activation grant changed')
        self.authority.require()
        return super().activation(grant_id)


class CurrentAccess:
    def __init__(self, application):
        self.application = application

    def authorize(self, actor, action, **refs):
        self.application.fence()
        if not refs.get('project_ids'):
            # manage_project_secret не доказывает authority над Nomad/signer
            # или другими installation credentials. Их use case ещё не связан.
            raise PermissionError('installation credential application authority is not bound')
        return access.Access(grants=self.application.local_grants).authorize(actor, action, **refs)


class CurrentPreparationAccess(CurrentAccess):
    def authorize_owner(self, owner_id, action, *, project_ids=()):
        # owner_id берётся только из persisted BootstrapRevision, не из wire.
        # Это revalidation владельца policy, не восстановление ActorContext.
        self.application.fence()
        return access.Access().authorize_owner(owner_id, action, project_ids=project_ids)


class CurrentRetention:
    """Root fence дополняет, но не заменяет SQL-only authority предметного владельца."""
    def __init__(self, application, authority, *, activation=None):
        self.application, self.authority, self.activation = application, authority, activation

    def check(self, actor, scope):
        self.application.fence()
        if self.activation is not None:
            self.activation.require()
        self.authority.check(actor, scope)
        self.application.fence()


class CredentialApplication:
    """Доверенная композиция: аргументы конструктора не берутся из wire."""
    def __init__(self, provider, snapshot, generation, *, delivery_gate=None):
        self.snapshot, self.generation = snapshot, generation
        # Metadata grant callbacks используют только публичные SQL-only ports.
        # Assignment projection не подменяет отдельные activation/delivery proofs.
        self.grants = Grants(provider=provider, bindings=CurrentBindings())
        self.local_grants = self.grants.local_authority()
        self.preparation = preparation.Preparation(provider=provider,
            authorizer=CurrentPreparationAccess(self))
        self.preparation.secrets.authority = CurrentRetention(self, self.preparation.secrets.authority)
        self.grants.approvals = self.preparation.approval_query()
        # Domain owner создал exact authority. Оборачиваем её публичный
        # RetentionAuthority port, не переносим Access/Grants ORM в Root.
        self.grants.secrets.authority = CurrentRetention(self, self.grants.secrets.authority)
        self.store = secrets.SecretStore(provider=provider, authorizer=CurrentAccess(self),
            delivery_gate=delivery_gate if delivery_gate is not None else AwaitAssignment(self))

    @secrets.storage_errors
    def fence(self):
        # Только ORM внутри Authorizer/короткой SQL секции. Root и schema
        # проверены на startup; файловый/provider I/O здесь запрещён.
        state = Installation.objects.values('identity', 'epoch', 'root_digest', 'effects_enabled').get(pk=1)
        state['epoch'] = str(state['epoch'])
        try:
            generation = require_restore_ready()
        except RuntimeError as exc:
            # Это уже безопасная фраза foundation, не raw driver exception.
            raise access.Denied(str(exc)) from None
        if state != self.snapshot or generation != self.generation:
            raise PermissionError('credential installation generation or root trust changed; restart required')

    def execute(self, credential, command, args):
        self.fence()
        actor = (access.token_context(credential.value, grants=self.local_grants)
                 if credential.kind == 'token' else access.session_context(credential.value))
        if command is p.Command.CREATE:
            result = self.store.create(actor, **args)
        elif command is p.Command.STAGE:
            result = self.store.stage(actor, **args)
        elif command is p.Command.UPDATE:
            result = self.store.update(actor, **args)
        elif command is p.Command.ADDRESSED:
            # Gate принадлежит одному запросу; concurrent socket tasks не меняют
            # target общего store и не могут подменить друг другу selection.
            store = secrets.SecretStore(provider=self.store.provider, authorizer=self.store.authorizer,
                delivery_gate=AwaitAssignment(self, **args))
            result = store.issue_addressed(actor)
        elif command is p.Command.ROTATION:
            result = self.store.issue_rotation(actor, **args)
        elif command is p.Command.PUBLISH_DEFINITION:
            row = self.grants.publish_definition(actor, **args)
            result = p.DefinitionInfo(row.id, row.role, row.digest, row.project_id, row.owner_id)
        elif command is p.Command.LAUNCH:
            from .launchdelivery import deliver
            result = deliver(self, actor, args['work'])
        elif command is p.Command.DEFINITION:
            row = self.grants.read_definition(actor, **args)
            result = p.DefinitionBytes(row.id, row.role, row.digest, row.manifest, row.prompt, row.provenance)
        elif command is p.Command.APPROVE_DEFINITION:
            result = self.grants.approve_definition(actor, **args)
        elif command is p.Command.PREPARE_REPLACEMENT:
            from .replacement import ReplacementPreparation
            row = self.grants.prepare_replacement(actor, authority=ReplacementPreparation(self, **args))
            result = p.GrantReceipt(row.id, row.project_id, row.target_id, row.incarnation,
                row.role, row.digest, row.state, row.revision, row.credential_version_id)
        elif command in (p.Command.RESERVE_GRANT, p.Command.PROVISION_GRANT, p.Command.ACTIVATE_GRANT):
            if command is p.Command.ACTIVATE_GRANT:
                authority = CreateActivation(self, actor, args['grant_id'])
                authority.require()
                # Отдельные ports запроса: concurrent clients не меняют общий Grants.
                grants = Grants(provider=self.store.provider, bindings=CreateActivationBindings(authority),
                    approvals=self.preparation.approval_query())
                grants.secrets.authority = CurrentRetention(self, grants.secrets.authority, activation=authority)
                row = grants.activate(**args)
            else:
                row = (self.grants.reserve(actor, **args) if command is p.Command.RESERVE_GRANT
                       else self.grants.provision(actor, **args))
            result = p.GrantReceipt(row.id, row.project_id, row.target_id, row.incarnation,
                row.role, row.digest, row.state, row.revision, row.credential_version_id)
        elif command is p.Command.PUBLISH_PREPARATION:
            row = self.preparation.publish(actor, **args)
            result = p.PreparationInfo(row.id, row.project_id, row.owner_id, row.number,
                row.content_version_id, row.digest, row.task_size, row.vars_size)
        elif command is p.Command.READ_PREPARATION:
            row = self.preparation.read_revision(actor, **args)
            result = p.PreparationBytes(row.tasks, row.variables, row.provenance, row.commit, row.secret_refs)
        elif command is p.Command.APPROVE_PREPARATION:
            row = self.preparation.approve(actor, **args)
            result = p.ApprovalReceipt(row.id, row.revision_id, row.human_id)
        else:
            raise p.CredentialError('unsupported credential command')
        self.fence()
        access.current_actor(actor, grants=self.local_grants)
        return _wire(result)


def _wire(value):
    if type(value) is dict:
        if len(value) > 128 or any(type(key) is not str for key in value):
            raise p.CredentialError('invalid credential result fields')
        return {key: _wire(item) for key, item in value.items()}
    if type(value) is bytes:
        return p.encoded(value)
    if type(value) is UUID:
        return str(value)
    if type(value) in (tuple, list):
        if len(value) > 128:
            raise p.CredentialError('credential result exceeds item limit')
        return [_wire(item) for item in value]
    if type(value) in (secrets.SecretMetadata, secrets.VersionMetadata, secrets.AddressedSecret,
                       p.DefinitionBytes, p.DefinitionInfo, p.GrantReceipt, p.PreparationInfo,
                       p.PreparationBytes, p.ApprovalReceipt):
        return {f.name: _wire(getattr(value, f.name)) for f in fields(value)}
    if value is None or type(value) in (str, int, bool):
        return value
    raise p.CredentialError('unsupported credential result')


def application(*, key_id='root-v1', provider=None):
    """Root/fingerprint читаются только из проверенной Installation, не из caller."""
    if settings.BOOTSTRAP['role'] != 'credential':
        raise RuntimeError('credential service requires its separate credential bootstrap and DB principal')
    verify_installation()
    generation = require_restore_ready()
    row = Installation.objects.get(pk=1)
    snapshot = {'identity': row.identity, 'epoch': str(row.epoch),
                'root_digest': row.root_digest, 'effects_enabled': row.effects_enabled}
    if provider is None:
        provider = bootstrap.secret_provider(settings.BOOTSTRAP, installation_id=row.identity,
            expected_root_digest=row.root_digest, key_id=key_id)
    return CredentialApplication(provider, snapshot, generation)


def compose(*, key_id):
    # Сюда нельзя попасть через обычный web/worker или с elevated DB principal.
    if settings.BOOTSTRAP['role'] != 'credential':
        raise RuntimeError('credential service requires its separate credential bootstrap and DB principal')
    try:
        maintenance('check')
        return application(key_id=key_id)
    finally:
        # Startup идёт на main thread. Его checkout нельзя оставить за
        # пределами Runtime: worker cleanup не закрывает чужой thread-local.
        close_pools()


def _failure(exc):
    # Library phrases этих категорий уже безопасны: не обрезаем их второй раз.
    if isinstance(exc, (p.CredentialError, access.Denied, secrets.Refused, secrets.Conflict,
                        secrets.StorageUnavailable, secrets.TransactionBoundary, SecretCryptoError,
                        preparation.Conflict, preparation.StorageUnavailable, GrantConflict, GrantUnavailable)):
        return {'ok': False, 'error': {'code': 'refused', 'message': str(exc)}}
    if type(exc) is PermissionError:
        return {'ok': False, 'error': {'code': 'refused', 'message': str(exc)}}
    if isinstance(exc, ObjectDoesNotExist):
        message = 'credential resource not found'
    elif isinstance(exc, ValidationError):
        message = 'invalid credential resource fields'
    elif isinstance(exc, Backpressure):
        message = 'credential service capacity exhausted'
    else:
        message = 'credential service unavailable; mutation outcome unknown, do not replay'
    return {'ok': False, 'error': {'code': 'unavailable', 'message': message}}


class CredentialService:
    """Ограниченный socket lifecycle; каждый accepted task принадлежит Runtime.

    Parent dir принадлежит service UID, без иных writers; optional group имеет
    только traversal и socket rw. UID allowlist проверяется до чтения auth/body.
    Stale socket никогда не удаляется при startup; cleanup только своего inode.
    """
    def __init__(self, path, *, application, runtime, allowed_uids=(), socket_group=None, timeout=5, limit=8):
        self.path = p.socket_path(path)
        if (type(allowed_uids) not in (tuple, list) or len(allowed_uids) > 16
                or any(type(uid) is not int or uid < 0 for uid in allowed_uids)
                or not 0 < timeout <= 10 or type(limit) is not int or not 1 <= limit <= 8):
            raise ValueError('credential service requires bounded limits and explicit peer UIDs')
        if socket_group is not None and (type(socket_group) is not int or socket_group not in {os.getegid(), *os.getgroups()}):
            raise ValueError('credential socket group must belong to the service account')
        self.application, self.runtime = application, runtime
        self.allowed_uids, self.socket_group = frozenset(allowed_uids), socket_group
        self.timeout, self.limit = timeout, limit
        self._server = None
        self._directory = None
        self._inode = None
        self._connections = 0
        self._tasks = set()

    async def start(self):
        if self._server is not None:
            raise RuntimeError('credential service is already started')
        directory = p.directory_fd(self.path, os.geteuid())
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            # bind не заменяет существующий endpoint. До chmod umask не
            # определяет окно доступа: приватный socket создаётся под 077.
            previous = os.umask(0o077)
            try:
                sock.bind(f'/proc/self/fd/{directory}/{self.path.name}')
            finally:
                os.umask(previous)
            self._inode = os.stat(self.path.name, dir_fd=directory, follow_symlinks=False).st_ino
            self._directory = directory
            if self.socket_group is not None:
                os.chown(self.path.name, -1, self.socket_group, dir_fd=directory, follow_symlinks=False)
            os.chmod(self.path.name, 0o660 if self.socket_group is not None else 0o600,
                     dir_fd=directory, follow_symlinks=False)
            sock.listen(self.limit)
            sock.setblocking(False)
            # 3.13 сам unlink-ит pathname при close; нашим inode владеем мы,
            # включая закреплённый dirfd и безопасный retry cleanup.
            options = {'cleanup_socket': False} if sys.version_info >= (3, 13) else {}
            self._server = await asyncio.start_unix_server(self._accepted, sock=sock,
                limit=p.MAX_FRAME + 4, **options)
        except BaseException:
            sock.close()
            if self._directory is not None:
                self._unlink_owned()
            else:
                os.close(directory)
            raise RuntimeError('credential socket unavailable or already exists; explicit local repair required') from None

    def _accepted(self, reader, writer):
        if self._connections >= self.limit or p.peer_uid(writer.get_extra_info('socket')) not in self.allowed_uids:
            writer.close()
            return
        self._connections += 1
        # Callback запускает только bookkeeping coroutine. Вся работа, включая
        # чтение медленного peer, проходит runtime effect capacity.
        task = asyncio.create_task(self._owned(reader, writer))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        task.add_done_callback(lambda value: value.exception() if not value.cancelled() else None)

    async def _owned(self, reader, writer):
        try:
            await self.runtime.effect(self._connection, reader, writer)
        except Exception:
            writer.close()
        finally:
            self._connections -= 1

    async def _connection(self, reader, writer):
        try:
            async with asyncio.timeout(self.timeout):
                header = await reader.readexactly(4)
                try:
                    size = p.frame_size(header)
                except p.CredentialError:
                    return
                credential, command, args = p.parse_request(p.loads(await reader.readexactly(size)))
                try:
                    result = await self.runtime.database(self.application.execute, credential, command, args)
                    response = {'ok': True, 'result': result}
                    wire = p.frame(response)
                except Exception as exc:
                    wire = p.frame(_failure(exc))
                writer.write(wire)
                await writer.drain()
        except p.CredentialError as exc:
            # Protocol refusals только фиксированные фразы, без echo payload.
            try:
                async with asyncio.timeout(self.timeout):
                    writer.write(p.frame(_failure(exc)))
                    await writer.drain()
            except (OSError, TimeoutError):
                pass
        except (OSError, TimeoutError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            try:
                async with asyncio.timeout(self.timeout):
                    await writer.wait_closed()
            except (OSError, TimeoutError):
                pass

    def _unlink_owned(self):
        if self._directory is None:
            return
        try:
            try:
                info = os.stat(self.path.name, dir_fd=self._directory, follow_symlinks=False)
            except FileNotFoundError:
                return
            if stat.S_ISSOCK(info.st_mode) and info.st_ino == self._inode:
                os.unlink(self.path.name, dir_fd=self._directory)
        finally:
            os.close(self._directory)
            self._directory = None

    async def aclose(self):
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        if self._tasks:
            _, pending = await asyncio.wait(tuple(self._tasks), timeout=10)
            if pending:
                raise RuntimeError('credential shutdown incomplete: connections are still running')
        self._unlink_owned()


def serve(*, path, allowed_uids, key_id, socket_group=None):
    app = compose(key_id=key_id)
    async def run():
        runtime = Runtime(queue_limit=0)
        server = CredentialService(path, application=app, runtime=runtime,
            allowed_uids=allowed_uids, socket_group=socket_group)
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for name in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(name, stop.set)
        try:
            await runtime.start()
            await server.start()
            await stop.wait()
        finally:
            await server.aclose()
            await runtime.aclose()
            for name in (signal.SIGINT, signal.SIGTERM):
                loop.remove_signal_handler(name)
    asyncio.run(run())
