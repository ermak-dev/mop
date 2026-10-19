"""Конечный planned HOST протокол #752; metadata не physical authority."""
from dataclasses import dataclass
from datetime import datetime, timezone
import time

from mop.common.initialcreate import BUDGETS, PLANNED, layout_metadata
from mop.node.host_inputs import (Descriptor, _json, _sha, admission_wire,
    canonical_json, validate_admission, validate_scope)
from mop.node.host_scope import Reference, Scope, canonical, decode, encode, exact
from mop.node.http_execution import Admission, EffectGuard, Refused, Work, counter, stamp

# Каждый entry разрешает ровно следующий compiled effect, не method/path/argv.
CHECKPOINTS = {
    'prepare_body': ('workspace', 'start-broker', 'source-input', 'git-init',
        'git-origin', 'git-fetch', 'git-checkout', 'source-remove'),
    'bootstrap_body': ('bootstrap-input', 'ansible', 'bootstrap-remove'),
    'initial_identity': ('git-name', 'git-email', 'git-use-config-only', 'role'),
}


def require_frame(value, expected):
    """Точное JSON equality без Python True==1/False==0 coercion."""
    if canonical_json(value) != canonical_json(expected):
        raise Refused('finite planned frame fields or exact types changed')


def verify_runner_image(executable, argv, python, runner, scope):
    """PID/start не переживает exec как право заменить installed broker."""
    if (executable != str(python) or type(argv) is not tuple
            or argv != ('/usr/bin/python3', '-ISB', str(runner), '--scope', scope.unit)):
        raise Refused('actual body executable/argv differs from the fixed installed lifecycle broker')


def admitted(guard, work):
    if (type(guard) is not EffectGuard or type(guard.admission) is not Admission
            or guard.admission.work != work):
        raise Refused('planned effect requires the actual Runtime Admission')
    return validate_admission(guard.admission)


def parse_admission(value):
    exact(value, ('work', 'request_id', 'attempt_id', 'revision', 'lease_until'))
    return validate_admission(Admission(Work.parse(value['work']), canonical(value['request_id']),
        canonical(value['attempt_id']), counter(value['revision']), stamp(value['lease_until'])))


def cutoff(admission, *, monotonic=None):
    validate_admission(admission)
    maximum, reserve = BUDGETS[admission.work.action]
    remaining = (admission.lease_until-datetime.now(timezone.utc)).total_seconds()-reserve
    if remaining <= 0:
        raise Refused('planned phase has no effect budget before cleanup reserve')
    effect = min(float(maximum), remaining)
    return effect, (time.monotonic() if monotonic is None else monotonic)+effect


@dataclass(frozen=True)
class Projection:
    display_name: str
    image_revision: str
    image_digest: str
    image_policy: dict
    selection_digest: str
    source: dict
    bootstrap_revision: str
    bootstrap_digest: str
    git_name: str
    git_email: str
    role: str
    bundle_digest: str

    def __post_init__(self):
        try:
            layout_metadata(**{key: getattr(self, key) for key in
                ('display_name', 'image_revision', 'image_policy', 'image_digest')})
        except ValueError as error:
            raise Refused(str(error)) from None
        for item in (self.selection_digest, self.bootstrap_digest, self.bundle_digest): _sha(item)
        canonical(self.bootstrap_revision)
        exact(self.source, ('origin', 'commit', 'ref'))
        from mop.node.host_phase_source import source_origin
        from mop.common.initialcreate import source_ref
        import re
        source_origin(self.source['origin'])
        if not (type(self.source['commit']) is str
                and re.fullmatch('[0-9a-f]{40}|[0-9a-f]{64}', self.source['commit'])
                and source_ref(self.source['ref'])):
            raise Refused('planned source requires exact origin/commit/ref')
        for text, maximum in ((self.git_name, 256), (self.git_email, 256), (self.role, 64)):
            if (type(text) is not str or not 1 <= len(text) <= maximum
                    or any(ord(c) < 32 or ord(c) == 127 for c in text)):
                raise Refused('bounded planned identity required')

    @classmethod
    def from_delivery(cls, value):
        return cls(*(value[key] for key in ('display_name', 'image_revision', 'image_digest', 'image_policy')),
            value['selection']['digest'], dict(value['selection']['source']),
            value['bootstrap_revision'], value['bootstrap_digest'], value['git_name'], value['git_email'],
            value['work']['role'], value['work']['bundle_digest'])

    def wire(self):
        # Отделить caller dict от retained immutable projection.
        return _json(canonical_json(self.__dict__))

    @classmethod
    def parse(cls, value):
        exact(value, cls.__dataclass_fields__)
        return cls(**_json(canonical_json(value)))


@dataclass(frozen=True)
class Submission:
    admission: Admission
    descriptor: Descriptor
    projection: Projection
    reference: Reference | None = None

    def __post_init__(self):
        validate_admission(self.admission)
        if self.admission.work.action not in PLANNED:
            raise Refused('only three planned HOST phases are enabled')
        if type(self.descriptor) is not Descriptor or type(self.projection) is not Projection:
            raise Refused('exact planned descriptor/projection required')
        validate_scope(self.descriptor.scope, self.admission)
        if self.descriptor.attempt_id != self.admission.attempt_id:
            raise Refused('planned phase attempt changed')
        if self.admission.work.action == 'prepare_body':
            if self.reference is not None:
                raise Refused('prepare cannot use a previous physical reference')
        elif type(self.reference) is not Reference or self.reference.scope != self.descriptor.scope:
            raise Refused('planned phase requires the exact retained physical reference')
        if (self.projection.role, self.projection.bundle_digest) != (self.admission.work.role, self.admission.work.bundle_digest):
            raise Refused('planned role binding changed')

    def wire(self):
        return dict(schema_version=1, action=self.admission.work.action,
            admission=admission_wire(self.admission), descriptor=self.descriptor.wire(),
            projection=self.projection.wire(), reference=self.reference.wire() if self.reference else None)

    def encode(self): return encode(self.wire())

    @classmethod
    def decode(cls, raw):
        value = decode(raw)
        exact(value, ('schema_version', 'action', 'admission', 'descriptor', 'projection', 'reference'))
        if type(value['schema_version']) is not int or value['schema_version'] != 1:
            raise Refused('unsupported planned phase schema')
        result = cls(parse_admission(value['admission']), Descriptor.parse(value['descriptor']),
            Projection.parse(value['projection']), Reference.parse(value['reference']) if value['reference'] is not None else None)
        if result.admission.work.action != value['action']:
            raise Refused('planned phase action changed')
        return result


class Checkpoint:
    """Один finite effect; соединение не может запасти approval для другого."""
    def __init__(self, effect, sequence):
        if effect not in {item for plan in CHECKPOINTS.values() for item in plan} or type(sequence) is not int or sequence < 0:
            raise Refused('finite compiled checkpoint required')
        self.effect, self.sequence = effect, sequence
        self.approved = False
        self._connection = self._nonce = None

    def challenge(self, connection):
        from uuid import uuid4
        if self.approved or self._connection is not None:
            raise Refused('checkpoint already approved or has an outstanding connection')
        self._connection, self._nonce = connection, str(uuid4())
        return self._nonce

    def disconnect(self, connection):
        if self._connection is connection:
            self._connection = self._nonce = None

    def approve(self, connection, nonce):
        if self.approved or self._connection is not connection or nonce != self._nonce or self._nonce is None:
            raise Refused('checkpoint response is not the outstanding single-use connection challenge')
        self.approved = True
        self._connection = self._nonce = None


def address(value):
    """Status/cleanup не превращают истёкший Work в новое execution право."""
    exact(value, ('schema_version', 'action', 'scope', 'attempt_id'))
    if (type(value['schema_version']) is not int or value['schema_version'] != 1
            or value['action'] not in ('phase_status', 'phase_checkpoint', 'retire_owned', 'cleanup_owned')):
        raise Refused('finite owned planned phase observation required')
    return Scope.parse(value['scope']), canonical(value['attempt_id'])
