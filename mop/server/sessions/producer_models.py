"""SQL currentSession и never-reuse ledger producer; никаких ключей/native файлов."""
from django.db import models
from django.db.models import Q
from django.utils import timezone

from mop.server.catalog.models import ImmutableRecord


class SessionProducer(ImmutableRecord):
    assignment = models.ForeignKey('execution.Assignment', on_delete=models.PROTECT)
    incarnation = models.ForeignKey('execution.ExecutionIncarnation', on_delete=models.PROTECT)
    node = models.ForeignKey('catalog.Node', on_delete=models.PROTECT)
    epoch = models.UUIDField()
    executor_epoch = models.UUIDField()
    # Глобальная одноразовая identity: смена инкарнации тоже не усыновляет её.
    producer_epoch = models.UUIDField(unique=True)
    session_id = models.CharField(max_length=128)
    executor_user_id = models.PositiveBigIntegerField()
    owner_id = models.PositiveBigIntegerField()
    generation = models.PositiveBigIntegerField()
    assignment_revision = models.PositiveBigIntegerField()
    role = models.CharField(max_length=64)
    bundle_digest = models.CharField(max_length=64)
    grant_id = models.UUIDField()
    operation_id = models.UUIDField()
    step_id = models.UUIDField()
    attempt_id = models.UUIDField()
    # Только digest конечного несекретного отчёта/запроса, не credential.
    proof_digest = models.CharField(max_length=64)
    request_digest = models.CharField(max_length=64)
    accepted_revision = models.PositiveBigIntegerField()
    pane = models.CharField(max_length=11)
    pid = models.PositiveIntegerField()
    process_start = models.CharField(max_length=56)
    registered_at = models.DateTimeField(default=timezone.now)

    class Meta(ImmutableRecord.Meta):
        default_permissions = ()
        constraints = ImmutableRecord.Meta.constraints + [
            models.UniqueConstraint(fields=('incarnation', 'accepted_revision'), name='producer_incarnation_revision'),
            models.CheckConstraint(condition=Q(accepted_revision__gte=1, generation__gte=1,
                assignment_revision__gte=1, pid__gte=1), name='producer_positive_counters')]


class CurrentProducer(models.Model):
    # CAS сериализуется Assignment lock; отдельный pointer на каждую инкарнацию
    # позволяет следующему запуску начать с null/0 без caller-selected истории.
    incarnation = models.OneToOneField('execution.ExecutionIncarnation', primary_key=True, on_delete=models.PROTECT)
    producer = models.OneToOneField(SessionProducer, on_delete=models.PROTECT)
    revision = models.PositiveBigIntegerField(default=1)

    class Meta:
        default_permissions = ()
        permissions = [('register_producer', 'Can register own admitted session producer')]
        constraints = [models.CheckConstraint(condition=Q(revision__gte=1), name='current_producer_revision_positive')]
