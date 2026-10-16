"""Proxy usage — отдельный leaf схемы execution; нет transcript checkpoints."""
from uuid import uuid4
from django.db import models
from django.db.models import Q
from django.utils import timezone


class UsageSource(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    remote_id = models.UUIDField(unique=True)
    boundary_id = models.UUIDField()
    profile = models.CharField(max_length=64)
    installed_version = models.CharField(max_length=32)
    enabled = models.BooleanField(default=True)
    source_complete = models.BooleanField(default=False, editable=False)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        app_label = 'execution'
        default_permissions = ()
        permissions = [('view_usage', 'Can read installation-wide proxy usage')]
        constraints = [models.CheckConstraint(condition=Q(source_complete=False), name='usage_source_never_complete')]


class UsageReadIntent(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    source = models.ForeignKey(UsageSource, on_delete=models.PROTECT)
    owner_id = models.UUIDField()
    boundary_id = models.UUIDField()
    count = models.PositiveSmallIntegerField()
    state = models.CharField(max_length=16, default='reading')
    started_at = models.DateTimeField(default=timezone.now)
    finished_at = models.DateTimeField(null=True)
    gap_code = models.CharField(max_length=32, default='')
    batch_fingerprint = models.CharField(max_length=64, default='')
    accepted = models.PositiveSmallIntegerField(default=0)
    duplicates = models.PositiveSmallIntegerField(default=0)
    conflicts = models.PositiveSmallIntegerField(default=0)
    rejected = models.PositiveSmallIntegerField(default=0)

    class Meta:
        app_label = 'execution'
        default_permissions = ()
        constraints = [
            models.UniqueConstraint(fields=('source',), condition=Q(state__in=('reading', 'unknown')),
                                    name='usage_one_unresolved_read'),
            models.CheckConstraint(condition=Q(count__gte=1, count__lte=100), name='usage_read_count_bound'),
            models.CheckConstraint(condition=Q(state__in=('reading', 'unknown', 'committed', 'gap')),
                                   name='usage_intent_state'),
        ]
        indexes = [models.Index(fields=('source', 'started_at'), name='usage_intent_source_time')]


class UsageEvent(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    source = models.ForeignKey(UsageSource, on_delete=models.PROTECT)
    intent = models.ForeignKey(UsageReadIntent, on_delete=models.PROTECT)
    event_id = models.CharField(max_length=64)
    fingerprint = models.CharField(max_length=64)
    occurred_at = models.DateTimeField()
    key_id = models.CharField(max_length=68, default='')
    provider = models.CharField(max_length=128)
    model = models.CharField(max_length=256)
    alias = models.CharField(max_length=256)
    failed = models.BooleanField()
    quality = models.CharField(max_length=16)
    counters = models.JSONField()

    class Meta:
        app_label = 'execution'
        default_permissions = ()
        constraints = [models.UniqueConstraint(fields=('source', 'event_id'), name='usage_execution_identity')]
        indexes = [models.Index(fields=('source', 'occurred_at'), name='usage_event_source_time')]


class UsageAggregate(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    source = models.ForeignKey(UsageSource, on_delete=models.PROTECT)
    day = models.DateField()
    key_id = models.CharField(max_length=68, default='')
    provider = models.CharField(max_length=128)
    model = models.CharField(max_length=256)
    alias = models.CharField(max_length=256)
    requests = models.PositiveBigIntegerField(default=0)
    failed = models.PositiveBigIntegerField(default=0)
    partial = models.PositiveBigIntegerField(default=0)
    totals = models.JSONField(default=dict)
    known = models.JSONField(default=dict)

    class Meta:
        app_label = 'execution'
        default_permissions = ()
        constraints = [models.UniqueConstraint(fields=('source', 'day', 'key_id', 'provider', 'model', 'alias'),
                                               name='usage_aggregate_dimensions')]
        indexes = [models.Index(fields=('day', 'source'), name='usage_aggregate_day_source')]
