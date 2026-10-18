"""Версия 1: неизменяемые DTO и узкие порты, без Django/HTTP и импорта node."""
from dataclasses import dataclass, fields
from datetime import datetime, timezone
import re
from typing import Protocol, Callable
from uuid import UUID

ACTIONS = frozenset(('restart_session', 'apply_identity', 'send_message', 'confirm_ready', 'wipe_clone'))
OUTCOMES = frozenset(('succeeded', 'failed', 'unknown'))
EVIDENCE = frozenset(('session-replaced', 'identity-applied', 'ready', 'delivered', 'clone-wiped',
    'confirmed', 'connection-lost', 'not-started', 'failed-before-effect', 'effect-running'))
FACT_FIELDS = {
    'states': frozenset(('running', 'dirty', 'session_count')),
    'clone': frozenset(('clean', 'branch', 'untracked', 'modified')),
    'sizes': frozenset(('bytes',)),
    'disk': frozenset(('free_bytes', 'total_bytes')),
    'junk': frozenset(('candidate_count', 'bytes', 'inventory_complete')),
    'health': frozenset(('healthy', 'latency_ms')),
}


def positive(value):
    if type(value) is not int or not 1 <= value < 2**63:
        raise ValueError('positive integer required')
    return value


def uuid_value(value):
    if type(value) is not str:
        raise ValueError('canonical UUID required')
    try:
        result = UUID(value)
    except (ValueError, AttributeError):
        raise ValueError('canonical UUID required') from None
    if str(result) != value:
        raise ValueError('canonical UUID required')
    return result


def counter(value):
    if type(value) is not str or not re.fullmatch(r'[1-9][0-9]{0,18}', value):
        raise ValueError('decimal counter string required')
    return positive(int(value))


def deadline(value):
    if type(value) is not str or len(value) > 40:
        raise ValueError('aware deadline required')
    try:
        result = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError('aware deadline required') from None
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError('aware deadline required')
    return result.astimezone(timezone.utc)


def exact(value, names):
    if type(value) is not dict or set(value) != set(names):
        raise ValueError('unknown or missing fields')
    if type(value.get('schema_version')) is not int or value['schema_version'] != 1:
        raise ValueError('unsupported schema version')


def role_binding(role, bundle_digest, grant_id):
    if (type(role) is not str or not re.fullmatch(r'[a-z][a-z0-9_-]{0,63}', role)
            or type(bundle_digest) is not str or not re.fullmatch(r'[0-9a-f]{64}', bundle_digest)
            or type(grant_id) is not UUID):
        raise ValueError('complete pinned role binding required')


@dataclass(frozen=True)
class AuthorityV1:
    epoch: UUID
    effects_enabled: bool

    def __post_init__(self):
        if type(self.epoch) is not UUID or type(self.effects_enabled) is not bool:
            raise ValueError('installation authority required')


@dataclass(frozen=True)
class AssignmentBinding:
    """Публичная внутренняя read projection, не grant или machine admission."""
    target_id: UUID
    project_id: UUID
    node_id: UUID
    incarnation_id: UUID
    executor_epoch: UUID
    owner_id: int
    role: str
    bundle_digest: str
    grant_id: UUID
    generation: int
    revision: int
    identity_applied: bool
    ready: bool


@dataclass(frozen=True)
class CredentialAssignment:
    """Точное текущее назначение и immutable подтверждения, не транспортный revoke."""
    binding: AssignmentBinding
    epoch: UUID
    identity_receipt_id: UUID
    readiness_receipt_id: UUID


@dataclass(frozen=True)
class WorkV1:
    epoch: UUID
    operation_id: UUID
    step_id: UUID
    target_id: UUID
    incarnation_id: UUID
    node_id: UUID
    executor_epoch: UUID
    owner_id: int
    generation: int
    revision: int
    role: str
    bundle_digest: str
    grant_id: UUID
    action: str
    not_after: datetime

    def __post_init__(self):
        for name in ('epoch', 'operation_id', 'step_id', 'target_id', 'incarnation_id',
                     'node_id', 'executor_epoch', 'grant_id'):
            if type(getattr(self, name)) is not UUID:
                raise ValueError('typed UUID required for ' + name)
        for value in (self.owner_id, self.generation, self.revision):
            positive(value)
        role_binding(self.role, self.bundle_digest, self.grant_id)
        if (type(self.action) is not str or self.action not in ACTIONS or type(self.not_after) is not datetime
                or self.not_after.tzinfo is None or self.not_after.utcoffset() is None):
            raise ValueError('named action and aware deadline required')


def work_to_wire(work):
    if type(work) is not WorkV1:
        raise ValueError('version 1 work required')
    return {'schema_version': 1, **{field.name: (
        str(getattr(work, field.name)) if field.name not in ('not_after', 'role', 'bundle_digest', 'action')
        else work.not_after.isoformat() if field.name == 'not_after' else getattr(work, field.name))
        for field in fields(WorkV1)}}


def work_from_wire(value):
    exact(value, {'schema_version', *(field.name for field in fields(WorkV1))})
    values = {name: uuid_value(value[name]) for name in ('epoch', 'operation_id', 'step_id',
        'target_id', 'incarnation_id', 'node_id', 'executor_epoch', 'grant_id')}
    values.update({name: counter(value[name]) for name in ('owner_id', 'generation', 'revision')})
    return WorkV1(**values, role=value['role'], bundle_digest=value['bundle_digest'],
                  action=value['action'], not_after=deadline(value['not_after']))


def result_codes(outcome, evidence):
    if type(outcome) is not str or outcome not in OUTCOMES or type(evidence) is not str or evidence not in EVIDENCE:
        raise ValueError('named outcome and public evidence code required')


@dataclass(frozen=True)
class ReceiptV1:
    id: UUID
    attempt_id: UUID
    revision: int
    outcome: str
    evidence: str

    def __post_init__(self):
        if type(self.id) is not UUID or type(self.attempt_id) is not UUID:
            raise ValueError('typed receipt identity required')
        positive(self.revision)
        result_codes(self.outcome, self.evidence)

    def to_wire(self):
        return {'schema_version': 1, 'receipt_id': str(self.id), 'attempt_id': str(self.attempt_id),
                'revision': str(self.revision), 'outcome': self.outcome, 'evidence': self.evidence}


@dataclass(frozen=True)
class FactV1:
    target_id: UUID | None
    incarnation_id: UUID | None
    sequence: int
    observed_at: datetime
    received_at: datetime
    state: str
    values: tuple[tuple[str, int | str | bool | None], ...]


@dataclass(frozen=True)
class FactPageV1:
    node_id: UUID
    kind: str
    complete: bool
    items: tuple[FactV1, ...]

    def validate(self, *, node_id, kind, limit):
        if (self.node_id != node_id or self.kind != kind or type(self.complete) is not bool
                or type(self.items) is not tuple or len(self.items) > limit):
            raise ValueError('invalid bounded fact page')
        for item in self.items:
            if (type(item) is not FactV1 or item.state not in ('fresh', 'stale', 'unavailable')
                    or type(item.values) is not tuple or len(item.values) > len(FACT_FIELDS[kind])):
                raise ValueError('invalid fact metadata')
            positive(item.sequence)
            for identity in (item.target_id, item.incarnation_id):
                if identity is not None and type(identity) is not UUID:
                    raise ValueError('invalid fact identity')
            for stamp in (item.observed_at, item.received_at):
                if type(stamp) is not datetime or stamp.tzinfo is None or stamp.utcoffset() is None:
                    raise ValueError('invalid fact timestamp')
            if item.observed_at > item.received_at or item.received_at > datetime.now(timezone.utc):
                raise ValueError('invalid fact ordering')
            names = set()
            for name, value in item.values:
                if (name not in FACT_FIELDS[kind] or name in names or type(value) not in (int, str, bool, type(None))
                        or type(value) is str and (len(value) > 256 or any(ord(c) < 32 for c in value))
                        or type(value) is int and not 0 <= value < 2**63):
                    raise ValueError('invalid named fact')
                names.add(name)
        return self


@dataclass(frozen=True)
class InspectionV1:
    attempt_id: UUID
    incarnation_id: UUID
    executor_epoch: UUID
    outcome: str
    evidence: str
    quiescent: bool

    def __post_init__(self):
        if (any(type(value) is not UUID for value in (self.attempt_id, self.incarnation_id, self.executor_epoch))
                or type(self.quiescent) is not bool):
            raise ValueError('typed effect inspection required')
        result_codes(self.outcome, self.evidence)


class EffectInspector(Protocol):
    """Только read/inspection вне транзакции, никакого retry или compensation."""
    def inspect(self, work: WorkV1, attempt_id: UUID) -> InspectionV1: ...


class WorkPort(Protocol):
    """Только ORM existing Operation/Step; caller transaction, без внешнего I/O."""
    def pending(self, *, node_id: UUID, executor_epoch: UUID, project_ids: tuple[UUID, ...],
                limit: int) -> tuple[WorkV1, ...]: ...
    def current(self, operation_id: UUID, step_id: UUID) -> WorkV1: ...
    def claimed(self, work: WorkV1, attempt_id: UUID) -> None: ...
    def completed_result(self, work: WorkV1, attempt_id: UUID, outcome: str, evidence: str) -> None: ...
    def retired(self, target_id: UUID, executor_epoch: UUID) -> None: ...


class GrantAuthority(Protocol):
    def authorize(self, *, owner_id: int, project_id: UUID, target_id: UUID, grant_id: UUID,
                  role: str, bundle_digest: str, incarnation_id: UUID, action: str) -> None: ...


class CleanCloneQuery(Protocol):
    """Только свежий clone fact для конкретного source; не присвоение/repair."""
    def observe(self, *, target_id: UUID, incarnation_id: UUID, node_id: UUID) -> FactV1: ...


class FactReader(Protocol):
    """Bounded read вне ORM transaction; отказ/unknown не превращается в empty."""
    def read(self, *, node_id: UUID, kind: str, limit: int) -> FactPageV1: ...


@dataclass(frozen=True)
class ReplacementLaunch:
    """Пины destination; Root проверяет одобренную policy, не latest."""
    bootstrap_revision: UUID
    bootstrap_digest: str
    image_digest: str
    image_revision_id: UUID | None

    def __post_init__(self):
        if (type(self.bootstrap_revision) is not UUID
                or self.image_revision_id is not None and type(self.image_revision_id) is not UUID
                or any(type(value) is not str or re.fullmatch('[0-9a-f]{64}', value) is None
                       for value in (self.bootstrap_digest, self.image_digest))):
            raise ValueError('pinned replacement launch required')


@dataclass(frozen=True)
class ReplacementAuthority:
    """Внутренний trusted SQL proof; wire parser намеренно отсутствует.

    receipt/index означают successful scheduler stop (stop/wipe) или exact
    registration readback (cutover), не claim, отправленный POST или observed
    job без operation token. Для recycle cutover Root также проверяет wipe.
    """
    operation_id: UUID
    step_id: UUID
    token: UUID
    epoch: UUID
    action: str
    phase: str
    source: AssignmentBinding
    source_expires_at: datetime
    destination_incarnation_id: UUID
    destination_grant_id: UUID
    destination_generation: int
    destination_revision: int
    expires_at: datetime
    launch: ReplacementLaunch
    receipt_id: UUID
    modify_index: int

    def __post_init__(self):
        for name in ('operation_id', 'step_id', 'token', 'epoch', 'destination_incarnation_id',
                     'destination_grant_id', 'receipt_id'):
            if type(getattr(self, name)) is not UUID:
                raise ValueError('typed replacement ' + name + ' required')
        if (type(self.source) is not AssignmentBinding or type(self.launch) is not ReplacementLaunch
                or type(self.action) is not str or self.action not in ('update', 'recycle')
                or type(self.phase) is not str or self.phase not in ('stop', 'cutover', 'wipe')
                or self.phase != 'cutover' and self.action != 'recycle'):
            raise ValueError('finite typed replacement authority required')
        for value in (self.modify_index, self.destination_generation, self.destination_revision,
                      self.source.generation, self.source.revision, self.source.owner_id):
            positive(value)
        for name in ('target_id', 'project_id', 'node_id', 'incarnation_id', 'executor_epoch'):
            if type(getattr(self.source, name)) is not UUID:
                raise ValueError('typed source binding required')
        role_binding(self.source.role, self.source.bundle_digest, self.source.grant_id)
        for stamp in (self.source_expires_at, self.expires_at):
            if type(stamp) is not datetime or stamp.tzinfo is None or stamp.utcoffset() is None:
                raise ValueError('aware replacement expiry required')


class ReplacementPort(Protocol):
    """Root-only SQL, caller transaction; не capability от caller JSON.

    require берёт restore -> Assignment -> Operation -> Steps -> sorted
    Resource locks, перечитывает actor/current rights, epoch, exact source,
    immutable destination/policy/launch, exact held claims и stage token.
    Проспективный grant обязан быть reserved с точным destination binding,
    owner/subject и сроком, без смены Actor/token или потери ancestry. При
    cutover требуется successful exact registration readback; при stop --
    successful quiescent scheduler receipt с фактическим applied index; при
    wipe -- этот stop и текущие права/deadline. Recycle registration требует
    предшествующий successful wipe того же operation. Исторический receipt
    не даёт повторить эффект. Никаких provider/network calls под locks.
    """
    def require(self, *, operation_id: UUID, step_id: UUID, token: UUID,
                phase: str) -> ReplacementAuthority: ...
    def stopped(self, work: WorkV1) -> ReplacementAuthority:
        """Текущий claimed recycle wipe, включая его token и old-grant authority."""
        ...


class StoppedSourceAuthority(Protocol):
    """Исключение только для exact wipe, не общий current/GrantAuthority."""
    def authorize(self, work: WorkV1) -> None: ...


InstallationQuery = Callable[[], AuthorityV1]
