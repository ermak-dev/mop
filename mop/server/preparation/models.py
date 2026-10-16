"""Схема Preparation. Первичные bytes — защищённый SecretVersion, не путь."""
import uuid
from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone


class Record(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    class Meta:
        abstract = True
        default_permissions = ()


class BootstrapRevision(Record):
    project = models.ForeignKey('catalog.Project', on_delete=models.PROTECT)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    number = models.PositiveBigIntegerField()
    content_version = models.OneToOneField('secrets.SecretVersion', on_delete=models.PROTECT)
    digest = models.CharField(max_length=64)
    provenance = models.CharField(max_length=1024)
    commit = models.CharField(max_length=64)
    task_size = models.PositiveIntegerField()
    vars_size = models.PositiveIntegerField()
    created_at = models.DateTimeField(default=timezone.now)

    class Meta(Record.Meta):
        constraints = [models.UniqueConstraint(fields=('project', 'owner', 'number'), name='prep_revision_number'),
            models.CheckConstraint(condition=Q(number__gte=1, task_size__lte=262144, vars_size__lte=131072), name='prep_revision_sizes')]
        permissions = [('approve_bootstrap', 'Can approve exact bootstrap bytes')]


class BootstrapSecretReference(Record):
    revision = models.ForeignKey(BootstrapRevision, on_delete=models.PROTECT)
    version = models.ForeignKey('secrets.SecretVersion', on_delete=models.PROTECT)

    class Meta(Record.Meta):
        constraints = [models.UniqueConstraint(fields=('revision', 'version'), name='prep_secret_reference')]


class BootstrapApproval(Record):
    revision = models.OneToOneField(BootstrapRevision, on_delete=models.PROTECT)
    human = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(default=timezone.now)


class ApprovalRevocation(Record):
    approval = models.OneToOneField(BootstrapApproval, on_delete=models.PROTECT)
    created_at = models.DateTimeField(default=timezone.now)


class BootstrapRun(Record):
    approval = models.ForeignKey(BootstrapApproval, on_delete=models.PROTECT)
    target_id = models.UUIDField()
    incarnation = models.UUIDField()
    request_id = models.UUIDField()
    version = models.PositiveBigIntegerField(default=1)
    state = models.CharField(max_length=16, default='accepted')
    failures = models.PositiveIntegerField(default=0)
    failure_limit = models.PositiveIntegerField(default=3)
    deadline = models.DateTimeField()
    started_at = models.DateTimeField(null=True)
    finished_at = models.DateTimeField(null=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta(Record.Meta):
        constraints = [models.UniqueConstraint(fields=('target_id', 'incarnation', 'request_id'), name='prep_run_request'),
            models.CheckConstraint(condition=Q(version__gte=1, failures__lte=10, failure_limit__gte=1, failure_limit__lte=10), name='prep_run_counters'),
            models.CheckConstraint(condition=Q(state__in=('accepted', 'running', 'succeeded', 'failed', 'refused', 'unknown', 'gave_up')), name='prep_run_state')]


class BootstrapStep(Record):
    run = models.ForeignKey(BootstrapRun, on_delete=models.PROTECT, related_name='steps')
    ordinal = models.PositiveIntegerField()
    name = models.CharField(max_length=64)
    state = models.CharField(max_length=16, default='pending')
    attempt = models.PositiveIntegerField(default=0)
    failures = models.PositiveIntegerField(default=0)
    log = models.TextField(blank=True)
    log_bytes = models.PositiveIntegerField(default=0)
    progress = models.PositiveIntegerField(default=0)
    started_at = models.DateTimeField(null=True)
    finished_at = models.DateTimeField(null=True)

    class Meta(Record.Meta):
        constraints = [models.UniqueConstraint(fields=('run', 'ordinal'), name='prep_step_ordinal'),
            models.CheckConstraint(condition=Q(ordinal__lt=64, attempt__lte=10, failures__lte=10, log_bytes__lte=4096, progress__lte=100), name='prep_step_bounds'),
            models.CheckConstraint(condition=Q(state__in=('pending', 'running', 'succeeded', 'failed', 'refused', 'unknown')), name='prep_step_state')]
