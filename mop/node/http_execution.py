"""HTTP execution v1: JSON boundary, bounded async client и process-local runtime.

Нет импорта controller, RPC fallback или локального durable журнала. Подтверждение
claim — короткий допуск, не обещание exactly-once внешнему tmux/Git эффекту.
"""
import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import re
import ssl
from typing import Awaitable, Callable, Protocol
from urllib.parse import urlsplit
from uuid import UUID, uuid4


class Refused(ValueError):
    pass


class Unavailable(RuntimeError):
    pass


ACTIONS = frozenset(('restart_session', 'apply_identity', 'send_message', 'confirm_ready'))
EVIDENCE = frozenset(('session-replaced', 'identity-applied', 'ready', 'delivered',
    'confirmed', 'connection-lost', 'not-started', 'failed-before-effect', 'effect-running'))
FACT_TYPES = {
    'states': dict(running=bool, dirty=bool, session_count=int),
    'clone': dict(clean=bool, branch=str, untracked=int, modified=int),
    'sizes': dict(bytes=int),
    'usage': dict(input_tokens=int, output_tokens=int, cache_read_tokens=int, cache_write_tokens=int),
    'disk': dict(free_bytes=int, total_bytes=int),
    'junk': dict(candidate_count=int, bytes=int, inventory_complete=bool),
    'health': dict(healthy=bool, latency_ms=int),
}
FACT_FIELDS = {kind: set(fields) for kind, fields in FACT_TYPES.items()}


def validate_fact_values(kind, values, *, partial=False):
    # bool — подкласс int, поэтому только точный type; 0 и False не взаимозаменяемы.
    if (type(values) is not dict or set(values) - FACT_FIELDS[kind]
            or not partial and set(values) != FACT_FIELDS[kind]):
        raise Refused('incomplete finite fact schema')
    for field, value in values.items():
        expected = FACT_TYPES[kind][field]
        if (type(value) is not expected
                or expected is int and not 0 <= value < 2**63
                or expected is str and (len(value) > 256 or any(ord(c) < 32 or ord(c) == 127 for c in value))):
            raise Refused('invalid typed fact value')


def uuid(value):
    try:
        if type(value) is str and str(UUID(value)) == value:
            return value
    except ValueError:
        pass
    raise Refused('canonical UUID required')


def counter(value):
    if (type(value) is not str or not re.fullmatch(r'[1-9][0-9]{0,18}', value)
            or int(value) >= 2**63):
        raise Refused('positive decimal counter required')
    return int(value)


def stamp(value):
    try:
        if type(value) is str and len(value) <= 40:
            result = datetime.fromisoformat(value)
            if result.tzinfo is not None and result.utcoffset() is not None:
                return result.astimezone(timezone.utc)
    except ValueError:
        pass
    raise Refused('aware deadline required')


def exact(value, names):
    if (type(value) is not dict or set(value) != set(names)
            or type(value.get('schema_version')) is not int or value['schema_version'] != 1):
        raise Refused('unknown fields or unsupported schema')


@dataclass(frozen=True)
class Work:
    epoch: str
    operation_id: str
    step_id: str
    target_id: str
    incarnation_id: str
    node_id: str
    executor_epoch: str
    owner_id: int
    generation: int
    revision: int
    role: str
    bundle_digest: str
    grant_id: str
    action: str
    not_after: datetime

    @classmethod
    def parse(cls, value):
        exact(value, {'schema_version', *cls.__dataclass_fields__})
        values = {k: uuid(value[k]) for k in ('epoch', 'operation_id', 'step_id',
            'target_id', 'incarnation_id', 'node_id', 'executor_epoch', 'grant_id')}
        values.update({k: counter(value[k]) for k in ('owner_id', 'generation', 'revision')})
        if (type(value['role']) is not str or not re.fullmatch(r'[a-z][a-z0-9_-]{0,63}', value['role'])
                or type(value['bundle_digest']) is not str or not re.fullmatch(r'[0-9a-f]{64}', value['bundle_digest'])
                or type(value['action']) is not str or value['action'] not in ACTIONS):
            raise Refused('finite action and pinned role required')
        return cls(**values, role=value['role'], bundle_digest=value['bundle_digest'],
                   action=value['action'], not_after=stamp(value['not_after']))

    def wire(self):
        return {'schema_version': 1, **{k: (
            str(getattr(self, k)) if k in ('owner_id', 'generation', 'revision')
            else self.not_after.isoformat() if k == 'not_after' else getattr(self, k))
            for k in self.__dataclass_fields__}}

    def gate(self, *, node_id, epoch, executor_epoch):
        if (self.node_id != node_id or self.epoch != epoch or self.executor_epoch != executor_epoch
                or self.not_after <= datetime.now(timezone.utc)):
            raise Refused('foreign or expired work')


class JsonTransport(Protocol):
    async def request(self, method, path, payload=None, params=None): ...
    async def aclose(self): ...


class HttpsTransport:
    """Только verified HTTPS, один bounded pool; POST не повторяется даже при EOF."""
    def __init__(self, origin, token, *, timeout=10.0, connections=8,
                 response_limit=262144, tls=None):
        parts = urlsplit(origin)
        if (parts.scheme != 'https' or not parts.hostname or parts.username or parts.password
                or parts.query or parts.fragment or parts.path not in ('', '/')
                or not 0 < timeout <= 60 or not 1 <= connections <= 16
                or not 1024 <= response_limit <= 1048576):
            raise Refused('bounded HTTPS origin required')
        if not isinstance(token, str) or not token or any(ord(c) < 33 or ord(c) > 126 for c in token):
            raise Refused('machine credential required')
        context = tls or ssl.create_default_context()
        if context.verify_mode != ssl.CERT_REQUIRED or not context.check_hostname:
            raise Refused('verified TLS required')
        self.origin = origin.rstrip('/') + '/api/v1/machine/'
        self._token, self._tls = token, context
        self.timeout, self.connections, self.response_limit = timeout, connections, response_limit
        self._session = None
        self._closed = False

    async def request(self, method, path, payload=None, params=None):
        if self._closed:
            raise Unavailable('HTTP client closed')
        allowed = {('GET','work'), ('POST','work/claim'), ('POST','work/result'),
                   ('GET','work/input'), ('GET','executor'), ('POST','executor/restarted'), ('GET','facts'), ('POST','telemetry')}
        if (method, path) not in allowed:
            raise Refused('unnamed machine endpoint')
        import aiohttp
        if self._session is None:
            self._session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(limit=self.connections, ssl=self._tls),
                timeout=aiohttp.ClientTimeout(total=self.timeout),
                headers={'Authorization': 'Token ' + self._token, 'Accept': 'application/json'},
                trust_env=False)
        try:
            # redirects не должны уносить credential или повторять эффект на другом origin.
            async with self._session.request(method, self.origin+path, json=payload,
                    params=params, allow_redirects=False) as response:
                if response.status != 200:
                    if response.status in (400, 401, 403, 404, 409):
                        raise Refused('machine request refused (HTTP ' + str(response.status) + ')')
                    raise Unavailable('machine authority unavailable (HTTP ' + str(response.status) + ')')
                if response.content_type != 'application/json':
                    raise Refused('JSON response required')
                body = bytearray()
                async for chunk in response.content.iter_chunked(8192):
                    body.extend(chunk)
                    if len(body) > self.response_limit:
                        raise Refused('machine response exceeds limit')
                try:
                    return json.loads(body, object_pairs_hook=_unique_object)
                except (ValueError, UnicodeError) as error:
                    raise Refused('invalid machine JSON') from error
        except (aiohttp.ClientError, TimeoutError, OSError) as error:
            raise Unavailable('machine HTTPS connection lost; outcome unknown') from error

    async def aclose(self):
        self._closed = True
        if self._session is not None:
            await self._session.close()


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise Refused('duplicate JSON field')
        result[key] = value
    return result


@dataclass(frozen=True)
class Admission:
    work: Work
    request_id: str
    attempt_id: str
    revision: int
    lease_until: datetime


class MachineClient:
    def __init__(self, transport: JsonTransport):
        self.transport = transport

    async def action_input(self, work):
        value = await self.transport.request('GET','work/input',params=dict(schema_version='1',
            operation_id=work.operation_id,step_id=work.step_id))
        names = ('schema_version','operation_id','step_id','git_name','git_email')
        if work.action == 'send_message':
            names += ('message', 'message_id', 'operation_request_id', 'priority', 'notify_when_idle', 'holder_endpoint', 'reply_address')
        exact(value, names)
        if work.action == 'send_message':
            text = value['message']
            valid_text = False
            if type(text) is str and text.strip() and '\x00' not in text:
                try:
                    valid_text = len(text.encode('utf-8')) <= 16384
                except UnicodeError:
                    pass
            if (not valid_text or type(value['priority']) is not str
                    or value['priority'] not in ('next', 'now', 'later') or type(value['notify_when_idle']) is not bool):
                raise Refused('invalid bounded message input')
            uuid(value['message_id'])
            uuid(value['operation_request_id'])
            if value['holder_endpoint'] is not None:
                uuid(value['holder_endpoint'])
                address = value['reply_address']
                if type(address) is not str or not address.startswith('private:'):
                    raise Refused('private reply address required')
                uuid(address.removeprefix('private:'))
            elif value['reply_address'] is not None:
                raise Refused('reply address requires an addressed holder')
            if value['notify_when_idle'] and value['holder_endpoint'] is None:
                raise Refused('idle notification requires an addressed holder')
        if value['operation_id'] != work.operation_id or value['step_id'] != work.step_id:
            raise Refused('action input address changed')
        if any(type(item) is not str or not 1 <= len(item) <= 256 or
                any(ord(char)<32 or ord(char)==127 for char in item)
                for item in (value['git_name'],value['git_email'])):
            raise Refused('confirmed owner identity required')
        return value

    async def executor_state(self, node_id):
        value = await self.transport.request('GET','executor',params=dict(schema_version='1',node_id=uuid(node_id)))
        exact(value, ('schema_version','node_id','executor_epoch'))
        if value['node_id'] != node_id:
            raise Refused('executor discovery changed node')
        return uuid(value['executor_epoch'])

    async def restarted(self, node_id, previous_epoch, new_epoch):
        value = await self.transport.request('POST', 'executor/restarted', dict(
            schema_version=1, node_id=uuid(node_id), previous_epoch=uuid(previous_epoch), new_epoch=uuid(new_epoch)))
        exact(value, ('schema_version', 'executor_epoch', 'replay_allowed'))
        if value['executor_epoch'] != new_epoch or value['replay_allowed'] is not False:
            raise Refused('executor restart not fenced')

    async def poll(self, node_id, executor_epoch, limit):
        value = await self.transport.request('GET', 'work', params=dict(
            schema_version='1', node_id=uuid(node_id), executor_epoch=uuid(executor_epoch), limit=str(limit)))
        exact(value, ('schema_version', 'work'))
        if type(value['work']) is not list or len(value['work']) > limit:
            raise Refused('unbounded work response')
        return tuple(Work.parse(row) for row in value['work'])

    async def claim(self, work, request_id):
        value = await self.transport.request('POST', 'work/claim', dict(
            schema_version=1, request_id=uuid(request_id), work=work.wire()))
        exact(value, ('schema_version', 'attempt_id', 'revision', 'state', 'lease_until', 'work'))
        admitted = Work.parse(value['work'])
        lease = stamp(value['lease_until'])
        if (admitted != work or value['state'] != 'claimed'
                or not datetime.now(timezone.utc) < lease <= work.not_after):
            raise Refused('claim changed or expired')
        return Admission(work, request_id, uuid(value['attempt_id']), counter(value['revision']), lease)

    async def recheck(self, admission):
        current = await self.claim(admission.work, admission.request_id)
        if current != admission:
            raise Refused('admission changed before effect')

    async def result(self, admission, outcome, evidence):
        if outcome not in ('succeeded', 'failed', 'unknown') or evidence not in EVIDENCE:
            raise Refused('finite result required')
        receipt_id = str(uuid4())
        value = await self.transport.request('POST', 'work/result', dict(schema_version=1,
            attempt_id=admission.attempt_id, expected_revision=str(admission.revision),
            receipt_id=receipt_id, outcome=outcome, evidence=evidence))
        exact(value, ('schema_version', 'receipt_id', 'attempt_id', 'revision', 'outcome', 'evidence'))
        if (value['receipt_id'] != receipt_id or value['attempt_id'] != admission.attempt_id
                or value['outcome'] != outcome or value['evidence'] != evidence
                or counter(value['revision']) <= admission.revision):
            raise Refused('receipt does not confirm result')

    async def facts(self, node_id, kind, limit=100):
        if kind not in FACT_FIELDS or type(limit) is not int or not 1 <= limit <= 100:
            raise Refused('bounded named facts required')
        value = await self.transport.request('GET', 'facts', params=dict(schema_version='1',
            node_id=uuid(node_id), kind=kind, limit=str(limit)))
        exact(value, ('schema_version', 'node_id', 'kind', 'complete', 'items'))
        if (value['node_id'] != node_id or value['kind'] != kind or type(value['complete']) is not bool
                or type(value['items']) is not list or len(value['items']) > limit):
            raise Refused('invalid fact page')
        for item in value['items']:
            if type(item) is not dict or set(item) != {'target_id', 'incarnation_id', 'sequence',
                    'observed_at', 'received_at', 'state', 'values'}:
                raise Refused('invalid fact item')
            for key in ('target_id', 'incarnation_id'):
                if item[key] is not None:
                    uuid(item[key])
            counter(item['sequence'])
            if not stamp(item['observed_at']) <= stamp(item['received_at']) <= datetime.now(timezone.utc):
                raise Refused('invalid fact timestamp')
            if (item['state'] not in ('fresh', 'stale', 'unavailable')
                    or value['complete'] and item['state'] != 'fresh'):
                raise Refused('invalid named facts or incomplete observation')
            validate_fact_values(kind, item['values'], partial=item['state'] != 'fresh')
        return value

    async def aclose(self):
        await self.transport.aclose()


@dataclass(frozen=True)
class EffectGuard:
    authorize: Callable[[], Awaitable[None]]
    check: Callable[[], None]

    async def __call__(self):
        await self.authorize()


async def run_http_agent(*, origin, token, node_id, epoch, executor_epoch,
                         previous_epoch, resolver, tls=None, readiness=None, replacement=None):
    """Новый composition entry: только HTTP, без старого agent/NATS/state fallback.

    #643 передаёт enrolled identity, предыдущий server-owned epoch и доверенный
    physical/input resolver; #639 — addressed action input. До этих bindings
    старт отказывает, а не выводит target/credentials из имени или .git marker.
    """
    from mop.node.execution_adapters import LocalExecutor, ProcessRunner
    client = MachineClient(HttpsTransport(origin, token, tls=tls))
    executor = LocalExecutor(ProcessRunner(), resolver, readiness=readiness, replacement=replacement)
    runtime = Runtime(client, executor, node_id=node_id, epoch=epoch, executor_epoch=executor_epoch)
    try:
        await runtime.start(previous_epoch)
        await runtime.run()
    finally:
        await runtime.aclose()


class ExecutorPort(Protocol):
    async def reconcile(self, node_id, previous_epoch, new_epoch) -> bool: ...
    async def execute(self, work: Work, guard) -> tuple[str, str]: ...
    async def aclose(self): ...


class Runtime:
    """Один владелец clients/tasks. Uncertain target остаётся занят до restart+сверки.

    Бounded blocked map — не очередь и не durable store. При заполнении — backpressure,
    а не eviction неизвестного эффекта. Новый процесс обязан сверить старые effects
    через composition port; restart HTTP fence сам по себе этого не доказывает.
    """
    def __init__(self, client, executor, *, node_id, epoch, executor_epoch,
                 concurrency=4, blocked_limit=256, poll_interval=1.0, shutdown_timeout=5.0):
        self.node_id, self.epoch, self.executor_epoch = map(uuid, (node_id, epoch, executor_epoch))
        if (type(concurrency) is not int or not 1 <= concurrency <= 4
                or type(blocked_limit) is not int or not concurrency <= blocked_limit <= 4096
                or not .1 <= poll_interval <= 60 or not .05 <= shutdown_timeout <= 30):
            raise Refused('bounded runtime configuration required')
        self.client, self.executor = client, executor
        self.concurrency, self.blocked_limit = concurrency, blocked_limit
        self.poll_interval, self.shutdown_timeout = poll_interval, shutdown_timeout
        self._ready = False
        self._closed = False
        self._targets = set()
        self._tasks = set()
        self._poll_lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self.last_error = None
        self._close_task = None
        self._cleanup_task = None

    async def start(self, previous_epoch):
        if self._closed or self._ready:
            raise Refused('runtime already started or closed')
        async with asyncio.timeout(self.shutdown_timeout):
            if previous_epoch is None:
                previous_epoch = await self.client.executor_state(self.node_id)
            await self.client.restarted(self.node_id, previous_epoch, self.executor_epoch)
            quiet = await self.executor.reconcile(self.node_id, previous_epoch, self.executor_epoch)
        if quiet is not True:
            raise Unavailable('old executor effects require quiescence reconciliation')
        self._ready = True

    def _gate(self, work):
        if not self._ready or self._closed:
            raise Unavailable('executor not accepting effects')
        work.gate(node_id=self.node_id, epoch=self.epoch, executor_epoch=self.executor_epoch)

    async def tick(self):
        if not self._ready or self._closed:
            raise Unavailable('executor not reconciled')
        async with self._poll_lock:
            room = min(self.concurrency-len(self._tasks), self.blocked_limit-len(self._targets))
            if room <= 0:
                return
            for work in await self.client.poll(self.node_id, self.executor_epoch, room):
                try:
                    self._gate(work)
                except Refused as error:
                    self.last_error = str(error)
                    continue
                if work.target_id in self._targets:
                    continue
                self._targets.add(work.target_id)
                task = asyncio.create_task(self._execute(work))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)

    async def _execute(self, work):
        # Даже потерянный claim reply мог оставить durable admission. Не освобождаем
        # target по timeout и не генерируем второй request_id для повтора эффекта.
        try:
            admission = await self.client.claim(work, str(uuid4()))
            def local_check():
                self._gate(work)
                if admission.lease_until <= datetime.now(timezone.utc):
                    raise Refused('admission lease expired')
            async def authorize():
                local_check()
                await self.client.recheck(admission)
                local_check()
            guard = EffectGuard(authorize, local_check)
            try:
                outcome, evidence = await self.executor.execute(work, guard)
            except (Refused, Unavailable) as error:
                # Отказ между двумя конечными effects тоже может оставить partial
                # ввод/identity. Не объявляем failed и не запускаем компенсацию.
                self.last_error = str(error)
                outcome, evidence = 'unknown', 'connection-lost'
            except Exception as error:
                self.last_error = 'executor adapter failed: ' + type(error).__name__
                outcome, evidence = 'unknown', 'connection-lost'
            await self.client.result(admission, outcome, evidence)
            if outcome != 'unknown':
                self._targets.discard(work.target_id)
        except (Refused, Unavailable) as error:
            self.last_error = str(error)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            # Не отдаём raw subprocess exception в телеметрию/receipt.
            self.last_error = 'executor adapter failed: ' + type(error).__name__

    async def drain(self):
        if self._tasks:
            await asyncio.gather(*tuple(self._tasks))

    async def run(self):
        try:
            while not self._stop.is_set():
                try:
                    await self.tick()
                except (Refused, Unavailable) as error:
                    self.last_error = str(error)
                try:
                    await asyncio.wait_for(self._stop.wait(), self.poll_interval)
                except TimeoutError:
                    pass
        finally:
            await self.aclose()

    async def aclose(self):
        if self._close_task is None:
            self._closed = True
            self._ready = False
            self._stop.set()
            self._close_task = asyncio.create_task(self._shutdown())
        # Отмена внешнего await не должна отменить единственного cleanup owner.
        await asyncio.shield(self._close_task)

    async def _shutdown(self):
        tasks = tuple(self._tasks)
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=self.shutdown_timeout)
            for task in pending:
                task.cancel()
        # Concrete executor reap'ит только своих subprocess, не живые сессии.
        close = getattr(self.executor, 'aclose', None)
        cleanup = asyncio.create_task(close()) if close else None
        self._cleanup_task = cleanup
        owned = {*tasks, *([cleanup] if cleanup else [])}
        try:
            if owned:
                done, pending = await asyncio.wait(owned, timeout=self.shutdown_timeout)
                await asyncio.gather(*done, return_exceptions=True)
                if pending:
                    # Нельзя выдавать cancellation за physical quiescence. Сохраняем
                    # fence и ссылки на задачи; supervisor видит явный отказ shutdown.
                    self.last_error = 'shutdown incomplete; executor effects require reconciliation'
                    raise Unavailable(self.last_error)
                if cleanup and not cleanup.cancelled() and cleanup.exception():
                    raise Unavailable('executor cleanup failed; reconciliation required') from cleanup.exception()
        finally:
            await self.client.aclose()
