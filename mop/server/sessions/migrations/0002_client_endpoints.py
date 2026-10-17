"""Новая схема private endpoints; frozen execution/secrets constraint различены."""
import uuid
from django.conf import settings
from django.db import migrations, models
from django.db.models import Q
from django.utils import timezone


def identity():
    return [('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
            ('revision', models.PositiveBigIntegerField(default=1))]


def fk(target, **kwargs):
    return models.ForeignKey(target, on_delete=models.PROTECT, **kwargs)


def options(name, *constraints, permissions=None):
    result = {'abstract': False, 'constraints': [models.CheckConstraint(
        condition=Q(revision__gte=1), name=name+'_revision_positive'), *constraints]}
    if permissions is not None:
        result['permissions'] = permissions
    return result


class Migration(migrations.Migration):
    dependencies = [('execution', '0002_receipt_constraint_namespace')]
    operations = [
        migrations.CreateModel(name='ClientSession', fields=identity()+[
            ('owner', fk(settings.AUTH_USER_MODEL)), ('client', fk('catalog.clientregistration')),
            ('external_session_id', models.CharField(max_length=256)), ('display_name', models.CharField(max_length=128)),
        ], options=options('clientsession', models.UniqueConstraint(
            fields=('owner', 'client', 'external_session_id'), name='client_actual_external_identity'))),
        migrations.CreateModel(name='Endpoint', fields=identity()+[
            ('session', fk('execution.clientsession')), ('project', fk('catalog.project')),
            ('address_id', models.UUIDField(default=uuid.uuid4, unique=True, editable=False)),
            ('expires_at', models.DateTimeField()), ('retired', models.BooleanField(default=False)),
        ], options=options('endpoint', models.UniqueConstraint(fields=('session', 'project'), name='endpoint_session_project'))),
        migrations.CreateModel(name='ConnectionLease', fields=identity()+[
            ('endpoint', models.OneToOneField('execution.endpoint', on_delete=models.PROTECT)),
            ('epoch', models.UUIDField()), ('gateway_id', models.UUIDField()),
            ('nonce', models.UUIDField(default=uuid.uuid4)), ('generation', models.PositiveBigIntegerField(default=1)),
            ('credential_id', models.UUIDField()), ('principal_id', models.UUIDField()),
            ('expires_at', models.DateTimeField()), ('connected', models.BooleanField(default=True)),
        ], options=options('connectionlease', models.CheckConstraint(
            condition=Q(generation__gte=1), name='endpoint_connection_positive_generation'))),
        migrations.CreateModel(name='MessageDelivery', fields=identity()+[
            ('endpoint', fk('execution.endpoint')), ('epoch', models.UUIDField()), ('message_id', models.UUIDField()),
            ('operation_request_id', models.UUIDField(null=True)), ('sender_user_id', models.PositiveBigIntegerField()),
            ('sender_principal_id', models.UUIDField()), ('sender_credential_id', models.UUIDField()),
            ('sender_kind', models.CharField(max_length=16)), ('sender_owner_id', models.PositiveBigIntegerField(null=True)),
            ('sender_node_id', models.UUIDField(null=True)), ('sender_grant_id', models.UUIDField(null=True)),
            ('reply_endpoint', fk('execution.endpoint', null=True, related_name='+')),
            ('fingerprint', models.CharField(max_length=64)), ('payload', models.BinaryField(null=True)),
            ('payload_key_id', models.CharField(max_length=64, null=True)), ('payload_nonce', models.BinaryField(null=True)),
            ('priority', models.CharField(max_length=8)), ('notify_when_idle', models.BooleanField(default=False)),
            ('not_after', models.DateTimeField()), ('state', models.CharField(max_length=16, default='queued')),
            ('connection_generation', models.PositiveBigIntegerField(null=True)), ('connection_nonce', models.UUIDField(null=True)),
            ('created_at', models.DateTimeField(default=timezone.now)),
        ], options=options('messagedelivery',
            models.UniqueConstraint(fields=('sender_principal_id', 'message_id'), name='endpoint_message_identity'),
            models.UniqueConstraint(fields=('payload_key_id', 'payload_nonce'), name='endpoint_payload_nonce'),
            models.CheckConstraint(condition=Q(state__in=('queued', 'attempting', 'accepted', 'unknown', 'unavailable')),
                name='endpoint_delivery_known_state'),
            models.CheckConstraint(condition=Q(priority__in=('next', 'now')), name='endpoint_delivery_priority'),
            models.CheckConstraint(condition=Q(state__in=('queued', 'attempting'), payload__isnull=False,
                payload_key_id__isnull=False, payload_nonce__isnull=False) | Q(
                state__in=('accepted', 'unknown', 'unavailable'), payload__isnull=True,
                payload_key_id__isnull=True, payload_nonce__isnull=True), name='endpoint_payload_retention'))),
        migrations.CreateModel(name='DeliveryReceipt', fields=identity()+[
            ('delivery', fk('execution.messagedelivery')), ('kind', models.CharField(max_length=16)),
            ('outcome', models.CharField(max_length=16)), ('connection_generation', models.PositiveBigIntegerField()),
            ('connection_nonce', models.UUIDField()), ('created_at', models.DateTimeField(default=timezone.now)),
        ], options=options('endpoint_deliveryreceipt',
            models.UniqueConstraint(fields=('delivery', 'kind'), name='endpoint_receipt_kind_unique'),
            models.CheckConstraint(condition=Q(kind='delivery', outcome__in=('accepted', 'unknown', 'unavailable')) |
                Q(kind='idle', outcome='observed'), name='endpoint_receipt_known_kind_outcome'))),
    ]
