"""Короткие atomic use cases #641; сеть/history остаётся вне транзакции.

Порядок locks: installation port → Assignment → observation → private cursor.
Никаких imports controller/root, общего audit payload или terminal input replay.
"""
from dataclasses import dataclass
from datetime import timedelta
import hashlib
from django.db import transaction
from django.utils import timezone

from mop.common.telemetry import (Ack, CurrentSession, Observation, PrivateHistoryReader,
    SessionAuthority, SessionScope, SourceAttributedTurn, TurnAttribution, canonical, text)
from mop.server.access.services import Access, current_actor, is_node_executor
from . import telemetry_models as m
from .models import Assignment, ExecutionIncarnation
from .protocol import AuthorityV1, InstallationQuery
from .services import Assignments, Conflict, Unavailable, machine, unbound_installation


class UnboundSessions:
    def current(self, binding):
        raise Unavailable('authoritative current session port is not bound')


class UnknownAttribution:
    def resolve(self, binding, session_id, turn_id):
        return None


class UnboundHistory:
    def read(self, scope, cursor):
        raise Unavailable('private history reader is not bound')


@dataclass(frozen=True)
class Latest:
    current_model: str | None
    last_confirmed_model: str | None
    model_hint: str | None
    sequence: int
    incomplete: bool
    received_at: object


@dataclass(frozen=True)
class ConfirmedModel:
    receipt_id: int
    confirmed_sequence: int
    observed_sequence: int
    observation_revision: int
    model: str


class Telemetry:
    def __init__(self, *, sessions: SessionAuthority | None = None,
                 attribution: TurnAttribution | None = None, history: PrivateHistoryReader | None = None,
                 installation: InstallationQuery | None = None, freshness_seconds=30, retained_records=4096):
        if (type(freshness_seconds) is not int or not 1 <= freshness_seconds <= 300
                or type(retained_records) is not int or not 1 <= retained_records <= 10000):
            raise ValueError('bounded telemetry freshness/retention required')
        self.sessions = sessions if sessions is not None else UnboundSessions()
        self.attribution = attribution if attribution is not None else UnknownAttribution()
        self.history = history if history is not None else UnboundHistory()
        self.installation = installation if installation is not None else unbound_installation
        self.freshness_seconds, self.retained_records = freshness_seconds, retained_records

    def _outside(self):
        if transaction.get_connection().in_atomic_block:
            raise Unavailable('telemetry acknowledgement requires no surrounding transaction')

    def _scope(self, actor, scope, *, private=False):
        if type(scope) is not SessionScope:
            raise ValueError('typed session scope required')
        current_actor(actor)
        authority = self.installation()
        if type(authority) is not AuthorityV1:
            raise Unavailable('invalid installation authority')
        if authority.epoch != scope.epoch:
            raise Conflict('installation epoch changed')
        if not private:
            machine(actor, scope.node_id, 'ingest_telemetry')
        row = Assignment.objects.select_for_update().filter(pk=scope.target_id).first()
        if row is None:
            raise Conflict('current assignment is unavailable')
        if private:
            Access().authorize(actor, 'execution.read_history', project_ids=(row.project_id,))
        else:
            machine(actor, row.node_id, 'ingest_telemetry', (row.project_id,))
        binding = Assignments().current(project_id=row.project_id, target_id=row.id)
        if (binding is None or row.node_id != scope.node_id or row.current_incarnation_id != scope.incarnation_id
                or binding.executor_epoch != scope.executor_epoch):
            raise Conflict('assignment or executor incarnation changed')
        # activate/restart берут Assignment lock первыми; authoritative current()
        # обязан быть SQL-only, не вызывать I/O/Access→Sessions reverse locks.
        incarnation = ExecutionIncarnation.objects.select_for_update().get(pk=scope.incarnation_id)
        if (not incarnation.accepting or incarnation.node_id != scope.node_id
                or incarnation.executor_epoch != scope.executor_epoch):
            raise Conflict('executor incarnation is no longer current')
        source = self.sessions.current(binding)
        if (type(source) is not CurrentSession or source.session_id != scope.session_id
                or source.producer_epoch != scope.producer_epoch):
            raise Conflict('authoritative current session changed')
        return binding

    def _observation(self, scope):
        row, _ = m.SessionObservation.objects.get_or_create(incarnation_id=scope.incarnation_id,
            producer_epoch=scope.producer_epoch, defaults=dict(assignment_id=scope.target_id,
                node_id=scope.node_id, epoch=scope.epoch, executor_epoch=scope.executor_epoch, session_id=scope.session_id))
        if (row.assignment_id != scope.target_id or row.session_id != scope.session_id
                or row.executor_epoch != scope.executor_epoch or row.epoch != scope.epoch):
            raise Conflict('producer epoch already bound to another session')
        return row

    def ingest(self, actor, observation):
        self._outside()
        if type(observation) is not Observation:
            raise ValueError('typed finite telemetry observation required')
        digest = hashlib.sha256(canonical(observation.wire()).encode()).hexdigest()
        with transaction.atomic():
            binding = self._scope(actor, observation.scope)
            row = self._observation(observation.scope)
            previous = m.TelemetryReceipt.objects.filter(observation=row, sequence=observation.sequence).first()
            if previous is not None:
                if previous.digest != digest:
                    raise Conflict('telemetry sequence has different evidence')
                return Ack(row.producer_epoch, observation.sequence)
            if observation.sequence <= row.sequence:
                raise Conflict('out-of-order or expired telemetry receipt')
            gap = observation.sequence-row.sequence-1
            row.incomplete = row.incomplete or gap > 0 or observation.dropped > 0
            if gap > 0 or observation.dropped > 0:
                # Пропущенное событие могло сменить модель. Сохраняем историческое
                # подтверждение, но не выдаём его за current после потери данных.
                row.model_proven = False
            # Drop count и sequence gap пересекаются; не складываем их дважды.
            row.gap_count = min(2**63-1, row.gap_count+max(gap, observation.dropped))
            row.sequence, row.event = observation.sequence, observation.event
            row.received_at = timezone.now()
            if observation.event in ('Statusline', 'PostModelSwitch'):
                m.ModelObservation.objects.create(observation=row, sequence=observation.sequence,
                    model=observation.model, kind=observation.event, causal_switch=observation.causal_switch)
                row.model_hint = observation.model
                row.model_received_at = row.received_at
                if observation.event == 'PostModelSwitch':
                    row.confirmed_model = observation.model
                    row.confirmed_sequence = observation.sequence
                    row.model_proven = True
                else:
                    # Ни arrival, ни external timestamp, ни совпадение ID без
                    # causal fence не доказывают, что status получен после switch.
                    row.model_proven = (row.confirmed_sequence > 0
                        and observation.causal_switch == row.confirmed_sequence
                        and observation.model == row.confirmed_model)
            else:
                source = row.turn_source
                if source is not None and source.turn_id == observation.turn_id:
                    # Источник уже зафиксирован. Даже unknown не заменяется новым
                    # owner/endpoint при продолжении того же хода или retention.
                    names = ('initiator_user_id', 'initiator_endpoint_id',
                             'initiator_session_id', 'operation_id', 'message_id')
                    values = {name: getattr(source, name) for name in names}
                else:
                    initiator = None
                    if observation.turn_id is not None:
                        attributed = self.attribution.resolve(binding, row.session_id, observation.turn_id)
                        if attributed is not None:
                            if type(attributed) is not SourceAttributedTurn or attributed.turn_id != observation.turn_id:
                                raise Unavailable('invalid source-attributed turn projection')
                            initiator = attributed.initiator
                    values = {} if initiator is None else dict(initiator_user_id=initiator.user_id,
                        initiator_endpoint_id=initiator.endpoint_id,
                        initiator_session_id=initiator.session_id, operation_id=initiator.operation_id,
                        message_id=initiator.message_id)
                outcome = m.TurnOutcome.objects.create(observation=row, sequence=observation.sequence,
                    turn_id=observation.turn_id, event=observation.event, error=observation.error, **values)
                if observation.turn_id is not None:
                    row.turn_source = outcome
            row.revision += 1
            row.save()
            m.TelemetryReceipt.objects.create(observation=row, sequence=observation.sequence, digest=digest)
            self._prune_records(row)
            ack = Ack(row.producer_epoch, observation.sequence)
        # durable COMMIT, не savepoint: HTTP reply разрешён только отсюда.
        return ack

    def _prune_records(self, row):
        cutoff = max(0, row.sequence-self.retained_records)
        for model in (m.TelemetryReceipt, m.TurnOutcome, m.ModelObservation):
            old = model.objects.filter(observation=row, sequence__lte=cutoff)
            if model is m.TurnOutcome:
                # Одна pinned ссылка на текущий ход сверх bounded истории.
                old = old.exclude(pk=row.turn_source_id)
            old.delete()

    def ready_model(self, scope, desired_model):
        """SQL-only owner port: вызывающий уже держит текущий Assignment lock.

        Не использует _observation: отсутствие hooks не создаёт пустой receipt.
        Историческая incomplete не мешает новому настоящему switch доказать current.
        """
        if (not transaction.get_connection().in_atomic_block or type(scope) is not SessionScope
                or not text(desired_model)):
            raise ValueError('transactional current scope and desired model required')
        row = m.SessionObservation.objects.filter(assignment_id=scope.target_id,
            incarnation_id=scope.incarnation_id, node_id=scope.node_id, epoch=scope.epoch,
            executor_epoch=scope.executor_epoch, producer_epoch=scope.producer_epoch,
            session_id=scope.session_id).first()
        now = timezone.now()
        if (row is None or not row.model_proven or row.confirmed_model != desired_model
                or row.model_received_at is None or row.model_received_at > now
                or now-row.model_received_at > timedelta(seconds=self.freshness_seconds)):
            raise Conflict('fresh causally confirmed desired model is unavailable')
        switch = m.ModelObservation.objects.filter(observation=row, sequence=row.confirmed_sequence,
            kind='PostModelSwitch', model=desired_model).first()
        receipt = m.TelemetryReceipt.objects.filter(observation=row, sequence=row.confirmed_sequence).first()
        if switch is None or receipt is None:
            raise Conflict('committed model switch and telemetry receipt are unavailable')
        return ConfirmedModel(receipt.pk, row.confirmed_sequence, row.sequence, row.revision, row.confirmed_model)

    def latest(self, actor, scope):
        with transaction.atomic():
            self._scope(actor, scope, private=not is_node_executor(actor))
            row = self._observation(scope)
            fresh = row.model_received_at is not None and timezone.now()-row.model_received_at <= timedelta(seconds=self.freshness_seconds)
            return Latest(row.confirmed_model if fresh and row.model_proven else None,
                row.confirmed_model, row.model_hint, row.sequence, row.incomplete, row.received_at)

    def _cursor(self, actor, row, expected_revision):
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError('nonnegative cursor revision required')
        cursor = m.PrivateHistoryCursor.objects.select_for_update().filter(
            user_id=actor.user_id, observation=row).first()
        if (cursor.revision if cursor is not None else 0) != expected_revision:
            raise Conflict('private history cursor revision changed')
        return cursor

    def read_history(self, actor, scope, *, expected_revision):
        self._outside()
        with transaction.atomic():
            self._scope(actor, scope, private=True)
            row = self._observation(scope)
            cursor = self._cursor(actor, row, expected_revision)
            previous = cursor.cursor if cursor is not None and cursor.cursor else None
        page = self.history.read(scope, previous)
        self._history_page(page, scope)
        # Наружу не уходит даже payload, прочитанный после отзыва прав/смены session.
        with transaction.atomic():
            self._scope(actor, scope, private=True)
            row = self._observation(scope)
            cursor = self._cursor(actor, row, expected_revision)
            if cursor is None:
                cursor = m.PrivateHistoryCursor(user_id=actor.user_id, observation=row)
            else:
                cursor.revision += 1
            cursor.cursor, cursor.source_epoch = page['cursor'], page['epoch']
            cursor.updated_at = timezone.now()
            cursor.save()
            revision = cursor.revision
        return dict(page, revision=revision)

    def _history_page(self, page, scope):
        names = {'epoch', 'session_id', 'cursor', 'records', 'reset', 'has_more', 'partial'}
        if (type(page) is not dict or set(page) != names or page['session_id'] != scope.session_id
                or not text(page['epoch']) or not text(page['cursor'], 8192)
                or type(page['records']) is not list or len(page['records']) > 200
                or any(type(page[k]) is not bool for k in ('reset', 'has_more', 'partial'))
                or len(canonical(page).encode()) > 262144):
            raise Unavailable('invalid bounded private history page')

    def reset_history(self, actor, scope, *, expected_revision):
        self._outside()
        with transaction.atomic():
            self._scope(actor, scope, private=True)
            row = self._observation(scope)
            cursor = self._cursor(actor, row, expected_revision)
            if cursor is None:
                raise Conflict('private history cursor does not exist')
            cursor.cursor, cursor.source_epoch = '', ''
            cursor.revision += 1
            cursor.updated_at = timezone.now()
            cursor.save()
            return cursor.revision

    def expire(self, *, before, limit=100):
        """Root maintenance вызывает bounded retention вне потока ingest/terminal."""
        self._outside()
        if (type(limit) is not int or not 1 <= limit <= 500
                or timezone.is_naive(before) or before > timezone.now()-timedelta(days=1)):
            raise ValueError('bounded historical retention required')
        ids = tuple(m.SessionObservation.objects.filter(received_at__lt=before)
                    .order_by('received_at').values_list('id', flat=True)[:limit])
        # Только неактивные producers: high-water живого producer удалять нельзя.
        removed = 0
        for identity in ids:
            source = m.SessionObservation.objects.get(pk=identity)
            with transaction.atomic():
                assignment = Assignment.objects.select_for_update().get(pk=source.assignment_id)
                row = m.SessionObservation.objects.filter(pk=identity, received_at__lt=before).first()
                if row is None:
                    continue
                binding = Assignments().current(project_id=assignment.project_id, target_id=assignment.pk)
                current = self.sessions.current(binding) if binding is not None else None
                if (binding is not None and binding.incarnation_id == row.incarnation_id
                        and type(current) is CurrentSession and current.producer_epoch == row.producer_epoch):
                    continue
                row.delete()
                removed += 1
        return removed
