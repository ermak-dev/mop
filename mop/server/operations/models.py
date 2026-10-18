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
    eligible = models.BooleanField(null=True)
    target_kind = models.CharField(max_length=16, default='node')
    target_id = models.UUIDField(null=True)
    incarnation_id = models.UUIDField(null=True)
    not_after = models.DateTimeField()
    state = models.CharField(max_length=16, default=State.ACCEPTED)
    revision = models.PositiveBigIntegerField(default=1)
    code = models.CharField(max_length=64, blank=True)
    reason = models.CharField(max_length=512, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    completed_at = models.DateTimeField(null=True)
    result_pruned = models.BooleanField(default=False)

    class Meta:
        permissions = [(code, 'Can '+code.replace('_', ' ')) for code in (
            'cancel_operation', 'recover_operation', 'view_audit', 'stop_puppet_job', 'override_assignment',
            'diagnose_pool', 'treat_pool', 'collect_pool_garbage', 'sweep_pool', 'view_node_disk')]
        constraints = [
            models.UniqueConstraint(fields=('actor', 'request_id'), name='operation_actor_request_unique'),
            models.CheckConstraint(condition=Q(revision__gte=1, expected_revision__gte=1), name='operation_positive_revisions'),
            models.CheckConstraint(condition=Q(state__in=tuple(State)), name='operation_finite_state'),
            models.CheckConstraint(condition=Q(action__in=('drain', 'enable', 'restart', 'build', 'stop_job', 'send', 'dispatch', 'wipe', 'delete', 'create', 'update')),
                name='operation_finite_action'),
            models.CheckConstraint(condition=(Q(target_kind='node', action__in=('drain','enable','build'), eligible__isnull=False,
                target_id__isnull=True, incarnation_id__isnull=True) | Q(target_kind='puppet',
                action__in=('restart', 'stop_job', 'send', 'dispatch', 'wipe', 'delete', 'create', 'update'),
                eligible__isnull=True, target_id__isnull=False, incarnation_id__isnull=False)), name='operation_typed_target'),
            models.CheckConstraint(condition=Q(result_pruned=False) | Q(completed_at__isnull=False), name='operation_pruned_terminal'),
        ]
        indexes = [models.Index(fields=('state', 'created_at'), name='operations__state_b33956_idx')]


class Step(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    operation = models.ForeignKey(Operation, on_delete=models.PROTECT)
    name = models.CharField(max_length=32, default='node_eligibility')
    ordinal = models.PositiveSmallIntegerField(default=1)
    attempt_id = models.UUIDField(null=True)
    outcome = models.CharField(max_length=16, blank=True)
    evidence = models.CharField(max_length=32, blank=True)
    token = models.UUIDField(null=True)
    started_at = models.DateTimeField(null=True)
    dispatched_at = models.DateTimeField(null=True)
    lease_until = models.DateTimeField(null=True)
    quiescent_at = models.DateTimeField(null=True)

    class Meta:
        default_permissions = ()
        constraints = [models.CheckConstraint(condition=Q(name__in=('node_eligibility', 'apply_identity',
            'restart_session', 'confirm_ready', 'image_build', 'scheduler_stop', 'send_message', 'wipe_clone',
            'scheduler_delete', 'scheduler_register', 'activate_grant', 'prepare_launch')),
            name='step_finite_handler'),
            models.UniqueConstraint(fields=('operation','ordinal'), name='operation_step_ordinal'),
            models.CheckConstraint(condition=Q(outcome__in=('', 'succeeded','failed','unknown')), name='step_finite_outcome')]


class RestartBinding(models.Model):
    """Снимок допуска конечного handler, не произвольный JSON workflow."""
    operation = models.OneToOneField(Operation, primary_key=True, on_delete=models.PROTECT)
    project = models.ForeignKey('catalog.Project', on_delete=models.PROTECT)
    executor_epoch = models.UUIDField()
    owner_id = models.PositiveBigIntegerField()
    generation = models.PositiveBigIntegerField()
    role = models.CharField(max_length=64)
    bundle_digest = models.CharField(max_length=64)
    grant_id = models.UUIDField()

    class Meta:
        default_permissions = ()


class ScheduledPuppet(models.Model):
    """Проверенный внешний job; только trusted enrollment, не HTTP payload."""
    target_id = models.UUIDField(primary_key=True)
    incarnation_id = models.UUIDField()
    epoch = models.UUIDField()
    restore_generation = models.UUIDField()
    scheduler_id = models.UUIDField()
    job_id = models.CharField(max_length=128)
    namespace = models.CharField(max_length=128)
    modify_index = models.PositiveBigIntegerField()

    class Meta:
        default_permissions = ()
        constraints = [models.UniqueConstraint(fields=('scheduler_id','namespace','job_id'), name='scheduled_job_identity_unique'),
            models.CheckConstraint(condition=Q(modify_index__gte=1), name='scheduled_job_positive_index')]


class ScheduledNode(models.Model):
    """Доверенная привязка узла к планировщику (#643): id узла в Nomad и
    заявленные capabilities запуска. Пишет только trusted enrollment Root,
    не HTTP payload; рендер спеки нового тела берёт размещение отсюда."""
    node = models.OneToOneField('catalog.Node', primary_key=True, on_delete=models.PROTECT)
    scheduler_id = models.UUIDField()
    nomad_node_id = models.UUIDField()
    capabilities = models.JSONField()
    enrolled_at = models.DateTimeField(default=timezone.now)

    class Meta:
        default_permissions = ()


class StopBinding(models.Model):
    operation = models.OneToOneField(Operation, primary_key=True, on_delete=models.PROTECT)
    applied_revision = models.PositiveBigIntegerField(null=True)
    project = models.ForeignKey('catalog.Project', on_delete=models.PROTECT)
    executor_epoch = models.UUIDField()
    owner_id = models.PositiveBigIntegerField()
    generation = models.PositiveBigIntegerField()
    role = models.CharField(max_length=64)
    bundle_digest = models.CharField(max_length=64)
    grant_id = models.UUIDField()
    scheduler_id = models.UUIDField()
    job_id = models.CharField(max_length=128)
    namespace = models.CharField(max_length=128)
    modify_index = models.PositiveBigIntegerField()

    class Meta:
        default_permissions = ()


class CreateBinding(models.Model):
    """Снимок допуска create (#643): одобренное определение и адрес будущего job.

    Узловые шаги идут по RestartBinding той же Operation; здесь -- то, что
    нужно серверным шагам регистрации и активации гранта."""
    operation = models.OneToOneField(Operation, primary_key=True, on_delete=models.PROTECT)
    definition_id = models.UUIDField()
    scheduler_id = models.UUIDField()
    job_id = models.CharField(max_length=128)
    namespace = models.CharField(max_length=128)
    applied_index = models.PositiveBigIntegerField(null=True)
    # #707: точная несекретная spec до dispatch; recovery не рендерит её заново.
    dispatched_job = models.JSONField(null=True)

    class Meta:
        default_permissions = ()


class MessageBinding(models.Model):
    """Ограниченный AEAD input общего Operation; не отдельная очередь доставки."""
    operation = models.OneToOneField(Operation, primary_key=True, on_delete=models.PROTECT)
    message_id = models.UUIDField(default=uuid.uuid4, unique=True)
    ciphertext = models.BinaryField()
    priority = models.CharField(max_length=8)
    wait_seconds = models.PositiveSmallIntegerField(default=0)
    notify_when_idle = models.BooleanField(default=False)
    holder_endpoint = models.UUIDField(null=True)
    holder_connection = models.UUIDField(null=True)

    class Meta:
        default_permissions = ()
        constraints = [
            models.CheckConstraint(condition=Q(priority__in=('now', 'next', 'later')), name='message_finite_priority'),
            models.CheckConstraint(condition=Q(wait_seconds__lte=60), name='message_bounded_wait'),
            models.CheckConstraint(condition=(Q(holder_endpoint__isnull=True, holder_connection__isnull=True)
                | Q(holder_endpoint__isnull=False, holder_connection__isnull=False)), name='message_complete_holder')]


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


class ReplacementBinding(Evidence):
    """Неизменный source/policy pin подготовки замены (#625).

    Destination -- Operation/RestartBinding того же журнала. Это ещё не
    admission физической замены: source остаётся current до отдельного proof.
    """
    operation = models.OneToOneField(Operation, primary_key=True, on_delete=models.PROTECT)
    source_incarnation_id = models.UUIDField()
    source_grant_id = models.UUIDField()
    source_revision = models.PositiveBigIntegerField()
    source_generation = models.PositiveBigIntegerField()
    source_expires_at = models.DateTimeField()
    expires_at = models.DateTimeField()
    definition_id = models.UUIDField()
    policy_approval_id = models.UUIDField()
    policy_version_id = models.UUIDField()
    policy_digest = models.CharField(max_length=64)

    class Meta(Evidence.Meta):
        constraints = [models.CheckConstraint(condition=Q(source_revision__gte=1, source_generation__gte=1)
            & Q(expires_at__lte=models.F('source_expires_at')), name='replacement_source_pins')]


class ReplacementPlan(Evidence):
    """Полный immutable план admitted update, без credential/частного transcript.

    Отдельно от seeded ReplacementBinding prerequisite: наличие одного source/
    policy pin ещё не доказывает admission. Продолжение -- opaque reference
    SQL-порта execution; HTTP его не выбирает и result его не раскрывает.
    """
    operation = models.OneToOneField(Operation, primary_key=True, on_delete=models.PROTECT)
    source_executor_epoch = models.UUIDField()
    owner_id = models.PositiveBigIntegerField()
    project_id = models.UUIDField()
    node_id = models.UUIDField()
    role = models.CharField(max_length=64)
    bundle_digest = models.CharField(max_length=64)
    destination_incarnation_id = models.UUIDField()
    destination_grant_id = models.UUIDField()
    destination_revision = models.PositiveBigIntegerField()
    destination_generation = models.PositiveBigIntegerField()
    scheduler_id = models.UUIDField()
    job_id = models.CharField(max_length=128)
    namespace = models.CharField(max_length=128)
    modify_index = models.PositiveBigIntegerField()
    origin = models.CharField(max_length=2048)
    source_ref = models.CharField(max_length=256)
    source_commit = models.CharField(max_length=64)
    clone_ref = models.UUIDField()
    continuation_ref = models.UUIDField(null=True)
    fresh = models.BooleanField()
    source_bootstrap_revision = models.UUIDField()
    source_bootstrap_digest = models.CharField(max_length=64)
    source_image_id = models.UUIDField()
    source_image_digest = models.CharField(max_length=64)
    bootstrap_revision = models.UUIDField()
    bootstrap_digest = models.CharField(max_length=64)
    image = models.ForeignKey('catalog.ImageRevision', on_delete=models.PROTECT)
    image_digest = models.CharField(max_length=64)

    class Meta(Evidence.Meta):
        constraints = [models.CheckConstraint(condition=Q(destination_revision__gte=1,
            destination_generation__gte=1, modify_index__gte=1), name='replacement_plan_counters'),
            models.CheckConstraint(condition=Q(fresh=True, continuation_ref__isnull=True)
                | Q(fresh=False, continuation_ref__isnull=False), name='replacement_exact_continuation')]


class CreateReservation(Evidence):
    """Неизменный intent и слот общего admission до credential I/O (#704/#709).

    После конверсии слот учитывается только по Operation. Пустой scope старой
    записи неизвестен, а не свободен: до recovery он занимает любой scope.
    Это не допуск к исполнению."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    request_id = models.UUIDField()
    digest = models.CharField(max_length=64)
    expires_at = models.DateTimeField()
    epoch = models.UUIDField(null=True)
    project = models.ForeignKey('catalog.Project', null=True, on_delete=models.PROTECT)

    class Meta(Evidence.Meta):
        constraints = [models.UniqueConstraint(fields=('actor', 'request_id'), name='create_reservation_actor_request')]


class CreateProvisioningAttempt(models.Model):
    """Незавершённый вызов держит слот reservation, не отдельную очередь.

    Только доказанный отказ закрывает попытку. Потеря ответа/процесса остаётся
    неизвестной; отказ параллельного retry не освобождает чужую попытку.
    Успешное завершение доказывает общий Operation, без второго lifecycle.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    reservation = models.ForeignKey(CreateReservation, on_delete=models.PROTECT)
    refused_at = models.DateTimeField(null=True)

    class Meta:
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
