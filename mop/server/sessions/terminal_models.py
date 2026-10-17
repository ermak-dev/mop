"""Приватный attach и control lease; не Assignment и не executor claim."""
import uuid
from django.db import models
from django.db.models import Q
from django.utils import timezone


class TerminalControl(models.Model):
    target = models.OneToOneField('execution.Assignment', primary_key=True, on_delete=models.PROTECT)
    generation = models.PositiveBigIntegerField(default=0)

    class Meta:
        app_label = 'execution'


class TerminalAttachment(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    target = models.ForeignKey('execution.Assignment', on_delete=models.PROTECT)
    incarnation = models.ForeignKey('execution.ExecutionIncarnation', on_delete=models.PROTECT)
    credential_id = models.UUIDField()
    user_id = models.PositiveBigIntegerField()
    mode = models.CharField(max_length=2)
    state = models.CharField(max_length=8, default='reserved')
    generation = models.PositiveBigIntegerField()
    gateway_id = models.UUIDField(null=True)
    connection_id = models.UUIDField(null=True)
    physical_instance_id = models.UUIDField(null=True)
    conversation_epoch = models.CharField(max_length=128, default='')
    terminal_epoch = models.CharField(max_length=128, default='')
    sequence = models.PositiveBigIntegerField(default=0)
    cols = models.PositiveIntegerField()
    rows = models.PositiveIntegerField()
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        app_label = 'execution'
        constraints = [
            models.CheckConstraint(condition=Q(mode__in=('ro','rw')),name='terminal_known_mode'),
            models.CheckConstraint(condition=Q(state__in=('reserved','active','closed','unknown')),name='terminal_known_state'),
            models.UniqueConstraint(fields=('target',),condition=Q(mode='rw',state__in=('reserved','active','unknown')),
                                    name='terminal_one_live_writer'),
            models.CheckConstraint(condition=Q(cols__gte=20,cols__lte=500,rows__gte=5,rows__lte=200),
                                    name='terminal_size_bounds')]
