"""Запрос, stages, fencing и публикация; эффектов в save/signals нет."""
import uuid
from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone

from .lifecycle import State


class Operation(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    actor_binding = models.JSONField()
    request_id = models.UUIDField()
    digest = models.CharField(max_length=64)
    epoch = models.UUIDField()
    restore_generation = models.UUIDField()
    action = models.CharField(max_length=32)
    node = models.ForeignKey('catalog.Node', on_delete=models.PROTECT)
    projects = models.ManyToManyField('catalog.Project')
    expected_revision = models.PositiveBigIntegerField()
    eligible = models.BooleanField()
    not_after = models.DateTimeField()
    state = models.CharField(max_length=16, default=State.ACCEPTED)
    revision = models.PositiveBigIntegerField(default=1)
    code = models.CharField(max_length=64, blank=True)
    reason = models.CharField(max_length=512, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    completed_at = models.DateTimeField(null=True)
    result_pruned = models.BooleanField(default=False)

    class Meta:
        permissions = [('cancel_operation', 'Can cancel operation'), ('recover_operation', 'Can recover operation'),
                       ('view_audit', 'Can view audit')]
        constraints = [
            models.UniqueConstraint(fields=('actor', 'request_id'), name='operation_actor_request_unique'),
            models.CheckConstraint(condition=Q(revision__gte=1, expected_revision__gte=1), name='operation_positive_revisions'),
            models.CheckConstraint(condition=Q(state__in=tuple(State)), name='operation_finite_state'),
            models.CheckConstraint(condition=Q(action__in=('drain', 'enable', 'build')), name='operation_finite_action'),
            models.CheckConstraint(condition=Q(result_pruned=False) | Q(completed_at__isnull=False), name='operation_pruned_terminal'),
        ]
        indexes = [models.Index(fields=('state', 'created_at'), name='operations__state_b33956_idx')]


class Step(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    operation = models.OneToOneField(Operation, on_delete=models.PROTECT)
    name = models.CharField(max_length=32, default='node_eligibility')
    token = models.UUIDField(null=True)
    started_at = models.DateTimeField(null=True)
    dispatched_at = models.DateTimeField(null=True)
    lease_until = models.DateTimeField(null=True)
    quiescent_at = models.DateTimeField(null=True)

    class Meta:
        default_permissions = ()
        constraints = [models.CheckConstraint(condition=Q(name__in=('node_eligibility', 'image_build')), name='step_finite_handler')]


class Resource(models.Model):
    key = models.CharField(primary_key=True, max_length=80)

    class Meta:
        default_permissions = ()


class ResourceClaim(models.Model):
    resource = models.ForeignKey(Resource, on_delete=models.PROTECT)
    operation = models.ForeignKey(Operation, on_delete=models.PROTECT)
    step = models.ForeignKey(Step, on_delete=models.PROTECT)
    token = models.UUIDField()
    acquired_at = models.DateTimeField(default=timezone.now)
    released_at = models.DateTimeField(null=True)

    class Meta:
        default_permissions = ()
        constraints = [models.UniqueConstraint(fields=('resource',), condition=Q(released_at__isnull=True), name='one_unresolved_resource_claim')]


class EvidenceQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValueError('immutable operation evidence')

    def delete(self):
        raise ValueError('immutable operation evidence')


class Evidence(models.Model):
    objects = EvidenceQuerySet.as_manager()

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValueError('immutable operation evidence')
        kwargs['force_insert'] = True
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError('immutable operation evidence')

    class Meta:
        abstract = True
        default_permissions = ()


class EffectReceipt(Evidence):
    step = models.OneToOneField(Step, on_delete=models.PROTECT)
    token = models.UUIDField()
    epoch = models.UUIDField()
    node = models.ForeignKey('catalog.Node', on_delete=models.PROTECT)
    source_revision = models.PositiveBigIntegerField()
    decision_revision = models.PositiveBigIntegerField()
    applied = models.BooleanField()
    received_at = models.DateTimeField(default=timezone.now)


class AuditEvent(Evidence):
    operation = models.ForeignKey(Operation, on_delete=models.PROTECT)
    revision = models.PositiveBigIntegerField()
    target_revision = models.PositiveBigIntegerField()
    state = models.CharField(max_length=16)
    code = models.CharField(max_length=64)
    reason = models.CharField(max_length=512, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta(Evidence.Meta):
        constraints = [models.UniqueConstraint(fields=('operation', 'revision'), name='operation_audit_revision_unique')]


class OutboxEvent(models.Model):
    id = models.BigAutoField(primary_key=True)
    event_id = models.UUIDField(default=uuid.uuid4, unique=True)
    operation = models.ForeignKey(Operation, on_delete=models.PROTECT)
    revision = models.PositiveBigIntegerField()
    target_revision = models.PositiveBigIntegerField()
    state = models.CharField(max_length=16)
    code = models.CharField(max_length=64)
    created_at = models.DateTimeField(default=timezone.now)
    published_at = models.DateTimeField(null=True)

    class Meta:
        default_permissions = ()
        constraints = [models.UniqueConstraint(fields=('operation', 'revision'), name='operation_outbox_revision_unique')]
        indexes = [models.Index(fields=('published_at', 'id'), name='operations__publish_4d4573_idx')]
