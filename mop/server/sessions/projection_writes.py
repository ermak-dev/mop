"""Явный SQL-only publication port. Внешний источник никогда не опрашивается здесь."""
from dataclasses import dataclass
from uuid import UUID, uuid4
from django.db import transaction
from django.utils import timezone
from mop.server.access.context import ActorContext, PrincipalKind
from mop.server.access.services import Access, Denied, NotVisible
from mop.server.catalog.models import Project
from .models import Assignment
from .projection_models import (ProjectionRuntime, ProjectionClock, SourceObservation,
                                SourceProducer, ProducerEpoch, ProjectSequence, ProjectionEvent)
from .projection_policy import KINDS, ERRORS, safe_payload


class SourceConflict(ValueError):
    """Ответ не принадлежит текущему запуску/назначению/источнику."""


@dataclass(frozen=True)
class ControllerRun:
    epoch: UUID
    id: UUID


def lock_projects(project_ids):
    """Внешняя composition берёт эти же parent locks до aggregate/result/outbox."""
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError('publication requires an application transaction')
    ids = tuple(sorted(set(project_ids), key=str))
    rows = tuple(Project.objects.select_for_update().filter(pk__in=ids).order_by('pk'))
    if len(rows) != len(ids):
        raise SourceConflict('publication project is missing')
    return rows


def entity_revision(target, clock_revision):
    # Никакого read counter: все слагаемые принадлежат сохранённым owners.
    # Incarnation revision не складывается: её замена уже advances Assignment.
    return target.revision + target.project.revision + target.node.revision + clock_revision


class ProjectionWriter:
    """Наблюдения и invalidation принадлежат caller transaction, а не signals."""
    def __init__(self, run):
        if type(run) is not ControllerRun:
            raise ValueError('trusted controller run required')
        self.run = run

    @classmethod
    def begin_run(cls, epoch):
        """Явный startup hook; GET его не вызывает. Старые наблюдения остаются stale."""
        if type(epoch) is not UUID:
            raise ValueError('typed installation epoch required')
        if transaction.get_connection().in_atomic_block:
            raise RuntimeError('startup requires its own commit boundary')
        with transaction.atomic():
            targets = tuple(ProjectionClock.objects.values_list('target_id', 'target__project_id')[:5001])
            if len(targets) > 5000:
                raise SourceConflict('projection startup invalidation exceeds bound')
            lock_projects(project for _, project in targets)
            runtime, _ = ProjectionRuntime.objects.select_for_update().get_or_create(pk=1,
                defaults={'epoch':epoch})
            runtime.epoch, runtime.run_id = epoch, uuid4()
            runtime.save(update_fields=('epoch', 'run_id'))
            writer = cls(ControllerRun(epoch, runtime.run_id))
            for target_id, _ in sorted(targets, key=lambda item: str(item[0])):
                target = Assignment.objects.select_for_update(of=('self',)).select_related('project','node').get(pk=target_id)
                clock = ProjectionClock.objects.get(target=target)
                writer._changed(target, retire=clock.retired)
            return writer.run

    def _current_run(self):
        current = ProjectionRuntime.objects.select_for_update().filter(pk=1).first()
        if current is None or current.run_id != self.run.id or current.epoch != self.run.epoch:
            raise SourceConflict('controller run changed; discard the old observation')

    def _changed(self, target, *, retire=False):
        clock, _ = ProjectionClock.objects.select_for_update().get_or_create(target=target)
        if clock.retired and not retire:
            raise SourceConflict('target is retired')
        clock.revision += 1
        clock.retired = clock.retired or retire
        clock.save(update_fields=('revision','retired'))
        sequence, _ = ProjectSequence.objects.select_for_update().get_or_create(project_id=target.project_id)
        sequence.position += 1
        sequence.save(update_fields=('position',))
        return ProjectionEvent.objects.create(project_id=target.project_id, position=sequence.position,
            epoch=self.run.epoch, target_id=target.id, incarnation_id=target.current_incarnation_id,
            revision=entity_revision(target, clock.revision), deleted=clock.retired)

    def changed(self, *, target_id, retire=False):
        """Owner port: вызывается после mutation, до result/audit COMMIT.

        Caller уже держит project locks через lock_projects. Это не новый effect,
        не обратный вызов Operations и не разрешение менять чужой aggregate.
        """
        if not transaction.get_connection().in_atomic_block:
            raise RuntimeError('publication requires the owner transaction')
        target = Assignment.objects.select_related('project','node').get(pk=target_id)
        lock_projects((target.project_id,))
        self._current_run()
        target = Assignment.objects.select_for_update(of=('self',)).select_related('project','node').get(pk=target_id)
        return str(self._changed(target, retire=retire).id)

    def retire(self, *, target_id):
        return self.changed(target_id=target_id, retire=True)

    def _source_target(self, actor, target_id, incarnation_id):
        if type(actor) is not ActorContext or actor.kind is not PrincipalKind.SERVICE:
            raise Denied('observation requires an enrolled node credential')
        source = Assignment.objects.filter(pk=target_id).values('project_id').first()
        if source is None:
            raise NotVisible('resource not found')
        projects = lock_projects((source['project_id'],))
        self._current_run()
        target = Assignment.objects.select_for_update(of=('self',)).select_related(
            'project','node','current_incarnation').get(pk=target_id)
        Access().authorize(actor, 'execution.ingest_observations', project_ids=(target.project_id,), node_ids=(target.node_id,))
        incarnation = target.current_incarnation
        if (projects[0].retired_at is not None or target.node.retired_at is not None
                or target.project_id != source['project_id'] or target.node_id != actor.node_id
                or incarnation is None or incarnation.id != incarnation_id or not incarnation.accepting
                or incarnation.assignment_id != target.id or incarnation.node_id != target.node_id):
            raise SourceConflict('source target or executor incarnation changed')
        clock = ProjectionClock.objects.filter(target=target).first()
        if clock is not None and clock.retired:
            raise SourceConflict('target is retired')
        return target

    def producer(self, actor, *, target_id, kind):
        """Адресованный query текущей регистрации; не get_or_create из GET."""
        if type(target_id) is not UUID or kind not in KINDS:
            raise ValueError('typed source target and kind required')
        with transaction.atomic():
            incarnation = Assignment.objects.filter(pk=target_id).values_list('current_incarnation_id', flat=True).first()
            target = self._source_target(actor, target_id, incarnation)
            row = SourceProducer.objects.filter(target=target, kind=kind).first()
            return {'target_id':str(target.id), 'project_id':str(target.project_id), 'node_id':str(target.node_id),
                'incarnation_id':str(target.current_incarnation_id),
                'executor_epoch':str(target.current_incarnation.executor_epoch), 'kind':kind,
                'producer':None if row is None else {'epoch':str(row.epoch), 'revision':str(row.revision),
                    'incarnation_id':str(row.incarnation_id), 'executor_epoch':str(row.executor_epoch)}}

    def bind_producer(self, actor, *, target_id, incarnation_id, executor_epoch, kind,
                      producer_epoch, expected_revision):
        if (any(type(value) is not UUID for value in (target_id, incarnation_id, executor_epoch, producer_epoch))
                or kind not in KINDS or type(expected_revision) is not int or not 0 <= expected_revision < 2**63):
            raise ValueError('complete addressed producer binding required')
        with transaction.atomic():
            target = self._source_target(actor, target_id, incarnation_id)
            if target.current_incarnation.executor_epoch != executor_epoch:
                raise SourceConflict('executor epoch changed')
            row = SourceProducer.objects.filter(target=target, kind=kind).first()
            if row is not None:
                if (row.incarnation_id, row.executor_epoch, row.epoch) == (incarnation_id, executor_epoch, producer_epoch):
                    return row.revision
                if row.revision != expected_revision:
                    raise SourceConflict('producer binding revision changed')
            elif expected_revision != 0:
                raise SourceConflict('producer binding revision changed')
            if ProducerEpoch.objects.filter(target=target, kind=kind, epoch=producer_epoch).exists():
                raise SourceConflict('retired producer epoch cannot be reused')
            ProducerEpoch.objects.create(target=target, kind=kind, epoch=producer_epoch)
            if row is None:
                row = SourceProducer.objects.create(target=target, kind=kind, incarnation_id=incarnation_id,
                    executor_epoch=executor_epoch, epoch=producer_epoch)
            else:
                row.incarnation_id, row.executor_epoch, row.epoch = incarnation_id, executor_epoch, producer_epoch
                row.revision += 1
                row.save(update_fields=('incarnation','executor_epoch','epoch','revision'))
                if SourceObservation.objects.filter(target=target, kind=kind).exists():
                    self._changed(target)
            return row.revision

    def observe(self, actor, *, target_id, incarnation_id, producer_epoch, sequence, kind,
                observed_at, payload, status='fresh', reason=''):
        if (any(type(value) is not UUID for value in (target_id, incarnation_id, producer_epoch))
                or type(sequence) is not int or not 1 <= sequence < 2**63 or kind not in KINDS
                or status not in ('fresh','stale','unavailable') or reason not in ('', *ERRORS)
                or type(observed_at) is not timezone.datetime or timezone.is_naive(observed_at)
                or observed_at > timezone.now() or status == 'unavailable' and not reason):
            raise ValueError('complete addressed versioned source observation required')
        values = safe_payload(kind, payload, status=status)
        with transaction.atomic():
            target = self._source_target(actor, target_id, incarnation_id)
            producer = SourceProducer.objects.filter(target=target, kind=kind).first()
            if (producer is None or producer.epoch != producer_epoch or producer.incarnation_id != incarnation_id
                    or producer.executor_epoch != target.current_incarnation.executor_epoch):
                raise SourceConflict('source producer is not the current enrolled producer')
            previous = SourceObservation.objects.filter(target=target, kind=kind).first()
            if (previous is not None and previous.incarnation_id == incarnation_id
                    and previous.producer_epoch == producer_epoch and sequence <= previous.sequence):
                return False
            SourceObservation.objects.update_or_create(target=target, kind=kind, defaults={
                'incarnation_id':incarnation_id, 'producer_epoch':producer_epoch, 'sequence':sequence,
                'controller_run':self.run.id, 'observed_at':observed_at, 'received_at':timezone.now(),
                'status':status, 'reason':reason, 'payload':values})
            self._changed(target)
            return True
