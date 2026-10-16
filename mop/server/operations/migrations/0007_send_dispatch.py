from django.db import migrations, models
from django.db.models import Q
import django.db.models.deletion
import uuid


class Migration(migrations.Migration):
    dependencies = [('operations', '0006_remove_operation_operation_finite_action_and_more')]
    operations = [
        migrations.RemoveConstraint(model_name='operation', name='operation_finite_action'),
        migrations.RemoveConstraint(model_name='operation', name='operation_typed_target'),
        migrations.RemoveConstraint(model_name='step', name='step_finite_handler'),
        migrations.AddConstraint(model_name='operation', constraint=models.CheckConstraint(
            condition=Q(action__in=('drain', 'enable', 'restart', 'build', 'send', 'dispatch')), name='operation_finite_action')),
        migrations.AddConstraint(model_name='operation', constraint=models.CheckConstraint(condition=(
            Q(target_kind='node', action__in=('drain', 'enable', 'build'), eligible__isnull=False,
              target_id__isnull=True, incarnation_id__isnull=True) |
            Q(target_kind='puppet', action__in=('restart', 'send', 'dispatch'), eligible__isnull=True,
              target_id__isnull=False, incarnation_id__isnull=False)), name='operation_typed_target')),
        migrations.AddConstraint(model_name='step', constraint=models.CheckConstraint(condition=Q(name__in=(
            'node_eligibility', 'apply_identity', 'restart_session', 'confirm_ready', 'image_build', 'send_message')),
            name='step_finite_handler')),
        migrations.CreateModel(name='MessageBinding', fields=[
            ('operation', models.OneToOneField(primary_key=True, serialize=False,
                on_delete=django.db.models.deletion.PROTECT, to='operations.operation')),
            ('message_id', models.UUIDField(default=uuid.uuid4, unique=True)),
            ('ciphertext', models.BinaryField()),
            ('priority', models.CharField(max_length=8)),
            ('wait_seconds', models.PositiveSmallIntegerField(default=0)),
            ('notify_when_idle', models.BooleanField(default=False)),
            ('holder_endpoint', models.UUIDField(null=True)),
            ('holder_connection', models.UUIDField(null=True)),
        ], options={'default_permissions': (), 'constraints': [
            models.CheckConstraint(condition=Q(priority__in=('now', 'next', 'later')), name='message_finite_priority'),
            models.CheckConstraint(condition=Q(wait_seconds__lte=60), name='message_bounded_wait'),
            models.CheckConstraint(condition=Q(holder_endpoint__isnull=True, holder_connection__isnull=True)
                | Q(holder_endpoint__isnull=False, holder_connection__isnull=False), name='message_complete_holder')]}),
    ]
