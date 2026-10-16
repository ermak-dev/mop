"""Нормализованное хранилище: закрытые таблицы, не публичные serializers.

Cross-table инварианты проверяет владелец команды под блокировкой родителя;
FK/unique/check защищают структурные факты. ORM guards исключают случайный
bulk обход immutable refs, но не являются границей против злонамеренного DBA.
"""
import re
import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone

from .crypto import Envelope, MAX_PLAINTEXT_BYTES
from .rotation import Stage, Effect, FailureReason, Outcome


KINDS = ('variable', 'file', 'credential')
PURPOSES = ('project', 'node', 'bootstrap_ssh', 'nomad', 'signer', 'proxy',
            'nats', 'ldap_bind', 'tls', 'grant', 'git', 'policy', 'operation')


def validate_name(name, kind):
    if type(name) is not str or not 1 <= len(name) <= 256:
        raise ValueError('invalid secret name')
    if kind == 'variable':
        valid = re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', name)
    elif kind == 'file':
        valid = (not name.startswith('/') and '\\' not in name
                 and all(part not in ('', '.', '..') for part in name.split('/'))
                 and all(32 < ord(c) < 127 for c in name))
    else:
        valid = kind == 'credential' and re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]*', name)
    if not valid:
        raise ValueError('invalid secret name')


class GuardedQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValueError('use secret lifecycle commands')

    def bulk_update(self, *args, **kwargs):
        raise ValueError('use secret lifecycle commands')

    def bulk_create(self, *args, **kwargs):
        raise ValueError('use secret lifecycle commands')

    def delete(self):
        from django.db.models.deletion import ProtectedError
        raise ProtectedError('secret evidence requires explicit retention review', ())


class Record(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    objects = GuardedQuerySet.as_manager()

    def delete(self, *args, **kwargs):
        from django.db.models.deletion import ProtectedError
        raise ProtectedError('secret evidence requires explicit retention review', (self,))

    class Meta:
        abstract = True
        default_permissions = ()


class Immutable(Record):
    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValueError('immutable secret evidence')
        self.clean()
        kwargs['force_insert'] = True
        return super().save(*args, **kwargs)

    class Meta(Record.Meta):
        abstract = True


class Secret(Record):
    installation = models.ForeignKey('controller.Installation', on_delete=models.PROTECT)
    project = models.ForeignKey('catalog.Project', null=True, on_delete=models.PROTECT)
    kind = models.CharField(max_length=16)
    name = models.CharField(max_length=256)
    purpose = models.CharField(max_length=32)
    revision = models.PositiveBigIntegerField(default=1)
    last_number = models.PositiveBigIntegerField(default=0)
    active_version = models.ForeignKey('SecretVersion', null=True, on_delete=models.PROTECT, related_name='+')
    revoked = models.BooleanField(default=False)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT)
    created_at = models.DateTimeField(default=timezone.now)

    def clean(self):
        validate_name(self.name, self.kind)
        if self.purpose not in PURPOSES:
            raise ValueError('invalid secret purpose')
        if self.active_version_id and self.active_version.secret_id != self.pk:
            raise ValueError('active version belongs to another secret')
        previous = type(self)._base_manager.filter(pk=self.pk).first()
        if previous and any(getattr(previous, field) != getattr(self, field) for field in
                            ('installation_id', 'project_id', 'name', 'kind', 'purpose')):
            raise ValueError('secret identity is immutable')

    def save(self, *args, **kwargs):
        self.clean()
        return super().save(*args, **kwargs)

    class Meta(Record.Meta):
        permissions = (('view_secret_metadata', 'View secret metadata'),
                       ('manage_project_secret', 'Manage project secrets'),
                       ('deliver_secret', 'Deliver addressed secrets'),
                       ('rotate_proxy_key', 'Rotate proxy credential'))
        constraints = [
            models.CheckConstraint(condition=Q(revision__gte=1), name='secret_revision_positive'),
            models.CheckConstraint(condition=Q(kind__in=KINDS), name='secret_known_kind'),
            models.CheckConstraint(condition=Q(purpose__in=PURPOSES), name='secret_known_purpose'),
            models.UniqueConstraint(fields=('project', 'kind', 'name'), condition=Q(project__isnull=False), name='secret_project_name'),
            models.UniqueConstraint(fields=('installation', 'kind', 'name'), condition=Q(project__isnull=True), name='secret_installation_name'),
        ]


class SecretVersion(Immutable):
    secret = models.ForeignKey(Secret, on_delete=models.PROTECT)
    number = models.PositiveBigIntegerField()
    envelope = models.BinaryField(editable=False)
    key_id = models.CharField(max_length=64, editable=False)
    nonce = models.BinaryField(editable=False)
    size = models.PositiveIntegerField()
    created_at = models.DateTimeField(default=timezone.now)

    def clean(self):
        envelope = Envelope.from_bytes(bytes(self.envelope))
        if envelope.key_id != self.key_id or envelope.nonce != bytes(self.nonce):
            raise ValueError('secret envelope metadata mismatch')
        if len(envelope.ciphertext) != self.size + 16:
            raise ValueError('secret envelope size mismatch')

    class Meta(Record.Meta):
        constraints = [
            models.UniqueConstraint(fields=('secret', 'number'), name='secret_version_number'),
            models.UniqueConstraint(fields=('key_id', 'nonce'), name='secret_key_nonce'),
            models.CheckConstraint(condition=Q(number__gte=1), name='secret_version_positive'),
            models.CheckConstraint(condition=Q(size__lte=MAX_PLAINTEXT_BYTES), name='secret_size_bound'),
        ]


class SecretBinding(Record):
    secret = models.ForeignKey(Secret, on_delete=models.PROTECT)
    node = models.ForeignKey('catalog.Node', null=True, on_delete=models.PROTECT)
    client = models.ForeignKey('catalog.ClientRegistration', null=True, on_delete=models.PROTECT)
    controller = models.BooleanField(default=False)
    role = models.CharField(max_length=128)
    digest = models.CharField(max_length=64)
    incarnation = models.UUIDField()
    grant_id = models.UUIDField()
    assignment_id = models.UUIDField()
    created_at = models.DateTimeField(default=timezone.now)
    retired_at = models.DateTimeField(null=True, editable=False)

    def save(self, *args, **kwargs):
        # Membership меняется только под тем же parent lock, что и frozen set.
        # Identity/evidence не меняются; единственный переход — current -> retired.
        from django.db import transaction
        with transaction.atomic():
            Secret.objects.select_for_update().get(pk=self.secret_id)
            previous = type(self)._base_manager.filter(pk=self.pk).first()
            if previous:
                if any(getattr(previous, f.attname) != getattr(self, f.attname)
                       for f in self._meta.concrete_fields if f.name != 'retired_at'):
                    raise ValueError('secret binding identity is immutable')
                if previous.retired_at is not None or self.retired_at is None:
                    raise ValueError('secret binding retirement is immutable')
            elif self.retired_at is not None:
                raise ValueError('new secret binding must be current')
            if CredentialRotation.objects.filter(secret_id=self.secret_id, resolved=False).exists():
                raise ValueError('secret binding membership is frozen by an unresolved rotation')
            self.clean()
            return super().save(*args, **kwargs)

    def clean(self):
        if not re.fullmatch(r'[A-Za-z0-9_.-]{1,128}', self.role) or not re.fullmatch(r'[0-9a-f]{64}', self.digest):
            raise ValueError('invalid addressed role binding')
        if sum((self.node_id is not None, self.client_id is not None, self.controller)) != 1:
            raise ValueError('exactly one secret recipient is required')
        if self.client_id and self.client.installation_id != self.secret.installation_id:
            raise ValueError('recipient belongs to another installation')
        if self.secret.purpose == 'nomad' and not self.controller:
            raise ValueError('scheduler credential is controller-only')

    class Meta(Record.Meta):
        constraints = [models.CheckConstraint(condition=(
            Q(node__isnull=False, client__isnull=True, controller=False)
            | Q(node__isnull=True, client__isnull=False, controller=False)
            | Q(node__isnull=True, client__isnull=True, controller=True)), name='secret_binding_one_recipient'),
            models.UniqueConstraint(fields=('secret', 'node', 'role'),
                condition=Q(retired_at__isnull=True, node__isnull=False), name='secret_current_node_role'),
            models.UniqueConstraint(fields=('secret', 'client', 'role'),
                condition=Q(retired_at__isnull=True, client__isnull=False), name='secret_current_client_role'),
            models.UniqueConstraint(fields=('secret', 'role'),
                condition=Q(retired_at__isnull=True, controller=True), name='secret_current_controller_role')]


def enum_field(enum, *, null=False, maximum=32):
    return models.CharField(max_length=maximum, choices=[(x.value, x.value) for x in enum], null=null)


def rotation_enum_constraints():
    result = []
    for field, enum in (('pending', Effect), ('retryable', Effect),
                        ('original_failure', Effect), ('compensation_failure', Effect),
                        ('original_reason', FailureReason), ('compensation_reason', FailureReason)):
        result.append(models.CheckConstraint(condition=(Q(**{field + '__isnull': True})
            | Q(**{field + '__in': tuple(item.value for item in enum)})), name='rotation_' + field + '_enum'))
    for prefix in ('original', 'compensation'):
        result.append(models.CheckConstraint(condition=(
            Q(**{prefix + '_failure__isnull': True, prefix + '_reason__isnull': True})
            | Q(**{prefix + '_failure__isnull': False, prefix + '_reason__isnull': False})),
            name='rotation_' + prefix + '_reason_pair'))
    return result


class CredentialRotation(Record):
    secret = models.ForeignKey(Secret, on_delete=models.PROTECT)
    old_version = models.ForeignKey(SecretVersion, on_delete=models.PROTECT, related_name='+')
    new_version = models.ForeignKey(SecretVersion, on_delete=models.PROTECT, related_name='+')
    secret_revision = models.PositiveBigIntegerField()
    revision = models.PositiveBigIntegerField(default=0)
    state = enum_field(Stage)
    pending = enum_field(Effect, null=True)
    retryable = enum_field(Effect, null=True)
    original_failure = enum_field(Effect, null=True)
    compensation_failure = enum_field(Effect, null=True)
    original_reason = enum_field(FailureReason, null=True)
    compensation_reason = enum_field(FailureReason, null=True)
    claim_id = models.UUIDField(null=True)
    generation = models.PositiveBigIntegerField(default=0)
    delivery_generation = models.PositiveBigIntegerField(default=0)
    resolved = models.BooleanField(default=False)
    created_at = models.DateTimeField(default=timezone.now)

    def clean(self):
        if (self.old_version.secret_id != self.secret_id or self.new_version.secret_id != self.secret_id
                or self.old_version_id == self.new_version_id):
            raise ValueError('rotation versions must belong to its secret')
        old = type(self)._base_manager.filter(pk=self.pk).first()
        if old and any(getattr(old, f) != getattr(self, f) for f in ('secret_id', 'old_version_id', 'new_version_id')):
            raise ValueError('rotation references are immutable')

    def save(self, *args, **kwargs):
        self.clean()
        return super().save(*args, **kwargs)

    class Meta(Record.Meta):
        constraints = [
            models.UniqueConstraint(fields=('secret',), condition=Q(resolved=False), name='secret_one_unresolved_rotation'),
            models.CheckConstraint(condition=Q(secret_revision__gte=1), name='rotation_secret_revision'),
            models.CheckConstraint(condition=Q(state__in=tuple(s.value for s in Stage)), name='rotation_known_state'),
            models.CheckConstraint(condition=~Q(old_version=models.F('new_version')), name='rotation_distinct_versions'),
            models.CheckConstraint(condition=Q(resolved=False) | Q(state__in=('retired', 'compensated')), name='rotation_resolution_terminal'),
        ] + rotation_enum_constraints()


class RotationRecipient(Immutable):
    rotation = models.ForeignKey(CredentialRotation, on_delete=models.PROTECT)
    binding = models.ForeignKey(SecretBinding, on_delete=models.PROTECT)

    def clean(self):
        if self.rotation.secret_id != self.binding.secret_id:
            raise ValueError('rotation recipient belongs to another secret')

    class Meta(Record.Meta):
        constraints = [models.UniqueConstraint(fields=('rotation', 'binding'), name='rotation_recipient_unique')]


class DeliveryReceipt(Immutable):
    recipient = models.ForeignKey(RotationRecipient, on_delete=models.PROTECT)
    version = models.ForeignKey(SecretVersion, on_delete=models.PROTECT)
    claim_id = models.UUIDField()
    generation = models.PositiveBigIntegerField()
    outcome = enum_field(Outcome)
    created_at = models.DateTimeField(default=timezone.now)

    def clean(self):
        if self.version_id not in (self.recipient.rotation.old_version_id, self.recipient.rotation.new_version_id):
            raise ValueError('receipt version does not match rotation')

    class Meta(Record.Meta):
        constraints = [
            models.UniqueConstraint(fields=('recipient', 'generation'), name='delivery_receipt_identity'),
            models.CheckConstraint(condition=Q(generation__gte=1), name='receipt_generation_positive'),
            models.CheckConstraint(condition=Q(outcome__in=tuple(s.value for s in Outcome)), name='receipt_known_outcome'),
        ]


class SecretAudit(Immutable):
    secret = models.ForeignKey(Secret, on_delete=models.PROTECT)
    rotation = models.ForeignKey(CredentialRotation, null=True, on_delete=models.PROTECT)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT)
    action = models.CharField(max_length=24)
    revision = models.PositiveBigIntegerField()
    created_at = models.DateTimeField(default=timezone.now)

    class Meta(Record.Meta):
        constraints = [models.CheckConstraint(condition=Q(action__in=(
            'create', 'stage', 'update', 'bind', 'retire_binding', 'revoke', 'rotation_stage', 'rotation_begin',
            'rotation_record', 'rotation_reconcile', 'rotation_activate', 'receipt')), name='secret_audit_action')]
