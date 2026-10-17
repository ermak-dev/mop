"""ORM-owned наблюдения и приватный cursor, не общий audit/outbox."""
from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone
from mop.server.catalog.models import Record


class SessionObservation(Record):
    assignment = models.ForeignKey('execution.Assignment', on_delete=models.CASCADE)
    incarnation = models.ForeignKey('execution.ExecutionIncarnation', on_delete=models.CASCADE)
    node = models.ForeignKey('catalog.Node', on_delete=models.PROTECT)
    epoch = models.UUIDField()
    executor_epoch = models.UUIDField()
    producer_epoch = models.UUIDField()
    session_id = models.CharField(max_length=128)
    sequence = models.PositiveBigIntegerField(default=0)
    event = models.CharField(max_length=32, default='')
    turn_source = models.ForeignKey('execution.TurnOutcome', null=True, related_name='+', on_delete=models.SET_NULL)
    incomplete = models.BooleanField(default=False)
    gap_count = models.PositiveBigIntegerField(default=0)
    confirmed_model = models.CharField(max_length=128, null=True)
    confirmed_sequence = models.PositiveBigIntegerField(default=0)
    model_hint = models.CharField(max_length=128, null=True)
    model_proven = models.BooleanField(default=False)
    model_received_at = models.DateTimeField(null=True)
    received_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta(Record.Meta):
        app_label = 'execution'
        permissions = [('ingest_telemetry', 'Can ingest enrolled node telemetry'),
                       ('read_history', 'Can read private session history')]
        constraints = Record.Meta.constraints + [models.UniqueConstraint(
            fields=('incarnation', 'producer_epoch'), name='telemetry_producer_identity')]


class TelemetryReceipt(models.Model):
    observation = models.ForeignKey(SessionObservation, on_delete=models.CASCADE)
    sequence = models.PositiveBigIntegerField()
    digest = models.CharField(max_length=64)
    received_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        app_label = 'execution'
        constraints = [models.UniqueConstraint(fields=('observation', 'sequence'), name='telemetry_sequence_identity'),
            models.CheckConstraint(condition=Q(sequence__gte=1), name='telemetry_receipt_sequence_positive')]


class TurnOutcome(models.Model):
    observation = models.ForeignKey(SessionObservation, on_delete=models.CASCADE)
    sequence = models.PositiveBigIntegerField()
    turn_id = models.UUIDField(null=True)
    event = models.CharField(max_length=32)
    error = models.CharField(max_length=64, null=True)
    initiator_user_id = models.PositiveBigIntegerField(null=True)
    initiator_endpoint_id = models.UUIDField(null=True)
    initiator_session_id = models.UUIDField(null=True)
    operation_id = models.UUIDField(null=True)
    message_id = models.UUIDField(null=True)
    received_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        app_label = 'execution'
        constraints = [models.UniqueConstraint(fields=('observation', 'sequence'), name='turn_sequence_identity'),
            models.CheckConstraint(condition=Q(sequence__gte=1), name='turn_sequence_positive')]


class ModelObservation(models.Model):
    observation = models.ForeignKey(SessionObservation, on_delete=models.CASCADE)
    sequence = models.PositiveBigIntegerField()
    model = models.CharField(max_length=128)
    kind = models.CharField(max_length=32)
    causal_switch = models.PositiveBigIntegerField(null=True)
    received_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        app_label = 'execution'
        constraints = [models.UniqueConstraint(fields=('observation', 'sequence'), name='model_sequence_identity'),
            models.CheckConstraint(condition=Q(sequence__gte=1), name='model_sequence_positive')]


class PrivateHistoryCursor(Record):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    observation = models.ForeignKey(SessionObservation, on_delete=models.CASCADE)
    cursor = models.CharField(max_length=8192, default='')
    source_epoch = models.CharField(max_length=128, default='')
    updated_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta(Record.Meta):
        app_label = 'execution'
        constraints = Record.Meta.constraints + [models.UniqueConstraint(
            fields=('user', 'observation'), name='history_private_cursor_scope')]
