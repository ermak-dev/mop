"""Отдельная credential OS-служба: root/provider остаются внутри процесса.

Socket принимает конечные application commands, не ciphertext/AAD/actor claims.
Ни HTTP, ни произвольного RPC, ни собственного credential store. Конкретные
assignment/approval bindings ещё не собраны: отсутствие порта всегда отказ.
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

from mop.common import credentials as p
from mop.server.access import services as access
from mop.server.access.grants import Grants
from mop.server.secrets import services as secrets
from mop.server.secrets.crypto import SecretCryptoError
from . import bootstrap
from .models import Installation
from .runtime import Runtime, Backpressure, require_restore_ready, verify_installation, maintenance, close_pools


class AwaitAssignment:
    """#639: не выдавать по permission/token/operation id вместо реального proof."""
    def resolve(self, actor):
        raise PermissionError('current assignment and addressed delivery composition is not bound')


class CurrentAccess:
    def __init__(self, application):
        self.application = application

    def authorize(self, actor, action, **refs):
        self.application.fence()
        if not refs.get('project_ids'):
            # manage_project_secret не доказывает authority над Nomad/signer
            # или другими installation credentials. Их use case ещё не связан.
            raise PermissionError('installation credential application authority is not bound')
        return access.Access(grants=self.application.grants).authorize(actor, action, **refs)


class CredentialApplication:
    """Доверенная композиция: аргументы конструктора не берутся из wire."""
    def __init__(self, provider, snapshot, generation, *, delivery_gate=None):
        self.snapshot, self.generation = snapshot, generation
        # Unbound approval/runtime queries в Grants — явный deny, не fake proof.
        self.grants = Grants(provider=provider)
        self.store = secrets.SecretStore(provider=provider, authorizer=CurrentAccess(self),
            delivery_gate=delivery_gate if delivery_gate is not None else AwaitAssignment())

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
        actor = (access.token_context(credential.value, grants=self.grants)
                 if credential.kind == 'token' else access.session_context(credential.value))
        if command is p.Command.CREATE:
            result = self.store.create(actor, **args)
        elif command is p.Command.STAGE:
            result = self.store.stage(actor, **args)
        elif command is p.Command.UPDATE:
            result = self.store.update(actor, **args)
        elif command is p.Command.ADDRESSED:
            result = self.store.issue_addressed(actor)
        elif command is p.Command.ROTATION:
            result = self.store.issue_rotation(actor, **args)
        elif command is p.Command.BUNDLE:
            row = self.grants.read_bundle(actor, **args)
            result = p.BundleBytes(row.id, row.role, row.digest, row.manifest, row.prompt, row.provenance)
        else:
            raise p.CredentialError('unsupported credential command')
        self.fence()
        access.current_actor(actor, grants=self.grants)
        return _wire(result)


def _wire(value):
    if type(value) is bytes:
        return p.encoded(value)
    if type(value) is UUID:
        return str(value)
    if type(value) in (tuple, list):
        if len(value) > 128:
            raise p.CredentialError('credential result exceeds item limit')
        return [_wire(item) for item in value]
    if type(value) in (secrets.SecretMetadata, secrets.VersionMetadata, secrets.AddressedSecret, p.BundleBytes):
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
                        secrets.StorageUnavailable, secrets.TransactionBoundary, SecretCryptoError)):
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
