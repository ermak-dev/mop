"""Native composition #640: live discovery, physical quiescence, затем fence.

Transport export не несёт epoch/policy. MachineClient проверяет полный live wire;
старый неполный discovery явно отказывает, без второго transport.
CLI не выдумывает physical root: полный LocalExecutor связывает доверенный caller.
"""
import asyncio
from dataclasses import dataclass
from uuid import UUID, uuid4

from mop.node.bootstrap_transport import BootstrapTransport
from mop.node.http_execution import HttpsTransport, MachineClient, Refused, Runtime, Unavailable, exact, uuid

STARTUP_TIMEOUT = 5.0


def _nonzero_uuid(value):
    value = uuid(value)
    if UUID(value).int == 0:
        raise Refused('nonzero installation/node/executor UUID required')
    return value


@dataclass(frozen=True, slots=True)
class Authority:
    installation_id: str
    epoch: str
    node_id: str
    executor_epoch: str

    @classmethod
    def parse(cls, value, *, installation_id, node_id):
        # Это проверка результата client port и привязки к transport identity,
        # не альтернативный HTTP parser или локальный authority snapshot.
        if type(value) is not dict:
            raise Unavailable('machine executor discovery lacks installation identity and epoch; startup unavailable')
        exact(value, ('schema_version', 'installation_id', 'epoch', 'node_id', 'executor_epoch'))
        if (type(value['installation_id']) is not str or value['installation_id'] != installation_id
                or value['node_id'] != node_id):
            raise Refused('machine executor discovery changed installation or node')
        return cls(installation_id, _nonzero_uuid(value['epoch']), _nonzero_uuid(value['node_id']),
                   _nonzero_uuid(value['executor_epoch']))


def machine_client(bootstrap):
    if type(bootstrap) is not BootstrapTransport:
        raise Refused('typed private node bootstrap transport required')
    return MachineClient(HttpsTransport(bootstrap.origin, bootstrap.token,
        timeout=STARTUP_TIMEOUT, connections=4, response_limit=262144, tls=bootstrap.tls()))


async def check(client, *, installation_id, node_id):
    """Только live read; ни reconcile, ни restarted, ни poll у --check нет."""
    try:
        async with asyncio.timeout(STARTUP_TIMEOUT):
            value = await client.executor_state(_nonzero_uuid(node_id))
            return Authority.parse(value, installation_id=installation_id, node_id=node_id)
    except TimeoutError:
        raise Unavailable('machine executor discovery timed out') from None


class NativeRuntime(Runtime):
    async def start(self, previous_epoch):
        if self._closed or self._ready:
            raise Refused('runtime already started or closed')
        # Один outer deadline включает также discovery в start() ниже. Нельзя
        # выдать HTTP ACK за inventory/retirement настоящих OS process scopes.
        try:
            async with asyncio.timeout(min(self.shutdown_timeout, STARTUP_TIMEOUT)):
                quiet = await self.executor.reconcile(self.node_id, previous_epoch, self.executor_epoch)
                if quiet is not True:
                    raise Unavailable('physical executor scopes require quiescence reconciliation')
                await self.client.restarted(self.node_id, previous_epoch, self.executor_epoch)
        except TimeoutError:
            raise Unavailable('native startup timed out before reconciliation and restart confirmation') from None
        self._ready = True


async def _close_unstarted(client, executor):
    """До Runtime cleanup тоже имеет одного owner, независимого от caller cancel."""
    async def close():
        try:
            async with asyncio.timeout(STARTUP_TIMEOUT):
                await executor.aclose()
        finally:
            async with asyncio.timeout(STARTUP_TIMEOUT):
                await client.aclose()
    owner = asyncio.create_task(close())
    cancelled = False
    while not owner.done():
        try:
            await asyncio.shield(owner)
        except asyncio.CancelledError:
            cancelled = True
    owner.result()
    if cancelled:
        raise asyncio.CancelledError


async def start(client, executor, *, installation_id, node_id):
    """Передача ownership clients/tasks; production ports, не fake reconciliation.

    Ни UUID от local file, ни Runtime.start старого порядка не участвуют. При
    отказе/cancel закрывается тот же owner; lost restart не повторяется.
    """
    runtime = None
    try:
        async with asyncio.timeout(STARTUP_TIMEOUT):
            authority = await check(client, installation_id=installation_id, node_id=node_id)
            runtime = NativeRuntime(client, executor, node_id=authority.node_id, epoch=authority.epoch,
                                    executor_epoch=str(uuid4()))
            await runtime.start(authority.executor_epoch)
        return runtime
    except BaseException as error:
        if runtime is not None:
            await runtime.aclose()
        else:
            await _close_unstarted(client, executor)
        if isinstance(error, TimeoutError):
            raise Unavailable('native startup timed out before reconciliation and restart confirmation') from None
        raise


@dataclass(frozen=True, slots=True)
class NativeBindings:
    """Полный physical graph от доверенного native root; не snapshot permissions."""
    executor: object

    def __post_init__(self):
        from mop.node.execution_adapters import LocalExecutor
        from mop.node.http_execution import ACTIONS
        # Poll не принимает capability filter. Расширение wire vocabulary само
        # по себе не даёт native adapter недостающих effects/physical ports.
        if ACTIONS != frozenset(('restart_session', 'apply_identity', 'send_message',
                                 'confirm_ready', 'wipe_clone')):
            raise Unavailable('native executor capabilities do not cover the machine action surface')
        executor = self.executor
        if type(executor) is not LocalExecutor:
            raise Unavailable('complete native physical LocalExecutor root is not bound')
        ports = ((executor.resolver, ('resolve', 'reconcile')),
                 (executor.readiness, ('confirmed',)), (executor.replacement, ('confirmed',)),
                 (executor.body, ('quiescent', 'wipe')), (executor.sender, ('send', 'aclose')),
                 (executor.runner, ('run', 'aclose')))
        if any(port is None or any(not callable(getattr(port, name, None)) for name in methods)
               for port, methods in ports):
            raise Unavailable('complete native physical/readiness/replacement/body root is not bound')


async def check_bootstrap(bootstrap):
    client = machine_client(bootstrap)
    try:
        return await check(client, installation_id=bootstrap.installation_id, node_id=bootstrap.node_id)
    finally:
        await client.aclose()


async def serve(bootstrap, *, bindings=None):
    if type(bindings) is not NativeBindings:
        raise Unavailable('native physical startup root is not bound; effectful agent serve is unavailable')
    try:
        client = machine_client(bootstrap)
    except BaseException:
        async with asyncio.timeout(STARTUP_TIMEOUT):
            await bindings.executor.aclose()
        raise
    # start() владеет cleanup на любой ошибке, run()/aclose() — после успеха.
    runtime = await start(client, bindings.executor, installation_id=bootstrap.installation_id,
                          node_id=bootstrap.node_id)
    try:
        await runtime.run()
    finally:
        await runtime.aclose()
