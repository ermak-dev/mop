"""Узкий initial-HOST body/root/node producer lane; bearer остаётся у NodeUID.

Mailbox хранит максимум четыре незавершённых операции, не второй telemetry spool.
HookBridge остаётся единственным владельцем sequence/drop/age. После native ACK
telemetry авторизуется exact attached scope, а не истёкшей phase Admission.
"""
import asyncio
from dataclasses import dataclass
import hashlib
import math
import time
from uuid import UUID, uuid4

from mop.common.producer import ProducerAck, ProducerReport
from mop.common.telemetry import Ack, Observation, counter
from mop.driver.launch import Registration
from mop.node.host_inputs import canonical_json
from mop.node.host_scope import Unknown, canonical, exact
from mop.node.http_execution import Admission, HttpsTransport, MachineClient, Refused, Unavailable, Work


def registration_wire(value):
    return dict(work=value.work.wire(), **{key: getattr(value, key) for key in
        ('bootstrap_revision', 'bootstrap_digest', 'image_digest', 'display_name')})


def registration_parse(value):
    exact(value, ('work', 'bootstrap_revision', 'bootstrap_digest', 'image_digest', 'display_name'))
    return Registration(Work.parse(value['work']), **{k: value[k] for k in value if k != 'work'})


MAILBOX_ACTIONS = ('native_poll', 'native_dispatch', 'native_validate', 'native_complete')


def operation_digest(operation, payload, registration):
    return hashlib.sha256(canonical_json(dict(operation=operation, payload=payload,
        registration=registration))).hexdigest()


def report_for(proof):
    r = proof.registration
    return ProducerReport(proof.scope, UUID(r.bootstrap_revision), r.bootstrap_digest,
        r.image_digest, r.display_name, proof.pane, proof.pid, proof.process_start)


def identity_wire(value):
    return dict(pid=value.pid, process_start=value.process_start, session_id=value.session_id,
        tmux_pid=value.tmux_identity[0], tmux_start=value.tmux_identity[1],
        socket_inode=list(value.socket_inode), tty_identity=list(value.tty_identity),
        namespaces=list(value.namespaces))


@dataclass(repr=False)
class _Operation:
    identifier: str
    operation: str
    payload: dict
    digest: str
    future: object
    deadline: float
    worker: str | None = None
    ticket: str | None = None
    started: bool = False


class NativeMailbox:
    capacity = 4

    def __init__(self, registration, admission, *, desired_model):
        from mop.node.host_native import model_id
        if (type(registration) is not Registration or type(admission) is not Admission
                or registration.work != admission.work or admission.work.action != 'native_exec'):
            raise Refused('original native registration and Admission required')
        self.registration, self.admission = registration, admission
        self.desired_model = model_id(desired_model)
        self.operations = {}
        self.producer_report = self.producer_ack = self.attachment = None
        self.last_observation = self.last_ack = self.model_committed = None
        self.closed = False
        self.frontend = None
        self.retired_frontends = set()

    @property
    def pending(self):
        return len(self.operations)

    def validate(self, operation, payload):
        if self.closed:
            raise Unavailable('native lifetime mailbox is closed')
        if len(canonical_json(payload)) > 16384:
            raise Refused('bounded native lifetime payload required')
        if operation == 'register_producer':
            exact(payload, ('report', 'identity'))
            report = ProducerReport.parse(payload['report'])
            r, w = self.registration, self.registration.work
            expected = (w.epoch, w.target_id, w.incarnation_id, w.node_id, w.executor_epoch)
            if (tuple(str(getattr(report.scope, k)) for k in
                    ('epoch', 'target_id', 'incarnation_id', 'node_id', 'executor_epoch')) != expected
                    or (str(report.bootstrap_revision), report.bootstrap_digest, report.image_digest, report.display_name)
                    != (r.bootstrap_revision, r.bootstrap_digest, r.image_digest, r.display_name)):
                raise Refused('native producer differs from original registration')
            identity = payload['identity']
            exact(identity, ('pid', 'process_start', 'session_id', 'tmux_pid', 'tmux_start',
                'socket_inode', 'tty_identity', 'namespaces'))
            if (identity['pid'], identity['process_start'], identity['session_id']) != (
                    report.pid, report.process_start, report.scope.session_id):
                raise Refused('native producer differs from actual attachment')
            if self.producer_report is not None and (self.producer_report != report or self.attachment != identity):
                raise Refused('native lifetime cannot replace original producer or attachment')
            self.producer_report, self.attachment = report, identity
            if self.producer_ack is not None:
                return self.producer_ack.wire()
        elif operation == 'ingest_telemetry':
            if self.producer_ack is None:
                raise Refused('telemetry requires the exact acknowledged attached producer')
            row = Observation.parse(payload)
            if self.producer_ack is None or row.scope != self.producer_ack.scope:
                raise Refused('telemetry requires the exact acknowledged attached producer')
            if self.last_observation is not None and row.sequence <= self.last_observation.sequence:
                if row == self.last_observation:
                    return dict(schema_version=1, producer_epoch=str(row.scope.producer_epoch), sequence=str(row.sequence))
                raise Refused('telemetry cannot replace an already committed sequence')
        else:
            raise Refused('only fixed native producer and telemetry operations are enabled')
        return None

    async def request(self, operation, payload):
        cached = self.validate(operation, payload)
        if cached is not None:
            return cached
        if len(self.operations) >= self.capacity:
            raise Unavailable('native lifetime mailbox capacity exhausted')
        # JSON copy закрывает mutation после submit; digest связывает replay.
        from mop.node.host_scope import decode
        data = decode(canonical_json(payload))
        digest = operation_digest(operation, data, registration_wire(self.registration))
        if any(op.operation == operation for op in self.operations.values()):
            raise Refused('native lifetime operation overlaps an unresolved original operation')
        future = asyncio.get_running_loop().create_future()
        item = _Operation(str(uuid4()), operation, data, digest, future, time.monotonic()+5.)
        self.operations[item.identifier] = item
        try:
            async with asyncio.timeout_at(item.deadline):
                return await asyncio.shield(item.future)
        finally:
            # Никакой фоновой очереди после timeout; исходный HookBridge решает
            # retry/drop, а producer retry сохраняет тот же report/CAS.
            if self.operations.get(item.identifier) is item:
                self.operations.pop(item.identifier)
            if not item.future.done():
                item.future.cancel()

    def poll(self, worker):
        if self.closed:
            raise Unavailable('native lifetime mailbox is closed')
        if type(worker) is not str or not 0 < len(worker) <= 160:
            raise Refused('pinned authenticated node worker identity required')
        if worker in self.retired_frontends:
            raise Refused('retired native frontend cannot reclaim its replacement')
        if self.frontend != worker:
            if self.frontend is not None:
                # Не вытесняем fencing history ради нового frontend.
                if len(self.retired_frontends) >= 256:
                    raise Unavailable('native frontend replacement capacity exhausted')
                self.retired_frontends.add(self.frontend)
            self.frontend = worker
            for item in self.operations.values():
                if item.started and not item.future.done():
                    item.future.set_exception(Unavailable('native frontend replaced after dispatch; HTTP outcome unknown; no replay'))
        for item in self.operations.values():
            if item.future.done() or time.monotonic() >= item.deadline:
                continue
            if item.worker == worker:
                continue
            item.worker, item.ticket = worker, str(uuid4())
            from mop.node.host_scope import decode
            return decode(canonical_json(dict(id=item.identifier, ticket=item.ticket, operation=item.operation,
                digest=item.digest, payload=item.payload, registration=registration_wire(self.registration), deadline=item.deadline)))
        return None

    def current(self, worker, identifier, ticket):
        canonical(identifier)
        canonical(ticket)
        item = self.operations.get(identifier)
        if (self.closed or self.frontend != worker or item is None or item.worker != worker
                or item.ticket != ticket or item.future.done() or time.monotonic() >= item.deadline):
            raise Refused('stale, closed or expired native mailbox dispatch/completion')
        return item

    def dispatch(self, worker, identifier, ticket, digest, deadline, *, begin):
        item = self.current(worker, identifier, ticket)
        if item.digest != digest or item.deadline != deadline:
            raise Refused('native dispatch changed original digest/deadline')
        if begin:
            if item.started:
                raise Refused('native HTTP dispatch already started; outcome unknown; no replay')
            item.started = True
        elif not item.started:
            raise Refused('native HTTP dispatch has not been admitted')
        return dict(id=identifier, ticket=ticket, digest=digest, deadline=deadline)

    def complete(self, worker, identifier, ticket, answer):
        item = self.current(worker, identifier, ticket)
        if item.operation == 'register_producer':
            ack = ProducerAck.parse(answer)
            if ack.scope != self.producer_report.scope or ack.revision != 1:
                raise Refused('native producer acknowledgement differs from original first-producer CAS')
            if self.producer_ack is not None and self.producer_ack != ack:
                raise Refused('native producer acknowledgement changed')
            self.producer_ack = ack
        elif item.operation == 'ingest_telemetry':
            exact(answer, ('schema_version', 'producer_epoch', 'sequence'))
            if type(answer['schema_version']) is not int or answer['schema_version'] != 1:
                raise Refused('invalid native telemetry acknowledgement')
            ack = Ack(UUID(answer['producer_epoch']), counter(answer['sequence']))
            row = Observation.parse(item.payload)
            if ack != Ack(row.scope.producer_epoch, row.sequence):
                raise Refused('foreign native telemetry acknowledgement')
            self.last_observation, self.last_ack = row, ack
            if row.event == 'PostModelSwitch' and row.model == self.desired_model and row.dropped == 0:
                self.model_committed = row
        item.future.set_result(answer)

    def fail(self, worker, identifier, ticket, reason):
        item = self.current(worker, identifier, ticket)
        # Только finite safe library reason; worker не присылает raw HTTP exception.
        if type(reason) is not str or not 0 < len(reason) <= 512 or not reason.isprintable():
            raise Refused('bounded native mailbox refusal required')
        item.future.set_exception(Unavailable(reason))

    def close(self):
        self.closed = True
        for item in self.operations.values():
            if not item.future.done():
                item.future.set_exception(Unavailable('native physical lifetime closed'))


class ProxyRegistration:
    def __init__(self, channel, registration, layout):
        self.channel, self.registration, self.layout = channel, registration, layout
        self.acknowledgement = self.proof = None

    async def register(self, proof):
        from mop.node.native_observer import _snapshot
        if proof.registration != self.registration or self.proof is not None and proof != self.proof:
            raise Refused('body proxy cannot replace original native proof')
        self.proof = proof
        identity = await asyncio.to_thread(_snapshot, self.layout)
        report = report_for(proof)
        answer = await self.channel.request('register_producer',
            dict(report=report.wire(), identity=identity_wire(identity)))
        ack = ProducerAck.parse(answer)
        if ack.scope != proof.scope or ack.revision != 1:
            raise Refused('body native producer acknowledgement changed')
        self.acknowledgement = ack
        return ack.scope


class ProxyTelemetry:
    def __init__(self, channel):
        self.channel = channel
        self.committed = None

    async def ingest(self, observation):
        answer = await self.channel.request('ingest_telemetry', observation.wire())
        exact(answer, ('schema_version', 'producer_epoch', 'sequence'))
        if type(answer['schema_version']) is not int or answer['schema_version'] != 1:
            raise Refused('invalid body telemetry acknowledgement')
        ack = Ack(UUID(answer['producer_epoch']), counter(answer['sequence']))
        if ack != Ack(observation.scope.producer_epoch, observation.sequence):
            raise Refused('body telemetry acknowledgement changed producer or sequence')
        self.committed = observation
        return ack


def mailbox_request(value):
    from mop.node.host_scope import Scope
    if type(value) is not dict or value.get('action') not in MAILBOX_ACTIONS:
        raise Refused('finite native mailbox request required')
    keys = ('schema_version', 'action', 'scope', 'attempt_id', 'frontend')
    action = value['action']
    fields = keys if action == 'native_poll' else (*keys, 'id', 'ticket',
        *(('answer', 'error') if action == 'native_complete' else ('digest', 'deadline')))
    exact(value, fields)
    canonical(value['frontend'])
    if type(value['schema_version']) is not int or value['schema_version'] != 1:
        raise Refused('unsupported native mailbox request')
    Scope.parse(value['scope'])
    canonical(value['attempt_id'])
    if action != 'native_poll':
        canonical(value['id'])
        canonical(value['ticket'])
    if action in ('native_dispatch', 'native_validate'):
        if (type(value['deadline']) is not float or not math.isfinite(value['deadline'])
                or type(value['digest']) is not str or len(value['digest']) != 64
                or any(c not in '0123456789abcdef' for c in value['digest'])):
            raise Refused('original native dispatch deadline/digest required')
    return value


class NativeDispatch:
    """Конкретный node/root owner; никаких переданных callback или phase lease."""
    def __init__(self, client, record, worker):
        from mop.node.host_native_client import LifetimeWorker
        if (type(worker) is not LifetimeWorker or worker.client is not client or worker.current is not record
                or worker.closed):
            raise Refused('native HTTP requires its current live frontend dispatch owner')
        exact(record, ('id', 'ticket', 'operation', 'digest', 'payload', 'registration', 'deadline'))
        if (type(record['deadline']) is not float or not math.isfinite(record['deadline'])
                or not 0 < record['deadline']-time.monotonic() <= 5.):
            raise Refused('original native HTTP operation deadline expired')
        canonical(record['id'])
        canonical(record['ticket'])
        if operation_digest(record['operation'], record['payload'], record['registration']) != record['digest']:
            raise Refused('native mailbox operation digest changed')
        self.client, self.record, self.worker = client, record, worker
        self.started = False

    def local(self):
        if (self.worker.closed or self.worker.current is not self.record
                or time.monotonic() >= self.record['deadline']):
            raise Refused('native dispatch frontend closed/replaced or operation expired')
        if operation_digest(self.record['operation'], self.record['payload'], self.record['registration']) != self.record['digest']:
            raise Refused('native dispatch operation changed while waiting')

    def http(self, transport, method, path, payload, params):
        self.local()
        operation = self.record['operation']
        if operation == 'ingest_telemetry':
            expected = self.record['payload']
            route = 'telemetry'
        elif operation == 'register_producer':
            expected = dict(schema_version=1, work=self.record['registration']['work'],
                **self.record['payload']['report'], expected_prior=None, expected_revision='0')
            route = 'session-producers'
        else:
            raise Refused('native HTTP operation is not enabled')
        if (transport is not self.client.transport or method != 'POST' or path != route
                or params is not None or payload != expected):
            raise Refused('native HTTP dispatch changed its fixed original operation')

    async def check(self, *, begin=False):
        self.local()
        record = self.record
        address = {key: record[key] for key in ('id', 'ticket', 'digest', 'deadline')}
        async with asyncio.timeout_at(record['deadline']):
            result = await self.worker.exchange(dict(**self.worker.address(
                'native_dispatch' if begin else 'native_validate'), **address))
        if result != address:
            raise Refused('Root native dispatch acknowledgement changed its original operation')
        self.local()

    async def watch(self):
        while True:
            await asyncio.sleep(.025)
            await self.check()

    async def execute(self):
        from mop.node.http_execution import _native_dispatch
        tasks = []
        token = _native_dispatch.set(self)
        try:
            async with asyncio.timeout_at(self.record['deadline']):
                await self.check(begin=True)
                self.started = True
                http = asyncio.create_task(_execute_proxy(self.client, self.record))
                watcher = asyncio.create_task(self.watch())
                tasks = [http, watcher]
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                if watcher in done:
                    watcher.result()
                result = await http
                await self.check()
                return result
        except (Refused, Unavailable, Unknown, TimeoutError, OSError, ValueError) as error:
            # Потерянный Root readback (Unknown) после begin — тоже unknown outcome.
            if self.started:
                reason = str(error) if isinstance(error, (Refused, Unavailable, Unknown)) else type(error).__name__
                raise Unavailable(reason+'; native HTTP outcome unknown; no automatic replay') from error
            raise
        finally:
            for task in tasks:
                if not task.done(): task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            _native_dispatch.reset(token)


async def execute_proxy(client, record, *, worker=None):
    if type(client) is not MachineClient or type(client.transport) is not HttpsTransport:
        raise Refused('native lifetime requires real node MachineClient/HttpsTransport')
    return await NativeDispatch(client, record, worker).execute()


async def _execute_proxy(client, record):
    """NodeUID only: root-selected finite operation, никогда URL/argv/env callback."""
    from mop.node.producer_registration import ProducerRegistrationClient
    from mop.node.telemetry import TelemetryClient
    from mop.node.http_execution import _native_dispatch
    dispatch = _native_dispatch.get()
    if type(dispatch) is not NativeDispatch or dispatch.client is not client or dispatch.record is not record:
        raise Refused('native HTTP dispatch has no original live Root owner')
    await dispatch.check()
    registration = registration_parse(record['registration'])
    if record['operation'] == 'register_producer':
        exact(record['payload'], ('report', 'identity'))
        report = ProducerReport.parse(record['payload']['report'])
        registration.work.gate(node_id=registration.work.node_id, epoch=registration.work.epoch,
            executor_epoch=registration.work.executor_epoch)
        owner = ProducerRegistrationClient(client, registration, expected_prior=None, expected_revision=0)
        await owner.register_report(report)
        return owner.acknowledgement.wire()
    if record['operation'] == 'ingest_telemetry':
        row = Observation.parse(record['payload'])
        w = registration.work
        if tuple(str(getattr(row.scope, key)) for key in
                ('epoch', 'target_id', 'incarnation_id', 'node_id', 'executor_epoch')) != (
                w.epoch, w.target_id, w.incarnation_id, w.node_id, w.executor_epoch):
            raise Refused('native telemetry scope differs from retained registration')
        # Не Work.gate: producer lifetime не равен истёкшей native phase lease.
        ack = await TelemetryClient(client.transport).ingest(row)
        return dict(schema_version=1, producer_epoch=str(ack.producer_epoch), sequence=str(ack.sequence))
    raise Refused('native mailbox operation is not enabled')
