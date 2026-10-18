"""Текущий producer и журнал одноразовых эпох (#641/#643)."""
import uuid
from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):
    dependencies = [('execution', '0012_launch_read_permission')]
    operations = [
        migrations.CreateModel(name='SessionProducer', fields=[
            ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
            ('revision', models.PositiveBigIntegerField(default=1)),
            ('epoch', models.UUIDField()),
            ('executor_epoch', models.UUIDField()),
            ('producer_epoch', models.UUIDField(unique=True)),
            ('session_id', models.CharField(max_length=128)),
            ('executor_user_id', models.PositiveBigIntegerField()),
            ('owner_id', models.PositiveBigIntegerField()),
            ('generation', models.PositiveBigIntegerField()),
            ('assignment_revision', models.PositiveBigIntegerField()),
            ('role', models.CharField(max_length=64)),
            ('bundle_digest', models.CharField(max_length=64)),
            ('grant_id', models.UUIDField()),
            ('operation_id', models.UUIDField()),
            ('step_id', models.UUIDField()),
            ('attempt_id', models.UUIDField()),
            ('proof_digest', models.CharField(max_length=64)),
            ('request_digest', models.CharField(max_length=64)),
            ('accepted_revision', models.PositiveBigIntegerField()),
            ('pane', models.CharField(max_length=11)),
            ('pid', models.PositiveIntegerField()),
            ('process_start', models.CharField(max_length=56)),
            ('registered_at', models.DateTimeField(default=django.utils.timezone.now)),
            ('assignment', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to='execution.assignment')),
            ('incarnation', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to='execution.executionincarnation')),
            ('node', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to='catalog.node')),
        ], options={'default_permissions': (), 'abstract': False,
            'constraints': [models.CheckConstraint(condition=models.Q(revision__gte=1), name='sessionproducer_revision_positive'),
                models.UniqueConstraint(fields=('incarnation', 'accepted_revision'), name='producer_incarnation_revision'),
                models.CheckConstraint(condition=models.Q(accepted_revision__gte=1, generation__gte=1,
                    assignment_revision__gte=1, pid__gte=1), name='producer_positive_counters')]}),
        migrations.CreateModel(name='CurrentProducer', fields=[
            ('incarnation', models.OneToOneField(on_delete=django.db.models.deletion.PROTECT,
                primary_key=True, serialize=False, to='execution.executionincarnation')),
            ('revision', models.PositiveBigIntegerField(default=1)),
            ('producer', models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, to='execution.sessionproducer')),
        ], options={'default_permissions': (),
            'permissions': [('register_producer', 'Can register own admitted session producer')],
            'constraints': [models.CheckConstraint(condition=models.Q(revision__gte=1), name='current_producer_revision_positive')]}),
    ]
