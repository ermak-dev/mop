"""Связь нового body launch с finite executor #640 и telemetry #641.

Source/physical ports принадлежат доверенному root. Нет discovery по старым
markers, произвольного argv/write или подмены SessionApi самодельным протоколом.
До поддержки current scope в SessionApi production observation отказывает.
"""
import asyncio
from dataclasses import dataclass
import os
from pathlib import Path
import secrets
from typing import Protocol
from uuid import UUID, uuid4

from mop.common.telemetry import SessionScope
from mop.driver.launch import LaunchResponse, MachineConfig, RegisteredLayout, Registration
from mop.node.execution_adapters import (
    ActionInput, BoundTarget, SessionProof, process_identity, check_session_peer,
)
from mop.node.http_execution import Refused, Unavailable, Work


class LaunchSource(Protocol):
    async def fetch(self, work: Work) -> LaunchResponse:
        """Authenticated current addressed query, revalidating approval и grant."""
        ...


class LaunchQuery(Protocol):
    async def read_launch(self, metadata: dict) -> dict:
        """Root-bound authenticated API; не новый transport/RPC из node."""
        ...


class AddressedLaunchSource:
    def __init__(self, registration, *, query: LaunchQuery | None = None):
        if type(registration) is not Registration:
            raise Refused('trusted launch registration required')
        self.registration, self.query = registration, query

    async def fetch(self, work):
        if not self.registration.matches(work):
            raise Refused('foreign addressed launch query')
        if self.query is None:
            raise Unavailable('authenticated addressed launch API is not bound')
        async with asyncio.timeout(5):
            value = await self.query.read_launch(work.wire())
        try:
            return LaunchResponse.parse(value, self.registration, input_factory=ActionInput)
        except ValueError as error:
            raise Refused(str(error)) from error


class PhysicalSessions(Protocol):
    async def observe(self, layout: RegisteredLayout) -> SessionProof | None: ...
    async def reconcile(self, node_id: str, previous_epoch: str, new_epoch: str) -> bool:
        """Инвентаризация/завершение старых process scopes, не HTTP ACK."""
        ...


class UnboundSessionApi:
    async def observe(self, layout):
        raise Unavailable('SessionApi current launch scope capability is not bound')

    async def reconcile(self, node_id, previous_epoch, new_epoch):
        raise Unavailable('physical executor scope reconciliation is not bound')


class SessionApiProbe:
    """Дополняет addressed SessionApi proof проверкой настоящего local process.

    Это не реализация нового SessionApi: current release его scope ещё не
    умеет. Factory проверяет capability до любых replacement effects.
    Container composition обязана стоять внутри нужного namespace.
    """
    def __init__(self, session_api, *, reconciliation=None):
        if getattr(session_api, 'supports_launch_scope', None) is not True:
            raise Unavailable('SessionApi current launch scope capability is unsupported')
        self.session_api, self.reconciliation = session_api, reconciliation

    async def observe(self, layout):
        async with asyncio.timeout(5):
            proof = await self.session_api.observe(layout)
        if (type(proof) is not SessionProof or proof.registration != layout.registration
                or proof.session_socket != layout.runtime/'session.sock'):
            raise Unavailable('SessionApi returned a foreign physical launch scope')
        writer = None
        try:
            if process_identity(proof.pid) != proof.process_start:
                raise Unavailable('physical session process changed')
            async with asyncio.timeout(.5):
                _, writer = await asyncio.open_unix_connection(str(proof.session_socket))
                check_session_peer(writer.get_extra_info('socket'), proof, layout.clone)
            return proof
        except (OSError, TimeoutError) as error:
            raise Unavailable('physical session socket or process unavailable') from error
        finally:
            if writer is not None:
                writer.close()
                await writer.wait_closed()

    async def reconcile(self, node_id, previous_epoch, new_epoch):
        # Результат должен прийти от владельца process scopes (cgroup/namespace),
        # не от server restarted ACK и не от отсутствия одного pane PID.
        if self.reconciliation is None:
            raise Unavailable('physical executor scope reconciliation is not bound')
        return await self.reconciliation.confirm(node_id, previous_epoch, new_epoch) is True


class BindingsResolver:
    def __init__(self, layout, *, source: LaunchSource, physical: PhysicalSessions | None = None):
        if type(layout) is not RegisteredLayout:
            raise Refused('trusted registered layout required')
        self.layout, self.source = layout, source
        self.physical = physical if physical is not None else UnboundSessionApi()

    async def proof(self, work):
        if not self.layout.registration.matches(work):
            raise Refused('work does not match this registered launch')
        async with asyncio.timeout(5):
            proof = await self.physical.observe(self.layout)
        if type(proof) is not SessionProof or proof.registration != self.layout.registration:
            raise Unavailable('current physical session scope unavailable')
        return proof

    async def resolve(self, work):
        if not self.layout.registration.matches(work):
            raise Refused('work does not match this registered launch')
        async with asyncio.timeout(5):
            response = await self.source.fetch(work)
        if type(response) is not LaunchResponse:
            raise Refused('typed addressed launch response required')
        response.check(self.layout.registration)
        if type(response.inputs) is not ActionInput:
            raise Refused('typed addressed action input required')
        response.inputs.check(work.action)
        proof = await self.proof(work)
        target = BoundTarget(work.target_id, work.incarnation_id, work.node_id,
            work.executor_epoch, work.generation, work.role, work.bundle_digest, work.grant_id,
            self.layout.clone, self.layout.tmux_socket, proof.pane, self.layout.launcher,
            proof.session_socket, proof)
        target.check(work)
        return target, response.inputs

    async def reconcile(self, node_id, previous_epoch, new_epoch):
        if node_id != self.layout.registration.work.node_id:
            raise Refused('foreign executor reconciliation')
        async with asyncio.timeout(10):
            return await self.physical.reconcile(node_id, previous_epoch, new_epoch) is True


@dataclass(frozen=True)
class ReadyReceipt:
    registration: Registration
    scope: SessionScope
    pid: int
    process_start: str
    receipt_id: str
    model: str | None


class ReadinessSource(Protocol):
    async def ready(self, work: Work, proof: SessionProof) -> ReadyReceipt:
        """Только committed addressed receipt текущего процесса и model hook."""
        ...


class ReadyConfirmation:
    def __init__(self, resolver: BindingsResolver, source: ReadinessSource):
        self.resolver, self.source = resolver, source

    async def confirmed(self, work, target):
        target.check(work)
        proof = await self.resolver.proof(work)
        layout = self.resolver.layout
        if (target.clone != layout.clone or target.tmux_socket != layout.tmux_socket
                or target.launcher != layout.launcher or target.pane != proof.pane
                or target.session_socket != proof.session_socket or target.session_proof != proof):
            raise Refused('readiness target differs from registered physical layout')
        async with asyncio.timeout(5):
            receipt = await self.source.ready(work, proof)
        if (type(receipt) is not ReadyReceipt or receipt.registration != layout.registration
                or receipt.scope != proof.scope or receipt.pid != proof.pid
                or receipt.process_start != proof.process_start
                or type(receipt.model) is not str or not 1 <= len(receipt.model) <= 128
                or any(ord(c) < 32 or ord(c) == 127 for c in receipt.model)):
            return False
        try:
            if type(receipt.receipt_id) is not str or str(UUID(receipt.receipt_id)) != receipt.receipt_id:
                return False
        except ValueError:
            return False
        # HTTP ACK не переживает смену процесса во время await.
        return await self.resolver.proof(work) == proof


class ProducerRegistration(Protocol):
    async def register(self, proof: SessionProof) -> SessionScope:
        """Root #625 регистрирует новый producer через Sessions public use case.

        Никакого вывода currentSession из hook или caller-selected session id.
        """
        ...


class _CurrentProducerTelemetry:
    """Queued hook не переживает смену native identity перед HTTP ingest."""
    def __init__(self, runtime):
        self.runtime = runtime

    async def ingest(self, row):
        await self.runtime._current()
        return await self.runtime.telemetry.ingest(row)


class SessionRuntime:
    """Один finite producer: prepare -> exec с env -> attach/ACK -> active.

    Prepare выдаёт ephemeral OS credential, но не scope и не listener. Root
    обязан передать environment в native exec, затем вызвать attach. Startup
    hooks тихо теряются обычным stdlib hook, не буферизуются как readiness.
    Observer только ищет explicit attachment; epoch создаётся здесь один раз.
    Нет authority-файла, native scope API, model/retirement capability или
    перехода между work DTO: новый operation/step требует отдельной root wiring.
    """
    def __init__(self, registration: ProducerRegistration, telemetry):
        self.registration, self.telemetry = registration, telemetry
        self._state = 'new'
        self._layout = self._path = self._token = self._epoch = None
        self._identity = self._proof = self._bridge = None
        self._attachment = self._cleanup = None

    def prepare(self, layout, *, path):
        from mop import session
        if self._state != 'new':
            raise Unavailable('session producer runtime already prepared or closed')
        if (type(layout) is not RegisteredLayout or not isinstance(path, Path)
                or not path.is_absolute() or '..' in path.parts
                or len(os.fsencode(path)) > 100):
            raise Refused('registered layout and bounded private IPC path required')
        # No-follow проверка всех компонентов, до выдачи env ребёнку. Ничего
        # не пишем на диск; directory создаёт доверенный OS composition root.
        fd = session.identity_open(str(path.parent), directory=True)
        try:
            info = os.fstat(fd)
            if info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise Refused('private owned hook runtime directory required')
        finally:
            os.close(fd)
        if os.path.lexists(path):
            raise Refused('hook endpoint already exists')
        self._layout, self._path = layout, path
        self._token, self._epoch = secrets.token_hex(32), uuid4()
        self._state = 'prepared'
        return self.environment()

    def environment(self):
        if self._state not in ('prepared', 'attaching', 'active'):
            raise Unavailable('session producer environment is unavailable')
        return dict(MOP_TELEMETRY_SOCKET=str(self._path), MOP_TELEMETRY_TOKEN=self._token)

    async def producer_epoch(self, layout, identity):
        """Lookup для NativePhysicalSessions: никогда не attach/mint на observe."""
        if (self._state not in ('attaching', 'active') or self._identity is None
                or layout != self._layout):
            return None
        if identity != self._identity:
            self._begin_close()
            return None
        return self._epoch

    async def attach(self):
        if self._state != 'prepared':
            raise Unavailable('session producer runtime is not prepared or already attached')
        self._state = 'attaching'
        # Отдельная owned task позволяет close прервать registration/start;
        # cleanup ждёт её окончания, прежде чем убирать свой listener.
        self._attachment = asyncio.create_task(self._attach())
        try:
            return await self._attachment
        except BaseException:
            await asyncio.shield(self._begin_close())
            raise

    async def _attach(self):
        from mop.node.native_observer import NativePhysicalSessions, _snapshot
        from mop.node.telemetry import HookBridge
        # Explicit lifecycle attachment, не побочный эффект producer lookup.
        # Snapshot -- уже существующий read-only native observer, не новый
        # протокол scope и не currentSession из caller/hook JSON.
        async with asyncio.timeout(5):
            self._identity = await asyncio.to_thread(_snapshot, self._layout)
        physical = NativePhysicalSessions(owner=self)
        proof = await physical.observe(self._layout)
        async with asyncio.timeout(5):
            scope = await self.registration.register(proof)
        if type(scope) is not SessionScope or scope != proof.scope:
            raise Refused('producer registration scope changed')
        if await physical.observe(self._layout) != proof:
            raise Unavailable('native producer changed during registration')
        self._proof = proof
        runtime = self

        class ScopedHookBridge(HookBridge):
            def accept(self, record, *, token):
                # start() может await-ить: listener ещё не означает activation.
                if runtime._state != 'active':
                    return False
                return super().accept(record, token=token)

        bridge = ScopedHookBridge(_CurrentProducerTelemetry(self), scope,
                                  path=str(self._path), token=self._token)
        self._bridge = bridge
        await bridge.start()
        if await physical.observe(self._layout) != proof or self._state != 'attaching':
            raise Unavailable('native producer changed before hook activation')
        self._state = 'active'
        return bridge

    async def _current(self):
        from mop.node.native_observer import NativePhysicalSessions
        if self._state != 'active':
            raise Unavailable('session producer bridge is inactive')
        try:
            if await NativePhysicalSessions(owner=self).observe(self._layout) != self._proof:
                raise Unavailable('session producer identity changed')
            if self._state != 'active':
                raise Unavailable('session producer bridge is inactive')
        except (Refused, Unavailable):
            self._begin_close()
            raise

    def _begin_close(self):
        # Немедленный gate для accept/lookup, cleanup не ждёт caller/worker,
        # который мог сам обнаружить смену identity. Repeated cancel не оставит
        # бесхозный start/socket; внешняя aclose может снова дождаться cleanup.
        self._state = 'closed'
        self._token = self._identity = None
        if self._cleanup is None:
            self._cleanup = asyncio.create_task(self._close())
            self._cleanup.add_done_callback(
                lambda done: None if done.cancelled() else done.exception())
        return self._cleanup

    async def _close(self):
        if self._attachment is not None:
            self._attachment.cancel()
            await asyncio.gather(self._attachment, return_exceptions=True)
        if self._bridge is not None:
            await self._bridge.aclose()

    async def aclose(self):
        await asyncio.shield(self._begin_close())


class BootstrapPorts(Protocol):
    supports_bootstrap: bool

    async def image(self, node_id: str, digest: str) -> bool:
        """#648 проверяет фактический образ на выбранном узле, не строку ORM."""
        ...
    async def route(self, machine: MachineConfig) -> bool:
        """Контроллер проверяет достижимость bootstrap SSH, включая PVE routes."""
        ...
    async def admit(self, registration: Registration, allow: bool) -> None:
        """Адресный временный SSH ключ; отзыв не зависит от успеха bootstrap."""
        ...
    async def bootstrap(self, registration: Registration, machine: MachineConfig) -> str:
        """#669/Preparation исполняет approved bytes и возвращает exact revision."""
        ...


class BootstrapDiscovery:
    def __init__(self, machine, registration, ports: BootstrapPorts):
        if type(machine) is not MachineConfig or type(registration) is not Registration:
            raise Refused('explicit machine and registered bootstrap pins required')
        if (getattr(ports, 'supports_bootstrap', None) is not True
                or any(not callable(getattr(ports, name, None)) for name in
                       ('image', 'route', 'admit', 'bootstrap'))):
            raise Unavailable('bootstrap capabilities are not bound')
        self.machine, self.registration, self.ports = machine, registration, ports
        self._started = self._closed = False
        self._caller = self._revocation = None

    async def run(self):
        if self._started or self._closed:
            raise Unavailable('bootstrap discovery already started or closed')
        self._started = True
        self._caller = asyncio.current_task()
        try:
            return await self._run()
        finally:
            self._caller = None

    async def _run(self):
        r, p = self.registration, self.ports
        async with asyncio.timeout(10):
            if await p.image(r.work.node_id, r.image_digest) is not True:
                raise Unavailable('external image availability not confirmed')
            if await p.route(self.machine) is not True:
                raise Unavailable('bootstrap SSH route not confirmed')
        # Даже lost admission ACK мог оставить ключ. Отзыв обязателен уже
        # после начала попытки admission, а не только после успешного ответа.
        try:
            async with asyncio.timeout(10):
                await p.admit(r, True)
            async with asyncio.timeout(300):
                revision = await p.bootstrap(r, self.machine)
            if revision != r.bootstrap_revision:
                raise Refused('bootstrap receipt revision does not match registration')
            return revision
        except asyncio.CancelledError:
            self._start_revocation()
            raise
        finally:
            # Только пойманная в этом запуске отмена возвращается немедленно.
            # Исторический Task.cancelling() не доказывает отзыв admission.
            if self._revocation is None:
                self._start_revocation()
                await asyncio.shield(self._revocation)

    def _start_revocation(self):
        # Ссылка и результат принадлежат discovery, не отменённому caller.
        # После cancellation supervisor обязан вызвать aclose; исключение
        # остаётся в задаче для aclose даже если caller уже ушёл.
        self._revocation = asyncio.create_task(self._revoke())
        self._revocation.add_done_callback(
            lambda done: None if done.cancelled() else done.exception())

    async def aclose(self, *, timeout=10):
        """Ограниченное ожидание отзыва; незавершённый effect остаётся owned.

        Timeout/cancellation не подтверждают физический отзыв. Ошибка хранится
        в задаче и повторный aclose возвращает тот же отказ, а не ложный успех.
        """
        if type(timeout) not in (int, float) or not 0 < timeout <= 10:
            raise ValueError('bootstrap close timeout must be within (0, 10] seconds')
        self._closed = True
        deadline = asyncio.get_running_loop().time() + timeout
        caller = self._caller
        if caller is asyncio.current_task():
            raise Unavailable('bootstrap revocation not confirmed; run is still active')
        if caller is not None:
            caller.cancel()
            _, pending = await asyncio.wait({caller}, timeout=timeout)
            if pending:
                raise Unavailable('bootstrap revocation not confirmed; bootstrap is still active')
        task = self._revocation
        if task is None:
            return  # Admission ещё не начинался; обязательного отзыва нет.
        _, pending = await asyncio.wait({task}, timeout=max(0, deadline-asyncio.get_running_loop().time()))
        if pending:
            raise Unavailable('bootstrap revocation not confirmed; cleanup still running, reconciliation required')
        if task.cancelled():
            raise Unavailable('bootstrap revocation not confirmed; cleanup cancelled, reconciliation required')
        error = task.exception()
        if error is not None:
            reason = str(error) if isinstance(error, (Refused, Unavailable)) else type(error).__name__
            raise Unavailable('bootstrap revocation not confirmed; reconciliation required: '+reason) from error

    async def _revoke(self):
        async with asyncio.timeout(10):
            await self.ports.admit(self.registration, False)
