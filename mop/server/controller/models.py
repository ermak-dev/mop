"""Инфраструктурная identity БД, не предметный каталог или workflow."""
import uuid
from django.db import models
from django.utils import timezone


class Installation(models.Model):
    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    identity = models.CharField(max_length=64, unique=True)
    root_digest = models.CharField(max_length=64, editable=False)
    epoch = models.UUIDField(default=uuid.uuid4, editable=False)
    effects_enabled = models.BooleanField(default=False, editable=False)
    initialized_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        constraints = [models.CheckConstraint(condition=models.Q(id=1), name='controller_single_installation')]
