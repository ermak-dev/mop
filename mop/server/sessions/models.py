"""Нормализованные fences/receipts; operation IDs не импортируют чужую ORM."""
from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone
from mop.server.catalog.models import Record, ImmutableRecord


class Assignment(Record):
    project = models.ForeignKey('catalog.Project', on_delete=models.PROTECT)
    node = models.ForeignKey('catalog.Node', on_delete=models.PROTECT)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT)
    generation = models.PositiveBigIntegerField(default=1)
    role = models.CharField(max_length=64)
    bundle_digest = models.CharField(max_length=64)
    grant_id = models.UUIDField()
    expires_at = models.DateTimeField()
    current_incarnation = models.ForeignKey('ExecutionIncarnation', null=True,
        on_delete=models.PROTECT, related_name='+')

    class Meta(Record.Meta):
        permissions = [('assign_work', 'Can assign human work')]
        constraints = Record.Meta.constraints + [models.CheckConstraint(
            condition=Q(generation__gte=1), name='assignment_generation_positive')]


class ExecutionIncarnation(Record):
    assignment = models.ForeignKey(Assignment, on_delete=models.PROTECT)
    node = models.ForeignKey('catalog.Node', on_delete=models.PROTECT)
    executor_epoch = models.UUIDField()
    accepting = models.BooleanField(default=True)
    replaced_by_epoch = models.UUIDField(null=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta(Record.Meta):
        constraints = Record.Meta.constraints + [models.CheckConstraint(
            condition=Q(accepting=False) | Q(replaced_by_epoch__isnull=True),
            name='incarnation_accepting_not_replaced')]


class AssignmentAttempt(Record):
    assignment = models.ForeignKey(Assignment, on_delete=models.PROTECT)
    incarnation = models.ForeignKey(ExecutionIncarnation, on_delete=models.PROTECT)
    principal_id = models.UUIDField()
    request_id = models.UUIDField()
    epoch = models.UUIDField()
    operation_id = models.UUIDField()
    step_id = models.UUIDField()
    executor_epoch = models.UUIDField()
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    generation = models.PositiveBigIntegerField()
    assignment_revision = models.PositiveBigIntegerField()
    role = models.CharField(max_length=64)
    bundle_digest = models.CharField(max_length=64)
    grant_id = models.UUIDField()
    action = models.CharField(max_length=32)
    not_after = models.DateTimeField()
    lease_until = models.DateTimeField()
    state = models.CharField(max_length=16, default='claimed')
    created_at = models.DateTimeField(default=timezone.now)

    class Meta(Record.Meta):
        permissions = [('poll_work', 'Can poll enrolled machine work'),
            ('claim_work', 'Can claim enrolled machine work'),
            ('report_result', 'Can report enrolled machine result'),
            ('read_facts', 'Can read enrolled machine facts'),
            ('reconcile_work', 'Can reconcile unknown machine work')]
        constraints = Record.Meta.constraints + [
            models.UniqueConstraint(fields=('principal_id', 'request_id'), name='attempt_request_identity'),
            models.UniqueConstraint(fields=('operation_id', 'step_id'), name='attempt_operation_step'),
            models.UniqueConstraint(fields=('assignment',), condition=Q(state__in=('claimed', 'unknown')),
                                    name='attempt_one_unresolved_resource'),
            models.CheckConstraint(condition=Q(generation__gte=1, assignment_revision__gte=1),
                                   name='attempt_admitted_counters'),
            models.CheckConstraint(condition=Q(state__in=('claimed', 'unknown', 'succeeded', 'failed')),
                                   name='attempt_known_state'),
            models.CheckConstraint(condition=Q(action__in=('restart_session', 'apply_identity', 'send_message', 'confirm_ready')),
                                   name='attempt_named_action'),
            models.CheckConstraint(condition=Q(lease_until__lte=models.F('not_after')),
                                   name='attempt_lease_within_deadline'),
        ]


class ResultReceipt(ImmutableRecord):
    attempt = models.ForeignKey(AssignmentAttempt, on_delete=models.PROTECT)
    expected_revision = models.PositiveBigIntegerField()
    resulting_revision = models.PositiveBigIntegerField()
    outcome = models.CharField(max_length=16)
    evidence = models.CharField(max_length=32)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta(Record.Meta):
        constraints = Record.Meta.constraints + [
            models.CheckConstraint(condition=Q(expected_revision__gte=1,
                resulting_revision=models.F('expected_revision') + 1), name='receipt_revision_successor'),
            models.CheckConstraint(condition=Q(outcome__in=('succeeded', 'failed', 'unknown')), name='receipt_known_outcome'),
        ]


class IdentityApplication(ImmutableRecord):
    attempt = models.OneToOneField(AssignmentAttempt, on_delete=models.PROTECT)
    receipt = models.OneToOneField(ResultReceipt, on_delete=models.PROTECT)


class ReadinessReceipt(ImmutableRecord):
    attempt = models.OneToOneField(AssignmentAttempt, on_delete=models.PROTECT)
    receipt = models.OneToOneField(ResultReceipt, on_delete=models.PROTECT)


class LandingToken(Record):
    project = models.OneToOneField('catalog.Project', on_delete=models.PROTECT)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT)
    generation = models.PositiveBigIntegerField(default=1)

    class Meta(Record.Meta):
        permissions = [('acquire_landing', 'Can acquire human landing token'),
                       ('release_landing', 'Can release own landing token')]
        constraints = Record.Meta.constraints + [models.CheckConstraint(
            condition=Q(generation__gte=1), name='landing_generation_positive')]
