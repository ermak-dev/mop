"""Телеметрия #641; отдельный leaf от endpoint-миграции #649."""
import uuid
from django.conf import settings
from django.db import migrations, models
from django.db.models import Q
from django.utils import timezone


def identity():
    return [('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
            ('revision', models.PositiveBigIntegerField(default=1))]


def event_identity(name):
    return [('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
        ('observation', models.ForeignKey('execution.sessionobservation', on_delete=models.CASCADE)),
        ('sequence', models.PositiveBigIntegerField()),
        ('received_at', models.DateTimeField(default=timezone.now, db_index=True))]


def event_options(prefix):
    return {'constraints': [models.UniqueConstraint(fields=('observation', 'sequence'), name=prefix+'_sequence_identity'),
        models.CheckConstraint(condition=Q(sequence__gte=1), name=prefix+'_sequence_positive')]}


class Migration(migrations.Migration):
    dependencies = [('execution', '0001_initial'), migrations.swappable_dependency(settings.AUTH_USER_MODEL)]
    operations = [
        migrations.CreateModel(name='SessionObservation', fields=identity()+[
            ('assignment', models.ForeignKey('execution.assignment', on_delete=models.CASCADE)),
            ('incarnation', models.ForeignKey('execution.executionincarnation', on_delete=models.CASCADE)),
            ('node', models.ForeignKey('catalog.node', on_delete=models.PROTECT)),
            ('epoch', models.UUIDField()), ('executor_epoch', models.UUIDField()), ('producer_epoch', models.UUIDField()),
            ('session_id', models.CharField(max_length=128)),
            ('sequence', models.PositiveBigIntegerField(default=0)), ('event', models.CharField(max_length=32, default='')),
            ('incomplete', models.BooleanField(default=False)), ('gap_count', models.PositiveBigIntegerField(default=0)),
            ('confirmed_model', models.CharField(max_length=128, null=True)),
            ('confirmed_sequence', models.PositiveBigIntegerField(default=0)),
            ('model_hint', models.CharField(max_length=128, null=True)), ('model_proven', models.BooleanField(default=False)),
            ('model_received_at', models.DateTimeField(null=True)),
            ('received_at', models.DateTimeField(default=timezone.now, db_index=True)),
        ], options={'abstract': False,
            'permissions': [('ingest_telemetry', 'Can ingest enrolled node telemetry'), ('read_history', 'Can read private session history')],
            'constraints': [models.CheckConstraint(condition=Q(revision__gte=1), name='sessionobservation_revision_positive'),
                models.UniqueConstraint(fields=('incarnation', 'producer_epoch'), name='telemetry_producer_identity')]}),
        migrations.CreateModel(name='TelemetryReceipt', fields=event_identity('telemetry')+[
            ('digest', models.CharField(max_length=64)),
        ], options={'constraints': [models.UniqueConstraint(fields=('observation', 'sequence'), name='telemetry_sequence_identity'),
            models.CheckConstraint(condition=Q(sequence__gte=1), name='telemetry_receipt_sequence_positive')]}),
        migrations.CreateModel(name='TurnOutcome', fields=event_identity('turn')+[
            ('turn_id', models.UUIDField(null=True)), ('event', models.CharField(max_length=32)),
            ('error', models.CharField(max_length=64, null=True)), ('initiator_user_id', models.PositiveBigIntegerField(null=True)),
            ('initiator_principal_id', models.UUIDField(null=True)), ('initiator_endpoint_id', models.UUIDField(null=True)),
            ('initiator_session_id', models.UUIDField(null=True)),
            ('operation_id', models.UUIDField(null=True)), ('message_id', models.UUIDField(null=True)),
        ], options=event_options('turn')),
        migrations.CreateModel(name='ModelObservation', fields=event_identity('model')+[
            ('model', models.CharField(max_length=128)), ('kind', models.CharField(max_length=32)),
            ('causal_switch', models.PositiveBigIntegerField(null=True)),
        ], options=event_options('model')),
        migrations.CreateModel(name='PrivateHistoryCursor', fields=identity()+[
            ('user', models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)),
            ('principal_id', models.UUIDField()),
            ('observation', models.ForeignKey('execution.sessionobservation', on_delete=models.CASCADE)),
            ('cursor', models.CharField(max_length=8192, default='')), ('source_epoch', models.CharField(max_length=128, default='')),
            ('updated_at', models.DateTimeField(default=timezone.now, db_index=True)),
        ], options={'abstract': False, 'constraints': [models.CheckConstraint(condition=Q(revision__gte=1), name='privatehistorycursor_revision_positive'),
            models.UniqueConstraint(fields=('principal_id', 'observation'), name='history_private_cursor_scope')]}),
        migrations.AddField(model_name='sessionobservation', name='turn_source',
            field=models.ForeignKey('execution.turnoutcome', null=True, related_name='+', on_delete=models.SET_NULL)),
    ]
