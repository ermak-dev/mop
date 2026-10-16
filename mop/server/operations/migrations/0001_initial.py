"""Durable operations в новой БД; единственный canonical catalog dependency."""
import uuid
import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True
    dependencies = [migrations.swappable_dependency(settings.AUTH_USER_MODEL),
                    ('catalog', '0002_action_permissions'), ('controller', '0002_restore_guard')]
    operations = [
        migrations.CreateModel(name='Operation', fields=[
            ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
            ('actor_binding', models.JSONField()), ('request_id', models.UUIDField()),
            ('digest', models.CharField(max_length=64)), ('epoch', models.UUIDField()),
            ('restore_generation', models.UUIDField()), ('action', models.CharField(max_length=32)),
            ('expected_revision', models.PositiveBigIntegerField()), ('eligible', models.BooleanField()),
            ('not_after', models.DateTimeField()), ('state', models.CharField(default='accepted', max_length=16)),
            ('revision', models.PositiveBigIntegerField(default=1)), ('code', models.CharField(blank=True, max_length=64)),
            ('created_at', models.DateTimeField(default=django.utils.timezone.now)),
            ('completed_at', models.DateTimeField(null=True)), ('result_pruned', models.BooleanField(default=False)),
            ('actor', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to=settings.AUTH_USER_MODEL)),
            ('node', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to='catalog.node')),
            ('projects', models.ManyToManyField(to='catalog.project')),
        ], options={'permissions': [('cancel_operation', 'Can cancel operation'), ('recover_operation', 'Can recover operation'), ('view_audit', 'Can view audit')]}),
        migrations.CreateModel(name='Resource', fields=[
            ('key', models.CharField(max_length=80, primary_key=True, serialize=False)),
        ], options={'default_permissions': ()}),
        migrations.CreateModel(name='Step', fields=[
            ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
            ('name', models.CharField(default='node_eligibility', max_length=32)), ('token', models.UUIDField(null=True)),
            ('started_at', models.DateTimeField(null=True)), ('lease_until', models.DateTimeField(null=True)),
            ('quiescent_at', models.DateTimeField(null=True)),
            ('operation', models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, to='operations.operation')),
        ], options={'default_permissions': ()}),
        migrations.CreateModel(name='ResourceClaim', fields=[
            ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
            ('token', models.UUIDField()), ('acquired_at', models.DateTimeField(default=django.utils.timezone.now)),
            ('released_at', models.DateTimeField(null=True)),
            ('operation', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to='operations.operation')),
            ('step', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to='operations.step')),
            ('resource', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to='operations.resource')),
        ], options={'default_permissions': ()}),
        migrations.CreateModel(name='EffectReceipt', fields=[
            ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
            ('token', models.UUIDField()), ('epoch', models.UUIDField()),
            ('source_revision', models.PositiveBigIntegerField()), ('decision_revision', models.PositiveBigIntegerField()),
            ('applied', models.BooleanField()),
            ('received_at', models.DateTimeField(default=django.utils.timezone.now)),
            ('node', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to='catalog.node')),
            ('step', models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, to='operations.step')),
        ], options={'abstract': False, 'default_permissions': ()}),
        migrations.CreateModel(name='AuditEvent', fields=[
            ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
            ('revision', models.PositiveBigIntegerField()), ('target_revision', models.PositiveBigIntegerField()),
            ('state', models.CharField(max_length=16)),
            ('code', models.CharField(max_length=64)), ('created_at', models.DateTimeField(default=django.utils.timezone.now)),
            ('operation', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to='operations.operation')),
        ], options={'abstract': False, 'default_permissions': ()}),
        migrations.CreateModel(name='OutboxEvent', fields=[
            ('id', models.BigAutoField(primary_key=True, serialize=False)),
            ('event_id', models.UUIDField(default=uuid.uuid4, unique=True)),
            ('revision', models.PositiveBigIntegerField()), ('target_revision', models.PositiveBigIntegerField()),
            ('state', models.CharField(max_length=16)),
            ('code', models.CharField(max_length=64)), ('created_at', models.DateTimeField(default=django.utils.timezone.now)),
            ('published_at', models.DateTimeField(null=True)),
            ('operation', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to='operations.operation')),
        ], options={'default_permissions': ()}),
        migrations.AddConstraint(model_name='operation', constraint=models.UniqueConstraint(fields=('actor', 'request_id'), name='operation_actor_request_unique')),
        migrations.AddConstraint(model_name='operation', constraint=models.CheckConstraint(condition=models.Q(revision__gte=1, expected_revision__gte=1), name='operation_positive_revisions')),
        migrations.AddConstraint(model_name='operation', constraint=models.CheckConstraint(condition=models.Q(state__in=('accepted', 'running', 'reconciling', 'succeeded', 'refused', 'failed', 'partial', 'unknown', 'expired', 'cancelled')), name='operation_finite_state')),
        migrations.AddConstraint(model_name='operation', constraint=models.CheckConstraint(condition=models.Q(action__in=('drain', 'enable')), name='operation_finite_action')),
        migrations.AddConstraint(model_name='operation', constraint=models.CheckConstraint(condition=models.Q(result_pruned=False) | models.Q(completed_at__isnull=False), name='operation_pruned_terminal')),
        migrations.AddConstraint(model_name='step', constraint=models.CheckConstraint(condition=models.Q(name='node_eligibility'), name='step_finite_handler')),
        migrations.AddConstraint(model_name='resourceclaim', constraint=models.UniqueConstraint(fields=('resource',), condition=models.Q(released_at__isnull=True), name='one_unresolved_resource_claim')),
        migrations.AddConstraint(model_name='auditevent', constraint=models.UniqueConstraint(fields=('operation', 'revision'), name='operation_audit_revision_unique')),
        migrations.AddConstraint(model_name='outboxevent', constraint=models.UniqueConstraint(fields=('operation', 'revision'), name='operation_outbox_revision_unique')),
        migrations.AddIndex(model_name='operation', index=models.Index(fields=('state', 'created_at'), name='operations__state_b33956_idx')),
        migrations.AddIndex(model_name='outboxevent', index=models.Index(fields=('published_at', 'id'), name='operations__publish_4d4573_idx')),
    ]
