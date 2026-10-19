"""Конечный private transport initial phase bytes (#746), не execution authority.

Root проверяет только outer JSON и byte identity. Project YAML/consumer metadata
остаётся opaque строкой до строгого body reader. Machine guard, root backend
verification и physical phase runner должны быть связаны отдельно.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import re

from mop.common import initial_transfer as transfer
from mop.node.host_scope import Scope, canonical, exact, unique
from mop.node.http_execution import Admission, Refused, Work, counter, stamp

MAX_FILE_BYTES = 1048576
MAX_TOTAL_BYTES = 16777216
PURPOSES = transfer.PURPOSES
DELIVERY_FIELDS = ('schema_version', 'work', 'phase', 'bootstrap_revision', 'bootstrap_digest',
    'image_digest', 'display_name', 'image_revision', 'image_policy', 'selection',
    'git_name', 'git_email', 'role', 'bootstrap', 'consumers')


def now():
    return datetime.now(timezone.utc)


_DEFAULT = object()


@dataclass(frozen=True, init=False)
class Limits:
    max_files: int = 64
    max_bytes: int = 8388608
    max_file_bytes: int = 262144
    _bootstrap_bytes: int = field(default=262144, init=False, repr=False)

    def __init__(self, max_files=64, max_bytes=8388608, max_file_bytes=_DEFAULT):
        default = max_file_bytes is _DEFAULT
        object.__setattr__(self, 'max_files', max_files)
        object.__setattr__(self, 'max_bytes', max_bytes)
        object.__setattr__(self, 'max_file_bytes', transfer.DEFAULT_BYTES if default else max_file_bytes)
        self.__post_init__()
        # Explicit caller caps остаются hard, включая явно переданные 262144.
        object.__setattr__(self, '_bootstrap_bytes', min(max_bytes, transfer.phase_limit('bootstrap_body'))
            if default else max_file_bytes)

    def file_bytes(self, action):
        return self._bootstrap_bytes if action == 'bootstrap_body' else self.max_file_bytes

    def __post_init__(self):
        if (type(self.max_files) is not int or not 1 <= self.max_files <= 256
                or type(self.max_bytes) is not int or not 1 <= self.max_bytes <= MAX_TOTAL_BYTES
                or type(self.max_file_bytes) is not int
                or not 1 <= self.max_file_bytes <= min(self.max_bytes, MAX_FILE_BYTES)):
            raise Refused('bounded phase input capacity required')


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def _sha(value):
    if type(value) is not str or not re.fullmatch('[0-9a-f]{64}', value):
        raise Refused('phase input SHA256 required')
    return value


def canonical_json(value):
    try:
        return transfer.canonical_json(value)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise Refused('finite phase input JSON required') from None


def _nonfinite(value):
    raise Refused('finite phase input JSON required')


def _json(raw):
    try:
        return json.loads(raw, object_pairs_hook=unique, parse_constant=_nonfinite)
    except Refused:
        raise
    except (ValueError, UnicodeError, RecursionError):
        raise Refused('invalid finite phase input JSON') from None


def admission_wire(admission):
    return dict(work=admission.work.wire(), request_id=admission.request_id,
                attempt_id=admission.attempt_id, revision=str(admission.revision),
                lease_until=admission.lease_until.isoformat())


def validate_admission(admission):
    try:
        if type(admission) is not Admission or type(admission.work) is not Work:
            raise Refused('typed phase input Admission required')
        work = admission.work
        if Work.parse(work.wire()) != work or work.action not in PURPOSES:
            raise Refused('finite initial phase input Work required')
        canonical(admission.request_id)
        canonical(admission.attempt_id)
        if (type(admission.revision) is not int or not 0 < admission.revision < 2**63
                or type(admission.lease_until) is not datetime
                or admission.lease_until.tzinfo is None
                or not now() < admission.lease_until <= work.not_after):
            raise Refused('current phase input admission lease required')
    except (TypeError, AttributeError, OverflowError):
        raise Refused('invalid phase input Admission') from None
    return admission


def validate_scope(scope, admission):
    if type(scope) is not Scope:
        raise Refused('typed phase input Scope required')
    work = admission.work
    if scope != Scope(work.epoch, work.node_id, work.executor_epoch, work.target_id,
                      work.incarnation_id, work.operation_id):
        raise Refused('phase input Work and Scope mismatch')


@dataclass(frozen=True)
class Descriptor:
    scope: Scope
    attempt_id: str
    sha256: str

    def __post_init__(self):
        if type(self.scope) is not Scope:
            raise Refused('typed phase input Scope required')
        canonical(self.attempt_id)
        _sha(self.sha256)

    @property
    def filename(self):
        # Обратимое имя из всех Scope UUID + attempt, без wire path/consumer name.
        return self.scope.unit.removesuffix('.service')+'-'+self.attempt_id+'.json'

    def wire(self):
        return dict(scope=self.scope.wire(), attempt_id=self.attempt_id, sha256=self.sha256)

    @classmethod
    def parse(cls, value):
        exact(value, ('scope', 'attempt_id', 'sha256'))
        return cls(Scope.parse(value['scope']), value['attempt_id'], value['sha256'])


@dataclass(frozen=True)
class Envelope:
    descriptor: Descriptor
    admission: Admission
    raw: bytes = field(repr=False)
    payload: bytes = field(repr=False)

    @classmethod
    def parse(cls, raw, descriptor, admission, *, backend_epoch, node_id, executor_epoch):
        validate_admission(admission)
        if type(descriptor) is not Descriptor:
            raise Refused('typed phase input descriptor required')
        for item in (backend_epoch, node_id, executor_epoch):
            canonical(item)
        validate_scope(descriptor.scope, admission)
        scope = descriptor.scope
        if (scope.epoch, scope.node_id, scope.executor_epoch) != (backend_epoch, node_id, executor_epoch):
            raise Refused('phase input trusted backend or node mismatch')
        if (type(raw) is not bytes or not 0 < len(raw) <= MAX_FILE_BYTES
                or digest(raw) != descriptor.sha256):
            raise Refused('phase input bytes exceed bound or digest mismatch')
        value = _json(raw)
        exact(value, ('schema_version', 'purpose', 'scope', 'admission', 'payload_digest', 'payload'))
        if (type(value['schema_version']) is not int or value['schema_version'] != 1
                or value['purpose'] != PURPOSES[admission.work.action]
                or Scope.parse(value['scope']) != scope):
            raise Refused('finite initial phase input purpose and Scope required')
        claim = value['admission']
        exact(claim, ('work', 'request_id', 'attempt_id', 'revision', 'lease_until'))
        actual = Admission(Work.parse(claim['work']), canonical(claim['request_id']),
                          canonical(claim['attempt_id']), counter(claim['revision']), stamp(claim['lease_until']))
        validate_admission(actual)
        if actual != admission or descriptor.attempt_id != actual.attempt_id:
            raise Refused('phase input admission or attempt changed')
        payload = value['payload']
        if type(payload) is not str or not payload or not payload.isascii():
            raise Refused('opaque canonical phase input payload required')
        payload = payload.encode('ascii')
        if digest(payload) != _sha(value['payload_digest']) or canonical_json(value) != raw:
            raise Refused('phase input payload digest or canonical bytes changed')
        return cls(descriptor, actual, raw, payload)


@dataclass(frozen=True)
class BodyDelivery:
    value: dict = field(repr=False)
    payload: bytes = field(repr=False)


def _delivery(value, work):
    from mop.common.credentials import CredentialError
    from mop.common.initialcreate import parse_delivery
    try:
        # Извлечение только конечных selected fields, не vars(client)/ambient env.
        exact(value, DELIVERY_FIELDS)
        selected = {name: value[name] for name in DELIVERY_FIELDS}
        selected = _json(canonical_json(selected))
        parse_delivery(selected, work.wire())
        return selected
    except (CredentialError, Refused) as error:
        # Конечные безопасные library phrases не проходят вторую санитизацию.
        raise Refused(str(error)) from None
    except (ValueError, TypeError, KeyError, RecursionError, UnicodeError):
        # Soul parser может отражать malformed manifest/prompt в raw ValueError.
        raise Refused('invalid exact initial phase delivery') from None


def pack(admission, scope, delivery):
    validate_admission(admission)
    validate_scope(scope, admission)
    selected = _delivery(delivery, admission.work)
    payload = canonical_json(selected)
    raw = transfer.private_frame(payload, phase=admission.work.action,
        scope=scope.wire(), admission=admission_wire(admission))
    descriptor = Descriptor(scope, admission.attempt_id, digest(raw))
    return Envelope.parse(raw, descriptor, admission, backend_epoch=scope.epoch,
                          node_id=scope.node_id, executor_epoch=scope.executor_epoch)


def parse_body(envelope):
    if type(envelope) is not Envelope:
        raise Refused('typed phase input envelope required')
    validate_admission(envelope.admission)
    selected = _delivery(_json(envelope.payload), envelope.admission.work)
    if canonical_json(selected) != envelope.payload:
        raise Refused('canonical body phase payload required')
    return BodyDelivery(selected, envelope.payload)
