"""Инфраструктурная identity БД, не предметный каталог или workflow."""
import uuid
from django.db import models
from django.utils import timezone


class RestoreGuard(models.Model):
    """Текущее обслуживание target, не часть восстанавливаемого архива."""
    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    generation = models.UUIDField(default=uuid.uuid4, editable=False)
    state = models.CharField(max_length=16, default='uninitialized', editable=False,
                             choices=[('uninitialized', 'Uninitialized'), ('ready', 'Ready'),
                                      ('restoring', 'Restoring')])

    class Meta:
        constraints = [
            models.CheckConstraint(condition=models.Q(id=1), name='controller_single_restore_guard'),
            models.CheckConstraint(condition=models.Q(state__in=['uninitialized', 'ready', 'restoring']),
                                   name='controller_restore_guard_state'),
        ]


class Installation(models.Model):
    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    identity = models.CharField(max_length=64, unique=True)
    root_digest = models.CharField(max_length=64, editable=False)
    epoch = models.UUIDField(default=uuid.uuid4, editable=False)
    effects_enabled = models.BooleanField(default=False, editable=False)
    initialized_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        permissions = [('manage_proxy', 'Can manage proxy'), ('view_models', 'Can view models')]
        constraints = [models.CheckConstraint(condition=models.Q(id=1), name='controller_single_installation')]
