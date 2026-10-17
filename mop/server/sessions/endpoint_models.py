"""Identity/presence не доказывают живой подписчик или принятие сообщения CLI."""
import uuid
from django.conf import settings
from django.db import models, transaction, router
from django.db.models import Q
from django.utils import timezone
from mop.server.catalog.models import Record, ImmutableRecord, ImmutableQuerySet


class ClientSession(Record):
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    client = models.ForeignKey('catalog.ClientRegistration', on_delete=models.PROTECT)
    external_session_id = models.CharField(max_length=256)
    display_name = models.CharField(max_length=128)

    class Meta(Record.Meta):
        constraints = Record.Meta.constraints + [models.UniqueConstraint(
            fields=('owner', 'client', 'external_session_id'), name='client_actual_external_identity')]


class Endpoint(Record):
    session = models.ForeignKey(ClientSession, on_delete=models.PROTECT)
    project = models.ForeignKey('catalog.Project', on_delete=models.PROTECT)
    address_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    expires_at = models.DateTimeField()
    retired = models.BooleanField(default=False)

    class Meta(Record.Meta):
        permissions = [('register_endpoint', 'Can register own human endpoint'),
                       ('view_delivery', 'Can inspect own private deliveries'),
                       ('deliver_report', 'Can deliver addressed private reports')]
        constraints = Record.Meta.constraints + [models.UniqueConstraint(
            fields=('session', 'project'), name='endpoint_session_project')]


class ConnectionLease(Record):
    endpoint = models.OneToOneField(Endpoint, on_delete=models.PROTECT)
    epoch = models.UUIDField()
    gateway_id = models.UUIDField()
    nonce = models.UUIDField(default=uuid.uuid4)
    generation = models.PositiveBigIntegerField(default=1)
    credential_id = models.UUIDField()
    user_id = models.PositiveBigIntegerField()
    expires_at = models.DateTimeField()
    connected = models.BooleanField(default=True)

    class Meta(Record.Meta):
        constraints = Record.Meta.constraints + [models.CheckConstraint(
            condition=Q(generation__gte=1), name='endpoint_connection_positive_generation')]


class MessageDelivery(Record):
    endpoint = models.ForeignKey(Endpoint, on_delete=models.PROTECT)
    epoch = models.UUIDField()
    message_id = models.UUIDField()
    operation_request_id = models.UUIDField(null=True)
    sender_user_id = models.PositiveBigIntegerField()
    sender_credential_id = models.UUIDField()
    sender_kind = models.CharField(max_length=16)
    sender_owner_id = models.PositiveBigIntegerField(null=True)
    sender_node_id = models.UUIDField(null=True)
    sender_grant_id = models.UUIDField(null=True)
    reply_endpoint = models.ForeignKey(Endpoint, null=True, on_delete=models.PROTECT, related_name='+')
    fingerprint = models.CharField(max_length=64)
    payload = models.BinaryField(null=True)
    payload_key_id = models.CharField(max_length=64, null=True)
    payload_nonce = models.BinaryField(null=True)
    priority = models.CharField(max_length=8)
    notify_when_idle = models.BooleanField(default=False)
    not_after = models.DateTimeField()
    state = models.CharField(max_length=16, default='queued')
    connection_generation = models.PositiveBigIntegerField(null=True)
    connection_nonce = models.UUIDField(null=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta(Record.Meta):
        constraints = Record.Meta.constraints + [
            models.UniqueConstraint(fields=('sender_user_id', 'message_id'), name='endpoint_message_identity'),
            models.UniqueConstraint(fields=('payload_key_id', 'payload_nonce'), name='endpoint_payload_nonce'),
            models.CheckConstraint(condition=Q(state__in=('queued', 'attempting', 'accepted', 'unknown', 'unavailable')),
                                   name='endpoint_delivery_known_state'),
            models.CheckConstraint(condition=Q(priority__in=('next', 'now')), name='endpoint_delivery_priority'),
            models.CheckConstraint(condition=Q(state__in=('queued', 'attempting'), payload__isnull=False,
                payload_key_id__isnull=False, payload_nonce__isnull=False) | Q(
                state__in=('accepted', 'unknown', 'unavailable'), payload__isnull=True,
                payload_key_id__isnull=True, payload_nonce__isnull=True), name='endpoint_payload_retention'),
        ]


class ReceiptQuerySet(ImmutableQuerySet):
    def bulk_create(self, *args, **kwargs):
        raise ValueError('delivery receipts require individual claim validation')


class DeliveryReceipt(ImmutableRecord):
    objects = ReceiptQuerySet.as_manager()

    delivery = models.ForeignKey(MessageDelivery, on_delete=models.PROTECT)
    kind = models.CharField(max_length=16)
    outcome = models.CharField(max_length=16)
    connection_generation = models.PositiveBigIntegerField()
    connection_nonce = models.UUIDField()
    created_at = models.DateTimeField(default=timezone.now)

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValueError('immutable delivery evidence')
        using = kwargs.get('using') or router.db_for_write(type(self), instance=self)
        with transaction.atomic(using=using):
            # Тот же row lock, что claim/result: normal ORM create не может
            # состязаться с переходом state и подтвердить чужое поколение.
            row = MessageDelivery.objects.using(using).select_for_update().get(pk=self.delivery_id)
            if (row.connection_generation is None or row.connection_nonce is None or
                    (self.connection_generation, self.connection_nonce) !=
                    (row.connection_generation, row.connection_nonce)):
                raise ValueError('delivery receipt does not match claimed receiver')
            if self.kind == 'delivery':
                valid = row.state == 'attempting' and self.outcome in ('accepted', 'unknown', 'unavailable')
            else:
                valid = (self.kind == 'idle' and self.outcome == 'observed' and
                    row.state == 'accepted' and row.notify_when_idle and
                    type(self).objects.using(using).filter(delivery_id=row.pk, kind='delivery', outcome='accepted',
                        connection_generation=self.connection_generation, connection_nonce=self.connection_nonce).exists())
            if not valid:
                raise ValueError('delivery receipt requires matching application evidence')
            return super().save(*args, **kwargs)

    class Meta(Record.Meta):
        constraints = [models.CheckConstraint(condition=Q(revision__gte=1),
            name='endpoint_deliveryreceipt_revision_positive'),
            models.UniqueConstraint(fields=('delivery', 'kind'), name='endpoint_receipt_kind_unique'),
            models.CheckConstraint(condition=Q(kind='delivery', outcome__in=('accepted', 'unknown', 'unavailable')) |
                Q(kind='idle', outcome='observed'), name='endpoint_receipt_known_kind_outcome'),
        ]
