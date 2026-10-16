"""Durable admission/lifecycle; SQL никогда не охватывает внешний эффект."""
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from ..access.context import ActorContext, PrincipalKind
from ..access.services import Access, Denied, NotVisible, current_actor
from ..catalog.models import Node, NodeProject, Project
from . import models as m
from .lifecycle import State, transition
from .node import NodeEligibility
from .requests import resolve_retry, RequestRefused, Retry


_MESSAGES = {
    'request_conflict': 'request key is already bound to different intent',
    'epoch_conflict': 'installation epoch changed; refresh before submitting',
    'result_expired': 'operation result expired; request key cannot be reused',
    'revision_conflict': 'target changed; refresh before submitting',
    'deadline_expired': 'command deadline expired',
    'deadline_horizon': 'command deadline exceeds five minutes',
    'effects_disabled': 'controller effects are disabled',
    'admission_limit': 'durable admission capacity exhausted',
    'claim_conflict': 'effect receipt does not match the durable claim',
    'operation_conflict': 'operation is not in the expected state',
    'transaction_boundary': 'operation requires an independent commit boundary',
    'controller_unavailable': 'controller trust state is unavailable',
}
_ACTIVE = ('accepted', 'running', 'reconciling', 'unknown')


class Refused(ValueError):
    def __init__(self, code, *, safe_reason=None):
        self.code = code
        self.status = 410 if code == 'result_expired' else 429 if code == 'admission_limit' else 503 if code in ('effects_disabled', 'controller_unavailable') else 409
        super().__init__(code + ': ' + (safe_reason or _MESSAGES[code]))


@dataclass(frozen=True)
class Claim:
    operation_id: UUID
    step_id: UUID
    token: UUID
    epoch: UUID
    node_id: UUID
    source_revision: int
    eligible: bool
    revision: int
    not_after: datetime


def _binding(actor):
    return dict(user_id=actor.user_id, principal_id=str(actor.principal_id), kind=actor.kind.value,
        credential_id=str(actor.credential_id), human_owner_id=actor.human_owner_id,
        node_id=str(actor.node_id) if actor.node_id else None,
        grant_id=str(actor.grant_id) if actor.grant_id else None)


def _actor(row):
    value = row.actor_binding
    return ActorContext(value['user_id'], UUID(value['principal_id']), PrincipalKind(value['kind']),
        UUID(value['credential_id']), value['human_owner_id'],
        UUID(value['node_id']) if value['node_id'] else None, UUID(value['grant_id']) if value['grant_id'] else None)


def _independent():
    connection = transaction.get_connection()
    if connection.in_atomic_block or not connection.get_autocommit():
        raise Refused('transaction_boundary')


@dataclass(frozen=True)
class InstallationState:
    """SQL-only root reader возвращает locked plain state, не ORM identity."""
    epoch: UUID
    effects_enabled: bool
    generation: UUID


def _snapshot(row):
    receipt = m.EffectReceipt.objects.filter(step__operation=row).order_by('-step__ordinal').values(
        'id', 'applied', 'source_revision', 'step__name').first()
    started = m.Step.objects.filter(operation=row, token__isnull=False).exists()
    result = dict(id=str(row.pk), request_id=str(row.request_id), epoch=str(row.epoch), state=row.state,
        revision=row.revision, code=row.code, reason=row.reason, target={'id': str(row.node_id), 'kind': 'node',
            'revision': str(row.expected_revision + int(started)), 'source_revision': str(row.expected_revision)},
        resources_held=m.ResourceClaim.objects.filter(operation=row, released_at=None).exists(),
        evidence=({'receipt_id': str(receipt['id']), 'applied': receipt['applied'],
            'source_revision': str(receipt['source_revision']),
            'applied_revision': str(receipt['source_revision'] + 1) if receipt['applied'] else None,
            'projection_updated': row.code == 'effect_confirmed'} if receipt else None))
    if row.target_kind == 'puppet':
        result.update(target={'id': str(row.target_id), 'kind': row.target_kind, 'incarnation': str(row.incarnation_id),
            'revision': str(row.expected_revision), 'source_revision': str(row.expected_revision)},
            steps=list(m.Step.objects.filter(operation=row).order_by('ordinal').values('name','outcome','evidence')),
            evidence=({'receipt_id':str(receipt['id']), 'action':receipt['step__name'],
                'incarnation':str(row.incarnation_id), 'confirmed':receipt['applied']} if receipt else None))
        if row.action == 'stop_job':
            applied_revision = row.stopbinding.applied_revision
            result['target']['revision'] = str(applied_revision or row.expected_revision)
            if result['evidence'] is not None:
                result['evidence']['projection_updated'] = applied_revision is not None
    return result


def _event(row):
    values = dict(operation=row, revision=row.revision, state=row.state, code=row.code,
                  target_revision=(row.projects.get().revision if row.action == 'build' else
                      (row.stopbinding.applied_revision or row.expected_revision) if row.action == 'stop_job' else
                      row.expected_revision if row.target_kind == 'puppet' else Node.objects.get(pk=row.node_id).revision))
    m.AuditEvent.objects.create(reason=row.reason, **values)
    m.OutboxEvent.objects.create(**values)


def _move(row, state, code, *, expected_revision, safe_reason=None):
    try:
        change = transition(row.state, state, revision=row.revision, expected_revision=expected_revision)
    except ValueError:
        raise Refused('revision_conflict' if row.revision != expected_revision else 'operation_conflict') from None
    before = row.revision
    row.state, row.revision, row.code = change.state.value, change.revision, code
    row.reason = safe_reason or _MESSAGES.get(code, code.replace('_', ' '))
    if state not in (State.ACCEPTED, State.RUNNING, State.RECONCILING):
        row.completed_at = timezone.now()
    if not m.Operation.objects.filter(pk=row.pk, revision=before).update(state=row.state,
            revision=row.revision, code=row.code, reason=row.reason, completed_at=row.completed_at):
        raise Refused('revision_conflict')
    _event(row)


class Operations:
    """Одна durable очередь; root внедряет только доверенную authority composition."""
    def __init__(self, *, installation_state: Callable[[], InstallationState] | None = None,
                 transaction_scope: Callable[[], AbstractContextManager] | None = None,
                 grants=None, executor=None, targets=None, restart_support=None,
                 queue_limit=256, project_limit=64, execution_limit=8):
        if not (1 <= queue_limit <= 256 and 1 <= project_limit <= 64 and 1 <= execution_limit <= 8):
            raise ValueError('operation limits exceed controller safety caps')
        self._read_installation = installation_state
        self._transaction_scope = transaction_scope
        self.access = Access(grants=grants)
        self.grants = grants
        self.executor = executor
        self.targets, self.restart_support = targets, restart_support
        self.builds = None
        self.stops = None
        self.queue_limit, self.project_limit, self.execution_limit = queue_limit, project_limit, execution_limit

    def _require_binding(self):
        if self._read_installation is None or self._transaction_scope is None:
            raise Refused('controller_unavailable')

    def _transaction(self):
        self._require_binding()
        return self._transaction_scope()

    def _installation(self, epoch=None):
        try:
            state = self._read_installation()
        except RuntimeError as error:
            raise Refused('controller_unavailable', safe_reason=str(error)) from error
        if type(state) is not InstallationState:
            raise Refused('controller_unavailable')
        if epoch is not None and state.epoch != epoch:
            raise Refused('epoch_conflict')
        return state

    def _authorize(self, actor, action, node_id, projects=()):
        self.access.authorize(actor, action, node_ids=(node_id,), project_ids=tuple(projects))

    def _scope(self, row):
        return tuple(row.projects.values_list('pk', flat=True))

    def _disclose(self, actor, row):
        if row.action == 'build':
            if self.builds is None:
                raise Refused('controller_unavailable')
            return self.builds.disclose(actor, row)
        if row.target_kind == 'puppet':
            self.access.authorize(actor, 'operations.view_operation', project_ids=self._scope(row))
        else:
            self._authorize(actor, 'operations.view_operation', row.node_id, self._scope(row))
        # Запрос по request_id принадлежит actor; ID disclosure допускает отдельное
        # view_operation право в актуальном scope, не владеет правом execution.
        if row.result_pruned:
            raise Refused('result_expired')
        return _snapshot(row)

    def submit(self, actor, command):
        from .commands import RestartCommand, StopJobCommand
        from .puppet import RestartHandler
        if type(command) is StopJobCommand:
            if self.stops is None:
                raise Refused('controller_unavailable', safe_reason='conditional stop-job scheduler is unavailable')
            return self.stops.submit(actor, command)
        # Конечный registry, не выбор метода/import из payload.
        handlers = {NodeEligibility: self.submit_node, RestartCommand: RestartHandler(self).submit}
        handler = handlers.get(type(command))
        if handler is None:
            raise ValueError('finite typed operation command required')
        return handler(actor, command)

    def submit_node(self, actor, command):
        if type(command) is not NodeEligibility:
            raise ValueError('finite typed operation command required')
        _independent()
        with self._transaction():
            installation = self._installation(command.epoch)
            current_actor(actor, grants=self.grants)
            self._authorize(actor, 'operations.view_operation', command.node_id)
            row = m.Operation.objects.filter(actor_id=actor.user_id, request_id=command.request_id).first()
            try:
                retry = resolve_retry(expected_epoch=command.epoch, current_epoch=installation.epoch,
                    submitted_digest=command.digest(), stored_digest=row.digest if row else None,
                    result_available=not row.result_pruned if row else True)
            except RequestRefused as error:
                raise Refused(error.code) from None
            if retry is Retry.EXPIRED:
                raise Refused('result_expired')
            if retry is Retry.REPLAY:
                return self._disclose(actor, row)
            projects = tuple(NodeProject.objects.filter(node_id=command.node_id).order_by('project_id').values_list('project_id', flat=True))
            tuple(Project.objects.select_for_update().filter(pk__in=projects).order_by('pk'))
            node = Node.objects.select_for_update().get(pk=command.node_id)
            self._authorize(actor, 'catalog.' + command.action + '_node', node.pk, projects)
            now = timezone.now()
            if not installation.effects_enabled:
                raise Refused('effects_disabled')
            if command.not_after <= now:
                raise Refused('deadline_expired')
            if command.not_after > now + timedelta(minutes=5):
                raise Refused('deadline_horizon')
            if node.revision != command.expected_revision:
                raise Refused('revision_conflict')
            active = m.Operation.objects.filter(state__in=_ACTIVE, epoch=installation.epoch)
            if active.count() >= self.queue_limit or any(active.filter(projects=project).count() >= self.project_limit for project in projects):
                raise Refused('admission_limit')
            row = m.Operation.objects.create(actor_id=actor.user_id, actor_binding=_binding(actor),
                request_id=command.request_id, digest=command.digest(), epoch=command.epoch,
                restore_generation=installation.generation, action=command.action, node=node,
                expected_revision=command.expected_revision, eligible=command.eligible, not_after=command.not_after)
            row.projects.set(projects)
            m.Step.objects.create(operation=row)
            _event(row)
            result = _snapshot(row)
        return result

    def status(self, actor, operation_id, *, epoch=None):
        with self._transaction():
            self._installation(epoch)
            current_actor(actor, grants=self.grants)
            return self._disclose(actor, m.Operation.objects.get(pk=operation_id))

    def by_request(self, actor, request_id, *, epoch):
        with self._transaction():
            self._installation(epoch)
            current_actor(actor, grants=self.grants)
            row = m.Operation.objects.filter(actor_id=actor.user_id, request_id=request_id).first()
            if row is None:
                raise NotVisible('resource not found')
            return self._disclose(actor, row)

    def _resources(self, row):
        return sorted(['node:' + str(row.node_id), *('project:' + str(pk) for pk in self._scope(row))])

    def _claim_value(self, row, step):
        return Claim(row.pk, step.pk, step.token, row.epoch, row.node_id,
                     row.expected_revision, row.eligible, row.revision, row.not_after)

    def claim(self, operation_id):
        _independent()
        with self._transaction():
            installation = self._installation()
            row = m.Operation.objects.select_for_update().get(pk=operation_id)
            if row.action == 'build':
                # Конечный build-handler вызывается Worker отдельно, не node adapter.
                return None
            if row.target_kind != 'node' or row.state != State.ACCEPTED:
                return None
            if row.epoch != installation.epoch or row.restore_generation != installation.generation or not installation.effects_enabled:
                return None
            projects = self._scope(row)
            tuple(Project.objects.select_for_update().filter(pk__in=projects).order_by('pk'))
            try:
                self._authorize(_actor(row), 'catalog.' + row.action + '_node', row.node_id, projects)
            except Denied as error:
                _move(row, State.REFUSED, 'access_denied', expected_revision=row.revision, safe_reason=str(error))
                return None
            if row.not_after <= timezone.now():
                _move(row, State.EXPIRED, 'deadline_expired', expected_revision=row.revision)
                return None
            node = Node.objects.select_for_update().get(pk=row.node_id)
            current_projects = set(NodeProject.objects.filter(node=node).values_list('project_id', flat=True))
            if node.revision != row.expected_revision or set(projects) != current_projects:
                _move(row, State.REFUSED, 'revision_conflict', expected_revision=row.revision)
                return None
            # FIFO для пересекающихся ресурсов, без обхода раньше принятой команды.
            earlier = m.Operation.objects.filter(state=State.ACCEPTED, epoch=row.epoch, created_at__lt=row.created_at)
            if earlier.filter(node_id=row.node_id).exists() or earlier.filter(projects__in=projects).exists():
                return None
            keys = self._resources(row)
            for key in keys:
                m.Resource.objects.get_or_create(key=key)
            tuple(m.Resource.objects.select_for_update().filter(pk__in=keys).order_by('pk'))
            if m.ResourceClaim.objects.filter(resource_id__in=keys, released_at=None).exists():
                return None
            running = m.Operation.objects.filter(state__in=('running', 'reconciling', 'unknown'), epoch=row.epoch,
                                                  resourceclaim__released_at=None).distinct()
            if running.count() >= self.execution_limit or running.filter(node_id=row.node_id).count() >= 4:
                return None
            from ..catalog.services import Catalog
            Catalog(authorizer=self.access).request_node_eligibility(_actor(row), row.node_id, row.eligible,
                expected_revision=row.expected_revision)
            step = m.Step.objects.select_for_update().get(operation=row)
            step.token, step.started_at = uuid4(), timezone.now()
            # Lease — лишь сигнал stale executor; её истечение не освобождает fence.
            step.lease_until = step.started_at + timedelta(seconds=30)
            step.save(update_fields=['token', 'started_at', 'lease_until'])
            for key in keys:
                m.ResourceClaim.objects.create(resource_id=key, operation=row, step=step, token=step.token)
            _move(row, State.RUNNING, 'effect_started', expected_revision=row.revision)
            result = self._claim_value(row, step)
        return result

    def _bound(self, claim):
        if type(claim) is not Claim:
            raise Refused('claim_conflict')
        installation = self._installation(claim.epoch)
        row = m.Operation.objects.select_for_update().get(pk=claim.operation_id)
        step = m.Step.objects.select_for_update().get(operation=row)
        if (row.epoch != claim.epoch or row.restore_generation != installation.generation or step.pk != claim.step_id
                or step.token != claim.token or row.node_id != claim.node_id
                or row.expected_revision != claim.source_revision or row.eligible != claim.eligible
                or row.not_after != claim.not_after):
            raise Refused('claim_conflict')
        return row, step, installation

    def prepare_effect(self, claim):
        """После claim COMMIT: текущий допуск и единственный dispatch checkpoint.

        COMMIT checkpoint не доказывает старт эффекта. Получатель также обязан
        проверить bound not_after перед физическим действием, включая transit wait.
        """
        _independent()
        with self._transaction():
            row, step, installation = self._bound(claim)
            if row.state != State.RUNNING or step.dispatched_at is not None:
                return None
            if row.revision != claim.revision:
                raise Refused('revision_conflict')
            projects, code, reason = self._scope(row), None, None
            try:
                self._authorize(_actor(row), 'catalog.' + row.action + '_node', row.node_id, projects)
            except Denied as error:
                code, reason = 'access_denied', str(error)
            node = Node.objects.select_for_update().get(pk=row.node_id)
            if code is None:
                if not installation.effects_enabled:
                    code = 'effects_disabled'
                elif row.not_after <= timezone.now():
                    code = 'deadline_expired'
                elif (node.revision != row.expected_revision + 1 or
                      set(projects) != set(NodeProject.objects.filter(node=node).values_list('project_id', flat=True))):
                    code = 'revision_conflict'
            if code is not None:
                # Только этот владелец знает, что adapter ещё не вызывался;
                # timeout уже dispatched stage сюда не попадает.
                _move(row, State.REFUSED, code, expected_revision=row.revision, safe_reason=reason)
                step.quiescent_at = timezone.now()
                step.save(update_fields=['quiescent_at'])
                m.ResourceClaim.objects.filter(operation=row, step=step, token=claim.token,
                    released_at=None).update(released_at=step.quiescent_at)
                return None
            step.dispatched_at = timezone.now()
            step.save(update_fields=['dispatched_at'])
            before = row.revision
            row.revision += 1
            row.code, row.reason = 'dispatch_authorized', 'current authority and execution deadline confirmed'
            if not m.Operation.objects.filter(pk=row.pk, revision=before, state=State.RUNNING).update(
                    revision=row.revision, code=row.code, reason=row.reason):
                raise Refused('revision_conflict')
            _event(row)
            return self._claim_value(row, step)

    def uncertain(self, claim, *, expected_revision):
        _independent()
        with self._transaction():
            row, step, installation = self._bound(claim)
            if row.state == State.RECONCILING:
                _move(row, State.UNKNOWN, 'outcome_unknown', expected_revision=expected_revision)
            else:
                _move(row, State.RECONCILING, 'effect_uncertain', expected_revision=expected_revision)
                _move(row, State.UNKNOWN, 'outcome_unknown', expected_revision=row.revision)
            return _snapshot(row)

    def complete(self, claim, *, expected_revision, applied):
        if type(applied) is not bool:
            raise ValueError('typed effect confirmation required')
        _independent()
        with self._transaction():
            row, step, installation = self._bound(claim)
            receipt = m.EffectReceipt.objects.filter(step=step).first()
            if receipt is not None:
                if receipt.token != claim.token or receipt.applied != applied:
                    raise Refused('claim_conflict')
                if type(expected_revision) is not int or receipt.decision_revision != expected_revision:
                    raise Refused('revision_conflict')
                return _snapshot(row)
            if row.revision != expected_revision:
                raise Refused('revision_conflict')
            if row.state == State.UNKNOWN:
                _move(row, State.RECONCILING, 'late_confirmation', expected_revision=row.revision)
            if row.state not in (State.RUNNING, State.RECONCILING):
                raise Refused('operation_conflict')
            # Подтверждённый факт записывается даже после revoke, без новых effects.
            # Catalog остаётся владельцем согласованной записи intent/application.
            from ..catalog.services import Catalog
            if self.executor is None or self.executor.kind is not PrincipalKind.SERVICE:
                raise Denied('enrolled node executor required for effect confirmation')
            catalog = Catalog(authorizer=self.access)
            projection_updated = False
            if applied:
                projection_updated = catalog.record_node_application(self.executor, row.node_id,
                    target_revision=row.expected_revision + 1, receipt=str(step.pk))
                if projection_updated:
                    catalog.observe_node(self.executor, row.node_id, eligible=row.eligible,
                        source='operation:' + str(row.pk), observed_at=timezone.now())
                # Новый catalog intent не отменяет подтверждённый старый факт.
                # Не затирать новую projection, но сохранить receipt/quiescence.
            else:
                self._authorize(self.executor, 'catalog.record_node_application', row.node_id, self._scope(row))
            m.EffectReceipt.objects.create(step=step, token=claim.token, epoch=row.epoch, node_id=row.node_id,
                source_revision=row.expected_revision, decision_revision=expected_revision, applied=applied)
            step.quiescent_at = timezone.now()
            step.save(update_fields=['quiescent_at'])
            code = ('effect_confirmed' if projection_updated else 'effect_confirmed_prior_revision') if applied else 'effect_failed'
            _move(row, State.SUCCEEDED if applied else State.FAILED, code, expected_revision=row.revision)
            m.ResourceClaim.objects.filter(operation=row, step=step, token=claim.token, released_at=None).update(released_at=step.quiescent_at)
            result = _snapshot(row)
        return result

    def reconcile(self, actor, operation_id, *, expected_revision):
        """Явный допуск inspection; никогда не выдаёт новый effect token."""
        return self._inspection(actor, operation_id, expected_revision=expected_revision, claim=False)

    def inspection_claim(self, actor, operation_id, *, expected_revision):
        """Trusted worker получает прежнюю generation только для inspection."""
        return self._inspection(actor, operation_id, expected_revision=expected_revision, claim=True)

    def _inspection(self, actor, operation_id, *, expected_revision, claim):
        _independent()
        with self._transaction():
            installation = self._installation()
            row = m.Operation.objects.select_for_update().get(pk=operation_id)
            if row.epoch != installation.epoch or row.restore_generation != installation.generation:
                raise Refused('epoch_conflict')
            projects = self._scope(row)
            self._authorize(actor, 'operations.recover_operation', row.node_id, projects)
            self._authorize(actor, 'catalog.' + row.action + '_node', row.node_id, projects)
            if row.state not in (State.RUNNING, State.UNKNOWN, State.RECONCILING):
                raise Refused('operation_conflict')
            if row.state == State.RECONCILING:
                if type(expected_revision) is not int or row.revision != expected_revision:
                    raise Refused('revision_conflict')
            else:
                _move(row, State.RECONCILING, 'inspection_requested', expected_revision=expected_revision)
            if claim:
                return self._claim_value(row, m.Step.objects.get(operation=row))
            return _snapshot(row)

    def cancel(self, actor, operation_id, *, expected_revision):
        _independent()
        with self._transaction():
            self._installation()
            row = m.Operation.objects.select_for_update().get(pk=operation_id)
            self._authorize(actor, 'operations.cancel_operation', row.node_id, self._scope(row))
            if row.state != State.ACCEPTED:
                raise Refused('operation_conflict')
            _move(row, State.CANCELLED, 'cancelled_before_start', expected_revision=expected_revision)
            return _snapshot(row)

    def handler(self, operation_id):
        self._require_binding()
        return m.Step.objects.get(operation_id=operation_id).name

    def pending(self, *, limit=8):
        self._require_binding()
        if not 1 <= limit <= 8:
            raise ValueError('worker batch exceeds controller safety caps')
        work = Q(state=State.ACCEPTED, target_kind='node') | Q(state=State.ACCEPTED, action='stop_job') | Q(action='build', state__in=('running','unknown','reconciling'), step__lease_until__lt=timezone.now())
        # Stale lease разрешает только inspection прежнего token, не новый apply.
        return tuple(m.Operation.objects.filter(work).order_by('created_at', 'id').values_list('pk', flat=True)[:limit])

    def pending_machine(self, *, node_id, executor_epoch, project_ids, limit):
        from .puppet import RestartHandler
        return RestartHandler(self).pending(node_id=node_id, executor_epoch=executor_epoch, project_ids=project_ids, limit=limit)

    def current_machine(self, operation_id, step_id):
        from .puppet import RestartHandler
        return RestartHandler(self).current(operation_id, step_id)

    def claimed_machine(self, work, attempt_id):
        from .puppet import RestartHandler
        return RestartHandler(self).claimed(work, attempt_id)

    def completed_machine(self, work, attempt_id, outcome, evidence):
        from .puppet import RestartHandler
        return RestartHandler(self).completed(work, attempt_id, outcome, evidence)

    def retired_machine(self, target_id, executor_epoch):
        from .puppet import RestartHandler
        return RestartHandler(self).retired(target_id, executor_epoch)

    def prune(self):
        _independent()
        with self._transaction():
            self._installation()
            rows = m.Operation.objects.filter(completed_at__lt=timezone.now() - timedelta(days=90), result_pruned=False)
            # Tombstone и всё ещё unresolved evidence не удаляются.
            ids = [row.pk for row in rows if not m.ResourceClaim.objects.filter(operation=row, released_at=None).exists()]
            return m.Operation.objects.filter(pk__in=ids).update(result_pruned=True)

    def publish_one(self, publisher):
        self._require_binding()
        _independent()
        event = m.OutboxEvent.objects.filter(published_at=None).select_related('operation').order_by('id').first()
        if event is None:
            return False
        value = {'event_id': str(event.event_id), 'epoch': str(event.operation.epoch),
            'operation_id': str(event.operation_id), 'revision': str(event.revision),
            'state': event.state, 'code': event.code, 'type': 'operation.changed',
            'target': {'kind': 'project' if event.operation.action == 'build' else event.operation.target_kind,
                'id': str(event.operation.projects.get().pk if event.operation.action == 'build' else event.operation.target_id if event.operation.target_kind == 'puppet' else event.operation.node_id),
                'revision': str(event.target_revision),
                **({'incarnation':str(event.operation.incarnation_id)} if event.operation.target_kind == 'puppet' else {})},
            'project_ids': [str(pk) for pk in event.operation.projects.values_list('pk', flat=True)]}
        # Duplicate delivery при потере ACK — штатна; event_id стабилен.
        publisher(value)
        with self._transaction():
            m.OutboxEvent.objects.filter(pk=event.pk, published_at=None).update(published_at=timezone.now())
        return True
