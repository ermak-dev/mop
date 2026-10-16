"""Наблюдения и производные bounded snapshots; не вторая authority каталога."""
import uuid
from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone
from mop.server.catalog.models import ImmutableRecord, Record


class ProjectionRuntime(models.Model):
    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    epoch = models.UUIDField()
    run_id = models.UUIDField(default=uuid.uuid4)

    class Meta:
        constraints = [models.CheckConstraint(condition=Q(id=1), name='projection_single_runtime')]


class ProjectionClock(models.Model):
    target = models.OneToOneField('execution.Assignment', primary_key=True, on_delete=models.PROTECT)
    revision = models.PositiveBigIntegerField(default=0)
    retired = models.BooleanField(default=False)


class SourceProducer(Record):
    target = models.ForeignKey('execution.Assignment', on_delete=models.PROTECT)
    kind = models.CharField(max_length=16)
    incarnation = models.ForeignKey('execution.ExecutionIncarnation', on_delete=models.PROTECT)
    executor_epoch = models.UUIDField()
    epoch = models.UUIDField()

    class Meta(Record.Meta):
        constraints = Record.Meta.constraints + [
            models.UniqueConstraint(fields=('target','kind'), name='projection_current_producer'),
            models.CheckConstraint(condition=Q(kind__in=('state','clone','size')), name='projection_producer_kind'),
        ]


class ProducerEpoch(ImmutableRecord):
    target = models.ForeignKey('execution.Assignment', on_delete=models.PROTECT)
    kind = models.CharField(max_length=16)
    epoch = models.UUIDField()

    class Meta(Record.Meta):
        constraints = Record.Meta.constraints + [models.UniqueConstraint(
            fields=('target','kind','epoch'), name='projection_producer_epoch_never_reused')]


class SourceObservation(models.Model):
    target = models.ForeignKey('execution.Assignment', on_delete=models.PROTECT)
    incarnation = models.ForeignKey('execution.ExecutionIncarnation', on_delete=models.PROTECT)
    producer_epoch = models.UUIDField()
    controller_run = models.UUIDField()
    kind = models.CharField(max_length=16)
    sequence = models.PositiveBigIntegerField()
    observed_at = models.DateTimeField()
    received_at = models.DateTimeField(default=timezone.now)
    status = models.CharField(max_length=16)
    reason = models.CharField(max_length=32, blank=True)
    payload = models.JSONField(default=dict)

    class Meta:
        db_table = 'projection_observation'
        permissions = [('ingest_observations', 'Can ingest enrolled node observations')]
        constraints = [
            models.UniqueConstraint(fields=('target', 'kind'), name='projection_source_target_kind'),
            models.CheckConstraint(condition=Q(sequence__gte=1), name='projection_source_positive_sequence'),
            models.CheckConstraint(condition=Q(kind__in=('state', 'clone', 'size')), name='projection_source_kind'),
            models.CheckConstraint(condition=Q(status__in=('fresh', 'stale', 'unavailable')), name='projection_source_status'),
            models.CheckConstraint(condition=Q(observed_at__lte=models.F('received_at')), name='projection_source_time_order'),
        ]


class ProjectSequence(models.Model):
    project = models.OneToOneField('catalog.Project', primary_key=True, on_delete=models.PROTECT)
    position = models.PositiveBigIntegerField(default=0)


class ProjectionEvent(ImmutableRecord):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey('catalog.Project', on_delete=models.PROTECT)
    position = models.PositiveBigIntegerField()
    epoch = models.UUIDField()
    target_id = models.UUIDField()
    incarnation_id = models.UUIDField(null=True)
    revision = models.PositiveBigIntegerField()
    deleted = models.BooleanField(default=False)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta(Record.Meta):
        constraints = Record.Meta.constraints + [models.UniqueConstraint(fields=('project', 'position'), name='projection_event_project_position')]


class ReadSnapshot(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    binding = models.CharField(max_length=64)
    epoch = models.UUIDField()
    run_id = models.UUIDField(null=True)
    scope = models.CharField(max_length=64)
    cursor = models.TextField()
    rows = models.JSONField()
    expires_at = models.DateTimeField()
