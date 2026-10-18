"""#625: preparation в том же конечном журнале, без допуска cutover."""
import django.db.models.deletion
from django.db import migrations, models
from django.db.models import Q


class Migration(migrations.Migration):
    dependencies = [('operations', '0015_create_admission_capacity')]
    operations = [
        migrations.RemoveConstraint(model_name='operation', name='operation_finite_action'),
        migrations.RemoveConstraint(model_name='operation', name='operation_typed_target'),
        migrations.RemoveConstraint(model_name='step', name='step_finite_handler'),
        migrations.AddConstraint(model_name='operation', constraint=models.CheckConstraint(
            condition=Q(action__in=('drain', 'enable', 'restart', 'build', 'stop_job', 'send', 'dispatch', 'wipe',
                                    'delete', 'create', 'update')), name='operation_finite_action')),
        migrations.AddConstraint(model_name='operation', constraint=models.CheckConstraint(condition=(
            Q(target_kind='node', action__in=('drain', 'enable', 'build'), eligible__isnull=False,
              target_id__isnull=True, incarnation_id__isnull=True) |
            Q(target_kind='puppet', action__in=('restart', 'stop_job', 'send', 'dispatch', 'wipe', 'delete', 'create', 'update'),
              eligible__isnull=True, target_id__isnull=False, incarnation_id__isnull=False)), name='operation_typed_target')),
        migrations.AddConstraint(model_name='step', constraint=models.CheckConstraint(condition=Q(name__in=(
            'node_eligibility', 'apply_identity', 'restart_session', 'confirm_ready', 'image_build', 'scheduler_stop',
            'send_message', 'wipe_clone', 'scheduler_delete', 'scheduler_register', 'activate_grant', 'prepare_launch')),
            name='step_finite_handler')),
        migrations.CreateModel(name='ReplacementBinding', fields=[
            ('operation', models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, primary_key=True,
                                               serialize=False, to='operations.operation')),
            ('source_incarnation_id', models.UUIDField()),
            ('source_grant_id', models.UUIDField()),
            ('source_revision', models.PositiveBigIntegerField()),
            ('source_generation', models.PositiveBigIntegerField()),
            ('source_expires_at', models.DateTimeField()),
            ('expires_at', models.DateTimeField()),
            ('definition_id', models.UUIDField()),
            ('policy_approval_id', models.UUIDField()),
            ('policy_version_id', models.UUIDField()),
            ('policy_digest', models.CharField(max_length=64)),
        ], options={'default_permissions': (), 'constraints': [models.CheckConstraint(
            condition=Q(source_revision__gte=1, source_generation__gte=1) & Q(expires_at__lte=models.F('source_expires_at')),
            name='replacement_source_pins')]}),
    ]
