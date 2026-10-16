"""Конечный restart handler; общий журнал/claims, никакой второй очереди.

Порты Targets/RestartSupport SQL-only: guard -> Assignment -> Operation -> Step.
Сеть и физический mapping остаются за controller composition и узлом.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import json
from uuid import UUID, uuid4

from django.db import transaction
from django.utils import timezone
from mop.server.access.services import Denied, current_actor
from . import models as m
from .commands import RestartCommand, InvalidCommand
from .lifecycle import State
from .requests import resolve_retry, RequestRefused, Retry
from .service import Refused, _independent, _binding, _actor, _snapshot, _event, _move, _ACTIVE


@dataclass(frozen=True)
class RestartTarget:
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


@dataclass(frozen=True)
class MachineStep:
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


STAGES = ('apply_identity', 'restart_session', 'confirm_ready')


def digest(command):
    intent = command.intent
    value = {name: str(getattr(intent, name)) for name in (
        'epoch','target_id','incarnation','expected_revision','not_after')}
    value.update(action='restart', fresh=command.fresh)
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


class RestartHandler:
    stages = STAGES

    def __init__(self, operations):
        self.ops = operations

    def _ports(self):
        if self.ops.targets is None:
            raise Refused('controller_unavailable', safe_reason='current target binding is unavailable')
        messages = self.ops.messages
        if self.ops.restart_support is None and (messages is None or messages.support is None):
            raise Refused('controller_unavailable', safe_reason='physical restart binding is unavailable')

    def require(self, target, row=None):
        if self.ops.restart_support is None:
            raise Refused('controller_unavailable', safe_reason='physical restart binding is unavailable')
        self.ops.targets.authorize(target)
        self.ops.restart_support.require(target)

    def _authorize(self, actor, target, row=None):
        self.ops.access.authorize(actor, 'sessions.restart_puppet', project_ids=(target.project_id,))
        # Разрешение action не даёт права уничтожить чужую человеческую работу.
        if actor.human_owner_id != target.owner_id:
            raise Denied('restart belongs to another human assignment')

    def submit(self, actor, command):
        if type(command) is not RestartCommand or command.fresh:
            raise InvalidCommand(field='fresh')
        _independent()
        intent = command.intent
        with self.ops._transaction():
            installation = self.ops._installation(intent.epoch)
            current_actor(actor, grants=self.ops.grants)
            # Assignment lock предшествует Operation; истёкший runtime binding
            # не прячет уже committed request/result от его текущего читателя.
            target = self.ops.targets.locked(intent.target_id) if self.ops.targets is not None else None
            old = m.Operation.objects.filter(actor_id=actor.user_id, request_id=intent.request_id).first()
            try:
                retry = resolve_retry(expected_epoch=intent.epoch, current_epoch=installation.epoch,
                    submitted_digest=digest(command), stored_digest=old.digest if old else None,
                    result_available=not old.result_pruned if old else True)
            except RequestRefused as error:
                raise Refused(error.code) from None
            if retry is Retry.EXPIRED:
                raise Refused('result_expired')
            if retry is Retry.REPLAY:
                return self.ops._disclose(actor, old)
            self._ports()
            if type(target) is not RestartTarget:
                raise Denied('current assignment is unavailable')
            self.ops.access.authorize(actor, 'operations.view_operation', project_ids=(target.project_id,))
            self._authorize(actor, target)
            self.require(target)
            if not installation.effects_enabled:
                raise Refused('effects_disabled')
            now = timezone.now()
            if intent.not_after <= now:
                raise Refused('deadline_expired')
            if intent.not_after > now + timedelta(minutes=5):
                raise Refused('deadline_horizon')
            if intent.expected_revision != target.revision or intent.incarnation != target.incarnation_id:
                raise Refused('revision_conflict')
            active = m.Operation.objects.filter(epoch=installation.epoch, state__in=_ACTIVE)
            if active.count() >= self.ops.queue_limit or active.filter(projects=target.project_id).count() >= self.ops.project_limit:
                raise Refused('admission_limit')
            row = m.Operation.objects.create(actor_id=actor.user_id, actor_binding=_binding(actor), request_id=intent.request_id,
                digest=digest(command), epoch=intent.epoch, restore_generation=installation.generation, action='restart',
                node_id=target.node_id, target_kind='puppet', target_id=target.target_id, incarnation_id=target.incarnation_id,
                expected_revision=target.revision, eligible=None, not_after=intent.not_after)
            row.projects.set((target.project_id,))
            m.RestartBinding.objects.create(operation=row, project_id=target.project_id, executor_epoch=target.executor_epoch,
                owner_id=target.owner_id, generation=target.generation, role=target.role,
                bundle_digest=target.bundle_digest, grant_id=target.grant_id)
            for ordinal, name in enumerate(STAGES, 1):
                m.Step.objects.create(operation=row, ordinal=ordinal, name=name)
            _event(row)
            result = _snapshot(row)
        return result

    def _value(self, row, step):
        pin = row.restartbinding
        return MachineStep(row.epoch, row.id, step.id, row.target_id, row.incarnation_id, row.node_id,
            pin.executor_epoch, pin.owner_id, pin.generation, row.expected_revision, pin.role,
            pin.bundle_digest, pin.grant_id, step.name, row.not_after)

    def pending(self, *, node_id, executor_epoch, project_ids, limit):
        self._ports()
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError('bounded machine projection required')
        # Текущее разрешённое project scope применяется в SQL до limit.
        rows = m.Step.objects.select_related('operation__restartbinding').filter(
            operation__node_id=node_id, operation__target_kind='puppet',
            operation__state__in=('accepted','running'), operation__restartbinding__executor_epoch=executor_epoch,
            operation__restartbinding__project_id__in=project_ids, token=None).exclude(
            operation__step__outcome='unknown').order_by('operation__created_at','operation_id','ordinal')
        result = []
        for step in rows:
            if m.Step.objects.filter(operation_id=step.operation_id, ordinal__lt=step.ordinal).exclude(outcome='succeeded').exists():
                continue
            result.append(self._value(step.operation, step))
            if len(result) == limit:
                break
        return tuple(result)

    def current(self, operation_id, step_id):
        if not transaction.get_connection().in_atomic_block:
            raise Refused('transaction_boundary')
        self._ports()
        installation = self.ops._installation()
        row = m.Operation.objects.select_for_update().get(pk=operation_id, action__in=('restart', 'send', 'dispatch'), target_kind='puppet')
        step = m.Step.objects.select_for_update().get(pk=step_id, operation=row)
        value = self._value(row, step)
        expected = RestartTarget(value.target_id, row.restartbinding.project_id, value.node_id, value.incarnation_id,
            value.executor_epoch, value.owner_id, value.role, value.bundle_digest, value.grant_id, value.generation, value.revision)
        handler = self.ops.machine_handler(row.action)
        handler._authorize(_actor(row), expected, row)
        target = self.ops.targets.current(row.target_id)
        if (target is None or value.epoch != installation.epoch or row.restore_generation != installation.generation
                or not installation.effects_enabled or row.state not in ('accepted','running')
                or row.not_after <= timezone.now()):
            raise Refused('operation_conflict')
        if target != expected:
            raise Refused('revision_conflict')
        handler.require(target, row)
        if m.Step.objects.filter(operation=row, ordinal__lt=step.ordinal).exclude(outcome='succeeded').exists():
            raise Refused('operation_conflict')
        return value

    def claimed(self, work, attempt_id):
        if self.current(work.operation_id, work.step_id) != work:
            raise Refused('claim_conflict')
        row = m.Operation.objects.get(pk=work.operation_id)
        step = m.Step.objects.get(pk=work.step_id)
        if step.attempt_id is not None:
            if step.attempt_id != attempt_id:
                raise Refused('claim_conflict')
            return
        keys = sorted(('node:'+str(row.node_id), 'project:'+str(row.restartbinding.project_id), 'puppet:'+str(row.target_id)))
        for key in keys:
            m.Resource.objects.get_or_create(key=key)
        tuple(m.Resource.objects.select_for_update().filter(pk__in=keys).order_by('pk'))
        claims = m.ResourceClaim.objects.filter(resource_id__in=keys, released_at=None)
        if claims.exclude(operation=row).exists():
            raise Refused('claim_conflict')
        if not claims.exists():
            if m.Operation.objects.filter(state__in=('running','reconciling','unknown'),
                    resourceclaim__released_at=None).distinct().count() >= self.ops.execution_limit:
                raise Refused('admission_limit')
            for key in keys:
                m.ResourceClaim.objects.create(resource_id=key, operation=row, step=step, token=attempt_id)
        step.token, step.attempt_id = attempt_id, attempt_id
        step.started_at = step.dispatched_at = timezone.now()
        step.lease_until = min(work.not_after, timezone.now()+timedelta(seconds=30))
        step.save(update_fields=('token','attempt_id','started_at','dispatched_at','lease_until'))
        if row.state == State.ACCEPTED:
            _move(row, State.RUNNING, 'effect_started', expected_revision=row.revision)

    def retired(self, target_id, executor_epoch):
        if not transaction.get_connection().in_atomic_block:
            raise Refused('transaction_boundary')
        installation = self.ops._installation()
        rows = m.Operation.objects.select_for_update().filter(target_kind='puppet', target_id=target_id,
            restartbinding__executor_epoch=executor_epoch, epoch=installation.epoch,
            restore_generation=installation.generation, state__in=('accepted','running','reconciling')).order_by('pk')
        for row in rows:
            m.MessageBinding.objects.filter(operation=row).update(ciphertext=b'')
            if row.state == State.ACCEPTED:
                _move(row, State.REFUSED, 'executor_retired', expected_revision=row.revision)
            else:
                if row.state == State.RUNNING:
                    _move(row, State.RECONCILING, 'executor_retired', expected_revision=row.revision)
                _move(row, State.UNKNOWN, 'outcome_unknown', expected_revision=row.revision)

    def completed(self, work, attempt_id, outcome, evidence):
        if not transaction.get_connection().in_atomic_block:
            raise Refused('transaction_boundary')
        # Historical result ne запускает effects и не требует прежних owner rights.
        installation = self.ops._installation(work.epoch)
        row = m.Operation.objects.select_for_update().get(pk=work.operation_id, target_kind='puppet')
        step = m.Step.objects.select_for_update().get(pk=work.step_id, operation=row)
        if row.restore_generation != installation.generation or self._value(row, step) != work or step.attempt_id != attempt_id:
            raise Refused('claim_conflict')
        if step.outcome == outcome and step.evidence == evidence:
            return
        if step.quiescent_at is not None:
            raise Refused('claim_conflict')
        if outcome == 'unknown':
            m.MessageBinding.objects.filter(operation=row).update(ciphertext=b'')
            step.outcome, step.evidence = outcome, evidence
            step.save(update_fields=('outcome','evidence'))
            if row.state != State.UNKNOWN:
                _move(row, State.RECONCILING, 'effect_uncertain', expected_revision=row.revision)
                _move(row, State.UNKNOWN, 'outcome_unknown', expected_revision=row.revision)
            return
        if row.state == State.UNKNOWN:
            _move(row, State.RECONCILING, 'late_confirmation', expected_revision=row.revision)
        m.EffectReceipt.objects.create(step=step, token=attempt_id, epoch=row.epoch, node_id=row.node_id,
            source_revision=row.expected_revision, decision_revision=row.revision, applied=outcome=='succeeded')
        step.outcome, step.evidence, step.quiescent_at = outcome, evidence, timezone.now()
        step.save(update_fields=('outcome','evidence','quiescent_at'))
        terminal = outcome == 'failed' or step.ordinal == len(self.ops.machine_handler(row.action).stages)
        if terminal:
            m.MessageBinding.objects.filter(operation=row).update(ciphertext=b'')
            _move(row, State.SUCCEEDED if outcome=='succeeded' else State.FAILED,
                'effect_confirmed' if outcome=='succeeded' else 'effect_failed', expected_revision=row.revision)
            m.ResourceClaim.objects.filter(operation=row, released_at=None).update(released_at=step.quiescent_at)
        elif row.state == State.RECONCILING:
            # Сверка частичного workflow не выдаёт оставшиеся effects автоматически.
            _move(row, State.UNKNOWN, 'outcome_unknown', expected_revision=row.revision)
        else:
            before = row.revision
            row.revision += 1
            row.code, row.reason = 'step_confirmed', 'named stage receipt committed'
            if not m.Operation.objects.filter(pk=row.id, revision=before, state=State.RUNNING).update(
                    revision=row.revision, code=row.code, reason=row.reason):
                raise Refused('revision_conflict')
            _event(row)
