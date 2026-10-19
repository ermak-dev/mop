"""Read-only initial-ready proof; тот же SQL snapshot проверяется внутри result.

Assignment lock сериализует producer CAS и telemetry ingest до result commit.
Никаких active-management prerequisites, mint, latest fallback или provider I/O.
"""
import hashlib
from uuid import UUID

from django.db import transaction
from django.db.models import F

from mop.common.producer import ProducerReport
from mop.common.readiness import ReadyRequest, ReadyEvidence
from mop.common.telemetry import canonical
from . import models as m
from .producer import CurrentSessions
from .producer_models import CurrentProducer
from .protocol import work_from_wire, work_to_wire
from .services import Assignments, Conflict, machine
from .telemetry import Telemetry


def query(execution, actor, request):
    """Public Sessions owner query, caller держит installation/Assignment locks."""
    if type(request) is not ReadyRequest or not transaction.get_connection().in_atomic_block:
        raise ValueError('transactional typed readiness request required')
    addressed = work_from_wire(request.work)
    work, attempt_id = execution.action_admission(actor,
        operation_id=addressed.operation_id, step_id=addressed.step_id)
    attempt = m.AssignmentAttempt.objects.get(pk=attempt_id)
    if (work != addressed or attempt_id != request.attempt_id or attempt.request_id != request.request_id
            or attempt.revision != request.revision or attempt.lease_until != request.lease_until):
        raise Conflict('exact current claimed readiness admission required')
    binding = Assignments().target(work.target_id, lock=True)
    if binding is None:
        raise Conflict('current readiness assignment is unavailable')
    machine(actor, binding.node_id, 'read_launch', (binding.project_id,))
    pins, native_id = execution.work.ready_launch(work, attempt_id)
    source = CurrentSessions(installation=execution.installation).current(binding)
    scope = request.scope
    if source is None or (source.session_id, source.producer_epoch) != (scope.session_id, scope.producer_epoch):
        raise Conflict('exact authoritative native producer required')
    current = CurrentProducer.objects.select_related('producer').get(incarnation_id=binding.incarnation_id)
    producer = current.producer
    native = m.AssignmentAttempt.objects.select_related('incarnation').filter(pk=native_id,
        assignment_id=work.target_id, incarnation_id=work.incarnation_id, epoch=work.epoch,
        executor_epoch=work.executor_epoch, executor_user_id=actor.user_id, owner_id=work.owner_id,
        generation=work.generation, assignment_revision=work.revision, role=work.role,
        bundle_digest=work.bundle_digest, grant_id=work.grant_id, action='native_exec', state='succeeded',
        operation_id=work.operation_id).first()
    if (native is None or producer.attempt_id != native_id or producer.step_id != native.step_id
            or producer.operation_id != work.operation_id or producer.executor_user_id != actor.user_id
            or producer.pid != request.pid or producer.process_start != request.process_start
            or current.revision != producer.accepted_revision
            or not m.ResultReceipt.objects.filter(attempt=native, outcome='succeeded', evidence='native-started',
                resulting_revision=F('attempt__revision'), expected_revision=F('attempt__revision')-1).exists()):
        raise Conflict('original successful native execution and unchanged observed process required')
    report = ProducerReport(scope, UUID(pins['bootstrap_revision']), pins['bootstrap_digest'],
        pins['image_digest'], pins['display_name'], producer.pane, request.pid, request.process_start)
    digest = hashlib.sha256(canonical(dict(work=work_to_wire(execution._work(native)), **report.wire())).encode()).hexdigest()
    if digest != producer.proof_digest:
        raise Conflict('original native producer registration pins changed')
    model = Telemetry(installation=execution.installation).ready_model(scope, pins['desired_model'])
    return ReadyEvidence(request, UUID(pins['bootstrap_revision']), pins['bootstrap_digest'], pins['image_digest'],
        pins['display_name'], UUID(pins['image_revision']), tuple(sorted(pins['image_policy'].items())),
        native_id, producer.id, current.revision, model.receipt_id, model.confirmed_sequence,
        model.observed_sequence, model.observation_revision, model.model)
