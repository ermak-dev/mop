"""#643: create в конечном журнале -- действие, шаги и снимок допуска."""
import django.db.models.deletion
from django.db import migrations, models
from django.db.models import Q


class Migration(migrations.Migration):
    dependencies = [('operations', '0010_delete')]
    operations = [
        migrations.RemoveConstraint(model_name='operation', name='operation_finite_action'),
        migrations.RemoveConstraint(model_name='operation', name='operation_typed_target'),
        migrations.RemoveConstraint(model_name='step', name='step_finite_handler'),
        migrations.AddConstraint(model_name='operation', constraint=models.CheckConstraint(
            condition=Q(action__in=('drain', 'enable', 'restart', 'build', 'stop_job', 'send', 'dispatch', 'wipe',
                                    'delete', 'create')),
            name='operation_finite_action')),
        migrations.AddConstraint(model_name='operation', constraint=models.CheckConstraint(condition=(
            Q(target_kind='node', action__in=('drain', 'enable', 'build'), eligible__isnull=False,
              target_id__isnull=True, incarnation_id__isnull=True) |
            Q(target_kind='puppet', action__in=('restart', 'stop_job', 'send', 'dispatch', 'wipe', 'delete', 'create'),
              eligible__isnull=True, target_id__isnull=False, incarnation_id__isnull=False)), name='operation_typed_target')),
        migrations.AddConstraint(model_name='step', constraint=models.CheckConstraint(condition=Q(name__in=(
            'node_eligibility', 'apply_identity', 'restart_session', 'confirm_ready', 'image_build', 'scheduler_stop',
            'send_message', 'wipe_clone', 'scheduler_delete', 'scheduler_register', 'activate_grant')),
            name='step_finite_handler')),
        migrations.CreateModel(name='CreateBinding', fields=[
            ('operation', models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, primary_key=True,
                                               serialize=False, to='operations.operation')),
            ('definition_id', models.UUIDField()),
            ('scheduler_id', models.UUIDField()),
            ('job_id', models.CharField(max_length=128)),
            ('namespace', models.CharField(max_length=128)),
            ('applied_index', models.PositiveBigIntegerField(null=True)),
        ], options={'default_permissions': ()}),
    ]
