"""Наблюдения/производные snapshots новой установки, отдельный leaf #626."""
import uuid
from django.conf import settings
from django.db import migrations, models
from django.db.models import Q, F
import django.utils.timezone


class Migration(migrations.Migration):
    dependencies = [('execution', '0001_initial'), ('catalog', '0004_safe_retirement'),
                    migrations.swappable_dependency(settings.AUTH_USER_MODEL)]
    operations = [
        migrations.CreateModel(name='ProjectionRuntime', fields=[
            ('id', models.PositiveSmallIntegerField(default=1, editable=False, primary_key=True, serialize=False)),
            ('epoch', models.UUIDField()), ('run_id', models.UUIDField(default=uuid.uuid4)),
        ], options={'constraints':[models.CheckConstraint(condition=Q(id=1), name='projection_single_runtime')]}),
        migrations.CreateModel(name='ProjectionClock', fields=[
            ('target', models.OneToOneField('execution.assignment', on_delete=models.PROTECT, primary_key=True, serialize=False)),
            ('revision', models.PositiveBigIntegerField(default=0)), ('retired', models.BooleanField(default=False)),
        ]),
        migrations.CreateModel(name='SourceProducer', fields=[
            ('id', models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False, serialize=False)),
            ('revision', models.PositiveBigIntegerField(default=1)),
            ('target', models.ForeignKey('execution.assignment', on_delete=models.PROTECT)),
            ('kind', models.CharField(max_length=16)),
            ('incarnation', models.ForeignKey('execution.executionincarnation', on_delete=models.PROTECT)),
            ('executor_epoch', models.UUIDField()), ('epoch', models.UUIDField()),
        ], options={'abstract':False, 'constraints':[
            models.CheckConstraint(condition=Q(revision__gte=1), name='sourceproducer_revision_positive'),
            models.UniqueConstraint(fields=('target','kind'), name='projection_current_producer'),
            models.CheckConstraint(condition=Q(kind__in=('state','clone','size')), name='projection_producer_kind'),
        ]}),
        migrations.CreateModel(name='ProducerEpoch', fields=[
            ('id', models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False, serialize=False)),
            ('revision', models.PositiveBigIntegerField(default=1)),
            ('target', models.ForeignKey('execution.assignment', on_delete=models.PROTECT)),
            ('kind', models.CharField(max_length=16)), ('epoch', models.UUIDField()),
        ], options={'abstract':False, 'constraints':[
            models.CheckConstraint(condition=Q(revision__gte=1), name='producerepoch_revision_positive'),
            models.UniqueConstraint(fields=('target','kind','epoch'), name='projection_producer_epoch_never_reused'),
        ]}),
        migrations.CreateModel(name='SourceObservation', fields=[
            ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
            ('target', models.ForeignKey('execution.assignment', on_delete=models.PROTECT)),
            ('incarnation', models.ForeignKey('execution.executionincarnation', on_delete=models.PROTECT)),
            ('producer_epoch', models.UUIDField()), ('controller_run', models.UUIDField()),
            ('kind', models.CharField(max_length=16)), ('sequence', models.PositiveBigIntegerField()),
            ('observed_at', models.DateTimeField()), ('received_at', models.DateTimeField(default=django.utils.timezone.now)),
            ('status', models.CharField(max_length=16)), ('reason', models.CharField(max_length=32, blank=True)),
            ('payload', models.JSONField(default=dict)),
        ], options={'db_table':'projection_observation',
            'permissions':[('ingest_observations', 'Can ingest enrolled node observations')], 'constraints':[
            models.UniqueConstraint(fields=('target','kind'), name='projection_source_target_kind'),
            models.CheckConstraint(condition=Q(sequence__gte=1), name='projection_source_positive_sequence'),
            models.CheckConstraint(condition=Q(kind__in=('state','clone','size')), name='projection_source_kind'),
            models.CheckConstraint(condition=Q(status__in=('fresh','stale','unavailable')), name='projection_source_status'),
            models.CheckConstraint(condition=Q(observed_at__lte=F('received_at')), name='projection_source_time_order'),
        ]}),
        migrations.CreateModel(name='ProjectSequence', fields=[
            ('project', models.OneToOneField('catalog.project', on_delete=models.PROTECT, primary_key=True, serialize=False)),
            ('position', models.PositiveBigIntegerField(default=0)),
        ]),
        migrations.CreateModel(name='ProjectionEvent', fields=[
            ('id', models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False, serialize=False)),
            ('project', models.ForeignKey('catalog.project', on_delete=models.PROTECT)),
            ('position', models.PositiveBigIntegerField()), ('epoch', models.UUIDField()),
            ('target_id', models.UUIDField()), ('incarnation_id', models.UUIDField(null=True)),
            ('revision', models.PositiveBigIntegerField()), ('deleted', models.BooleanField(default=False)),
            ('created_at', models.DateTimeField(default=django.utils.timezone.now)),
        ], options={'abstract':False, 'constraints':[
            models.CheckConstraint(condition=Q(revision__gte=1), name='projectionevent_revision_positive'),
            models.UniqueConstraint(fields=('project','position'), name='projection_event_project_position')]}),
        migrations.CreateModel(name='ReadSnapshot', fields=[
            ('id', models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False, serialize=False)),
            ('user', models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)),
            ('binding', models.CharField(max_length=64)), ('epoch', models.UUIDField()),
            ('run_id', models.UUIDField(null=True)), ('scope', models.CharField(max_length=64)),
            ('cursor', models.TextField()), ('rows', models.JSONField()), ('expires_at', models.DateTimeField()),
        ]),
    ]
