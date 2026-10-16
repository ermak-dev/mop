"""Build metadata связаны с общей Operation/Step, не образуют новую очередь."""
import uuid
from django.db import models
from django.db.models import Q
from django.utils import timezone
from ..operations.models import Evidence


_RUN_MUTABLE = frozenset({'planned_at','skipped','original_failure'})


class BuildRunQuerySet(models.QuerySet):
    def update(self, **kwargs):
        if not set(kwargs) <= _RUN_MUTABLE:
            raise ValueError('immutable pinned build inputs')
        return super().update(**kwargs)


class BuildRun(models.Model):
    objects = BuildRunQuerySet.as_manager()

    def save(self, *args, **kwargs):
        if not self._state.adding and (kwargs.get('update_fields') is None or not set(kwargs['update_fields']) <= _RUN_MUTABLE):
            raise ValueError('immutable pinned build inputs')
        return super().save(*args, **kwargs)

    operation = models.OneToOneField('operations.Operation', primary_key=True, on_delete=models.PROTECT)
    policy = models.ForeignKey('catalog.ProjectPolicySnapshot', on_delete=models.PROTECT)
    project = models.ForeignKey('catalog.Project', on_delete=models.PROTECT)
    origin = models.CharField(max_length=2048)
    project_name = models.CharField(max_length=128)
    manifest = models.BinaryField()
    manifest_digest = models.CharField(max_length=64)
    source_commit = models.CharField(max_length=64)
    source_ref = models.CharField(max_length=256)
    source_digest = models.CharField(max_length=64)
    mode = models.CharField(max_length=8)
    force_safety = models.BooleanField()
    planned_at = models.DateTimeField(null=True)
    skipped = models.BooleanField(default=False)
    original_failure = models.CharField(max_length=512, blank=True)

    class Meta:
        default_permissions = ()
        permissions = [('force_build', 'Can force image build safety')]
        constraints = [models.CheckConstraint(condition=Q(mode__in=('missing', 'update', 'rebuild')), name='build_finite_mode')]


class BuildNodePlan(Evidence):
    build = models.ForeignKey(BuildRun, on_delete=models.PROTECT)
    node = models.ForeignKey('catalog.Node', on_delete=models.PROTECT)
    source_revision = models.PositiveBigIntegerField()
    driver = models.CharField(max_length=16)

    class Meta(Evidence.Meta):
        constraints = [models.UniqueConstraint(fields=('build', 'node'), name='build_unique_node')]


class AffectedTargetPlan(Evidence):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    build = models.ForeignKey(BuildRun, on_delete=models.PROTECT)
    assignment = models.ForeignKey('execution.Assignment', on_delete=models.PROTECT)
    incarnation = models.ForeignKey('execution.ExecutionIncarnation', on_delete=models.PROTECT)
    node = models.ForeignKey('catalog.Node', on_delete=models.PROTECT)
    source_revision = models.PositiveBigIntegerField()
    generation = models.PositiveBigIntegerField()
    role = models.CharField(max_length=64)
    bundle_digest = models.CharField(max_length=64)
    grant_id = models.UUIDField()
    source_spec = models.BinaryField()
    spec_digest = models.CharField(max_length=64)

    class Meta(Evidence.Meta):
        constraints = [models.UniqueConstraint(fields=('build', 'assignment'), name='build_unique_target')]


class StageIntent(Evidence):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    build = models.ForeignKey(BuildRun, on_delete=models.PROTECT)
    step = models.ForeignKey('operations.Step', on_delete=models.PROTECT)
    token = models.UUIDField()
    key = models.CharField(max_length=80)
    stage = models.CharField(max_length=16)
    target = models.ForeignKey(AffectedTargetPlan, null=True, on_delete=models.PROTECT)
    node = models.ForeignKey('catalog.Node', null=True, on_delete=models.PROTECT)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta(Evidence.Meta):
        constraints = [models.UniqueConstraint(fields=('build', 'key'), name='build_stage_key'),
            models.CheckConstraint(condition=Q(stage__in=('clear', 'wipe', 'build', 'restore', 'announce')), name='build_finite_stage'),
            models.CheckConstraint(condition=Q(stage__in=('clear', 'wipe', 'restore'), target__isnull=False, node__isnull=False)
                | Q(stage='build', target__isnull=True, node__isnull=True)
                | Q(stage='announce', target__isnull=True, node__isnull=False), name='build_stage_shape')]


class ExecutorDispatch(Evidence):
    intent = models.OneToOneField(StageIntent, on_delete=models.PROTECT)
    created_at = models.DateTimeField(default=timezone.now)


class AvailabilityReceipt(Evidence):
    build = models.ForeignKey(BuildRun, on_delete=models.PROTECT)
    node = models.ForeignKey('catalog.Node', on_delete=models.PROTECT)
    source_digest = models.CharField(max_length=64)
    external_reference = models.CharField(max_length=512)
    observed_at = models.DateTimeField()
    received_at = models.DateTimeField(default=timezone.now)

    class Meta(Evidence.Meta):
        constraints = [models.UniqueConstraint(fields=('build', 'node'), name='build_availability_node')]


class StageReceipt(Evidence):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    intent = models.ForeignKey(StageIntent, on_delete=models.PROTECT)
    outcome = models.CharField(max_length=16)
    quiescent = models.BooleanField()
    resolved = models.BooleanField(default=False)
    reason = models.CharField(max_length=512)
    received_at = models.DateTimeField(default=timezone.now)

    class Meta(Evidence.Meta):
        constraints = [models.UniqueConstraint(fields=('intent', 'resolved'), name='build_receipt_resolution'),
            models.CheckConstraint(condition=Q(resolved=False, outcome='unknown') | Q(resolved=True, outcome__in=('succeeded','failed'), quiescent=True), name='build_receipt_resolved_fact'),
            models.CheckConstraint(condition=Q(outcome='unknown') | Q(outcome__in=('succeeded', 'failed'), quiescent=True), name='build_receipt_outcome')]
