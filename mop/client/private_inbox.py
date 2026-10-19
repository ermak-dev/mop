"""Внутренний launcher-owned WSS port, не способ обнаружить чужой native.

OwnSession и синхронный current поступают только из trusted composition.
Нет registry/env discovery, ORM, локальной election или повторной доставки.
Закрытый owner одноразовый: новый transport требует нового launcher proof.
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import ssl
from uuid import UUID

from mop.common.endpoint import DeliveryFrame, inbox_decode, inbox_encode, INBOX_DELIVERY_BYTES
from . import control as api
from .local_bridge import LocalBridge, OwnSession


@dataclass(frozen=True, repr=False)
class BoundInbox:
    """Transient borrow настоящего bound owner; не DTO с caller-defined headers."""
    _owner: object

    def _headers(self):
        owner = self._owner
        if type(owner) is not InboxOwner or owner.holder() is not self:
            raise api.ClientError('private inbox holder is not current')
        return {'X-Mop-Endpoint': owner.endpoint.id, 'X-Mop-Connection': owner._nonce}


class InboxOwner:
    """Один native bridge, один WS reader и один незавершённый delivery.

    Management не зависит от этого owner. Дубликат bind не выбирает primary,
    а закрывается; authoritative exclusivity остаётся у сервера. Reconnect
    выполняется только trusted composition, без переноса кадров/очередей.
    """
    def __init__(self, context, own, *, client_id, project_id, display_name, current):
        if type(context) is not api.ServerContext or type(own) is not OwnSession or not callable(current):
            raise TypeError('trusted server context, OwnSession and current port required')
        api._key(client_id); api._key(project_id)
        api._require(type(display_name) is str and 0 < len(display_name) <= 128)
        self.context, self.own, self.current = context, own, current
        self.client_id, self.project_id, self.display_name = client_id, project_id, display_name
        self.bridge = LocalBridge(own, timeout=2)
        self.endpoint = None
        self.epoch = self._nonce = None
        self._bound = self._started = self._closing = False
        self._holder = BoundInbox(self)
        self._closed = asyncio.Event()
        self._tasks = set()
        self._session = self._ws = self._runner = None
        self._delivery_task = self._probe_task = self._capacity_task = None
        self._records = {}
        self._permit = None
        self._send_lock = asyncio.Lock()
        self._senders = 0
        self._executor = None
        self._http_future = None
        self._startup = self._cleanup_task = None
        self.failure = None
        self._close_error = None

    @property
    def tasks(self):
        owned = self._tasks | {task for task in (self._startup, self._cleanup_task) if task is not None}
        return frozenset(task for task in owned if not task.done())

    def _check(self):
        if self._closing or self.current() is not True:
            raise PermissionError('trusted conversation is no longer current')
        self.bridge._path()
        self.own.check_process(self.own.pid, self.own.uid)

    def holder(self):
        if not self._bound or self._closed.is_set():
            return None
        try:
            self._check()
        except (OSError, ValueError, PermissionError, RuntimeError):
            return None
        return self._holder

    async def _http(self, function):
        # Единственный worker и одна admission; cancel не забывает underlying I/O.
        self._check()
        if self._http_future is not None:
            raise RuntimeError('private HTTP admission is occupied')
        future = self._executor.submit(function)
        self._http_future = future
        try:
            return await asyncio.wait_for(asyncio.shield(asyncio.wrap_future(future)), 5)
        finally:
            if future.done() and self._http_future is future:
                self._http_future = None

    def _spawn(self, coroutine):
        task = asyncio.create_task(coroutine)
        self._tasks.add(task)
        def finished(done):
            self._tasks.discard(done)
            if not done.cancelled() and done.exception() is not None:
                # Причина конечная, raw transport exception с token не хранится.
                self.failure = 'private inbox transport or authority unavailable'
                if not self._closing and self._runner is not None and not self._runner.done():
                    self._runner.cancel()
        task.add_done_callback(finished)
        return task

    async def start(self):
        if self._started or self._closing:
            raise RuntimeError('private inbox owner is single use')
        self._started = True
        # Отдельный resource task: cleanup отменяет и собирает startup, но не
        # ждёт вызывающего start(), который сам ожидает тот же cleanup.
        self._startup = asyncio.create_task(self._start())
        try:
            return await asyncio.shield(self._startup)
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling():
                # Отменён сам вызывающий: его cancel не теряется, cleanup
                # всё равно собран до выхода.
                await asyncio.shield(self._begin_cleanup())
                raise
        except api.ClientError:
            await asyncio.shield(self._begin_cleanup())
            raise
        # Startup отменил чужой close: неотменённый вызывающий получает отказ,
        # а не CancelledError. Ошибка cleanup остаётся у aclose/wait_closed.
        await asyncio.shield(self._begin_cleanup())
        raise api.ClientError('private inbox closed during startup')

    async def _start(self):
        failed = False
        try:
            # Импорт declared dependency только здесь, management остаётся stdlib.
            import aiohttp
            self._check()
            await self.bridge.ready()
            self._check()
            await self.bridge.start()
            self._check()
            self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='mop-inbox')
            limits = api.Limits(connect_timeout=2, read_timeout=2, request_timeout=5)
            client = api.Client(self.context, limits=limits)
            scope = api.CapabilityScope(project_id=self.project_id, client_id=self.client_id)
            capabilities = await self._http(lambda: client.capabilities(scope))
            if capabilities.identity.kind != 'human' or 'execution.register_endpoint' not in capabilities.identity.action_permissions:
                raise PermissionError('private receiver requires current human authority')
            self.epoch = capabilities.epoch
            self._check()
            self.endpoint = await self._http(lambda: client.register_endpoint(client_id=self.client_id,
                project_id=self.project_id, external_session_id=self.own.external_session_id, display_name=self.display_name))
            self._check()
            tls = ssl.create_default_context(cafile=self.context.ca_file)
            tls.minimum_version = ssl.TLSVersion.TLSv1_2
            # aiohttp следует redirects; trace отказывает до следующего запроса.
            async def redirect(session, trace, params):
                raise api.ProtocolError('private inbox redirects are not allowed')
            trace = aiohttp.TraceConfig()
            trace.on_request_redirect.append(redirect)
            self._session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=tls, limit=1),
                trust_env=False, cookie_jar=aiohttp.DummyCookieJar(), trace_configs=[trace],
                timeout=aiohttp.ClientTimeout(total=5, connect=2, sock_read=2),
                headers={'Authorization': 'Token '+self.context.token, 'Cache-Control': 'no-store'})
            self._check()
            async with asyncio.timeout(5):
                self._ws = await self._session.ws_connect('wss'+self.context.origin[5:]+'/ws/v1/inbox/'+self.endpoint.id,
                    compress=0, max_msg_size=INBOX_DELIVERY_BYTES, autoping=False,
                    heartbeat=None, receive_timeout=5)
                await self._send('peer_ready', external_session_id=self.own.external_session_id, unbound=True)
                value = await self._read()
                if (value['type'] != 'bound' or value['endpoint_id'] != self.endpoint.id or
                    value['external_session_id'] != self.own.external_session_id or value['epoch'] != self.epoch):
                    raise PermissionError('private inbox binding does not match trusted context')
                self._nonce = value['connection_nonce']
                await self.bridge.ready()
                self._check()
                await self._send('bound_received', unbound=True)
            self._check()
            self._bound = True
            self._runner = self._spawn(self._run())
            self._spawn(self._monitor())
            self._spawn(self._idle())
        except (api.ClientError, asyncio.CancelledError):
            raise
        except Exception:
            failed = True
        if failed:
            # Как у HTTPS client: даже __context__ не удерживает raw exception.
            raise api.ClientError('private inbox could not bind current native receiver')
        return self

    async def _send(self, kind, *, unbound=False, **fields):
        self._check()
        if self._senders >= 16 or self._ws is None or self._ws.closed:
            raise ConnectionError('private inbox writer unavailable')
        value = dict(schema_version=1, type=kind, **fields)
        if kind != 'peer_ready':
            value['connection_nonce'] = self._nonce
        wire = inbox_encode(value)
        self._senders += 1
        try:
            async with asyncio.timeout(2):
                async with self._send_lock:
                    self._check()
                    await self._ws.send_str(wire)
        finally:
            self._senders -= 1

    async def _read(self):
        import aiohttp
        raw = await self._ws.receive()
        if raw.type != aiohttp.WSMsgType.TEXT:
            raise ConnectionError('private inbox closed or non-text frame')
        return inbox_decode(raw.data, direction='server')

    async def _run(self):
        try:
            while True:
                value = await self._read()
                self._check()
                if value.get('connection_nonce') != self._nonce:
                    raise PermissionError('private inbox connection changed')
                kind = value['type']
                if kind == 'delivery':
                    frame = value['frame']
                    if (frame['external_session_id'] != self.own.external_session_id or
                        len(self._records) >= 64 or frame['message_id'] in self._records or
                        any(row['id'] == frame['id'] for row in self._records.values()) or
                        self._delivery_task is not None and not self._delivery_task.done()):
                        raise PermissionError('private inbox delivery admission exhausted or duplicate')
                    # Не evict uncertain ID; closing generation не позволяет replay.
                    self._records[frame['message_id']] = dict(id=frame['id'], outcome=None,
                        notify=frame['notify_when_idle'], receipt=None, event=asyncio.Event(), idle=False)
                    self._delivery_task = self._spawn(self._deliver(frame))
                elif kind == 'write_allowed':
                    if self._permit is None or self._permit[0:2] != (value['delivery_id'], value['message_id']) or self._permit[2].done():
                        raise PermissionError('unexpected private prewrite permit')
                    self._permit[2].set_result(None)
                elif kind == 'receipt':
                    result = value['result']; row = self._records.get(result['message_id'])
                    if row is None or row['id'] != result['id'] or row['outcome'] != result['state']:
                        raise PermissionError('unmatched private domain receipt')
                    if result['idle'] and (not row['notify'] or not row['idle']):
                        raise PermissionError('unsolicited idle receipt')
                    old = row['receipt']
                    if old is not None and old['idle'] and not result['idle']:
                        raise PermissionError('private receipt regressed')
                    row['receipt'] = result
                    row['event'].set()
                elif kind == 'probe':
                    if self._probe_task is not None and not self._probe_task.done():
                        raise PermissionError('concurrent private readiness probes')
                    self._probe_task = self._spawn(self._probe(value['probe_id']))
                else:
                    raise PermissionError('unexpected private inbox lifecycle frame')
        except asyncio.CancelledError:
            pass
        except (Exception,):
            self.failure = 'private inbox closed without replay'
        finally:
            # Runner не ждёт cleanup, который обязан собрать сам runner.
            self._begin_cleanup()

    async def _deliver(self, value):
        message = value['message_id']; row = self._records[message]
        frame = DeliveryFrame(UUID(value['id']), UUID(message), value['external_session_id'],
            value['text'], value['priority'], value['notify_when_idle'], value['reply_address'])
        permitted = False
        async def guard():
            nonlocal permitted
            self._check()
            if not permitted:
                future = asyncio.get_running_loop().create_future()
                self._permit = (value['id'], message, future)
                try:
                    await self._send('prewrite', delivery_id=value['id'], message_id=message)
                    await asyncio.wait_for(asyncio.shield(future), 2)
                finally:
                    self._permit = None
                    if not future.done(): future.cancel()
                permitted = True
            # Повторный вызов для idle control — новая physical/current проверка,
            # не второй одноразовый server prewrite для того же user input.
            self._check()
        outcome = 'unknown'
        try:
            async with asyncio.timeout(5):
                acceptance = await self.bridge.deliver_guarded(frame, guard)
                outcome = acceptance.outcome
        except TimeoutError:
            pass
        self._check()
        row['outcome'] = outcome
        await self._send('application_acceptance', delivery_id=value['id'], message_id=message, outcome=outcome)
        await asyncio.wait_for(row['event'].wait(), 2)
        if len(self._records) >= 64:
            self._capacity_task = self._spawn(self._capacity())

    async def _capacity(self):
        # Конечный drain отдельно от idle: молчание peer не удерживает cap вечно.
        deadline = asyncio.get_running_loop().time()+2
        while asyncio.get_running_loop().time() < deadline:
            if all(not row['notify'] or row['outcome']!='accepted' or
                    row['receipt'] is not None and row['receipt']['idle'] for row in self._records.values()):
                break
            await asyncio.sleep(.02)
        self._runner.cancel()

    async def _probe(self, probe_id):
        async with asyncio.timeout(2):
            self._check()
            await self.bridge.ready()
            self._check()
            await self._send('peer_ready_again', probe_id=probe_id, external_session_id=self.own.external_session_id)

    async def _monitor(self):
        while True:
            await asyncio.sleep(.2)
            self._check()
            await self.bridge.ready()

    async def _idle(self):
        while True:
            await asyncio.sleep(.05)
            for message, row in tuple(self._records.items()):
                if row['outcome'] != 'accepted' or not row['notify'] or row['idle'] or row['receipt'] is None:
                    continue
                if await self.bridge.wait_idle(UUID(message), timeout=.01):
                    self._check()
                    row['idle'] = True
                    await self._send('idle_observed', delivery_id=row['id'], message_id=message)

    def _begin_cleanup(self):
        if self._cleanup_task is None:
            # Fence ставится до первого yield и до отмены startup/runner.
            self._closing = True
            self._bound = False
            self._cleanup_task = asyncio.create_task(self._cleanup())
        return self._cleanup_task

    async def _drain_http(self, failures):
        executor, future = self._executor, self._http_future
        if executor is None:
            return
        deadline = asyncio.get_running_loop().time()+5
        shutdown = False
        try:
            # Даже при deadline настоящий worker получит sentinel после I/O.
            # wait=True здесь блокировал бы event loop без конечного join.
            executor.shutdown(wait=False, cancel_futures=True)
            shutdown = True
        except Exception:
            failures.append('HTTP executor')
        if future is not None:
            try:
                await asyncio.wait_for(asyncio.shield(asyncio.wrap_future(future)),
                    max(0, deadline-asyncio.get_running_loop().time()))
            except (Exception, asyncio.CancelledError):
                if asyncio.current_task().cancelling():
                    raise
                if not future.done():
                    failures.append('HTTP worker')
                elif not future.cancelled():
                    # Request failure не означает живой worker; причина самого
                    # запроса конечная, raw transport exception не сохраняется.
                    self.failure = 'private inbox HTTP request failed during shutdown'
            if future.done() and self._http_future is future:
                self._http_future = None
        # CPython ThreadPoolExecutor: admission уже закрыта, submit/startup
        # собраны, новых _threads больше не будет. done Future не равен exit.
        threads = tuple(executor._threads)
        while any(thread.is_alive() for thread in threads):
            remaining = deadline-asyncio.get_running_loop().time()
            if remaining <= 0:
                if 'HTTP worker' not in failures: failures.append('HTTP worker')
                return
            await asyncio.sleep(min(.01, remaining))
        for thread in threads:
            thread.join(timeout=0)
        if shutdown and self._http_future is None:
            self._executor = None

    def _remaining(self):
        # Success — доказанное состояние, а не отсутствие исключений по пути:
        # шаг, вернувшийся без эффекта, не выдаёт live resource за closed.
        # Cancel внутри finally bridge-callback оставляет в его set уже done
        # task: это запись, а не live work, поэтому решает task.done().
        bridge, current = self.bridge, asyncio.current_task()
        return [name for name, live in (
            ('tasks', bool(self.tasks - {current})),
            ('native bridge', bridge._server is not None or bridge._callback is not None
                or any(not task.done() for task in (bridge._tasks | bridge._outbound) - {current})),
            ('WSS', self._ws is not None and not self._ws.closed),
            ('HTTPS session', self._session is not None and not self._session.closed),
            ('HTTP worker', self._http_future is not None),
            ('HTTP executor', self._executor is not None)) if live]

    async def _cleanup(self):
        failures = []
        async def close_one(name, operation):
            try:
                await asyncio.wait_for(operation(), 5)
            except Exception:
                # Raw exceptions не переживают cleanup; следующие owner-ы всё
                # равно получают свою попытку закрытия, ошибки не скрываются.
                failures.append(name)
        try:
            tasks = self.tasks - {asyncio.current_task()}
            for task in tasks: task.cancel()
            if tasks:
                done, pending = await asyncio.wait(tasks, timeout=5)
                if pending:
                    failures.append('tasks')
                for task in done:
                    if not task.cancelled(): task.exception()
            # Startup собран до sweep: он больше не создаст session/callback.
            await close_one('native bridge', self.bridge.aclose)
            if self._ws is not None:
                await close_one('WSS', self._ws.close)
            if self._session is not None:
                await close_one('HTTPS session', self._session.close)
            await self._drain_http(failures)
        except (Exception, asyncio.CancelledError):
            # Неожиданный сбой тоже terminal refusal, не closed-success.
            failures.append('cleanup')
        finally:
            failures += [name for name in self._remaining() if name not in failures]
            if not failures:
                self._records.clear()
            else:
                self._close_error = 'private inbox shutdown incomplete: '+', '.join(failures)
                self.failure = self._close_error
            self._closed.set()

    async def wait_closed(self):
        await self._closed.wait()
        if self._close_error:
            raise RuntimeError(self._close_error)

    async def aclose(self):
        # Ни первый, ни повторный waiter не владеет cancel cleanup task.
        await asyncio.shield(self._begin_cleanup())
        if self._close_error:
            raise RuntimeError(self._close_error)
