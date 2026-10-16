"""Общая петлевая полоса и конечные состояния ключей, без secret bytes."""
from django.db import models
from django.db.models import Q


class KeyState(models.Model):
    fingerprint = models.CharField(primary_key=True, max_length=64, editable=False)
    state = models.CharField(max_length=16, default='unproven', editable=False)
    version_id = models.UUIDField(editable=False)
    revision = models.PositiveBigIntegerField(default=1, editable=False)

    class Meta:
        default_permissions = ()
        constraints = [models.CheckConstraint(condition=Q(state__in=('unproven', 'proven', 'rejected')),
            name='gateway_key_state')]


class Circuit(models.Model):
    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    revision = models.PositiveBigIntegerField(default=1, editable=False)
    claim_id = models.UUIDField(null=True, editable=False)
    claim_fingerprint = models.CharField(max_length=64, blank=True, editable=False)
    version_id = models.UUIDField(null=True, editable=False)
    deadline = models.DateTimeField(null=True, editable=False)
    cooldown_until = models.DateTimeField(null=True, editable=False)
    state = models.CharField(max_length=16, default='idle', editable=False)
    write = models.BooleanField(default=False, editable=False)

    class Meta:
        default_permissions = ()
        permissions = (('manage_proxy', 'Manage proxy management gateway'),)
        constraints = [models.CheckConstraint(condition=Q(id=1), name='gateway_single_circuit'),
            models.CheckConstraint(condition=Q(state__in=('idle', 'running', 'unknown')), name='gateway_circuit_state'),
            models.CheckConstraint(condition=(Q(state='idle', claim_id__isnull=True, deadline__isnull=True)
                | Q(state__in=('running', 'unknown'), claim_id__isnull=False, deadline__isnull=False)),
                name='gateway_claim_lifecycle')]
