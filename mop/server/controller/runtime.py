"""Composition root: короткие sync-транзакции и отдельно async effects."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import fields, is_dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from functools import partial
import io
import os
import signal
import time
from uuid import UUID

from channels.db import database_sync_to_async
from django.db import connections, transaction

SETTINGS_MODULE = 'mop.server.controller.settings'


def configure():
    """Framework включается явно, без старых config/context/.env."""
    if os.environ.get('DJANGO_SETTINGS_MODULE', SETTINGS_MODULE) != SETTINGS_MODULE:
        raise RuntimeError('controller requires its protected Django settings')
    if 'DJANGO_ALLOW_ASYNC_UNSAFE' in os.environ:
        raise RuntimeError('DJANGO_ALLOW_ASYNC_UNSAFE is forbidden')
    os.environ['DJANGO_SETTINGS_MODULE'] = SETTINGS_MODULE
    import django
    django.setup()


def materialized(value):
    """Снимок всех полей без Model/QuerySet/cursor/generator и циклов."""
    return _snapshot(value, set())


def _snapshot(value, ancestors):
    kind = type(value)
    # isinstance() читает __class__ у Django lazy proxy и тем самым запускает
    # его loader. Только обычные scalar/container значения проходят без hooks.
    if value is None or kind in (str, bytes, bool, int, float, Decimal, date, datetime, UUID):
        return value
    identity = id(value)
    if identity in ancestors:
        raise TypeError('database boundary requires acyclic materialized values')
    ancestors.add(identity)
    try:
        if is_dataclass(kind):
            if not kind.__dataclass_params__.frozen:
                raise TypeError('database boundary requires materialized frozen dataclasses')
            try:
                values = {field.name: _snapshot(object.__getattribute__(value, field.name), ancestors)
                          for field in fields(kind)}
            except AttributeError:
                raise TypeError('database boundary requires all dataclass fields to be materialized') from None
            # replace() повторяет factory/post_init и теряет состояние init=False.
            # Встроенные базовые типы с непрозрачным состоянием нельзя копировать
            # через object.__new__: явно отказываем, а не теряем это состояние.
            try:
                copy = object.__new__(kind)
                for name, item in values.items():
                    object.__setattr__(copy, name, item)
            except (TypeError, AttributeError):
                raise TypeError('database boundary requires materialized plain frozen dataclasses') from None
            return copy
        if issubclass(kind, Enum):
            metadata = object.__getattribute__(value, '__dict__')
            allowed = {'_value_', '_name_', '__objclass__', '_sort_order_', '_label_'}
            scalars = (str, bytes, bool, int, float, Decimal, date, datetime, UUID, type(None))
            if (not {'_value_', '_name_', '__objclass__'} <= metadata.keys()
                    or set(metadata) - allowed or metadata.get('__objclass__') is not kind
                    or any(type(item) not in scalars for name, item in metadata.items() if name != '__objclass__')
                    or any(base.__dict__.get('__slots__') for base in kind.__mro__)):
                raise TypeError('database boundary requires materialized plain enum values')
            if kind.__members__.get(metadata.get('_name_')) is not value:
                raise TypeError('database boundary requires materialized enum members')
            return value
        if kind is dict:
            if any(type(key) not in (str, int, bool) for key in value):
                raise TypeError('database boundary requires materialized dictionary keys')
            return {key: _snapshot(item, ancestors) for key, item in value.items()}
        if kind in (list, tuple):
            items = [_snapshot(item, ancestors) for item in value]
            return items if kind is list else tuple(items)
        raise TypeError('database boundary requires materialized values, not live ORM or I/O objects')
    finally:
        ancestors.remove(identity)


def _database_invoke(function, args, kwargs):
    try:
        return materialized(function(*args, **kwargs))
    finally:
        # Работник потока возвращает все свои соединения, даже при ошибке
        # в materialization и даже когда async caller уже отменён.
        connections.close_all()


def _observe(task):
    # Отменённый caller уже не прочтёт ошибку; владелец продолжает учитывать
    # задачу до настоящего завершения, без утечки traceback через asyncio log.
    if not task.cancelled():
        task.exception()


@contextmanager
def short_transaction(using='default', budget=10):
    """Deadline до COMMIT; при сетевом отказе соединение fenced. Без внешнего I/O.

    Служба берёт project rows по id, resource/aggregate rows по (kind, id).
    IntegrityError/CAS/lock timeout выходят вызывающему; автоматического retry
    внешнего эффекта здесь нет. Неопределённый COMMIT требует reconciliation.
    """
    if not 0 < budget <= 10:
        raise ValueError('transaction budget must be within 10 seconds')
    from .postgresql.base import transaction_deadline
    start = time.monotonic()
    # Бюджет действует на каждом driver wait, не только после yield. Python
    # без обращений к БД нельзя прервать: для него остаётся проверка на выходе.
    with transaction_deadline(budget), transaction.atomic(using=using):
        yield
        if time.monotonic() - start > budget:
            raise TimeoutError('application transaction exceeded its time budget')


class Backpressure(RuntimeError):
    """Локальный ресурс исчерпан; shared admission остаётся владельцем #625."""


class Runtime:
    """Владелец ограниченных потоков, async effects и долгоживущих adapters.

    Это не очередь операций: никакой admission/auth/claim здесь не подменяется.
    Общие project/node/resource bounds проверяет durable executor #625/#639.
    """
    def __init__(self, *, blocking_limit=8, effect_limit=8, database_limit=None, queue_limit=256, adapters=()):
        database_limit = blocking_limit if database_limit is None else database_limit
        if not 1 <= blocking_limit <= 8 or not 1 <= effect_limit <= 8 or \
                not 1 <= database_limit <= 8 or not 0 <= queue_limit <= 256:
            raise ValueError('runtime limits exceed controller safety caps')
        # Медленный Git/provider I/O не занимает потоки claim/result/renewal.
        self._executor = ThreadPoolExecutor(max_workers=blocking_limit, thread_name_prefix='mop-controller-io')
        self._database_executor = ThreadPoolExecutor(max_workers=database_limit, thread_name_prefix='mop-controller-db')
        self._blocking = asyncio.Semaphore(blocking_limit)
        self._database = asyncio.Semaphore(database_limit)
        self._effects = asyncio.Semaphore(effect_limit)
        self._capacity = blocking_limit + database_limit + effect_limit + queue_limit
        self._blocking_capacity = blocking_limit + queue_limit
        self._database_capacity = database_limit + queue_limit
        self._effect_capacity = effect_limit + queue_limit
        self._counts = {'blocking': 0, 'database': 0, 'effect': 0}
        self._tasks = set()
        self._adapters = tuple(adapters)
        self._started_adapters = []
        self._started = False
        self._stopped = False
        self._closed = False
        self._cleanup = None
        self._closing_adapter = None

    @property
    def accepting(self):
        return not self._stopped

    @property
    def pending(self):
        return sum(self._counts.values())

    def stop_accepting(self):
        """Остановить новые claims, сохранив owned result/cleanup lane."""
        self._stopped = True

    async def start(self):
        if self._stopped:
            raise RuntimeError('runtime is stopped')
        if self._started:
            return
        try:
            for adapter in self._adapters:
                # Частично запущенный adapter тоже обязан поддерживать cleanup.
                self._started_adapters.append(adapter)
                await adapter.start()
            self._started = True
        except BaseException:
            await self.aclose()
            raise

    async def _submit(self, kind, semaphore, capacity, function):
        # Уже принадлежащий Runtime эффект должен записать result при drain.
        # Новые внешние callers после stop по-прежнему получают отказ.
        if self._stopped and (kind != 'database' or asyncio.current_task() not in self._tasks):
            raise RuntimeError('runtime is stopped')
        if self._counts[kind] >= capacity or self.pending >= self._capacity:
            raise Backpressure('controller runtime capacity exhausted')
        self._counts[kind] += 1
        async def execute():
            try:
                async with semaphore:
                    return await function()
            finally:
                self._counts[kind] -= 1
        task = asyncio.create_task(execute())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        task.add_done_callback(_observe)
        # Отмена caller завершается сразу, но не отменяет работу и не отдаёт
        # её слот. Бесконечный CPU callback оставит честный incomplete shutdown.
        return await asyncio.shield(task)

    async def database(self, function, *args, **kwargs):
        """Единственная async DB-граница: целая служба в owned bounded worker."""
        args, kwargs = materialized(args), materialized(kwargs)
        call = database_sync_to_async(_database_invoke, thread_sensitive=False, executor=self._database_executor)
        return await self._submit('database', self._database, self._database_capacity,
                                  lambda: call(function, args, kwargs))

    async def blocking(self, function, *args, **kwargs):
        # Только adapters с собственными timeout: поток Python нельзя убить.
        async def call():
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(self._executor, partial(function, *args, **kwargs))
        return await self._submit('blocking', self._blocking, self._blocking_capacity, call)

    async def effect(self, function, *args, **kwargs):
        return await self._submit('effect', self._effects, self._effect_capacity,
                                  lambda: function(*args, **kwargs))

    async def aclose(self, timeout=10):
        if self._closed:
            return
        self._stopped = True
        if self._tasks:
            _, pending = await asyncio.wait(tuple(self._tasks), timeout=timeout)
            if pending:
                # Не закрывать adapters под ещё живым эффектом и не освобождать
                # его слот. Следующий shutdown может дождаться quiescence.
                raise RuntimeError('controller shutdown incomplete: work is still running')
        while self._started_adapters:
            if self._closing_adapter is None:
                self._closing_adapter = asyncio.ensure_future(self._started_adapters[-1].aclose())
                self._closing_adapter.add_done_callback(_observe)
            # wait_for отменяет coroutine и ждёт acknowledgement без верхней
            # границы. Здесь cleanup остаётся owned и продолжается при retry.
            _, pending = await asyncio.wait((self._closing_adapter,), timeout=timeout)
            if pending:
                raise RuntimeError('controller shutdown incomplete: adapter cleanup is still running')
            try:
                self._closing_adapter.result()
            except Exception as exc:
                self._closing_adapter = None
                raise RuntimeError('controller adapter shutdown failed: ' + type(exc).__name__) from None
            self._closing_adapter = None
            self._started_adapters.pop()
        if self._cleanup is None:
            self._cleanup = asyncio.get_running_loop().run_in_executor(self._executor, close_pools)
            self._cleanup.add_done_callback(_observe)
        _, pending = await asyncio.wait((self._cleanup,), timeout=timeout)
        if pending:
            raise RuntimeError('controller shutdown incomplete: database cleanup is still running')
        try:
            self._cleanup.result()
        except Exception:
            # Пул сохранил живых workers; после их stop следующий drain может
            # повторить cleanup, а не навсегда читать старую failed future.
            self._cleanup = None
            raise
        self._executor.shutdown(wait=True, cancel_futures=True)
        self._database_executor.shutdown(wait=True, cancel_futures=True)
        self._closed = True


def _maintenance_role():
    from django.conf import settings
    if settings.BOOTSTRAP['role'] != 'maintenance':
        raise RuntimeError('this action requires the separate maintenance DB principal')


def _guard_row(using, *, lock=False):
    from django.db import DatabaseError
    from .models import RestoreGuard
    if lock and not connections[using].in_atomic_block:
        raise RuntimeError('restore guard locking requires an application transaction')
    rows = RestoreGuard.objects.using(using)
    if lock:
        rows = rows.select_for_update()
    try:
        return rows.get(pk=1)
    except RestoreGuard.DoesNotExist as exc:
        raise RuntimeError('controller restore guard is missing; maintenance required') from exc
    except DatabaseError as exc:
        raise RuntimeError(f'controller restore guard is unavailable: {type(exc).__name__}; maintenance required') from exc


def require_restore_ready(using='default', *, lock=False):
    """Дешёвый fence для startup/admission/claims; не разрешает effects сам по себе.

    lock=True вызывается внутри короткой транзакции перед project/resource locks.
    Внешний I/O под этой блокировкой запрещён. Поколение проверяется потребителем
    вместе с epoch; восстановление требует остановленных служб.
    """
    guard = _guard_row(using, lock=lock)
    if guard.state != 'ready':
        raise RuntimeError('controller restore guard is not ready; maintenance recovery required')
    return str(guard.generation)


def operation_installation_state():
    """SQL-only reader внутри caller transaction: guard раньше любых domain locks."""
    from .models import Installation
    from ..operations.service import InstallationState
    generation = UUID(require_restore_ready(lock=True))
    try:
        row = Installation.objects.select_for_update().get(pk=1)
    except Installation.DoesNotExist as error:
        raise RuntimeError('controller installation is not initialized') from error
    return InstallationState(row.epoch, row.effects_enabled, generation)


def _composed_operations(*, transaction_scope, grants=None, executor=None, targets=None, restart_support=None, queue_limit=256, project_limit=64, execution_limit=8):
    from ..operations.service import Operations
    from ..builds.service import Builds
    service = Operations(installation_state=operation_installation_state, transaction_scope=transaction_scope,
        grants=grants, executor=executor, targets=targets, restart_support=restart_support, queue_limit=queue_limit,
        project_limit=project_limit, execution_limit=execution_limit)
    service.builds = Builds(service)
    return service


def operations_service(*, grants=None, executor=None, targets=None, restart_support=None,
                       queue_limit=256, project_limit=64, execution_limit=8):
    """Typed composition durable Operations с настоящим restore fence и DB budget."""
    return _composed_operations(transaction_scope=short_transaction, grants=grants, executor=executor,
        targets=targets, restart_support=restart_support, queue_limit=queue_limit,
        project_limit=project_limit, execution_limit=execution_limit)


@contextmanager
def build_transaction():
    """Долгий blocking build не удерживает idle connection общего controller pool."""
    connection = connections['default']
    outer = not connection.in_atomic_block
    try:
        with short_transaction():
            yield
    finally:
        if outer:
            connection.close()


def builds_service(*, scheduler=None, image_builder=None, node_executor=None, grants=None):
    """Trusted named ports: HTTP получает только admission/status, не adapters."""
    from ..builds.service import Builds
    operations = _composed_operations(transaction_scope=build_transaction, grants=grants)
    builds = Builds(operations, scheduler=scheduler, image_builder=image_builder, node_executor=node_executor)
    operations.builds = builds
    return builds


def privileged_build_executor(*, path, caller_uid, image_builder, node_executor, grants=None):
    """Private OS service: только named ports, scoped SQL callbacks и safe refusals."""
    from ..access.services import Denied
    from ..operations.service import Refused
    from ..builds.executor import PrivilegedExecutor
    builds = builds_service(grants=grants)
    return PrivilegedExecutor(path=path, caller_uid=caller_uid, resolve=builds.resolve_ticket,
        record=builds._record, image_builder=image_builder, node_executor=node_executor,
        safe_refusals=(Denied, Refused))


def assignments_service(**options):
    """Root связывает shared target fence без обратного domain import."""
    from ..sessions.services import Assignments
    from ..operations.resources import target_held
    return Assignments(target_fence=target_held, **options)


def machine_execution_service(**options):
    """NodeService scope не даёт прав privileged image executor."""
    from ..sessions.services import MachineExecution
    from ..operations.resources import target_held
    return MachineExecution(target_fence=target_held, **options)


def installation_snapshot(using='default'):
    from .models import Installation
    require_restore_ready(using)
    value = Installation.objects.using(using).values('identity', 'epoch', 'effects_enabled').first()
    if value is None:
        raise RuntimeError('controller installation is not initialized')
    return {**value, 'epoch': str(value['epoch'])}


def _schema_ready(using):
    from django.db.migrations.executor import MigrationExecutor
    executor = MigrationExecutor(connections[using])
    if executor.migration_plan(executor.loader.graph.leaf_nodes()):
        raise RuntimeError('controller schema requires migrations')


def _verify_installation_state(using='default'):
    """Проверка содержимого, в том числе пока guard ещё закрыт для restore."""
    from django.conf import settings
    from .bootstrap import root_digest
    from .models import Installation
    _schema_ready(using)
    row = Installation.objects.using(using).first()
    if row is None:
        raise RuntimeError('controller installation is not initialized')
    if row.identity != settings.BOOTSTRAP['installation_id']:
        raise RuntimeError('controller installation identity does not match bootstrap')
    if settings.BOOTSTRAP['role'] in {'maintenance', 'credential'} and row.root_digest != root_digest(settings.BOOTSTRAP):
        raise RuntimeError('controller root key does not match initialized trust')
    return {'identity': row.identity, 'epoch': str(row.epoch), 'effects_enabled': row.effects_enabled}


def verify_installation(using='default'):
    require_restore_ready(using)
    return _verify_installation_state(using)


def initialize(using='default'):
    from django.conf import settings
    from .bootstrap import root_digest
    from .models import Installation
    _maintenance_role()
    _schema_ready(using)
    digest = root_digest(settings.BOOTSTRAP)
    with short_transaction(using):
        guard = _guard_row(using, lock=True)
        exists = Installation.objects.using(using).filter(pk=1).exists()
        if guard.state == 'uninitialized' and not exists:
            Installation.objects.using(using).create(
                pk=1, identity=settings.BOOTSTRAP['installation_id'], root_digest=digest)
            guard.state = 'ready'
            guard.save(using=using, update_fields=['state'])
        elif guard.state != 'ready' or not exists:
            raise RuntimeError('controller restore guard requires maintenance recovery, not initialization')
    return verify_installation(using)


def create_admin(username, password, using='default'):
    from django.contrib.auth import get_user_model
    from django.contrib.auth.password_validation import validate_password
    from .models import Installation
    _maintenance_role()
    verify_installation(using)
    User = get_user_model()
    with short_transaction(using):
        Installation.objects.using(using).select_for_update().get(pk=1)
        if User.objects.using(using).filter(is_superuser=True).exists():
            raise RuntimeError('initial administrator already exists; use authorized account recovery')
        if User.objects.using(using).filter(username=username).exists():
            raise RuntimeError('administrator username already exists')
        user = User(username=username, is_staff=True, is_superuser=True, is_active=True)
        user.full_clean(exclude=['password'])
        validate_password(password, user)
        user.set_password(password)
        user.save(using=using)
        from mop.server.access.services import bind_local_identity
        bind_local_identity(user.pk)
    return {'username': user.username}


def close_pools():
    """Вызывается после quiescence на shutdown, не после каждого request."""
    connections.close_all()
    for connection in connections.all():
        if hasattr(connection, 'close_pool'):
            connection.close_pool()


def maintenance(action, *, username=None, file=None):
    from django.core.exceptions import ValidationError
    from django.core.management import call_command, CommandError
    from django.db import DatabaseError
    output = io.StringIO()
    try:
        if action not in {'check', 'migrations-check'}:
            _maintenance_role()
        if action in {'migrate', 'migrations-check'}:
            command = 'migrate' if action == 'migrate' else 'makemigrations'
            options = {'interactive': False} if action == 'migrate' else {'check': True, 'dry_run': True}
            call_command(command, stdout=output, stderr=output, verbosity=0, **options)
        elif action == 'initialize':
            initialize()
        elif action == 'check':
            call_command('check', databases=['default'], stdout=output, stderr=output, verbosity=0)
            verify_installation()
        elif action in {'backup', 'restore'}:
            from .backup import backup, restore
            if not file:
                raise RuntimeError(f'{action} requires --file')
            (backup if action == 'backup' else restore)(file)
        else:
            raise RuntimeError('unsupported controller maintenance action')
    except (CommandError, ValidationError) as exc:
        # Это сообщения проверок/валидаторов, не сырые DSN и не SQL driver.
        raise RuntimeError(f'controller {action} refused: {exc}') from exc
    except DatabaseError as exc:
        code = getattr(exc.__cause__, 'sqlstate', None)
        detail = f' (SQLSTATE {code})' if code else ''
        raise RuntimeError(f'controller database refused {action}: {type(exc).__name__}{detail}') from exc
    finally:
        close_pools()


def validate_asgi_startup():
    from django.conf import settings
    if settings.BOOTSTRAP['role'] not in {'web', 'gateway'}:
        raise RuntimeError('ASGI requires a web/gateway bootstrap, not maintenance credentials')
    maintenance('check')
    # Смена controller run — startup mutation, не скрытый repair из HTTP GET.
    from mop.server.sessions.projection_writes import ProjectionWriter
    ProjectionWriter.begin_run(UUID(installation_snapshot()['epoch']))


def serve(host, port, *, hostnames=(), tls_cert=None, tls_key=None):
    import ipaddress
    import re
    from pathlib import Path
    from django.conf import settings
    from .bootstrap import private_bytes
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = host == 'localhost'
    if bool(tls_cert) != bool(tls_key) or not loopback and not tls_cert:
        raise RuntimeError('native TLS certificate and key are required for a public listener')
    if not 1 <= port <= 65535:
        raise RuntimeError('ASGI port must be within 1..65535')
    names = list(hostnames or [host])
    if len(names) > 16 or any(not isinstance(name, str) or len(name) > 253 or
                              not re.fullmatch(r'[A-Za-z0-9.\[\]:-]+', name) for name in names):
        raise RuntimeError('ASGI requires explicit allowed hostnames without wildcards')
    validate_asgi_startup()
    if tls_key:
        private_bytes(tls_key)
        if not Path(tls_cert).is_file():
            raise RuntimeError('TLS certificate file is unavailable')
    settings.ALLOWED_HOSTS = names
    import uvicorn
    uvicorn.run('mop.server.controller.asgi:application', host=host, port=port,
                workers=1, lifespan='on', limit_concurrency=16, timeout_graceful_shutdown=10,
                proxy_headers=False, server_header=False, ssl_certfile=tls_cert, ssl_keyfile=tls_key)


def compose_worker(runtime, *, node_executor=None, executor=None, scheduler=None,
                   image_builder=None, build_node_executor=None):
    """Только доверенные named ports; нет legacy config, fake или общего execute.

    Node eligibility требует отдельно enrolled service для текущей authority.
    Build ports сохраняют приватный executor #648; root не достаёт hypervisor
    credentials в web/worker. Физические fenced clients и OS enrollment задаёт
    installation root: без них standalone worker явно отказывает на startup.
    Restart потребляется только machine HTTP, не этим controller poll.
    """
    from ..access.context import ActorContext, PrincipalKind
    from ..access.grants import Grants
    from ..operations.worker import Worker
    from .machinebinding import AssignmentBindings

    node_bound = node_executor is not None
    if node_bound != (executor is not None):
        raise RuntimeError('node executor and enrolled service authority are required together')
    if node_bound and (type(executor) is not ActorContext or executor.kind is not PrincipalKind.SERVICE
                       or executor.node_id is None):
        raise RuntimeError('enrolled node executor authority required')
    if node_bound and not asyncio.iscoroutinefunction(getattr(node_executor, 'set_eligibility', None)):
        raise RuntimeError('node executor requires a bound managed async set_eligibility capability')
    build_ports = (scheduler, image_builder, build_node_executor)
    build_bound = any(port is not None for port in build_ports)
    if build_bound and any(port is None for port in build_ports):
        raise RuntimeError('privileged build ports are not fully configured')
    if not node_bound and not build_bound:
        raise RuntimeError('controller worker capabilities are not configured: fenced node eligibility or privileged build ports required')

    # Только SQL authority: credential/owner/approval перечитываются каждым
    # use case. Provider secret I/O не вызывается под application transaction.
    authority = Grants(provider=None, bindings=AssignmentBindings()).local_authority()
    operations = operations_service(grants=authority, executor=executor)
    builds = None
    if build_bound:
        builds = builds_service(scheduler=scheduler, image_builder=image_builder,
            node_executor=build_node_executor, grants=authority)
        builds._preflight()
    return Worker(runtime, operations, node_executor, image_builds=builds,
        node_id=executor.node_id if node_bound else None)


async def _drain_worker(runtime, batch, stopped, timeout):
    # Batch остаётся owned до результата: его cancel скрыл бы ошибку эффекта,
    # который Runtime shield продолжает исполнять. Новые claims уже остановлены.
    deadline = asyncio.get_running_loop().time() + timeout
    if stopped is not None:
        stopped.cancel()
        await asyncio.gather(stopped, return_exceptions=True)
    try:
        if batch is not None:
            _, pending = await asyncio.wait((batch,), timeout=max(0, deadline-asyncio.get_running_loop().time()))
            if pending:
                raise RuntimeError('controller shutdown incomplete: worker pass is still running')
            batch.result()
    finally:
        # Не закрывать adapters и не освобождать slots под живым эффектом.
        await runtime.aclose(timeout=max(0, deadline-asyncio.get_running_loop().time()))


async def run_worker(stop, runtime, *, worker=None, poll_interval=.5, batch_limit=8, shutdown_timeout=10):
    """Один bounded durable проход за раз; wake loss покрывает следующий poll.

    Cleanup принадлежит root даже при повторной отмене caller. Если внешний I/O
    ещё жив после бюджета, Runtime оставляет tasks/slots/adapters owned и выдаёт
    честный incomplete shutdown; stale lease не доказывает quiescence.
    """
    import math
    batch = stopped = None
    drain_timeout = 10
    try:
        if type(batch_limit) is not int or not 1 <= batch_limit <= 8:
            raise ValueError('worker batch exceeds controller safety caps')
        if type(poll_interval) not in (int, float) or not math.isfinite(poll_interval) or not 0 < poll_interval <= 30:
            raise ValueError('worker poll interval must be within 30 seconds')
        if type(shutdown_timeout) not in (int, float) or not math.isfinite(shutdown_timeout) or not 0 < shutdown_timeout <= 10:
            raise ValueError('worker shutdown budget must be within 10 seconds')
        drain_timeout = shutdown_timeout
        await runtime.start()
        worker = compose_worker(runtime) if worker is None else worker
        stopped = asyncio.create_task(stop.wait())
        while not stop.is_set():
            batch = asyncio.create_task(worker.run_pending(limit=batch_limit))
            batch.add_done_callback(_observe)  # Поздняя ошибка после incomplete drain уже наблюдена.
            done, _ = await asyncio.wait((batch, stopped), return_when=asyncio.FIRST_COMPLETED)
            if batch in done:
                batch.result()  # Discovery/DB отказ не становится успешным idle loop.
            if stop.is_set():
                break
            batch = None
            # Даже пустая очередь не вызывает busy spin; Event не теряется
            # между двумя проходами и не зависит от ephemeral notification.
            await asyncio.wait((stopped,), timeout=poll_interval)
    finally:
        runtime.stop_accepting()
        cleanup = asyncio.create_task(_drain_worker(runtime, batch, stopped, drain_timeout))
        cancelled = False
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                cancelled = True
        cleanup.result()
        if cancelled:
            raise asyncio.CancelledError


def worker():
    from django.conf import settings
    if settings.BOOTSTRAP['role'] != 'worker':
        raise RuntimeError('worker requires its restricted runtime DB principal')
    maintenance('check')
    async def run():
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for name in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(name, stop.set)
        try:
            await run_worker(stop, Runtime())
        finally:
            for name in (signal.SIGTERM, signal.SIGINT):
                loop.remove_signal_handler(name)
    asyncio.run(run())
