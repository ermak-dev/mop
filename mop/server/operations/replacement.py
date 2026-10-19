"""Update admission и synchronous prepare общего журнала (#625).

Физические стадии здесь не исполняются. Нет discovery prepare для worker,
нет сохранённого human credential и нет повторной мутации после unknown.
SQL-only порты source/continuation обязана явно связать composition.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import json
import re
from typing import Protocol
from uuid import UUID, uuid4, uuid5

from django.utils import timezone

from mop.common.credentials import Credential, GrantReceipt
from mop.server.access.context import ActorKind
from mop.server.access.services import Denied, current_actor
from . import models as m
from .commands import UpdateCommand
from .launch import REGISTER, image_pin
from .lifecycle import State
from .puppet import RestartTarget
from .requests import resolve_retry, RequestRefused, Retry
from .service import Refused, _independent, _binding, _actor, _snapshot, _event, _move

STAGES = ('prepare_launch', 'scheduler_register', 'apply_identity', 'confirm_ready', 'activate_grant')
REPLACEMENT_CAPABILITIES = frozenset({'replace_launch', 'preserve_clone', 'exact_continuation', 'fresh_dialogue'})
_IDENTITY = UUID('92eff7c9-42e8-47ad-bbdf-46274c0e8c9c')


@dataclass(frozen=True)
class SourcePin:
    """Доказанный working source и private opaque refs, не guessed newest session."""
    source: RestartTarget
    origin: str
    source_ref: str
    source_commit: str
    clone_ref: UUID
    continuation_ref: UUID | None


class ReplacementSupport(Protocol):
    """Публичный SQL-only порт execution для Root. Отсутствие -- отказ до записей.

    capture под source Assignment lock проверяет точный source и continuation;
    refs закреплены в execution, а не произвольные пути/имена файлов. fresh
    требует доказанного clean-dialogue режима и возвращает continuation=None.
    Никаких сетевых/физических эффектов и чтения transcript на admission.
    """
    capabilities: frozenset[str]

    def capture(self, source: RestartTarget, *, fresh: bool, origin: str) -> SourcePin:
        ...


@dataclass(frozen=True)
class LaunchPins:
    """SQL DTO источника от controller, не обратная зависимость на execution ORM."""
    expires_at: datetime
    grant_expires_at: datetime
    definition_id: UUID
    policy_approval_id: UUID
    policy_version_id: UUID
    policy_digest: str
    bootstrap_revision: UUID
    bootstrap_digest: str
    image_id: UUID
    image_digest: str


def digest(command):
    intent = command.intent
    value = {name: str(getattr(intent, name)) for name in (
        'epoch', 'target_id', 'incarnation', 'expected_revision', 'not_after')}
    value.update(action='update', fresh=command.fresh, origin=command.origin)
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def identities(actor, request_id):
    base = f'{actor.user_id}:{request_id}'
    return (uuid5(_IDENTITY, base+':operation'), uuid5(_IDENTITY, base+':incarnation'),
            uuid5(_IDENTITY, base+':grant'))


def resource_keys(source):
    return sorted(('node:'+str(source.node_id), 'project:'+str(source.project_id),
                   'puppet:'+str(source.target_id), 'target:'+str(source.target_id)))


class UpdatePuppets:
    permissions = ('operations.view_operation', 'execution.update_puppet',
                   'execution.create_puppet', 'access.issue_grant')

    def __init__(self, operations, *, targets, issuer=None, scheduler=None, support=None):
        self.ops, self.targets = operations, targets
        self.issuer, self.scheduler, self.support = issuer, scheduler, support

    def _ports(self):
        if (self.issuer is None or not callable(getattr(self.issuer, 'prepare_replacement', None))
                or self.scheduler is None or type(self.scheduler.scheduler_id) is not UUID
                or not REGISTER <= self.scheduler.capabilities or self.support is None
                or not REPLACEMENT_CAPABILITIES <= self.support.capabilities
                or not callable(getattr(self.support, 'capture', None))):
            raise Refused('controller_unavailable', safe_reason='replacement and exact continuation bindings are unavailable')

    def _authorize(self, actor, source):
        if actor.kind is not ActorKind.HUMAN or actor.grant_id is not None or actor.user_id != source.owner_id:
            raise Denied('update requires the current human assignment owner')
        for permission in self.permissions:
            self.ops.access.authorize(actor, permission, project_ids=(source.project_id,))
        self.targets.authorize(source)

    def _source_pin(self, source, command, origin):
        pin = self.support.capture(source, fresh=command.fresh, origin=origin)
        if (type(pin) is not SourcePin or pin.source != source or pin.origin != origin
                or type(pin.source_ref) is not str or not 1 <= len(pin.source_ref) <= 256
                or any(ord(c) < 32 or ord(c) == 127 for c in pin.source_ref)
                or type(pin.source_commit) is not str or re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', pin.source_commit) is None
                or type(pin.clone_ref) is not UUID
                or (pin.continuation_ref is not None if command.fresh else type(pin.continuation_ref) is not UUID)):
            raise Refused('controller_unavailable', safe_reason='exact working source and continuation proof is unavailable')
        return pin

    def submit(self, actor, command, *, credential):
        if type(command) is not UpdateCommand:
            raise ValueError('typed update command required')
        if type(credential) is not Credential:
            raise Denied('current human credential required')
        _independent()
        intent = command.intent
        with self.ops._transaction():
            installation = self.ops._installation(intent.epoch)
            current_actor(actor, grants=self.ops.grants)
            source = self.targets.locked(intent.target_id)
            old = m.Operation.objects.select_for_update().filter(actor_id=actor.user_id, request_id=intent.request_id).first()
            try:
                retry = resolve_retry(expected_epoch=intent.epoch, current_epoch=installation.epoch,
                    submitted_digest=digest(command), stored_digest=old.digest if old else None,
                    result_available=not old.result_pruned if old else True)
            except RequestRefused as error:
                raise Refused(error.code) from None
            if retry is Retry.EXPIRED:
                raise Refused('result_expired')
            if retry is Retry.REPLAY:
                # Только наблюдение committed запроса, даже если порты исчезли.
                result = self.ops._disclose(actor, old)
                step = m.Step.objects.select_for_update().get(operation=old, name='prepare_launch')
                if old.state == 'running' and step.quiescent_at is None and step.lease_until <= timezone.now():
                    self._unknown(old, step)
                    result = self.ops._disclose(actor, old)
                return result
            self._ports()
            if type(source) is not RestartTarget:
                raise Denied('current assignment is unavailable')
            self._authorize(actor, source)
            if not installation.effects_enabled:
                raise Refused('effects_disabled')
            now = timezone.now()
            if intent.not_after <= now:
                raise Refused('deadline_expired')
            if intent.not_after > now+timedelta(minutes=5):
                raise Refused('deadline_horizon')
            if (intent.incarnation, intent.expected_revision) != (source.incarnation_id, source.revision):
                raise Refused('revision_conflict')
            from mop.server.catalog.models import NodeImage, Project
            project = Project.objects.get(pk=source.project_id, retired_at=None)
            origin = command.origin or project.origin
            # Одинаковый basename другого host не означает тот же Catalog project.
            if origin != project.origin:
                raise Refused('revision_conflict', safe_reason='update origin must belong to the same Catalog project')
            pin = self._source_pin(source, command, origin)
            launch = self.targets.launch_pins(source)
            enrolled = m.ScheduledPuppet.objects.filter(target_id=source.target_id).first()
            if (enrolled is None or (enrolled.incarnation_id, enrolled.epoch, enrolled.restore_generation,
                    enrolled.scheduler_id) != (source.incarnation_id, installation.epoch, installation.generation,
                                               self.scheduler.scheduler_id)):
                raise Refused('revision_conflict', safe_reason='exact source scheduler enrollment is unavailable')
            if not m.ScheduledNode.objects.filter(node_id=source.node_id, scheduler_id=enrolled.scheduler_id).exists():
                raise Refused('controller_unavailable', safe_reason='node scheduler enrollment is unavailable')
            image = NodeImage.objects.filter(node_id=source.node_id, project_id=source.project_id, available=True).first()
            if image is None or image.observed_image_id is None or image.desired_image_id != image.observed_image_id:
                raise Refused('revision_conflict', safe_reason='the pinned replacement image is unavailable')
            self.ops._admission(installation, (source.project_id,), actor=actor, request_id=intent.request_id)
            keys = resource_keys(source)
            for key in keys:
                m.Resource.objects.get_or_create(key=key)
            tuple(m.Resource.objects.select_for_update().filter(pk__in=keys).order_by('pk'))
            if m.ResourceClaim.objects.filter(resource_id__in=keys, released_at=None).exists():
                raise Refused('claim_conflict', safe_reason='replacement source has unresolved work')
            if m.Operation.objects.filter(state__in=('running', 'reconciling', 'unknown'),
                    resourceclaim__released_at=None).distinct().count() >= self.ops.execution_limit:
                raise Refused('admission_limit')
            operation_id, incarnation_id, grant_id = identities(actor, intent.request_id)
            row = m.Operation.objects.create(id=operation_id, actor_id=actor.user_id, actor_binding=_binding(actor),
                request_id=intent.request_id, digest=digest(command), epoch=installation.epoch,
                restore_generation=installation.generation, action='update', node_id=source.node_id,
                target_kind='puppet', target_id=source.target_id, incarnation_id=incarnation_id,
                expected_revision=source.revision+1, not_after=intent.not_after)
            row.projects.set((source.project_id,))
            m.RestartBinding.objects.create(operation=row, project_id=source.project_id, executor_epoch=source.executor_epoch,
                owner_id=source.owner_id, generation=source.generation+1, role=source.role,
                bundle_digest=source.bundle_digest, grant_id=grant_id)
            m.ReplacementBinding.objects.create(operation=row, source_incarnation_id=source.incarnation_id,
                source_grant_id=source.grant_id, source_revision=source.revision, source_generation=source.generation,
                source_expires_at=launch.expires_at, expires_at=min(launch.expires_at, launch.grant_expires_at),
                definition_id=launch.definition_id, policy_approval_id=launch.policy_approval_id,
                policy_version_id=launch.policy_version_id, policy_digest=launch.policy_digest)
            m.ReplacementPlan.objects.create(operation=row, source_executor_epoch=source.executor_epoch,
                owner_id=source.owner_id, project_id=source.project_id, node_id=source.node_id, role=source.role,
                bundle_digest=source.bundle_digest, destination_incarnation_id=incarnation_id, destination_grant_id=grant_id,
                destination_revision=source.revision+1, destination_generation=source.generation+1,
                scheduler_id=enrolled.scheduler_id, job_id=enrolled.job_id, namespace=enrolled.namespace,
                modify_index=enrolled.modify_index, origin=pin.origin, source_ref=pin.source_ref,
                source_commit=pin.source_commit, clone_ref=pin.clone_ref, continuation_ref=pin.continuation_ref,
                fresh=command.fresh, source_bootstrap_revision=launch.bootstrap_revision,
                source_bootstrap_digest=launch.bootstrap_digest, source_image_id=launch.image_id,
                source_image_digest=launch.image_digest, bootstrap_revision=launch.policy_version_id,
                bootstrap_digest=launch.policy_digest, image_id=image.observed_image_id,
                image_digest=image_pin(image.observed_image_id))
            _event(row)
            steps = [m.Step.objects.create(operation=row, ordinal=ordinal, name=name)
                     for ordinal, name in enumerate(STAGES, 1)]
            step = steps[0]
            step.token = step.attempt_id = uuid4()
            step.started_at = step.dispatched_at = now
            step.lease_until = min(row.not_after, now+timedelta(seconds=30))
            step.save(update_fields=('token', 'attempt_id', 'started_at', 'dispatched_at', 'lease_until'))
            for key in keys:
                m.ResourceClaim.objects.create(resource_id=key, operation=row, step=step, token=step.token)
            _move(row, State.RUNNING, 'preparation_claimed', expected_revision=row.revision)
            token = step.token
        # Единственное место human credential; операция и claim уже committed.
        try:
            receipt = self.issuer.prepare_replacement(credential, row.id)
        except PermissionError as error:
            return self._record(row.id, token, 'failed', reason=str(error))
        except Exception:
            return self._record(row.id, token, 'unknown')
        return self._record(row.id, token, 'succeeded', receipt=receipt)

    def _locked(self, operation_id):
        installation = self.ops._installation()
        target_id = m.Operation.objects.values_list('target_id', flat=True).get(pk=operation_id, action='update')
        source = self.targets.locked(target_id)
        row = m.Operation.objects.select_for_update().get(pk=operation_id, action='update')
        step = m.Step.objects.select_for_update().get(operation=row, name='prepare_launch')
        keys = resource_keys(self._source(row))
        tuple(m.Resource.objects.select_for_update().filter(pk__in=keys).order_by('pk'))
        return installation, source, row, step

    def _source(self, row):
        plan, binding = row.replacementplan, row.replacementbinding
        return RestartTarget(row.target_id, plan.project_id, plan.node_id, binding.source_incarnation_id,
            plan.source_executor_epoch, plan.owner_id, plan.role, plan.bundle_digest, binding.source_grant_id,
            binding.source_generation, binding.source_revision)

    def _unknown(self, row, step):
        step.outcome, step.evidence = 'unknown', 'preparation-uncertain'
        step.save(update_fields=('outcome', 'evidence'))
        if row.state == 'running':
            _move(row, State.RECONCILING, 'effect_uncertain', expected_revision=row.revision)
            _move(row, State.UNKNOWN, 'outcome_unknown', expected_revision=row.revision)

    def _record(self, operation_id, token, outcome, *, receipt=None, reason=None, recovery_actor=None):
        _independent()
        with self.ops._transaction():
            installation, source, row, step = self._locked(operation_id)
            if recovery_actor is not None:
                current_actor(recovery_actor, grants=self.ops.grants)
                self.ops.access.authorize(recovery_actor, 'operations.recover_operation', project_ids=(row.replacementplan.project_id,))
            if step.token != token or row.state not in ('running', 'unknown', 'reconciling'):
                raise Refused('claim_conflict')
            if step.quiescent_at is not None:
                return _snapshot(row)
            if (row.epoch != installation.epoch or row.restore_generation != installation.generation):
                raise Refused('epoch_conflict')
            if recovery_actor is not None and row.state == 'running':
                # Recovery -- наблюдение потерянного completion, не продолжение
                # прежнего HTTP-вызова. Поздняя квитанция не запускает workflow.
                self._unknown(row, step)
            if outcome == 'succeeded':
                keys = resource_keys(self._source(row))
                claims = tuple(m.ResourceClaim.objects.filter(resource_id__in=keys, released_at=None)
                    .values_list('resource_id', 'operation_id', 'step_id', 'token'))
                if (set(claim[0] for claim in claims) != set(keys)
                        or any(claim[1:] != (row.id, step.id, token) for claim in claims)):
                    outcome = 'unknown'
            if outcome == 'succeeded':
                if source != self._source(row):
                    outcome = 'unknown'
                else:
                    try:
                        self._authorize(_actor(row), source)
                    except (Denied, Refused):
                        outcome = 'unknown'
            if outcome == 'succeeded':
                # Даже формально корректный transport result недостаточен: SQL
                # metadata должна подтвердить exact immutable plan. Это чтение,
                # не повторная выдача и не новое разрешение продолжить workflow.
                committed = self.targets.prepared(row)
                if (source != self._source(row) or committed is None
                        or (receipt is not None and (type(receipt) is not GrantReceipt or receipt != committed))):
                    outcome = 'unknown'
            if outcome == 'unknown':
                self._unknown(row, step)
                return _snapshot(row)
            step.outcome = outcome
            step.evidence = 'replacement-prepared' if outcome == 'succeeded' else 'preparation-refused'
            step.quiescent_at = timezone.now()
            step.save(update_fields=('outcome', 'evidence', 'quiescent_at'))
            m.EffectReceipt.objects.create(step=step, token=token, epoch=row.epoch, node_id=row.node_id,
                source_revision=row.replacementbinding.source_revision, decision_revision=row.revision,
                applied=outcome == 'succeeded')
            if outcome == 'failed':
                _move(row, State.FAILED, 'preparation_refused', expected_revision=row.revision, safe_reason=reason)
                m.ResourceClaim.objects.filter(operation=row, released_at=None).update(released_at=step.quiescent_at)
            else:
                # Target fence не отпускается между prepare и scheduler. Другие
                # ключи также остаются здесь: handoff -- задача физической полосы.
                before = row.revision
                row.revision += 1
                row.code, row.reason = 'replacement_prepared', 'exact replacement grant prepared; physical update not yet applied'
                m.Operation.objects.filter(pk=row.pk, revision=before).update(revision=row.revision, code=row.code, reason=row.reason)
                _event(row)
            return _snapshot(row)

    def reconcile(self, actor, operation_id):
        """Только чтение exact grant metadata; ни credential, ни повторного prepare."""
        _independent()
        with self.ops._transaction():
            _, _, row, step = self._locked(operation_id)
            current_actor(actor, grants=self.ops.grants)
            self.ops.access.authorize(actor, 'operations.recover_operation', project_ids=(row.replacementplan.project_id,))
            if row.state not in ('running', 'unknown', 'reconciling') or step.token is None:
                raise Refused('operation_conflict')
            token = step.token
        return self._record(operation_id, token, 'succeeded', recovery_actor=actor)
