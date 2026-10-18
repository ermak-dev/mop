"""Единственная mutation текущего producer: короткий authenticated SQL CAS.

Порядок locks: installation -> Assignment -> incarnation -> launch admission
(Operations port) -> current producer. Ни внешнего I/O, ни чтения native файлов.
ACK выходит только после outer COMMIT. Смена producer не утверждает retirement.
"""
from dataclasses import dataclass
import hashlib
from typing import Protocol

from django.db import IntegrityError, transaction

from mop.common.producer import ProducerAck, ProducerReport, predecessor
from mop.common.telemetry import CurrentSession, SessionScope, canonical, counter, fields
from . import producer_models as m
from .models import Assignment, ExecutionIncarnation, NodeExecutor
from .protocol import AuthorityV1, WorkV1, work_from_wire, work_to_wire
from .services import Assignments, Conflict, Unavailable, machine


@dataclass(frozen=True)
class RegistrationRequest:
    work: WorkV1
    report: ProducerReport
    expected_prior: SessionScope | None
    expected_revision: int

    def __post_init__(self):
        if type(self.work) is not WorkV1 or type(self.report) is not ProducerReport:
            raise ValueError('typed launch work and observed producer required')
        predecessor(self.expected_prior, self.expected_revision)
        scope, work = self.report.scope, self.work
        if any(getattr(scope, name) != getattr(work, name) for name in
               ('epoch', 'target_id', 'incarnation_id', 'node_id', 'executor_epoch')):
            raise ValueError('observed producer must match exact launch work scope')
        if self.expected_prior is not None and any(getattr(self.expected_prior, name) != getattr(scope, name)
                for name in ('epoch', 'target_id', 'incarnation_id', 'node_id', 'executor_epoch')):
            raise ValueError('predecessor must belong to this launch incarnation')

    def wire(self):
        return dict(schema_version=1, work=work_to_wire(self.work), **self.report.wire(),
            expected_prior=self.expected_prior.wire() if self.expected_prior is not None else None,
            expected_revision=str(self.expected_revision))

    @classmethod
    def parse(cls, value):
        fields(value, ('schema_version', 'work', 'scope', 'launch', 'physical', 'expected_prior', 'expected_revision'))
        if type(value['schema_version']) is not int or value['schema_version'] != 1:
            raise ValueError('unsupported producer registration version')
        revision = 0 if type(value['expected_revision']) is str and value['expected_revision'] == '0' else counter(value['expected_revision'])
        return cls(work_from_wire(value['work']), ProducerReport.parse({key:value[key] for key in ('scope','launch','physical')}),
            SessionScope.parse(value['expected_prior']) if value['expected_prior'] is not None else None, revision)


class LaunchAuthority(Protocol):
    def check(self, actor, work: WorkV1, report: ProducerReport):
        """SQL-only current own claim + fixed job/pins/fences; возвращает attempt UUID."""
        ...


def _scope(row):
    return SessionScope(row.epoch, row.assignment_id, row.incarnation_id, row.node_id,
        row.executor_epoch, row.session_id, row.producer_epoch)


def _digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class CurrentSessions:
    """SQL-only SessionAuthority: pointer не переживает смену exact binding.

    Текущая stage/lease не нужна для чтения после запуска; raw telemetry никогда
    не создаёт/не обновляет этот pointer. Epoch проверяется SQL installation port.
    """
    def __init__(self, *, installation):
        self.installation = installation

    def current(self, binding):
        if binding is None:
            return None
        authority = self.installation()
        if type(authority) is not AuthorityV1:
            raise Unavailable('invalid installation authority')
        # Binding аргумента -- locator/snapshot, не право вернуть старый pointer.
        if Assignments().current(project_id=binding.project_id, target_id=binding.target_id) != binding:
            return None
        row = m.SessionProducer.objects.filter(currentproducer__incarnation_id=binding.incarnation_id,
            incarnation_id=binding.incarnation_id, assignment_id=binding.target_id,
            node_id=binding.node_id, executor_epoch=binding.executor_epoch, epoch=authority.epoch,
            owner_id=binding.owner_id, generation=binding.generation, assignment_revision=binding.revision,
            role=binding.role, bundle_digest=binding.bundle_digest, grant_id=binding.grant_id).first()
        # NodeExecutor -- дополнительный SQL fence, не новый источник эпохи.
        if row is None or not NodeExecutor.objects.filter(node_id=binding.node_id,
                executor_epoch=binding.executor_epoch).exists():
            return None
        return CurrentSession(row.session_id, row.producer_epoch)


class ProducerRegistration:
    def __init__(self, *, installation, launch: LaunchAuthority):
        self.installation, self.launch = installation, launch

    def _check(self, actor, request):
        work = request.work
        authority = self.installation()
        if type(authority) is not AuthorityV1:
            raise Unavailable('invalid installation authority')
        if authority.epoch != work.epoch:
            raise Conflict('installation epoch changed')
        if not authority.effects_enabled:
            raise Unavailable('installation effects are disabled')
        machine(actor, work.node_id, 'register_producer')
        row = Assignment.objects.select_for_update().filter(pk=work.target_id).first()
        if row is None:
            raise Conflict('current launch assignment is unavailable')
        machine(actor, row.node_id, 'register_producer', (row.project_id,))
        binding = Assignments().current(project_id=row.project_id, target_id=row.id)
        if binding is None or any(getattr(binding, name) != getattr(work, name) for name in
                ('target_id','node_id','incarnation_id','executor_epoch','owner_id','generation','revision',
                 'role','bundle_digest','grant_id')):
            raise Conflict('current launch assignment changed')
        incarnation = ExecutionIncarnation.objects.select_for_update().get(pk=work.incarnation_id)
        if (not incarnation.accepting or incarnation.assignment_id != work.target_id
                or incarnation.executor_epoch != work.executor_epoch or incarnation.node_id != work.node_id
                or not NodeExecutor.objects.filter(node_id=work.node_id, executor_epoch=work.executor_epoch).exists()):
            raise Conflict('current launch executor changed')
        return self.launch.check(actor, work, request.report)

    def register(self, actor, request):
        connection = transaction.get_connection()
        if connection.in_atomic_block or not connection.get_autocommit():
            raise Unavailable('producer registration acknowledgement requires no surrounding transaction')
        if type(request) is not RegistrationRequest:
            raise ValueError('typed producer registration request required')
        proof_digest = _digest(dict(work=work_to_wire(request.work), **request.report.wire()))
        request_digest = _digest(request.wire())
        try:
            with transaction.atomic():
                attempt_id = self._check(actor, request)
                current = m.CurrentProducer.objects.select_for_update().select_related('producer').filter(
                    incarnation_id=request.work.incarnation_id).first()
                ack = self._register(actor, request, current, attempt_id, proof_digest, request_digest)
                # Последняя проверка не полагается на context, полученный HTTP
                # authentication до proof. Token/enrollment/RBAC/grant/claim могут
                # быть отозваны; ни ACK, ни pointer не выходят из такого запроса.
                if self._check(actor, request) != attempt_id:
                    raise Conflict('producer launch admission changed before acknowledgement')
            return ack
        except IntegrityError:
            if m.SessionProducer.objects.filter(producer_epoch=request.report.scope.producer_epoch).exists():
                raise Conflict('producer epoch already registered') from None
            raise

    def _register(self, actor, request, current, attempt_id, proof_digest, request_digest):
        scope, work = request.report.scope, request.work
        if current is not None:
            old = current.producer
            if _scope(old) == scope:
                exact = old.proof_digest == proof_digest and old.executor_user_id == actor.user_id
                retry = old.request_digest == request_digest
                renew = request.expected_revision == current.revision and request.expected_prior == scope
                if not exact or not (retry or renew):
                    raise Conflict('producer retry or renewal changed evidence or predecessor')
                return ProducerAck(scope, current.revision)
            if request.expected_revision != current.revision or request.expected_prior != _scope(old):
                raise Conflict('current producer predecessor or revision changed')
        elif request.expected_revision != 0 or request.expected_prior is not None:
            raise Conflict('first producer requires null predecessor and zero revision')
        if m.SessionProducer.objects.filter(producer_epoch=scope.producer_epoch).exists():
            raise Conflict('historical producer epoch cannot become current again')
        revision = current.revision+1 if current is not None else 1
        if revision >= 2**63:
            raise Conflict('producer revision exhausted')
        report = request.report
        row = m.SessionProducer.objects.create(assignment_id=work.target_id, incarnation_id=work.incarnation_id,
            node_id=work.node_id, epoch=scope.epoch, executor_epoch=scope.executor_epoch,
            producer_epoch=scope.producer_epoch, session_id=scope.session_id, executor_user_id=actor.user_id,
            owner_id=work.owner_id, generation=work.generation, assignment_revision=work.revision,
            role=work.role, bundle_digest=work.bundle_digest, grant_id=work.grant_id,
            operation_id=work.operation_id, step_id=work.step_id, attempt_id=attempt_id,
            proof_digest=proof_digest, request_digest=request_digest, accepted_revision=revision,
            pane=report.pane, pid=report.pid, process_start=report.process_start)
        if current is None:
            m.CurrentProducer.objects.create(incarnation_id=work.incarnation_id, producer=row, revision=revision)
        else:
            changed = m.CurrentProducer.objects.filter(pk=current.pk, revision=request.expected_revision,
                producer=current.producer).update(producer=row, revision=revision)
            if changed != 1:
                raise Conflict('current producer compare-and-swap failed')
        return ProducerAck(scope, revision)
