"""Конечные locators начальной готовности (#749), только stdlib.

Ни Work, ни admission, ни receipt на wire не дают полномочий. Сервер заново
проверяет их владельцев; успешный result сравнивает тот же immutable snapshot.
TelemetryReceipt имеет настоящий bigint id, не выдуманный UUID ready receipt.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
import re
from uuid import UUID

from .initialcreate import layout_metadata
from .telemetry import SessionScope, counter, fields, positive, text, uuid


def stamp(value):
    if type(value) is not str or len(value) > 40:
        raise ValueError('aware readiness deadline required')
    result = datetime.fromisoformat(value)
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError('aware readiness deadline required')
    return result.astimezone(timezone.utc)


def version(value):
    if type(value) is not int or value != 1:
        raise ValueError('unsupported readiness schema version')


def work_document(value):
    fields(value, ('schema_version', 'epoch', 'operation_id', 'step_id', 'target_id', 'incarnation_id',
        'node_id', 'executor_epoch', 'owner_id', 'generation', 'revision', 'role', 'bundle_digest',
        'grant_id', 'action', 'not_after'))
    version(value['schema_version'])
    for key in ('epoch', 'operation_id', 'step_id', 'target_id', 'incarnation_id', 'node_id', 'executor_epoch', 'grant_id'):
        uuid(value[key])
    for key in ('owner_id', 'generation', 'revision'):
        counter(value[key])
    if (value['action'] != 'confirm_ready' or type(value['role']) is not str
            or re.fullmatch('[a-z][a-z0-9_-]{0,63}', value['role']) is None):
        raise ValueError('exact confirm-ready work required')
    digest(value['bundle_digest'])
    stamp(value['not_after'])
    return tuple(sorted(value.items()))


def digest(value):
    if type(value) is not str or re.fullmatch('[0-9a-f]{64}', value) is None:
        raise ValueError('pinned readiness digest required')


@dataclass(frozen=True)
class ReadyRequest:
    work_fields: tuple
    attempt_id: UUID
    request_id: UUID
    revision: int
    lease_until: datetime
    scope: SessionScope
    pid: int
    process_start: str

    def __post_init__(self):
        if type(self.work_fields) is not tuple or work_document(dict(self.work_fields)) != self.work_fields:
            raise ValueError('immutable canonical readiness work required')
        work = dict(self.work_fields)
        if (type(self.scope) is not SessionScope or any(type(item) is not UUID for item in (self.attempt_id, self.request_id))
                or type(self.lease_until) is not datetime or self.lease_until.tzinfo is None
                or not self.lease_until <= stamp(work['not_after'])
                or any(str(getattr(self.scope, name)) != work[name] for name in
                    ('epoch', 'target_id', 'incarnation_id', 'node_id', 'executor_epoch'))
                or type(self.pid) is not int or not 0 < self.pid < 2**31
                or type(self.process_start) is not str or len(self.process_start) > 56):
            raise ValueError('exact readiness admission and observed process required')
        positive(self.revision)
        parts = self.process_start.split(':')
        if len(parts) != 2:
            raise ValueError('observed boot UUID and start ticks required')
        uuid(parts[0])
        counter(parts[1])

    @property
    def work(self):
        return dict(self.work_fields)

    def wire(self):
        return dict(schema_version=1, work=self.work, admission=dict(attempt_id=str(self.attempt_id),
            request_id=str(self.request_id), revision=str(self.revision), lease_until=self.lease_until.isoformat()),
            scope=self.scope.wire(), physical=dict(pid=str(self.pid), process_start=self.process_start))

    @classmethod
    def parse(cls, value):
        fields(value, ('schema_version', 'work', 'admission', 'scope', 'physical'))
        version(value['schema_version'])
        admission, physical = value['admission'], value['physical']
        fields(admission, ('attempt_id', 'request_id', 'revision', 'lease_until'))
        fields(physical, ('pid', 'process_start'))
        return cls(work_document(value['work']), uuid(admission['attempt_id']), uuid(admission['request_id']),
            counter(admission['revision']), stamp(admission['lease_until']), SessionScope.parse(value['scope']),
            counter(physical['pid']), physical['process_start'])


@dataclass(frozen=True)
class ReadyEvidence:
    request: ReadyRequest
    bootstrap_revision: UUID
    bootstrap_digest: str
    image_digest: str
    display_name: str
    image_revision: UUID
    image_policy_fields: tuple
    native_attempt_id: UUID
    producer_id: UUID
    producer_revision: int
    receipt_id: int
    confirmed_sequence: int
    observed_sequence: int
    observation_revision: int
    model: str

    def __post_init__(self):
        if (type(self.request) is not ReadyRequest or any(type(item) is not UUID for item in
                (self.bootstrap_revision, self.image_revision, self.native_attempt_id, self.producer_id))
                or type(self.image_policy_fields) is not tuple
                or tuple(sorted(dict(self.image_policy_fields).items())) != self.image_policy_fields
                or not text(self.model)):
            raise ValueError('typed committed readiness evidence required')
        for value in (self.producer_revision, self.receipt_id, self.confirmed_sequence,
                      self.observed_sequence, self.observation_revision):
            positive(value)
        if self.confirmed_sequence > self.observed_sequence:
            raise ValueError('confirmed switch cannot follow the observed sequence')
        digest(self.bootstrap_digest)
        layout_metadata(display_name=self.display_name, image_revision=str(self.image_revision),
            image_policy=dict(self.image_policy_fields), image_digest=self.image_digest)

    def wire(self):
        return dict(schema_version=1, request=self.request.wire(), launch=dict(
            bootstrap_revision=str(self.bootstrap_revision), bootstrap_digest=self.bootstrap_digest,
            image_digest=self.image_digest, display_name=self.display_name,
            image_revision=str(self.image_revision), image_policy=dict(self.image_policy_fields)),
            native_attempt_id=str(self.native_attempt_id), producer_id=str(self.producer_id),
            **{key: str(getattr(self, key)) for key in ('producer_revision', 'receipt_id',
                'confirmed_sequence', 'observed_sequence', 'observation_revision')}, model=self.model)

    @classmethod
    def parse(cls, value, request=None):
        fields(value, ('schema_version', 'request', 'launch', 'native_attempt_id', 'producer_id',
            'producer_revision', 'receipt_id', 'confirmed_sequence', 'observed_sequence', 'observation_revision', 'model'))
        version(value['schema_version'])
        observed = ReadyRequest.parse(value['request'])
        if request is not None and (type(request) is not ReadyRequest or observed != request):
            raise ValueError('readiness evidence changed the addressed admission or process')
        launch = value['launch']
        fields(launch, ('bootstrap_revision', 'bootstrap_digest', 'image_digest', 'display_name', 'image_revision', 'image_policy'))
        fields(launch['image_policy'], ('policy_id', 'cpu', 'memory_mb', 'disk_mb'))
        return cls(observed, uuid(launch['bootstrap_revision']), launch['bootstrap_digest'], launch['image_digest'],
            launch['display_name'], uuid(launch['image_revision']), tuple(sorted(launch['image_policy'].items())),
            uuid(value['native_attempt_id']), uuid(value['producer_id']), counter(value['producer_revision']),
            counter(value['receipt_id']), counter(value['confirmed_sequence']), counter(value['observed_sequence']),
            counter(value['observation_revision']), value['model'])
