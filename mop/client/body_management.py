"""Пустой body-only management до protected handoff #748.

Файлы транспортируют expected runtime и transient Token, но не права. Единственный
worker владеет blocking HTTPS, командлетом и временным CA до завершения I/O.
Root staging/issuer и physical body binding сюда не входят (#745/#746).
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import stat
import tempfile
import threading
import time

from mop.common.management_context import BodyBootstrap, ManagementContext, body_deadline
from . import control as api

BODY_LIMITS = api.Limits(connect_timeout=5, read_timeout=10, request_timeout=10, wait_timeout=10)
UNAVAILABLE = 'body management unavailable'


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate body field')
        result[key] = value
    return result


class BodyFiles:
    """Два bounded regular single-link файла в одном owned private каталоге.

    Каждый компонент открывается без follow. Каталог и опубликованные файлы
    закрепляются inode/dev identity; исчезновение/замена не означает rebind.
    """
    def __init__(self, bootstrap, context):
        paths = tuple(Path(value) for value in (bootstrap, context))
        if (any(not p.is_absolute() or '..' in p.parts for p in paths)
                or paths[0].parent != paths[1].parent or paths[0] == paths[1]):
            raise api.ClientError('body files must be distinct absolute siblings')
        self.paths, self.directory, self.identities = paths, None, {}

    def _parent(self):
        fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            for part in self.paths[0].parent.parts[1:]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd); fd = child
            info = os.fstat(fd)
            if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise ValueError()
            identity = info.st_dev, info.st_ino
            if self.directory is not None and identity != self.directory:
                raise ValueError()
            self.directory = identity
            return fd
        except BaseException:
            os.close(fd)
            raise

    def read(self):
        fd = None
        try:
            fd = self._parent()
            result = []
            for path, maximum in zip(self.paths, (65536, 16384)):
                try:
                    leaf = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=fd)
                except FileNotFoundError:
                    if path.name in self.identities:
                        raise ValueError()
                    result.append(None); continue
                try:
                    before = os.fstat(leaf)
                    if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid()
                            or before.st_nlink != 1 or stat.S_IMODE(before.st_mode) not in (0o400, 0o600)
                            or not 0 < before.st_size <= maximum):
                        raise ValueError()
                    raw = os.read(leaf, maximum+1)
                    after = os.fstat(leaf)
                    def signature(info):
                        return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns,
                            info.st_uid, info.st_mode, info.st_nlink)
                    identity = signature(after)
                    if (signature(before) != identity or len(raw) > maximum or len(raw) != after.st_size
                            or path.name in self.identities and self.identities[path.name] != identity):
                        raise ValueError()
                    value = json.loads(raw.decode('utf-8'), object_pairs_hook=_pairs,
                        parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
                    if type(value) is not dict:
                        raise ValueError()
                    self.identities[path.name] = identity
                    result.append(value)
                finally:
                    os.close(leaf)
            return result
        except FileNotFoundError:
            if self.directory is None:
                return None, None
            raise api.ClientError('private body handoff disappeared') from None
        except (OSError, ValueError, UnicodeError, RecursionError):
            raise api.ClientError('invalid private body handoff') from None
        finally:
            if fd is not None:
                os.close(fd)


class BodyManagement:
    def __init__(self, bootstrap, context, deadline, factory):
        self.files = BodyFiles(bootstrap, context)
        try:
            end = body_deadline(deadline)
        except ValueError as error:
            raise api.ClientError(str(error)) from None
        self.deadline = time.monotonic() + (end-datetime.now(timezone.utc)).total_seconds()
        self.factory = factory
        self.state, self.manager, self.bootstrap, self.context = 'waiting', None, None, None
        self.private_ca = None
        self.failure = None
        self.io_deadline = None
        self.cancelled = lambda: False
        self.stopping = threading.Event()

    def _inputs(self):
        boot, secret = self.files.read()
        if boot is None or secret is None:
            return None
        try:
            bootstrap = BodyBootstrap.parse(boot)
            context = ManagementContext.parse(secret, bootstrap.request)
            if (bootstrap.body_uid != os.geteuid()
                    or any(getattr(bootstrap, key) != getattr(context, key) for key in
                        ('installation_id', 'restore_generation', 'project_id', 'grant_id'))):
                raise ValueError()
        except (ValueError, TypeError, KeyError, OverflowError):
            raise api.ClientError('invalid body management binding') from None
        return bootstrap, context

    def _guard(self):
        if self.stopping.is_set() or self.cancelled():
            raise api.ClientError('body management request cancelled')
        if self._inputs() != (self.bootstrap, self.context):
            raise api.ClientError('body management binding changed')
        left = self.io_deadline - time.monotonic()
        if left <= 0:
            raise api.ClientError('body management I/O deadline exceeded')
        return min(BODY_LIMITS.request_timeout, left)

    def _begin(self):
        self.io_deadline = time.monotonic() + BODY_LIMITS.request_timeout
        if self.state == 'waiting':
            self.io_deadline = min(self.io_deadline, self.deadline)

    def _validate(self, current):
        self._guard()
        identity = current.identity
        if (current.epoch != str(self.context.request.scope.epoch)
                or current.scope != api.CapabilityScope(project_id=str(self.context.project_id))
                or identity.kind != 'agent' or identity.user_id != self.context.subject_id
                or identity.grant_id != str(self.context.grant_id)):
            raise api.ClientError('body management actor or installation changed')

    def _fail(self, error=None):
        if error is not None:
            reason = str(error) if isinstance(error, api.ClientError) else 'body management handoff failed'
            if self.context is not None:
                reason = reason.replace(self.context.token, '[redacted]')
            self.failure = reason[:4096]
        self.state, self.manager = 'failed', None
        if self.private_ca is not None:
            self.private_ca.cleanup(); self.private_ca = None

    def tools(self):
        if self.state == 'failed':
            return []
        self._begin()
        try:
            if self.state == 'waiting':
                if time.monotonic() >= self.deadline:
                    raise api.ClientError('body management handoff deadline exceeded')
                inputs = self._inputs()
                if inputs is None:
                    return []
                self.bootstrap, self.context = inputs
                self._guard()
                self.private_ca = tempfile.TemporaryDirectory(prefix='mop-body-ca-')
                path = Path(self.private_ca.name)/'ca.pem'
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
                with os.fdopen(fd, 'w', encoding='ascii') as stream:
                    stream.write(self.bootstrap.ca_pem)
                selected = api.ServerContext(self.bootstrap.origin, self.context.token, str(path))
                self.manager = self.factory(selected, api.CapabilityScope(project_id=str(self.context.project_id)),
                    limits=BODY_LIMITS, validate=self._validate, guard=self._guard, on_failure=self._fail)
                result = self.manager.tools()
                if time.monotonic() >= self.deadline:
                    raise api.ClientError('body management handoff deadline exceeded')
                self.state = 'active'
                return result
            self._guard()
            return self.manager.tools()
        except Exception as error:
            self._fail(error)
            return []

    def call(self, name, values):
        from mcp import types
        if self.state == 'active':
            self._begin()
            try:
                self._guard()
                # Management сам обновляет live catalogue до dispatcher. Ошибка
                # refresh должна снять binding, даже когда loud превращает её в result.
                return self.manager.call(name, values)
            except Exception as error:
                self._fail(error)
        reason = UNAVAILABLE + (': ' + self.failure if self.failure else '')
        return types.CallToolResult(isError=True, content=[types.TextContent(type='text', text=reason)])

    def close(self):
        self.stopping.set()
        self._fail()
        self.bootstrap = self.context = None


@dataclass(repr=False)
class _Job:
    method: str
    args: tuple = field(repr=False)
    result: asyncio.Future = field(repr=False)
    cancelled: threading.Event = field(default_factory=threading.Event)


class SerializedOwner:
    """Bounded очередь; отмена caller не бросает worker или private CA.

    Отменённая очередь не исполняется. Начатый запрос завершает тот же owner;
    POST никогда не replay'ится. Close ждёт текущий bounded I/O и cleanup,
    включая repeated cancellation, а не abandon_on_cancel thread.
    """
    def __init__(self, management, capacity=8):
        self.management, self.queue = management, asyncio.Queue(maxsize=capacity)
        self.closed = False
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='mop-body-management')
        self.worker = asyncio.create_task(self._work())

    async def _work(self):
        loop = asyncio.get_running_loop()
        try:
            while not self.closed:
                job = await self.queue.get()
                if job is None:
                    break
                if job.cancelled.is_set():
                    if not job.result.done(): job.result.set_result(None)
                    continue
                self.management.cancelled = job.cancelled.is_set
                try:
                    result = await loop.run_in_executor(self.executor,
                        lambda: getattr(self.management, job.method)(*job.args))
                except Exception as error:
                    if not job.result.done(): job.result.set_exception(error)
                else:
                    if not job.result.done(): job.result.set_result(result)
        finally:
            while not self.queue.empty():
                job = self.queue.get_nowait()
                if job is not None and not job.result.done():
                    job.result.set_exception(api.ClientError(UNAVAILABLE))
            await loop.run_in_executor(self.executor, self.management.close)

    async def request(self, method, *args):
        if self.closed:
            raise api.ClientError(UNAVAILABLE)
        job = _Job(method, args, asyncio.get_running_loop().create_future())
        try:
            self.queue.put_nowait(job)
        except asyncio.QueueFull:
            raise api.ClientError('body management queue is full') from None
        try:
            return await asyncio.shield(job.result)
        except asyncio.CancelledError:
            job.cancelled.set()
            # Owner ещё держит future; exception отменённого caller не должна
            # породить unhandled-future diagnostic с приватными args.
            job.result.add_done_callback(lambda f: f.exception() if not f.cancelled() else None)
            raise

    async def aclose(self):
        self.closed = True
        self.management.stopping.set()
        try:
            self.queue.put_nowait(None)
        except asyncio.QueueFull:
            pass
        cancelled = False
        while not self.worker.done():
            try:
                await asyncio.shield(self.worker)
            except asyncio.CancelledError:
                cancelled = True
        try:
            self.worker.result()
        finally:
            self.executor.shutdown(wait=True, cancel_futures=True)
        if cancelled:
            raise asyncio.CancelledError()
